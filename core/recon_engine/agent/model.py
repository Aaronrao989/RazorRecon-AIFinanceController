"""The model behind the ReAct loop.

`AgentModel` is a tiny protocol: given the running message list and the tool
schemas, return the next step — either tool calls or a final message. Two
implementations:

  * ``GroqToolModel``      — real Groq tool-calling (OpenAI-compatible), every
    call routed through the SHARED RateLimiter so a bursty loop stays under
    30 RPM. Retries with backoff on 429/5xx, falls back to the 20b model.
  * ``SimulatedAgentModel`` — a deterministic, clearly-labelled offline
    investigator (name ``simulated``, NOT a real model). It performs a genuine
    two-step investigate-then-conclude cycle so the whole agent flow — loop,
    tools, guardrails, audit — runs and is testable without a key.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

from .. import config
from .ratelimit import RateLimiter


class AgentModelError(Exception):
    """Hard failure (transport, auth, rate-limit exhaustion, unparseable)."""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class AgentModelResponse:
    content: Optional[str] = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    model: str = ""


class AgentModel(Protocol):
    name: str

    def step(self, messages: list[dict], tools: list[dict]) -> AgentModelResponse:  # pragma: no cover
        ...


# --------------------------------------------------------------------------- #
# Real Groq tool-calling model
# --------------------------------------------------------------------------- #
class GroqToolModel:
    name = "groq"

    def __init__(self, limiter: RateLimiter, model: Optional[str] = None,
                 api_key: Optional[str] = None, base_url: Optional[str] = None):
        import os
        self.model = model or config.GROQ_MODEL
        self.fallback_model = config.GROQ_MODEL_FALLBACK
        self.base_url = (base_url or config.GROQ_BASE_URL).rstrip("/")
        self.api_key = api_key or os.getenv(config.GROQ_API_KEY_ENV)
        if not self.api_key:
            raise AgentModelError(f"{config.GROQ_API_KEY_ENV} is not set")
        self.limiter = limiter

    def step(self, messages: list[dict], tools: list[dict]) -> AgentModelResponse:
        try:
            import requests
        except Exception as e:  # pragma: no cover
            raise AgentModelError(f"requests is required: {e}")

        last_err: Optional[Exception] = None
        for model in (self.model, self.fallback_model):
            for attempt in range(config.LLM_MAX_RETRIES):
                self.limiter.acquire()  # SHARED rate cap — every call goes through here
                try:
                    resp = requests.post(
                        f"{self.base_url}/chat/completions",
                        headers={"Authorization": f"Bearer {self.api_key}",
                                 "Content-Type": "application/json"},
                        json={
                            "model": model,
                            "temperature": config.AGENT_TEMPERATURE,
                            "messages": messages,
                            "tools": tools,
                            "tool_choice": "auto",
                        },
                        timeout=config.LLM_TIMEOUT_SECONDS,
                    )
                    if resp.status_code == 429 or resp.status_code >= 500:
                        last_err = AgentModelError(f"HTTP {resp.status_code}")
                        time.sleep(config.LLM_BACKOFF_BASE_SECONDS * (2 ** attempt))
                        continue
                    if resp.status_code != 200:
                        raise AgentModelError(f"Groq {resp.status_code}: {resp.text[:300]}")

                    msg = resp.json()["choices"][0]["message"]
                    calls = []
                    for tc in (msg.get("tool_calls") or []):
                        fn = tc.get("function", {})
                        try:
                            args = json.loads(fn.get("arguments") or "{}")
                        except Exception:
                            args = {}
                        calls.append(ToolCall(id=tc.get("id", ""), name=fn.get("name", ""), arguments=args))
                    return AgentModelResponse(
                        content=msg.get("content"), tool_calls=calls,
                        finish_reason=resp.json()["choices"][0].get("finish_reason", "stop"),
                        model=model,
                    )
                except AgentModelError:
                    raise
                except Exception as e:
                    last_err = e
                    time.sleep(config.LLM_BACKOFF_BASE_SECONDS * (2 ** attempt))
        raise AgentModelError(f"Groq tool call failed after retries: {last_err}")


# --------------------------------------------------------------------------- #
# Offline deterministic investigator (NOT a language model)
# --------------------------------------------------------------------------- #
_RECORD_RE = re.compile(r"txn_id=(?P<txn>\S+)\s+net=(?P<net>[\d.]+)\s+order_id=(?P<order>\S+)")


class SimulatedAgentModel:
    """A deterministic stand-in that runs a genuine multi-step investigation:

      1. widen the date window to look for the settlement's credit;
      2. if a credit tagged to this txn cleanly matches on amount, conclude
         MATCHED — otherwise dig deeper with `find_split_or_merged`;
      3. if a split pair is recovered, conclude SPLIT_SETTLEMENT.

    Because it uses tools the single-shot path does not (it only ever sees the
    ONE pre-selected candidate), it can resolve cases the single-shot arm gets
    wrong — which is exactly the A/B lift. Clearly labelled (name='simulated');
    it is not a language model.
    """

    name = "simulated"

    def step(self, messages: list[dict], tools: list[dict]) -> AgentModelResponse:
        model = "simulated-agent"
        user_msg = next((m for m in messages if m.get("role") == "user"), {"content": ""})
        m = _RECORD_RE.search(user_msg.get("content", "") or "")
        txn = m.group("txn") if m else ""
        order = m.group("order") if m else ""

        ran = [msg.get("name") for msg in messages if msg.get("role") == "tool"]

        # Step 1: widen the date window.
        if "widen_date_window" not in ran:
            return AgentModelResponse(
                tool_calls=[ToolCall(id="c1", name="widen_date_window",
                                     arguments={"txn_id": txn, "days": 4})],
                finish_reason="tool_calls", model=model,
            )

        widen = self._last_obs(messages, "widen_date_window")
        cands = widen.get("candidates", []) if widen else []
        # A credit tagged to THIS settlement that cleanly matches on amount?
        clean = next(
            (c for c in cands
             if c.get("references_txn") == txn
             and float(c.get("delta_vs_net", 99)) <= config.AMOUNT_DELTA_CAP
             and int(c.get("days_after_settlement", 99)) <= config.TOLERANCES.date_window_days_gray),
            None,
        )
        if clean is not None:
            d = float(clean.get("delta_vs_net", 0.0))
            days = int(clean.get("days_after_settlement", 0))
            return self._verdict(
                "MATCHED", clean.get("utr", ""),
                f"credit ₹ off by {d:.2f} at T+{days} is the same payment "
                f"(fee rounding + normal settlement lag)", 0.9, model)

        # Step 2: no clean single match — check for a split/merge pattern.
        if "find_split_or_merged" not in ran:
            return AgentModelResponse(
                tool_calls=[ToolCall(id="c2", name="find_split_or_merged",
                                     arguments={"order_id": order})],
                finish_reason="tool_calls", model=model,
            )

        split = self._last_obs(messages, "find_split_or_merged") or {}
        pair = split.get("split_credits") or split.get("possible_summing_pair")
        if split.get("split_detected") or pair:
            utr = (pair[0].get("utr") if pair else "")
            return self._verdict(
                "SPLIT_SETTLEMENT", utr,
                "net was paid across multiple bank credits (recovered the summing set)",
                0.88, model)

        # Nothing conclusive.
        return self._verdict("UNRESOLVED", "",
                             "could not locate a matching credit or split pattern", 0.6, model)

    @staticmethod
    def _last_obs(messages: list[dict], name: str) -> Optional[dict]:
        for msg in reversed(messages):
            if msg.get("role") == "tool" and msg.get("name") == name:
                try:
                    return json.loads(msg.get("content", "{}"))
                except Exception:
                    return {}
        return None

    def _verdict(self, resolution, utr, reason, conf, model) -> AgentModelResponse:
        return AgentModelResponse(
            tool_calls=[ToolCall(id="call_v", name="submit_verdict",
                                 arguments={"resolution": resolution, "matched_utr": utr,
                                            "reason": reason, "confidence": conf})],
            finish_reason="tool_calls", model=model,
        )
