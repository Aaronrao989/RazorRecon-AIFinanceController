"""Investigative reconciliation agent — a bounded, tool-calling ReAct loop.

Invoked ONLY on records the deterministic layer flags as ambiguous. The agent
reasons and calls READ-ONLY tools to investigate across the three data sources,
then proposes a verdict that the engine's existing guardrails gate before it
counts. The agent reasons; deterministic code retains final authority over every
money decision.
"""

from .ratelimit import RateLimiter
from .tools import ToolContext, TOOL_SCHEMAS, execute_tool
from .model import (
    AgentModel,
    AgentModelResponse,
    GroqToolModel,
    SimulatedAgentModel,
    ToolCall,
)
from .investigator import AgentResult, InvestigatorAgent, investigate

__all__ = [
    "RateLimiter",
    "ToolContext",
    "TOOL_SCHEMAS",
    "execute_tool",
    "AgentModel",
    "AgentModelResponse",
    "ToolCall",
    "GroqToolModel",
    "SimulatedAgentModel",
    "AgentResult",
    "InvestigatorAgent",
    "investigate",
]
