"""Regression tests for worker-isolated persistent Chrome contexts."""

from types import SimpleNamespace

import pytest

from agents.argos_agent import ArgosTrevorAgent


class _PersistentChromium:
    def __init__(self):
        self.calls = []
        self.context = SimpleNamespace(pages=[])

    async def launch_persistent_context(self, **kwargs):
        self.calls.append(kwargs)
        return self.context


def test_profile_directory_is_isolated_by_xdist_worker(monkeypatch, tmp_path):
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw/3")
    agent = ArgosTrevorAgent(profile_dir=tmp_path, persistent_context=True)

    assert agent._persistent_profile_path() == tmp_path / "gw_3"


@pytest.mark.asyncio
async def test_persistent_context_launches_installed_chrome(monkeypatch, tmp_path):
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw1")
    chromium = _PersistentChromium()
    agent = ArgosTrevorAgent(
        headless=False,
        channel="chrome",
        persistent_context=True,
        profile_dir=tmp_path,
        cdp_chrome=False,
    )
    agent._playwright = SimpleNamespace(chromium=chromium)

    await agent._launch_browser_context(
        {"locale": "en-GB", "timezone_id": "Europe/London"}
    )

    assert agent._context is chromium.context
    assert agent._browser is None
    assert chromium.calls == [
        {
            "user_data_dir": str(tmp_path / "gw1"),
            "headless": False,
            "ignore_default_args": ["--no-sandbox", "--enable-automation"],
            "channel": "chrome",
            "locale": "en-GB",
            "timezone_id": "Europe/London",
        }
    ]


def test_cdp_port_is_isolated_by_xdist_worker(monkeypatch):
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw4")
    agent = ArgosTrevorAgent(cdp_chrome=True)

    assert agent._cdp_port() == 9227
