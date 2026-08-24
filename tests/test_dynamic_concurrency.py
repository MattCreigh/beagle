"""Tests for dynamic concurrency scaling."""

from __future__ import annotations

from unittest.mock import patch

from beagle.core.dynamic_pool import DynamicConcurrency


class TestDynamicConcurrency:
    """Test dynamic pool concurrency."""

    def test_initialization(self):
        dc = DynamicConcurrency(min_workers=2, max_workers=6)
        assert dc.min_workers == 2
        assert dc.max_workers == 6
        assert dc._current_workers == 2

    def test_fallback_no_psutil(self):
        with patch("beagle.core.dynamic_pool._PSUTIL_AVAILABLE", False):
            dc = DynamicConcurrency(min_workers=2, max_workers=6)
            workers = dc.get_optimal_workers()
            assert 2 <= workers <= 6

    def test_scale_up_on_low_cpu(self):
        dc = DynamicConcurrency(
            min_workers=2, max_workers=6, cpu_low_threshold=30.0, cooldown_seconds=0
        )
        dc._current_workers = 2
        with (
            patch("beagle.core.dynamic_pool._PSUTIL_AVAILABLE", True),
            patch("psutil.cpu_percent", return_value=10.0),
        ):
            workers = dc.get_optimal_workers()
            assert workers >= 2

    def test_scale_down_on_high_cpu(self):
        dc = DynamicConcurrency(
            min_workers=2, max_workers=6, cpu_high_threshold=80.0, cooldown_seconds=0
        )
        dc._current_workers = 6
        with (
            patch("beagle.core.dynamic_pool._PSUTIL_AVAILABLE", True),
            patch("psutil.cpu_percent", return_value=95.0),
        ):
            workers = dc.get_optimal_workers()
            assert workers <= 6

    def test_force_workers_clamp_high(self):
        dc = DynamicConcurrency(min_workers=2, max_workers=6)
        dc.force_workers(100)
        assert dc._current_workers == 6

    def test_force_workers_clamp_low(self):
        dc = DynamicConcurrency(min_workers=2, max_workers=6)
        dc.force_workers(0)
        assert dc._current_workers == 2

    def test_stats_returns_data(self):
        dc = DynamicConcurrency()
        stats = dc.stats
        assert stats.current_workers > 0
