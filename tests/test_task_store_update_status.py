"""SP-11: branch-by-branch contract for TaskStore.update_task_status.

beagle-spotless-phase2, work package SP-11 (S608). The statement in
``update_task_status`` was assembled by joining column-assignment fragments
into an f-string. Every fragment was a literal, but the shape read as
SQL-by-string-construction to every scanner. The statement is now a single
literal in which each optional column keeps its own value behind a CASE
guard.

Only one test in the suite touched ``update_task_status`` at all
(``test_concurrency_stress.py``, which asserts throughput rather than which
columns changed). These tests pin the column-level behaviour the rewrite
must preserve: which of ``started_at``, ``completed_at``, ``result_json`` and
``error`` a call writes, and — just as important — which ones it leaves alone.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from beagle.infrastructure.task_store import TaskStore


@pytest.fixture
def store(tmp_path: Path) -> TaskStore:
    return TaskStore(db_path=tmp_path / "tasks.db")


def _new_task(store: TaskStore) -> str:
    return store.create_task(task_type="workflow", spec={"name": "t"})


def test_running_sets_started_at_only(store: TaskStore) -> None:
    task_id = _new_task(store)

    assert store.update_task_status(task_id, "running") is True

    row = store.get_task(task_id)
    assert row is not None
    assert row["status"] == "running"
    assert row["started_at"] is not None
    assert row["completed_at"] is None


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
def test_terminal_status_sets_completed_at_only(store: TaskStore, status: str) -> None:
    task_id = _new_task(store)

    assert store.update_task_status(task_id, status) is True

    row = store.get_task(task_id)
    assert row is not None
    assert row["status"] == status
    assert row["completed_at"] is not None
    assert row["started_at"] is None


def test_started_at_survives_a_later_terminal_update(store: TaskStore) -> None:
    """The terminal update must not clear the timestamp the running update wrote."""
    task_id = _new_task(store)
    store.update_task_status(task_id, "running")
    started = store.get_task(task_id)["started_at"]
    assert started is not None

    store.update_task_status(task_id, "completed")

    row = store.get_task(task_id)
    assert row["started_at"] == started
    assert row["completed_at"] is not None


def test_result_and_error_are_written_when_given(store: TaskStore) -> None:
    task_id = _new_task(store)

    store.update_task_status(task_id, "failed", result={"k": "v"}, error="boom")

    row = store.get_task(task_id)
    assert row["error"] == "boom"
    stored = row["result_json"]
    assert stored == {"k": "v"}


def test_result_and_error_are_left_alone_when_omitted(store: TaskStore) -> None:
    """An omitted result or error must preserve the stored value, not null it."""
    task_id = _new_task(store)
    store.update_task_status(task_id, "failed", result={"k": "v"}, error="boom")

    # A later update that passes neither must not erase either column.
    store.update_task_status(task_id, "cancelled")

    row = store.get_task(task_id)
    assert row["error"] == "boom"
    stored = row["result_json"]
    assert stored == {"k": "v"}


def test_falsy_result_is_treated_as_omitted(store: TaskStore) -> None:
    """An empty dict is falsy and must leave result_json untouched, as before."""
    task_id = _new_task(store)
    store.update_task_status(task_id, "running", result={"k": "v"})

    store.update_task_status(task_id, "completed", result={})

    row = store.get_task(task_id)
    stored = row["result_json"]
    assert stored == {"k": "v"}


def test_unknown_task_id_returns_false(store: TaskStore) -> None:
    assert store.update_task_status("no-such-task", "running") is False


def test_invalid_status_is_rejected(store: TaskStore) -> None:
    task_id = _new_task(store)
    with pytest.raises(ValueError, match="Invalid status"):
        store.update_task_status(task_id, "not-a-status")
