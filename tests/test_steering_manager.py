"""Tests for Steering Manager.

Tests for workflow steering directives.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

from beagle.steering.manager import (
    SteeringManager,
)
from beagle.steering.types import (
    SteeringDirective,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


class TestSteeringDirective:
    """Test SteeringDirective dataclass."""

    def test_directive_creation(self):
        """SteeringDirective can be created."""
        directive = SteeringDirective(workflow_id="test-wf")
        assert directive.workflow_id == "test-wf"
        assert directive.has_guidance is False

    def test_directive_with_priority(self):
        """SteeringDirective can have priority guidance."""
        directive = SteeringDirective(
            workflow_id="test",
            priority_guidance="Focus on security issues",
            has_guidance=True,
        )
        assert directive.has_guidance is True
        assert directive.priority_guidance == "Focus on security issues"

    def test_directive_with_skip_nodes(self):
        """SteeringDirective can skip nodes."""
        directive = SteeringDirective(
            workflow_id="test",
            skip_nodes=["research", "synthesis"],
            has_guidance=True,
        )
        assert directive.has_guidance is True
        assert "research" in directive.skip_nodes

    def test_directive_with_budget_override(self):
        """SteeringDirective can override budget."""
        directive = SteeringDirective(
            workflow_id="test",
            budget_override_usd=5.0,
            has_guidance=True,
        )
        assert directive.has_guidance is True
        assert directive.budget_override_usd == 5.0

    def test_directive_with_stop_after(self):
        """SteeringDirective can stop after node."""
        directive = SteeringDirective(
            workflow_id="test",
            stop_after_node="planner",
            has_guidance=True,
        )
        assert directive.has_guidance is True
        assert directive.stop_after_node == "planner"

    def test_directive_full(self):
        """SteeringDirective can have all fields."""
        directive = SteeringDirective(
            workflow_id="full-test",
            priority_guidance="Test priority",
            skip_nodes=["n1", "n2"],
            budget_override_usd=10.0,
            stop_after_node="final",
            source="file",
            has_guidance=True,
        )
        assert directive.has_guidance is True
        assert directive.priority_guidance == "Test priority"
        assert len(directive.skip_nodes) == 2
        assert directive.budget_override_usd == 10.0
        assert directive.stop_after_node == "final"
        assert directive.source == "file"


class TestSteeringManager:
    """Test SteeringManager."""

    def test_manager_creation(self):
        """SteeringManager can be created."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = SteeringManager(Path(tmpdir), "test-workflow")
            assert manager.workflow_id == "test-workflow"

    def test_manager_check_empty(self):
        """SteeringManager.check returns empty directive when no guidance."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = SteeringManager(Path(tmpdir), "test")
            directive = manager.check()

            assert directive is not None
            assert directive.has_guidance is False

    def test_manager_applied_count(self):
        """SteeringManager tracks applied directives."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = SteeringManager(Path(tmpdir), "test")

            # Initial count is 0
            assert manager.applied_count == 0


class TestSteeringSourcesIntegration:
    """Integration tests for steering sources."""

    def test_file_source_creation(self):
        """File steering source can be created."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source_manager = SteeringManager(Path(tmpdir), "test")
            assert source_manager is not None

    def test_check_returns_directive(self):
        """Check always returns a SteeringDirective."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = SteeringManager(Path(tmpdir), "test-workflow")

            result = manager.check()

            assert isinstance(result, SteeringDirective)
            assert result.workflow_id == "test-workflow"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
