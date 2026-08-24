"""SP-5: tests for context/checkpoint.CompactionCheckpoint (was zero-coverage).

beagle-spotless-phase2, work package SP-5. The compaction checkpoint dataclass
snapshots orchestrator state before a fold for rehydration recovery. These
exercise the JSON round-trip and the H-MEM v13 / v13.7.x fields.
"""

from __future__ import annotations

from datetime import UTC, datetime

from beagle.context.checkpoint import CompactionCheckpoint


def _sample() -> CompactionCheckpoint:
    return CompactionCheckpoint(
        timestamp=datetime(2026, 8, 16, 12, 0, tzinfo=UTC),
        current_task="audit auth",
        iteration=3,
        total_iterations=10,
        files_modified=["src/auth.py"],
        next_steps=["review findings"],
        session_id="sess-1",
        memory_trace="reasoning summary",
        tool_preferences={"execute": "goose"},
        fold_id="abc123",
    )


def test_to_json_includes_iso_timestamp() -> None:
    """to_json serializes the timestamp as ISO 8601."""
    payload = _sample().to_json()
    assert "2026-08-16T12:00:00+00:00" in payload
    assert '"current_task": "audit auth"' in payload


def test_round_trip_preserves_fields() -> None:
    """from_json(to_json(x)) reconstructs the checkpoint."""
    cp = _sample()
    restored = CompactionCheckpoint.from_json(cp.to_json())
    assert restored.timestamp == cp.timestamp
    assert restored.current_task == "audit auth"
    assert restored.iteration == 3
    assert restored.total_iterations == 10
    assert restored.files_modified == ["src/auth.py"]
    assert restored.session_id == "sess-1"
    assert restored.memory_trace == "reasoning summary"
    assert restored.tool_preferences == {"execute": "goose"}
    assert restored.fold_id == "abc123"


def test_defaults_round_trip() -> None:
    """Optional lists/dicts default to empty and survive the round-trip."""
    cp = CompactionCheckpoint(
        timestamp=datetime(2026, 8, 16, tzinfo=UTC),
        current_task="t",
        iteration=0,
        total_iterations=0,
    )
    restored = CompactionCheckpoint.from_json(cp.to_json())
    assert restored.files_modified == []
    assert restored.extracted_constraints == []
    assert restored.archived_outputs == {}
    assert restored.fallback_directives == []
    assert restored.compaction_history == []


def test_from_json_missing_optional_keys() -> None:
    """from_json tolerates missing optional keys (older snapshots)."""
    import json

    data = json.dumps(
        {
            "timestamp": "2026-08-16T12:00:00+00:00",
            "current_task": "t",
            "iteration": 1,
            "total_iterations": 5,
        }
    )
    cp = CompactionCheckpoint.from_json(data)
    assert cp.fold_id == ""
    assert cp.memory_trace == ""
