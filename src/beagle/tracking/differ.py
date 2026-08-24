"""Cross-run comparison logic for findings and metrics."""

from __future__ import annotations

from dataclasses import dataclass, field

from .database import TrackingDatabase
from .models import Finding


@dataclass
class RunDiff:
    """Differences between two workflow runs."""

    run_id_a: str
    run_id_b: str
    new_findings: list[Finding] = field(default_factory=list)
    resolved_findings: list[Finding] = field(default_factory=list)
    persistent_findings: list[Finding] = field(default_factory=list)
    cost_delta_usd: float = 0.0
    token_delta: int = 0
    duration_delta_seconds: float = 0.0


class RunDiffer:
    """Compares two workflow runs to find changes."""

    def __init__(self, db: TrackingDatabase | None = None):
        self.db = db or TrackingDatabase.get_instance()

    def compare(self, run_id_a: str, run_id_b: str) -> RunDiff:
        """Compare run A (baseline) to run B (current)."""
        findings_a = self.db.get_findings_for_run(run_id_a)
        findings_b = self.db.get_findings_for_run(run_id_b)

        # Helper to create key for deduplication
        def get_key(f: Finding) -> tuple:
            return (f.file_path, f.line_number, f.title)

        keys_a = {get_key(f): f for f in findings_a}
        keys_b = {get_key(f): f for f in findings_b}

        diff = RunDiff(run_id_a=run_id_a, run_id_b=run_id_b)

        # New findings: in B but not in A
        for key, finding in keys_b.items():
            if key not in keys_a:
                diff.new_findings.append(finding)
            else:
                diff.persistent_findings.append(finding)

        # Resolved findings: in A but not in B
        for key, finding in keys_a.items():
            if key not in keys_b:
                diff.resolved_findings.append(finding)

        # Metrics deltas
        # (Implementation would fetch WorkflowRun objects and compare fields)

        return diff
