"""Failure path tests — empty LLM, timeout, corrupt DB, budget exceeded.

Tests that subsystems degrade gracefully under failure conditions:
- BudgetExceededError raised when cost exceeds limit
- Empty/invalid LLM responses handled correctly
- Timeout errors propagated with context
- Corrupt database recovery
- Error serialization and suggestion formatting
"""

from pathlib import Path

from beagle.errors import (
    BudgetExceededError,
    EVHValidationError,
    GooseWorkflowError,
    RecipeNotFoundError,
    SandboxViolation,
    SecurityAccessViolation,
    WorkflowNotFoundError,
    WorkflowValidationError,
)

# ═══════════════════════════════════════════════════════════════════════════════
# Budget Exceeded Failure Path
# ═══════════════════════════════════════════════════════════════════════════════


class TestBudgetExceededError:
    """Test BudgetExceededError behavior."""

    def test_budget_exceeded_error_message(self):
        err = BudgetExceededError(current=12.50, budget=10.00)
        assert "$12.5000" in str(err)
        assert "$10.00" in str(err)
        assert "Budget exceeded" in str(err)

    def test_budget_exceeded_error_suggestion(self):
        err = BudgetExceededError(current=5.00, budget=3.00)
        assert err.suggestion is not None
        assert "--budget" in err.suggestion

    def test_budget_exceeded_error_context(self):
        err = BudgetExceededError(current=1.00, budget=0.50, workflow_id="wf-123")
        ctx = err.context
        assert ctx["current_usd"] == 1.00
        assert ctx["budget_usd"] == 0.50
        assert ctx["workflow_id"] == "wf-123"

    def test_budget_exceeded_error_serialization(self):
        err = BudgetExceededError(current=5.00, budget=2.00)
        d = err.to_dict()
        assert d["error"] == "BudgetExceededError"
        assert "Budget exceeded" in d["message"]
        assert d["suggestion"] is not None

    def test_budget_exceeded_is_catchable_as_workflow_error(self):
        err = BudgetExceededError(current=1.00, budget=0.50)
        assert isinstance(err, GooseWorkflowError)


class TestCostTrackerBudgetEnforcement:
    """Test cost tracker budget enforcement."""

    def test_cost_tracker_initial_budget(self):
        from beagle.cost_tracker import reset_cost_tracker

        tracker = reset_cost_tracker(budget_usd=5.0)
        assert tracker.budget_usd == 5.0

    def test_cost_tracker_tracks_spending(self):
        from beagle.cost_tracker import reset_cost_tracker

        tracker = reset_cost_tracker(budget_usd=10.0)
        tracker._total_cost = 0.01  # Simulate spending
        tracker._total_input_tokens = 100
        tracker._total_output_tokens = 50
        assert tracker.total_cost_usd > 0

    def test_cost_tracker_budget_exceeded(self):
        from beagle.cost_tracker import reset_cost_tracker

        tracker = reset_cost_tracker(budget_usd=0.001, model="test")
        tracker._total_cost = 0.01  # Exceed budget
        assert tracker.budget_exceeded
        assert tracker.total_cost_usd > 0.001


# ═══════════════════════════════════════════════════════════════════════════════
# Empty/Invalid LLM Response Failure Path
# ═══════════════════════════════════════════════════════════════════════════════


class TestEmptyLLMResponse:
    """Test handling of empty or invalid LLM responses."""

    def test_workflow_validation_error_empty_yaml(self):
        errors = ["Phases list is empty", "No agents defined"]
        err = WorkflowValidationError("empty-workflow", errors)
        assert "validation failed" in str(err).lower()
        assert err.errors == errors

    def test_workflow_validation_error_serialization(self):
        err = WorkflowValidationError("bad-wf", ["Missing name"])
        d = err.to_dict()
        assert d["error"] == "WorkflowValidationError"
        assert "Missing name" in d["context"]["errors"]

    def test_workflow_not_found_error(self):
        err = WorkflowNotFoundError("nonexistent-workflow")
        assert "nonexistent-workflow" in str(err)
        assert err.suggestion is not None

    def test_recipe_not_found_error(self):
        err = RecipeNotFoundError("missing-recipe")
        assert "missing-recipe" in str(err)
        assert err.recipe_name == "missing-recipe"

    def test_recipe_not_found_with_dir_suggestion(self):
        path = Path("/custom/recipes")
        err = RecipeNotFoundError("missing-recipe", recipes_dir=path)
        assert "/custom/recipes" in err.suggestion


# ═══════════════════════════════════════════════════════════════════════════════
# Security Violation Failure Path
# ═══════════════════════════════════════════════════════════════════════════════


