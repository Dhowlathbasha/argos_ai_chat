"""
tests/test_scenario.py
───────────────────────
Data-driven Playwright scenario runner for Argos 'Ask Trevor' AI Chat.
Discovers all YAML scenarios in test_data/argos/*.yaml and executes multi-turn
conversational sessions with:
- Playwright video recording and automatic MP4 transcoding
- Per-turn screenshot capture and timestamping
- 3-tier judging evaluation (PASS / FAIL / WARN with PASS)
- Comprehensive PDF evidence report generation
"""

from __future__ import annotations

import sys
from pathlib import Path
import pytest
import yaml
import scenario

sys.path.insert(0, str(Path(__file__).parent.parent))
from agents.argos_agent import ArgosTrevorAgent
from utils.follow_up_generator import DynamicFollowUpGenerator
from utils.evidence import EvidenceRecorder
from utils.judge import ArgosJudgeEngine
from utils.video_transcoder import transcode_webm_to_mp4

SCENARIOS_DIR = Path(__file__).parent.parent / "test_data" / "argos"


def discover_scenarios():
    """Discover all YAML scenario files under test_data/argos/."""
    if not SCENARIOS_DIR.exists():
        return []
    return [pytest.param(p, id=p.stem) for p in sorted(SCENARIOS_DIR.glob("*.yaml"))]


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario_file", discover_scenarios())
async def test_scenario(scenario_file: Path):
    """Run an automated conversation scenario against the live Ask Trevor widget."""
    with open(scenario_file, "r") as f:
        data = yaml.safe_load(f)

    scenario_id = data.get("id", scenario_file.stem)
    persona = data.get("persona", "Shopper")
    scenario_objective = data.get("scenario_objective", "")
    conversation = data.get("conversation", [])
    judge_criteria = data.get("judge_criteria", [])
    proceed_cfg = data.get("proceed", {})
    proceed_turns = proceed_cfg.get("turns")
    max_turns = data.get("max_turns", len(conversation) or 2)
    open_ended = data.get("open_ended_until_goal", proceed_turns is None)

    if proceed_turns is not None:
        print(f"\n[Test] Executing Scenario {scenario_id} ({persona}) from {scenario_file.name}")
        print(f"       Scenario proceed: scenario.proceed(turns={proceed_turns})")
        proceed_step = scenario.proceed(turns=proceed_turns)
        target_turns = proceed_turns
    else:
        print(f"\n[Test] Executing Scenario {scenario_id} ({persona}) from {scenario_file.name}")
        print(f"       Scenario proceed: scenario.proceed() [Dynamic goal-seeking up to max_turns={max_turns}]")
        proceed_step = scenario.proceed()
        target_turns = max_turns

    assert proceed_step is not None, "Scenario proceed step creation failed"

    # Initialize EvidenceRecorder
    recorder = EvidenceRecorder(
        scenario_name=scenario_file.stem,
        case_label=scenario_id,
        persona=persona,
        scenario_objective=scenario_objective,
    )

    follow_up_gen = DynamicFollowUpGenerator()
    agent = ArgosTrevorAgent(
        evidence=recorder,
        video_dir=recorder.video_dir,
    )

    history = []
    verdict = None
    execution_error = None
    judging_error = None

    try:
        await agent.setup()

        turn_num = 1
        while turn_num <= target_turns:
            turn_idx = turn_num - 1
            turn_data = conversation[turn_idx] if turn_idx < len(conversation) else {}

            if turn_num == 1:
                # Turn 1 always starts with the scenario's first user query;
                # Trevor's initial greeting/setup state is not a conversation turn.
                user_msg = turn_data.get("user_message") or data.get("initial_message")
                print(f"  -> Turn {turn_num} Initial Customer: '{user_msg}'")
            else:
                prev_turn = history[-1]
                user_msg = follow_up_gen.generate_follow_up(
                    turn_number=turn_num,
                    persona=persona,
                    scenario_objective=scenario_objective,
                    previous_query=prev_turn["user"],
                    previous_reply=prev_turn["bot"],
                    intent=turn_data.get("follow_up_intent"),
                )
                print(f"  -> Turn {turn_num} Dynamic Customer Follow-Up (grounded in Turn {turn_num - 1} reply):")
                print(f"     '{user_msg}'")

            reply = await agent.call(user_msg, turn_number=turn_num)
            print(f"  <- Turn {turn_num} Trevor Reply ({len(reply)} chars):")
            print(f"     {reply[:160]}...")

            assert len(reply) > 15, f"Turn {turn_num} reply was empty or invalid"
            history.append(
                {
                    "turn": turn_num,
                    "user": user_msg,
                    "bot": reply,
                    "response_complete": True,
                }
            )

            # Check if open-ended goal has been achieved
            if open_ended and turn_num >= 2:
                lower_reply = reply.lower()
                if any(kw in lower_reply for kw in ["recommend", "battery", "hour", "model", "rating", "£"]) and len(reply) > 150:
                    print(f"  [Goal Check] Scenario objective successfully fulfilled at Turn {turn_num}!")
                    break

            turn_num += 1

    except Exception as exc:
        execution_error = exc
        print(f"[Test] Execution encountered exception: {exc}")

    finally:
        # 1. Close browser context and flush video recording
        await agent.teardown()

        # 2. Transcode captured WebM video to MP4 format
        try:
            raw_video = agent.get_recorded_video_path()
            if raw_video and raw_video.exists():
                print(f"  [Video] Transcoding Playwright recording: {raw_video}")
                target_mp4 = recorder.out_dir / "execution_video.mp4"
                transcoded = transcode_webm_to_mp4(
                    webm_path=raw_video,
                    output_mp4_path=target_mp4,
                    remove_source=False,
                )
                if transcoded and transcoded.exists():
                    recorder.set_video_path(transcoded)
                    print(f"  [Video] Transcoded execution video: {transcoded}")
        except Exception as v_err:
            print(f"  [Video] Video transcoding notice: {v_err}")

        # 3. Judge only a successfully completed scenario. Partial/empty
        # transcripts are reported as execution failures and never sent to LLM.
        if execution_error is not None:
            print(
                f"  [Judge] Skipped for {scenario_id}: scenario execution did not complete."
            )
        else:
            try:
                print(f"  [Judge] Starting evaluation for {scenario_id}...")
                judge_engine = ArgosJudgeEngine()
                verdict = judge_engine.evaluate(
                    scenario_id=scenario_id,
                    persona=persona,
                    scenario_objective=scenario_objective,
                    judge_criteria=judge_criteria,
                    conversation_turns=history,
                )
                print(f"\n[Judge] Evaluation Summary for {scenario_id}:")
                print(f"        Overall Verdict: {verdict.overall_status}")
                for c in verdict.criteria_results:
                    print(f"        - [{c.status}] {c.criterion}: {c.reason}")
            except Exception as j_err:
                judging_error = j_err
                print(f"  [Judge] Evaluation failed: {j_err}")

        # 4. Generate Comprehensive Evidence PDF Report
        print(f"  [Report] Building evidence report for {scenario_id}...")
        is_passed = (
            verdict.overall_status in ("PASS", "WARN with PASS")
            if verdict
            else execution_error is None and judging_error is None
        )
        pdf_path = recorder.generate_pdf(
            passed=is_passed,
            judge_verdict=verdict,
            reasoning=(
                verdict.summary_reasoning
                if verdict
                else str(execution_error or judging_error or "")
            ),
        )
        if pdf_path and pdf_path.exists():
            print(f"  [Report] Generated Evidence PDF: {pdf_path}")

    # Re-raise execution error or fail on verdict FAIL
    if execution_error:
        raise execution_error

    if judging_error:
        raise judging_error

    if verdict and verdict.overall_status == "FAIL":
        pytest.fail(f"Scenario {scenario_id} FAILED judging: {verdict.summary_reasoning}")
