"""Bounded LLM exception resolver.

Wraps a raw ``LLMProvider`` verdict in hard guardrails before it is allowed to
affect a money decision:

  * A ``MATCHED`` verdict is only honoured if confidence >= CONFIDENCE_THRESHOLD
    AND the amount delta <= AMOUNT_DELTA_CAP. Otherwise the record is escalated
    to the exception list (status UNRESOLVED, source GUARDRAIL) — the model does
    not get the last word on money it isn't sure about or that moved too much.
  * Any provider failure (timeout, 429 exhaustion, unparseable output) is caught
    and escalated (source ERROR). The batch never crashes on the LLM.
"""

from __future__ import annotations

from . import config
from .llm_provider import LLMError, LLMProvider, LLMResult
from .matcher import AmbiguousCase
from .models import Decision, DecisionSource, Status


_RESOLUTION_TO_STATUS = {
    "MATCHED": Status.MATCHED,
    "MISMATCH_AMOUNT": Status.MISMATCH_AMOUNT,
    "DATE_SKEW": Status.DATE_SKEW,
    "UNRESOLVED": Status.UNRESOLVED,
    "SPLIT_SETTLEMENT": Status.SPLIT_SETTLEMENT,
    "DUPLICATE_UTR": Status.DUPLICATE_UTR,
}


def resolve_case(case: AmbiguousCase, provider: LLMProvider) -> tuple[Decision, dict]:
    """Adjudicate one ambiguous case. Returns ``(decision, llm_trace)``.

    ``llm_trace`` is a plain dict for the audit log (model, raw verdict, guardrail
    outcome). It is empty-ish only on provider construction errors.
    """
    s = case.settlement

    try:
        result: LLMResult = provider.resolve(case)
    except LLMError as e:
        decision = Decision(
            txn_id=s.txn_id, order_id=s.order_id, status=Status.UNRESOLVED,
            source=DecisionSource.ERROR, confidence=0.0,
            amount_delta=case.amount_delta,
            matched_utr=case.candidate.utr if case.candidate else None,
            llm_used=True,
            reason=f"LLM resolution failed, escalated to exception list: {e}",
        )
        return decision, {"provider": getattr(provider, "name", "?"), "error": str(e)}

    trace = {
        "provider": getattr(provider, "name", "?"),
        "model": result.model,
        "resolution": result.resolution,
        "reason": result.reason,
        "confidence": result.confidence,
        "raw": result.raw,
    }
    # Attach the agent's investigation trail (if this was the tool-calling agent).
    if result.evidence is not None:
        trace["evidence"] = result.evidence
        trace["agent_steps"] = result.agent_steps
        trace["agent_tool_calls"] = result.agent_tool_calls

    mapped = _RESOLUTION_TO_STATUS.get(result.resolution, Status.UNRESOLVED)
    delta = case.amount_delta if case.amount_delta is not None else 0.0
    # Prefer the UTR the agent says it matched; fall back to the flagged candidate.
    utr = result.agent_matched_utr or (case.candidate.utr if case.candidate else None)

    # Guardrails only gate the *positive* (MATCHED) verdict — that's the one that
    # moves money into the "resolved" bucket.
    if mapped is Status.MATCHED:
        if delta > config.AMOUNT_DELTA_CAP:
            trace["guardrail"] = "amount_delta_cap_exceeded"
            return (
                Decision(
                    txn_id=s.txn_id, order_id=s.order_id, status=Status.UNRESOLVED,
                    source=DecisionSource.GUARDRAIL, confidence=result.confidence,
                    amount_delta=delta, matched_utr=utr, llm_used=True,
                    reason=(f"LLM proposed MATCHED but amount delta ₹{delta:.2f} exceeds "
                            f"the ₹{config.AMOUNT_DELTA_CAP:.2f} cap; escalated"),
                ),
                trace,
            )
        if result.confidence < config.CONFIDENCE_THRESHOLD:
            trace["guardrail"] = "below_confidence_threshold"
            return (
                Decision(
                    txn_id=s.txn_id, order_id=s.order_id, status=Status.UNRESOLVED,
                    source=DecisionSource.GUARDRAIL, confidence=result.confidence,
                    amount_delta=delta, matched_utr=utr, llm_used=True,
                    reason=(f"LLM proposed MATCHED at confidence {result.confidence:.2f} "
                            f"< {config.CONFIDENCE_THRESHOLD}; escalated"),
                ),
                trace,
            )
        trace["guardrail"] = "passed"
        return (
            Decision(
                txn_id=s.txn_id, order_id=s.order_id, status=Status.MATCHED,
                source=DecisionSource.LLM, confidence=result.confidence,
                amount_delta=delta, matched_utr=utr, llm_used=True,
                reason=f"LLM: {result.reason}",
            ),
            trace,
        )

    # Non-match verdicts are already exceptions; accept the model's classification.
    trace["guardrail"] = "n/a (non-match verdict)"
    return (
        Decision(
            txn_id=s.txn_id, order_id=s.order_id, status=mapped,
            source=DecisionSource.LLM, confidence=result.confidence,
            amount_delta=delta, matched_utr=utr, llm_used=True,
            reason=f"LLM: {result.reason}",
        ),
        trace,
    )
