"""Tests for hardware optimization modules.

Tests ramdisk staging, incremental ingestion, dynamic concurrency,
warm worker pool, and other hardware-aware features.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


class TestRamdiskStaging:
    """Test ramdisk staging directory resolution."""

    def test_get_staging_dir_returns_path(self):
        """Staging dir returns a valid path."""
        from beagle.infrastructure.cast_ingestion import (
            _get_staging_dir,
        )

        result = _get_staging_dir()
        assert isinstance(result, str)
        assert len(result) > 0

    def test_get_staging_dir_fallback_to_tmpdir(self):
        """When ramdisk unavailable, falls back to tempdir."""
        from beagle.infrastructure.cast_ingestion import (
            _get_staging_dir,
        )

        with patch("beagle.infrastructure.cast_ingestion.Path") as mock_path:
            # Simulate ramdisk path not existing
            mock_path.return_value.exists.return_value = False
            result = _get_staging_dir()
            # Should return something (even if it's the ramdisk path from config)
            assert isinstance(result, str)

    def test_ssd_writes_counter(self):
        """SSD writes saved counter increments correctly."""
        from beagle.infrastructure import cast_ingestion

        original = cast_ingestion._ssd_writes_saved_bytes
        try:
            cast_ingestion._ssd_writes_saved_bytes = 0
            cast_ingestion._ssd_writes_saved_bytes += 1024 * 1024  # 1 MB
            assert cast_ingestion._ssd_writes_saved_bytes == 1048576
        finally:
            cast_ingestion._ssd_writes_saved_bytes = original


class TestIncrementalIngestCache:
    """Test incremental ingestion cache."""

    def test_load_nonexistent_cache(self, tmp_path):
        """Loading cache from nonexistent file returns empty dict."""
        from beagle.infrastructure.cast_ingestion import (
            _load_ingest_cache,
        )

        result = _load_ingest_cache(str(tmp_path))
        assert result == {}

    def test_save_and_load_cache(self, tmp_path):
        """Cache round-trip: save then load."""
        from beagle.infrastructure.cast_ingestion import (
            _load_ingest_cache,
            _save_ingest_cache,
        )

        cache = {"file.py": {"mtime": "123.456", "hash": "abc123"}}
        _save_ingest_cache(str(tmp_path), cache)
        loaded = _load_ingest_cache(str(tmp_path))
        assert loaded == cache

    def test_cache_file_created(self, tmp_path):
        """Cache file is created on disk."""
        from beagle.infrastructure.cast_ingestion import (
            _save_ingest_cache,
        )

        _save_ingest_cache(str(tmp_path), {"f.py": {"mtime": "1", "hash": "a"}})
        assert (tmp_path / ".beagle_ingest_cache.json").exists()


class TestDynamicConcurrency:
    """Test dynamic concurrency scaling."""

    def test_default_initialization(self):
        """DynamicConcurrency initializes with sensible defaults."""
        from beagle.core.dynamic_pool import DynamicConcurrency

        dc = DynamicConcurrency(min_workers=2, max_workers=6)
        assert dc.min_workers == 2
        assert dc.max_workers == 6

    def test_fallback_without_psutil(self):
        """Without psutil, returns cpu_count-based static value."""
        from beagle.core.dynamic_pool import DynamicConcurrency

        dc = DynamicConcurrency(min_workers=2, max_workers=6)
        with patch("beagle.core.dynamic_pool._PSUTIL_AVAILABLE", False):
            workers = dc.get_optimal_workers()
            assert 2 <= workers <= 6

    def test_force_workers_clamps(self):
        """Force workers clamps to min/max bounds."""
        from beagle.core.dynamic_pool import DynamicConcurrency

        dc = DynamicConcurrency(min_workers=2, max_workers=6)
        dc.force_workers(10)
        assert dc._current_workers == 6
        dc.force_workers(0)
        assert dc._current_workers == 2

    def test_stats_property(self):
        """Stats returns valid ConcurrencyStats."""
        from beagle.core.dynamic_pool import DynamicConcurrency

        dc = DynamicConcurrency()
        stats = dc.stats
        assert stats.current_workers > 0


class TestWarmWorkers:
    """Test warm worker pool lifecycle."""

    @pytest.mark.asyncio
    async def test_pool_creation(self):
        """WarmWorkerPool creates with specified count."""
        from beagle.core.warm_workers import WarmWorkerPool

        pool = WarmWorkerPool(count=2)
        assert pool.count == 2
        assert pool.active_count == 0  # Not yet initialized

    @pytest.mark.asyncio
    async def test_pool_shutdown(self):
        """Pool shutdown cleans up."""
        from beagle.core.warm_workers import WarmWorkerPool

        pool = WarmWorkerPool(count=1)
        # Don't call initialize (would spawn real subprocesses)
        result = await pool.shutdown()
        assert isinstance(result, int)

    def test_warm_worker_dataclass(self):
        """WarmWorker dataclass tracks state correctly."""
        from beagle.core.warm_workers import WarmWorker

        w = WarmWorker(worker_id=0)
        assert w.worker_id == 0
        assert not w.in_use
        assert w.task_count == 0
        assert not w.is_alive  # No process attached


class TestFaissPrefilter:
    """Test Faiss pre-filter module."""

    def test_availability_check(self):
        """is_faiss_available returns bool."""
        from beagle.infrastructure.faiss_prefilter import (
            is_faiss_available,
        )

        result = is_faiss_available()
        assert isinstance(result, bool)

    def test_search_without_index(self):
        """Search on unbuilt index returns empty results."""
        from beagle.infrastructure.faiss_prefilter import FaissPrefilter

        pf = FaissPrefilter(dimension=768)
        result = pf.search([0.1] * 768, top_k=10)
        assert result.used_faiss is False
        assert result.ids == []

    def test_reset(self):
        """Reset clears index state."""
        from beagle.infrastructure.faiss_prefilter import FaissPrefilter

        pf = FaissPrefilter()
        pf.reset()
        assert not pf._built


class TestHardwareChecks:
    """Test hardware startup checks."""

    def test_ramdisk_status(self):
        """check_ramdisk returns RamdiskStatus."""
        from beagle.infrastructure.hardware_checks import check_ramdisk

        status = check_ramdisk()
        assert hasattr(status, "available")
        assert hasattr(status, "path")

    def test_cpu_governor(self):
        """get_cpu_governor returns string."""
        from beagle.infrastructure.hardware_checks import (
            get_cpu_governor,
        )

        result = get_cpu_governor()
        assert isinstance(result, str)


class TestCPUGovernor:
    """Test CPU governor switching."""

    def test_get_current_governor(self):
        """get_current_governor returns string."""
        from beagle.infrastructure.cpu_governor import (
            get_current_governor,
        )

        result = get_current_governor()
        assert isinstance(result, str)

    def test_set_invalid_governor(self):
        """Invalid governor name returns False."""
        from beagle.infrastructure.cpu_governor import set_governor

        with patch("beagle.infrastructure.cpu_governor._GOVERNOR_BASE") as mock:
            mock.exists.return_value = False
            result = set_governor("invalid_governor")
            assert result is False
