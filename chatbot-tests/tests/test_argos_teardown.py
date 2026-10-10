"""Regression tests for Playwright cleanup releasing xdist workers."""
from __future__ import annotations

import asyncio

import agents.argos_agent as argos_agent_module
from agents.argos_agent import ArgosTrevorAgent


class FakeVideo:
    def __init__(self, path):
        self._path = path

    async def path(self):
        return str(self._path)


class HangingPage:
    def __init__(self, video):
        self.video = video

    async def close(self):
        await asyncio.Event().wait()


class CloseTracker:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class StopTracker:
    def __init__(self):
        self.stopped = False

    async def stop(self):
        self.stopped = True


def test_hung_page_close_does_not_block_remaining_cleanup(monkeypatch, tmp_path):
    monkeypatch.setattr(argos_agent_module, "PLAYWRIGHT_TEARDOWN_TIMEOUT_S", 0.01)
    video_path = tmp_path / "capture.webm"
    video_path.write_bytes(b"playwright-video")

    agent = ArgosTrevorAgent(headless=True, channel=None, video_dir=tmp_path)
    context = CloseTracker()
    browser = CloseTracker()
    playwright = StopTracker()
    agent._page = HangingPage(FakeVideo(video_path))
    agent._context = context
    agent._browser = browser
    agent._playwright = playwright

    asyncio.run(asyncio.wait_for(agent.teardown(), timeout=0.5))

    assert context.closed
    assert browser.closed
    assert playwright.stopped
    assert agent.get_recorded_video_path() == video_path
