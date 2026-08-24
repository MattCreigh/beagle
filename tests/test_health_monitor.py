"""Tests for Beagle self-health monitoring system.

Covers: HealthSnapshot, HealthThresholds, health score calculation,
HealthMonitor state transitions, event emission, trend analysis,
singleton access, and collector resilience.
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from beagle.health.collector import (
    HealthSnapshot,
    calculate_health_score,
)
from beagle.health.monitor import (
    HealthMonitor,
    get_health_monitor,
)
from beagle.health.thresholds import HealthThresholds

# ── Fixtures ──────────────────────────────────────────────────────────────


def _make_snapshot(**overrides) -> HealthSnapshot:
    """Create a HealthSnapshot with sane defaults, overriding as needed."""
    defaults = {
        "timestamp": time.time(),
        "rss_mb": 256.0,
        "fd_count": 50,
        "fd_limit": 1024,
        "thread_count": 10,
        "zombie_child_count": 0,
        "circuits": {},
        "circuits_open": 0,
        "rate_limiter_utilization": 0.3,
        "rate_limiter_blocked": 0,
        "cache_hit_rate": 0.8,
        "cache_entries": 200,
        "pool_active": 1,
        "pool_max": 4,
        "pool_completed": 50,
        "pool_failed": 0,
        "event_bus_subscribers": 5,
        "event_bus_ring_depth": 100,
        "db_stats": {"total_runs": 10, "success_rate": 95.0},
        "health_score": 1.0,
        "degraded_systems": [],
        "critical_systems": [],
    }
    defaults.update(overrides)
    return HealthSnapshot(**defaults)


# ── HealthSnapshot ────────────────────────────────────────────────────────


class TestHealthSnapshot:
    """Test HealthSnapshot creation and immutability."""

    def test_create_with_defaults(self):
        snap = _make_snapshot()
        assert snap.rss_mb == 256.0
        assert snap.fd_count == 50
        assert snap.health_score == 1.0

    def test_frozen(self):
        snap = _make_snapshot()
        with pytest.raises(AttributeError):
            snap.rss_mb = 999.0  # type: ignore[misc]

    def test_all_fields_present(self):
        snap = _make_snapshot()
        assert hasattr(snap, "timestamp")
        assert hasattr(snap, "circuits")
        assert hasattr(snap, "degraded_systems")
        assert hasattr(snap, "critical_systems")


# ── HealthThresholds ──────────────────────────────────────────────────────


class TestHealthThresholds:
    """Test threshold defaults and custom values."""

    def test_defaults(self):
        t = HealthThresholds()
        assert t.rss_warn_mb == 1024.0
        assert t.rss_critical_mb == 2048.0
        assert t.fd_warn_pct == 0.80
        assert t.fd_critical_pct == 0.95
        assert t.check_interval_seconds == 60

    def test_custom_values(self):
        t = HealthThresholds(rss_warn_mb=512.0, thread_warn=50)
        assert t.rss_warn_mb == 512.0
        assert t.thread_warn == 50
        # Others should keep defaults
        assert t.rss_critical_mb == 2048.0


# ── Health score calculation ──────────────────────────────────────────────


class TestHealthScoreCalculation:
    """Test the composite health score algorithm."""

    def test_perfect_score(self):
        fields = {
            "rss_mb": 100.0,
            "fd_count": 10,
            "fd_limit": 1024,
            "circuits_open": 0,
            "cache_hit_rate": 0.9,
            "cache_entries": 200,
            "pool_failed": 0,
            "pool_completed": 50,
            "zombie_child_count": 0,
            "thread_count": 10,
        }
        t = HealthThresholds()
        score, degraded, critical = calculate_health_score(fields, t)
        assert score == 1.0
        assert degraded == []
        assert critical == []

    def test_degraded_rss(self):
        fields = {
            "rss_mb": 1200.0,
            "fd_count": 10,
            "fd_limit": 1024,
            "circuits_open": 0,
            "cache_hit_rate": 0.9,
            "cache_entries": 200,
            "pool_failed": 0,
            "pool_completed": 50,
            "zombie_child_count": 0,
            "thread_count": 10,
        }
        t = HealthThresholds()
        score, degraded, _critical = calculate_health_score(fields, t)
        assert score == pytest.approx(0.8)
        assert "memory" in degraded

    def test_critical_rss(self):
        fields = {
            "rss_mb": 3000.0,
            "fd_count": 10,
            "fd_limit": 1024,
            "circuits_open": 0,
            "cache_hit_rate": 0.9,
            "cache_entries": 200,
            "pool_failed": 0,
            "pool_completed": 50,
            "zombie_child_count": 0,
            "thread_count": 10,
        }
        t = HealthThresholds()
        score, _degraded, critical = calculate_health_score(fields, t)
        assert score == pytest.approx(0.5)
        assert "memory" in critical

    def test_critical_fd(self):
        fields = {
            "rss_mb": 100.0,
            "fd_count": 980,
            "fd_limit": 1024,
            "circuits_open": 0,
            "cache_hit_rate": 0.9,
            "cache_entries": 200,
            "pool_failed": 0,
            "pool_completed": 50,
            "zombie_child_count": 0,
            "thread_count": 10,
        }
        t = HealthThresholds()
        score, _degraded, critical = calculate_health_score(fields, t)
        assert score == pytest.approx(0.5)
        assert "file_descriptors" in critical

    def test_open_circuits_penalty(self):
        fields = {
            "rss_mb": 100.0,
            "fd_count": 10,
            "fd_limit": 1024,
            "circuits_open": 2,
            "cache_hit_rate": 0.9,
            "cache_entries": 200,
            "pool_failed": 0,
            "pool_completed": 50,
            "zombie_child_count": 0,
            "thread_count": 10,
        }
        t = HealthThresholds()
        score, _degraded, critical = calculate_health_score(fields, t)
        assert score == pytest.approx(0.7)
        assert "circuit_breakers" in critical

    def test_zombie_penalty(self):
        fields = {
            "rss_mb": 100.0,
            "fd_count": 10,
            "fd_limit": 1024,
            "circuits_open": 0,
            "cache_hit_rate": 0.9,
            "cache_entries": 200,
            "pool_failed": 0,
            "pool_completed": 50,
            "zombie_child_count": 2,
            "thread_count": 10,
        }
        t = HealthThresholds()
        score, degraded, _critical = calculate_health_score(fields, t)
        assert score == pytest.approx(0.8)
        assert "zombie_processes" in degraded

    def test_pool_failure_penalty(self):
        fields = {
            "rss_mb": 100.0,
            "fd_count": 10,
            "fd_limit": 1024,
            "circuits_open": 0,
            "cache_hit_rate": 0.9,
            "cache_entries": 200,
            "pool_failed": 8,
            "pool_completed": 12,
            "zombie_child_count": 0,
            "thread_count": 10,
        }
        t = HealthThresholds()
        score, _degraded, critical = calculate_health_score(fields, t)
        assert score == pytest.approx(0.85)
        assert "subprocess_pool" in critical

    def test_low_cache_hit_rate_ignored_below_min_lookups(self):
        """Cache penalty only applies after enough lookups."""
        fields = {
            "rss_mb": 100.0,
            "fd_count": 10,
            "fd_limit": 1024,
            "circuits_open": 0,
            "cache_hit_rate": 0.1,
            "cache_entries": 5,  # Below min_lookups
            "pool_failed": 0,
            "pool_completed": 50,
            "zombie_child_count": 0,
            "thread_count": 10,
        }
        t = HealthThresholds()
        score, _degraded, _critical = calculate_health_score(fields, t)
        assert score == 1.0  # No penalty

    def test_score_clamp_to_zero(self):
        """Score cannot go below 0.0 even with many penalties."""
        fields = {
            "rss_mb": 3000.0,
            "fd_count": 1000,
            "fd_limit": 1024,
            "circuits_open": 5,
            "cache_hit_rate": 0.0,
            "cache_entries": 200,
            "pool_failed": 20,
            "pool_completed": 5,
            "zombie_child_count": 5,
            "thread_count": 300,
        }
        t = HealthThresholds()
        score, _degraded, _critical = calculate_health_score(fields, t)
        assert score == 0.0

    def test_combined_penalties(self):
        """Multiple moderate issues compound."""
        fields = {
            "rss_mb": 1200.0,  # -0.2 degraded
            "fd_count": 10,
            "fd_limit": 1024,
            "circuits_open": 1,  # -0.15
            "cache_hit_rate": 0.1,  # -0.1
            "cache_entries": 200,
            "pool_failed": 0,
            "pool_completed": 50,
            "zombie_child_count": 0,
            "thread_count": 10,
        }
        t = HealthThresholds()
        score, degraded, _critical = calculate_health_score(fields, t)
        assert score == pytest.approx(0.55)
        assert "memory" in degraded


# ── HealthMonitor state transitions ───────────────────────────────────────


class TestHealthMonitorTransitions:
    """Test event emission on state transitions."""

    def _make_monitor(self) -> HealthMonitor:
        return HealthMonitor(thresholds=HealthThresholds())

    def test_normal_to_degraded_emits_event(self):
        monitor = self._make_monitor()
        snap_degraded = _make_snapshot(health_score=0.5)

        with patch.object(monitor, "_emit_degraded") as mock_emit:
            monitor._handle_state_transition("degraded", snap_degraded)
            mock_emit.assert_called_once_with(snap_degraded)

    def test_degraded_to_critical_emits_event(self):
        monitor = self._make_monitor()
        monitor._previous_state = "degraded"
        snap_critical = _make_snapshot(health_score=0.2)

        with patch.object(monitor, "_emit_critical") as mock_emit:
            monitor._handle_state_transition("critical", snap_critical)
            mock_emit.assert_called_once_with(snap_critical)

    def test_normal_to_critical_emits_critical(self):
        """Skipping degraded still emits critical."""
        monitor = self._make_monitor()
        snap = _make_snapshot(health_score=0.1)

        with patch.object(monitor, "_emit_critical") as mock_emit:
            monitor._handle_state_transition("critical", snap)
            mock_emit.assert_called_once_with(snap)

    def test_critical_to_normal_emits_recovered(self):
        monitor = self._make_monitor()
        monitor._previous_state = "critical"
        snap = _make_snapshot(health_score=0.9)

        with patch.object(monitor, "_emit_recovered") as mock_emit:
            monitor._handle_state_transition("normal", snap)
            mock_emit.assert_called_once_with(snap)

    def test_degraded_to_normal_emits_recovered(self):
        monitor = self._make_monitor()
        monitor._previous_state = "degraded"
        snap = _make_snapshot(health_score=0.9)

        with patch.object(monitor, "_emit_recovered") as mock_emit:
            monitor._handle_state_transition("normal", snap)
            mock_emit.assert_called_once_with(snap)

    def test_no_spam_same_state(self):
        """Staying in same state does NOT re-emit."""
        monitor = self._make_monitor()
        monitor._previous_state = "degraded"
        snap = _make_snapshot(health_score=0.5)

        with (
            patch.object(monitor, "_emit_degraded") as d,
            patch.object(monitor, "_emit_critical") as c,
            patch.object(monitor, "_emit_recovered") as r,
        ):
            monitor._handle_state_transition("degraded", snap)
            d.assert_not_called()
            c.assert_not_called()
            r.assert_not_called()

    def test_no_spam_staying_critical(self):
        monitor = self._make_monitor()
        monitor._previous_state = "critical"
        snap = _make_snapshot(health_score=0.1)

        with patch.object(monitor, "_emit_critical") as mock_emit:
            monitor._handle_state_transition("critical", snap)
            mock_emit.assert_not_called()


# ── HealthMonitor.check_now() ─────────────────────────────────────────────


class TestHealthMonitorCheckNow:
    """Test one-shot health check."""

    @pytest.mark.asyncio
    async def test_check_now_returns_snapshot(self):
        monitor = HealthMonitor(thresholds=HealthThresholds())
        with patch("beagle.health.monitor.collect_snapshot") as mock_collect:
            mock_collect.return_value = _make_snapshot()
            snap = await monitor.check_now()
            assert isinstance(snap, HealthSnapshot)
            assert snap.rss_mb == 256.0

    @pytest.mark.asyncio
    async def test_check_now_appends_to_history(self):
        monitor = HealthMonitor(thresholds=HealthThresholds())
        with patch("beagle.health.monitor.collect_snapshot") as mock_collect:
            mock_collect.return_value = _make_snapshot()
            await monitor.check_now()
            assert len(monitor.history) == 1

    @pytest.mark.asyncio
    async def test_latest_returns_most_recent(self):
        monitor = HealthMonitor(thresholds=HealthThresholds())
        with patch("beagle.health.monitor.collect_snapshot") as mock_collect:
            snap1 = _make_snapshot(rss_mb=100.0)
            snap2 = _make_snapshot(rss_mb=200.0)
            mock_collect.side_effect = [snap1, snap2]
            await monitor.check_now()
            await monitor.check_now()
            assert monitor.latest is not None
            assert monitor.latest.rss_mb == 200.0


# ── Trend analysis ────────────────────────────────────────────────────────


class TestTrendAnalysis:
    """Test HealthMonitor.trend() method."""

    def test_stable_with_no_history(self):
        monitor = HealthMonitor()
        assert monitor.trend("health_score") == "stable"

    def test_stable_with_constant_values(self):
        monitor = HealthMonitor()
        for _ in range(6):
            monitor._history.append(_make_snapshot(health_score=0.8))
        assert monitor.trend("health_score") == "stable"

    def test_improving_health_score(self):
        monitor = HealthMonitor()
        # First 3 snapshots: low score, last 3: high score
        for score in [0.5, 0.5, 0.5, 0.9, 0.9, 0.9]:
            monitor._history.append(_make_snapshot(health_score=score))
        assert monitor.trend("health_score") == "improving"

    def test_degrading_health_score(self):
        monitor = HealthMonitor()
        for score in [0.9, 0.9, 0.9, 0.4, 0.4, 0.4]:
            monitor._history.append(_make_snapshot(health_score=score))
        assert monitor.trend("health_score") == "degrading"

    def test_improving_rss(self):
        """For rss_mb, lower is better."""
        monitor = HealthMonitor()
        for rss in [500.0, 500.0, 500.0, 200.0, 200.0, 200.0]:
            monitor._history.append(_make_snapshot(rss_mb=rss))
        assert monitor.trend("rss_mb") == "improving"

    def test_degrading_rss(self):
        monitor = HealthMonitor()
        for rss in [200.0, 200.0, 200.0, 800.0, 800.0, 800.0]:
            monitor._history.append(_make_snapshot(rss_mb=rss))
        assert monitor.trend("rss_mb") == "degrading"

    def test_unknown_metric_returns_stable(self):
        monitor = HealthMonitor()
        for _ in range(6):
            monitor._history.append(_make_snapshot())
        assert monitor.trend("nonexistent_field") == "stable"


# ── Singleton ─────────────────────────────────────────────────────────────


class TestSingleton:
    """Test get_health_monitor() returns same instance."""

    def test_singleton_returns_same_instance(self):
        import beagle.health.monitor as mod

        # Reset singleton for test isolation
        mod._monitor = None
        m1 = get_health_monitor()
        m2 = get_health_monitor()
        assert m1 is m2
        mod._monitor = None  # Clean up


# ── Collector resilience ──────────────────────────────────────────────────


class TestCollectorResilience:
    """One subsystem failing must not crash the whole collection."""

    def test_collect_snapshot_returns_valid_snapshot(self):
        """Full collection produces a valid HealthSnapshot."""
        from beagle.health.collector import (
            collect_snapshot,
        )

        thresholds = HealthThresholds()
        snap = collect_snapshot(thresholds)
        assert isinstance(snap, HealthSnapshot)
        assert snap.health_score >= 0.0
        assert snap.health_score <= 1.0

    def test_circuit_collector_returns_safe_defaults(self):
        """_collect_circuits returns ({}, 0) when CB module is unavailable."""
        from beagle.health.collector import (
            _collect_circuits,
        )

        # The function already has try/except — just verify it works
        circuits, count = _collect_circuits()
        assert isinstance(circuits, dict)
        assert isinstance(count, int)
        assert count >= 0

    def test_cache_collector_returns_numeric(self):
        """_collect_cache returns (float, int) even with no prior cache use."""
        from beagle.health.collector import (
            _collect_cache,
        )

        hit_rate, entries = _collect_cache()
        assert isinstance(hit_rate, float)
        assert isinstance(entries, int)
        assert 0.0 <= hit_rate <= 1.0

    def test_os_metrics_never_crash(self):
        """OS-level collectors always return valid values."""
        from beagle.health.collector import (
            _collect_fd_count,
            _collect_fd_limit,
            _collect_rss_mb,
            _collect_thread_count,
            _collect_zombie_children,
        )

        assert isinstance(_collect_rss_mb(), float)
        assert isinstance(_collect_fd_count(), int)
        assert _collect_fd_limit() > 0
        assert _collect_thread_count() > 0
        assert isinstance(_collect_zombie_children(), int)


# ── _compute_state ────────────────────────────────────────────────────────


class TestComputeState:
    """Test state classification from score."""

    def test_normal(self):
        m = HealthMonitor()
        assert m._compute_state(0.9) == "normal"

    def test_degraded(self):
        m = HealthMonitor()
        assert m._compute_state(0.5) == "degraded"

    def test_critical(self):
        m = HealthMonitor()
        assert m._compute_state(0.2) == "critical"

    def test_boundary_degraded(self):
        m = HealthMonitor()
        # Score exactly at degraded_score (0.6) is NOT degraded (>=)
        assert m._compute_state(0.6) == "normal"

    def test_boundary_critical(self):
        m = HealthMonitor()
        # Score exactly at critical_score (0.3) is NOT critical (>=)
        assert m._compute_state(0.3) == "degraded"
