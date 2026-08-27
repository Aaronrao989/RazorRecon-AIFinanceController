"""Tests for the investigable-exception auditor + injector.

Assert that injected cases are GENUINELY lift-eligible — not merely labelled so:
  (a) the single-shot resolver gets each one wrong/escalates, and
  (b) the agent's read-only tools can reach the correct answer,
and that injection is seeded + idempotent + keeps the batch at 50+ records.
"""

from __future__ import annotations

import tempfile

import pytest

from recon_engine import generate, reconcile, SimulatedAgentModel, SimulatedLLMProvider
from recon_engine.dataio import load_all, load_ground_truth
from recon_engine.investigability import (
    MIN_LIFT_CASES,
    audit,
    inject,
)
from recon_engine.agent.tools import ToolContext, find_split_or_merged
from recon_engine.matcher import DeterministicMatcher
from recon_engine.models import Status
from recon_engine.resolver import resolve_case


def _prepare():
    d = tempfile.mkdtemp()
    generate(d, seed=42, total=60)
    return d


def test_base_dataset_has_no_lift_then_injection_fixes_it():
    d = _prepare()
    assert audit(d).lift_eligible_count == 0  # honest: base data ties
    res = inject(d)
    assert res["injected"] == MIN_LIFT_CASES
    rep = audit(d)
    assert rep.lift_eligible_count >= MIN_LIFT_CASES
    assert rep.total_records >= 50  # still a real batch


def test_injection_is_idempotent():
    d = _prepare()
    inject(d)
    n1 = audit(d).total_records
    second = inject(d)  # should be a no-op
    assert second["injected"] == 0
    assert audit(d).total_records == n1


def test_each_injected_case_is_genuinely_lift_eligible():
    d = _prepare()
    inject(d)
    data = load_all(d)
    truth = load_ground_truth(d)
    matcher = DeterministicMatcher(data.settlements, data.bank, data.orders)
    ctx = ToolContext.from_data(data.settlements, data.bank, data.orders)
    single = SimulatedLLMProvider()

    injected = [s for s in data.settlements if s.txn_id.startswith("INJ")]
    assert len(injected) >= MIN_LIFT_CASES

    for s in injected:
        gt = truth[s.txn_id]
        assert gt.true_status == Status.SPLIT_SETTLEMENT.value
        # It must actually reach the agent (become ambiguous).
        _, amb = matcher.classify(s)
        assert amb is not None, f"{s.txn_id} did not reach the agent"
        # (a) single-shot gets it wrong.
        single_dec, _ = resolve_case(amb, single)
        assert single_dec.status.value != gt.true_status
        # (b) a read-only tool can reach the correct (split) answer.
        r = find_split_or_merged(ctx, order_id=s.order_id)
        assert r.get("split_detected") or r.get("possible_summing_pair")


def test_offline_ab_shows_lift_after_injection():
    d = _prepare()
    inject(d)
    single = reconcile(d, provider=SimulatedLLMProvider(), use_llm=True)
    agent = reconcile(d, agent_model=SimulatedAgentModel(), use_agent=True)
    truth = load_ground_truth(d)
    sb = {x.txn_id: x for x in single.decisions}
    ab = {x.txn_id: x for x in agent.decisions}

    lift = [
        txn for txn, gt in truth.items()
        if sb.get(txn) and ab.get(txn)
        and ab[txn].status.value == gt.true_status
        and sb[txn].status.value != gt.true_status
    ]
    assert len(lift) >= MIN_LIFT_CASES
    # And no new false positives were introduced by the agent.
    assert agent.metrics["false_positive_count"] == 0