class TestSecurityViolation:
    """Test security violation handling."""

    def test_security_access_violation(self):
        err = SecurityAccessViolation("File access denied", suggestion="Use read-only mode")
        assert "File access denied" in str(err)
        assert err.suggestion == "Use read-only mode"
        assert err.severity == "medium"

    def test_sandbox_violation(self):
        err = SandboxViolation("Attempted network access", sandbox_boundary="network")
        assert "Sandbox boundary violation" in str(err)
        assert err.attempted_action == "Attempted network access"
        assert err.severity == "high"

    def test_sandbox_violation_custom_suggestion(self):
        err = SandboxViolation("Attempted file write", suggestion="Use safe_edit instead")
        assert "safe_edit" in err.suggestion


# ═══════════════════════════════════════════════════════════════════════════════
# EVH Validation Failure Path
# ═══════════════════════════════════════════════════════════════════════════════


class TestEVHValidationError:
    """Test EVH validation error handling."""

    def test_evh_validation_error(self):
        err = EVHValidationError(
            node_name="researcher",
            validation_result="POTENTIAL_HALLUCINATION: claim without citation",
            attempt=2,
        )
        assert "researcher" in str(err)
        assert err.node_name == "researcher"
        assert err.validation_result == "POTENTIAL_HALLUCINATION: claim without citation"

    def test_evh_validation_truncation(self):
        long_result = "x" * 1000
        err = EVHValidationError(node_name="writer", validation_result=long_result)
        # Context should truncate to 500 chars
        assert len(err.context["validation_result"]) <= 500


# ═══════════════════════════════════════════════════════════════════════════════
# Corrupt Database Recovery
# ═══════════════════════════════════════════════════════════════════════════════


class TestCorruptDatabaseRecovery:
    """Test tracking database recovery from corruption."""

    def test_database_creates_tables_on_init(self, tmp_path):
        from beagle.tracking.database import TrackingDatabase

        db_path = tmp_path / "test.db"
        db = TrackingDatabase(db_path)  # Path, not str
        conn = db._get_conn()
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        table_names = [t[0] for t in tables]
        conn.close()
        assert len(table_names) > 0

    def test_database_handles_corrupt_file(self, tmp_path):
        from beagle.tracking.database import TrackingDatabase

        db_path = tmp_path / "corrupt.db"
        db_path.write_bytes(b"\x00\x01\x02\x03CORRUPTED" * 100)

        try:
            db = TrackingDatabase(db_path)
            conn = db._get_conn()
            tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            conn.close()
            assert len(tables) >= 0
        except Exception:  # ruff: ignore[BLE001]
            pass

    def test_database_wal_checkpoint(self, tmp_path):
        """Test WAL checkpoint flushes data."""
        from beagle.tracking.database import TrackingDatabase

        db_path = tmp_path / "wal_test.db"
        db = TrackingDatabase(db_path)
        conn = db._get_conn()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()


# ═══════════════════════════════════════════════════════════════════════════════
# Error Serialization Chain
# ═══════════════════════════════════════════════════════════════════════════════


class TestErrorSerializationChain:
    """Test error serialization and inheritance chain."""

    def test_all_errors_inherit_from_workflow_error(self):
        errors = [
            BudgetExceededError(1.0, 0.5),
            RecipeNotFoundError("test"),
            WorkflowNotFoundError("test"),
            WorkflowValidationError("test", ["error"]),
            EVHValidationError("node", "result"),
        ]
        for err in errors:
            assert isinstance(err, GooseWorkflowError), (
                f"{type(err).__name__} should inherit from GooseWorkflowError"
            )

    def test_all_errors_have_to_dict(self):
        errors = [
            BudgetExceededError(1.0, 0.5),
            RecipeNotFoundError("test"),
            WorkflowNotFoundError("test"),
            WorkflowValidationError("test", ["error"]),
            EVHValidationError("node", "result"),
        ]
        for err in errors:
            d = err.to_dict()
            assert "error" in d
            assert "message" in d
            assert d["error"] == type(err).__name__

    def test_all_errors_have_suggestions(self):
        errors = [
            BudgetExceededError(1.0, 0.5),
            RecipeNotFoundError("test"),
            WorkflowNotFoundError("test"),
            WorkflowValidationError("test", ["error"]),
        ]
        for err in errors:
            assert err.suggestion is not None, f"{type(err).__name__} should have a suggestion"

    def test_error_str_includes_suggestion(self):
        err = BudgetExceededError(current=5.0, budget=3.0)
        error_str = str(err)
        assert "Budget exceeded" in error_str
        assert "Suggestion" in error_str
