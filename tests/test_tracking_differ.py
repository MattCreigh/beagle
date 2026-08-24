"""SP-5: tests for tracking/differ (was zero-coverage).

beagle-spotless-phase2, work package SP-5. The cross-run differ compares
findings between a baseline run (A) and a current run (B). These tests drive
it against a stub database and assert the new / resolved / persistent buckets.
"""

from __future__ import annotations

from beagle.tracking.differ import RunDiff, RunDiffer
from beagle.tracking.models import Finding


def _finding(title: str, file_path: str | None, line: int | None, run_id: str) -> Finding:
    return Finding(
        id=f"{title}-{line}",
        workflow_run_id=run_id,
        node_name="validation",
        severity="medium",
        category="bug",
        title=title,
        description="desc",
        file_path=file_path,
        line_number=line,
    )


class _StubDB:
    """In-memory stand-in for TrackingDatabase.get_findings_for_run."""

    def __init__(self, runs: dict[str, list[Finding]]):
        self._runs = runs

    def get_findings_for_run(self, run_id: str) -> list[Finding]:
        return self._runs.get(run_id, [])


def test_empty_runs() -> None:
    """No findings on either run → empty buckets, zero deltas."""
    differ = RunDiffer(_StubDB({}))  # type: ignore[arg-type]
    diff = differ.compare("A", "B")
    assert diff.new_findings == []
    assert diff.resolved_findings == []
    assert diff.persistent_findings == []
    assert diff.cost_delta_usd == 0.0


def test_new_finding_detected() -> None:
    """A finding only in run B is a new finding."""
    a = [_finding("f1", "a.py", 1, "A")]
    b = [
        _finding("f1", "a.py", 1, "A"),
        _finding("f2", "b.py", 2, "B"),
    ]
    result = RunDiffer(_StubDB({"A": a, "B": b}))  # type: ignore[arg-type]
    result = result.compare("A", "B")
    assert [f.title for f in result.new_findings] == ["f2"]


def test_resolved_finding_detected() -> None:
    """A finding only in run A is resolved."""
    a = [_finding("f1", "a.py", 1, "A"), _finding("old", "x.py", 9, "A")]
    b = [_finding("f1", "a.py", 1, "A")]
    diff = RunDiffer(_StubDB({"A": a, "B": b})).compare("A", "B")  # type: ignore[arg-type]
    assert [f.title for f in diff.resolved_findings] == ["old"]


def test_persistent_finding_detected() -> None:
    """A finding in both runs is persistent."""
    a = [_finding("f1", "a.py", 1, "A")]
    b = [_finding("f1", "a.py", 1, "A")]
    diff = RunDiffer(_StubDB({"A": a, "B": b})).compare("A", "B")  # type: ignore[arg-type]
    assert [f.title for f in diff.persistent_findings] == ["f1"]


def test_dedup_key_uses_file_line_title() -> None:
    """Findings are deduplicated by (file_path, line_number, title)."""
    a = [_finding("dup", "a.py", 1, "A")]
    b = [_finding("dup", "a.py", 1, "B")]
    diff = RunDiffer(_StubDB({"A": a, "B": b})).compare("A", "B")  # type: ignore[arg-type]
    assert diff.new_findings == []
    assert diff.resolved_findings == []
    assert len(diff.persistent_findings) == 1


def test_run_diff_defaults() -> None:
    """RunDiff carries the two run ids and empty buckets by default."""
    rd = RunDiff(run_id_a="A", run_id_b="B")
    assert rd.run_id_a == "A"
    assert rd.run_id_b == "B"
    assert rd.new_findings == []
    assert rd.cost_delta_usd == 0.0
