"""Tests for execution-history-driven model selection."""

from __future__ import annotations

import pathlib


def _make_fresh_db(tmp_path: pathlib.Path):
    """Create a TrackingDatabase with fresh singleton for test isolation."""
    from beagle.tracking.database import TrackingDatabase

    # Reset singleton to ensure fresh DB per test
    TrackingDatabase._instance = None
    db = TrackingDatabase(db_path=tmp_path / "test.db")
    TrackingDatabase._instance = db
    return db


def _cleanup_db() -> None:
    """Reset singleton after test."""
    from beagle.tracking.database import TrackingDatabase

    TrackingDatabase._instance = None


class TestModelPerformanceTracking:
    """Verify model outcomes are recorded and queried correctly."""

    def test_record_success(self, tmp_path):
        """Successful execution should increment success count."""
        db = _make_fresh_db(tmp_path)
        try:
            db.record_model_outcome(
                model="glm-5.1:cloud",
                provider="ollama",
                node_type="research",
                success=True,
                latency_seconds=2.5,
                input_tokens=100,
                output_tokens=500,
            )
            rankings = db.query_model_rankings(
                node_type="research",
                min_executions=1,
            )
            assert len(rankings) == 1
            assert rankings[0]["model"] == "glm-5.1:cloud"
            assert rankings[0]["success_rate"] == 1.0
        finally:
            _cleanup_db()

    def test_record_failure(self, tmp_path):
        """Failed execution should increment failure count."""
        db = _make_fresh_db(tmp_path)
        try:
            db.record_model_outcome(
                model="glm-5.1:cloud",
                provider="ollama",
                node_type="research",
                success=False,
                latency_seconds=30.0,
                failure_reason="timeout",
            )
            rankings = db.query_model_rankings(
                node_type="research",
                min_executions=1,
            )
            assert rankings[0]["success_rate"] == 0.0
        finally:
            _cleanup_db()

    def test_running_averages(self, tmp_path):
        """Multiple executions should produce correct running averages."""
        db = _make_fresh_db(tmp_path)
        try:
            for _ in range(3):
                db.record_model_outcome(
                    model="fast-model",
                    provider="cloud",
                    node_type="exec",
                    success=True,
                    latency_seconds=1.0,
                )
            db.record_model_outcome(
                model="fast-model",
                provider="cloud",
                node_type="exec",
                success=False,
                latency_seconds=30.0,
                failure_reason="crash",
            )
            rankings = db.query_model_rankings(
                node_type="exec",
                min_executions=1,
            )
            assert rankings[0]["total_executions"] == 4
            assert rankings[0]["success_rate"] == 0.75
        finally:
            _cleanup_db()

    def test_min_executions_filter(self, tmp_path):
        """Models below min_executions should be excluded."""
        db = _make_fresh_db(tmp_path)
        try:
            db.record_model_outcome(
                model="rare-model",
                provider="cloud",
                node_type="exec",
                success=True,
                latency_seconds=1.0,
            )
            rankings = db.query_model_rankings(
                node_type="exec",
                min_executions=3,
            )
            assert len(rankings) == 0
        finally:
            _cleanup_db()


class TestLearnedFallbackChain:
    """Verify fallback chain reordering from learned data."""

    def test_reorders_by_success_rate(self, tmp_path):
        """Higher success rate models should come first."""
        db = _make_fresh_db(tmp_path)
        try:
            # Model A: 100% success (5/5)
            for _ in range(5):
                db.record_model_outcome(
                    model="model-a",
                    provider="cloud",
                    node_type="research",
                    success=True,
                    latency_seconds=2.0,
                )
            # Model B: 60% success (3 success, 2 failure)
            for _ in range(3):
                db.record_model_outcome(
                    model="model-b",
                    provider="cloud",
                    node_type="research",
                    success=True,
                    latency_seconds=1.0,
                )
            for _ in range(2):
                db.record_model_outcome(
                    model="model-b",
                    provider="cloud",
                    node_type="research",
                    success=False,
                    latency_seconds=1.0,
                    failure_reason="error",
                )

            from beagle.utils.subprocess_pool import (
                _get_learned_fallback_chain,
            )

            chain = _get_learned_fallback_chain(
                ["model-b", "model-a", "model-c"],
                node_type="research",
            )
            # model-a (100%) should come before model-b (60%)
            assert chain.index("model-a") < chain.index("model-b")
            # model-c (no data) should be last
            assert chain[-1] == "model-c"
        finally:
            _cleanup_db()

    def test_empty_history_returns_static_chain(self, tmp_path):
        """No execution history returns the original static chain."""
        # Set up a fresh DB with no model_performance rows
        _make_fresh_db(tmp_path)
        try:
            from beagle.utils.subprocess_pool import (
                _get_learned_fallback_chain,
            )

            chain = ["model-x", "model-y", "model-z"]
            result = _get_learned_fallback_chain(
                chain,
                node_type="test-node",
            )
            assert result == chain
        finally:
            _cleanup_db()

    def test_disabled_returns_static_chain(self, tmp_path):
        """When learned_routing.enabled=False, return the original chain."""
        db = _make_fresh_db(tmp_path)
        try:
            # Seed history so reordering would normally happen
            for _ in range(5):
                db.record_model_outcome(
                    model="model-a",
                    provider="cloud",
                    node_type="research",
                    success=True,
                    latency_seconds=1.0,
                )
            for _ in range(3):
                db.record_model_outcome(
                    model="model-b",
                    provider="cloud",
                    node_type="research",
                    success=False,
                    latency_seconds=1.0,
                    failure_reason="error",
                )

            from beagle.config.config import get_config
            from beagle.utils.subprocess_pool import (
                _get_learned_fallback_chain,
            )

            config = get_config()
            original_enabled = config.learned_routing.enabled
            config.learned_routing.enabled = False
            try:
                chain = _get_learned_fallback_chain(
                    ["model-b", "model-a"],
                    node_type="research",
                )
                # Should return the original static chain unchanged
                assert chain == ["model-b", "model-a"]
            finally:
                config.learned_routing.enabled = original_enabled
        finally:
            _cleanup_db()


class TestLearnedRoutingConfig:
    """Verify LearnedRoutingConfig dataclass defaults."""

    def test_default_values(self):
        """Config should have sensible defaults."""
        from beagle.config.schema import LearnedRoutingConfig

        cfg = LearnedRoutingConfig()
        assert cfg.enabled is True
        assert cfg.min_executions == 3
        assert cfg.success_rate_weight == 0.7
        assert cfg.latency_weight == 0.3
        assert cfg.node_type_routing is True

    def test_in_workflow_config(self):
        """LearnedRoutingConfig should be accessible via WorkflowConfig."""
        from beagle.config.schema import WorkflowConfig

        cfg = WorkflowConfig()
        assert hasattr(cfg, "learned_routing")
        assert cfg.learned_routing.enabled is True
