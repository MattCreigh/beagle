"""Tests for Beagle Pre-Flight Cost Forecasting."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from beagle.preflight.estimator import (
    NodeEstimate,
    PreFlightEstimate,
    PreFlightEstimator,
)


def test_estimate_calculation_known_model():
    """Test estimate calculation with known model pricing."""
    estimator = PreFlightEstimator(budget_usd=10.0)

    # Mock node from graph
    node = MagicMock()
    node.name = "test_node"
    node.skill_name = "test_skill"
    node.model_override = "minimax-m3"

    estimate = estimator.estimate("test_wf", [node])

    assert estimate.workflow_name == "test_wf"
    assert len(estimate.nodes) == 1
    assert estimate.nodes[0].model == "minimax-m3"

    # pricing for minimax-m3: uses default pricing {"input": 1.0, "output": 4.0}
    # default tokens: 2000 in, 4000 out
    # cost = (2000/1M * 1.0) + (4000/1M * 4.0) = 0.002 + 0.016 = 0.018
    assert pytest.approx(estimate.total_estimated_cost_usd, 0.0001) == 0.018
    assert estimate.budget_sufficient is True


def test_missing_model_pricing_warning():
    """Test that missing model pricing produces a warning but doesn't crash."""
    estimator = PreFlightEstimator(budget_usd=10.0)

    # Use a model name that doesn't exist in MODEL_PRICING.
    # Must patch resolve_model to bypass the allowlist gate — the test is
    # exercising the *pricing* fallback path, not the allowlist boundary.
    node = {"name": "n1", "skill_name": "s1", "model": "non-existent-model"}

    with patch(
        "beagle.preflight.estimator.resolve_model",
        return_value="non-existent-model",
    ):
        estimate = estimator.estimate("test_wf", [node])

    assert estimate.total_estimated_cost_usd > 0
    assert any("no pricing data" in w for warning in estimate.warnings for w in [warning])
    # The warning is in estimate.warnings list


def test_budget_insufficient():
    """Test that budget_sufficient is False when estimate exceeds budget."""
    # Set a very low budget
    estimator = PreFlightEstimator(budget_usd=0.001)

    node = {"name": "n1", "skill_name": "s1", "model": "minimax-m3"}
    estimate = estimator.estimate("test_wf", [node])

    # cost is 0.018
    assert pytest.approx(estimate.total_estimated_cost_usd, 0.0001) == 0.018
    assert estimate.budget_sufficient is False


@patch("rich.prompt.Prompt.ask")
def test_display_preflight_check_proceed(mock_ask):
    """Test the display and confirmation logic (Proceed)."""
    from beagle.preflight.display import display_preflight_check

    mock_ask.return_value = "y"

    # Create a dummy estimate
    node = NodeEstimate(
        node_name="n1",
        skill_name="s1",
        model="m1",
        provider="p1",
        estimated_input_tokens=100,
        estimated_output_tokens=100,
        estimated_cost_usd=0.1,
        estimated_runtime_seconds=10,
        context_window=1000,
        estimated_utilisation_percent=20,
    )
    estimate = PreFlightEstimate(
        workflow_name="wf",
        node_count=1,
        nodes=[node],
        total_estimated_cost_usd=0.1,
        total_estimated_tokens=200,
        total_estimated_runtime_seconds=10,
        budget_usd=1.0,
        budget_sufficient=True,
        warnings=[],
    )

    choice = display_preflight_check(estimate)
    assert choice == "y"


def test_cli_integration_estimate_only():
    """Test that --estimate-only flag produces output and exits correctly."""
    from typer.testing import CliRunner

    from beagle.cli.cli import app

    runner = CliRunner()
    # Mock resolve_workflow, get_workflow_nodes, and display_preflight_check
    # v1.0.0 (F2 split): `run` moved from the cli.py monolith into
    # cli/commands/execution.py, which binds these three names at import.
    # Patch where they are *used* — patching beagle.cli.cli here now raises
    # AttributeError because the names no longer exist on that module.
    with (
        patch("beagle.cli.commands.execution._resolve_workflow") as mock_resolve,
        patch("beagle.cli.commands.execution.get_workflow_nodes") as mock_get_nodes,
        patch("beagle.cli.commands.execution.display_preflight_check") as mock_display,
    ):
        mock_resolve.return_value = Path("test_workflow.yaml")
        mock_get_nodes.return_value = [{"name": "n1", "skill_name": "s1"}]
        mock_display.return_value = "y"

        # Run workflow with --estimate
        result = runner.invoke(app, ["run", "test_workflow", "query", "--estimate"])

        # Typer raises SystemExit(0) which CliRunner returns as exit_code 0
        assert result.exit_code == 0
        mock_display.assert_called_once()


def test_estimator_with_dict_nodes():
    """Test that the estimator handles nodes passed as dictionaries."""
    estimator = PreFlightEstimator()
    nodes = [
        {"name": "planning", "agent": "research-planner"},
        {"name": "execution", "agent": "search-executor"},
    ]
    estimate = estimator.estimate("test", nodes)
    assert estimate.node_count == 2
    assert estimate.nodes[0].node_name == "planning"


def test_runtime_estimation():
    """Test that runtime estimation varies by model."""
    estimator = PreFlightEstimator()

    # Fast model: gemma4:31b (80 t/s) -> 6000/80 = 75s
    node_fast = {"name": "fast", "agent": "a1", "model": "gemma4:31b"}
    # Slow model: qwen3.5:397b (20 t/s) -> 6000/20 = 300s
    node_slow = {"name": "slow", "agent": "a2", "model": "qwen3.5:397b"}

    est_fast = estimator.estimate("wf", [node_fast])
    est_slow = estimator.estimate("wf", [node_slow])

    assert est_fast.total_estimated_runtime_seconds == 75.0
    assert est_slow.total_estimated_runtime_seconds == 300.0


def test_preflight_log_output(caplog):
    """Test the non-interactive log output.

    v1.0.9 (audit M5): log_preflight_estimate now routes through the logging
    module, emitting plain greppable text (no Rich ANSI highlighting) even in
    headless mode. Capture via caplog (log records, handler-independent).
    """
    import logging

    from beagle.preflight.display import log_preflight_estimate

    estimate = PreFlightEstimate(
        workflow_name="test_wf",
        node_count=1,
        nodes=[],
        total_estimated_cost_usd=0.5,
        total_estimated_tokens=1000,
        total_estimated_runtime_seconds=60,
        budget_usd=1.0,
        budget_sufficient=True,
        warnings=[],
    )

    with caplog.at_level(logging.INFO, logger="Beagle.preflight.display"):
        log_preflight_estimate(estimate)
    assert "[PREFLIGHT]" in caplog.text
    assert "test_wf" in caplog.text
    assert "$0.500" in caplog.text
