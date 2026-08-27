"""LLMProvider interface + Groq implementation + an offline simulator.

The provider takes one structured ``AmbiguousCase`` and returns a bounded,
structured verdict: ``{resolution, reason, confidence}``. It is deliberately
narrow — the model may only *judge whether an existing candidate is the same
payment*. It is told, and structurally prevented from being trusted to, invent
or alter any amount (the resolver re-derives every number from the source data;
the model's numeric claims are never used).

Two implementations:
  * ``GroqLLMProvider``      — real calls to Groq's OpenAI-compatible endpoint.
  * ``SimulatedLLMProvider`` — a deterministic, clearly-labelled heuristic used
    for offline demos and unit tests. It is NOT a language model and never
    pretends to be; its ``model`` field says ``simulated-heuristic`` so the
    audit trail can't be mistaken for a real LLM run.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Optional, Protocol

from . import config
from .matcher import AmbiguousCase


# Resolutions the model / agent is allowed to return.
ALLOWED_RESOLUTIONS = {
    "MATCHED", "MISMATCH_AMOUNT", "DATE_SKEW", "UNRESOLVED",
    "SPLIT_SETTLEMENT", "DUPLICATE_UTR",
}


class LLMError(Exception):
    """Raised for transport, auth, rate-limit-exhaustion or parse failures.

    The resolver catches this and escalates the record to the exception list —
    a failed LLM call never crashes the batch.
    """


@dataclass
class LLMResult:
    resolution: str
    reason: str
    confidence: float
    model: str
    raw: Optional[str] = None
    # Optional agent metadata (set by the investigative agent; None for single-shot).
    evidence: Optional[list] = None
    agent_steps: Optional[int] = None
    agent_tool_calls: Optional[int] = None
    agent_matched_utr: Optional[str] = None

    def normalized(self) -> "LLMResult":
        res = (self.resolution or "").strip().upper()
        if res not in ALLOWED_RESOLUTIONS:
            res = "UNRESOLVED"
        conf = self.confidence
        try:
            conf = float(conf)
        except Exception:
            conf = 0.0
        conf = max(0.0, min(1.0, conf))
        return LLMResult(
            res, self.reason or "", conf, self.model, self.raw,
            evidence=self.evidence, agent_steps=self.agent_steps,
            agent_tool_calls=self.agent_tool_calls, agent_matched_utr=self.agent_matched_utr,
        )


class LLMProvider(Protocol):
    """One method: adjudicate a single ambiguous record."""

    name: str

    def resolve(self, case: AmbiguousCase) -> LLMResult:  # pragma: no cover - protocol
        ...


_SYSTEM_PROMPT = (
    "You are a meticulous finance reconciliation assistant for an Indian payment "
    "gateway (amounts in INR). You are given ONE gateway settlement, its "
    "order-ledger entry, and ONE candidate bank credit that a deterministic rule "
    "engine already flagged as JUST OUTSIDE its strict auto-match thresholds "
    "(amount ±₹1 or ±0.5%, date T+0..T+2). Your job is to judge whether, despite "
    "being just outside those strict limits, the credit is CLEARLY THE SAME "
    "payment as the settlement.\n\n"
    "CALIBRATION (what is normal in this domain):\n"
    "- Settlement 'net' is gross minus a ~2% gateway fee and 18% GST on that fee, "
    "so tiny differences arise from fee/tax rounding.\n"
    "- An amount gap up to about ₹5 (or ~1-2% of net) is typical fee/rounding or a "
    "minor bank charge -> still the SAME payment -> MATCHED.\n"
    "- Indian bank settlements commonly land T+1 to T+3, sometimes T+4. A credit "
    "up to ~4 days after settlement whose amount otherwise agrees is the SAME "
    "payment -> MATCHED.\n"
    "- Only return MISMATCH_AMOUNT if the money gap is clearly too large to be "
    "fee rounding (well beyond a few rupees / a couple of percent).\n"
    "- Only return DATE_SKEW if the timing is genuinely implausible (well beyond "
    "~4 days) while the amount matches.\n\n"
    "STRICT RULES:\n"
    "- NEVER invent, alter, or recompute any monetary amount. Use only the "
    "numbers given to you.\n"
    "- Respond with ONLY a JSON object, no prose, of the exact form:\n"
    '  {"resolution": "MATCHED|MISMATCH_AMOUNT|DATE_SKEW|UNRESOLVED", '
    '"reason": "<one short sentence>", "confidence": <0.0-1.0>}\n'
    "- confidence reflects how sure you are of the resolution."
)


def _build_user_prompt(case: AmbiguousCase) -> str:
    return (
        "Adjudicate this reconciliation case.\n\n"
        + case.summary()
        + "\n\nReturn the JSON verdict now."
    )


def _extract_json(text: str) -> dict:
    """Pull the first JSON object out of a model response, tolerantly."""
    text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise LLMError(f"no JSON object found in model output: {text!r}")
    blob = text[start : end + 1]
    try:
        return json.loads(blob)
    except json.JSONDecodeError as e:
        raise LLMError(f"could not parse model JSON: {e}: {blob!r}")


class GroqLLMProvider:
    """Real Groq calls over the OpenAI-compatible REST endpoint (via requests)."""

    name = "groq"

    def __init__(
        self,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
    ):
        self.model = model or config.GROQ_MODEL
        self.fallback_model = config.GROQ_MODEL_FALLBACK
        self.base_url = (base_url or config.GROQ_BASE_URL).rstrip("/")
        self.api_key = api_key or os.getenv(config.GROQ_API_KEY_ENV)
        if not self.api_key:
            raise LLMError(
                f"{config.GROQ_API_KEY_ENV} is not set; cannot use GroqLLMProvider"
            )
        self._last_call_ts = 0.0

    def _space_calls(self) -> None:
        """Keep well under the 30 RPM free-tier limit."""
        wait = config.LLM_MIN_INTERVAL_SECONDS - (time.time() - self._last_call_ts)
        if wait > 0:
            time.sleep(wait)

    def resolve(self, case: AmbiguousCase) -> LLMResult:
        try:
            import requests
        except Exception as e:  # pragma: no cover
            raise LLMError(f"requests is required for GroqLLMProvider: {e}")

        payload_messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_prompt(case)},
        ]

        last_err: Optional[Exception] = None
        for model in (self.model, self.fallback_model):
            for attempt in range(config.LLM_MAX_RETRIES):
                self._space_calls()
                try:
                    resp = requests.post(
                        f"{self.base_url}/chat/completions",
                        headers={
                            "Authorization": f"Bearer {self.api_key}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "model": model,
                            "temperature": config.LLM_TEMPERATURE,
                            "messages": payload_messages,
                            "response_format": {"type": "json_object"},
                        },
                        timeout=config.LLM_TIMEOUT_SECONDS,
                    )
                    self._last_call_ts = time.time()

                    if resp.status_code == 429:
                        # Rate limited — back off and retry.
                        backoff = config.LLM_BACKOFF_BASE_SECONDS * (2 ** attempt)
                        last_err = LLMError(f"429 rate limited (attempt {attempt + 1})")
                        time.sleep(backoff)
                        continue
                    if resp.status_code >= 500:
                        backoff = config.LLM_BACKOFF_BASE_SECONDS * (2 ** attempt)
                        last_err = LLMError(f"server error {resp.status_code}")
                        time.sleep(backoff)
                        continue
                    if resp.status_code != 200:
                        raise LLMError(
                            f"Groq returned {resp.status_code}: {resp.text[:300]}"
                        )

                    content = resp.json()["choices"][0]["message"]["content"]
                    data = _extract_json(content)
                    return LLMResult(
                        resolution=str(data.get("resolution", "UNRESOLVED")),
                        reason=str(data.get("reason", "")),
                        confidence=data.get("confidence", 0.0),
                        model=model,
                        raw=content,
                    ).normalized()

                except LLMError:
                    raise
                except Exception as e:  # network/timeout/etc.
                    last_err = e
                    backoff = config.LLM_BACKOFF_BASE_SECONDS * (2 ** attempt)
                    time.sleep(backoff)
            # primary model exhausted its retries -> try the documented fallback
        raise LLMError(f"Groq resolution failed after retries: {last_err}")


class SimulatedLLMProvider:
    """Deterministic offline stand-in. NOT a language model.

    Encodes the same judgement a human/LLM would apply to the gray zone: a small
    fee-rounding delta or a short settlement delay is the same payment; anything
    larger is not. Lets the full pipeline (and its guardrails) be demonstrated
    and unit-tested without network access or an API key.
    """

    name = "simulated"

    def resolve(self, case: AmbiguousCase) -> LLMResult:
        model = "simulated-heuristic"
        delta = case.amount_delta if case.amount_delta is not None else 0.0
        ddays = case.date_delta_days

        # A settlement delay beyond the gray window is not a plausible match.
        if ddays is not None and ddays > config.TOLERANCES.date_window_days_gray:
            return LLMResult("DATE_SKEW", "settlement delay is implausibly long", 0.8, model)

        # A small money delta looks like fee rounding — the same payment.
        if delta <= config.AMOUNT_DELTA_CAP:
            return LLMResult(
                "MATCHED",
                (f"₹{delta:.2f} difference is consistent with fee rounding; "
                 f"timing within a plausible settlement delay"),
                0.9,
                model,
            )

        # Larger money delta — not the same payment.
        return LLMResult(
            "MISMATCH_AMOUNT",
            f"₹{delta:.2f} difference is too large to be fee rounding",
            0.85,
            model,
        )
