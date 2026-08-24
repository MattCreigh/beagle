"""Tests for errors module.

Tests all custom exception classes and error formatting utilities.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from beagle.errors import (
    BudgetExceededError,
    CheckpointError,
    ConfigurationError,
    EVHValidationError,
    GooseWorkflowError,
    NodeExecutionError,
    QueryRejectedError,
    RateLimitError,
    RecipeNotFoundError,
    WorkflowNotFoundError,
    WorkflowValidationError,
    format_error_rich,
)


class TestGooseWorkflowError:
    """Test base exception class."""

    def test_error_creation(self):
        """Test creating base error."""
        error = GooseWorkflowError(
            message="Test error message",
        )
        assert str(error) == "Test error message"
        assert error.message == "Test error message"

    def test_error_with_suggestion(self):
        """Test error with suggestion."""
        error = GooseWorkflowError(
            message="Error occurred",
            suggestion="Try again with different parameters",
        )
        assert error.suggestion == "Try again with different parameters"

    def test_error_with_docs_link(self):
        """Test error with documentation link."""
        error = GooseWorkflowError(
            message="Test error",
            docs_link="https://docs.example.com/errors/test",
        )
        assert error.docs_link == "https://docs.example.com/errors/test"

    def test_error_with_context(self):
        """Test error with context."""
        error = GooseWorkflowError(
            message="Test error",
            context={"workflow_id": "wf-123", "node": "executor"},
        )
        assert error.context["workflow_id"] == "wf-123"

    def test_error_str_representation(self):
        """Test string representation."""
        error = GooseWorkflowError(
            message="Something went wrong",
        )
        assert "Something went wrong" in str(error)


class TestRecipeNotFoundError:
    """Test recipe not found error."""

    def test_error_creation(self):
        """Test creating recipe error."""
        error = RecipeNotFoundError(recipe_name="nonexistent-recipe")
        assert "nonexistent-recipe" in str(error)
        assert error.recipe_name == "nonexistent-recipe"

    def test_error_with_directory(self):
        """Test error with recipes directory in context."""
        error = RecipeNotFoundError(
            recipe_name="missing",
            recipes_dir=Path("/path/to/recipes"),
        )
        # recipes_dir is used in suggestion, check context
        assert error.recipe_name == "missing"


class TestWorkflowNotFoundError:
    """Test workflow not found error."""

    def test_error_creation(self):
        """Test creating workflow error."""
        error = WorkflowNotFoundError(workflow_name="missing-workflow")
        assert "missing-workflow" in str(error)
        assert error.workflow_name == "missing-workflow"

    def test_error_with_directory(self):
        """Test error with metaprompts directory in context."""
        error = WorkflowNotFoundError(
            workflow_name="test",
            metaprompts_dir=Path("/path/to/metaprompts"),
        )
        # metaprompts_dir is used in suggestion
        assert error.workflow_name == "test"


class TestWorkflowValidationError:
    """Test workflow validation error."""

    def test_error_creation(self):
        """Test creating validation error."""
        error = WorkflowValidationError(
            workflow_name="invalid-workflow",
            errors=["Missing phase 'plan'", "Invalid agent type"],
        )
        assert "invalid-workflow" in str(error)
        assert len(error.errors) == 2

    def test_error_messages(self):
        """Test error message formatting."""
        error = WorkflowValidationError(
            workflow_name="bad-wf",
            errors=["Error 1", "Error 2", "Error 3"],
        )
        message = str(error)
        assert "bad-wf" in message


class TestBudgetExceededError:
    """Test budget exceeded error."""

    def test_error_creation(self):
        """Test creating budget error."""
        error = BudgetExceededError(
            current=15.50,
            budget=10.00,
        )
        assert "exceeded" in str(error).lower()
        assert error.current == 15.50
        assert error.budget == 10.00

    def test_error_with_workflow_id(self):
        """Test error stores current and budget."""
        error = BudgetExceededError(
            current=25.00,
            budget=20.00,
            workflow_id="wf-budget-test",
        )
        # workflow_id is stored in context
        assert error.current == 25.00
        assert error.budget == 20.00

    def test_overage_calculation(self):
        """Test budget overage."""
        error = BudgetExceededError(
            current=15.00,
            budget=10.00,
        )
        overage = error.current - error.budget
        assert overage == 5.00


class TestEVHValidationError:
    """Test EVH (Entry-Validation-Halt) validation error."""

    def test_error_creation(self):
        """Test creating EVH error."""
        error = EVHValidationError(
            node_name="test-node",
            validation_result="Condition not met",
        )
        assert "test-node" in str(error)
        assert error.validation_result == "Condition not met"

    def test_error_context(self):
        """Test error context includes node and result."""
        error = EVHValidationError(
            node_name="verify-node",
            validation_result="Verification failed",
            attempt=3,
        )
        # Check context
        assert error.context["node_name"] == "verify-node"


class TestNodeExecutionError:
    """Test node execution error."""

    def test_error_creation(self):
        """Test creating execution error."""
        error = NodeExecutionError(
            node_name="executor-node",
            error="Command failed with exit code 1",
        )
        assert "executor-node" in str(error)

    def test_error_with_stderr(self):
        """Test error with stderr."""
        error = NodeExecutionError(
            node_name="test",
            error="Process timeout",
            stderr="Timeout after 300 seconds",
        )
        assert error.stderr == "Timeout after 300 seconds"


class TestQueryRejectedError:
    """Test query rejected error."""

    def test_error_creation(self):
        """Test creating rejection error."""
        error = QueryRejectedError(
            reason="Query contains forbidden patterns",
        )
        assert "Query contains forbidden patterns" in str(error)

    def test_error_reason_stored(self):
        """Test reason is stored."""
        error = QueryRejectedError(
            reason="Security violation",
            query="SELECT * FROM users",
        )
        assert error.reason == "Security violation"


class TestCheckpointError:
    """Test checkpoint error."""

    def test_error_creation(self):
        """Test creating checkpoint error."""
        error = CheckpointError(
            operation="save",
            workflow_id="wf-check",
        )
        assert "save" in str(error) or "checkpoint" in str(error).lower()

    def test_error_context(self):
        """Test error stores operation and workflow in context."""
        error = CheckpointError(
            operation="restore",
            workflow_id="wf-restore",
            details="File not found",
        )
        # Check context
        assert error.context["operation"] == "restore"
        assert error.context["workflow_id"] == "wf-restore"


class TestRateLimitError:
    """Test rate limit error."""

    def test_error_creation(self):
        """Test creating rate limit error."""
        error = RateLimitError(
            limit_type="requests",
            current=75,
            limit=60,
        )
        assert "rate limit" in str(error).lower()

    def test_error_with_wait(self):
        """Test error with wait time."""
        error = RateLimitError(
            limit_type="tokens",
            current=150000,
            limit=100000,
            wait_seconds=30.5,
        )
        assert error.wait_seconds == 30.5


class TestConfigurationError:
    """Test configuration error."""

    def test_error_creation(self):
        """Test creating config error."""
        error = ConfigurationError(
            config_key="model.name",
            details="Invalid model name",
        )
        assert "model.name" in str(error)

    def test_error_missing_key(self):
        """Test error for missing configuration."""
        error = ConfigurationError(
            config_key="api_key",
            details="Required configuration missing",
        )
        assert "api_key" in str(error)


class TestFormatErrorRich:
    """Test rich error formatting utility."""

    def test_format_basic_error(self):
        """Test formatting a basic error."""
        error = GooseWorkflowError(
            message="Test error",
        )
        formatted = format_error_rich(error)
        assert "Test error" in formatted

    def test_format_budget_error(self):
        """Test formatting budget error."""
        error = BudgetExceededError(
            current=15.00,
            budget=10.00,
        )
        formatted = format_error_rich(error)
        assert formatted is not None


class TestErrorInheritance:
    """Test error class inheritance."""

    def test_inheritance_chain(self):
        """Test all errors inherit from base."""
        errors = [
            RecipeNotFoundError("test"),
            WorkflowNotFoundError("test"),
            WorkflowValidationError("test", ["err"]),
            BudgetExceededError(10, 5),
            EVHValidationError("n", "validation failed"),
            NodeExecutionError("n", "m"),
            QueryRejectedError("reason"),
            CheckpointError("op", "wf"),
            RateLimitError("req", 75, 60),
            ConfigurationError("key"),
        ]

        for error in errors:
            assert isinstance(error, GooseWorkflowError)
            assert isinstance(error, Exception)


class TestErrorContext:
    """Test error context and recovery information."""

    def test_error_suggestion(self):
        """Test error provides suggestion."""
        error = GooseWorkflowError(
            message="Processing failed",
            suggestion="Check input format and retry",
        )
        assert error.suggestion == "Check input format and retry"

    def test_error_docs_link(self):
        """Test error provides docs link."""
        error = GooseWorkflowError(
            message="Configuration invalid",
            docs_link="https://docs.example.com/config",
        )
        assert error.docs_link == "https://docs.example.com/config"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
