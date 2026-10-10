"""
tests/test_argos_agent.py
─────────────────────────
Verification test for ArgosTrevorAgent Playwright adapter.
Validates:
1. Cookie consent auto-handling (OneTrust)
2. Ask Trevor drawer and iframe loading
3. Asking a test question and extracting Trevor's response
4. Clean teardown
"""

import pytest
from agents.argos_agent import ArgosTrevorAgent

@pytest.mark.asyncio
async def test_argos_trevor_interaction():
    agent = ArgosTrevorAgent()
    try:
        print("\n--- Running ArgosTrevorAgent Setup ---")
        await agent.setup()
        assert agent._page is not None, "Page failed to initialize"
        assert agent._frame is not None, "Trevor iframe failed to initialize"

        print("--- Sending Persona Query to Trevor ---")
        query = "What is a good lightweight cordless vacuum under £150?"
        reply = await agent.call(query)
        print(f"\nTrevor Reply Received:\n{reply}\n")

        assert len(reply) > 20, "Trevor reply was empty or too short"
        assert any(kw in reply.lower() for kw in ["vacuum", "cordless", "makita", "quest", "£", "option"]), (
            f"Reply did not contain expected shopping keywords: {reply}"
        )
        print("--- Test Passed Successfully ---")
    finally:
        await agent.teardown()
