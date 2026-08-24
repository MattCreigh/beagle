"""ServerSideTokenCounter — in-process subscriber to ContextWarning events.

This module is the *server-side counting hook* of the auto-compacting
watchdog.  Per the user directive: "have watchdog monitor tokens/context
window fullness percentage and have it do it every hour if and only if
its not reached values the watchdog uses, from context/tokens etc/ event
bus".

The source of truth for token counts is the LLM-reported
``ContextWarning`` event published by ``cost_tracker.update_context()``
on every 0.5% utilization change.  The subscriber:

    1. Listens to ``context.warning`` on the EventBus.
    2. Maintains an in-memory snapshot of (current_tokens, max_tokens,
       utilization_pct) and persists it to ``~/.beagle/context_report.json``
       as the authoritative server-side reading.
    3. On threshold crossings, fires the ``WatchdogActor`` to do the
       actual fold (decision logic lives in the actor, not here).
    4. On ``critical`` (>= 0.85), logs at CRITICAL and fires the fold
       *unconditionally* — the actor's override kicks in. (It used to
       ``print()`` to stderr; that was removed in v13.22.1 because print is
       forbidden in library code, and the record now goes to the logger.)

Why a server-side subscriber at all?

    The cron watchdog can only run hourly.  Between runs, the LLM might
    fill 30% of the context in 10 minutes (very long context, big
    inputs).  The in-process subscriber fires within milliseconds of
    the first ``ContextWarning`` past pre-compact (0.58), giving us
    proactive folding with no LLM discipline required.

Why a singleton?

    The MCP utility server is long-running and hosts the subscriber.
    The actor is also a singleton (see ``watchdog_actor.py``).  Together
    they form a tight feedback loop: cost_tracker -> EventBus ->
    ServerSideTokenCounter -> WatchdogActor -> ContextMonitor.fold ->
    rehydration sidecar -> next session picks it up.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("Beagle.context.token_counter_subscriber")

# ── Paths (mirrored from watchdog_actor.py to avoid circular import) ────────
BEAGLE_DIR = Path.home() / ".beagle"
CONTEXT_REPORT = BEAGLE_DIR / "context_report.json"

# ── Tunables (defaults match ContextThresholdConfig) ─────────────────────────
DEFAULT_PRE_COMPACT = 0.58
DEFAULT_CRITICAL = 0.85
#: Below this utilization, we don't even write context_report.json — the
#: cost_tracker already does, and we'd just be churning the file.  But
#: we DO write at warning (0.50) so the actor's should_fire has fresh data.
DEFAULT_WARNING = 0.50


# ── Subscriber class ─────────────────────────────────────────────────────────


class ServerSideTokenCounter:
    """EventBus subscriber that maintains a server-side token count.

    Lifecycle:
        counter = get_token_counter()   # singleton; auto-subscribes
        # ... the counter is now wired to the EventBus ...

    Thread-safety: the EventBus callback may be invoked from any thread.
    The internal state uses a lock to prevent torn reads/writes of the
    utilization snapshot.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current_tokens: int = 0
        self._max_tokens: int = 0
        self._utilization: float = 0.0
        self._last_event_at: float = 0.0
        self._last_fire_at: float = 0.0
        self._fires_triggered: int = 0
        self._events_seen: int = 0
        self._subscription_id: str = ""
        self._subscribed: bool = False

    # ── Public API ──────────────────────────────────────────────────────

    def subscribe(self) -> str:
        """Subscribe to context.warning on the global EventBus.

        Idempotent — calling twice returns the same subscription ID and
        does not double-register.  This is the function the MCP utility
        server calls at startup.
        """
        if self._subscribed:
            return self._subscription_id

        try:
            from beagle.events.bus import get_event_bus
        except ImportError as exc:
            logger.error(f"[TokenCounter] Cannot import EventBus: {exc}")
            return ""

        bus = get_event_bus()
        sub_id = bus.subscribe("context.warning", self._on_event)
        if sub_id:
            self._subscription_id = sub_id
            self._subscribed = True
            logger.info(f"[TokenCounter] Subscribed to context.warning (sub_id={sub_id[:8]}…)")
        else:
            logger.warning("[TokenCounter] Failed to subscribe to EventBus")
        return sub_id

    def get_snapshot(self) -> dict[str, Any]:
        """Return the current server-side reading (thread-safe)."""
        with self._lock:
            return {
                "current_tokens": self._current_tokens,
                "max_tokens": self._max_tokens,
                "utilization": self._utilization,
                "last_event_at": self._last_event_at,
                "events_seen": self._events_seen,
                "fires_triggered": self._fires_triggered,
                "last_fire_at": self._last_fire_at,
                "subscriber_verified": self._subscribed,
            }

    def force_fire_test(self) -> dict[str, Any]:
        """Test hook: invoke the actor synchronously and return the result.

        Used by the watchdog tests; the production path is event-driven.
        """
        return self._maybe_fire_actor(force=True)

    # ── EventBus callback ───────────────────────────────────────────────

    def _on_event(self, event: Any) -> None:
        """Handle a ContextWarning event from the EventBus.

        Runs in whatever thread the publisher called publish() from.
        The actual fold is fired on a background thread to avoid
        blocking the publisher; the threshold decision is fast (one
        file read), the fold itself can take ~1s for a TurboQuant build.
        """
        # Defensive: filter to ContextWarning-shaped events only.
        if not hasattr(event, "utilization"):
            return

        utilization = float(getattr(event, "utilization", 0.0))
        current_tokens = int(getattr(event, "current_tokens", 0))
        max_tokens = int(getattr(event, "max_tokens", 0))

        with self._lock:
            self._utilization = utilization
            self._current_tokens = current_tokens
            self._max_tokens = max_tokens
            self._last_event_at = time.time()
            self._events_seen += 1

        # Persist through the single writer.  context_reporter owns the
        # file format; this subscriber only supplies the numbers.  Import
        # inside the method to keep the existing import graph.
        from beagle.context.context_reporter import write_report

        write_report(
            percentage=utilization,
            used_tokens=current_tokens,
            max_tokens=max_tokens,
            source="token_counter_subscriber",
            diagnostics={
                "subscriber_verified": self._subscribed,
                "events_seen": self._events_seen,
                "fires_triggered": self._fires_triggered,
            },
        )

        # Threshold decision — read the live config to honor env-var
        # overrides (e.g. GOOSE_AUTO_COMPACT_THRESHOLD).
        try:
            from beagle.config.config import get_config

            cfg = get_config()
            pre_compact = cfg.context_threshold.pre_compact
            critical = cfg.context_threshold.critical
        except (ImportError, AttributeError, RuntimeError):
            pre_compact = DEFAULT_PRE_COMPACT
            critical = DEFAULT_CRITICAL

        if utilization >= critical:
            # CRITICAL — log loudly and force the fold.
            #
            # v13.22.1 (B-10) removed a `print()` to stderr here, correctly:
            # print() is forbidden in library code. But nothing replaced it, so
            # the loud record this branch promises — and that the module
            # docstring still advertises — silently stopped happening, and the
            # test asserting on it had been failing ever since. Removing a
            # forbidden mechanism is not the same as removing the feature; the
            # feature belongs on the logger.
            logger.critical(
                "Context utilization %.1f%% >= critical %.1f%% (%d/%d tokens) — forcing fold",
                utilization * 100,
                critical * 100,
                current_tokens,
                max_tokens,
            )
            self._maybe_fire_actor(force=True)
        elif utilization >= pre_compact:
            # Pre-compact zone — consult the actor (it has the timer
            # + post-final-answer exemption logic).
            self._maybe_fire_actor(force=False)
        # else: below pre-compact — no action.

    # ── Internal ────────────────────────────────────────────────────────

    def _maybe_fire_actor(self, *, force: bool) -> dict[str, Any]:
        """Run the WatchdogActor on a background thread.

        The fold is expensive (~1s) and the EventBus callback should
        not block the publisher.  We capture the result via a list
        (mutable container, thread-safe-ish in CPython) and log it
        from the main thread on the next event.
        """
        # Throttle: never fire more than once per 30s from the
        # subscriber.  The actor's own timer is the primary guard;
        # this is just a belt-and-suspenders debounce.
        with self._lock:
            if not force and (time.monotonic() - self._last_fire_at) < 30.0:
                return {"status": "throttled", "reason": "subscriber debounce 30s"}
            self._last_fire_at = time.monotonic()
            self._fires_triggered += 1

        result_holder: list[dict[str, Any]] = []

        def _run() -> None:
            try:
                from beagle.context.watchdog_actor import (
                    get_watchdog_actor,
                )

                actor = get_watchdog_actor()
                outcome = actor.compact_now(node_name="token_counter_subscriber", force=force)
                result_holder.append(outcome)
            except (ImportError, RuntimeError, OSError, ValueError) as exc:
                logger.exception(f"[TokenCounter] Actor invocation failed: {exc}")
                result_holder.append({"status": "error", "reason": str(exc)})

        t = threading.Thread(
            target=_run,
            name="watchdog-actor-fire",
            daemon=True,
        )
        t.start()
        # Don't join — the EventBus expects callbacks to return quickly.
        # The result is logged when the thread completes (via a small
        # atexit hook would be ideal, but for now we just return early).
        return {"status": "fired_async", "thread_started": True}


# ── Singleton accessor ──────────────────────────────────────────────────────

_counter_singleton: ServerSideTokenCounter | None = None
_counter_lock = threading.Lock()


def get_token_counter() -> ServerSideTokenCounter:
    """Return the process-wide ServerSideTokenCounter singleton.

    Calling this function is the *only* required action — it auto-
    subscribes to the EventBus on first call.  Idempotent; subsequent
    calls return the same instance.
    """
    global _counter_singleton
    with _counter_lock:
        if _counter_singleton is None:
            _counter_singleton = ServerSideTokenCounter()
            _counter_singleton.subscribe()
    return _counter_singleton


def reset_token_counter() -> None:
    """Reset the singleton (for tests only)."""
    global _counter_singleton
    _counter_singleton = None


__all__ = [
    "CONTEXT_REPORT",
    "DEFAULT_CRITICAL",
    "DEFAULT_PRE_COMPACT",
    "DEFAULT_WARNING",
    "ServerSideTokenCounter",
    "get_token_counter",
    "reset_token_counter",
]
