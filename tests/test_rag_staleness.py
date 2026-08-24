"""Tests for RAG staleness tracker and hot-swap integration with context folding."""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


def _make_tracker(staleness_file: str, *, ingested: bool = True):
    """Create a fresh RAGStalenessTracker with a temp sidecar file.

    Always resets the singleton first to guarantee isolation.

    Args:
        ingested: When True (default) seed a *backdated* successful
            ingestion so the tracker starts in the fresh, un-throttled
            state. B-24 (audit v13.22.1) corrected ``is_stale`` so that a
            tracker which has never recorded a successful ingestion reports
            **stale** — an empty index is not fresh. Tests needing "fresh"
            as a precondition must now say so explicitly. The timestamp is
            backdated past _MIN_REINGEST_INTERVAL (but well inside
            _MAX_STALE_AGE) so the tracker is simultaneously not stale and
            not throttled, which is the state those tests actually mean.
            Counters are left at zero so assertions about them still read
            naturally.
    """
    import time as _time

    from beagle.context.rag_staleness import (
        _MIN_REINGEST_INTERVAL,
        RAGStalenessTracker,
        reset_staleness_tracker,
    )

    reset_staleness_tracker()
    tracker = RAGStalenessTracker(staleness_file=staleness_file)
    if ingested:
        tracker._record.last_reingested_at = _time.time() - (_MIN_REINGEST_INTERVAL + 60)
    return tracker


# ── RAGStalenessTracker Tests ─────────────────────────────────────────────────


class TestRAGStalenessTracker:
    """Test RAG staleness tracking and persistence."""

    def setup_method(self):
        self._tmpdir = tempfile.mkdtemp()
        self._staleness_file = str(Path(self._tmpdir) / ".rag_staleness.json")

    def teardown_method(self):
        from beagle.context.rag_staleness import reset_staleness_tracker

        reset_staleness_tracker()
        import shutil

        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_never_ingested_is_stale(self):
        """B-24: a tracker with no successful ingestion on record is stale.

        There is no index to serve, so reporting "fresh" told the hydration
        path to query an empty store. This is only safe to assert now that
        the in-flight guard and the attempt-based throttle bound how often
        the resulting reingest can fire (B-1).
        """
        tracker = _make_tracker(self._staleness_file, ingested=False)
        assert tracker.is_stale
        assert tracker.get_status()["last_reingested_at"] == 0

    def test_state_is_fresh_after_a_successful_ingest(self):
        """Tracker with a recorded successful ingestion is not stale."""
        tracker = _make_tracker(self._staleness_file)
        assert not tracker.is_stale
        assert tracker.staleness_age == 0.0

    def test_mark_stale_persists(self):
        """Marking stale should persist to sidecar file."""
        tracker = _make_tracker(self._staleness_file)
        tracker.mark_stale(reason="context_fold")

        assert tracker.is_stale
        assert tracker._record.reason == "context_fold"
        assert tracker._record.marked_at > 0

        # Verify persistence to file
        stale_file = Path(self._staleness_file)
        assert stale_file.exists()
        data = json.loads(stale_file.read_text(encoding="utf-8"))
        assert data["stale"] is True
        assert data["reason"] == "context_fold"

    def test_mark_fresh_persists(self):
        """Marking fresh should clear staleness."""
        tracker = _make_tracker(self._staleness_file)
        tracker.mark_stale(reason="test")
        assert tracker.is_stale

        tracker.mark_fresh(codebase_path="/test/path")
        assert not tracker.is_stale
        assert tracker._record.reingest_count == 1
        assert tracker._record.codebase_path == "/test/path"
        assert tracker._record.last_reingested_at > 0

    def test_can_reingest_after_fresh(self):
        """After mark_fresh, can_reingest should be False (throttled)."""
        tracker = _make_tracker(self._staleness_file)
        # Never ingested, so can_reingest is True
        assert tracker.can_reingest()

        # After mark_fresh, immediate re-reingest should be throttled
        tracker.mark_fresh(codebase_path="/test")

        # Override the MIN_REINGEST_INTERVAL to 0 for testing
        import beagle.context.rag_staleness as mod

        original = mod._MIN_REINGEST_INTERVAL
        mod._MIN_REINGEST_INTERVAL = 0
        try:
            assert tracker.can_reingest()
        finally:
            mod._MIN_REINGEST_INTERVAL = original

    def test_staleness_survives_reload(self):
        """Staleness state should survive re-instantiation."""
        tracker = _make_tracker(self._staleness_file)
        tracker.mark_stale(reason="context_compaction")

        # Reload from file
        from beagle.context.rag_staleness import (
            RAGStalenessTracker,
            reset_staleness_tracker,
        )

        reset_staleness_tracker()
        new_tracker = RAGStalenessTracker(staleness_file=self._staleness_file)

        assert new_tracker.is_stale
        assert new_tracker._record.reason == "context_compaction"

    def test_get_status(self):
        """Status dict should include all key fields."""
        tracker = _make_tracker(self._staleness_file)
        tracker.mark_stale(reason="test_reason")

        status = tracker.get_status()
        assert status["stale"] is True
        assert status["reason"] == "test_reason"
        assert status["can_reingest"] is True
        assert status["reingest_count"] == 0


