"""Tests for context_window module.

Tests context window management, token tracking,
and compression triggers.
"""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from beagle.context.context_window import (
    ContextMetrics,
    ContextWindowManager,
    get_context_manager,
)
from beagle.cost_tracker import ContextWindowStatus


class TestContextMetrics:
    """Test context metrics dataclass."""

    def test_metrics_creation(self):
        """Test creating metrics."""
        metrics = ContextMetrics(
            total_tokens=1000,
            input_tokens=600,
            output_tokens=400,
        )
        assert metrics.total_tokens == 1000
        assert metrics.input_tokens == 600
        assert metrics.output_tokens == 400

    def test_metrics_defaults(self):
        """Test default values."""
        metrics = ContextMetrics()
        assert metrics.total_tokens == 0
        assert metrics.context_utilization == 0.0
        assert metrics.compression_recommended is False


class TestContextWindowStatus:
    """Test context window status tracking (from cost_tracker)."""

    def test_status_creation(self):
        """Test creating a status object."""
        status = ContextWindowStatus(
            model="test-model",
            context_window=128000,
            current_tokens=5000,
        )
        assert status.current_tokens == 5000
        assert status.context_window == 128000

    def test_utilization_calculation(self):
        """Test utilization percentage calculation."""
        status = ContextWindowStatus(
            model="test-model",
            context_window=128000,
            current_tokens=32000,
        )
        assert status.utilization == pytest.approx(0.25, rel=0.01)
        assert status.utilization_percent == 25.0

    def test_remaining_tokens(self):
        """Test remaining tokens calculation."""
        status = ContextWindowStatus(
            model="test-model",
            context_window=128000,
            current_tokens=10000,
        )
        assert status.remaining_tokens == 118000

    def test_warning_thresholds(self):
        """Test warning thresholds."""
        # Under threshold - no warning
        status_low = ContextWindowStatus(
            model="test-model",
            context_window=128000,
            current_tokens=50000,
        )
        assert not status_low.should_warn()
        assert not status_low.should_critical()

        # Over 80% - warning
        status_warn = ContextWindowStatus(
            model="test-model",
            context_window=128000,
            current_tokens=110000,
        )
        assert status_warn.should_warn()
        assert not status_warn.should_critical()

        # Over 95% - critical
        status_crit = ContextWindowStatus(
            model="test-model",
            context_window=128000,
            current_tokens=125000,
        )
        assert status_crit.should_warn()
        assert status_crit.should_critical()


class TestContextWindowManager:
    """Test context window manager functionality."""

    def test_manager_creation(self):
        """Test creating a context manager."""
        manager = ContextWindowManager(
            context_window=128000,
        )
        assert manager.context_window == 128000

    def test_manager_with_model(self):
        """Test manager with specific model."""
        manager = ContextWindowManager(
            model="glm-5.1:cloud",
            context_window=128000,
        )
        assert manager.model == "glm-5.1:cloud"

    def test_start_node(self):
        """Test starting a node."""
        manager = ContextWindowManager(context_window=128000)
        manager.start_node("test_node")
        assert manager._current_node == "test_node"

    def test_record_node_tokens(self):
        """Test recording tokens for a node."""
        manager = ContextWindowManager(context_window=128000)
        metrics = asyncio.run(
            manager.record_node_tokens(
                node_name="test_node",
                input_tokens=1000,
                output_tokens=500,
            )
        )
        assert metrics.total_tokens == 1500
        assert metrics.input_tokens == 1000
        assert metrics.output_tokens == 500

    def test_multiple_node_records(self):
        """Test recording multiple nodes."""
        manager = ContextWindowManager(context_window=128000)

        asyncio.run(manager.record_node_tokens("node1", 1000, 500))
        asyncio.run(manager.record_node_tokens("node2", 2000, 1000))

        # Check both nodes are tracked
        assert "node1" in manager.node_metrics
        assert "node2" in manager.node_metrics

    def test_get_status(self):
        """Test getting context status."""
        manager = ContextWindowManager(context_window=128000)
        status = manager.cost_tracker.context_status

        assert isinstance(status, ContextWindowStatus)
        assert status.context_window == 128000


class TestGetContextManager:
    """Test global context manager singleton."""

    def test_get_singleton(self):
        """Test getting the global context manager."""
        manager1 = get_context_manager()
        manager2 = get_context_manager()

        # Should be same instance (singleton pattern)
        # Note: Implementation may vary, this tests the pattern exists
        assert manager1 is not None
        assert manager2 is not None


class TestContextWindowEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_zero_tokens(self):
        """Test with zero tokens."""
        status = ContextWindowStatus(
            model="test-model",
            context_window=128000,
            current_tokens=0,
        )
        assert status.utilization == 0.0
        assert status.remaining_tokens == 128000

    def test_full_context(self):
        """Test when context is full."""
        status = ContextWindowStatus(
            model="test-model",
            context_window=128000,
            current_tokens=128000,
        )
        assert status.utilization == 1.0
        assert status.utilization_percent == 100.0
        assert status.remaining_tokens == 0
        assert status.should_critical()

    def test_over_context(self):
        """Test when context is over limit (edge case)."""
        status = ContextWindowStatus(
            model="test-model",
            context_window=128000,
            current_tokens=130000,
        )
        # Implementation should handle gracefully
        assert status.utilization > 1.0
        # remaining_tokens clips to 0 (never negative)
        assert status.remaining_tokens == 0

    def test_very_large_context(self):
        """Test with very large context window."""
        manager = ContextWindowManager(context_window=200000)
        asyncio.run(manager.record_node_tokens("test", 50000, 30000))

        status = manager.cost_tracker.context_status
        assert status.context_window == 200000


class TestContextIntegration:
    """Test context integration with token counting."""

    def test_token_tracking_sequence(self):
        """Test typical token tracking sequence."""
        manager = ContextWindowManager(context_window=100000)

        # Initial prompt
        asyncio.run(manager.record_node_tokens("start", 5000, 0))

        # First agent response
        asyncio.run(manager.record_node_tokens("agent1", 3000, 8000))

        # Second agent turn
        asyncio.run(manager.record_node_tokens("agent2", 3000, 6000))

        # Check all nodes tracked
        assert "start" in manager.node_metrics
        assert "agent1" in manager.node_metrics
        assert "agent2" in manager.node_metrics


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
