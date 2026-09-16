"""Regression: ``beagle run --headless`` must not exit 0 on a workflow error.

C-01 of the 2026-09-16 sweep. The headless branch printed
"Workflow completed with errors" and then fell through to a 0 exit, so a CI/CD
pipeline read a failed run as green. The check is on the process exit code,
which is the only signal a pipeline consumes.
"""

from __future__ import annotations

from typing import Any

import pytest
from typer.testing import CliRunner

from beagle.cli.commands import execution


@pytest.fixture
def _stub_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the workflow driver with one that reports a node error."""

    async def _fake_run_workflow(**_kwargs: Any) -> dict[str, Any]:
        return {
            "errors": ["node hydration failed: boom"],
            "total_tokens": 0,
            "total_cost": 0.0,
            "completed_nodes": [],
            "final_report": "",
            "workflow_id": "wf-test",
        }

    monkeypatch.setattr(execution, "run_workflow", _fake_run_workflow)


def test_headless_exits_nonzero_on_workflow_errors(_stub_run: None) -> None:
    """A node error under --headless must produce a non-zero exit code."""
    runner = CliRunner()
    result = runner.invoke(
        execution.execution_app,
        ["run", "research", "a query", "--headless", "--skip-preflight"],
    )

    assert result.exit_code != 0, (
        "headless run with workflow errors exited 0 — a CI/CD pipeline would "
        f"treat the failed run as green. exit={result.exit_code} "
        f"output={result.stdout!r}"
    )
    assert "Workflow completed with errors" in result.stdout


def test_headless_exits_zero_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fix must not invert the signal: a clean run still exits 0."""

    async def _ok_run_workflow(**_kwargs: Any) -> dict[str, Any]:
        return {
            "errors": [],
            "total_tokens": 0,
            "total_cost": 0.0,
            "completed_nodes": [],
            "final_report": "",
            "workflow_id": "wf-ok",
        }

    monkeypatch.setattr(execution, "run_workflow", _ok_run_workflow)

    runner = CliRunner()
    result = runner.invoke(
        execution.execution_app,
        ["run", "research", "a query", "--headless", "--skip-preflight"],
    )

    assert result.exit_code == 0, (
        f"clean headless run must exit 0, got {result.exit_code}: {result.stdout!r}"
    )
    assert "SUCCESS" in result.stdout
