"""Beagle Structured Output module."""

from .formatters import to_github_issues, to_json, to_markdown, to_sarif
from .parser import OutputParser
from .schema import Finding, OutputMetrics, WorkflowOutput

__all__ = [
    "Finding",
    "OutputMetrics",
    "OutputParser",
    "WorkflowOutput",
    "to_github_issues",
    "to_json",
    "to_markdown",
    "to_sarif",
]
