"""Configuration for the Beagle Query Engine.

Centralizes execution limits, token budgets, and behavioral parameters
for workflow execution.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class QueryEngineConfig:
    """Configuration for query execution limits and behavior.

    Inspired by claw-code QueryEngineConfig.
    """

    # Execution limits
    max_turns: int = 15
    max_budget_usd: float = 10.0
    max_total_tokens: int = 500000

    # Context management
    compact_after_tokens: int = 50000
    context_window_warning_threshold: float = 0.80

    # Behavior
    enable_grpo: bool = False
    enable_ensemble: bool = False
    structured_output: bool = True

    # Security
    require_trust_gate: bool = True

    @classmethod
    def from_dict(cls, data: dict) -> QueryEngineConfig:
        """Create config from a dictionary, ignoring unknown keys."""
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


# Default configuration
DEFAULT_QUERY_CONFIG = QueryEngineConfig()

# Research configuration (higher limits)
RESEARCH_QUERY_CONFIG = QueryEngineConfig(
    max_turns=25,
    max_budget_usd=25.0,
    max_total_tokens=1000000,
    compact_after_tokens=100000,
)

# Trivial configuration (lower limits)
TRIVIAL_QUERY_CONFIG = QueryEngineConfig(max_turns=3, max_budget_usd=1.0, structured_output=False)
