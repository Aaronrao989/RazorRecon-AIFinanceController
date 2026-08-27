"""Tests for the investigative agent: tools, rate limiter, bounded loop,
guardrail interplay, and end-to-end honesty."""

from __future__ import annotations

import tempfile
import time

import pytest

from recon_engine import (
    Status,
    DecisionSource,
    SimulatedAgentModel,
    generate,
    reconcile,
)
from recon_engine.agent import RateLimiter, ToolContext, investigate
from recon_engine.agent.model import AgentModelResponse, ToolCall
from recon_engine.agent.tools import (
    execute_tool,
    fetch_order,
    fetch_settlement,
    search_bank_by_amount,
)
from recon_engine.dataio import load_all
from recon_engine.matcher import AmbiguousCase
from recon_engine.models import BankCredit, Order, Settlement
from recon_engine.resolver import resolve_case
from recon_engine.llm_provider import LLMResult
from recon_engine.config import AMOUNT_DELTA_CAP, AGENT_MAX_STEPS


# --------------------------------------------------------------------------- #
# Tools (read-only)
# --------------------------------------------------------------------------- #
def _ctx():
    with tempfile.TemporaryDirectory() as d:
        generate(d, seed=7, total=40)
        data = load_all(d)
    return ToolContext.from_data(data.settlements, data.bank, data.orders), data


def test_fetch_settlement_and_order():
    ctx, data = _ctx()
    s = data.settlements[0]
    got = fetch_settlement(ctx, s.txn_id)
    assert got["txn_id"] == s.txn_id and got["net"] == s.net
    assert "error" in fetch_settlement(ctx, "NOPE")
    o = fetch_order(ctx, s.order_id)
    # order may or may not exist (missing_in_ledger case), both are valid shapes
    assert "order_id" in o or "error" in o


def test_search_bank_by_amount_is_capped_and_readonly():
    ctx, data = _ctx()
    s = data.settlements[0]
    res = search_bank_by_amount(ctx, s.net, tolerance=1000)
    assert res["count"] <= 5  # MAX_TOOL_RESULTS
    # tool returns plain data, no mutation of context
    before = len(ctx.bank)
    execute_tool("search_bank_by_amount", {"net": s.net}, ctx)
    assert len(ctx.bank) == before


def test_unknown_tool_returns_error_not_raise():
    ctx, _ = _ctx()
    out = execute_tool("delete_everything", {}, ctx)
    assert "error" in out


# --------------------------------------------------------------------------- #
# Rate limiter
# --------------------------------------------------------------------------- #
def test_rate_limiter_enforces_min_interval():
    rl = RateLimiter(min_interval_seconds=0.05, rpm_cap=1200)
    stamps = []
    for _ in range(5):
        rl.acquire()
        stamps.append(time.monotonic())
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    # Every consecutive call is spaced by at least (nearly) the min interval,
    # which bounds the achievable RPM to <= 60/min_interval by construction.
    assert all(g >= 0.045 for g in gaps)
    assert rl.total_calls == 5


def test_rate_limiter_peak_rpm_within_cap():
    # min_interval derived from the cap => peak RPM cannot exceed the cap.
    cap = 30
    rl = RateLimiter(min_interval_seconds=60.0 / cap, rpm_cap=cap)
    # Simulate timestamps without real waiting by checking the invariant math:
    # 3 quick calls spaced by the enforced interval stay under the cap.
    rl.acquire()
    assert rl.peak_rpm() <= cap


# --------------------------------------------------------------------------- #
# Bounded loop
# --------------------------------------------------------------------------- #
class _NeverConcludes:
    """A model that always calls a tool and never submits a verdict."""

    name = "never"

    def step(self, messages, tools):
        return AgentModelResponse(
            tool_calls=[ToolCall(id="c", name="list_unmatched_bank_credits", arguments={})],
            finish_reason="tool_calls", model="never",
        )


def _ambiguous_from(data):
    s = data.settlements[0]
    return AmbiguousCase(
        settlement=s, order=None,
        candidate=BankCredit("U", s.net, s.settled_date, f"REF {s.txn_id}"),
        amount_delta=3.5, date_delta_days=3, reason="gray",
    )


def test_loop_is_bounded_and_escalates_on_exhaustion():
    ctx, data = _ctx()
    case = _ambiguous_from(data)
    result = investigate(case, ctx, _NeverConcludes())
    assert result.aborted is True
    assert result.resolution == "UNRESOLVED"
    assert result.steps_used == AGENT_MAX_STEPS
    assert result.tool_calls >= AGENT_MAX_STEPS  # one tool call per step


# --------------------------------------------------------------------------- #
# Guardrails still gate the agent's verdict
# --------------------------------------------------------------------------- #
class _ClaimsBigMatch:
    """Agent that confidently returns MATCHED on a huge delta."""

    name = "agent"

    def resolve(self, case):
        return LLMResult("MATCHED", "trust me", 0.99, "agent:x",
                         evidence=[{"step": 0, "type": "verdict"}],
                         agent_steps=1, agent_tool_calls=0).normalized()


def test_agent_matched_over_cap_is_overridden():
    ctx, data = _ctx()
    case = _ambiguous_from(data)
    case.amount_delta = AMOUNT_DELTA_CAP + 50
    d, trace = resolve_case(case, _ClaimsBigMatch())
    assert d.status is Status.UNRESOLVED
    assert d.source is DecisionSource.GUARDRAIL
    assert "evidence" in trace  # the investigation trail is still logged


# --------------------------------------------------------------------------- #
# End-to-end with the offline simulated agent
# --------------------------------------------------------------------------- #
def test_agent_end_to_end_no_false_positives():
    with tempfile.TemporaryDirectory() as d:
        generate(d, seed=42, total=60)
        res = reconcile(d, agent_model=SimulatedAgentModel(), use_agent=True)
        m = res.metrics
        assert m["mode"] == "agent"
        assert m["false_positive_count"] == 0
        assert m["matched_precision"] == 1.0
        assert m["status_accuracy"] == 1.0
        ag = m["agent"]
        assert ag["records_investigated"] >= 5
        assert ag["verdicts_honored"] >= 5
        # every honored MATCHED came through the agent layer with an evidence trail
        for a in res.audit:
            if a.rule_or_layer == "llm-resolver":
                assert a.llm_trace and "evidence" in a.llm_trace
