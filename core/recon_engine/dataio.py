"""CSV loading with graceful handling of malformed rows and missing files.

A bad row never crashes the batch: it is collected as a ``LoadError`` and the
pipeline turns it into an exception-list entry with a clear reason.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Optional

import pandas as pd

from .models import BankCredit, Order, Settlement
from .metrics import GroundTruthRow


@dataclass
class LoadError:
    source: str
    row_index: int
    raw: dict[str, Any]
    error: str


@dataclass
class LoadedData:
    settlements: list[Settlement] = field(default_factory=list)
    bank: list[BankCredit] = field(default_factory=list)
    orders: list[Order] = field(default_factory=list)
    errors: list[LoadError] = field(default_factory=list)


def _read_csv(path: str) -> pd.DataFrame:
    if not os.path.exists(path):
        raise FileNotFoundError(f"required data file not found: {path}")
    return pd.read_csv(path, dtype=str, keep_default_na=False)


def load_all(data_dir: str) -> LoadedData:
    """Load the three source CSVs from ``data_dir`` (robust to bad rows)."""
    out = LoadedData()

    s_df = _read_csv(os.path.join(data_dir, "gateway_settlements.csv"))
    for i, row in enumerate(s_df.to_dict(orient="records")):
        try:
            out.settlements.append(Settlement.from_row(row))
        except Exception as e:
            out.errors.append(LoadError("gateway_settlements", i, row, str(e)))

    b_df = _read_csv(os.path.join(data_dir, "bank_statement.csv"))
    for i, row in enumerate(b_df.to_dict(orient="records")):
        try:
            out.bank.append(BankCredit.from_row(row))
        except Exception as e:
            out.errors.append(LoadError("bank_statement", i, row, str(e)))

    o_df = _read_csv(os.path.join(data_dir, "order_ledger.csv"))
    for i, row in enumerate(o_df.to_dict(orient="records")):
        try:
            out.orders.append(Order.from_row(row))
        except Exception as e:
            out.errors.append(LoadError("order_ledger", i, row, str(e)))

    return out


def load_ground_truth(data_dir: str) -> dict[str, GroundTruthRow]:
    path = os.path.join(data_dir, "ground_truth.csv")
    if not os.path.exists(path):
        return {}
    df = _read_csv(path)
    truth: dict[str, GroundTruthRow] = {}
    for row in df.to_dict(orient="records"):
        txn = str(row.get("txn_id", "")).strip()
        if not txn:
            continue
        truth[txn] = GroundTruthRow(
            txn_id=txn,
            true_status=str(row.get("true_status", "")).strip(),
            is_true_match=str(row.get("is_true_match", "")).strip().lower() in ("true", "1", "yes"),
            dirty=str(row.get("dirty", "")).strip().lower() in ("true", "1", "yes"),
            kind=str(row.get("kind", "")).strip(),
            note=str(row.get("note", "")).strip(),
        )
    return truth
