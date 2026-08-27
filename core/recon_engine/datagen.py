"""Synthetic data generator for the reconciliation loop.

Produces three linked datasets plus a hidden ground-truth file:

    1. gateway_settlements.csv  (txn_id, order_id, gross, fee, tax, net, settled_date)
    2. bank_statement.csv       (utr, credit_amount, value_date, narration)
    3. order_ledger.csv         (order_id, invoice_amount, currency, status, date)
    4. ground_truth.csv         (txn_id, order_id, true_status, is_true_match, dirty, note)

~20% of records are deliberately "dirty" — fee rounding, date skew, missing bank
credits, duplicate UTRs, split settlements, currency edges and wrong order
mappings. The generator is seeded, so ground truth is exact and reproducible.

The design intent behind the dirty cases (so tests and metrics line up):
  * fee_rounding_gray / date_skew_gray  -> TRUE status MATCHED, but only an LLM
    (or a human) should confirm them; deterministic rules alone must abstain.
  * everything else                     -> a specific exception a deterministic
    rule can and should catch on its own.
"""

from __future__ import annotations

import csv
import os
import random
import string
from datetime import date, timedelta
from dataclasses import dataclass
from typing import Any

try:
    from faker import Faker
except Exception:  # pragma: no cover - faker is a declared dependency
    Faker = None  # type: ignore

from .models import Status


# Razorpay-like fee model: 2% platform fee + 18% GST on the fee.
FEE_RATE = 0.02
GST_RATE = 0.18


def _round2(x: float) -> float:
    return round(x + 1e-9, 2)


def _utr() -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=16))


@dataclass
class _Plan:
    """How many of each dirty case to inject. The remainder are clean."""

    total: int = 60
    fee_rounding_gray: int = 3     # TRUE=MATCHED, needs LLM to confirm
    date_skew_gray: int = 2        # TRUE=MATCHED, needs LLM to confirm
    mismatch_amount: int = 2       # TRUE=MISMATCH_AMOUNT (deterministic)
    date_skew_hard: int = 1        # TRUE=DATE_SKEW (deterministic)
    missing_in_bank: int = 1       # TRUE=MISSING_IN_BANK (deterministic)
    duplicate_utr: int = 1         # TRUE=DUPLICATE_UTR (deterministic)
    missing_in_ledger: int = 1     # TRUE=MISSING_IN_LEDGER (deterministic)
    currency_mismatch: int = 1     # TRUE=CURRENCY_MISMATCH (deterministic)
    split_settlement: int = 1      # TRUE=SPLIT_SETTLEMENT (deterministic)

    @property
    def dirty_total(self) -> int:
        return (
            self.fee_rounding_gray + self.date_skew_gray + self.mismatch_amount
            + self.date_skew_hard + self.missing_in_bank + self.duplicate_utr
            + self.missing_in_ledger + self.currency_mismatch + self.split_settlement
        )


def _narration(fake, txn_id: str) -> str:
    """Bank narration typically carries the gateway reference (the txn_id)."""
    prefixes = ["RAZORPAY", "RAZORPAYX", "RZP", "PG SETTLEMENT"]
    return f"{random.choice(prefixes)} SETTLEMENT {txn_id} NEFT CR"


