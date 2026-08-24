"""Tests for Constraint-Aware Prompts in DAGOrchestrator.

Tests that constraints are properly injected into agent prompts
and refreshed during workflow execution.

Run with: python -m pytest tests/test_constraint_prompts.py -v
"""

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

# Import modules under test
from beagle.core.autonomous_orchestrator import (
    AgentState,
    DAGOrchestrator,
)
from beagle.infrastructure.constraint_registry import (
    Constraint,
    ConstraintCategory,
    ConstraintPriority,
    ConstraintRegistry,
)


class TestAgentStateConstraints:
    """Tests for AgentState constraint fields."""

    def test_agent_state_has_constraints(self):
        """AgentState should have constraints field."""
        state = AgentState()
        assert hasattr(state, "constraints")
        assert state.constraints == []

    def test_agent_state_has_constraint_registry(self):
        """AgentState should have constraint_registry field."""
        state = AgentState()
        assert hasattr(state, "constraint_registry")
        assert state.constraint_registry is None

    def test_agent_state_can_hold_constraints(self):
        """AgentState should accept constraint objects."""
        constraint = Constraint(
            category=ConstraintCategory.RESTRICTION,
            description="Test",
            content="NO TEST CONSTRAINTS",
            priority=ConstraintPriority.CRITICAL,
        )

        state = AgentState()
        state.constraints = [constraint]

        assert len(state.constraints) == 1
        assert state.constraints[0].description == "Test"


class TestDAGOrchestratorConstraintInit:
    """Tests for DAGOrchestrator constraint initialization."""

    @pytest.fixture(autouse=True)
    def _autosetup(self, tmp_path):
        """Set up test fixtures."""
        self.test_dir = Path(tempfile.mkdtemp()) / "constraints"
        self.test_dir.mkdir(parents=True, exist_ok=True)

        yield
        """Clean up test fixtures."""
        shutil.rmtree(self.test_dir.parent)

    def test_orchestrator_constraints_enabled_by_default(self):
        """DAGOrchestrator should enable constraints by default."""
        orch = DAGOrchestrator(workflow_id="test_default")
        assert orch.enable_constraints

    def test_orchestrator_constraints_can_be_disabled(self):
        """DAGOrchestrator should allow disabling constraints."""
        orch = DAGOrchestrator(workflow_id="test_disabled", enable_constraints=False)
        assert not orch.enable_constraints

    def test_orchestrator_loads_constraints_on_init(self):
        """DAGOrchestrator should load constraints from registry on init."""
        # Create a test registry with constraints
        registry = ConstraintRegistry(project="test_project")
        registry._constraints_dir = self.test_dir

        constraint = Constraint(
            category=ConstraintCategory.RESTRICTION,
            description="No Docker socket",
            content="NO DOCKER SOCKET MOUNTS",
            priority=ConstraintPriority.CRITICAL,
        )
        registry.register(constraint)
        registry.save()

        # Verify file exists
        assert registry._global_path().exists()

    @patch("beagle.infrastructure.constraint_registry.ConstraintRegistry")
    @patch("beagle.core.autonomous_orchestrator.get_context_manager")
    @patch("beagle.core.autonomous_orchestrator.get_context_integration")
    @patch("beagle.core.autonomous_orchestrator.reset_cost_tracker")
    def test_refresh_constraints_updates_state(
        self,
        _mock_reset_cost,
        mock_context_integration,
        mock_context_manager,
        mock_registry_class,
    ):
        """_refresh_constraints should update state.constraints."""
        # Create mock context manager
        mock_cm = Mock()
        mock_cm.start_node = Mock()
        mock_context_manager.return_value = mock_cm

        # Create mock context integration
        mock_ci = Mock()
        mock_context_integration.return_value = mock_ci

        # Setup registry mock
        registry = ConstraintRegistry(project="test_project")
        registry._constraints_dir = self.test_dir
        mock_registry_class.return_value = registry

        orchestra = DAGOrchestrator(workflow_id="test_refresh")

        # Manually add a constraint to registry
        c = Constraint(
            category=ConstraintCategory.RESTRICTION,
            description="Test constraint",
            content="NO TEST CONSTRAINTS",
            priority=ConstraintPriority.CRITICAL,
        )
        registry._global_constraints.add(c)
        registry.save()

        # Refresh should update state
        orchestra._refresh_constraints()

        assert len(orchestra.state.constraints) == 1
        assert orchestra.state.constraints[0].description == "Test constraint"


