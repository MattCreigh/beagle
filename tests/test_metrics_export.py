"""Sections 11.3-11.4: Metrics export + health endpoint tests."""

from __future__ import annotations

import json

from beagle.utils.metrics import (
    MetricsRegistry,
    get_metrics_registry,
)


class TestMetricsRegistry:
    """MetricsRegistry collects counters and gauges."""

    def test_inc_counter(self):
        reg = MetricsRegistry()
        reg.inc_counter("requests_total")
        reg.inc_counter("requests_total")
        metrics = reg.get_all_metrics()
        counters = [m for m in metrics if m.metric_type == "counter"]
        assert len(counters) == 1
        assert counters[0].value == 2.0

    def test_set_gauge(self):
        reg = MetricsRegistry()
        reg.set_gauge("active_workflows", 5.0)
        metrics = reg.get_all_metrics()
        gauges = [m for m in metrics if m.metric_type == "gauge"]
        assert len(gauges) == 1
        assert gauges[0].value == 5.0

    def test_counter_with_labels(self):
        reg = MetricsRegistry()
        reg.inc_counter("http_requests", labels={"method": "GET"})
        reg.inc_counter("http_requests", labels={"method": "POST"})
        metrics = reg.get_all_metrics()
        assert len(metrics) >= 2

    def test_gauge_overwrite(self):
        reg = MetricsRegistry()
        reg.set_gauge("cpu_pct", 50.0)
        reg.set_gauge("cpu_pct", 75.0)
        metrics = reg.get_all_metrics()
        assert len(metrics) == 1
        assert metrics[0].value == 75.0

    def test_to_prometheus_text(self):
        reg = MetricsRegistry()
        reg.inc_counter("requests_total", 10)
        reg.set_gauge("connections", 3)
        text = reg.to_prometheus_text()
        assert "counter" in text
        assert "gauge" in text
        assert "requests_total 10" in text
        assert "connections 3" in text

    def test_to_json(self):
        reg = MetricsRegistry()
        reg.inc_counter("ops", 5)
        data = json.loads(reg.to_json())
        assert len(data) >= 1
        assert data[0]["name"] == "ops"
        assert data[0]["value"] == 5

    def test_reset_clears_all(self):
        reg = MetricsRegistry()
        reg.inc_counter("x")
        reg.set_gauge("y", 1)
        reg.reset()
        assert len(reg.get_all_metrics()) == 0

    def test_singleton(self):
        r1 = get_metrics_registry()
        r2 = get_metrics_registry()
        assert r1 is r2

    def test_empty_prometheus_output(self):
        reg = MetricsRegistry()
        assert reg.to_prometheus_text() == ""
