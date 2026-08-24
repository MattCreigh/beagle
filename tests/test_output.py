"""Tests for Output Formatters.

Tests for markdown, JSON, SARIF, and GitHub issue formatters.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from beagle.output.formatters import to_json, to_markdown, to_sarif
from beagle.output.parser import OutputParser
from beagle.output.schema import Finding, WorkflowOutput

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from beagle.output.formatters import (  # ruff: ignore[E402]
    to_github_issues,
)
from beagle.output.schema import (  # ruff: ignore[E402]
    OutputMetrics,
)


class TestFinding:
    """Test Finding model."""

    def test_finding_creation_minimal(self):
        """Finding can be created with minimal fields."""
        finding = Finding(
            title="Test finding",
            description="Test description",
            severity="info",
            category="style",
        )
        assert finding.title == "Test finding"
        assert finding.description == "Test description"
        assert finding.severity == "info"
        assert finding.category == "style"

    def test_finding_creation_full(self):
        """Finding can be created with all fields."""
        finding = Finding(
            title="Security issue",
            description="Found a security vulnerability",
            severity="high",
            category="security",
            file_path="/path/to/file.py",
            line_start=42,
            line_end=45,
            suggested_fix="Use parameterized queries",
        )
        assert finding.severity == "high"
        assert finding.file_path == "/path/to/file.py"
        assert finding.line_start == 42

    def test_finding_defaults(self):
        """Finding uses correct defaults."""
        finding = Finding(title="Test", description="Desc", severity="low", category="bug")
        assert finding.severity == "low"
        assert finding.category == "bug"
        assert finding.file_path is None


class TestOutputMetrics:
    """Test OutputMetrics model."""

    def test_metrics_creation(self):
        """OutputMetrics can be created."""
        metrics = OutputMetrics(
            total_findings=10,
            files_affected=5,
            by_severity={"high": 2, "medium": 3, "low": 5},
        )
        assert metrics.total_findings == 10
        assert metrics.files_affected == 5
        assert metrics.by_severity["high"] == 2


class TestWorkflowOutput:
    """Test WorkflowOutput model."""

    def test_workflow_output_creation(self):
        """WorkflowOutput can be created."""
        output = WorkflowOutput(
            workflow_name="test_workflow",
            workflow_id="test-123",
            query="Test query",
            summary="Test summary",
            findings=[],
            metrics=OutputMetrics(total_findings=0, files_affected=0, by_severity={}),
            raw_report="Full report text",
        )
        assert output.workflow_name == "test_workflow"
        assert output.workflow_id == "test-123"
        assert output.raw_report == "Full report text"

    def test_workflow_output_with_findings(self):
        """WorkflowOutput can contain findings."""
        finding = Finding(
            title="Test",
            description="Desc",
            severity="medium",
            category="bug",
        )
        output = WorkflowOutput(
            workflow_name="test",
            workflow_id="id",
            query="q",
            summary="s",
            findings=[finding],
            metrics=OutputMetrics(
                total_findings=1,
                files_affected=1,
                by_severity={"medium": 1},
            ),
            raw_report="Report",
        )
        assert len(output.findings) == 1
        assert output.findings[0].severity == "medium"


class TestToMarkdown:
    """Test to_markdown formatter."""

    def test_to_markdown_empty(self):
        """to_markdown works with empty findings."""
        output = WorkflowOutput(
            workflow_name="test",
            workflow_id="123",
            query="Test query",
            summary="Empty summary",
            findings=[],
            metrics=OutputMetrics(total_findings=0, files_affected=0, by_severity={}),
            raw_report="",
        )
        md = to_markdown(output)
        assert "# Beagle Analysis Report: test" in md
        assert "Test query" in md
        assert "Total Findings:** 0" in md

    def test_to_markdown_with_findings(self):
        """to_markdown includes findings."""
        finding = Finding(
            title="Issue found",
            description="Something is wrong",
            severity="high",
            category="security",
        )
        output = WorkflowOutput(
            workflow_name="security_scan",
            workflow_id="sec-123",
            query="Scan for vulnerabilities",
            summary="Found 1 security issue",
            findings=[finding],
            metrics=OutputMetrics(
                total_findings=1,
                files_affected=1,
                by_severity={"high": 1},
            ),
            raw_report="Full security report",
        )
        md = to_markdown(output)
        assert "### 1. Issue found" in md
        assert "HIGH" in md
        assert "security" in md


class TestToJson:
    """Test to_json formatter."""

    def test_to_json_valid(self):
        """to_json produces valid JSON."""
        output = WorkflowOutput(
            workflow_name="test",
            workflow_id="123",
            query="q",
            summary="s",
            findings=[],
            metrics=OutputMetrics(total_findings=0, files_affected=0, by_severity={}),
            raw_report="",
        )
        json_str = to_json(output)
        parsed = json.loads(json_str)
        assert parsed["workflow_name"] == "test"

    def test_to_json_structure(self):
        """to_json includes all fields."""
        finding = Finding(title="T", description="D", severity="low", category="bug")
        output = WorkflowOutput(
            workflow_name="test",
            workflow_id="id",
            query="query",
            summary="summary",
            findings=[finding],
            metrics=OutputMetrics(total_findings=1, files_affected=1, by_severity={"info": 1}),
            raw_report="raw",
        )
        parsed = json.loads(to_json(output))
        assert parsed["workflow_name"] == "test"
        assert len(parsed["findings"]) == 1


class TestToGitHubIssues:
    """Test to_github_issues formatter."""

    def test_to_github_issues_empty(self):
        """to_github_issues returns empty list for no findings."""
        output = WorkflowOutput(
            workflow_name="test",
            workflow_id="123",
            query="q",
            summary="s",
            findings=[],
            metrics=OutputMetrics(total_findings=0, files_affected=0, by_severity={}),
            raw_report="",
        )
        issues = to_github_issues(output)
        assert issues == []

    def test_to_github_issues_with_findings(self):
        """to_github_issues creates issue dicts."""
        finding = Finding(
            title="Bug found",
            description="There is a bug",
            severity="high",
            category="bug",
            file_path="src/main.py",
            line_start=10,
            suggested_fix="Fix the bug",
        )
        output = WorkflowOutput(
            workflow_name="test",
            workflow_id="id",
            query="q",
            summary="s",
            findings=[finding],
            metrics=OutputMetrics(total_findings=1, files_affected=1, by_severity={"high": 1}),
            raw_report="",
        )
        issues = to_github_issues(output)
        assert len(issues) == 1
        assert "[HIGH] Bug found" in issues[0]["title"]
        assert "src/main.py" in issues[0]["body"]
        assert "10" in issues[0]["body"]

    def test_to_github_issues_multiple(self):
        """to_github_issues creates multiple issues."""
        findings = [
            Finding(title="Issue 1", description="D1", severity="high", category="bug"),
            Finding(title="Issue 2", description="D2", severity="low", category="style"),
        ]
        output = WorkflowOutput(
            workflow_name="test",
            workflow_id="id",
            query="q",
            summary="s",
            findings=findings,
            metrics=OutputMetrics(
                total_findings=2,
                files_affected=2,
                by_severity={"high": 1, "low": 1},
            ),
            raw_report="",
        )
        issues = to_github_issues(output)
        assert len(issues) == 2


class TestFormattersIntegration:
    """Integration tests for formatters."""

    def test_full_workflow_output(self):
        """Test complete workflow output formatting."""
        findings = [
            Finding(
                title="SQL Injection",
                description="User input not sanitized",
                severity="critical",
                category="security",
                file_path="db/queries.py",
                line_start=23,
                suggested_fix="Use parameterized queries",
            ),
            Finding(
                title="Memory Leak",
                description="Connection not closed",
                severity="medium",
                category="performance",
                file_path="services/api.py",
                line_start=45,
            ),
        ]

        output = WorkflowOutput(
            workflow_name="code_audit",
            workflow_id="audit-001",
            query="Audit the codebase",
            summary="Found 2 issues",
            findings=findings,
            metrics=OutputMetrics(
                total_findings=2,
                files_affected=2,
                by_severity={"critical": 1, "medium": 1},
            ),
            raw_report="Full audit report with details...",
        )

        # Test markdown
        md = to_markdown(output)
        assert "SQL Injection" in md
        assert "Memory Leak" in md
        assert "critical" in md.lower() or "CRITICAL" in md

        # Test JSON
        json_out = to_json(output)
        parsed = json.loads(json_out)
        assert len(parsed["findings"]) == 2

        # Test GitHub issues
        issues = to_github_issues(output)
        assert len(issues) == 2
        assert "[CRITICAL]" in issues[0]["title"]


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])


# ── Merged from test_output_inner.py (v1.0.0 consolidation) ──────────
def test_json_block_extraction():
    """Test JSON extraction from a mock <final_answer> containing a JSON block."""
    raw = """
