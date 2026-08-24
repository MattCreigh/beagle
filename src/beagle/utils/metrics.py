"""Beagle metrics export module for operational observability.

Collects and exports runtime metrics in Prometheus-compatible format
for monitoring dashboards and alerting.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field


@dataclass
class MetricPoint:
    """A single metric data point."""

    name: str
    value: float
    timestamp: float
    labels: dict[str, str] = field(default_factory=dict)
    metric_type: str = "gauge"  # gauge, counter, histogram


class MetricsRegistry:
    """Thread-safe metrics registry for Beagle.

    Collects counters, gauges, and histograms that can be exported
    in Prometheus text or JSON format.
    """

    def __init__(self) -> None:
        self._counters: dict[str, float] = {}
        self._gauges: dict[str, float] = {}
        self._timestamps: dict[str, float] = {}
        self._lock = threading.Lock()

    def inc_counter(
        self, name: str, value: float = 1.0, labels: dict[str, str] | None = None
    ) -> None:
        """Increment a counter metric."""
        key = self._make_key(name, labels)
        with self._lock:
            self._counters[key] = self._counters.get(key, 0.0) + value
            self._timestamps[key] = time.time()

    def set_gauge(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        """Set a gauge metric to a specific value."""
        key = self._make_key(name, labels)
        with self._lock:
            self._gauges[key] = value
            self._timestamps[key] = time.time()

    def get_all_metrics(self) -> list[MetricPoint]:
        """Get all current metrics as MetricPoint list."""
        now = time.time()
        points = []
        with self._lock:
            for key, value in self._counters.items():
                points.append(
                    MetricPoint(
                        name=key,
                        value=value,
                        timestamp=self._timestamps.get(key, now),
                        metric_type="counter",
                    )
                )
            for key, value in self._gauges.items():
                points.append(
                    MetricPoint(
                        name=key,
                        value=value,
                        timestamp=self._timestamps.get(key, now),
                        metric_type="gauge",
                    )
                )
        return points

    def to_prometheus_text(self) -> str:
        """Export metrics in Prometheus text exposition format."""
        lines = []
        with self._lock:
            for key, value in self._counters.items():
                lines.append(f"# TYPE {key} counter")
                lines.append(f"{key} {value}")
            for key, value in self._gauges.items():
                lines.append(f"# TYPE {key} gauge")
                lines.append(f"{key} {value}")
        return "\n".join(lines) + "\n" if lines else ""

    def to_json(self) -> str:
        """Export metrics as JSON."""
        return json.dumps(
            [
                {"name": p.name, "value": p.value, "type": p.metric_type, "ts": p.timestamp}
                for p in self.get_all_metrics()
            ],
            indent=2,
        )

    def reset(self) -> None:
        """Reset all metrics (for testing)."""
        with self._lock:
            self._counters.clear()
            self._gauges.clear()
            self._timestamps.clear()

    @staticmethod
    def _make_key(name: str, labels: dict[str, str] | None = None) -> str:
        """Create a metric key with optional labels."""
        if not labels:
            return name
        label_str = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
        return f"{name}{{{label_str}}}"


# Module-level singleton
_registry: MetricsRegistry | None = None
_registry_lock = threading.Lock()


def get_metrics_registry() -> MetricsRegistry:
    """Get the global metrics registry singleton."""
    global _registry
    with _registry_lock:
        if _registry is None:
            _registry = MetricsRegistry()
    return _registry
