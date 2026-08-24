"""SP-5: tests for output/formatters + output/schema (was zero-coverage).

beagle-spotless-phase2, work package SP-5. The structured output formatters
(to_markdown / to_json / to_sarif / to_github_issues) and their schema types had
no direct tests. These exercise construction and each formatter contract.
"""

from __future__ import annotations

import json

from beagle.output.formatters import (
    to_github_issues,
    to_json,
    to_markdown,
    to_sarif,
)
from beagle.output.schema import Finding, OutputMetrics, WorkflowOutput


def _sample_output() -> WorkflowOutput:
    return WorkflowOutput(
        workflow_id="wf-1",
        workflow_name="security-audit",
        query="audit the auth module",
        summary="Found 2 issues.",
        findings=[
            Finding(
                title="SQL injection",
                severity="critical",
                category="security",
                description="unsafe query",
                file_path="src/auth.py",
                line_start=42,
                suggested_fix="use params",
            ),
            Finding(
                title="Naming",
                severity="low",
                category="style",
                description="bad name",
            ),
        ],
        metrics=OutputMetrics(
            total_findings=2,
            by_severity={"critical": 1, "low": 1},
            files_affected=1,
        ),
        raw_report="# raw",
    )


def test_schema_defaults() -> None:
    """Finding/OutputMetrics have safe defaults."""
    f = Finding()
    assert f.severity == "info"
    assert f.category == "bug"
    assert f.confidence == 1.0
    m = OutputMetrics()
    assert m.total_findings == 0


def test_to_markdown_contains_sections() -> None:
    """Markdown output has the report header and finding details."""
    md = to_markdown(_sample_output())
    assert "security-audit" in md
    assert "wf-1" in md
    assert "SQL injection" in md
    assert "CRITICAL" in md  # severity is uppercased in markdown
    assert "src/auth.py:42" in md
    assert "use params" in md  # suggested fix rendered as a code block


def test_to_markdown_location_without_line() -> None:
    """A finding with no line_start renders the file path without a line."""
    out = _sample_output()
    # Finding is frozen — build a new one with a file_path and no line.
    out.findings[1] = Finding(
        title="Naming",
        severity="low",
        category="style",
        description="bad name",
        file_path="src/other.py",
    )
    md = to_markdown(out)
    assert "src/other.py" in md


def test_to_json_is_valid_json() -> None:
    """JSON output round-trips and includes metrics."""
    payload = json.loads(to_json(_sample_output()))
    assert payload["workflow_id"] == "wf-1"
    assert payload["metrics"]["total_findings"] == 2


def test_to_github_issues_shape() -> None:
    """GitHub issues list has one entry per finding with title/body/labels."""
    issues = to_github_issues(_sample_output())
    assert len(issues) == 2
    first = issues[0]
    assert first["title"] == "[CRITICAL] SQL injection"
    assert "unsafe query" in first["body"]
    assert "src/auth.py" in first["body"]
    assert first["labels"] == ["beagle", "security", "critical"]


def test_to_sarif_returns_string() -> None:
    """SARIF output is a JSON string."""
    sarif = to_sarif(_sample_output())
    payload = json.loads(sarif)
    assert payload["version"] == "2.1.0"
    assert payload["runs"][0]["tool"]["driver"]["name"] == "Beagle"
