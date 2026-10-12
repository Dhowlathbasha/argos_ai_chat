"""Regression tests for completed-turn-only LLM judging."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from utils.judge import ArgosJudgeEngine


def _completed_turn(number: int, user: str, bot: str):
    return {
        "turn": number,
        "user": user,
        "bot": bot,
        "response_complete": True,
    }


def test_judge_prompt_starts_with_first_user_query_and_completed_response(monkeypatch):
    captured = {}

    def completion(**params):
        captured.update(params)
        content = json.dumps(
            {
                "results": [
                    {
                        "criterion": "Helpful answer",
                        "status": "PASS",
                        "reason": "Completed response answered the query.",
                    }
                ]
            }
        )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
        )

    monkeypatch.setattr("utils.judge.litellm.completion", completion)
    engine = ArgosJudgeEngine(model="bedrock/test-model")
    verdict = engine.evaluate(
        scenario_id="case",
        persona="Shopper",
        scenario_objective="Get help",
        judge_criteria=["Helpful answer"],
        conversation_turns=[
            _completed_turn(1, "My first user query", "First completed response")
        ],
    )

    prompt = captured["messages"][0]["content"]
    assert "Turn 1 [Customer Query]: My first user query" in prompt
    assert "Turn 1 [Completed Trevor Response]: First completed response" in prompt
    assert "welcome" not in prompt.lower()
    assert verdict.overall_status == "PASS"


@pytest.mark.parametrize(
    "turns",
    [
        [],
        [{"turn": 1, "user": "Query", "bot": "", "response_complete": False}],
        [_completed_turn(2, "Wrong first turn", "Response")],
    ],
)
def test_judge_rejects_empty_partial_or_nonsequential_turns(monkeypatch, turns):
    called = False

    def completion(**_params):
        nonlocal called
        called = True

    monkeypatch.setattr("utils.judge.litellm.completion", completion)
    engine = ArgosJudgeEngine(model="bedrock/test-model")

    with pytest.raises(ValueError, match="LLM judge"):
        engine.evaluate(
            scenario_id="case",
            persona="Shopper",
            scenario_objective="Get help",
            judge_criteria=["Helpful answer"],
            conversation_turns=turns,
        )

    assert not called
