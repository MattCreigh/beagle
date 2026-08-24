"""Formatters for rendering WorkflowOutput in various formats."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from .sarif import to_sarif as _render_sarif
from .schema import WorkflowOutput


def to_markdown(output: WorkflowOutput) -> str:
    """Render as a structured markdown report."""
    lines = [
        f"# Beagle Analysis Report: {output.workflow_name}",
        "",
        f"**Run ID:** `{output.workflow_id}`",
        f"**Query:** {output.query}",
        "",
        "## Executive Summary",
        output.summary,
        "",
        "## Findings Overview",
        f"- **Total Findings:** {output.metrics.total_findings}",
        f"- **Files Affected:** {output.metrics.files_affected}",
        "",
        "### Severity Breakdown",
    ]

    for sev, count in sorted(output.metrics.by_severity.items()):
        lines.append(f"- **{sev.capitalize()}:** {count}")

    lines.extend(["", "## Detailed Findings", ""])

    for i, f in enumerate(output.findings):
        lines.append(f"### {i + 1}. {f.title}")
        lines.append(f"- **Severity:** {f.severity.upper()}")
        lines.append(f"- **Category:** {f.category}")
        if f.file_path:
            loc = f"{f.file_path}"
            if f.line_start:
                loc += f":{f.line_start}"
            lines.append(f"- **Location:** `{loc}`")
        lines.append("")
        lines.append(f"{f.description}")
        if f.suggested_fix:
            lines.append("")
            lines.append("**Suggested Fix:**")
            lines.append(f"```\n{f.suggested_fix}\n```")
        lines.append("---")

    return "\n".join(lines)


def to_json(output: WorkflowOutput) -> str:
    """Render as pretty-printed JSON."""
    return json.dumps(asdict(output), indent=2)


def to_sarif(output: WorkflowOutput) -> str:
    """Render as SARIF v2.1."""
    return _render_sarif(output)


def to_github_issues(output: WorkflowOutput) -> list[dict[str, Any]]:
    """Render as a list of GitHub issue bodies, one per finding."""
    issues = []
    for f in output.findings:
        body = f"{f.description}\n\n"
        if f.file_path:
            body += f"**File:** {f.file_path}\n"
        if f.line_start:
            body += f"**Line:** {f.line_start}\n"
        if f.suggested_fix:
            body += f"\n### Suggested Fix\n{f.suggested_fix}\n"

        issues.append(
            {
                "title": f"[{f.severity.upper()}] {f.title}",
                "body": body,
                "labels": ["beagle", f.category, f.severity],
            }
        )
    return issues
