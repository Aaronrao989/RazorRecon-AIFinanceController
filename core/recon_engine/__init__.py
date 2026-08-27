"""recon_engine — standalone multi-source financial reconciliation engine.

Deterministic rules first; a bounded, guardrailed LLM only for genuinely
ambiguous exceptions. No web-framework imports — usable as a plain library.

Typical use::

    from recon_engine import reconcile, generate
    generate("data")                 # synthetic datasets + hidden ground truth
    result = reconcile("data")       # run the batch
    print(result.metrics["match_rate"])
"""

from .config import (
    AMOUNT_DELTA_CAP,
    CONFIDENCE_THRESHOLD,
    GROQ_MODEL,
    GROQ_MODEL_FALLBACK,
    TOLERANCES,
)
from .datagen import generate
from .llm_provider import (
    GroqLLMProvider,
    LLMError,
    LLMProvider,
    LLMResult,
    SimulatedLLMProvider,
)
from .matcher import AmbiguousCase, DeterministicMatcher
from .metrics import GroundTruthRow, compute_metrics
from .models import (
    BankCredit,
    Decision,
    DecisionSource,
    Order,
    Settlement,
    Status,
)
from .pipeline import ReconResult, reconcile
from .resolver import resolve_case
from .agent import (
    AgentResult,
    InvestigatorAgent,
    RateLimiter,
    SimulatedAgentModel,
    ToolContext,
    investigate,
)

__all__ = [
    "generate",
    "reconcile",
    "ReconResult",
    "resolve_case",
    "DeterministicMatcher",
    "AmbiguousCase",
    "compute_metrics",
    "GroundTruthRow",
    "Status",
    "Decision",
    "DecisionSource",
    "Settlement",
    "BankCredit",
    "Order",
    "LLMProvider",
    "LLMResult",
    "LLMError",
    "GroqLLMProvider",
    "SimulatedLLMProvider",
    "TOLERANCES",
    "CONFIDENCE_THRESHOLD",
    "AMOUNT_DELTA_CAP",
    "GROQ_MODEL",
    "GROQ_MODEL_FALLBACK",
    # investigative agent
    "InvestigatorAgent",
    "SimulatedAgentModel",
    "ToolContext",
    "RateLimiter",
    "AgentResult",
    "investigate",
]

__version__ = "0.1.0"
