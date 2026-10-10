#!/usr/bin/env python3
"""
run_tests.py — parallel entrypoint for the scenario test suite.

Accounts are independent (separate browser + Bedrock calls per test), so
they run concurrently via pytest-xdist (-n) instead of one after another.

pytest.ini's `timeout`/`timeout_method = signal` (pytest-timeout) fails an
individual hung test quickly and prints a thread dump. That is NOT enough to
guarantee the whole run finishes: a stuck background thread (scenario.arun()
still delegates the actual LLM call to litellm, which can itself wedge a
thread on a genuinely unresponsive endpoint) is something Python cannot
forcibly stop - CPython's concurrent.futures registers an atexit hook that
joins every ThreadPoolExecutor worker before the process can exit, so a
single stuck account can keep the whole pytest process (or an xdist worker)
alive indefinitely even after pytest has already reported that test as
FAILED. Confirmed via a live repro on the older scenario.run() path: pytest-
timeout printed "FAILED" at the 90s mark but the process itself needed a
hard kill 57 minutes later.

The watchdog below is what actually guarantees this script's own process
always terminates: it hard-kills the whole pytest process group (and any
leftover Playwright browsers) if the wall-clock budget is exceeded,
regardless of what's stuck inside.

Config - read from .env (loaded here via python-dotenv, same as conftest.py)
or the shell environment, shell environment wins:
    TEST_WORKERS         pytest-xdist worker count (default 4)
    PYTEST_WALL_CLOCK_S  hard-kill budget in seconds (default 3600)

Usage mirrors the old run_tests.sh - any extra arguments pass straight
through to pytest:
    .venv/bin/python run_tests.py
    .venv/bin/python run_tests.py -k bill_verification
    .venv/bin/python run_tests.py -k kkwpjowom652
    TEST_WORKERS=8 .venv/bin/python run_tests.py
See command.csv for the full command reference (tests/test_scenario.py is
the only test file now - target a family/sub-kind/case via -k or -m, not by
file path).
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "true")

WORKERS = os.environ.get("TEST_WORKERS", "2")
WALL_CLOCK_S = int(os.environ.get("PYTEST_WALL_CLOCK_S", "3600"))

# pytest-rerunfailures can be present as an indirect dependency even though this
# suite neither configures nor uses it.  With xdist, that plugin starts a local
# TCP status server during pytest_configure.  Sandboxed runners that disallow
# socket.bind() then fail before collection with PermissionError.  Disable only
# that unused plugin; xdist, pytest-timeout, and pytest-asyncio remain enabled.
DISABLED_PYTEST_PLUGINS = ("rerunfailures",)


def build_pytest_command(argv: list[str]) -> list[str]:
    """Build the child command without auto-loading incompatible plugins."""
    disabled_plugins = [
        option
        for plugin in DISABLED_PYTEST_PLUGINS
        for option in ("-p", f"no:{plugin}")
    ]
    return [
        sys.executable,
        "-m",
        "pytest",
        *disabled_plugins,
        "-n",
        WORKERS,
        *argv,
    ]


def _kill_leftover_browsers() -> None:
    subprocess.run(
        ["pkill", "-9", "-f", "ms-playwright/chromium"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def build_reports(started: str) -> None:
    """Post-test reporting hook for Argos scenarios. Aggregates results, PDFs, and MP4s."""
    import json
    from datetime import datetime

    reports_dir = ROOT / "reports"
    if not reports_dir.exists():
        return

    result_files = list(reports_dir.glob("*/latest/result.json"))
    if not result_files:
        # Fallback: scan timestamped directories
        result_files = list(reports_dir.glob("*/*/result.json"))

    if not result_files:
        return

    total = 0
    passes = 0
    warns = 0
    fails = 0
    rows = []

    for rf in sorted(result_files):
        try:
            data = json.loads(rf.read_text())
            scenario_name = data.get("scenario", rf.parent.parent.name)
            case_id = data.get("case", "")
            overall_status = data.get("overall_status", "UNKNOWN")
            pdf_path = rf.parent / "evidence_report.pdf"
            mp4_file = data.get("video_file") or "execution_video.mp4"
            mp4_path = rf.parent / mp4_file

            total += 1
            if overall_status == "PASS":
                passes += 1
                status_badge = "🟢 PASS"
            elif overall_status == "WARN with PASS":
                warns += 1
                status_badge = "🟡 WARN with PASS"
            else:
                fails += 1
                status_badge = "🔴 FAIL"

            pdf_link = str(pdf_path.relative_to(ROOT)) if pdf_path.exists() else "N/A"
            mp4_link = str(mp4_path.relative_to(ROOT)) if mp4_path.exists() else "N/A"

            rows.append((case_id, scenario_name, status_badge, pdf_link, mp4_link))
        except Exception:
            continue

    summary_content = [
        f"# Argos 'Ask Trevor' Execution & Evaluation Summary",
        f"**Run Started:** {started} | **Run Finished:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"",
        f"### Metrics",
        f"- **Total Scenarios Evaluated:** {total}",
        f"- **Passed (Strict):** {passes}",
        f"- **Passed with Warnings (WARN with PASS):** {warns}",
        f"- **Failed:** {fails}",
        f"- **Success Rate:** {(passes + warns) / total * 100:.1f}%" if total else "",
        f"",
        f"### Scenario Reports & Video Evidence",
        f"| Case ID | Scenario | Overall Status | PDF Report | MP4 Video |",
        f"|---|---|---|---|---|",
    ]
    for cid, sname, badge, pdf, mp4 in rows:
        summary_content.append(f"| {cid} | {sname} | {badge} | `{pdf}` | `{mp4}` |")

    summary_file = reports_dir / "summary.md"
    summary_file.write_text("\n".join(summary_content))

    print("\n" + "=" * 70)
    print("ARGOS 'ASK TREVOR' EVALUATION SUITE SUMMARY")
    print("=" * 70)
    print(f"Total: {total} | Pass: {passes} | Warn with Pass: {warns} | Fail: {fails}")
    print(f"Summary saved to: {summary_file}")
    print("=" * 70 + "\n")




def main(argv: list[str]) -> int:
    os.chdir(ROOT)
    # Resolve a Secrets Manager credential once in the controller process;
    # all pytest-xdist workers inherit the selected credentials and marker.
    from utils.aws_config import configure_aws_credentials

    llm_config = configure_aws_credentials()
    print(
        f"[run_tests.py] LLM credentials: {llm_config.credential_source}; "
        f"model: {llm_config.model}; region: {llm_config.region}",
        flush=True,
    )
    from datetime import datetime
    started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cmd = build_pytest_command(argv)

    case_count = len(list((ROOT / "test_data" / "argos").glob("*.yaml")))
    print(
        f"[run_tests.py] Collected scenario files: {case_count}; workers: {WORKERS}. "
        "Cases above the worker count remain queued until a worker finishes.",
        flush=True,
    )

    # start_new_session=True makes this child (and everything it spawns -
    # xdist workers, Playwright browsers) its own process group, so a single
    # os.killpg() below reaches all of them, not just the immediate pytest
    # process.
    proc = subprocess.Popen(cmd, start_new_session=True)

    finished = threading.Event()

    def watchdog() -> None:
        if finished.wait(WALL_CLOCK_S):
            return  # process finished on its own before the budget ran out
        print(
            f"[run_tests.py] wall-clock budget ({WALL_CLOCK_S}s) exceeded - "
            "force-killing pytest and any leftover browsers",
            file=sys.stderr,
        )
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass  # already gone
        _kill_leftover_browsers()

    watchdog_thread = threading.Thread(target=watchdog, daemon=True)
    watchdog_thread.start()

    try:
        exit_code = proc.wait()
    except KeyboardInterrupt:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGINT)
        except ProcessLookupError:
            pass
        exit_code = proc.wait()
    finally:
        finished.set()

    build_reports(started)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