# ── Context Integration Staleness Marking Tests ───────────────────────────────


class TestContextFoldStalenessMarking:
    """Test that enhanced_context_fold marks RAG as stale."""

    def setup_method(self):
        self._tmpdir = tempfile.mkdtemp()
        self._staleness_file = str(Path(self._tmpdir) / ".rag_staleness.json")

    def teardown_method(self):
        from beagle.context.context_integration import (
            reset_context_integration,
        )
        from beagle.context.rag_staleness import reset_staleness_tracker

        reset_staleness_tracker()
        reset_context_integration()
        import shutil

        shutil.rmtree(self._tmpdir, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_fold_marks_stale(self):
        """When context folds, RAG should be marked stale."""
        from beagle.context.context_integration import (
            ContextIntegration,
        )

        # Set up tracker with temp file before integration is created
        tracker = _make_tracker(self._staleness_file)
        assert not tracker.is_stale

        integration = ContextIntegration(auto_compress_threshold=0.50)

        # Trigger a context fold
        _result = await integration.enhanced_context_fold("x" * 10000, "aggressive")

        # RAG should now be stale
        assert tracker.is_stale
        assert tracker._record.reason == "context_fold"

    @pytest.mark.asyncio
    async def test_fold_does_not_re_mark_if_already_stale(self):
        """If RAG is already stale, fold should not overwrite reason."""
        from beagle.context.context_integration import (
            ContextIntegration,
        )

        tracker = _make_tracker(self._staleness_file)
        tracker.mark_stale(reason="manual_reindex_needed")

        integration = ContextIntegration(auto_compress_threshold=0.50)

        # Fold should not overwrite the existing reason
        _result = await integration.enhanced_context_fold("x" * 10000, "aggressive")

        assert tracker._record.reason == "manual_reindex_needed"


# ── Compaction Hook Staleness Marking Tests ─────────────────────────────────────


class TestCompactionHookStalenessMarking:
    """Test that compaction hook marks RAG stale on checkpoint save."""

    def setup_method(self):
        self._tmpdir = tempfile.mkdtemp()
        self._staleness_file = str(Path(self._tmpdir) / ".rag_staleness.json")

    def teardown_method(self):
        from beagle.context.rag_staleness import reset_staleness_tracker

        reset_staleness_tracker()
        import shutil

        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_save_checkpoint_marks_stale(self):
        """Saving a compaction checkpoint should mark RAG as stale."""
        from beagle.context.context_compaction_hook import (
            ContextMonitor,
        )

        tracker = _make_tracker(self._staleness_file)
        assert not tracker.is_stale

        monitor = ContextMonitor(
            total_iterations=25,
            checkpoint_dir=Path(self._tmpdir) / "checkpoints",
        )
        monitor.current_task = "test_task"
        monitor.current_iteration = 5
        monitor.extract_constraints = False
        monitor.extract_knowledge = False

        checkpoint_path = monitor.save_checkpoint()

        assert checkpoint_path.exists()
        assert tracker.is_stale
        assert tracker._record.reason == "context_compaction"


# ── Hydration Node Staleness Integration Tests ──────────────────────────────────


class TestHydrationStalenessCheck:
    """Test that hydration triggers hot-swap reingestion when stale."""

    def setup_method(self):
        self._tmpdir = tempfile.mkdtemp()
        self._staleness_file = str(Path(self._tmpdir) / ".rag_staleness.json")

    def teardown_method(self):
        from beagle.context.rag_staleness import reset_staleness_tracker

        reset_staleness_tracker()
        import shutil

        shutil.rmtree(self._tmpdir, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_hydration_triggers_reingest_when_stale(self):
        """When RAG is stale, hydration should trigger hot-swap reingestion."""

        tracker = _make_tracker(self._staleness_file)
        tracker.mark_stale(reason="context_fold")
        assert tracker.is_stale

        # Patch hotswap_ingest at the module level where it's imported
        with patch("beagle.infrastructure.hotswap_ingest.hotswap_ingest") as mock_hotswap:
            mock_hotswap.return_value = {
                "status": "ok",
                "stage": {"files_processed": 100, "chunks_created": 500},
                "swap": {"swapped": ["lancedb", "kuzu"]},
            }

            reingest_result = await tracker.trigger_reingest_if_stale()

            assert reingest_result["status"] == "reingested"
            assert not tracker.is_stale  # Should be fresh after reingest
            mock_hotswap.assert_called_once()

    @pytest.mark.asyncio
    async def test_hydration_skips_reingest_when_fresh(self):
        """When RAG is fresh, reingestion should be skipped."""

        tracker = _make_tracker(self._staleness_file)
        assert not tracker.is_stale

        reingest_result = await tracker.trigger_reingest_if_stale()
        assert reingest_result["status"] == "skipped"
        assert reingest_result["reason"] == "not_stale"


# ── End-to-End Integration Tests ────────────────────────────────────────────────


class TestEndToEndStalenessFlow:
    """Test the complete flow: fold → stale → hydrate → reingest → fresh."""

    def setup_method(self):
        self._tmpdir = tempfile.mkdtemp()
        self._staleness_file = str(Path(self._tmpdir) / ".rag_staleness.json")

    def teardown_method(self):
        from beagle.context.context_integration import (
            reset_context_integration,
        )
        from beagle.context.rag_staleness import reset_staleness_tracker

        reset_staleness_tracker()
        reset_context_integration()
        import shutil

        shutil.rmtree(self._tmpdir, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_fold_to_hydrate_flow(self):
        """Context fold marks stale, reingestion clears it."""
        from beagle.context.context_integration import (
            ContextIntegration,
        )

        tracker = _make_tracker(self._staleness_file)

        # Step 1: Initial state is fresh
        assert not tracker.is_stale

        # Step 2: Context fold marks stale
        integration = ContextIntegration(auto_compress_threshold=0.50)
        _result = await integration.enhanced_context_fold("test data" * 100, "aggressive")

        assert tracker.is_stale
        assert tracker._record.reason == "context_fold"

        # Step 3: Simulate hydration triggering reingestion
        with patch("beagle.infrastructure.hotswap_ingest.hotswap_ingest") as mock_hotswap:
            mock_hotswap.return_value = {
                "status": "ok",
                "stage": {"files_processed": 50},
                "swap": {"swapped": ["lancedb", "kuzu"]},
            }

            reingest_result = await tracker.trigger_reingest_if_stale(codebase_path="/test/path")

            assert reingest_result["status"] == "reingested"
            assert not tracker.is_stale
            assert tracker._record.reingest_count == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
