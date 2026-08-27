"""The bounded ReAct investigation loop.

Given ONE ambiguous record, the agent reasons and calls read-only tools until it
can submit a verdict or exhausts its step budget. It returns an ``AgentResult``
carrying the full evidence trail (every tool call, its arguments, and the
observation) plus a proposed verdict.

The proposed verdict is then fed to the engine's EXISTING guardrails via
``InvestigatorAgent`` (which implements the ``LLMProvider`` protocol) — so the
agent's conclusion is only ever a recommendation; deterministic code decides.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from .. import config
from ..llm_provider import LLMResult
from ..matcher import AmbiguousCase
from .model import AgentModel, AgentModelError, AgentModelResponse, ToolCall
from .tools import ALL_SCHEMAS, ToolContext, execute_tool


_SYSTEM_PROMPT = (
    "You are an investigative reconciliation agent for an Indian payment gateway "
    "(amounts in INR). A deterministic rule engine could not confidently match "
    "ONE gateway settlement to a bank credit and has handed it to you to "
    "investigate. You have READ-ONLY tools to look across the settlement, the "
    "order ledger, and the bank statement. Investigate, then finish by calling "
    "`submit_verdict`.\n\n"
    "DOMAIN CALIBRATION:\n"
    "- 'net' = gross minus ~2% gateway fee and 18% GST on that fee; tiny "
    "differences are fee/tax rounding.\n"
    "- An amount gap up to about ₹5 (or ~1-2% of net) is normal fee rounding / a "
    "minor bank charge -> the SAME payment -> MATCHED.\n"
    "- Indian settlements commonly land T+1 to T+3, sometimes T+4; a credit up to "
    "~4 days after settlement whose amount agrees is the SAME payment -> MATCHED.\n"
    "- Use MISMATCH_AMOUNT only if the gap is clearly beyond fee rounding; "
    "DATE_SKEW only if timing is genuinely implausible (well beyond ~4 days); "
    "SPLIT_SETTLEMENT if the net was paid across multiple credits; DUPLICATE_UTR "
    "if the matched credit's UTR is not unique.\n\n"
    "RULES:\n"
    "- NEVER invent or alter an amount. Use only tool-returned numbers.\n"
    f"- You have at most {config.AGENT_MAX_STEPS} steps. Investigate efficiently, "
    "then call submit_verdict with resolution, matched_utr (or empty), reason, "
    "and confidence (0-1)."
)


@dataclass
class AgentResult:
    resolution: str
    reason: str
    confidence: float
    matched_utr: Optional[str]
    evidence: list[dict[str, Any]]
    steps_used: int
    tool_calls: int
    model: str
    aborted: bool = False
    error: Optional[str] = None


def _user_prompt(case: AmbiguousCase) -> str:
    s = case.settlement
    # The machine-readable line lets the offline simulator parse the record; the
    # real model just reads it as part of the context.
    head = f"RECORD txn_id={s.txn_id} net={s.net:.2f} order_id={s.order_id}"
    return (
        f"{head}\n\n"
        f"Investigate this flagged settlement.\n{case.summary()}\n\n"
        "Use the tools to gather evidence, then call submit_verdict."
    )


def _compact(obs: dict[str, Any], limit: int = 1200) -> str:
    """Serialise a tool observation, staying under `limit` chars while ALWAYS
    remaining valid JSON (trim list entries, never cut mid-structure)."""
    s = json.dumps(obs, default=str)
    if len(s) <= limit:
        return s
    o = dict(obs)
    for key in ("candidates", "credits", "split_credits"):
        if isinstance(o.get(key), list) and o[key]:
            while o[key] and len(json.dumps(o, default=str)) > limit:
                o[key] = o[key][:-1]
            o["_note"] = f"{key} truncated to fit token budget"
    s2 = json.dumps(o, default=str)
    if len(s2) <= limit:
        return s2
    return json.dumps({"_note": "observation too large", "keys": list(obs.keys())})


def investigate(case: AmbiguousCase, ctx: ToolContext, model: AgentModel) -> AgentResult:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": _user_prompt(case)},
    ]
    evidence: list[dict[str, Any]] = []
    tool_calls = 0

    for step in range(config.AGENT_MAX_STEPS):
        try:
            resp: AgentModelResponse = model.step(messages, ALL_SCHEMAS)
        except AgentModelError as e:
            # Provider failure -> escalate with whatever evidence we gathered.
            return AgentResult(
                resolution="UNRESOLVED", reason=f"agent aborted: {e}", confidence=0.0,
                matched_utr=case.candidate.utr if case.candidate else None,
                evidence=evidence, steps_used=step, tool_calls=tool_calls,
                model=getattr(model, "name", "?"), aborted=True, error=str(e),
            )

        if resp.content:
            evidence.append({"step": step, "type": "thought", "text": resp.content[:500]})

        if not resp.tool_calls:
            # Model didn't act; nudge it once toward a verdict (costs a step).
            messages.append({"role": "assistant", "content": resp.content or ""})
            messages.append({"role": "user",
                             "content": "Call submit_verdict now with your best conclusion."})
            continue

        # Terminal: a verdict was submitted.
        verdict_tc = next((tc for tc in resp.tool_calls if tc.name == "submit_verdict"), None)
        if verdict_tc is not None:
            a = verdict_tc.arguments or {}
            evidence.append({"step": step, "type": "verdict", "arguments": a})
            return AgentResult(
                resolution=str(a.get("resolution", "UNRESOLVED")),
                reason=str(a.get("reason", "")),
                confidence=_as_float(a.get("confidence", 0.0)),
                matched_utr=(a.get("matched_utr") or None),
                evidence=evidence, steps_used=step + 1, tool_calls=tool_calls,
                model=resp.model or getattr(model, "name", "?"),
            )

        # Otherwise execute the read-only tool calls and feed observations back.
        messages.append({
            "role": "assistant", "content": resp.content,
            "tool_calls": [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)}}
                for tc in resp.tool_calls
            ],
        })
        for tc in resp.tool_calls:
            obs = execute_tool(tc.name, tc.arguments, ctx)
            tool_calls += 1
            evidence.append({"step": step, "type": "tool_call", "tool": tc.name,
                             "arguments": tc.arguments, "observation": obs})
            messages.append({"role": "tool", "tool_call_id": tc.id, "name": tc.name,
                             "content": _compact(obs)})

    # Step budget exhausted -> escalate with the partial evidence trail.
    return AgentResult(
        resolution="UNRESOLVED",
        reason=f"investigation inconclusive within {config.AGENT_MAX_STEPS} steps; escalated",
        confidence=0.0,
        matched_utr=case.candidate.utr if case.candidate else None,
        evidence=evidence, steps_used=config.AGENT_MAX_STEPS, tool_calls=tool_calls,
        model=getattr(model, "name", "?"), aborted=True,
    )


def _as_float(x: Any) -> float:
    try:
        return max(0.0, min(1.0, float(x)))
    except Exception:
        return 0.0


class InvestigatorAgent:
    """Adapts the ReAct loop to the ``LLMProvider`` protocol so it flows through
    the engine's existing ``resolve_case`` guardrails unchanged."""

    name = "agent"

    def __init__(self, model: AgentModel, ctx: ToolContext):
        self.model = model
        self.ctx = ctx

    def resolve(self, case: AmbiguousCase) -> LLMResult:
        r = investigate(case, self.ctx, self.model)
        return LLMResult(
            resolution=r.resolution,
            reason=r.reason,
            confidence=r.confidence,
            model=f"agent:{r.model}",
            raw=None,
            evidence=r.evidence,
            agent_steps=r.steps_used,
            agent_tool_calls=r.tool_calls,
            agent_matched_utr=r.matched_utr,
        ).normalized()
