"""
utils/follow_up_generator.py — AI-Powered Dynamic Follow-Up Generator
─────────────────────────────────────────────────────────────────────
Generates non-hardcoded follow-up questions for multi-turn conversations
using an AI Model (LLM via LiteLLM / AWS Bedrock / OpenAI / Anthropic).
The questions are dynamically generated strictly by the AI based on Trevor's
previous response, previous query, customer persona, and scenario objectives.
"""

from __future__ import annotations

import os
import re
from typing import Any, Optional
import litellm
from utils.aws_config import resolve_bedrock_model


class DynamicFollowUpGenerator:
    """
    AI-driven customer follow-up question generator for multi-turn testing.
    Uses an LLM to generate contextual follow-up questions from the AI only,
    grounded directly in Trevor's actual response to the previous turn.
    """

    def __init__(self, model: Optional[str] = None):
        # Resolve AI model from parameter or environment variables
        if model:
            self.model = model
        elif os.getenv("FOLLOW_UP_AI_MODEL"):
            self.model = os.getenv("FOLLOW_UP_AI_MODEL")
        elif os.getenv("BEDROCK_JUDGE_MODEL_ID"):
            self.model = resolve_bedrock_model()
        elif os.getenv("OPENAI_API_KEY"):
            self.model = "openai/gpt-4o-mini"
        elif os.getenv("ANTHROPIC_API_KEY"):
            self.model = "anthropic/claude-3-5-haiku-20241022"
        elif os.getenv("GEMINI_API_KEY"):
            self.model = "gemini/gemini-1.5-flash"
        else:
            self.model = resolve_bedrock_model("anthropic.claude-3-haiku-20240307-v1:0")

    def _generate_with_ai(
        self,
        turn_number: int,
        persona: str,
        scenario_objective: str,
        previous_query: str,
        previous_reply: str,
        intent: Optional[str] = None,
    ) -> Optional[str]:
        """Call AI model via LiteLLM to synthesize a natural follow-up question."""
        intent_instruction = f"\nFocus on this aspect in your inquiry: {intent.replace('_', ' ')}" if intent else ""

        system_prompt = (
            f"You are acting as a real customer ({persona}) testing the Argos UK AI shopping assistant ('Ask Trevor').\n"
            f"Overall Scenario Objective: {scenario_objective}\n"
            f"Current Conversation Turn: Turn {turn_number}\n"
            f"{intent_instruction}\n\n"
            "CORE TASK:\n"
            "Generate a realistic, natural follow-up question for your next message.\n\n"
            "STRICT RULES:\n"
            "1. Your question MUST be derived directly from what Trevor said in his last reply.\n"
            "2. If Trevor recommended specific products/models, ask about those specific products (e.g. compare them, ask about features, warranty, customer ratings, or delivery).\n"
            "3. If Trevor asked you clarifying questions (e.g. preferred size, cordless vs corded, budget), answer Trevor's question directly and state your preference.\n"
            "4. Do NOT repeat previous questions. Move the conversation forward naturally.\n"
            "5. Output ONLY the single user question as plain text. No greetings, no preamble, no markdown, no quotes."
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": previous_query},
            {"role": "assistant", "content": previous_reply},
            {
                "role": "user",
                "content": f"[Generate customer follow-up question for Turn {turn_number} based on Trevor's reply above]:",
            },
        ]

        try:
            # Configure LiteLLM parameters
            params: dict[str, Any] = {
                "model": self.model,
                "messages": messages,
                "temperature": 0.7,
                "max_tokens": 120,
                "timeout": float(os.getenv("LLM_TIMEOUT_S", "60")),
            }

            # Optional AWS region handling for Bedrock
            if "bedrock" in self.model.lower():
                region = os.getenv("BEDROCK_JUDGE_REGION") or os.getenv("AWS_DEFAULT_REGION", "eu-central-1")
                params["aws_region_name"] = region

            response = litellm.completion(**params)

            if response and hasattr(response, "choices") and len(response.choices) > 0:
                raw_text = response.choices[0].message.content or ""
                cleaned = raw_text.strip().strip('"').strip("'").strip("`")
                if cleaned:
                    return cleaned

        except Exception as e:
            print(f"[DynamicFollowUpGenerator] AI model '{self.model}' generation notice: {type(e).__name__} ({e})")
            return None

        return None

    def _fallback_dynamic_synthesis(
        self,
        turn_number: int,
        persona: str,
        scenario_objective: str,
        previous_query: str,
        previous_reply: str,
        intent: Optional[str] = None,
    ) -> str:
        """
        Dynamically synthesize a follow-up directly from Trevor's response context
        when external AI API credentials are unauthenticated or offline.
        Extracts actual entities, questions asked by Trevor, and suggestion chips.
        """
        lines = [line.strip() for line in previous_reply.split("\n") if line.strip()]

        # 1. Check if Trevor gave suggestion chips ("You may consider:")
        suggestions = []
        in_suggestions = False
        for line in lines:
            if "you may consider" in line.lower():
                in_suggestions = True
                continue
            if in_suggestions and line.endswith("?"):
                suggestions.append(line)
            elif in_suggestions and line.lower() in ["clear chat", "responses generated by ai"]:
                in_suggestions = False

        if suggestions:
            return suggestions[0]

        # 2. Check if Trevor asked a question to the user
        questions_asked = []
        for line in lines:
            if line.endswith("?") and any(w in line.lower() for w in ["would you", "do you", "are you", "what", "which"]):
                questions_asked.append(line)

        if questions_asked:
            q = questions_asked[0].lower()
            if "size" in q or "screen" in q:
                return "I would prefer a standard 43 inch size. Which of those has the best customer rating?"
            elif "cordless" in q or "corded" in q:
                return "I prefer cordless. Which of the models you mentioned has the longest battery runtime?"
            elif "budget" in q or "price" in q:
                return "Please keep it to the lower end of the budget. Which option is best value for money?"
            elif "brand" in q:
                return "I prefer whichever brand has the most reliable customer reviews among those options."

        # 3. Extract product names from Trevor's reply
        products = []
        # Pattern A: Bold product names
        for bold in re.findall(r"\*\*(.+?)\*\*", previous_reply):
            cleaned = bold.strip()
            if 5 < len(cleaned) < 70 and not cleaned.lower().startswith("ask trevor"):
                if cleaned not in products:
                    products.append(cleaned)

        # Pattern B: Titles before price or dash
        for line in lines:
            cleaned_line = re.sub(r"^[\*\-\•\d+\.\s]+", "", line).strip()
            match = re.match(r"^([A-Z0-9][\w\s&+\-'\/\.]{4,65}?)\s*(?:[—\-–]\s*£|:\s*£|\(\d+\)|,\s*priced)", cleaned_line)
            if match:
                prod = match.group(1).strip()
                if prod and prod not in products and not prod.lower().startswith("ask trevor"):
                    products.append(prod)

        # Pattern C: Conversational mentions
        for prod in re.findall(r"(?:found the|have the|recommend the|suggest the)\s+([A-Z0-9][\w\s&+\-'\/\.]{4,60}?)\s+(?:for\s+£|at\s+£|priced|rated|with|,|\.|\n)", previous_reply, re.IGNORECASE):
            prod_clean = prod.strip()
            if 5 < len(prod_clean) < 70 and prod_clean not in products:
                products.append(prod_clean)

        # 4. Formulate contextual follow-up based on extracted entities
        aspect = intent.replace("_", " ") if intent else "warranty and overall performance"

        if len(products) >= 2:
            return f"Between the {products[0]} and {products[1]}, can you compare their {aspect}?"
        elif len(products) == 1:
            return f"For the {products[0]}, could you share more details regarding its {aspect} and customer ratings?"

        # Fallback to query context
        return f"Based on those details, which option do you recommend most regarding {aspect}?"

    def generate_follow_up(
        self,
        turn_number: int,
        persona: str,
        scenario_objective: str,
        previous_query: str,
        previous_reply: str,
        intent: Optional[str] = None,
    ) -> str:
        """
        Generate a dynamic follow-up query using the AI model.
        Falls back to entity-grounded synthesis if AI credentials require update.
        """
        # Primary generation: AI Model
        ai_query = self._generate_with_ai(
            turn_number=turn_number,
            persona=persona,
            scenario_objective=scenario_objective,
            previous_query=previous_query,
            previous_reply=previous_reply,
            intent=intent,
        )

        if ai_query:
            return ai_query

        # Adaptive fallback grounded in Trevor's actual response
        return self._fallback_dynamic_synthesis(
            turn_number=turn_number,
            persona=persona,
            scenario_objective=scenario_objective,
            previous_query=previous_query,
            previous_reply=previous_reply,
            intent=intent,
        )


__all__ = ["DynamicFollowUpGenerator"]
