"""Investigable-exception auditor + injector.

Answers one question honestly: does the dataset contain exceptions that *reward
investigation* — i.e. cases where the single-shot resolver is WRONG/escalates but
the agent's read-only tools can reach the correct answer AND that answer survives
the existing guardrails?

This module is a pure-Python dataset utility — it makes NO LLM calls and does NOT
touch the deterministic matcher, the guardrails, or the agent. The injector only
APPENDS realistic rows (never mutates existing ones) and writes consistent ground
truth, so metrics stay honest.

Why the injected class is a SPLIT-with-decoy:
  The guardrail gates a MATCHED verdict on the matcher's aggregate
  `case.amount_delta`, so a MATCHED-type lift (agent finds the right credit where
  the pre-selected candidate was wrong) is blocked offline — the aggregate delta
  trips the cap. The clean, guardrail-surviving lift is a NON-MATCH
  reclassification: a messy multi-candidate case the single-shot arm calls
  MISMATCH_AMOUNT, which the agent's `find_split_or_merged` correctly resolves to
  SPLIT_SETTLEMENT. That is what we inject and verify.
"""

from __future__ import annotations

import csv
import os
import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Optional

from . import config
from .config import AMOUNT_DELTA_CAP
import string

from .datagen import FEE_RATE, GST_RATE, _round2
from .dataio import load_all, load_ground_truth
from .llm_provider import SimulatedLLMProvider
from .matcher import AmbiguousCase, DeterministicMatcher
from .models import Status
from .resolver import resolve_case
from .agent.tools import ToolContext, find_split_or_merged, search_bank_by_amount


# --------------------------------------------------------------------------- #
# Top-of-file config
# --------------------------------------------------------------------------- #
MIN_LIFT_CASES: int = 3
INJECT_SEED: int = 4242
INJECTED_ID_PREFIX: str = "INJ"


@dataclass
class Report:
    total_records: int
    dirty_records: int
    reached_agent: int                       # became AmbiguousCase
    by_true_status: dict[str, int] = field(default_factory=dict)
    lift_eligible: list[dict[str, Any]] = field(default_factory=list)

    @property
    def lift_eligible_count(self) -> int:
        return len(self.lift_eligible)

    def can_show_lift(self) -> bool:
        return self.lift_eligible_count > 0


# --------------------------------------------------------------------------- #
# Auditor
# --------------------------------------------------------------------------- #
def _tool_reachable(amb: AmbiguousCase, ctx: ToolContext, truth_status: str) -> Optional[str]:
    """Can a read-only tool reach the correct answer? Returns a short evidence
    string when yes, else None. Only claims classes we can actually verify."""
    s = amb.settlement
    if truth_status == Status.SPLIT_SETTLEMENT.value:
        r = find_split_or_merged(ctx, order_id=s.order_id)
        if r.get("split_detected") or r.get("possible_summing_pair"):
            pair = r.get("split_credits") or r.get("possible_summing_pair") or []
            utrs = [c.get("utr") for c in pair]
            return f"find_split_or_merged surfaced a split pair {utrs}"
        return None
    if truth_status == Status.MATCHED.value:
        r = search_bank_by_amount(ctx, s.net, tolerance=AMOUNT_DELTA_CAP)
        for c in r.get("candidates", []):
            if c.get("references_txn") == s.txn_id and c.get("delta_vs_net", 99) <= AMOUNT_DELTA_CAP:
                return f"search_bank_by_amount found exact credit {c.get('utr')}"
        return None
    return None


def _survives_guardrail(amb: AmbiguousCase, truth_status: str) -> bool:
    """A MATCHED verdict must clear the aggregate-delta cap; non-match verdicts
    are never gated, so they always survive."""
    if truth_status != Status.MATCHED.value:
        return True
    return amb.amount_delta is None or amb.amount_delta <= AMOUNT_DELTA_CAP


def audit(data_dir: str) -> Report:
    data = load_all(data_dir)
    truth = load_ground_truth(data_dir)
    matcher = DeterministicMatcher(data.settlements, data.bank, data.orders)
    ctx = ToolContext.from_data(data.settlements, data.bank, data.orders)
    single = SimulatedLLMProvider()

    by_true: dict[str, int] = {}
    for gt in truth.values():
        if gt.dirty:
            by_true[gt.true_status] = by_true.get(gt.true_status, 0) + 1

    reached = 0
    lift: list[dict[str, Any]] = []
    for s in data.settlements:
        _, amb = matcher.classify(s)
        if amb is None:
            continue
        reached += 1
        gt = truth.get(s.txn_id)
        if gt is None:
            continue
        # Would the single-shot arm get it wrong or escalate?
        single_dec, _ = resolve_case(amb, single)
        single_wrong = single_dec.status.value != gt.true_status
        reachable = _tool_reachable(amb, ctx, gt.true_status)
        survives = _survives_guardrail(amb, gt.true_status)
        if single_wrong and reachable and survives:
            lift.append({
                "txn_id": s.txn_id,
                "true_status": gt.true_status,
                "single_shot_said": single_dec.status.value,
                "tool_evidence": reachable,
                "class": _classify(gt.true_status),
            })

    return Report(
        total_records=len(data.settlements),
        dirty_records=sum(1 for gt in truth.values() if gt.dirty),
        reached_agent=reached,
        by_true_status=by_true,
        lift_eligible=lift,
    )


def _classify(true_status: str) -> str:
    return {
        Status.SPLIT_SETTLEMENT.value: "split/merged",
        Status.MATCHED.value: "mis-mapped/out-of-window (recoverable match)",
        Status.DATE_SKEW.value: "out-of-window date skew",
    }.get(true_status, "other")