def generate(out_dir: str, seed: int = 42, total: int = 60) -> dict[str, str]:
    """Generate the four CSVs into ``out_dir``. Returns a map of name -> path."""

    if Faker is None:
        raise RuntimeError("Faker is required for data generation: pip install faker")

    os.makedirs(out_dir, exist_ok=True)
    random.seed(seed)
    fake = Faker()
    Faker.seed(seed)

    plan = _Plan(total=total)
    n_clean = max(0, total - plan.dirty_total)

    # Assign a "kind" to every record index.
    kinds: list[str] = (
        ["clean"] * n_clean
        + ["fee_rounding_gray"] * plan.fee_rounding_gray
        + ["date_skew_gray"] * plan.date_skew_gray
        + ["mismatch_amount"] * plan.mismatch_amount
        + ["date_skew_hard"] * plan.date_skew_hard
        + ["missing_in_bank"] * plan.missing_in_bank
        + ["duplicate_utr"] * plan.duplicate_utr
        + ["missing_in_ledger"] * plan.missing_in_ledger
        + ["currency_mismatch"] * plan.currency_mismatch
        + ["split_settlement"] * plan.split_settlement
    )
    random.shuffle(kinds)

    settlements: list[dict[str, Any]] = []
    bank_rows: list[dict[str, Any]] = []
    orders: list[dict[str, Any]] = []
    truth: list[dict[str, Any]] = []

    base_day = date(2026, 7, 1)

    for i, kind in enumerate(kinds):
        txn_id = f"TXN{10000 + i}"
        order_id = f"ORD{20000 + i}"

        # Keep amounts modest so the "gray" fee-rounding deltas can sit above the
        # hard tolerance yet under the LLM's absolute delta cap (see config).
        if kind in ("fee_rounding_gray",):
            gross = _round2(random.uniform(250, 600))
        else:
            gross = _round2(random.uniform(500, 8000))

        fee = _round2(gross * FEE_RATE)
        tax = _round2(fee * GST_RATE)
        net = _round2(gross - fee - tax)
        settled = base_day + timedelta(days=random.randint(0, 25))

        currency = "INR"
        order_status = "paid"

        # Bank credit defaults (a clean, in-window credit for `net`).
        make_bank = True
        credit_amount = net
        value_date = settled + timedelta(days=random.randint(0, 2))  # T+0..T+2
        utr = _utr()
        duplicate_bank = False
        split = False
        make_order = True

        true_status = Status.MATCHED
        note = "clean: all three sources agree within tolerance"

        if kind == "fee_rounding_gray":
            # Bank credits ~₹3.5 short of net: beyond hard tolerance, within the
            # gray band and under the LLM delta cap. TRUE match.
            credit_amount = _round2(net - 3.5)
            true_status = Status.MATCHED
            note = "fee-rounding gray zone (~₹3.5 short) — LLM should confirm MATCHED"
        elif kind == "date_skew_gray":
            value_date = settled + timedelta(days=3)  # T+3, just past the window
            true_status = Status.MATCHED
            note = "date skew T+3 (gray) — LLM should confirm MATCHED"
        elif kind == "mismatch_amount":
            credit_amount = _round2(net - random.uniform(60, 400))  # clearly wrong
            true_status = Status.MISMATCH_AMOUNT
            note = "bank credit differs from net beyond gray tolerance"
        elif kind == "date_skew_hard":
            value_date = settled + timedelta(days=random.randint(6, 12))
            true_status = Status.DATE_SKEW
            note = "bank credit far outside the settlement date window"
        elif kind == "missing_in_bank":
            make_bank = False
            true_status = Status.MISSING_IN_BANK
            note = "no matching bank credit was ever received"
        elif kind == "duplicate_utr":
            duplicate_bank = True  # emit two identical bank rows (same UTR)
            true_status = Status.DUPLICATE_UTR
            note = "same UTR appears twice in the bank statement"
        elif kind == "missing_in_ledger":
            make_order = False  # settlement references an order_id not in the ledger
            true_status = Status.MISSING_IN_LEDGER
            note = "settlement's order_id is absent from the order ledger"
        elif kind == "currency_mismatch":
            currency = "USD"
            true_status = Status.CURRENCY_MISMATCH
            note = "order booked in a non-base currency (USD)"
        elif kind == "split_settlement":
            split = True  # net paid out as two smaller credits
            true_status = Status.SPLIT_SETTLEMENT
            note = "one settlement paid across two bank credits"

        settlements.append(
            {
                "txn_id": txn_id,
                "order_id": order_id,
                "gross": gross,
                "fee": fee,
                "tax": tax,
                "net": net,
                "settled_date": settled.isoformat(),
            }
        )

        if make_order:
            orders.append(
                {
                    "order_id": order_id,
                    "invoice_amount": gross,
                    "currency": currency,
                    "status": order_status,
                    "date": settled.isoformat(),
                }
            )

        if make_bank:
            if split:
                a = _round2(credit_amount * 0.6)
                b = _round2(credit_amount - a)
                for part in (a, b):
                    bank_rows.append(
                        {
                            "utr": _utr(),
                            "credit_amount": part,
                            "value_date": value_date.isoformat(),
                            "narration": _narration(fake, txn_id),
                        }
                    )
            else:
                row = {
                    "utr": utr,
                    "credit_amount": credit_amount,
                    "value_date": value_date.isoformat(),
                    "narration": _narration(fake, txn_id),
                }
                bank_rows.append(row)
                if duplicate_bank:
                    bank_rows.append(dict(row))  # exact duplicate, same UTR

        truth.append(
            {
                "txn_id": txn_id,
                "order_id": order_id,
                "true_status": true_status.value,
                "is_true_match": true_status is Status.MATCHED,
                "dirty": kind != "clean",
                "kind": kind,
                "note": note,
            }
        )

    # Shuffle bank rows so ordering carries no signal.
    random.shuffle(bank_rows)
    random.shuffle(orders)

    paths = {
        "gateway_settlements": os.path.join(out_dir, "gateway_settlements.csv"),
        "bank_statement": os.path.join(out_dir, "bank_statement.csv"),
        "order_ledger": os.path.join(out_dir, "order_ledger.csv"),
        "ground_truth": os.path.join(out_dir, "ground_truth.csv"),
    }

    _write_csv(paths["gateway_settlements"], settlements,
               ["txn_id", "order_id", "gross", "fee", "tax", "net", "settled_date"])
    _write_csv(paths["bank_statement"], bank_rows,
               ["utr", "credit_amount", "value_date", "narration"])
    _write_csv(paths["order_ledger"], orders,
               ["order_id", "invoice_amount", "currency", "status", "date"])
    _write_csv(paths["ground_truth"], truth,
               ["txn_id", "order_id", "true_status", "is_true_match", "dirty", "kind", "note"])

    return paths


def _write_csv(path: str, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow(r)


if __name__ == "__main__":  # pragma: no cover
    import argparse

    ap = argparse.ArgumentParser(description="Generate synthetic reconciliation data.")
    ap.add_argument("--out", default="data", help="output directory")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--total", type=int, default=60)
    args = ap.parse_args()
    paths = generate(args.out, seed=args.seed, total=args.total)
    for name, p in paths.items():
        print(f"{name}: {p}")
