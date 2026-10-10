"""Regression tests for bounded Akamai Access Denied handling."""

from types import SimpleNamespace

import pytest

import agents.argos_agent as argos_module
from agents.argos_agent import ArgosAccessDeniedError, ArgosTrevorAgent
from conftest import pytest_exception_interact


class _Body:
    def __init__(self, page):
        self.page = page

    async def inner_text(self, timeout=0):
        return self.page.body


class _Page:
    def __init__(self, statuses):
        self.statuses = iter(statuses)
        self.goto_calls = 0
        self.waits = []
        self.url = "https://www.argos.co.uk/"
        self.body = "Argos homepage"

    async def goto(self, *_args, **_kwargs):
        self.goto_calls += 1
        status = next(self.statuses)
        self.body = (
            "Access Denied. You don't have permission to access this server. "
            "Reference #18.test"
            if status == 403
            else "Argos homepage"
        )
        return SimpleNamespace(status=status)

    async def wait_for_timeout(self, milliseconds):
        self.waits.append(milliseconds)

    async def title(self):
        return "Access Denied" if "Access Denied" in self.body else "Argos"

    def locator(self, selector):
        assert selector == "body"
        return _Body(self)


@pytest.mark.asyncio
async def test_access_denied_gets_one_cooldown_retry(monkeypatch):
    monkeypatch.setattr(argos_module, "ARGOS_NAVIGATION_RETRIES", 1)
    monkeypatch.setattr(argos_module, "ARGOS_BLOCK_BACKOFF_S", 0.01)
    agent = ArgosTrevorAgent()
    page = _Page([403, 200])
    agent._page = page

    await agent._navigate_to_argos()

    assert page.goto_calls == 2
    assert 10 in page.waits


@pytest.mark.asyncio
async def test_persistent_access_denied_fails_without_an_unbounded_loop(monkeypatch):
    monkeypatch.setattr(argos_module, "ARGOS_NAVIGATION_RETRIES", 1)
    monkeypatch.setattr(argos_module, "ARGOS_BLOCK_BACKOFF_S", 0)
    agent = ArgosTrevorAgent()
    page = _Page([403, 403])
    agent._page = page

    with pytest.raises(ArgosAccessDeniedError, match="bounded retry was exhausted"):
        await agent._navigate_to_argos()

    assert page.goto_calls == 2


def test_access_denied_stops_remaining_scenarios():
    session = SimpleNamespace(shouldstop=False)
    node = SimpleNamespace(session=session)
    call = SimpleNamespace(
        excinfo=SimpleNamespace(value=ArgosAccessDeniedError("blocked"))
    )

    pytest_exception_interact(node, call, report=SimpleNamespace())

    assert "remaining scenarios were not started" in session.shouldstop
