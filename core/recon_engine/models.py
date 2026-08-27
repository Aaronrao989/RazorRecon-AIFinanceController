"""Domain models and the closed set of reconciliation decisions.

Kept dependency-free (only the standard library) so the classification vocabulary
can be imported anywhere — engine, tests, backend, or a notebook.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Optional


class Status(str, Enum):
    """The closed set of per-record decisions the engine can emit.

    Inherits from ``str`` so values serialise straight to JSON / CSV.
    """

    MATCHED = "MATCHED"                       # all three sources agree within tolerance
    MISMATCH_AMOUNT = "MISMATCH_AMOUNT"       # linked but money differs beyond tolerance
    MISSING_IN_BANK = "MISSING_IN_BANK"       # no bank credit for this settlement
    MISSING_IN_LEDGER = "MISSING_IN_LEDGER"   # order_id not present in the order ledger
    DUPLICATE_UTR = "DUPLICATE_UTR"           # same bank UTR claimed by >1 settlement
    DATE_SKEW = "DATE_SKEW"                    # matched on money but outside the date window
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"   # order not in the base currency
    SPLIT_SETTLEMENT = "SPLIT_SETTLEMENT"     # one order paid out across multiple credits
    UNRESOLVED = "UNRESOLVED"                 # LLM/guardrail could not safely resolve it

    @property
    def is_match(self) -> bool:
        return self is Status.MATCHED


# Statuses that represent a clean, auto-resolved match for throughput accounting.
RESOLVED_STATUSES = {Status.MATCHED}


class DecisionSource(str, Enum):
    RULE = "deterministic-rule"
    LLM = "llm-resolver"
    GUARDRAIL = "guardrail-escalation"
    ERROR = "error-escalation"


@dataclass
class Settlement:
    """A row from gateway_settlements.csv (the engine's central record)."""

    txn_id: str
    order_id: str
    gross: float
    fee: float
    tax: float
    net: float
    settled_date: str  # ISO date string

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Settlement":
        return cls(
            txn_id=str(row["txn_id"]).strip(),
            order_id=str(row["order_id"]).strip(),
            gross=float(row["gross"]),
            fee=float(row["fee"]),
            tax=float(row["tax"]),
            net=float(row["net"]),
            settled_date=str(row["settled_date"]).strip(),
        )


@dataclass
class BankCredit:
    """A row from bank_statement.csv."""

    utr: str
    credit_amount: float
    value_date: str
    narration: str

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "BankCredit":
        return cls(
            utr=str(row["utr"]).strip(),
            credit_amount=float(row["credit_amount"]),
            value_date=str(row["value_date"]).strip(),
            narration=str(row["narration"]).strip(),
        )


@dataclass
class Order:
    """A row from order_ledger.csv."""

    order_id: str
    invoice_amount: float
    currency: str
    status: str
    date: str

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Order":
        return cls(
            order_id=str(row["order_id"]).strip(),
            invoice_amount=float(row["invoice_amount"]),
            currency=str(row["currency"]).strip().upper(),
            status=str(row["status"]).strip(),
            date=str(row["date"]).strip(),
        )


@dataclass
class Decision:
    """The engine's final, explainable verdict for one settlement."""

    txn_id: str
    order_id: str
    status: Status
    source: DecisionSource
    reason: str
    confidence: float = 1.0
    amount_delta: Optional[float] = None
    matched_utr: Optional[str] = None
    llm_used: bool = False

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        d["source"] = self.source.value
        return d

    @property
    def is_exception(self) -> bool:
        return self.status is not Status.MATCHED
