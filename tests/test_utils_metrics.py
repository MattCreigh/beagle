"""SP-5: tests for utils/metrics (was zero-coverage).

beagle-spotless-phase2, work package SP-5. The metrics registry had no direct
tests. These exercise counter/gauge recording, key construction with labels,
and the Prometheus/JSON export formats.
"""

from __future__ import annotations

import json

from beagle.utils.metrics import MetricPoint, MetricsRegistry


def _new_registry() -> MetricsRegistry:
    reg = MetricsRegistry()
    reg.reset()
    return reg


def test_counter_increments() -> None:
    """inc_counter accumulates from 0."""
    reg = _new_registry()
    reg.inc_counter("requests")
    reg.inc_counter("requests")
    reg.inc_counter("requests", value=2.0)
    points = {p.name: p.value for p in reg.get_all_metrics()}
    assert points["requests"] == 4.0


def test_gauge_sets_value() -> None:
    """set_gauge stores the exact value (last write wins)."""
    reg = _new_registry()
    reg.set_gauge("workers", 3)
    reg.set_gauge("workers", 5)
    points = {p.name: p.value for p in reg.get_all_metrics()}
    assert points["workers"] == 5.0


def test_labels_in_key() -> None:
    """Labels are encoded into the metric key (sorted for determinism)."""
    reg = _new_registry()
    reg.inc_counter("requests", labels={"model": "glm", "region": "us"})
    reg.inc_counter("requests", labels={"region": "us", "model": "glm"})
    points = reg.get_all_metrics()
    # Both calls share one label-set → one key, value 2.
    assert len(points) == 1
    assert points[0].value == 2.0
    assert points[0].metric_type == "counter"


def test_metric_type_tagged() -> None:
    """Counters and gauges carry their metric_type."""
    reg = _new_registry()
    reg.inc_counter("c")
    reg.set_gauge("g", 1)
    types = {p.name: p.metric_type for p in reg.get_all_metrics()}
    assert types["c"] == "counter"
    assert types["g"] == "gauge"


def test_to_prometheus_text() -> None:
    """Prometheus text format includes TYPE lines and values."""
    reg = _new_registry()
    reg.inc_counter("http_requests", value=3)
    reg.set_gauge("up", 1)
    text = reg.to_prometheus_text()
    assert "# TYPE http_requests counter" in text
    assert "http_requests 3.0" in text
    assert "# TYPE up gauge" in text


def test_to_json_round_trip() -> None:
    """JSON export is a valid list of metric dicts."""
    reg = _new_registry()
    reg.inc_counter("x")
    payload = json.loads(reg.to_json())
    assert isinstance(payload, list)
    assert payload[0]["name"] == "x"
    assert payload[0]["type"] == "counter"


def test_reset_clears() -> None:
    """reset() empties all metric stores."""
    reg = _new_registry()
    reg.inc_counter("x")
    reg.set_gauge("g", 1)
    reg.reset()
    assert reg.get_all_metrics() == []


def test_metric_point_defaults() -> None:
    """MetricPoint defaults label dict and metric_type."""
    mp = MetricPoint(name="m", value=1.0, timestamp=0)
    assert mp.labels == {}
    assert mp.metric_type == "gauge"


def test_singleton_is_shared() -> None:
    """get_metrics_registry returns the same instance."""
    from beagle.utils.metrics import get_metrics_registry

    assert get_metrics_registry() is get_metrics_registry()
