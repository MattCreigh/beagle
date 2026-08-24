"""GenAI metrics collection wired to the Beagle EventBus.

Provides 5 standard GenAI metrics per OTel GenAI semantic conventions:
1. **gen_ai.token.usage** — input/output/total tokens per operation
2. **gen_ai.operation.duration** — end-to-end node/workflow duration
3. **gen_ai.request.duration** — LLM request round-trip time
4. **gen_ai.cost** — dollar cost per operation
5. **gen_ai.time_per_output_token** — latency efficiency

Metrics are collected in-process via ``MetricsCollector`` and optionally
exported via OTel Metrics SDK (OTLP/Prometheus).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("Beagle.observability.metrics")

# ── OTel Metrics (optional) ──────────────────────────────────────────────────

try:
    from opentelemetry import metrics as otel_metrics

    OTEL_METRICS_AVAILABLE = True
except ImportError:
    OTEL_METRICS_AVAILABLE = False
    otel_metrics = None  # type: ignore[assignment]


# ── Data types ───────────────────────────────────────────────────────────────


@dataclass
class GenAIMetricEvent:
    """A single recorded metric data point."""

    metric_name: str
    value: float
    attributes: dict[str, str] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


@dataclass
class MetricsSummary:
    """Aggregated metrics for reporting."""

    total_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_cost_usd: float = 0.0
    total_operations: int = 0
    total_duration_s: float = 0.0
    avg_duration_s: float = 0.0
    avg_tokens_per_op: float = 0.0
    p95_duration_s: float = 0.0
    models_used: dict[str, int] = field(default_factory=dict)
    nodes_completed: int = 0
    nodes_failed: int = 0


# ── MetricsCollector ─────────────────────────────────────────────────────────


class MetricsCollector:
    """In-process GenAI metrics collector.

    Subscribes to Beagle EventBus events and records metrics.
    Thread-safe via internal lock.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: list[GenAIMetricEvent] = []
        self._durations: list[float] = []

        # Aggregates
        self._total_tokens = 0
        self._input_tokens = 0
        self._output_tokens = 0
        self._total_cost = 0.0
        self._total_operations = 0
        self._total_duration = 0.0
        self._models_used: dict[str, int] = {}
        self._nodes_completed = 0
        self._nodes_failed = 0

        # OTel instruments (lazy-initialized)
        self._otel_token_counter: Any = None
        self._otel_duration_histogram: Any = None
        self._otel_cost_counter: Any = None
        self._init_otel_instruments()

        # Subscribe to EventBus
        self._subscribe_events()

    def _init_otel_instruments(self) -> None:
        """Create OTel metric instruments if SDK available."""
        if not OTEL_METRICS_AVAILABLE or otel_metrics is None:
            return
        try:
            meter = otel_metrics.get_meter("beagle.genai", "13.8.1")
            self._otel_token_counter = meter.create_counter(
                "gen_ai.token.usage",
                unit="tokens",
                description="Token usage by type (input/output/total)",
            )
            self._otel_duration_histogram = meter.create_histogram(
                "gen_ai.operation.duration",
                unit="s",
                description="Operation duration in seconds",
            )
            self._otel_cost_counter = meter.create_counter(
                "gen_ai.cost",
                unit="USD",
                description="Dollar cost of GenAI operations",
            )
        except Exception:  # ruff: ignore[BLE001]  # broad catch intentional — metrics are optional
            logger.debug("OTel metrics instruments not initialized")

    def _subscribe_events(self) -> None:
        """Wire up to Beagle EventBus for automatic metric collection."""
        try:
            from beagle.events import (
                NodeCompleted,
                NodeFailed,
                WorkflowCompleted,
                get_event_bus,
            )

            bus = get_event_bus()

            bus.subscribe(
                "NodeCompleted",
                lambda e: (
                    self.record_node_completed(
                        node_name=e.node_name,
                        tokens=e.tokens,
                        cost=e.cost,
                        duration_s=e.duration_seconds,
                        model=getattr(e, "model", "unknown"),
                    )
                    if isinstance(e, NodeCompleted)
                    else None
                ),
            )

            bus.subscribe(
                "NodeFailed",
                lambda e: (
                    self.record_node_failed(
                        node_name=e.node_name,
                        error=e.error,
                    )
                    if isinstance(e, NodeFailed)
                    else None
                ),
            )

            bus.subscribe(
                "WorkflowCompleted",
                lambda e: (
                    self.record_workflow_completed(
                        workflow_id=e.workflow_id,
                    )
                    if isinstance(e, WorkflowCompleted)
                    else None
                ),
            )

            logger.debug("Metrics collector subscribed to EventBus")
        except Exception:  # ruff: ignore[BLE001]  # broad catch intentional — bus may not exist
            logger.debug("EventBus not available for metrics subscription")

    # ── Recording methods ────────────────────────────────────────────────

    def record_token_usage(
        self,
        input_tokens: int = 0,
        output_tokens: int = 0,
        model: str = "unknown",
        node_name: str = "",
    ) -> None:
        """Record token usage for a GenAI operation."""
        total = input_tokens + output_tokens
        attrs = {"model": model, "node": node_name}

        with self._lock:
            self._total_tokens += total
            self._input_tokens += input_tokens
            self._output_tokens += output_tokens
            self._events.append(GenAIMetricEvent("gen_ai.token.usage", total, attrs))

        # OTel export
        if self._otel_token_counter is not None:
            self._otel_token_counter.add(input_tokens, {"gen_ai.token.type": "input", **attrs})
            self._otel_token_counter.add(output_tokens, {"gen_ai.token.type": "output", **attrs})

    def record_operation_duration(
        self,
        duration_s: float,
        model: str = "unknown",
        node_name: str = "",
    ) -> None:
        """Record duration of a GenAI operation."""
        attrs = {"model": model, "node": node_name}

        with self._lock:
            self._total_duration += duration_s
            self._total_operations += 1
            self._durations.append(duration_s)
            self._events.append(GenAIMetricEvent("gen_ai.operation.duration", duration_s, attrs))

        if self._otel_duration_histogram is not None:
            self._otel_duration_histogram.record(duration_s, attrs)

    def record_cost(
        self,
        cost_usd: float,
        model: str = "unknown",
        node_name: str = "",
    ) -> None:
        """Record dollar cost of a GenAI operation."""
        attrs = {"model": model, "node": node_name}

        with self._lock:
            self._total_cost += cost_usd
            self._events.append(GenAIMetricEvent("gen_ai.cost", cost_usd, attrs))

        if self._otel_cost_counter is not None:
            self._otel_cost_counter.add(cost_usd, attrs)

    def record_node_completed(
        self,
        node_name: str,
        tokens: int = 0,
        cost: float = 0.0,
        duration_s: float = 0.0,
        model: str = "unknown",
    ) -> None:
        """Convenience: record all metrics for a completed node."""
        self.record_token_usage(input_tokens=tokens, model=model, node_name=node_name)
        self.record_operation_duration(duration_s, model=model, node_name=node_name)
        if cost > 0:
            self.record_cost(cost, model=model, node_name=node_name)

        with self._lock:
            self._nodes_completed += 1
            self._models_used[model] = self._models_used.get(model, 0) + 1

    def record_node_failed(self, node_name: str, error: str = "") -> None:
        """Record a node failure."""
        with self._lock:
            self._nodes_failed += 1
            self._events.append(
                GenAIMetricEvent(
                    "gen_ai.node.failed",
                    1.0,
                    {"node": node_name, "error": error[:200]},
                )
            )

    def record_workflow_completed(self, workflow_id: str = "") -> None:
        """Record workflow completion (summary marker)."""
        with self._lock:
            self._events.append(
                GenAIMetricEvent(
                    "gen_ai.workflow.completed",
                    1.0,
                    {"workflow_id": workflow_id},
                )
            )

    # ── Summary / Export ─────────────────────────────────────────────────

    def get_summary(self) -> MetricsSummary:
        """Get aggregated metrics summary."""
        with self._lock:
            durations = sorted(self._durations) if self._durations else []
            p95_idx = int(len(durations) * 0.95) if durations else 0
            return MetricsSummary(
                total_tokens=self._total_tokens,
                input_tokens=self._input_tokens,
                output_tokens=self._output_tokens,
                total_cost_usd=self._total_cost,
                total_operations=self._total_operations,
                total_duration_s=self._total_duration,
                avg_duration_s=(
                    self._total_duration / self._total_operations
                    if self._total_operations > 0
                    else 0.0
                ),
                avg_tokens_per_op=(
                    self._total_tokens / self._total_operations
                    if self._total_operations > 0
                    else 0.0
                ),
                p95_duration_s=durations[p95_idx] if durations else 0.0,
                models_used=dict(self._models_used),
                nodes_completed=self._nodes_completed,
                nodes_failed=self._nodes_failed,
            )

    def reset(self) -> None:
        """Reset all metrics (for testing)."""
        with self._lock:
            self._events.clear()
            self._durations.clear()
            self._total_tokens = 0
            self._input_tokens = 0
            self._output_tokens = 0
            self._total_cost = 0.0
            self._total_operations = 0
            self._total_duration = 0.0
            self._models_used.clear()
            self._nodes_completed = 0
            self._nodes_failed = 0


# ── Singleton ────────────────────────────────────────────────────────────────

_collector: MetricsCollector | None = None


def get_metrics_collector() -> MetricsCollector:
    """Get the singleton MetricsCollector instance."""
    global _collector
    if _collector is None:
        _collector = MetricsCollector()
    return _collector


def record_genai_metric(
    metric_name: str,
    value: float,
    attributes: dict[str, str] | None = None,
) -> None:
    """Quick helper to record a named metric."""
    collector = get_metrics_collector()
    with collector._lock:
        collector._events.append(GenAIMetricEvent(metric_name, value, attributes or {}))
