"""Tests for the Beagle Terminal Dashboard (TUI)."""

import asyncio

import pytest

from beagle.events import (
    NodeCompleted,
    NodeOutput,
    NodeStarted,
    WorkflowStarted,
    get_event_bus,
)
from beagle.tui.app import BeagleApp


@pytest.mark.asyncio
async def test_tui_initialization():
    """Test that the dashboard initializes correctly."""
    app = BeagleApp(workflow_id="test_wf", query="test query")
    async with app.run_test():
        assert app.workflow_id == "test_wf"
        assert app.query == "test query"
        assert app.title == "Beagle: test_wf"


@pytest.mark.asyncio
async def test_tui_event_handling():
    """Test that the TUI responds to bus events."""
    app = BeagleApp(workflow_id="test_wf", query="test query")
    async with app.run_test():
        bus = get_event_bus()

        # 1. Workflow Started (sets budget)
        bus.publish(WorkflowStarted(workflow_id="test_wf", query="test", budget_usd=50.0))
        await asyncio.sleep(0.1)
        assert app.budget == 50.0

        # 2. Node Started
        bus.publish(NodeStarted(workflow_id="test_wf", node_name="planning", model="m1"))
        await asyncio.sleep(0.1)
        # Check DAGStatus widget state via message processing
        dag_status = app.query_one("DAGStatus")
        assert dag_status.nodes["planning"] == "running"

        # 3. Node Output
        bus.publish(
            NodeOutput(workflow_id="test_wf", node_name="planning", content="Starting plan...")
        )
        await asyncio.sleep(0.1)
        # RichLog is harder to inspect directly in run_test without complex query,
        # but we verified the message handler was called via code coverage usually.

        # 4. Node Completed (updates metrics)
        bus.publish(
            NodeCompleted(workflow_id="test_wf", node_name="planning", cost=0.5, tokens=100)
        )
        await asyncio.sleep(0.1)
        assert app.total_cost == 0.5
        assert app.total_tokens == 100
        assert dag_status.nodes["planning"] == "completed"


@pytest.mark.asyncio
async def test_tui_workflow_id_filtering():
    """Test that the TUI ignores events from other workflows."""
    app = BeagleApp(workflow_id="my_wf", query="test")
    async with app.run_test():
        bus = get_event_bus()

        # Event for different workflow
        bus.publish(NodeStarted(workflow_id="other_wf", node_name="other_node"))
        await asyncio.sleep(0.1)

        dag_status = app.query_one("DAGStatus")
        assert "other_node" not in dag_status.nodes


def test_cli_flags_mutual_exclusion():
    """Verify CLI error handling for mutually exclusive flags."""
    from typer.testing import CliRunner

    from beagle.cli.cli import app

    runner = CliRunner()
    # Note: we don't actually run it because it would trigger workflow logic,
    # but we test the logic we added to cli.py
    result = runner.invoke(app, ["run", "research", "query", "--tui", "--headless"])
    assert result.exit_code != 0
    assert "--tui and --headless are mutually exclusive" in result.stdout


@pytest.mark.asyncio
async def test_tui_exit_unsubscribes():
    """Test that the TUI cleans up its subscription on exit."""
    bus = get_event_bus()
    app = BeagleApp(workflow_id="test", query="test")

    async with app.run_test() as pilot:
        sub_id = app.sub_id
        assert sub_id in bus._subscribers
        await pilot.press("q")

    assert sub_id not in bus._subscribers


@pytest.mark.asyncio
async def test_tui_metric_clamping():
    """Test that progress bars handle 100%+ values gracefully."""
    app = BeagleApp(workflow_id="test", query="test")
    async with app.run_test():
        bus = get_event_bus()

        # Set small budget
        bus.publish(WorkflowStarted(workflow_id="test", query="q", budget_usd=1.0))
        # Exceed it
        bus.publish(NodeCompleted(workflow_id="test", node_name="n", cost=2.0, tokens=10))
        await asyncio.sleep(0.1)

        progress = app.query_one("#budget-progress")
        assert progress.progress == 100  # Clamped at 100
