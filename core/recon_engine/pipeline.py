"""End-to-end reconciliation pipeline.

Wires the layers together:
    load -> deterministic match -> (LLM resolve ambiguous) -> audit -> metrics

Exposes a single ``reconcile`` entry point with an optional ``progress_cb`` so a
caller (e.g. the FastAPI backend) can stream progress without the engine knowing
anything about HTTP. The engine stays a plain library.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from . import config
from .audit import AuditEntry, build_entry
from .dataio import LoadedData, load_all, load_ground_truth
from .llm_provider import LLMProvider, SimulatedLLMProvider
from .matcher import AmbiguousCase, DeterministicMatcher
from .metrics import compute_metrics
from .models import Decision, DecisionSource, Settlement, Status
from .resolver import resolve_case
from .agent import (
    InvestigatorAgent,
    RateLimiter,
    SimulatedAgentModel,
    ToolContext,
)
from .agent.model import AgentModel


ProgressCB = Callable[[dict[str, Any]], None]


@dataclass
class ReconResult:
    decisions: list[Decision]
    audit: list[AuditEntry]
    metrics: dict[str, Any]
    exceptions: list[Decision] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "metrics": self.metrics,
            "decisions": [d.to_dict() for d in self.decisions],
            "exceptions": [d.to_dict() for d in self.exceptions],
            "audit": [e.to_dict() for e in self.audit],
        }


def _settlement_inputs(s: Settlement) -> dict[str, Any]:
    return {
        "settlement": {
            "txn_id": s.txn_id, "order_id": s.order_id, "gross": s.gross,
            "fee": s.fee, "tax": s.tax, "net": s.net, "settled_date": s.settled_date,
        }
    }


def _resolve_llm_provider(
    provider: Optional[LLMProvider], use_llm: bool
) -> Optional[LLMProvider]:
    """Pick a provider. If the caller passed one, use it. Otherwise, if use_llm
    is on and a Groq key is present, build a GroqLLMProvider; if not, return None
    (ambiguous records will be escalated honestly, not silently faked)."""
    if provider is not None:
        return provider
    if not use_llm:
        return None
    import os
    if os.getenv(config.GROQ_API_KEY_ENV):
        # Imported lazily so the engine imports fine without `requests` installed.
        from .llm_provider import GroqLLMProvider
        try:
            return GroqLLMProvider()
        except Exception:
            return None
    return None


def _build_agent_provider(
    data: LoadedData,
    agent_model: Optional[AgentModel],
) -> tuple[Optional[InvestigatorAgent], Optional[RateLimiter]]:
    """Build the investigative agent as the resolver provider.

    Returns (agent_provider, limiter). If no model is available (no injected
    model and no Groq key), returns (None, None) so ambiguous records escalate
    honestly rather than being guessed.
    """
    import os

    ctx = ToolContext.from_data(data.settlements, data.bank, data.orders)
    limiter = RateLimiter()

    model: Optional[AgentModel] = agent_model
    if model is None and os.getenv(config.GROQ_API_KEY_ENV):
        from .agent.model import GroqToolModel  # lazy import (needs requests)
        try:
            model = GroqToolModel(limiter)
        except Exception:
            model = None
    if model is None:
        return None, None
    return InvestigatorAgent(model, ctx), limiter


def reconcile(
    data_dir: str,
    *,
    provider: Optional[LLMProvider] = None,
    agent_model: Optional[AgentModel] = None,
    use_llm: bool = True,
    use_agent: bool = False,
    progress_cb: Optional[ProgressCB] = None,
    measure: bool = True,
) -> ReconResult:
    """Run the full batch over the CSVs in ``data_dir``.

    ``provider`` — explicit single-shot LLMProvider (e.g. SimulatedLLMProvider).
    ``use_agent`` — resolve ambiguous records with the tool-calling investigative
    agent instead of a single-shot call. ``agent_model`` injects a specific
    AgentModel (e.g. SimulatedAgentModel for offline runs); otherwise a
    GroqToolModel is built when GROQ_API_KEY is set.

    In all modes, if no resolver is available the ambiguous records are escalated
    to the exception list rather than guessed.
    """
    t0 = time.time()

    def emit(**kw: Any) -> None:
        if progress_cb:
            progress_cb(kw)

    emit(phase="loading", message=f"Loading CSVs from {data_dir}")
    data: LoadedData = load_all(data_dir)

    limiter: Optional[RateLimiter] = None
    if use_agent:
        llm, limiter = _build_agent_provider(data, agent_model)
    else:
        llm = _resolve_llm_provider(provider, use_llm)
    llm_name = getattr(llm, "name", None)
    emit(
        phase="loaded",
        settlements=len(data.settlements),
        bank_rows=len(data.bank),
        orders=len(data.orders),
        load_errors=len(data.errors),
        mode="agent" if use_agent else "single-shot",
        llm_provider=llm_name or "none (ambiguous -> exception)",
    )

    matcher = DeterministicMatcher(data.settlements, data.bank, data.orders)

    decisions: list[Decision] = []
    audit: list[AuditEntry] = []

    # Malformed settlement rows -> exception entries (failure handled gracefully).
    for err in [e for e in data.errors if e.source == "gateway_settlements"]:
        txn = str(err.raw.get("txn_id", f"row{err.row_index}"))
        d = Decision(
            txn_id=txn, order_id=str(err.raw.get("order_id", "")),
            status=Status.UNRESOLVED, source=DecisionSource.ERROR,
            reason=f"malformed settlement row skipped: {err.error}",
        )
        decisions.append(d)
        audit.append(build_entry(d, {"raw_row": err.raw, "load_error": err.error}))

    total = len(data.settlements)
    llm_calls = 0

    for i, s in enumerate(data.settlements):
        decision, ambiguous = matcher.classify(s)

        if decision is not None:
            decisions.append(decision)
            inputs = _settlement_inputs(s)
            if decision.matched_utr:
                inputs["matched_utr"] = decision.matched_utr
            audit.append(build_entry(decision, inputs))
        else:
            assert ambiguous is not None
            inputs = _settlement_inputs(s)
            inputs["candidate_bank"] = (
                {
                    "utr": ambiguous.candidate.utr,
                    "credit_amount": ambiguous.candidate.credit_amount,
                    "value_date": ambiguous.candidate.value_date,
                    "narration": ambiguous.candidate.narration,
                }
                if ambiguous.candidate
                else None
            )
            inputs["order"] = (
                {
                    "invoice_amount": ambiguous.order.invoice_amount,
                    "currency": ambiguous.order.currency,
                    "status": ambiguous.order.status,
                }
                if ambiguous.order
                else None
            )

            if llm is None:
                # No LLM available — escalate honestly rather than guess.
                d = Decision(
                    txn_id=s.txn_id, order_id=s.order_id, status=Status.UNRESOLVED,
                    source=DecisionSource.GUARDRAIL, amount_delta=ambiguous.amount_delta,
                    matched_utr=ambiguous.candidate.utr if ambiguous.candidate else None,
                    reason=(f"ambiguous ({ambiguous.reason}) and no LLM configured; "
                            f"escalated to exception list"),
                )
                decisions.append(d)
                audit.append(build_entry(d, inputs, ambiguity_reason=ambiguous.reason))
            else:
                d, trace = resolve_case(ambiguous, llm)
                llm_calls += 1
                decisions.append(d)
                audit.append(
                    build_entry(d, inputs, ambiguity_reason=ambiguous.reason, llm_trace=trace)
                )

        emit(
            phase="reconciling",
            processed=i + 1,
            total=total,
            llm_calls=llm_calls,
            last_txn=s.txn_id,
            last_status=decisions[-1].status.value,
        )

    wall = time.time() - t0
    exceptions = [d for d in decisions if d.is_exception]

    metrics: dict[str, Any] = {}
    if measure:
        truth = load_ground_truth(data_dir)
        nets = {s.txn_id: s.net for s in data.settlements}
        metrics = compute_metrics(decisions, truth, settlements_net=nets, wall_clock_seconds=wall)
    metrics["llm_calls"] = llm_calls
    metrics["load_errors"] = len(data.errors)
    metrics["mode"] = "agent" if use_agent else "single-shot"

    # Agent-specific honesty stats.
    if use_agent:
        investigated = [a for a in audit if a.rule_or_layer == "llm-resolver"]
        steps = [a.llm_trace.get("agent_steps") for a in investigated
                 if a.llm_trace and a.llm_trace.get("agent_steps") is not None]
        toolc = [a.llm_trace.get("agent_tool_calls") for a in investigated
                 if a.llm_trace and a.llm_trace.get("agent_tool_calls") is not None]
        honored = sum(1 for d in decisions if d.llm_used and d.source is DecisionSource.LLM)
        overridden = sum(1 for d in decisions if d.llm_used and d.source is DecisionSource.GUARDRAIL)
        errored = sum(1 for d in decisions if d.llm_used and d.source is DecisionSource.ERROR)
        agent_stats: dict[str, Any] = {
            "records_investigated": len(investigated),
            "avg_steps": round(sum(steps) / len(steps), 2) if steps else None,
            "total_tool_calls": sum(toolc) if toolc else 0,
            "verdicts_honored": honored,
            "verdicts_overridden_by_guardrail": overridden,
            "verdicts_errored": errored,
        }
        if limiter is not None:
            agent_stats["groq"] = limiter.stats()
        metrics["agent"] = agent_stats

    emit(phase="done", **{k: metrics.get(k) for k in ("match_rate", "auto_matched", "exceptions")})

    return ReconResult(decisions=decisions, audit=audit, metrics=metrics, exceptions=exceptions)