# --------------------------------------------------------------------------- #
# Injector (append-only, seeded, idempotent)
# --------------------------------------------------------------------------- #
def _read_rows(path: str) -> tuple[list[str], list[dict[str, str]]]:
    with open(path, newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        rows = list(r)
        return list(r.fieldnames or []), rows


def _append_rows(path: str, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        for row in rows:
            w.writerow(row)


def _existing_injected(data_dir: str) -> int:
    path = os.path.join(data_dir, "gateway_settlements.csv")
    _, rows = _read_rows(path)
    return sum(1 for r in rows if str(r.get("txn_id", "")).startswith(INJECTED_ID_PREFIX))


def _build_split_case(idx: int, rng: random.Random) -> dict[str, Any]:
    """One realistic SPLIT-with-decoy investigable case.

    The settlement's net is paid across two bank credits (a genuine split), but a
    third credit erroneously carries the same gateway reference (a real
    data-quality issue), so the referenced credits do NOT cleanly sum to net.
    The deterministic layer therefore cannot auto-classify it as a split and
    hands it to the agent; the single-shot arm sees a large aggregate delta and
    calls MISMATCH_AMOUNT, while `find_split_or_merged` recovers the true pair.
    """
    txn = f"{INJECTED_ID_PREFIX}TXN{idx:04d}"
    order = f"{INJECTED_ID_PREFIX}ORD{idx:04d}"

    def _utr() -> str:
        return "".join(rng.choices(string.ascii_uppercase + string.digits, k=16))

    gross = _round2(rng.uniform(2500, 6000))
    fee = _round2(gross * FEE_RATE)
    tax = _round2(fee * GST_RATE)
    net = _round2(gross - fee - tax)
    settled = date(2026, 7, rng.randint(1, 24))
    vdate = (settled + timedelta(days=rng.randint(0, 2))).isoformat()

    part1 = _round2(net * 0.6)
    part2 = _round2(net - part1)          # part1 + part2 == net exactly
    decoy = _round2(net * 0.5)            # stray credit sharing the reference

    settlement = {
        "txn_id": txn, "order_id": order, "gross": gross, "fee": fee,
        "tax": tax, "net": net, "settled_date": settled.isoformat(),
    }
    order_row = {
        "order_id": order, "invoice_amount": gross, "currency": "INR",
        "status": "paid", "date": settled.isoformat(),
    }
    bank_rows = [
        {"utr": _utr(), "credit_amount": part1, "value_date": vdate,
         "narration": f"RAZORPAY SETTLEMENT {txn} PART1 NEFT CR"},
        {"utr": _utr(), "credit_amount": part2, "value_date": vdate,
         "narration": f"RAZORPAY SETTLEMENT {txn} PART2 NEFT CR"},
        {"utr": _utr(), "credit_amount": decoy, "value_date": vdate,
         "narration": f"RAZORPAY ADJ {txn} REVERSAL"},
    ]
    truth_row = {
        "txn_id": txn, "order_id": order, "true_status": Status.SPLIT_SETTLEMENT.value,
        "is_true_match": False, "dirty": True, "kind": "injected_split",
        "note": (f"net ₹{net:.2f} split across two credits (₹{part1:.2f}+₹{part2:.2f}); "
                 f"a third credit ₹{decoy:.2f} shares the reference — agent must "
                 f"find the true pair"),
    }
    return {"settlement": settlement, "order": order_row, "bank": bank_rows, "truth": truth_row}


def inject(data_dir: str, min_cases: int = MIN_LIFT_CASES, seed: int = INJECT_SEED) -> dict[str, Any]:
    """Top up the dataset to at least ``min_cases`` lift-eligible investigable
    cases. Idempotent: re-running does not double-inject (prior injected cases
    remain lift-eligible and count toward the target)."""
    report = audit(data_dir)
    need = max(0, min_cases - report.lift_eligible_count)
    if need == 0:
        return {"injected": 0, "reason": "already has enough lift-eligible cases",
                "lift_eligible": report.lift_eligible_count}

    rng = random.Random(seed + _existing_injected(data_dir))
    start = _existing_injected(data_dir)

    s_path = os.path.join(data_dir, "gateway_settlements.csv")
    o_path = os.path.join(data_dir, "order_ledger.csv")
    b_path = os.path.join(data_dir, "bank_statement.csv")
    t_path = os.path.join(data_dir, "ground_truth.csv")
    s_fields, _ = _read_rows(s_path)
    o_fields, _ = _read_rows(o_path)
    b_fields, _ = _read_rows(b_path)
    t_fields, _ = _read_rows(t_path)

    injected = []
    for i in range(need):
        case = _build_split_case(start + i + 1, rng)
        _append_rows(s_path, s_fields, [case["settlement"]])
        _append_rows(o_path, o_fields, [case["order"]])
        _append_rows(b_path, b_fields, case["bank"])
        _append_rows(t_path, t_fields, [case["truth"]])
        injected.append({
            "txn_id": case["settlement"]["txn_id"],
            "class": "split/merged",
            "true_status": Status.SPLIT_SETTLEMENT.value,
            "correct_match": f"{case['bank'][0]['credit_amount']} + {case['bank'][1]['credit_amount']} = net",
            "decoy": case["bank"][2]["credit_amount"],
        })

    after = audit(data_dir)
    return {
        "injected": len(injected),
        "cases": injected,
        "lift_eligible_before": report.lift_eligible_count,
        "lift_eligible_after": after.lift_eligible_count,
    }
