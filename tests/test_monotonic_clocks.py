"""SP-13: monotonic clock for every elapsed measurement (I17).

beagle-spotless-phase2.xml, work package SP-13.

Rule (logic block L1):
    use_monotonic ≡ (measures_duration ∧ ¬persisted)
    use_wallclock ≡ (¬measures_duration ∧ timezone_aware)

A ``time.time()`` read used to compute an in-process elapsed duration breaks
under an NTP / daylight-saving backward step (negative duration). Convert such
in-process durations to ``time.monotonic()``. A wall-clock read against a
persisted timestamp (mtime, DB record, checkpoint) must stay wall-clock.

This test guards two properties:
1. No in-memory ``start``/``_last``/``cached_at``/``created_at`` timestamp that
   is only used for an elapsed comparison is stored with ``time.time()``.
2. A rate-limiter-style elapsed check stays correct when the wall clock moves
   backward.
"""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"


def _iter_py_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


# Known in-memory (non-persisted) elapsed-duration holders. Each must be
# written and read with the SAME clock. A `time.time()` here is a defect.
_IN_MEMORY_ELAPSED_HOLDERS = {
    "warm_workers.py": "created_at",
    "secrets_loader.py": "cached_at",
    "mcp_security.py": "created_at",  # token TTL (in-memory dict)
    "retriever.py": "ts",  # result cache TTL
    "mcp_rag_server.py": "timestamp",  # RAG cache TTL
    "semantic_prompt_cache.py": "created_at",  # LRU cache TTL
}


def test_no_wallclock_store_for_in_memory_elapsed_holder() -> None:
    """SP-13 gate: an in-memory elapsed holder is never stored with time.time()."""
    offenders: list[str] = []
    for filename, holder in _IN_MEMORY_ELAPSED_HOLDERS.items():
        py = SRC / "beagle" / "infrastructure" / filename
        if not py.exists():
            # Some holders live elsewhere.
            for cand in SRC.rglob(filename):
                py = cand
                break
        else:
            py = SRC / "beagle" / "infrastructure" / filename
        if not py.exists():
            for cand in SRC.rglob(filename):
                py = cand
        text = py.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            # `holder = time.time()` (write) is the defect; monotonic is fine.
            if re.search(rf"\b{re.escape(holder)}\s*=\s*time\.time\(\)", line):
                offenders.append(f"{py.relative_to(SRC)}:{lineno}: {line.strip()}")
    assert offenders == [], (
        "in-memory elapsed holders are stored with wall-clock time.time(); "
        "use time.monotonic() so an NTP backward step cannot produce a "
        "negative duration:\n" + "\n".join(offenders)
    )


def test_in_memory_elapsed_read_is_monotonic() -> None:
    """SP-13 gate: an in-memory elapsed subtraction uses time.monotonic()."""
    offenders: list[str] = []
    for filename, holder in _IN_MEMORY_ELAPSED_HOLDERS.items():
        for cand in SRC.rglob(filename):
            text = cand.read_text(encoding="utf-8")
            for lineno, line in enumerate(text.splitlines(), 1):
                # A `time.time() - <holder>` read on an in-memory elapsed value.
                if re.search(rf"time\.time\(\)\s*-\s*{re.escape(holder)}", line):
                    offenders.append(f"{cand.relative_to(SRC)}:{lineno}: {line.strip()}")
    assert offenders == [], (
        "in-memory elapsed values are read with wall-clock time.time(); "
        "use time.monotonic():\n" + "\n".join(offenders)
    )


def test_rate_limiter_survives_backward_wall_clock_step() -> None:
    """SP-13 behaviour: a debounce rate-limiter stays correct under a backward step.

    The subscriber debounce must not fire spuriously (or hang) when the wall
    clock moves backwards. We drive the throttling branch and assert the
    monotonic clock keeps the 30 s window intact.
    """
    from unittest.mock import MagicMock, patch

    from beagle.context.token_counter_subscriber import ServerSideTokenCounter

    sub = ServerSideTokenCounter()
    # Simulate an NTP backward step: monotonic keeps advancing while time.time
    # jumps back.
    wall = 1_700_000_000.0
    mono = 1000.0

    def fake_wall() -> float:
        return wall

    def fake_mono() -> float:
        return mono

    # Mock Thread so the non-throttled branch does not spawn a real watchdog
    # actor thread (keeps the test hermetic and side-effect free).
    fake_thread = MagicMock()

    with (
        patch("time.time", fake_wall),
        patch("time.monotonic", fake_mono),
        patch("threading.Thread", fake_thread),
    ):
        # First fire records the monotonic baseline.
        sub._last_fire_at = mono
        # Advance monotonic by 10s (inside the 30s window) but jump wall back.
        mono += 10.0
        wall -= 50.0
        result = sub._maybe_fire_actor(force=False)
        assert result.get("status") == "throttled", (
            "debounce must stay throttled inside the window even after a "
            f"backward wall-clock step; got {result}"
        )
        # Advance monotonic past 30s → allowed.
        mono += 30.0
        result2 = sub._maybe_fire_actor(force=False)
        assert result2.get("status") == "fired_async", (
            "debounce must release once the monotonic window elapses, "
            f"regardless of wall clock; got {result2}"
        )
