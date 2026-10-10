"""
conftest.py — shared pytest setup for the chatbot scenario tests.

Sets up:
  - AWS Bedrock configuration for Scenario judge/user-simulator

Per-scenario test data (bill fields, conversation turns, judge criteria) is
NOT loaded here - tests/test_scenario.py discovers and reads its JSON files
directly from test_data/ at collection time. PDFs are never touched at test
run time; utils/bulk_pdf_extractor.py (PDF -> bill_data) and
utils/generate_test_data.py (bill_data -> scenario JSON) produce the
test_data/*.json files offline, ahead of time - see tests/test_scenario.py's
module docstring for the full schema.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

try:
    from tqdm import tqdm
except ImportError:  # pragma: no cover - tqdm is a listed dependency
    tqdm = None

# ── Load environment variables ────────────────────────────────────────────────
load_dotenv(Path(__file__).parent / ".env")

# LiteLLM otherwise performs a remote pricing-map fetch in every xdist worker
# before pytest can collect a single case. Its bundled map is sufficient here.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "true")

from utils.aws_config import configure_aws_credentials
from agents.argos_agent import ArgosAccessDeniedError

aws_llm_config = configure_aws_credentials()

# Import after credential resolution so Scenario/LiteLLM see the selected source.
import scenario

# ── Scenario global config ────────────────────────────────────────────────────
# Use AWS Bedrock Claude Sonnet 4.5 as the default model for user-simulator
# and judge agents.
scenario.configure(
    default_model=aws_llm_config.model,
    # cache_key="chatbot-v1",  # uncomment to make runs deterministic
)

# ── "Test cases" progress bar ─────────────────────────────────────────────────
# Session-wide, advances once per completed test (any outcome). Deliberately
# NOT shown under pytest-xdist (run_tests.py -n): each worker is a separate
# process running a different subset, and their tqdm bars would write
# competing \r-based updates to the same shared terminal, producing garbled
# output rather than a useful progress indicator. For xdist runs, pytest's
# own per-test PASSED/FAILED/SKIPPED lines (addopts -v) are the progress
# signal instead; this bar is for direct `pytest ...` (sequential) runs.
_session_pbar = None


def pytest_collection_modifyitems(session, config, items):
    global _session_pbar
    scenario_timeout = float(os.getenv("SCENARIO_TIMEOUT_S", "420"))
    for item in items:
        if item.nodeid.startswith("tests/test_scenario.py") or item.nodeid.startswith(
            "tests/test_argos_scenarios.py"
        ):
            item.add_marker(pytest.mark.timeout(scenario_timeout))

    if tqdm is None:
        return
    is_xdist_worker = hasattr(config, "workerinput")
    is_xdist_controller = getattr(config.option, "numprocesses", None) not in (None, 0)
    if is_xdist_worker or is_xdist_controller:
        return
    _session_pbar = tqdm(total=len(items), desc="Test cases", unit="test")


def pytest_runtest_logreport(report):
    if _session_pbar is not None and report.when == "call":
        _session_pbar.update(1)


def pytest_exception_interact(node, call, report):
    """Stop scheduling scenarios after an Akamai infrastructure block.

    Retrying every scenario from the same IP cannot repair a server-side
    denial and can lengthen its cooldown. The first failed scenario still
    writes its screenshot/video/report before this hook stops the session.
    """
    if call.excinfo and isinstance(call.excinfo.value, ArgosAccessDeniedError):
        message = (
            "Argos/Akamai blocked this test IP. The remaining scenarios were "
            "not started to avoid extending the block. Verify the site in normal "
            "Chrome on this network or use an Argos-approved allowlisted test URL/IP."
        )
        node.session.shouldstop = message
        print(f"\n[run_tests.py] BLOCKED: {message}", flush=True)


def pytest_sessionfinish(session, exitstatus):
    if _session_pbar is not None:
        _session_pbar.close()
