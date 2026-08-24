"""SP-1: tests for context/context_tracker_ext (silent-handler graceful degradation).

beagle-spotless-phase2, work package SP-1. The context tracker extension
swallows persistence/load errors with bare `pass` handlers so a corrupt or
missing session file degrades gracefully instead of crashing the caller. These
tests lock in that contract: the public methods return safe values (None or a
fresh state) rather than raising.
"""

from __future__ import annotations

import pytest

import beagle.context.context_tracker_ext as ct
from beagle.context.context_tracker_ext import (
    ContextTrackerState,
    _persist_state,
    load_session_state,
)


def test_state_defaults() -> None:
    """A fresh state has zero tokens and the default context window."""
    s = ContextTrackerState()
    assert s.input_tokens == 0
    assert s.output_tokens == 0
    assert s.context_window == 128000
    assert s.current_tokens == 0


def test_state_utilization() -> None:
    """utilization is current_tokens / context_window."""
    s = ContextTrackerState()
    s.record_request(1000, 500)
    assert s.current_tokens == 1500
    assert s.utilization == pytest.approx(1500 / 128000)
    assert s.utilization_percent == int(1500 / 128000 * 100)


def test_state_remaining_never_negative() -> None:
    """remaining clamps at 0 when tokens exceed the window."""
    s = ContextTrackerState(context_window=100)
    s.record_request(200, 0)
    assert s.remaining == 0


def test_load_session_state_missing_file_returns_none() -> None:
    """A missing session file returns None (graceful, not an error)."""
    # Point the module at a non-existent dir.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ct, "_session_state_file", __import__("pathlib").Path("/nonexistent/x.json"))
        assert load_session_state("any-session") is None


def test_persist_state_corrupt_path_is_silent() -> None:
    """A failed save is swallowed (logs at debug) and does not raise.

    This is the silent-handler contract: persistence is best-effort and the
    tracker keeps working if the state file cannot be written.
    """
    # Force a write failure by pointing the file at a directory.
    import pathlib

    bad_path = pathlib.Path("/nonexistent-dir/ctx.json")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ct, "_session_state_file", bad_path)
        # Should not raise (best-effort persistence).
        _persist_state(ContextTrackerState(session_id="s1"))


def test_record_request_updates_totals() -> None:
    """record_request accumulates input and output tokens."""
    s = ContextTrackerState()
    s.record_request(100, 50)
    s.record_request(10, 20)
    assert s.input_tokens == 110
    assert s.output_tokens == 70


def test_status_bar_renders() -> None:
    """The status bar string includes the percentage and token counts."""
    s = ContextTrackerState(context_window=1000)
    s.record_request(100, 100)
    bar = s.get_status_bar()
    assert "20%" in bar
    assert "200/1,000" in bar
