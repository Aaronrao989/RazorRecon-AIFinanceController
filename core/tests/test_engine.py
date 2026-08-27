"""Tests for the standalone reconciliation engine.

These exercise the core library directly — no web framework, no network — proving
the engine is usable and testable on its own.
"""

from __future__ import annotations

import os
import tempfile

import pytest

from recon_engine import (
    Decision,
    DecisionSource,
    SimulatedLLMProvider,
    Status,
    generate,
    reconcile,
)
from recon_engine.config import AMOUNT_DELTA_CAP, CONFIDENCE_THRESHOLD
from recon_engine.matcher import AmbiguousCase, DeterministicMatcher
from recon_engine.models import BankCredit, Order, Settlement
from recon_engine.llm_provider import LLMError, LLMResult
from recon_engine.resolver import resolve_case


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #
def _settlement(**kw):
    base = dict(txn_id="TXN1", order_id="ORD1", gross=1000.0, fee=20.0, tax=3.6,
                net=976.4, settled_date="2026-07-01")
    base.update(kw)
    return Settlement(**base)


def _order(**kw):
    base = dict(order_id="ORD1", invoice_amount=1000.0, currency="INR",
                status="paid", date="2026-07-01")
    base.update(kw)
    return Order(**base)


def _bank(**kw):
    base = dict(utr="UTR1", credit_amount=976.4, value_date="2026-07-02",
                narration="RAZORPAY SETTLEMENT TXN1 NEFT CR")
    base.update(kw)
    return BankCredit(**base)


def _classify(settlement, bank, orders):
    m = DeterministicMatcher([settlement], bank, orders)
    return m.classify(settlement)


# --------------------------------------------------------------------------- #
# Deterministic matcher
# --------------------------------------------------------------------------- #
def test_clean_match():
    d, amb = _classify(_settlement(), [_bank()], [_order()])
    assert amb is None
    assert d.status is Status.MATCHED
    assert d.source is DecisionSource.RULE


def test_missing_in_ledger():
    d, amb = _classify(_settlement(order_id="ORDX"), [_bank()], [_order()])
    assert d.status is Status.MISSING_IN_LEDGER


def test_currency_mismatch():
    d, amb = _classify(_settlement(), [_bank()], [_order(currency="USD")])
    assert d.status is Status.CURRENCY_MISMATCH


def test_missing_in_bank():
    d, amb = _classify(_settlement(), [], [_order()])
    assert d.status is Status.MISSING_IN_BANK


def test_duplicate_utr():
    d, amb = _classify(_settlement(), [_bank(), _bank()], [_order()])
    assert d.status is Status.DUPLICATE_UTR


def test_split_settlement():
    parts = [
        _bank(utr="U1", credit_amount=576.4),
        _bank(utr="U2", credit_amount=400.0),
    ]
    d, amb = _classify(_settlement(), parts, [_order()])
    assert d.status is Status.SPLIT_SETTLEMENT


def test_large_amount_mismatch_is_deterministic():
    d, amb = _classify(_settlement(), [_bank(credit_amount=800.0)], [_order()])
    assert amb is None
    assert d.status is Status.MISMATCH_AMOUNT


def test_far_date_skew_is_deterministic():
    d, amb = _classify(_settlement(), [_bank(value_date="2026-07-20")], [_order()])
    assert d.status is Status.DATE_SKEW


def test_gray_zone_abstains_for_llm():
    # net 976.4, credit short by ~4 (beyond hard tol, within gray, under cap)
    d, amb = _classify(_settlement(net=580.0, gross=600.0),
                       [_bank(credit_amount=576.5)], [_order(invoice_amount=600.0)])
    assert d is None
    assert isinstance(amb, AmbiguousCase)
    assert amb.amount_delta == pytest.approx(3.5, abs=0.01)


