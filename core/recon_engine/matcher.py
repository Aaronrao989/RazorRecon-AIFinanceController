"""Deterministic matching layer.

Runs first, on every record. It resolves the clear-cut cases with auditable
rules and *abstains* on genuinely ambiguous ones (returning an ``AmbiguousCase``)
so the bounded LLM resolver can take a look. It never guesses on money it cannot
justify — abstaining is a first-class outcome here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from .config import TOLERANCES, BASE_CURRENCY
from .models import (
    BankCredit,
    Decision,
    DecisionSource,
    Order,
    Settlement,
    Status,
)


def _parse_date(s: str) -> Optional[date]:
    try:
        return date.fromisoformat(s.strip())
    except Exception:
        return None


def _days_between(settled: str, value: str) -> Optional[int]:
    d1, d2 = _parse_date(settled), _parse_date(value)
    if d1 is None or d2 is None:
        return None
    return (d2 - d1).days


@dataclass
class AmbiguousCase:
    """Everything the LLM needs to adjudicate one uncertain record.

    This is also exactly what gets written to the audit trail, so the reasoning
    is reproducible from the logged inputs alone.
    """

    settlement: Settlement
    order: Optional[Order]
    candidate: Optional[BankCredit]
    amount_delta: Optional[float]
    date_delta_days: Optional[int]
    reason: str  # why the deterministic layer abstained

    def summary(self) -> str:
        s = self.settlement
        parts = [
            f"Settlement {s.txn_id} (order {s.order_id}): net ₹{s.net:.2f}, "
            f"settled {s.settled_date}."
        ]
        if self.order is not None:
            parts.append(
                f"Ledger: invoice ₹{self.order.invoice_amount:.2f} "
                f"{self.order.currency}, status {self.order.status}."
            )
        if self.candidate is not None:
            parts.append(
                f"Bank credit: ₹{self.candidate.credit_amount:.2f} on "
                f"{self.candidate.value_date} (UTR {self.candidate.utr}); "
                f"narration \"{self.candidate.narration}\"."
            )
        if self.amount_delta is not None:
            parts.append(f"Amount delta vs net: ₹{self.amount_delta:.2f}.")
        if self.date_delta_days is not None:
            parts.append(f"Date delta: T+{self.date_delta_days}.")
        parts.append(f"Deterministic layer abstained because: {self.reason}")
        return " ".join(parts)


class DeterministicMatcher:
    def __init__(self, settlements, bank, orders):
        self.settlements: list[Settlement] = settlements
        self.bank: list[BankCredit] = bank
        self.orders_by_id: dict[str, Order] = {o.order_id: o for o in orders}

        # Index bank rows by the txn_id embedded in their narration (strong link).
        self._bank_by_txn: dict[str, list[BankCredit]] = {}
        for b in bank:
            for s in settlements:
                if s.txn_id in b.narration:
                    self._bank_by_txn.setdefault(s.txn_id, []).append(b)

        # How often each UTR appears across the whole statement (duplicate check).
        self._utr_counts: dict[str, int] = {}
        for b in bank:
            self._utr_counts[b.utr] = self._utr_counts.get(b.utr, 0) + 1

    def classify(self, s: Settlement) -> tuple[Optional[Decision], Optional[AmbiguousCase]]:
        """Return ``(decision, None)`` for a resolved record, or
        ``(None, ambiguous_case)`` when the LLM should adjudicate."""

        order = self.orders_by_id.get(s.order_id)

        # 1) Ledger existence.
        if order is None:
            return (
                Decision(
                    txn_id=s.txn_id, order_id=s.order_id, status=Status.MISSING_IN_LEDGER,
                    source=DecisionSource.RULE,
                    reason="order_id not found in the order ledger (likely a wrong mapping)",
                ),
                None,
            )

        # 2) Currency.
        if order.currency != BASE_CURRENCY:
            return (
                Decision(
                    txn_id=s.txn_id, order_id=s.order_id, status=Status.CURRENCY_MISMATCH,
                    source=DecisionSource.RULE,
                    reason=f"order currency {order.currency} is not the base currency {BASE_CURRENCY}",
                ),
                None,
            )

        # 3) Candidate bank credits (strong link via txn_id in narration).
        candidates = list(self._bank_by_txn.get(s.txn_id, []))

        if not candidates:
            return (
                Decision(
                    txn_id=s.txn_id, order_id=s.order_id, status=Status.MISSING_IN_BANK,
                    source=DecisionSource.RULE,
                    reason="no bank credit references this settlement",
                ),
                None,
            )

        if len(candidates) > 1:
            utrs = {c.utr for c in candidates}
            if len(utrs) == 1:
                return (
                    Decision(
                        txn_id=s.txn_id, order_id=s.order_id, status=Status.DUPLICATE_UTR,
                        source=DecisionSource.RULE, matched_utr=candidates[0].utr,
                        reason=f"UTR {candidates[0].utr} appears {len(candidates)} times for this settlement",
                    ),
                    None,
                )
            total = sum(c.credit_amount for c in candidates)
            if TOLERANCES.within_gray_amount(s.net, total):
                return (
                    Decision(
                        txn_id=s.txn_id, order_id=s.order_id, status=Status.SPLIT_SETTLEMENT,
                        source=DecisionSource.RULE, amount_delta=round(abs(s.net - total), 2),
                        reason=(f"net ₹{s.net:.2f} paid across {len(candidates)} credits "
                                f"summing to ₹{total:.2f}"),
                    ),
                    None,
                )
            # Multiple credits that neither dup nor cleanly split -> let the LLM look.
            return (
                None,
                AmbiguousCase(
                    settlement=s, order=order, candidate=candidates[0],
                    amount_delta=round(abs(s.net - total), 2), date_delta_days=None,
                    reason=(f"{len(candidates)} candidate bank credits with distinct UTRs "
                            f"that do not cleanly sum to net"),
                ),
            )

        # 4) Exactly one candidate — the common path.
        c = candidates[0]

        # Duplicate UTR even with a single narration match.
        if self._utr_counts.get(c.utr, 0) > 1:
            return (
                Decision(
                    txn_id=s.txn_id, order_id=s.order_id, status=Status.DUPLICATE_UTR,
                    source=DecisionSource.RULE, matched_utr=c.utr,
                    reason=f"UTR {c.utr} is not unique in the bank statement",
                ),
                None,
            )

        amount_delta = round(abs(c.credit_amount - s.net), 2)
        date_delta = _days_between(s.settled_date, c.value_date)

        hard_amount = TOLERANCES.within_hard_amount(s.net, c.credit_amount)
        gray_amount = TOLERANCES.within_gray_amount(s.net, c.credit_amount)
        hard_date = date_delta is not None and 0 <= date_delta <= TOLERANCES.date_window_days
        gray_date = date_delta is not None and 0 <= date_delta <= TOLERANCES.date_window_days_gray

        # Clean match.
        if hard_amount and hard_date:
            return (
                Decision(
                    txn_id=s.txn_id, order_id=s.order_id, status=Status.MATCHED,
                    source=DecisionSource.RULE, amount_delta=amount_delta, matched_utr=c.utr,
                    reason=(f"bank credit ₹{c.credit_amount:.2f} matches net ₹{s.net:.2f} "
                            f"within tolerance at T+{date_delta}"),
                ),
                None,
            )

        # Clearly wrong money — no need to spend an LLM call.
        if not gray_amount:
            return (
                Decision(
                    txn_id=s.txn_id, order_id=s.order_id, status=Status.MISMATCH_AMOUNT,
                    source=DecisionSource.RULE, amount_delta=amount_delta, matched_utr=c.utr,
                    reason=(f"bank credit ₹{c.credit_amount:.2f} differs from net "
                            f"₹{s.net:.2f} by ₹{amount_delta:.2f}, beyond gray tolerance"),
                ),
                None,
            )

        # Clearly outside any plausible settlement window.
        if not gray_date:
            return (
                Decision(
                    txn_id=s.txn_id, order_id=s.order_id, status=Status.DATE_SKEW,
                    source=DecisionSource.RULE, amount_delta=amount_delta, matched_utr=c.utr,
                    reason=(f"bank credit dated {c.value_date} is T+{date_delta} vs settlement, "
                            f"far outside the T+{TOLERANCES.date_window_days_gray} window"),
                ),
                None,
            )

        # Gray zone on amount and/or date -> hand to the LLM.
        why = []
        if not hard_amount:
            why.append(f"amount off by ₹{amount_delta:.2f} (beyond hard tolerance)")
        if not hard_date:
            why.append(f"credit landed at T+{date_delta} (beyond the T+{TOLERANCES.date_window_days} window)")
        return (
            None,
            AmbiguousCase(
                settlement=s, order=order, candidate=c,
                amount_delta=amount_delta, date_delta_days=date_delta,
                reason="; ".join(why) or "ambiguous",
            ),
        )
