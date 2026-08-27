"""Measurement harness — the judging bar.

Compares the engine's decisions against the hidden ground truth and reports:
  * match rate (share auto-resolved as MATCHED)
  * precision & recall of MATCHED vs true matches
  * false positives (called MATCHED but truly NOT a match) — with money at risk
  * per-status accuracy
  * throughput

Nothing here hides failures: the false-positive list and every unresolved
exception are first-class outputs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .models import Decision, Status


@dataclass
class GroundTruthRow:
    txn_id: str
    true_status: str
    is_true_match: bool
    dirty: bool
    kind: str = ""
    note: str = ""


def compute_metrics(
    decisions: list[Decision],
    truth: dict[str, GroundTruthRow],
    settlements_net: Optional[dict[str, float]] = None,
    wall_clock_seconds: Optional[float] = None,
) -> dict[str, Any]:
    """Return a JSON-serialisable metrics summary."""

    settlements_net = settlements_net or {}
    total = len(decisions)

    tp = fp = fn = tn = 0
    false_positives: list[dict[str, Any]] = []
    false_negatives: list[dict[str, Any]] = []
    status_correct = 0
    status_total = 0
    n_matched = 0

    for d in decisions:
        gt = truth.get(d.txn_id)
        predicted_match = d.status is Status.MATCHED
        if predicted_match:
            n_matched += 1

        if gt is None:
            # No ground truth for this record — count it but don't score it.
            continue

        status_total += 1
        if d.status.value == gt.true_status:
            status_correct += 1

        actual_match = gt.is_true_match
        if predicted_match and actual_match:
            tp += 1
        elif predicted_match and not actual_match:
            fp += 1
            false_positives.append(
                {
                    "txn_id": d.txn_id,
                    "order_id": d.order_id,
                    "said": d.status.value,
                    "truth": gt.true_status,
                    "money_at_risk": round(settlements_net.get(d.txn_id, 0.0), 2),
                    "reason": d.reason,
                    "source": d.source.value,
                }
            )
        elif (not predicted_match) and actual_match:
            fn += 1
            false_negatives.append(
                {
                    "txn_id": d.txn_id,
                    "order_id": d.order_id,
                    "said": d.status.value,
                    "truth": gt.true_status,
                    "reason": d.reason,
                    "source": d.source.value,
                }
            )
        else:
            tn += 1

    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0

    fp_cost = round(sum(f["money_at_risk"] for f in false_positives), 2)

    summary: dict[str, Any] = {
        "total_records": total,
        "auto_matched": n_matched,
        "match_rate": round(n_matched / total, 4) if total else 0.0,
        "exceptions": total - n_matched,
        "matched_precision": round(precision, 4),
        "matched_recall": round(recall, 4),
        "matched_f1": round(f1, 4),
        "confusion": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
        "status_accuracy": round(status_correct / status_total, 4) if status_total else None,
        "false_positive_count": len(false_positives),
        "false_positive_cost": fp_cost,
        "false_positives": false_positives,
        "false_negatives": false_negatives,
    }

    if wall_clock_seconds is not None:
        summary["wall_clock_seconds"] = round(wall_clock_seconds, 3)
        summary["throughput_rps"] = (
            round(total / wall_clock_seconds, 2) if wall_clock_seconds > 0 else None
        )

    # Breakdown by final status (for the UI's metrics table).
    by_status: dict[str, int] = {}
    for d in decisions:
        by_status[d.status.value] = by_status.get(d.status.value, 0) + 1
    summary["by_status"] = by_status

    return summary