# --------------------------------------------------------------------------- #
# Guardrails on the LLM resolver
# --------------------------------------------------------------------------- #
class _StubProvider:
    name = "stub"

    def __init__(self, result):
        self._result = result

    def resolve(self, case):
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def _ambiguous(delta=3.5, ddays=1):
    return AmbiguousCase(
        settlement=_settlement(net=580.0),
        order=_order(invoice_amount=600.0),
        candidate=_bank(credit_amount=580.0 - delta),
        amount_delta=delta,
        date_delta_days=ddays,
        reason="gray zone",
    )


def test_guardrail_passes_confident_small_delta():
    prov = _StubProvider(LLMResult("MATCHED", "fee rounding", 0.95, "m"))
    d, trace = resolve_case(_ambiguous(delta=3.5), prov)
    assert d.status is Status.MATCHED
    assert d.source is DecisionSource.LLM
    assert trace["guardrail"] == "passed"


def test_guardrail_escalates_low_confidence():
    prov = _StubProvider(LLMResult("MATCHED", "maybe", 0.5, "m"))
    d, trace = resolve_case(_ambiguous(delta=3.5), prov)
    assert d.status is Status.UNRESOLVED
    assert d.source is DecisionSource.GUARDRAIL
    assert trace["guardrail"] == "below_confidence_threshold"


def test_guardrail_escalates_delta_over_cap():
    big = AMOUNT_DELTA_CAP + 10
    prov = _StubProvider(LLMResult("MATCHED", "confident but big", 0.99, "m"))
    d, trace = resolve_case(_ambiguous(delta=big), prov)
    assert d.status is Status.UNRESOLVED
    assert d.source is DecisionSource.GUARDRAIL
    assert trace["guardrail"] == "amount_delta_cap_exceeded"


def test_llm_error_escalates_not_crashes():
    prov = _StubProvider(LLMError("timeout"))
    d, trace = resolve_case(_ambiguous(), prov)
    assert d.status is Status.UNRESOLVED
    assert d.source is DecisionSource.ERROR


def test_llm_cannot_invent_a_match_out_of_thin_air():
    # Even if the model claims MATCHED, a delta over the cap is escalated.
    prov = _StubProvider(LLMResult("MATCHED", "trust me", 1.0, "m"))
    d, _ = resolve_case(_ambiguous(delta=999.0), prov)
    assert d.status is not Status.MATCHED


# --------------------------------------------------------------------------- #
# Full pipeline against ground truth
# --------------------------------------------------------------------------- #
def test_end_to_end_no_false_positives_with_simulator():
    with tempfile.TemporaryDirectory() as d:
        generate(d, seed=42, total=60)
        result = reconcile(d, provider=SimulatedLLMProvider())
        m = result.metrics
        assert m["total_records"] == 60
        assert m["false_positive_count"] == 0
        assert m["matched_precision"] == 1.0
        # Simulator resolves the gray-zone true matches -> perfect status accuracy.
        assert m["status_accuracy"] == 1.0


def test_end_to_end_without_llm_is_honest():
    with tempfile.TemporaryDirectory() as d:
        generate(d, seed=42, total=60)
        result = reconcile(d, use_llm=False)
        m = result.metrics
        # No LLM: still zero false positives; ambiguous true-matches escalate.
        assert m["false_positive_count"] == 0
        assert m["by_status"].get("UNRESOLVED", 0) >= 5
        assert m["matched_recall"] < 1.0


def test_malformed_settlement_row_becomes_exception():
    with tempfile.TemporaryDirectory() as d:
        generate(d, seed=1, total=30)
        # Corrupt one settlement row's numeric field.
        path = os.path.join(d, "gateway_settlements.csv")
        with open(path) as f:
            lines = f.readlines()
        parts = lines[1].split(",")
        parts[2] = "not_a_number"  # gross
        lines[1] = ",".join(parts)
        with open(path, "w") as f:
            f.writelines(lines)

        result = reconcile(d, provider=SimulatedLLMProvider())
        # Batch did not crash; the bad row is an UNRESOLVED exception.
        bad = [x for x in result.decisions if x.status is Status.UNRESOLVED
               and x.source is DecisionSource.ERROR]
        assert len(bad) >= 1
        assert result.metrics["load_errors"] >= 1
