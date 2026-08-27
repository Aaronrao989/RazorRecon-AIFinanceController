"""Read-only investigation tools for the agent.

Every tool is a thin wrapper over the already-loaded engine data. NONE of them
mutate anything, move money, or author amounts — they only look things up and
return compact structured data. Results are capped at MAX_TOOL_RESULTS and carry
only the fields the agent needs, to stay within Groq's 8K TPM budget.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional

from .. import config
from ..models import BankCredit, Order, Settlement


def _pdate(s: str) -> Optional[date]:
    try:
        return date.fromisoformat(s.strip())
    except Exception:
        return None


@dataclass
class ToolContext:
    """Holds the loaded data + light indexes the tools read from."""

    settlements: dict[str, Settlement]           # by txn_id
    orders: dict[str, Order]                      # by order_id
    bank: list[BankCredit]
    txn_ids: set[str] = field(default_factory=set)
    utr_counts: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_data(cls, settlements, bank, orders) -> "ToolContext":
        s_by_id = {s.txn_id: s for s in settlements}
        o_by_id = {o.order_id: o for o in orders}
        utr_counts: dict[str, int] = {}
        for b in bank:
            utr_counts[b.utr] = utr_counts.get(b.utr, 0) + 1
        return cls(
            settlements=s_by_id, orders=o_by_id, bank=list(bank),
            txn_ids=set(s_by_id.keys()), utr_counts=utr_counts,
        )

    def referenced_txn(self, narration: str) -> Optional[str]:
        """Which settlement txn_id (if any) this bank narration references."""
        for txn in self.txn_ids:
            if txn in narration:
                return txn
        return None


def _credit_view(ctx: ToolContext, b: BankCredit, net: Optional[float] = None) -> dict[str, Any]:
    v = {
        "utr": b.utr,
        "credit_amount": round(b.credit_amount, 2),
        "value_date": b.value_date,
        "references_txn": ctx.referenced_txn(b.narration),
        "utr_is_duplicate": ctx.utr_counts.get(b.utr, 0) > 1,
    }
    if net is not None:
        v["delta_vs_net"] = round(abs(b.credit_amount - net), 2)
    return v


# --------------------------------------------------------------------------- #
# Tool implementations
# --------------------------------------------------------------------------- #
def fetch_settlement(ctx: ToolContext, txn_id: str) -> dict[str, Any]:
    s = ctx.settlements.get(str(txn_id).strip())
    if not s:
        return {"error": f"no settlement with txn_id {txn_id}"}
    return {
        "txn_id": s.txn_id, "order_id": s.order_id, "gross": s.gross,
        "fee": s.fee, "tax": s.tax, "net": s.net, "settled_date": s.settled_date,
    }


def fetch_order(ctx: ToolContext, order_id: str) -> dict[str, Any]:
    o = ctx.orders.get(str(order_id).strip())
    if not o:
        return {"error": f"order_id {order_id} not present in the order ledger"}
    return {
        "order_id": o.order_id, "invoice_amount": o.invoice_amount,
        "currency": o.currency, "status": o.status, "date": o.date,
    }


def search_bank_by_amount(ctx: ToolContext, net: float, tolerance: float = 5.0) -> dict[str, Any]:
    net = float(net)
    tol = float(tolerance)
    hits = [b for b in ctx.bank if abs(b.credit_amount - net) <= tol]
    hits.sort(key=lambda b: abs(b.credit_amount - net))
    hits = hits[: config.MAX_TOOL_RESULTS]
    return {
        "query": {"net": round(net, 2), "tolerance": round(tol, 2)},
        "count": len(hits),
        "candidates": [_credit_view(ctx, b, net) for b in hits],
    }


def widen_date_window(ctx: ToolContext, txn_id: str, days: int) -> dict[str, Any]:
    s = ctx.settlements.get(str(txn_id).strip())
    if not s:
        return {"error": f"no settlement with txn_id {txn_id}"}
    d0 = _pdate(s.settled_date)
    if d0 is None:
        return {"error": "settlement has an unparseable settled_date"}
    days = int(days)
    hits = []
    for b in ctx.bank:
        bd = _pdate(b.value_date)
        if bd is None:
            continue
        delta = (bd - d0).days
        if 0 <= delta <= days:
            hits.append((delta, b))
    # Prefer credits closest in amount to net, then soonest.
    hits.sort(key=lambda t: (abs(t[1].credit_amount - s.net), t[0]))
    hits = hits[: config.MAX_TOOL_RESULTS]
    return {
        "query": {"txn_id": s.txn_id, "settled_date": s.settled_date, "days": days, "net": s.net},
        "count": len(hits),
        "candidates": [{**_credit_view(ctx, b, s.net), "days_after_settlement": delta}
                       for delta, b in hits],
    }


def find_split_or_merged(ctx: ToolContext, order_id: Optional[str] = None,
                         amount: Optional[float] = None) -> dict[str, Any]:
    """Detect 1:N (split) or N:1 (merged) patterns around a target amount."""
    target: Optional[float] = None
    txn: Optional[str] = None
    if order_id:
        # Resolve the settlement for this order to get its net.
        for s in ctx.settlements.values():
            if s.order_id == str(order_id).strip():
                target = s.net
                txn = s.txn_id
                break
    if amount is not None:
        target = float(amount)
    if target is None:
        return {"error": "provide order_id (with a known settlement) or amount"}

    tol = max(config.TOLERANCES.amount_abs_gray, target * config.TOLERANCES.amount_pct_gray)

    # Split: bank credits referencing this txn that together ~ target.
    split_candidates = []
    if txn:
        refs = [b for b in ctx.bank if txn in b.narration]
        if len(refs) >= 2 and abs(sum(b.credit_amount for b in refs) - target) <= tol:
            split_candidates = [_credit_view(ctx, b, None) for b in refs][: config.MAX_TOOL_RESULTS]

    # Merged: a single credit ~ the sum of several settlements' nets is hard to
    # prove read-only cheaply; instead surface any pair of credits summing ~target.
    pair = None
    near = [b for b in ctx.bank if b.credit_amount < target]
    near.sort(key=lambda b: b.credit_amount)
    seen: dict[float, BankCredit] = {}
    for b in near:
        need = round(target - b.credit_amount, 2)
        for amt, other in seen.items():
            if abs(amt - need) <= tol and other.utr != b.utr:
                pair = [_credit_view(ctx, other, None), _credit_view(ctx, b, None)]
                break
        if pair:
            break
        seen[b.credit_amount] = b

    return {
        "target_amount": round(target, 2),
        "split_detected": bool(split_candidates),
        "split_credits": split_candidates,
        "possible_summing_pair": pair,
    }


def list_unmatched_bank_credits(ctx: ToolContext) -> dict[str, Any]:
    """Bank credits whose narration references no known settlement (orphans)."""
    orphans = [b for b in ctx.bank if ctx.referenced_txn(b.narration) is None]
    orphans = orphans[: config.MAX_TOOL_RESULTS]
    return {
        "count": len(orphans),
        "credits": [_credit_view(ctx, b, None) for b in orphans],
    }


# --------------------------------------------------------------------------- #
# Dispatch + OpenAI-style tool schemas (also used by the Groq tool-calling API)
# --------------------------------------------------------------------------- #
_DISPATCH = {
    "fetch_settlement": lambda ctx, a: fetch_settlement(ctx, a["txn_id"]),
    "fetch_order": lambda ctx, a: fetch_order(ctx, a["order_id"]),
    "search_bank_by_amount": lambda ctx, a: search_bank_by_amount(
        ctx, a["net"], a.get("tolerance", 5.0)),
    "widen_date_window": lambda ctx, a: widen_date_window(ctx, a["txn_id"], a.get("days", 4)),
    "find_split_or_merged": lambda ctx, a: find_split_or_merged(
        ctx, a.get("order_id"), a.get("amount")),
    "list_unmatched_bank_credits": lambda ctx, a: list_unmatched_bank_credits(ctx),
}


def execute_tool(name: str, arguments: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Run one read-only tool. Unknown tool / bad args -> structured error (never raises)."""
    fn = _DISPATCH.get(name)
    if fn is None:
        return {"error": f"unknown tool {name!r}"}
    try:
        return fn(ctx, arguments or {})
    except Exception as e:  # never crash the loop on a bad tool call
        return {"error": f"tool {name} failed: {e}"}


