"""Structured output schemas for Beagle workflow results."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Finding:
    """A single actionable finding from a workflow run."""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    severity: str = "info"  # critical, high, medium, low, info
    category: str = "bug"  # bug, security, performance, style, architecture
    title: str = ""  # One-line summary
    description: str = ""  # Detailed explanation
    file_path: str | None = None  # Affected file
    line_start: int | None = None
    line_end: int | None = None
    suggested_fix: str | None = None  # Concrete fix suggestion
    confidence: float = 1.0  # 0.0-1.0 model confidence
    references: list[str] = field(default_factory=list)  # URLs, docs


@dataclass(frozen=True)
class OutputMetrics:
    """Aggregated metrics for the workflow output."""

    total_findings: int = 0
    by_severity: dict[str, int] = field(default_factory=dict)
    by_category: dict[str, int] = field(default_factory=dict)
    files_affected: int = 0
    total_cost_usd: float = 0.0
    total_tokens: int = 0
    duration_seconds: float = 0.0


@dataclass(frozen=True)
class WorkflowOutput:
    """Complete structured output from a workflow run."""

    workflow_id: str
    workflow_name: str
    query: str
    summary: str  # 2-3 sentence executive summary
    findings: list[Finding]
    metrics: OutputMetrics
    raw_report: str  # The original prose report