Executive summary: We found some issues.

```json
{
  "summary": "Short summary",
  "findings": [
    {
      "severity": "high",
      "category": "security",
      "title": "SQL Injection",
      "description": "Found in main.py",
      "file_path": "main.py",
      "line_start": 10
    }
  ]
}
```
"""
    parser = OutputParser("w1", "test", "q")
    # Strategy 1 should work
    data = parser._extract_json_block(raw)
    assert data is not None
    assert data["summary"] == "Short summary"
    assert len(data["findings"]) == 1


@pytest.mark.asyncio
async def test_llm_assisted_extraction_fallback():
    """Test fallback to LLM-assisted extraction when JSON block is missing."""
    raw = "Prose report without any JSON blocks."

    parser = OutputParser("w1", "test", "q")

    mock_json = {
        "summary": "Extracted summary",
        "findings": [{"title": "F1", "severity": "info"}],
    }

    with patch.object(parser, "_llm_assisted_extraction", return_value=mock_json):
        output = await parser.parse(raw)
        assert output.summary == "Extracted summary"
        assert len(output.findings) == 1
        assert output.findings[0].title == "F1"


@pytest.mark.asyncio
async def test_graceful_degradation_all_fail():
    """Test graceful degradation when both strategies fail."""
    raw = "Totally unparseable garbage."

    parser = OutputParser("w1", "test", "q")

    with (
        patch.object(parser, "_extract_json_block", return_value=None),
        patch.object(parser, "_llm_assisted_extraction", return_value=None),
    ):
        output = await parser.parse(raw)
        assert "Automated parsing failed" in output.summary
        assert len(output.findings) == 1
        assert output.findings[0].severity == "info"


def test_markdown_formatter():
    """Test markdown formatter produces valid markdown with correct sections."""
    from beagle.output.schema import OutputMetrics

    finding = Finding(title="Test Bug", severity="high", description="Detail")
    output = WorkflowOutput(
        workflow_id="w1",
        workflow_name="wf",
        query="q",
        summary="Summary text",
        findings=[finding],
        metrics=OutputMetrics(total_findings=1, by_severity={"high": 1}),
        raw_report="Raw",
    )

    md = to_markdown(output)
    assert "# Beagle Analysis Report" in md
    assert "## Executive Summary" in md
    assert "Summary text" in md
    assert "### 1. Test Bug" in md


def test_json_formatter():
    """Test JSON formatter produces valid JSON matching the schema."""
    from beagle.output.schema import OutputMetrics

    finding = Finding(title="F1", severity="low", category="style")
    output = WorkflowOutput(
        workflow_id="w1",
        workflow_name="wf",
        query="q",
        summary="S",
        findings=[finding],
        metrics=OutputMetrics(total_findings=1),
        raw_report="R",
    )

    js = to_json(output)
    data = json.loads(js)
    assert data["workflow_id"] == "w1"
    assert len(data["findings"]) == 1
    assert data["findings"][0]["title"] == "F1"


def test_sarif_formatter_validity():
    """Test SARIF formatter produces valid SARIF v2.1 structure."""
    from beagle.output.schema import OutputMetrics

    finding = Finding(
        title="SQLi",
        severity="critical",
        category="security",
        file_path="db.py",
        line_start=5,
        description="Danger",
    )
    output = WorkflowOutput(
        workflow_id="w1",
        workflow_name="wf",
        query="q",
        summary="S",
        findings=[finding],
        metrics=OutputMetrics(total_findings=1),
        raw_report="R",
    )

    sarif_str = to_sarif(output)
    data = json.loads(sarif_str)

    assert data["version"] == "2.1.0"
    assert len(data["runs"]) == 1
    result = data["runs"][0]["results"][0]
    assert result["level"] == "error"
    assert "db.py" in result["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]


def test_severity_validation():
    """Test that build_output handles severity and category normalization."""
    parser = OutputParser("w1", "test", "q")

    data = {"findings": [{"severity": "CRITICAL", "category": "SECURITY", "title": "T1"}]}

    output = parser._build_output(data, "raw")
    assert output.findings[0].severity == "critical"
    assert output.findings[0].category == "security"


def test_metrics_calculation():
    """Test that metrics are correctly calculated during output building."""
    parser = OutputParser("w1", "test", "q")

    data = {
        "findings": [
            {"severity": "high", "category": "bug", "file_path": "a.py"},
            {"severity": "high", "category": "security", "file_path": "a.py"},
            {"severity": "low", "category": "style", "file_path": "b.py"},
        ]
    }

    output = parser._build_output(data, "raw")
    assert output.metrics.total_findings == 3
    assert output.metrics.by_severity["high"] == 2
    assert output.metrics.files_affected == 2
