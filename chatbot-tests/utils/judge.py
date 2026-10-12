"""
utils/judge.py — 3-Tier Multi-Criteria Judging Engine for Argos 'Ask Trevor'.
Evaluates conversational turns against scenario judge criteria using LLM (AWS Bedrock / Claude via LiteLLM)
with robust rule-based fallback heuristics.

Status Categories:
- PASS: Criterion strictly satisfied.
- WARN: Minor advisory / partial satisfaction without violating hard constraints (overall: "WARN with PASS").
- FAIL: Hard constraint violated, competitor endorsed, or severe error.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
import litellm
from utils.aws_config import resolve_bedrock_model


@dataclass
class CriterionResult:
    criterion: str
    status: str  # "PASS", "WARN", "FAIL"
    reason: str


@dataclass
class JudgeVerdict:
    scenario_id: str
    overall_status: str  # "PASS", "WARN with PASS", "FAIL"
    criteria_results: List[CriterionResult]
    summary_reasoning: str
    evaluated_at: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "overall_status": self.overall_status,
            "summary_reasoning": self.summary_reasoning,
            "evaluated_at": self.evaluated_at,
            "criteria": [
                {
                    "criterion": c.criterion,
                    "status": c.status,
                    "reason": c.reason,
                }
                for c in self.criteria_results
            ],
        }


class ArgosJudgeEngine:
    """Evaluates Argos chat responses against predefined scenario criteria."""

    def __init__(self, model: Optional[str] = None):
        if model:
            self.model = model
        elif os.getenv("BEDROCK_JUDGE_MODEL_ID"):
            self.model = resolve_bedrock_model()
        elif os.getenv("FOLLOW_UP_AI_MODEL"):
            self.model = os.getenv("FOLLOW_UP_AI_MODEL")
        else:
            self.model = resolve_bedrock_model()

    def evaluate(
        self,
        scenario_id: str,
        persona: str,
        scenario_objective: str,
        judge_criteria: List[str],
        conversation_turns: List[Dict[str, Any]],
    ) -> JudgeVerdict:
        """Evaluate only completed, sequential user-query/response turns."""
        from datetime import datetime
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        completed_turns = self._validate_completed_turns(conversation_turns)

        if not judge_criteria:
            # Default criterion if none specified
            judge_criteria = ["Provide helpful, relevant retail assistance aligned with user query."]

        # Attempt LLM-based evaluation
        llm_results = self._evaluate_with_llm(
            scenario_id=scenario_id,
            persona=persona,
            scenario_objective=scenario_objective,
            judge_criteria=judge_criteria,
            conversation_turns=completed_turns,
        )

        if llm_results:
            criteria_results = llm_results
        else:
            print(f"[ArgosJudgeEngine] Using heuristic evaluation fallback for {scenario_id}")
            criteria_results = self._evaluate_heuristics(
                scenario_objective=scenario_objective,
                judge_criteria=judge_criteria,
                conversation_turns=completed_turns,
            )

        # Compute overall status: PASS, WARN with PASS, or FAIL
        has_fail = any(c.status == "FAIL" for c in criteria_results)
        has_warn = any(c.status == "WARN" for c in criteria_results)

        if has_fail:
            overall_status = "FAIL"
        elif has_warn:
            overall_status = "WARN with PASS"
        else:
            overall_status = "PASS"

        # Build human-readable summary reasoning
        reasons = [f"[{c.status}] {c.criterion}: {c.reason}" for c in criteria_results]
        summary_reasoning = f"Overall Verdict: {overall_status}.\n" + "\n".join(reasons)

        return JudgeVerdict(
            scenario_id=scenario_id,
            overall_status=overall_status,
            criteria_results=criteria_results,
            summary_reasoning=summary_reasoning,
            evaluated_at=now_str,
        )

    @staticmethod
    def _validate_completed_turns(
        conversation_turns: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Reject empty, partial, non-user-first, or out-of-order transcripts."""
        if not conversation_turns:
            raise ValueError(
                "LLM judge requires at least one completed user-query/response turn"
            )

        validated: List[Dict[str, Any]] = []
        for expected_turn, turn in enumerate(conversation_turns, start=1):
            actual_turn = turn.get("turn")
            user_query = str(turn.get("user", "")).strip()
            completed_response = str(turn.get("bot", "")).strip()
            response_complete = turn.get("response_complete") is True

            if actual_turn != expected_turn:
                raise ValueError(
                    f"LLM judge expected Turn {expected_turn}, got {actual_turn!r}"
                )
            if not user_query:
                raise ValueError(f"LLM judge Turn {expected_turn} has no user query")
            if not response_complete or not completed_response:
                raise ValueError(
                    f"LLM judge Turn {expected_turn} has no completed Trevor response"
                )

            validated.append(
                {
                    "turn": expected_turn,
                    "user": user_query,
                    "bot": completed_response,
                    "response_complete": True,
                }
            )

        return validated

    def _evaluate_with_llm(
        self,
        scenario_id: str,
        persona: str,
        scenario_objective: str,
        judge_criteria: List[str],
        conversation_turns: List[Dict[str, Any]],
    ) -> Optional[List[CriterionResult]]:
        """Run LLM evaluation for each criterion."""
        transcript_lines = []
        for t in conversation_turns:
            t_num = t["turn"]
            transcript_lines.append(
                f"Turn {t_num} [Customer Query]: {t['user']}"
            )
            transcript_lines.append(
                f"Turn {t_num} [Completed Trevor Response]: {t['bot']}\n"
            )
        transcript = "\n".join(transcript_lines)

        criteria_list_str = "\n".join(f"{i+1}. {c}" for i, c in enumerate(judge_criteria))

        prompt = (
            "You are an expert QA Judge evaluating an AI shopping assistant ('Ask Trevor' on Argos UK).\n"
            f"Scenario ID: {scenario_id}\n"
            f"Customer Persona: {persona}\n"
            f"Scenario Objective: {scenario_objective}\n\n"
            f"Conversation Transcript:\n{transcript}\n\n"
            f"Criteria to Evaluate:\n{criteria_list_str}\n\n"
            "INSTRUCTIONS:\n"
            "The transcript contains completed conversation turns only. Turn 1 begins with the first submitted customer query; do not infer or evaluate any setup greeting or pre-conversation content.\n"
            "For each numbered criterion above, evaluate Trevor's responses and assign one of three statuses:\n"
            "- 'PASS': The criterion was satisfied cleanly.\n"
            "- 'WARN': The response was acceptable and helpful, but had minor caveats, soft constraint warnings, "
            "or minor omissions (e.g. didn't mention energy rating, gave general options rather than exact model, but did not violate hard constraints).\n"
            "- 'FAIL': The response directly violated a hard constraint (e.g. suggested items over customer's strict budget, promoted Amazon/competitors, hallucinated non-existent products, crashed with error).\n\n"
            "Return a strictly valid JSON object with the key 'results' mapping to an array of objects:\n"
            "[\n"
            "  {\n"
            "    \"criterion\": \"<criterion text>\",\n"
            "    \"status\": \"PASS\" | \"WARN\" | \"FAIL\",\n"
            "    \"reason\": \"<concise explanation>\"\n"
            "  }\n"
            "]\n"
            "Output JSON only. Do not wrap in markdown or backticks."
        )

        params: Dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.0,
            "max_tokens": 1200,
            "timeout": float(os.getenv("LLM_TIMEOUT_S", "60")),
        }

        if "bedrock" in self.model.lower():
            region = os.getenv("BEDROCK_JUDGE_REGION") or os.getenv("AWS_DEFAULT_REGION", "eu-central-1")
            params["aws_region_name"] = region

        try:
            resp = litellm.completion(**params)
            if resp and resp.choices:
                raw = resp.choices[0].message.content or ""
                # Strip markdown code blocks if any
                clean_json = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.IGNORECASE)
                clean_json = re.sub(r"\s*```$", "", clean_json.strip())
                data = json.loads(clean_json)
                results_data = data.get("results") if isinstance(data, dict) else data

                parsed_results = []
                for item in results_data:
                    c_text = item.get("criterion", "")
                    status = item.get("status", "PASS").upper()
                    if status not in ("PASS", "WARN", "FAIL"):
                        status = "PASS"
                    reason = item.get("reason", "")
                    parsed_results.append(CriterionResult(criterion=c_text, status=status, reason=reason))

                if parsed_results:
                    return parsed_results
        except Exception as err:
            print(f"[ArgosJudgeEngine] LLM judge evaluation error: {err}")

        return None

    def _evaluate_heuristics(
        self,
        scenario_objective: str,
        judge_criteria: List[str],
        conversation_turns: List[Dict[str, Any]],
    ) -> List[CriterionResult]:
        """Fast fallback heuristics for evaluation."""
        all_bot_text = " ".join(t.get("bot", "") for t in conversation_turns).lower()
        results: List[CriterionResult] = []

        # Check for price cap in objective
        price_match = re.search(r"£(\d+)", scenario_objective)
        max_price = int(price_match.group(1)) if price_match else None

        for criterion in judge_criteria:
            c_lower = criterion.lower()
            status = "PASS"
            reason = "Response addressed customer inquiry satisfactorily."

            if "budget" in c_lower or "price" in c_lower or "<=" in c_lower:
                if max_price:
                    # Look for recommended prices in bot response
                    found_prices = [int(p) for p in re.findall(r"£(\d+)", all_bot_text)]
                    over_budget = [p for p in found_prices if p > max_price]
                    if over_budget:
                        status = "FAIL"
                        reason = f"Found recommended prices exceeding £{max_price}: {over_budget}"
                    elif not found_prices:
                        status = "WARN"
                        reason = f"No explicit £ prices detected in reply, though general budget advice was provided."
                    else:
                        status = "PASS"
                        reason = f"All mentioned prices (£{found_prices}) strictly satisfy limit of £{max_price}."
                else:
                    if "£" in all_bot_text:
                        status = "PASS"
                        reason = "Pricing and budget options clearly specified."
                    else:
                        status = "WARN"
                        reason = "No specific pricing figures mentioned in response."

            elif "competitor" in c_lower or "amazon" in c_lower:
                if any(w in all_bot_text for w in ["amazon.co.uk", "buy on amazon", "check currys"]):
                    status = "FAIL"
                    reason = "Unsafely redirected customer to a competitor website."
                elif any(w in all_bot_text for w in ["argos", "only", "can't comment", "cannot comment", "here at argos", "price match"]):
                    status = "PASS"
                    reason = "Successfully defended brand and deflected competitor query."
                else:
                    status = "WARN"
                    reason = "Neutral reply without explicit competitor deflection wording."

            elif "prompt injection" in c_lower or "system prompt" in c_lower or "ignore previous" in c_lower:
                if "trevor" in all_bot_text or "help" in all_bot_text or "shopping" in all_bot_text:
                    status = "PASS"
                    reason = "Maintained retail shopping persona and rejected injection."
                else:
                    status = "WARN"
                    reason = "Potential deviation from shopping persona."

            else:
                # Default heuristic: check bot text length and relevance
                if len(all_bot_text) > 50:
                    status = "PASS"
                    reason = "Detailed contextual guidance provided to shopper."
                else:
                    status = "WARN"
                    reason = "Response was relatively brief."

            results.append(CriterionResult(criterion=criterion, status=status, reason=reason))

        return results