def _tool(name: str, desc: str, params: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name, "description": desc,
            "parameters": {"type": "object", "properties": params, "required": required},
        },
    }


TOOL_SCHEMAS: list[dict] = [
    _tool("fetch_settlement", "Get the full gateway settlement row for a txn_id.",
          {"txn_id": {"type": "string"}}, ["txn_id"]),
    _tool("fetch_order", "Get the order-ledger row for an order_id (or an error if absent).",
          {"order_id": {"type": "string"}}, ["order_id"]),
    _tool("search_bank_by_amount",
          "Find bank credits whose amount is within `tolerance` rupees of `net`.",
          {"net": {"type": "number"}, "tolerance": {"type": "number", "description": "₹, default 5"}},
          ["net"]),
    _tool("widen_date_window",
          "Re-search bank credits from the settlement date out to T+`days`, ranked by amount closeness.",
          {"txn_id": {"type": "string"}, "days": {"type": "integer", "description": "default 4"}},
          ["txn_id"]),
    _tool("find_split_or_merged",
          "Detect if an amount was paid across multiple credits (split) or credits sum to it (merged).",
          {"order_id": {"type": "string"}, "amount": {"type": "number"}}, []),
    _tool("list_unmatched_bank_credits",
          "List bank credits that reference no known settlement (orphan credits).",
          {}, []),
]


# The verdict-submission "tool" the agent calls to finish its investigation.
SUBMIT_VERDICT_SCHEMA = _tool(
    "submit_verdict",
    "Conclude the investigation with a final structured verdict.",
    {
        "resolution": {"type": "string",
                       "enum": ["MATCHED", "MISMATCH_AMOUNT", "DATE_SKEW",
                                "SPLIT_SETTLEMENT", "DUPLICATE_UTR", "UNRESOLVED"]},
        "matched_utr": {"type": "string", "description": "UTR of the matched credit, or empty"},
        "reason": {"type": "string"},
        "confidence": {"type": "number"},
    },
    ["resolution", "reason", "confidence"],
)

ALL_SCHEMAS: list[dict] = TOOL_SCHEMAS + [SUBMIT_VERDICT_SCHEMA]
