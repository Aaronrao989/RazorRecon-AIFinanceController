"""Central configuration for the reconciliation engine.

Every money-relevant threshold lives here so the behaviour of the engine is
inspectable and auditable in one place. Nothing in this module imports a web
framework — the core engine is a plain, importable library.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


# --------------------------------------------------------------------------- #
# LLM configuration (Groq, OpenAI-compatible endpoint)
# --------------------------------------------------------------------------- #
# Default model per the brief; the fallback is documented and swappable.
GROQ_MODEL: str = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
GROQ_MODEL_FALLBACK: str = os.getenv("GROQ_MODEL_FALLBACK", "openai/gpt-oss-20b")
GROQ_BASE_URL: str = os.getenv(
    "GROQ_BASE_URL", "https://api.groq.com/openai/v1"
)
# NEVER hardcode the key — read it from the environment at call time.
GROQ_API_KEY_ENV: str = "GROQ_API_KEY"

# Deterministic, auditable output.
LLM_TEMPERATURE: float = 0.1
LLM_TIMEOUT_SECONDS: float = 20.0
LLM_MAX_RETRIES: int = 3          # retries on 429 / transient errors
LLM_BACKOFF_BASE_SECONDS: float = 2.0

# Politeness spacing so we never trip the Groq free-tier 30 RPM limit even if a
# batch happens to produce many ambiguous records back-to-back.
LLM_MIN_INTERVAL_SECONDS: float = 2.1  # ~28 requests/minute ceiling


# --------------------------------------------------------------------------- #
# Guardrails on the LLM's authority (bounded + gated)
# --------------------------------------------------------------------------- #
# The LLM may only *confirm* a match it is confident about and whose money delta
# is small. Anything outside these bounds is escalated to the exception list,
# regardless of what the model says.
CONFIDENCE_THRESHOLD: float = 0.75     # below this -> escalate to exception
AMOUNT_DELTA_CAP: float = 5.00         # ₹ delta above this -> escalate, no matter what


# --------------------------------------------------------------------------- #
# Deterministic matching tolerances
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Tolerances:
    # Hard tolerance for a clean automatic match.
    amount_abs: float = 1.00          # ±₹1
    amount_pct: float = 0.005         # ±0.5%
    # Settlement lands in the bank within T+0..T+2 for a clean match.
    date_window_days: int = 2

    # "Gray zone": beyond the hard tolerance but plausibly the same payment
    # (fee rounding, small skew). These records are handed to the LLM.
    amount_abs_gray: float = 6.00     # up to ₹6 delta is ambiguous
    amount_pct_gray: float = 0.02     # up to 2% delta is ambiguous
    date_window_days_gray: int = 4    # T+3..T+4 is ambiguous

    def within_hard_amount(self, expected: float, actual: float) -> bool:
        delta = abs(expected - actual)
        return delta <= self.amount_abs or delta <= abs(expected) * self.amount_pct

    def within_gray_amount(self, expected: float, actual: float) -> bool:
        delta = abs(expected - actual)
        return delta <= self.amount_abs_gray or delta <= abs(expected) * self.amount_pct_gray


TOLERANCES = Tolerances()

# Currency we can reconcile deterministically. Anything else is an exception.
BASE_CURRENCY: str = "INR"


# --------------------------------------------------------------------------- #
# Investigative agent (bounded, tool-calling ReAct loop)
# --------------------------------------------------------------------------- #
# The agent is invoked ONLY on the records the deterministic layer flags as
# ambiguous — the same subset the single-shot resolver would see. It reasons and
# calls READ-ONLY tools in a loop, then proposes a verdict that the SAME
# guardrails (CONFIDENCE_THRESHOLD / AMOUNT_DELTA_CAP) gate before it counts.
AGENT_MAX_STEPS: int = 6              # hard cap on reasoning/tool steps per record
AGENT_TEMPERATURE: float = 0.1       # low, for deterministic auditable tool use
MAX_TOOL_RESULTS: int = 5            # cap rows any tool returns (token discipline)

# Shared rate cap across ALL agent Groq calls in a batch. The agent makes one
# call PER reasoning step (not per record), so a tight loop can burst; this keeps
# us safely under Groq's 30 RPM free-tier limit.
RPM_CAP: int = 28
# Minimum interval between any two agent Groq calls, derived from the RPM cap.
AGENT_MIN_INTERVAL_SECONDS: float = 60.0 / RPM_CAP
