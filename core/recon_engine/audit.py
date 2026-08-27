"""Per-record audit trail.

Every decision — deterministic or LLM-assisted — produces one audit entry
capturing what the engine saw, which rule fired, any LLM reasoning, the
confidence, and the final verdict. The trail is returned by the pipeline and is
downloadable as JSON, so any money decision is fully reconstructible.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from .matcher import AmbiguousCase
from .models import Decision


@dataclass
class AuditEntry:
    txn_id: str
    order_id: str
    timestamp: str
    inputs_seen: dict[str, Any]
    rule_or_layer: str            # "deterministic" | "llm-resolver"
    ambiguity_reason: Optional[str]
    llm_trace: Optional[dict[str, Any]]
    decision: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "txn_id": self.txn_id,
            "order_id": self.order_id,
            "timestamp": self.timestamp,
            "inputs_seen": self.inputs_seen,
            "rule_or_layer": self.rule_or_layer,
            "ambiguity_reason": self.ambiguity_reason,
            "llm_trace": self.llm_trace,
            "decision": self.decision,
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_entry(
    decision: Decision,
    inputs_seen: dict[str, Any],
    *,
    ambiguity_reason: Optional[str] = None,
    llm_trace: Optional[dict[str, Any]] = None,
) -> AuditEntry:
    return AuditEntry(
        txn_id=decision.txn_id,
        order_id=decision.order_id,
        timestamp=_now(),
        inputs_seen=inputs_seen,
        rule_or_layer="llm-resolver" if decision.llm_used else "deterministic",
        ambiguity_reason=ambiguity_reason,
        llm_trace=llm_trace,
        decision=decision.to_dict(),
    )


def dump_audit(entries: list[AuditEntry], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump([e.to_dict() for e in entries], f, indent=2, default=str)
