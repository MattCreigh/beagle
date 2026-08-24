"""Context Window Manager for Goose Agentic Workflow.

Provides context window tracking and management across DAG nodes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ..cost_tracker import (
    CONTEXT_CRITICAL_THRESHOLD,
    CONTEXT_WARNING_THRESHOLD,
    MODEL_CONTEXT_WINDOWS,
    ContextAwareCostTracker,
)

logger = logging.getLogger("BEAGLE_Context")


def _resolve_default_model() -> str:
    """Resolve the default model from the canonical config preset.

    v1.1.1 (S9): the previous ``"gemma3:27b"`` literal drifted from the
    allowlisted fleet. The SSOT is config.toml ``[model_presets]``.
    """
    try:
        from ..config.model_resolver import get_preset

        return get_preset("default")
    except (ImportError, KeyError, ValueError, RuntimeError):  # pragma: no cover
        return "gemma4:31b"


@dataclass
class ContextMetrics:
    """Metrics for a context window."""

    total_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    context_utilization: float = 0.0
    compression_recommended: bool = False
    warning_issued: bool = False
    critical_issued: bool = False


class ContextWindowManager:
    """Manages context window across DAG nodes.

    Provides:
    - Token counting per node
    - Cumulative context tracking
    - Compression recommendations
    - Graceful degradation support
    """

    def __init__(
        self,
        model: str = "",
        context_window: int | None = None,
        warning_threshold: float = CONTEXT_WARNING_THRESHOLD,
        critical_threshold: float = CONTEXT_CRITICAL_THRESHOLD,
    ):
        # v1.1.1 (S9): empty model resolves to the canonical config preset.
        self.model = model or _resolve_default_model()
        self.context_window = context_window or MODEL_CONTEXT_WINDOWS.get(
            model, MODEL_CONTEXT_WINDOWS["default"]
        )
        self.warning_threshold = warning_threshold
        self.critical_threshold = critical_threshold

        self.cost_tracker = ContextAwareCostTracker(
            budget_usd=10.0,
            model=model,
            context_window=self.context_window,
        )

        self.node_metrics: dict[str, ContextMetrics] = {}
        self._current_node: str | None = None

    def start_node(self, node_name: str) -> None:
        """Mark the start of a DAG node execution."""
        self._current_node = node_name
        logger.info(f"[Context] Starting node: {node_name}")

    async def record_node_tokens(
        self,
        node_name: str,
        input_tokens: int,
        output_tokens: int,
    ) -> ContextMetrics:
        """Record tokens for a node and update context."""
        metrics = ContextMetrics(
            total_tokens=input_tokens + output_tokens,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

        # Update cost tracker
        await self.cost_tracker.record_usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model=self.model,
            node_name=node_name,
        )

        # Get updated context status
        status = self.cost_tracker.context_status
        metrics.context_utilization = status.utilization
        metrics.compression_recommended = self.cost_tracker.compress_context_if_needed(
            input_tokens + output_tokens
        )

        # Check thresholds
        if metrics.context_utilization >= self.critical_threshold:
            metrics.critical_issued = True
            logger.critical(
                f"[Context] CRITICAL: {metrics.context_utilization * 100:.1f}% "
                f"utilization during {node_name}"
            )
        elif metrics.context_utilization >= self.warning_threshold:
            metrics.warning_issued = True
            logger.warning(
                f"[Context] WARNING: {metrics.context_utilization * 100:.1f}% "
                f"utilization during {node_name}"
            )

        self.node_metrics[node_name] = metrics
        return metrics

    def can_accept_prompt(self, prompt_tokens: int) -> tuple[bool, str]:
        """Check if a prompt of given size can be accepted.

        Returns:
            (can_accept, reason)

        """
        # Check absolute limit
        current = self.cost_tracker.context_status.current_tokens
        remaining = self.context_window - current

        if prompt_tokens > remaining:
            return (
                False,
                f"Prompt ({prompt_tokens}) exceeds remaining context ({remaining})",
            )

        # Check warning threshold
        projected = current + prompt_tokens
        if projected > self.context_window * self.warning_threshold:
            return False, (
                f"Prompt would push context to {projected / self.context_window * 100:.1f}% "
                f"(above {self.warning_threshold * 100:.0f}% threshold)"
            )

        return True, "OK"

    def get_compression_strategy(self) -> str | None:
        """Get recommended compression strategy based on utilization."""
        utilization = self.cost_tracker.context_status.utilization

        if utilization >= self.critical_threshold:
            return "aggressive"  # Summarize everything
        elif utilization >= self.warning_threshold:
            return "moderate"  # Summarize older nodes
        elif utilization >= 0.5:
            return "light"  # Trim whitespace, obvious redundancy
        else:
            return None  # No compression needed

    def get_summary(self) -> dict[str, Any]:
        """Get context window summary."""
        status = self.cost_tracker.context_status
        return {
            "model": self.model,
            "context_window": self.context_window,
            "current_tokens": status.current_tokens,
            "peak_tokens": status.peak_tokens,
            "utilization_percent": f"{status.utilization_percent:.1f}%",
            "compression_strategy": self.get_compression_strategy(),
            "nodes": {
                name: {
                    "total_tokens": m.total_tokens,
                    "input_tokens": m.input_tokens,
                    "output_tokens": m.output_tokens,
                }
                for name, m in self.node_metrics.items()
            },
        }


# Global context manager
_context_manager: ContextWindowManager | None = None


def get_context_manager(
    model: str = "",
    context_window: int | None = None,
) -> ContextWindowManager:
    """Get or create global context manager."""
    global _context_manager
    if _context_manager is None:
        _context_manager = ContextWindowManager(model=model, context_window=context_window)
    return _context_manager


def reset_context_manager(
    model: str = "",
    context_window: int | None = None,
) -> ContextWindowManager:
    """Reset global context manager."""
    global _context_manager
    _context_manager = ContextWindowManager(model=model, context_window=context_window)
    return _context_manager


if __name__ == "__main__":
    # Test context manager
    import logging

    logging.basicConfig(level=logging.INFO)

    ctx = ContextWindowManager(model="qwen3.5:397b")

    ctx.start_node("PlanningPhase")
    ctx.record_node_tokens("PlanningPhase", 5000, 3000)  # type: ignore[unused-coroutine]

    ctx.start_node("ExecutionPhase")
    ctx.record_node_tokens("ExecutionPhase", 8000, 5000)  # type: ignore[unused-coroutine]

    logger.info("\nSummary:")
    import json

    logger.info(json.dumps(ctx.get_summary(), indent=2))

    logger.info("\nCan accept 50000 token prompt?", ctx.can_accept_prompt(50000))
    logger.info("Can accept 10000 token prompt?", ctx.can_accept_prompt(10000))
