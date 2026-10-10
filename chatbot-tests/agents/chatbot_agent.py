"""
agents/chatbot_agent.py — Argos 'Ask Trevor' Bot adapter (Unified Adapter)
──────────────────────────────────────────────────────────────────────────
Adapter for Argos 'Ask Trevor' AI Shopping Assistant (`https://www.argos.co.uk/`).
Provides both `ArgosTrevorAgent` and `ChatbotAgent` (scenario.AgentAdapter compatible).
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional
import scenario
from agents.argos_agent import ArgosTrevorAgent


class ChatbotAgent(scenario.AgentAdapter):
    """Scenario AgentAdapter for Argos Ask Trevor AI Shopping Assistant."""

    def __init__(self, email: str = "", password: str = "", evidence: Any = None, total_turns: Optional[int] = None, case_label: Optional[str] = None):
        self._trevor = ArgosTrevorAgent(evidence=evidence)
        self.evidence = evidence
        self.total_turns = total_turns
        self.case_label = case_label

    async def setup(self) -> None:
        await self._trevor.setup()

    async def call(self, input: scenario.AgentInput) -> scenario.AgentReturnTypes:
        query = input.last_new_user_message_str()
        reply = await self._trevor.call(query)
        return reply

    async def teardown(self) -> None:
        await self._trevor.teardown()


__all__ = ["ArgosTrevorAgent", "ChatbotAgent"]
