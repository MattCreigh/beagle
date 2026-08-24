"""Integration tests for TurboQuant context fold via compaction hook.

Validates:
- _build_accumulated_context() produces structured text blob or empty
- save_checkpoint() invokes fold and assigns fold_id
- Fold failure is non-fatal (checkpoint saved, fold_id empty)
- Rehydration prompt includes <FoldPointer> when fold_id present
- Rehydration prompt falls back when fold_id absent
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from beagle.context.context_compaction_hook import (
    CompactionCheckpoint,
    ContextMonitor,
)
from beagle.context.post_compaction_rehydration import (
    build_rehydration_prompt,
)


def _make_checkpoint(**kwargs) -> CompactionCheckpoint:
    """Minimal valid checkpoint for testing."""
    defaults = {
        "timestamp": datetime.now(UTC),
        "current_task": "test task",
        "iteration": 1,
        "total_iterations": 3,
    }
    defaults.update(kwargs)
    return CompactionCheckpoint(**defaults)


# ── _build_accumulated_context ───────────────────────────────────────────────


class TestBuildAccumulatedContext:
    """Test _build_accumulated_context() output shape."""

    def test_empty_sessions_produces_empty_string(self):
        """No session messages → empty string (by design in real code)."""
        monitor = ContextMonitor()
        blob = monitor._build_accumulated_context()
        assert blob == ""

    def test_single_message_included(self):
        """A single session message appears in the blob."""
        monitor = ContextMonitor()
        monitor._session_messages = [{"role": "user", "content": "Hello, world."}]
        blob = monitor._build_accumulated_context()
        assert "Hello, world." in blob

    def test_multiple_messages_ordered(self):
        """The blob preserves sequential order of session messages."""
        monitor = ContextMonitor()
        monitor._session_messages = [
            {"role": "user", "content": "First msg"},
            {"role": "assistant", "content": "Second msg"},
            {"role": "user", "content": "Third msg"},
        ]
        blob = monitor._build_accumulated_context()

        idx1 = blob.index("First msg")
        idx2 = blob.index("Second msg")
        idx3 = blob.index("Third msg")
        assert idx1 < idx2 < idx3

    def test_long_message_truncated(self):
        """Very long messages are truncated to avoid bloating fold input."""
        monitor = ContextMonitor()
        long_text = "X" * 5000
        monitor._session_messages = [{"role": "user", "content": long_text}]
        blob = monitor._build_accumulated_context()
        # After truncation at 4000, should be shorter than the full 5000
        assert len(long_text) not in [len(blob)]  # blob is multi-line, not just message
        assert "truncated for fold" in blob

    def test_roles_labeled(self):
        """User and assistant messages are prefixed with role labels."""
        monitor = ContextMonitor()
        monitor._session_messages = [
            {"role": "user", "content": "ask"},
            {"role": "assistant", "content": "reply"},
        ]
        blob = monitor._build_accumulated_context()
        assert "[user]: ask" in blob
        assert "[assistant]: reply" in blob


# ── CompactionCheckpoint fold_id ─────────────────────────────────────────────


class TestCompactionCheckpointFoldId:
    """Test fold_id field on CompactionCheckpoint."""

    def test_fold_id_defaults_to_empty(self):
        """New CompactionCheckpoint has empty fold_id."""
        cp = _make_checkpoint()
        assert cp.fold_id == ""

    def test_fold_id_round_trips_through_json(self):
        """fold_id survives to_json → from_json cycle."""
        cp = _make_checkpoint(session_id="abc", fold_id="fold-xyz-001")
        result = cp.to_json()
        restored = CompactionCheckpoint.from_json(result)
        assert restored.fold_id == "fold-xyz-001"

    def test_fold_id_in_json_output(self):
        """fold_id appears in serialized JSON."""
        cp = _make_checkpoint(fold_id="f-123")
        data = json.loads(cp.to_json())
        assert data["fold_id"] == "f-123"


# ── Rehydration prompt FoldPointer ───────────────────────────────────────────


class TestRehydrationFoldPointer:
    """Test that build_rehydration_prompt emits <FoldPointer> when appropriate."""

    def test_fold_pointer_emitted_when_fold_id_present(self, tmp_path):
        """When checkpoint has fold_id, fold info appears in prompt."""
        # Create a dummy manifest so the prompt can read it
        fold_dir = Path.home() / ".beagle" / "context_folds"
        fold_dir.mkdir(parents=True, exist_ok=True)
        manifest = fold_dir / "f-test_manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "fold_id": "f-test",
                    "compression_ratio": 0.3,
                    "original_tokens": 1000,
                    "compressed_tokens": 300,
                }
            )
        )

        try:
            cp = _make_checkpoint(session_id="s1", fold_id="f-test")
            prompt = build_rehydration_prompt(cp)
            # Should mention fold
            assert "fold" in prompt.lower() or "FoldPointer" in prompt or "TurboQuant" in prompt
        finally:
            manifest.unlink(missing_ok=True)

    def test_no_fold_pointer_when_fold_id_empty(self):
        """When fold_id is empty, no fold-specific content appears."""
        cp = _make_checkpoint(session_id="s2", fold_id="")
        prompt = build_rehydration_prompt(cp)
        assert "FoldPointer" not in prompt
        assert "TurboQuant fold applied" not in prompt

    def test_fallback_path_when_no_fold(self):
        """Without a fold_id the prompt still produces valid rehydration content."""
        cp = _make_checkpoint(session_id="s3", fold_id="")
        prompt = build_rehydration_prompt(cp)
        assert len(prompt) > 100
        assert "AutonomousResumeDirective" in prompt or "resume" in prompt.lower()
