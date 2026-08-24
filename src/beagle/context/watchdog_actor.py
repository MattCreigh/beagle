"""WatchdogActor — auto-compacting watchdog (v13.22.0).

This module is the *actor* (not just observer) layer of Beagle's context
management. The previous watchdog was observability-only: it read state
and logged drift. The new design — per session-bootstrap directive
"fix context management which has been broken now and does not fire
automatically as it should" — turns the watchdog into the deterministic
safety net that fires TurboQuant folds when the in-band triggers (LLM-
initiative `check_and_fold_context`, post-final-answer `enforce_post_final_answer_fold`)
fail to fire.

Design contract (per .beagle/design/auto_compact_watchdog.xml):

    1. The actor is a *singleton* (`get_watchdog_actor()`), matching the
       existing `get_context_integration()` and `get_monitor()` accessors.
    2. The actor decides whether to fire based on:
         - `last_compaction_at` (from `compaction_state.json`)
         - `current_pct` (from `context_report.json`)
         - `last_fold_type` (to skip when post-final-answer fold just ran)
       Override: if `current_pct >= critical` (0.85), fire unconditionally.
    3. The actor is *idempotent* — calling `compact_now()` twice within
       1h is a no-op (the second call sees `last_compaction_at` recent
       and bails).  This makes it safe to be called from both the cron
       and the in-process ServerSideTokenCounter subscriber.
    4. The actor reuses `ContextMonitor.fold_and_surrender()` — the
       existing sovereignty fold primitive.  No reinvention.

Exit codes for the cron (used by `scripts/beagle_watchdog.py --compact`):
    0 = fired successfully (or correctly skipped a no-op)
    2 = skipped with reason (recently compacted, fold just ran)
    3 = error (couldn't read state, fold raised, etc.)

The class is split from the cron script for testability — the cron
imports and drives it; unit tests import and exercise the pure threshold
logic with mocked `ContextMonitor`.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("Beagle.context.watchdog_actor")

# ── Paths ────────────────────────────────────────────────────────────────────
BEAGLE_DIR = Path.home() / ".beagle"
COMPACTION_STATE = BEAGLE_DIR / "compaction_state.json"
CONTEXT_REPORT = BEAGLE_DIR / "context_report.json"

# ── Constants ────────────────────────────────────────────────────────────────
DEFAULT_TIMER_SECONDS = 3600  # 1 hour
#: A fold that happened within this window satisfies the "recently compacted"
#: timer — the actor will NOT re-fire.  This is the user's "when compacted
#: timer restarts for 1 hour" rule.
RECENT_COMPACTION_WINDOW_S = DEFAULT_TIMER_SECONDS


# ── Helpers ──────────────────────────────────────────────────────────────────


def _read_json(path: Path) -> dict[str, Any]:
    """Read a JSON file, returning an empty dict on any failure.

    Robust against:
        - missing file (first run, never compacted)
        - corrupt JSON (truncated write)
        - permission errors (rare on ~/.beagle, but possible)
    Never raises; the caller decides what to do with an empty result.
    """
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        return dict(loaded) if isinstance(loaded, dict) else {}
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        logger.debug(f"Failed to read {path}: {exc}")
        return {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write a JSON file atomically with a parent-mkdir guard.

    Uses a temp-file rename to avoid half-written state if the process is
    killed mid-write.  Best-effort: never raises; logs at debug.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(path)
    except OSError as exc:
        logger.debug(f"Failed to write {path}: {exc}")


# ── Core class ───────────────────────────────────────────────────────────────


class WatchdogActor:
    """Deterministic auto-compacting watchdog.

    Lifecycle:
        actor = get_watchdog_actor()
        if actor.should_fire():
            result = actor.compact_now()
        status = actor.get_status()    # for --status mode and the TUI

    Thread-safety: this class is a single-process singleton.  Methods
    mutate `_last_fold_id` and read on-disk state but do not protect
    against concurrent invocations.  The cron and the server-side
    subscriber are expected to call `compact_now()` at most once per
    minute; the idempotency check in `should_fire()` makes races safe
    (worst case: two folds, second is a no-op since data is identical).
    """

    def __init__(self) -> None:
        self._last_fold_id: str = ""
        self._last_compact_at: float = 0.0
        self._last_outcome: dict[str, Any] = {}

    # ── Public API ──────────────────────────────────────────────────────

    def should_fire(
        self,
        *,
        current_pct: float | None = None,
        now: float | None = None,
        timer_seconds: int = RECENT_COMPACTION_WINDOW_S,
    ) -> tuple[bool, str]:
        """Decide whether to fire a fold now.

        Args:
            current_pct: Override current context utilization (0.0-1.0).
                If None, read from `context_report.json`.
            now: Override the wall clock (testability).  Defaults to
                `time.time()`.
            timer_seconds: How recent a fold counts as "already done"
                (default 3600s = 1h).

        Returns:
            (should_fire, reason) tuple.  `reason` is human-readable and
            ends up in the cron log and the watchdog's JSON line.

        """
        if current_pct is None:
            report = _read_json(CONTEXT_REPORT)
            current_pct = float(report.get("utilization", 0.0))
        now = now if now is not None else time.time()

        # Pull thresholds from the live config (env-var aware via
        # ContextThresholdConfig.effective_compact).
        try:
            from beagle.config.config import get_config

            cfg = get_config()
            warning = cfg.context_threshold.warning
            critical = cfg.context_threshold.critical
        except (ImportError, AttributeError, RuntimeError) as exc:
            # No config available — fall back to the doctrine defaults.
            logger.debug(f"Config unavailable, using hard-coded thresholds: {exc}")
            warning = 0.50
            critical = 0.85

        # Critical always fires (override the timer).
        if current_pct >= critical:
            return True, f"critical threshold reached: {current_pct:.1%} >= {critical:.0%}"

        # Below warning: nothing to do.
        if current_pct < warning:
            return False, f"below warning threshold: {current_pct:.1%} < {warning:.0%}"

        # Above warning but below critical: respect the timer and the
        # post-final-answer exemption.
        state = _read_json(COMPACTION_STATE)
        last_compaction = float(state.get("last_compaction", 0.0))
        last_fold_type = state.get("last_fold_type", "")
        compaction_count = int(state.get("compaction_count", 0))

        elapsed = now - last_compaction if last_compaction > 0 else float("inf")
        if elapsed < timer_seconds and last_fold_type == "post_final_answer":
            return (
                False,
                f"recently compacted ({elapsed:.0f}s ago, type={last_fold_type}); "
                f"Beagle fold just ran — skipping to honor <post_final_answer_fold>",
            )
        if elapsed < timer_seconds:
            return (
                False,
                f"recently compacted ({elapsed:.0f}s ago, type={last_fold_type or 'unknown'}); "
                f"timer not elapsed ({timer_seconds}s)",
            )
        return (
            True,
            f"timer elapsed ({elapsed:.0f}s >= {timer_seconds}s) and current_pct="
            f"{current_pct:.1%} >= warning {warning:.0%}; previous compactions: "
            f"{compaction_count}",
        )

    def compact_now(
        self,
        *,
        node_name: str = "watchdog",
        force: bool = False,
    ) -> dict[str, Any]:
        """Fire a fold via ContextMonitor.fold_and_surrender.

        Args:
            node_name: Identifies the originator in the compaction log.
            force: Bypass the `should_fire` check (used by the cron
                after manual trigger or for testing).  The idempotency
                of fold_and_surrender itself is preserved (it checks
                the count).

        Returns:
            Dict with keys:
                status: "fired" | "skipped" | "error"
                fold_id: 12-char hex (if fired)
                reason: human-readable
                elapsed_s: time taken
                compaction_count: number from state

        """

        result: dict[str, Any] = {
            "status": "error",
            "fold_id": "",
            "reason": "",
            "elapsed_s": 0.0,
            "compaction_count": 0,
        }
        start = time.monotonic()

        should, reason = self.should_fire()
        if not should and not force:
            result["status"] = "skipped"
            result["reason"] = reason
            logger.info(f"[WatchdogActor] Skipping fold: {reason}")
            self._last_outcome = result
            return result

        # Read current state to capture the post-fold count.
        pre_state = _read_json(COMPACTION_STATE)
        result["compaction_count"] = int(pre_state.get("compaction_count", 0))

        # Lazy import to avoid pulling trigger.py into the cron at module
        # load time (it transitively imports torch / tiktoken, which
        # has caused DNS failures in the broken beagle-factory container).
        try:
            from beagle.context.trigger import (
                ContextMonitor,
            )
        except ImportError as exc:
            result["reason"] = f"context.trigger not importable: {exc}"
            result["status"] = "error"
            logger.error(f"[WatchdogActor] Failed to import ContextMonitor: {exc}")
            self._last_outcome = result
            return result

        # Get the singleton monitor (or create a fresh one).  We
        # deliberately do NOT use the existing singleton via get_monitor()
        # in the cron path — the cron is a separate process, so its
        # in-memory state is fresh anyway.  The on-disk state is the
        # contract that matters.
        monitor = ContextMonitor(session_id=f"watchdog-{int(start)}")
        try:
            fold_id = monitor.fold_and_surrender(node_name=node_name)
        except (RuntimeError, OSError, ValueError) as exc:
            result["reason"] = f"fold_and_surrender raised: {exc}"
            result["status"] = "error"
            logger.exception(f"[WatchdogActor] fold_and_surrender failed: {exc}")
            self._last_outcome = result
            return result

        result["elapsed_s"] = round(time.monotonic() - start, 3)
        result["fold_id"] = fold_id or ""
        if fold_id:
            self._last_fold_id = fold_id
            self._last_compact_at = time.time()
            self._record_compaction(fold_id, "watchdog", monitor)
            result["status"] = "fired"
            result["reason"] = f"fold_id={fold_id}"
            result["compaction_count"] = int(
                _read_json(COMPACTION_STATE).get("compaction_count", 0)
            )
            logger.info(
                f"[WatchdogActor] Fold fired: fold_id={fold_id} "
                f"compaction_count={result['compaction_count']} "
                f"elapsed={result['elapsed_s']}s"
            )
        else:
            # fold_and_surrender returned None — usually because there
            # was no accumulated context.  Treat as a no-op success.
            result["status"] = "skipped"
            result["reason"] = "fold_and_surrender returned None (no accumulated context)"
            logger.info(f"[WatchdogActor] No fold produced: {result['reason']}")

        self._last_outcome = result
        return result

    def get_status(self) -> dict[str, Any]:
        """Return a snapshot of current threshold state for `--status` mode."""
        state = _read_json(COMPACTION_STATE)
        report = _read_json(CONTEXT_REPORT)
        should, reason = self.should_fire()
        try:
            from beagle.config.config import get_config

            cfg = get_config()
            thresholds = {
                "warning": cfg.context_threshold.warning,
                "pre_compact": cfg.context_threshold.pre_compact,
                "compact": cfg.context_threshold.compact,
                "hard_compact": cfg.context_threshold.hard_compact,
                "critical": cfg.context_threshold.critical,
            }
        except (ImportError, AttributeError, RuntimeError):
            thresholds = {
                "warning": 0.50,
                "pre_compact": 0.58,
                "compact": 0.70,
                "hard_compact": 0.78,
                "critical": 0.85,
            }
        return {
            "now": time.time(),
            "current_pct": report.get("utilization", 0.0),
            "current_tokens": report.get("current_tokens", 0),
            "max_tokens": report.get("max_tokens", 0),
            "last_compaction_at": state.get("last_compaction", 0.0),
            "last_fold_type": state.get("last_fold_type", "unknown"),
            "compaction_count": int(state.get("compaction_count", 0)),
            "thresholds": thresholds,
            "should_fire": should,
            "reason": reason,
            "subscriber_verified": report.get("subscriber_verified", False),
            "last_outcome": self._last_outcome,
        }

    # ── Internal ────────────────────────────────────────────────────────

    def _record_compaction(
        self,
        fold_id: str,
        fold_type: str,
        monitor: Any,
    ) -> None:
        """Persist the fold to compaction_state.json.

        Mirrors `ContextMonitor.record_compaction` but explicitly tags
        `last_fold_type` so the watchdog's own folds are distinguishable
        from sovereignty folds and post-final-answer folds.  Also calls
        the monitor's own record_compaction so the in-memory history
        stays consistent (in case the monitor is reused).
        """
        try:
            monitor.record_compaction(0, node_name=fold_type)  # type: ignore[attr-defined]
        except (AttributeError, RuntimeError, ValueError, OSError) as exc:
            logger.debug(f"monitor.record_compaction failed (non-fatal): {exc}")

        state = _read_json(COMPACTION_STATE)
        state["last_compaction"] = time.time()
        state["last_fold_type"] = fold_type
        state["last_fold_id"] = fold_id
        state["compaction_count"] = int(
            state.get("compaction_count", 0)
        )  # record_compaction already incremented
        state.setdefault("history", []).append(
            {
                "fold_id": fold_id,
                "fold_type": fold_type,
                "timestamp": time.time(),
            }
        )
        # Trim history to last 10.
        state["history"] = state["history"][-10:]
        _write_json(COMPACTION_STATE, state)


# ── Singleton accessor ──────────────────────────────────────────────────────

_actor_singleton: WatchdogActor | None = None


def get_watchdog_actor() -> WatchdogActor:
    """Return the process-wide WatchdogActor singleton.

    Matches the existing pattern used by `get_context_integration()` and
    `get_monitor()`.  Idempotent; safe to call from cron (fresh process)
    or from the MCP utility server (long-running).
    """
    global _actor_singleton
    if _actor_singleton is None:
        _actor_singleton = WatchdogActor()
    return _actor_singleton


def reset_watchdog_actor() -> None:
    """Reset the singleton (for tests only)."""
    global _actor_singleton
    _actor_singleton = None


__all__ = [
    "COMPACTION_STATE",
    "CONTEXT_REPORT",
    "RECENT_COMPACTION_WINDOW_S",
    "WatchdogActor",
    "get_watchdog_actor",
    "reset_watchdog_actor",
]
