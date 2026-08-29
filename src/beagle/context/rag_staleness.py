"""RAG Staleness Tracker — Signals when RAG data needs re-ingestion.

When context folds or compacts, the codebase may have changed since the
last RAG ingestion. This module tracks staleness and triggers hot-swap
reingestion before the next hydration cycle serves stale data.

Lifecycle:
  1. Context fold marks RAG as "stale" (timestamp + reason)
  2. Next hydration call checks staleness before querying RAG
  3. If stale, triggers hotswap_ingest() to refresh the index
  4. After successful reingestion, marks RAG as "fresh" again

The staleness marker persists across compaction boundaries via a
sidecar JSON file so that even after context is truncated, the flag
survives and forces a reindex on the next hydration.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..utils.atomic import atomic_write_text

logger = logging.getLogger("Beagle.RAGStaleness")

# ── Configuration ──────────────────────────────────────────────────────────────


def _env_int(name: str, default: int) -> int:
    """Read an int from the environment, falling back loudly on garbage.

    B-31 (audit v13.22.1): a bare ``int(os.environ.get(...))`` at module
    scope turns a typo'd env var into an ImportError, which surfaces as
    "module not available" three layers up. Warn and use the default.
    """
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning(f"[RAGStaleness] {name}={raw!r} is not an integer; using {default}")
        return default


_STALENESS_FILE = os.environ.get(
    "BEAGLE_RAG_STALENESS_FILE",
    str(Path.home() / ".beagle" / "rag_staleness.json"),
)

# Minimum interval between hot-swap reingestion ATTEMPTS (seconds).
#
# B-1/B-5 (audit v13.22.1): v13.22.x lowered this to 5s on the premise
# that the incremental path is cheap. It was not — the delta state was
# never written, so every trigger took the full multi-minute re-index
# (B-4). Combined with a throttle keyed only on *successful* reingests
# and no in-flight guard, every `rag_search` spawned another concurrent
# full CAST pipeline. Restored to 300s; the interval now throttles
# attempts, not just successes, so a failing reingest cannot spin.
_MIN_REINGEST_INTERVAL = _env_int("BEAGLE_MIN_REINGEST_INTERVAL_SECONDS", 300)

# Maximum staleness age before forcing reingestion (seconds)
_MAX_STALE_AGE = _env_int("BEAGLE_MAX_STALE_AGE_SECONDS", 3600)

# Strong references to fire-and-forget reingest tasks.
#
# B-26 (audit v13.22.1): asyncio only holds a weak reference to a running
# task. `trigger_reingest_async` returns the task, but its callers just log
# the name and drop it, so the task could be garbage-collected mid-flight.
# Tasks are added here on creation and discarded by a done-callback.
_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()

# D-04 (release audit 2026-08-29): test-only kill switch for the DEFAULT
# (production sidecar file) reingest path only. A test that isolates its own
# tracker with an explicit, non-default `staleness_file` — the pattern every
# existing reingest test already follows to stay off the real project's
# sidecar — is never affected by this flag; only a caller reaching the
# production singleton against the real on-disk file is. This is what let a
# test that neither isolates its tracker nor mocks `hotswap_ingest` spawn a
# real, unbounded CAST/Kuzu rebuild of the whole codebase mid-suite.
_DEFAULT_REINGEST_DISABLED = False


def set_default_reingest_disabled(disabled: bool) -> None:
    """Enable or disable automatic reingest for the DEFAULT tracker only.

    Test-only hook. A tracker instance pointed at an explicit, non-default
    ``staleness_file`` (every existing reingest test already does this) is
    unaffected — this gates only the production singleton reached via the
    real on-disk sidecar file, never a test-isolated one.

    Args:
        disabled: True to refuse automatic reingest on the default tracker.
    """
    global _DEFAULT_REINGEST_DISABLED
    _DEFAULT_REINGEST_DISABLED = disabled


def _is_default_sidecar(tracker: RAGStalenessTracker) -> bool:
    """True if `tracker` is pointed at the real, on-disk sidecar file.

    False for any tracker a test constructed with an explicit
    ``staleness_file`` override, which is how every existing reingest test
    already isolates itself from the production state.
    """
    return str(tracker._file) == str(Path(_STALENESS_FILE))  # type: ignore[has-type]


def _seconds_since(epoch: float) -> float:
    """Seconds elapsed since a *persisted* wall-clock epoch.

    Wall clock BY NECESSITY, despite the interval rule: these epochs live in
    the sidecar JSON and must stay comparable across process restarts AND
    reboots, where ``time.monotonic()``'s baseline is meaningless. Computed
    via UTC datetimes so a DST shift or backwards NTP step degrades the age
    estimate instead of corrupting bookkeeping (negative ages are clamped).
    This helper is the single sanctioned wall-interval site in this module.
    """
    delta = datetime.now(UTC) - datetime.fromtimestamp(epoch, UTC)
    return max(0.0, delta.total_seconds())


# v13.22.4 heat fix: live reingest threads + a *bounded* exit-join.
# A plain daemon thread dies the instant the CLI exits, which meant a
# render-hints-triggered rebuild was killed mid-staging every tick and the
# index never actually converged. concurrent.futures' atexit join was the
# opposite extreme (process hung for the whole multi-minute embed). The
# compromise: register an atexit hook that joins in-flight reingest threads
# with a timeout (BEAGLE_REINGEST_EXIT_WAIT_SECONDS, default 120s). Typical
# post-deploy deltas (small, thanks to the content-hash delta fix) finish in
# seconds; a pathological storm is cut off at the cap instead of hanging the
# cron child for 20 minutes. An aborted attempt leaves only the staging dir
# dirty, which the next stage_ingest wipes — live stores are untouched until
# the final atomic swap.
_LIVE_REINGEST_THREADS: set[threading.Thread] = set()
_EXIT_JOIN_REGISTERED = False

#: Threads whose result-delivery failed because the caller's event loop closed
#: before ``call_soon_threadsafe`` could run. The result is genuinely lost (the
#: fire-and-forget caller has moved on), but the loss must stay observable —
#: health reporting reads this counter the same way it reads
#: observability.logging.trace_correlation_failures(). A bare ``pass`` here
#: would make delivery failures invisible (SP-1 doctrine gate).
_LOST_REINGEST_RESULTS = 0


def lost_reingest_results() -> int:
    """Count of reingest results that could not be delivered to their caller."""
    global _LOST_REINGEST_RESULTS
    return _LOST_REINGEST_RESULTS


def _join_pending_reingests() -> None:
    """Join in-flight reingest threads at exit, bounded by a total timeout."""
    timeout = _env_int(
        "BEAGLE_REINGEST_EXIT_WAIT_SECONDS",
        120,
    )
    # In-process join budget: monotonic is required here (doctrine floor) —
    # this deadline never leaves the process, unlike the persisted sidecar
    # epochs consumed by _seconds_since().
    deadline = time.monotonic() + timeout
    for t in list(_LIVE_REINGEST_THREADS):
        remaining = max(0.0, deadline - time.monotonic())
        t.join(timeout=remaining)


def _ensure_exit_join_hook() -> None:
    global _EXIT_JOIN_REGISTERED
    if not _EXIT_JOIN_REGISTERED:
        import atexit

        atexit.register(_join_pending_reingests)
        _EXIT_JOIN_REGISTERED = True


@dataclass
class StalenessRecord:
    """Tracks the staleness state of the RAG index.

    Attributes:
        stale: Whether RAG data is considered stale.
        marked_at: Epoch timestamp when staleness was marked.
        reason: Why RAG was marked stale (e.g., 'context_fold').
        last_reingested_at: Epoch timestamp of last successful reingestion.
        last_attempt_at: Epoch timestamp of the last reingestion *attempt*,
            successful or not. Throttling keys off this so a persistently
            failing reingest cannot be retried in a tight loop (B-1).
        reingest_count: Number of reingestions performed.
        codebase_path: Path to the codebase that was last ingested.

    """

    stale: bool = False
    marked_at: float = 0.0
    reason: str = ""
    last_reingested_at: float = 0.0
    last_attempt_at: float = 0.0
    reingest_count: int = 0
    codebase_path: str = ""
    # v13.22.4 heat fix: cheap change-detector for codebase_path
    # ("{file_count}:{total_size}:{max_mtime_ns}"), captured at mark_fresh()
    # time. When it matches the live tree, _sync_reingest skips the ingest
    # entirely instead of rebuilding an identical index every TTL expiry.
    last_fingerprint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StalenessRecord:
        return cls(
            stale=data.get("stale", False),
            marked_at=data.get("marked_at", 0.0),
            reason=data.get("reason", ""),
            last_reingested_at=data.get("last_reingested_at", 0.0),
            last_attempt_at=data.get("last_attempt_at", 0.0),
            reingest_count=data.get("reingest_count", 0),
            codebase_path=data.get("codebase_path", ""),
            last_fingerprint=data.get("last_fingerprint", ""),
        )


class RAGStalenessTracker:
    """Tracks RAG data staleness and triggers hot-swap reingestion.

    Thread-safe singleton pattern. The staleness record is persisted
    to a JSON sidecar file so it survives context compaction.
    """

    _instance: RAGStalenessTracker | None = None
    _initialized: bool = False
    _flight_lock: threading.Lock
    _in_flight: bool = False

    def __new__(cls, *_args: Any, **_kwargs: Any) -> RAGStalenessTracker:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = (
                False  # In-flight guard (B-1). Created here rather than in __init__
            )
            # because __init__ early-returns on the singleton re-entry path.
            cls._instance._flight_lock = threading.Lock()
            cls._instance._in_flight = False
        return cls._instance

    # ── In-flight guard (B-1) ─────────────────────────────────────────────

    def _acquire_reingest_slot(self) -> bool:
        """Claim the single reingest slot, recording the attempt time.

        Returns False if a reingest is already running. This is the guard
        that stops `rag_search` from spawning an unbounded number of
        concurrent full re-index pipelines: `can_reingest()` alone was
        insufficient because it only advanced on *success*, so nothing
        throttled while a multi-minute reindex was in flight.
        """
        with self._flight_lock:
            if self._in_flight:
                return False
            self._in_flight = True
        # Record the attempt outside the flight lock (this writes the
        # sidecar file) so the critical section stays short.
        self.mark_attempt()
        return True

    def _release_reingest_slot(self) -> None:
        with self._flight_lock:
            self._in_flight = False

    @property
    def reingest_in_flight(self) -> bool:
        """True while a reingest is running (for telemetry and tests)."""
        with self._flight_lock:
            return self._in_flight

    def __init__(self, staleness_file: str | None = None) -> None:
        if self._initialized:  # Re-init only if a new staleness_file is provided and differs
            if staleness_file and staleness_file != str(self._file):  # type: ignore[has-type]
                self._file = Path(staleness_file)
                self._record = StalenessRecord()
                self._load()
            return
        self._initialized = True
        self._file = Path(staleness_file or _STALENESS_FILE)
        self._record = StalenessRecord()
        self._load()

    # ── Persistence ───────────────────────────────────────────────────────

    def _load(self) -> None:
        """Load staleness record from sidecar file."""
        if self._file.exists():
            try:
                data = json.loads(self._file.read_text(encoding="utf-8"))
                self._record = StalenessRecord.from_dict(data)
                logger.debug(
                    f"[RAGStaleness] Loaded: stale={self._record.stale}, "
                    f"reason={self._record.reason}"
                )
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"[RAGStaleness] Failed to load record: {e}")
                self._record = StalenessRecord()

    def _save(self) -> None:
        """Persist staleness record to sidecar file."""
        try:
            # Atomic write: the hydration gate reads this sidecar on every
            # tick; a partial write could flip the perceived staleness state.
            atomic_write_text(
                self._file,
                json.dumps(self._record.to_dict(), indent=2),
                mode=0o644,
            )
        except OSError as e:
            logger.warning(f"[RAGStaleness] Failed to persist record: {e}")

    # ── Public API ─────────────────────────────────────────────────────────

    @property
    def is_stale(self) -> bool:
        """Check if RAG data is stale.

        Returns True if:
        - Explicitly marked stale, OR
        - There has never been a successful reingestion, OR
        - Last reingestion was more than _MAX_STALE_AGE seconds ago

        B-24 (audit v13.22.1): the never-ingested case is now handled as
        the docstring always claimed. Previously the `last_reingested_at
        > 0` guard meant a brand-new install reported "fresh" while
        holding no index at all. This is only safe to fix alongside the
        in-flight guard and the attempt-based throttle — otherwise it
        would make every search trigger a reingest.
        """
        if self._record.stale:
            return True

        if self._record.last_reingested_at == 0:
            return True  # never successfully ingested

        age = _seconds_since(self._record.last_reingested_at)
        if age > _MAX_STALE_AGE:
            logger.info(
                f"[RAGStaleness] Auto-stale: last reingestion was "
                f"{age:.0f}s ago (max: {_MAX_STALE_AGE}s)"
            )
            return True

        return False

    @property
    def staleness_age(self) -> float:
        """Seconds since staleness was marked."""
        if self._record.marked_at == 0:
            return 0.0
        return _seconds_since(self._record.marked_at)

    @property
    def last_reingested_age(self) -> float:
        """Seconds since last successful reingestion."""
        if self._record.last_reingested_at == 0:
            return float("inf")
        return _seconds_since(self._record.last_reingested_at)

    def mark_stale(self, reason: str = "unknown") -> None:
        """Mark RAG data as stale, requiring reingestion.

        Called when:
        - Context is folded/compacted (valuable context truncated)
        - Codebase files are modified during a session
        - Manual reingestion is requested

        Args:
            reason: Why the data is being marked stale.

        """
        self._record.stale = True
        self._record.marked_at = time.time()
        self._record.reason = reason
        self._save()
        logger.info(f"[RAGStaleness] Marked stale: {reason}")

    def mark_attempt(self) -> None:
        """Record that a reingestion attempt is starting.

        B-1: throttling must advance on *attempts*, not only successes.
        Without this a permanently failing reingest (e.g. B-2's missing
        symbol) is retried on every single search.
        """
        self._record.last_attempt_at = time.time()
        self._save()

    def mark_fresh(self, codebase_path: str = "") -> None:
        """Mark RAG data as fresh after successful reingestion.

        Args:
            codebase_path: Path to the codebase that was reingested.

        """
        self._record.stale = False
        self._record.last_reingested_at = time.time()
        self._record.reingest_count += 1
        self._record.codebase_path = codebase_path
        if codebase_path:
            self._record.last_fingerprint = _target_fingerprint(codebase_path)
        else:
            self._record.last_fingerprint = ""
        self._record.marked_at = 0.0
        self._record.reason = ""
        self._save()
        logger.info(f"[RAGStaleness] Marked fresh: reingestion #{self._record.reingest_count}")

    def can_reingest(self) -> bool:
        """Check if enough time has passed since last reingestion.

        Prevents thrashing from repeated reingestion attempts.

        B-1: keyed off the most recent *attempt or success*, whichever is
        later. Keying off successes alone let a failing reingest retry on
        every call, and let new triggers pile up while one was in flight.

        Returns:
            True if reingestion is allowed.

        """
        last_activity = max(self._record.last_reingested_at, self._record.last_attempt_at)
        if last_activity == 0:
            return True  # Never attempted — allow

        elapsed = _seconds_since(last_activity)
        if elapsed >= _MIN_REINGEST_INTERVAL:
            return True

        logger.debug(
            f"[RAGStaleness] Reingestion throttled: "
            f"{elapsed:.0f}s < {_MIN_REINGEST_INTERVAL}s minimum"
        )
        return False

    async def trigger_reingest_if_stale(
        self,
        codebase_path: str | None = None,
    ) -> dict[str, Any]:
        """Trigger hot-swap reingestion if RAG data is stale.

        This is the primary integration point: hydration calls this
        before querying RAG, and if data is stale, it runs hotswap_ingest.

        Args:
            codebase_path: Override codebase path (defaults to
                BEAGLE_KNOWLEDGE_DIR parent project).

        Returns:
            Dict with reingest result, or {"status": "skipped"} if not needed.

        """
        if _DEFAULT_REINGEST_DISABLED and _is_default_sidecar(self):
            return {"status": "skipped", "reason": "disabled_for_tests"}

        if not self.is_stale:
            return {"status": "skipped", "reason": "not_stale"}

        if not self.can_reingest():
            return {
                "status": "skipped",
                "reason": f"throttled: {self.last_reingested_age:.0f}s < {_MIN_REINGEST_INTERVAL}s",
            }

        if not self._acquire_reingest_slot():
            return {"status": "skipped", "reason": "reingest already in flight"}

        try:
            # `_sync_reingest` is blocking CPU/IO work; keep it off the loop.
            return await asyncio.to_thread(self._sync_reingest, codebase_path)
        finally:
            self._release_reingest_slot()

    def trigger_reingest_async(
        self,
        codebase_path: str | None = None,
    ) -> asyncio.Task | None:
        """Fire-and-forget hot-swap reingestion — does NOT block the caller.

        v13.19.5: The previous `await tracker.trigger_reingest_if_stale()`
        pattern in hydration_node blocked the workflow for 30+ seconds
        (full CAST pipeline on 442 source files). The caller (workflow
        RAG query) couldn't make progress and the user observed a hang.

        This method:
          1. Returns `None` immediately if the tracker is not stale or
             cannot reingest (throttled), so callers pay zero cost.
          2. Otherwise schedules a background `asyncio.Task` that runs
             the reingest in a worker thread (since `hotswap_ingest` is
             synchronous CPU/IO work and would otherwise block the
             event loop).
          3. Publishes a `RAGStale` event on the EventBus for telemetry
             before kicking off the task, so subscribers see liveness.
          4. The task itself swallows any exception and logs it; it
             never propagates back to the caller (caller already
             returned).

        Args:
            codebase_path: Override codebase path (defaults to
                BEAGLE_KNOWLEDGE_DIR parent project).

        Returns:
            The asyncio.Task running the reingest, or None if skipped.
            Callers should NOT await the returned task — fire and forget.

        """
        if _DEFAULT_REINGEST_DISABLED and _is_default_sidecar(self):
            logger.debug(
                "[RAGStaleness] trigger_reingest_async: disabled for the default "
                "tracker (test mode), skipping"
            )
            return None

        if not self.is_stale:
            logger.debug("[RAGStaleness] trigger_reingest_async: not stale, skipping")
            return None

        if not self.can_reingest():
            logger.debug("[RAGStaleness] trigger_reingest_async: throttled, skipping")
            return None

        # B-1: claim the single reingest slot BEFORE scheduling anything.
        # This is what bounds concurrency — `can_reingest()` above cannot,
        # because it only sees persisted timestamps, not the task that is
        # already running.
        if not self._acquire_reingest_slot():
            logger.debug("[RAGStaleness] trigger_reingest_async: already in flight, skipping")
            return None

        # Publish a RAGStale event before kicking off the task, so any
        # subscriber (e.g. the workflow heartbeat bridge) sees liveness.
        try:
            from ..events.bus import get_event_bus

            # Lazy import of the event class to avoid circular deps.
            _RAGStaleEvent: type[Any] | None
            try:
                from ..events.events import RAGStale as _RAGStaleEvent
            except ImportError:
                _RAGStaleEvent = None

            if _RAGStaleEvent is not None:
                get_event_bus().publish(
                    _RAGStaleEvent(
                        workflow_id="rag_staleness",
                        trigger="auto",
                        codebase_path=codebase_path or "",
                    )
                )
        except Exception as _evt_exc:  # ruff: ignore[BLE001]  # broad: telemetry must never block work
            logger.debug(f"[RAGStaleness] RAGStale event publish skipped: {_evt_exc}")

        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()

        def _set_result(res: dict[str, Any]) -> None:
            if not fut.done():
                fut.set_result(res)

        # v13.22.4 heat fix: run the reingestion on a *daemon* thread instead
        # of asyncio.to_thread. concurrent.futures registers an atexit hook
        # that joins its worker threads, so a CLI process (e.g. the hourly
        # `beagle render-hints` cron child) stayed alive for the entire
        # multi-minute embed even though the task was fire-and-forget — the
        # "stuck embedding loop". A daemon thread dies with the process; the
        # half-written staging dir is rmtree'd by the next stage_ingest call,
        # and the live stores are only touched during the final atomic swap.
        def _work() -> None:
            try:
                try:
                    res = self._sync_reingest(codebase_path)
                finally:
                    # Free the slot whatever happened, or the tracker would
                    # refuse every future reingest for the life of the process.
                    self._release_reingest_slot()
                    _LIVE_REINGEST_THREADS.discard(thread)
            except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional — background thread: log and bridge the failure back
                logger.error(f"[RAGStaleness] Background reingest failed: {e}")
                res = {"status": "error", "error": str(e)}
            try:
                if not loop.is_closed():
                    loop.call_soon_threadsafe(_set_result, res)
                else:
                    raise RuntimeError("event loop closed before result delivery")
            except RuntimeError:
                # Loop already closed — the fire-and-forget caller moved on and
                # the result is undeliverable. The failure is recorded via the
                # module counter (readable through lost_reingest_results())
                # because logging from here risks nothing but observability
                # requires the loss to be counted, not silent (SP-1).
                global _LOST_REINGEST_RESULTS
                _LOST_REINGEST_RESULTS += 1

        thread = threading.Thread(
            target=_work,
            name=f"beagle-rag-reingest-{codebase_path or 'default'}",
            daemon=True,
        )
        # Bounded exit-join (see _LIVE_REINGEST_THREADS above): lets a normal
        # rebuild finish before the CLI exits without ever hanging for the
        # full duration of a pathological one.
        _LIVE_REINGEST_THREADS.add(thread)
        _ensure_exit_join_hook()

        async def _runner() -> dict[str, Any]:
            thread.start()
            return await fut

        try:
            task = asyncio.create_task(
                _runner(),
                name=f"beagle.rag_reingest.{codebase_path or 'default'}",
            )
        except RuntimeError:
            # No running event loop (sync caller). Release the slot we took
            # so a later async caller is not locked out forever.
            self._release_reingest_slot()
            logger.debug("[RAGStaleness] trigger_reingest_async: no running event loop")
            return None

        # B-26: retain a strong reference until the task completes.
        _BACKGROUND_TASKS.add(task)
        task.add_done_callback(_BACKGROUND_TASKS.discard)

        logger.info(f"[RAGStaleness] Scheduled background reingest (task={task.get_name()})")
        return task

    def _sync_reingest(self, codebase_path: str | None) -> dict[str, Any]:
        """Inner sync entry point used by `trigger_reingest_async`.

        Mirrors the body of the original `trigger_reingest_if_stale`,
        minus the `async`/`await` machinery. Returns the same dict
        shape so any caller that wanted a result can still await the
        task returned by `trigger_reingest_async`.
        """
        from ..utils.env_manager import get_workspace_root as _get_ws

        target = codebase_path or os.environ.get(
            "BEAGLE_PROJECT_ROOT",
            str(_get_ws()),
        )

        logger.info(f"[RAGStaleness] Background reingest starting for: {target}")

        try:
            from ..infrastructure.hotswap_ingest import hotswap_ingest

            # v13.22.4 heat fix: fingerprint gate. The TTL-based is_stale
            # fires every hour by construction; combined with mtime-resetting
            # redeploys this produced a full ~5k-chunk re-embed (19 min of
            # llama-server at ~600% CPU → 90 °C package) on far too many
            # ticks. If the target tree is byte-identical since the last
            # successful ingest AND the live index opens cleanly, there is
            # nothing to rebuild.
            fp_now = _target_fingerprint(target)
            if (
                self._record.last_fingerprint
                and fp_now == self._record.last_fingerprint
                and _index_usable()
            ):
                logger.info(
                    "[RAGStaleness] Reingest skipped: target unchanged since "
                    "last ingestion and live index healthy"
                )
                self.mark_fresh(codebase_path=target)
                return {"status": "skipped_unchanged"}

            result = hotswap_ingest(target)

            if result.get("status") == "ok":
                self.mark_fresh(codebase_path=target)
                return {
                    "status": "reingested",
                    "result": result,
                }

            return {
                "status": "failed",
                "error": result.get("error", "unknown"),
                "phase": result.get("phase", "unknown"),
            }
        except ImportError:
            logger.warning("[RAGStaleness] hotswap_ingest module not available")
            return {"status": "error", "error": "hotswap_ingest not available"}
        except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
            logger.error(f"[RAGStaleness] Background reingest failed: {e}")
            return {"status": "error", "error": str(e)}

    def get_status(self) -> dict[str, Any]:
        """Get current staleness status for telemetry.

        Returns:
            Dict with staleness details.

        """
        return {
            "stale": self._record.stale,
            "reason": self._record.reason,
            "marked_at": self._record.marked_at,
            "staleness_age_seconds": round(self.staleness_age, 1),
            "last_reingested_at": self._record.last_reingested_at,
            "last_attempt_at": self._record.last_attempt_at,
            "reingest_count": self._record.reingest_count,
            "codebase_path": self._record.codebase_path,
            "can_reingest": self.can_reingest(),
            "reingest_in_flight": self.reingest_in_flight,
        }


# ── Module-level helpers ──────────────────────────────────────────────────────


def _target_fingerprint(target: str) -> str:
    """Cheap change-detector for *target*: "{count}:{total_size}:{max_mtime_ns}".

    Excludes ``__pycache__`` / bytecode (churns on import without any content
    change). Returns "" when the target is missing or unreadable so callers
    fail open (i.e. proceed with the ingest).
    """
    root = Path(target)
    if not root.is_dir():
        return ""
    count = 0
    total_size = 0
    newest_ns = 0
    try:
        for p in root.rglob("*"):
            if not p.is_file() or "__pycache__" in p.parts:
                continue
            if p.suffix in {".pyc", ".pyo"}:
                continue
            st = p.stat()
            count += 1
            total_size += st.st_size
            if st.st_mtime_ns > newest_ns:
                newest_ns = st.st_mtime_ns
    except OSError:
        return ""
    return f"{count}:{total_size}:{newest_ns}"


def _index_usable() -> bool:
    """True when the live LanceDB table opens cleanly.

    Used to avoid skipping a reingest that would repair a torn index.
    Any probe failure returns False (→ do the ingest; correctness over quiet).
    """
    try:
        from ..infrastructure.hotswap_ingest import _live_lance_is_healthy
        from ..infrastructure.rag_paths import LANCE_TABLE_NAME, db_root

        live_table = Path(db_root()) / "lancedb" / f"{LANCE_TABLE_NAME}.lance"
        return _live_lance_is_healthy(live_table)
    except Exception:  # ruff: ignore[BLE001]  # broad catch intentional — probe must never crash the gate
        return False


# ── Module-level convenience ──────────────────────────────────────────────────

_tracker: RAGStalenessTracker | None = None


def get_staleness_tracker(
    staleness_file: str | None = None,
) -> RAGStalenessTracker:
    """Get or create the singleton staleness tracker."""
    global _tracker
    if _tracker is None:
        _tracker = RAGStalenessTracker(staleness_file=staleness_file)
    return _tracker


def reset_staleness_tracker() -> None:
    """Reset the singleton tracker (for testing)."""
    global _tracker
    _tracker = None
    RAGStalenessTracker._instance = None