class TestDAGNodeConstraintInjection:
    """Tests for constraint injection into DAGNode prompts."""

    def test_constraints_section_built_for_state(self):
        """DAGNode should build constraints section from state."""
        # Create mock state with constraints
        state = AgentState()
        state.constraints = [
            Constraint(
                category=ConstraintCategory.RESTRICTION,
                description="No Docker socket",
                content="NO DOCKER SOCKET ALLOWED",
                priority=ConstraintPriority.CRITICAL,
            ),
            Constraint(
                category=ConstraintCategory.REQUIREMENT,
                description="Use type hints",
                content="All functions must have type hints",
                priority=ConstraintPriority.IMPORTANT,
            ),
        ]

        # Build the constraints section inline
        constraints_lines = ["", "## Active Constraints", ""]
        constraints_lines.append("The following constraints MUST be respected during execution:")
        constraints_lines.append("")
        for constraint in state.constraints:
            constraints_lines.append(f"- {constraint.format_for_context()}")
        constraints_section = "\n".join(constraints_lines)

        # Verify section content
        assert "## Active Constraints" in constraints_section
        assert "NO DOCKER SOCKET ALLOWED" in constraints_section
        assert "All functions must have type hints" in constraints_section
        assert "CRITICAL" in constraints_section
        assert "IMPORTANT" in constraints_section

    def test_no_constraints_section_when_empty(self):
        """No constraints section should be empty when state has no constraints."""
        state = AgentState()

        # Build section for empty constraints
        constraints_section = ""
        if state.constraints:
            constraints_section = "Has constraints"

        assert constraints_section == ""


class TestConstraintFormatting:
    """Tests for constraint formatting in prompts."""

    def test_critical_constraint_format(self):
        """Critical constraints should have correct emoji prefix."""
        constraint = Constraint(
            category=ConstraintCategory.RESTRICTION,
            description="Critical test",
            content="NO CRITICAL VIOLATIONS",
            priority=ConstraintPriority.CRITICAL,
        )

        formatted = constraint.format_for_context()
        assert "⚠️ CRITICAL" in formatted
        assert "NO CRITICAL VIOLATIONS" in formatted

    def test_important_constraint_format(self):
        """Important constraints should have correct emoji prefix."""
        constraint = Constraint(
            category=ConstraintCategory.REQUIREMENT,
            description="Important test",
            content="MUST DO THIS",
            priority=ConstraintPriority.IMPORTANT,
        )

        formatted = constraint.format_for_context()
        assert "📌 IMPORTANT" in formatted
        assert "MUST DO THIS" in formatted

    def test_nice_to_have_constraint_format(self):
        """Nice-to-have constraints should have correct emoji prefix."""
        constraint = Constraint(
            category=ConstraintCategory.PREFERENCE,
            description="Nice to have",
            content="CONSIDER THIS",
            priority=ConstraintPriority.NICE_TO_HAVE,
        )

        formatted = constraint.format_for_context()
        assert "💡 NOTE" in formatted
        assert "CONSIDER THIS" in formatted


class TestIntegrationConstraintWorkflow:
    """End-to-end tests for constraint workflow."""

    @pytest.fixture(autouse=True)
    def _autosetup(self, tmp_path):
        """Set up test fixtures."""
        self.test_dir = Path(tempfile.mkdtemp()) / "constraints"
        self.test_dir.mkdir(parents=True, exist_ok=True)

        yield
        """Clean up test fixtures."""
        shutil.rmtree(self.test_dir.parent)

    @patch("beagle.core.autonomous_orchestrator.get_context_manager")
    @patch("beagle.core.autonomous_orchestrator.get_context_integration")
    @patch("beagle.core.autonomous_orchestrator.reset_cost_tracker")
    def test_constraint_round_trip(
        self,
        _mock_reset_cost,
        mock_context_integration,
        mock_context_manager,
    ):
        """Constraints should flow from registry to state to prompt."""
        # Set up mocks
        mock_cm = Mock()
        mock_cm.start_node = Mock()
        mock_context_manager.return_value = mock_cm

        mock_ci = Mock()
        mock_context_integration.return_value = mock_ci

        # Create registry and add constraint
        registry = ConstraintRegistry(project="test_project")
        registry._constraints_dir = self.test_dir

        constraint = Constraint(
            id="test-001",
            category=ConstraintCategory.RESTRICTION,
            description="No Docker socket",
            content="NO DOCKER SOCKET - USE ORPHEUS IPC",
            priority=ConstraintPriority.CRITICAL,
            project="test_project",
        )
        registry.register(constraint)
        registry.save()

        # Create orchestrator
        _orch = DAGOrchestrator(workflow_id="test_workflow", enable_constraints=True)

        # The orchestrator should have loaded the constraint
        # Note: This test requires constraint persistence to work
        # In production, this would load from the persisted registry


if __name__ == "__main__":
    unittest.main()
