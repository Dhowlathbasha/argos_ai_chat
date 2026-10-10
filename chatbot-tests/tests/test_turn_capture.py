"""Regression tests for user-turn boundaries and post-response screenshots."""
from __future__ import annotations

import asyncio

from agents.argos_agent import ArgosTrevorAgent
from utils.evidence import EvidenceRecorder


class FakeTextarea:
    def __init__(self, events):
        self.events = events

    async def wait_for(self, **_kwargs):
        self.events.append("input-ready")

    async def fill(self, message):
        self.events.append(("fill", message))

    async def press(self, key):
        self.events.append(("press", key))


class FakeFrame:
    def __init__(self, textarea):
        self.textarea = textarea

    def locator(self, _selector):
        return self.textarea


class FakePage:
    def __init__(self, events):
        self.events = events

    async def wait_for_timeout(self, _milliseconds):
        return None

    async def screenshot(self, **_kwargs):
        self.events.append("screenshot")
        return b"png"


class FakeEvidence:
    def __init__(self, events):
        self.events = events

    def record(self, **kwargs):
        self.events.append(("record", kwargs))


def test_screenshot_and_turn_record_happen_after_completed_response():
    events = []
    agent = ArgosTrevorAgent(
        headless=True,
        channel=None,
        evidence=FakeEvidence(events),
    )
    agent._page = FakePage(events)
    agent._frame = FakeFrame(FakeTextarea(events))

    async def dismiss():
        return False

    async def reply_state():
        return 1, "Welcome"

    async def completed_reply(**_kwargs):
        events.append("response-complete")
        return "The completed Trevor response"

    agent.dismiss_cookie_popups = dismiss
    agent._extract_reply_state = reply_state
    agent._wait_for_reply = completed_reply

    reply = asyncio.run(agent.call("My first query", turn_number=1))

    assert reply == "The completed Trevor response"
    assert events.index("response-complete") < events.index("screenshot")
    record_event = next(event for event in events if isinstance(event, tuple) and event[0] == "record")
    assert events.index("screenshot") < events.index(record_event)
    assert record_event[1]["query"] == "My first query"
    assert record_event[1]["turn_index"] == 1


def test_waiter_ignores_user_query_appended_after_previous_reply():
    agent = ArgosTrevorAgent(headless=True, channel=None)
    agent._page = FakePage([])
    states = iter(
        [
            (1, "Old response plus the new user query"),
            (1, "Old response plus the new user query"),
            (2, "Streaming response"),
            (2, "Completed response text"),
            (2, "Completed response text"),
            (2, "Completed response text"),
            (2, "Completed response text"),
        ]
    )

    async def reply_state():
        return next(states)

    agent._extract_reply_state = reply_state
    reply = asyncio.run(
        agent._wait_for_reply(
            previous_reply_count=1,
            previous_reply="Old response",
            max_wait_sec=1,
        )
    )

    assert reply == "Completed response text"


def test_evidence_first_turn_is_explicitly_turn_one(tmp_path, monkeypatch):
    import utils.evidence as evidence_module

    monkeypatch.setattr(evidence_module, "REPORTS_ROOT", tmp_path)
    recorder = EvidenceRecorder("scenario", "case")
    recorder.record(
        query="First user query",
        response="First completed response",
        screenshot_bytes=b"not-an-image",
        turn_index=1,
    )

    assert recorder.turns[0].index == 1
    assert recorder.turns[0].query == "First user query"
