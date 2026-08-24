"""Pre-flight cost and runtime estimator for Beagle workflows.

Forecasts budget usage and execution time before starting a workflow.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..config.model_resolver import resolve_model, resolve_provider
from ..cost_tracker import MODEL_CONTEXT_WINDOWS, MODEL_PRICING

# Fallback heuristics
DEFAULT_INPUT_TOKENS = 2000
DEFAULT_OUTPUT_TOKENS = 4000

# Speed profiles (tokens per second) - approximate for Ollama Cloud
# Jul-2026 model refresh (v13.22.3): allowlist now uses *-cloud suffixed names;
# legacy unsuffixed entries retained for backward compat with pricing cache.
SPEED_PROFILES = {
    "gemma3:27b": 80.0,
    "gemma4:31b-cloud": 80.0,
    "gemma4:31b": 80.0,
    "minimax-m2.7:cloud": 50.0,
    "minimax-m3:cloud": 50.0,
    "minimax-m3": 50.0,
    "glm-5.1:cloud": 40.0,
    "glm-5.2:cloud": 40.0,
    "glm-5.2": 40.0,
    "kimi-k2.5": 30.0,
    "kimi-k2.6:cloud": 30.0,
    "kimi-k2.6": 30.0,
    "kimi-k2.7-code:cloud": 30.0,
    "kimi-k2.7-code": 30.0,
    "kimi-k3": 30.0,
    "kimi-k2-thinking": 25.0,
    "qwen3.5:397b": 20.0,
    "qwen3.5:397b-cloud": 20.0,
    "deepseek-v4-pro:cloud": 35.0,
    "deepseek-v4-pro": 35.0,
    "deepseek-v4-flash:cloud": 60.0,
    "deepseek-v4-flash": 60.0,
    "deepseek/deepseek-v4-flash-0731": 60.0,
    "nemotron-3-ultra:cloud": 25.0,
    "nemotron-3-ultra": 25.0,
    "glm-4.7": 45.0,
    "default": 30.0,
}


@dataclass(frozen=True)
class NodeEstimate:
    """Estimated metrics for a single DAG node."""

    node_name: str
    skill_name: str
    model: str
    provider: str
    estimated_input_tokens: int
    estimated_output_tokens: int
    estimated_cost_usd: float
    estimated_runtime_seconds: float
    context_window: int
    estimated_utilisation_percent: float


@dataclass(frozen=True)
class PreFlightEstimate:
    """Complete pre-flight forecast for a workflow."""

    workflow_name: str
    node_count: int
    nodes: list[NodeEstimate]
    total_estimated_cost_usd: float
    total_estimated_tokens: int
    total_estimated_runtime_seconds: float
    budget_usd: float
    budget_sufficient: bool
    warnings: list[str] = field(default_factory=list)


class PreFlightEstimator:
    """Calculates cost and time forecasts for a given workflow."""

    def __init__(self, budget_usd: float = 10.0):
        self.budget_usd = budget_usd

    def estimate(self, workflow_name: str, dag_nodes: list[Any]) -> PreFlightEstimate:
        """Produce an estimate for the given list of DAG nodes.

        Args:
            workflow_name: Name of the workflow
            dag_nodes: List of node objects (from graph or workflow_loader)

        Returns:
            PreFlightEstimate summary

        """
        node_estimates = []
        total_cost = 0.0
        total_tokens = 0
        total_runtime = 0.0
        warnings = []

        for node in dag_nodes:
            # Resolve model and provider
            # Check if it's a DAGNode or a dict from loader
            if hasattr(node, "skill_name"):
                skill = node.skill_name
                name = node.name
                model_hint = getattr(node, "model_override", None)
            else:
                # Fallback for dict-based specs
                skill = node.get("skill_name", node.get("agent", "unknown"))
                name = node.get("name", "unknown")
                model_hint = node.get("model")

            model = resolve_model(phase_model=model_hint, recipe_name=skill)
            provider = resolve_provider()

            # Get pricing
            pricing = MODEL_PRICING.get(model)
            if not pricing:
                warnings.append(f"Model '{model}' has no pricing data — using default")
                pricing = MODEL_PRICING["default"]

            # Heuristics (Phase 5 will add historical averages)
            input_tokens = DEFAULT_INPUT_TOKENS
            output_tokens = DEFAULT_OUTPUT_TOKENS

            # Calculate cost
            node_cost = (input_tokens / 1_000_000 * pricing["input"]) + (
                output_tokens / 1_000_000 * pricing["output"]
            )

            # Calculate runtime
            speed = SPEED_PROFILES.get(model, SPEED_PROFILES["default"])
            node_runtime = (input_tokens + output_tokens) / speed

            # Context window
            window = MODEL_CONTEXT_WINDOWS.get(model, MODEL_CONTEXT_WINDOWS["default"])
            utilisation = ((input_tokens + output_tokens) / window) * 100

            est = NodeEstimate(
                node_name=name,
                skill_name=skill,
                model=model,
                provider=provider,
                estimated_input_tokens=input_tokens,
                estimated_output_tokens=output_tokens,
                estimated_cost_usd=node_cost,
                estimated_runtime_seconds=node_runtime,
                context_window=window,
                estimated_utilisation_percent=utilisation,
            )

            node_estimates.append(est)
            total_cost += node_cost
            total_tokens += input_tokens + output_tokens
            total_runtime += node_runtime

        return PreFlightEstimate(
            workflow_name=workflow_name,
            node_count=len(node_estimates),
            nodes=node_estimates,
            total_estimated_cost_usd=total_cost,
            total_estimated_tokens=total_tokens,
            total_estimated_runtime_seconds=total_runtime,
            budget_usd=self.budget_usd,
            budget_sufficient=total_cost <= self.budget_usd,
            warnings=list(set(warnings)),
        )
