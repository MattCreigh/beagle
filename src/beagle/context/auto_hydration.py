"""Auto-Hydration — Ensures RAG is fresh on goose startup.

When a new goose instance starts, this module:
  1. Detects the project root (CWD or walks up to find .git/pyproject.toml)
  2. Checks RAG staleness via RAGStalenessTracker
  3. Triggers hot-swap reingestion if stale or forced
  4. Verifies Kùzu graph has meaningful data (not just test fixtures)
  5. Ensures CLAUDE.md and other .md files are in scope

Integration: call auto_hydrate_sync() from the goose session startup hook.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from beagle.security.validation import validate_cypher_identifier

logger = logging.getLogger("Beagle.context.auto_hydration")

# ── Configuration ──────────────────────────────────────────────────────────────


def _resolve_project_root() -> str:
    """Lazily resolve project root via canonical env_manager."""
    from ..utils.env_manager import get_workspace_root

    return str(get_workspace_root())


_DEFAULT_PROJECT_ROOT = os.environ.get("BEAGLE_PROJECT_ROOT") or _resolve_project_root()


@dataclass
class AutoHydrationConfig:
    """Configuration for auto-hydration on startup."""

    project_dir: str = ""  # Empty = auto-detect from CWD
    force: bool = False  # Force reingest even if fresh
    check_claude_md: bool = True  # Also check CLAUDE.md existence/freshness
    timeout_seconds: int = 300  # Max seconds to wait for reingestion
    # v13.22.3: when True, the reingest is scheduled on a background
    # asyncio task via ``RAGStalenessTracker.trigger_reingest_async``
    # (fire-and-forget — the call returns immediately, the hot-swap
    # runs in a worker thread, the caller's event loop is never
    # blocked). When False (the default), ``auto_hydrate`` waits
    # synchronously for the full hot-swap to complete. Use True for
    # session-start hooks and tool-time triggers; use False for
    # explicit one-off invocations that want a result before
    # returning.
    fire_and_forget: bool = False
    # Low-limit background posture (2026-08-22 directive): a background
    # reingest must be SLOW but CONTINUOUS — small embed batches with an
    # inter-batch pause, so it can never saturate the shared local
    # embedding runner. Values are applied to the embedder for the
    # duration of the background ingest only; foreground paths keep the
    # [embed] SSOT values.
    background_embed_batch_size: int = 8
    background_embed_pause_s: float = 2.0


@dataclass
class HydrationResult:
    """Result of an auto-hydration check or reingestion.

    v13.22.3: ``reingest_task`` carries the name of the background
    asyncio.Task when the fire-and-forget path scheduled one. When
    the reingest completes, the task's name still resolves to it
    via ``asyncio.Task.get_name()``; check ``asyncio.all_tasks()`` to
    find it. The result's ``status`` field is set to
    ``"reingest_scheduled"`` in this case (vs. ``"reingested"`` for
    the blocking path which waited for completion).
    """

    status: str = "skipped"  # "fresh" | "reingested" | "reingest_scheduled" | "error" | "skipped"
    chunks_created: int = 0
    relations_extracted: int = 0
    files_processed: int = 0
    kuzu_nodes: int = 0
    kuzu_edges: int = 0
    elapsed_seconds: float = 0.0
    errors: list[str] = field(default_factory=list)
    reingest_task: str | None = None  # name of background task, if any


# ── Project Root Detection ─────────────────────────────────────────────────────

_PROJECT_MARKERS = {".git", "pyproject.toml", "setup.py", "Cargo.toml", "go.mod"}


def get_project_root() -> Path:
    """Walk up from CWD to find a project root directory.

    Looks for common project markers (.git, pyproject.toml, etc.).
    Falls back to CWD if no marker is found.
    """
    start = Path(os.environ.get("BEAGLE_PROJECT_ROOT", os.getcwd())).resolve()

    # If start is already a project root, return it
    if any((start / marker).exists() for marker in _PROJECT_MARKERS):
        return start

    # Walk up
    current = start
    for _ in range(20):  # Safety limit
        parent = current.parent
        if parent == current:
            break
        if any((parent / marker).exists() for marker in _PROJECT_MARKERS):
            return parent
        current = parent

    logger.warning(f"[AutoHydration] No project root found, using CWD: {start}")
    return start


# ── Hydration Check ────────────────────────────────────────────────────────────

KUZU_MIN_RATIO = 0.5  # Kùzu nodes should be >= 50% of LanceDB chunks


def should_hydrate(project_dir: Path | None = None) -> tuple[bool, str]:
    """Check if hydration is needed.

    Returns:
        Tuple of (is_needed: bool, reason: str)

    """
    # rag_staleness is effectively an optional dependency at this call site: a
    # partial venv or a race during install can make the import (or the
    # tracker construction) raise. The contract is to degrade, not crash —
    # callers fall through to the blocking hotswap path and still get a
    # result. Unguarded, the ImportError propagated all the way out of
    # auto_hydrate() and killed hydration outright.
    # If staleness cannot be determined, assume stale: re-ingesting
    # unnecessarily is cheap, skipping a needed ingest is not.
    try:
        from .rag_staleness import get_staleness_tracker

        tracker = get_staleness_tracker()
    except (
        ImportError,
        AttributeError,
    ) as exc:  # catch: NARROWED  # RATIONALE=two-tuple: ImportError for a missing/partial rag_staleness module, AttributeError when the module imports but the factory is absent mid-install.
        logger.warning("Staleness tracker unavailable (%s) — assuming RAG is stale", exc)
        return True, f"staleness tracker unavailable: {exc}"

    # Check 1: Is RAG data stale?
    if tracker.is_stale:
        return True, f"RAG marked stale: {tracker._record.reason or 'unknown'}"

    # Check 2: Has RAG ever been hydrated?
    if tracker._record.reingest_count == 0:
        return True, "RAG has never been hydrated"

    # Check 3: Is the codebase path different from what was last ingested?
    root = project_dir or get_project_root()
    if tracker._record.codebase_path and str(root) != tracker._record.codebase_path:
        return True, f"Codebase path changed: {tracker._record.codebase_path} -> {root}"

    # Check 4: Is the staleness data too old?
    if tracker.last_reingested_age > 3600:  # 1 hour
        return True, f"RAG data is {tracker.last_reingested_age:.0f}s old"

    return False, "RAG data is fresh"


def _check_kuzu_health() -> tuple[int, int, list[str]]:
    """Check Kùzu graph health by reading node/edge counts.

    Returns:
        Tuple of (node_count, edge_count, errors)

    """
    errors: list[str] = []

    # Try to read from the Kùzu database
    kuzu_path = os.environ.get(
        "BEAGLE_RAG_KUZU_PATH",
        str(Path.home() / ".beagle" / "rag_kuzu"),
    )
    kuzu_path = Path(kuzu_path)  # type: ignore[assignment]

    if not kuzu_path.exists():  # type: ignore[attr-defined]
        errors.append(f"Kùzu database not found at {kuzu_path}")
        return 0, 0, errors

    try:
        import kuzu

        db = kuzu.Database(str(kuzu_path), read_only=True)
        conn = kuzu.Connection(db)

        # Count nodes
        try:
            result = conn.execute("MATCH (n:ASTNode) RETURN count(n) AS cnt")
            nodes = result.get_next()[0] if result.has_next() else 0  # type: ignore[index,union-attr]
        except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
            nodes = 0
            errors.append(f"Kùzu node count query failed: {e}")

        # Count edges — try each relationship type from allowlist
        _ALLOWED_REL_TYPES = frozenset({"CALLS", "INHERITS_FROM", "IMPORTS", "CONTAINS"})
        total_edges = 0
        for rel_type in ("CALLS", "INHERITS_FROM", "IMPORTS", "CONTAINS"):
            if rel_type not in _ALLOWED_REL_TYPES:
                continue
            try:
                # Kùzu requires relationship type as an identifier in MATCH; validate against
                # the allowlist first, then the shared Cypher identifier gate as defense in depth.
                validate_cypher_identifier(rel_type)
                # Kùzu requires relationship type as an identifier in MATCH; validated above
                query = "MATCH ()-[r:" + rel_type + "]->() RETURN count(r) AS cnt"
                result = conn.execute(query)
                cnt = result.get_next()[0] if result.has_next() else 0  # type: ignore[index,union-attr]
                total_edges += cnt
            except (RuntimeError, ValueError, TypeError, AttributeError, IndexError) as exc:
                # A relationship table that does not exist yet is the normal case
                # on a fresh graph, but any other failure means the edge count is
                # low and the hydration decision below is made on bad numbers.
                logger.warning(
                    "Cannot count %s edges in the graph (%s); the edge total is "
                    "understated and may trigger an unnecessary reingestion.",
                    rel_type,
                    exc,
                )

        conn.close()
        return nodes, total_edges, errors

    except ImportError:
        errors.append("kuzu package not available")
        return 0, 0, errors
    except RuntimeError as e:
        if "lock" in str(e).lower():
            errors.append(f"Kùzu is locked by another process: {e}")
            # Try to get counts from status file instead
            return _kuzu_counts_from_status()
        errors.append(f"Kùzu runtime error: {e}")
        return 0, 0, errors
    except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
        errors.append(f"Kùzu check failed: {e}")
        return 0, 0, errors


def _kuzu_counts_from_status() -> tuple[int, int, list[str]]:
    """Fallback: try to read Kùzu counts from RAG status/staleness files."""
    # Check if we can get info from the staleness record
    try:
        from .rag_staleness import get_staleness_tracker

        tracker = get_staleness_tracker()
        tracker.get_status()
        # No direct node/edge counts in staleness, but we can report what we know
        return 0, 0, ["Kùzu locked; counts unavailable"]
    except Exception:  # ruff: ignore[BLE001]  # broad catch intentional
        return 0, 0, ["Kùzu locked; staleness unavailable"]


# ── Main Hydration Logic ──────────────────────────────────────────────────────


async def auto_hydrate(config: AutoHydrationConfig | None = None) -> HydrationResult:
    """Main entry point: check RAG freshness and rehydrate if needed.

    This should be called on goose startup to ensure RAG data is available.
    """
    start = time.monotonic()
    config = config or AutoHydrationConfig()

    project_dir = Path(config.project_dir) if config.project_dir else get_project_root()
    result = HydrationResult()

    logger.info(f"[AutoHydration] Starting hydration check for: {project_dir}")

    # Step 1: Check if hydration is needed
    needed, reason = should_hydrate(project_dir)

    if not needed and not config.force:
        # Still verify Kùzu health even if "fresh"
        nodes, edges, errors = _check_kuzu_health()
        result.kuzu_nodes = nodes
        result.kuzu_edges = edges
        result.errors.extend(errors)

        # If Kùzu has suspiciously few nodes, force reingestion
        try:
            from ..infrastructure.cast_ingestion import scan_codebase

            files = scan_codebase(project_dir)
            result.files_processed = len(files)
            if nodes < len(files) * KUZU_MIN_RATIO and nodes < 50:
                logger.warning(
                    f"[AutoHydration] Kùzu has only {nodes} nodes for "
                    f"{len(files)} source files — forcing reingestion"
                )
                needed = True
                reason = f"Kùzu underpopulated: {nodes} nodes for {len(files)} files"
        except ImportError as exc:
            logger.warning(
                "Cannot import the graph client to check Kùzu population (%s); the "
                "underpopulation check is skipped and a stale graph may go unnoticed.",
                exc,
            )

    if not needed and not config.force:
        result.status = "fresh"
        result.elapsed_seconds = time.monotonic() - start
        logger.info(f"[AutoHydration] RAG data is fresh ({reason})")
        return result

    logger.info(f"[AutoHydration] Rehydration needed: {reason}")

    # Step 2: Trigger hot-swap reingestion.
    #
    # v13.22.3: two execution paths, selected by config.fire_and_forget:
    #
    #  - fire_and_forget=True  (NEW default for session-start hooks):
    #    route through ``RAGStalenessTracker.trigger_reingest_async``,
    #    which returns a background ``asyncio.Task`` immediately. The
    #    caller's event loop is never blocked; the actual hot-swap
    #    runs in a worker thread (asyncio.to_thread under the hood)
    #    and updates the staleness state when complete. This is the
    #    pattern documented in rag_staleness.py:359 — the v13.19.5
    #    audit found that the blocking call here caused 30+ second
    #    hangs in production. The session-start hook now uses this
    #    path; the dag.py:881 and dag.py:1057 callers (which DO need
    #    a synchronous result) keep the blocking path via
    #    config.fire_and_forget=False.
    #
    #  - fire_and_forget=False (DEFAULT, preserves existing behaviour):
    #    direct hotswap_ingest() call, synchronous, blocks until the
    #    full reingest completes (up to timeout_seconds). Used by the
    #    orchestrator's "do the next workflow step" path where the
    #    caller wants the reingest result before proceeding.
    if config.fire_and_forget:
        # v13.22.3: true fire-and-forget via a daemon thread. We
        # previously tried ``tracker.trigger_reingest_async`` here
        # but it requires a persistent asyncio event loop — inside
        # ``asyncio.run()`` the loop is torn down as soon as this
        # coroutine returns, which kills the background task
        # mid-ingest and surfaces as "Received signal 15, cleaning
        # up..." from the orchestrator. A plain thread does not
        # have that lifecycle coupling; the caller returns
        # immediately, the daemon thread runs the ingest to
        # completion in the background, and the hot-swap itself
        # handles the live RAG-server read-only Kùzu handle via
        # ``_release_rag_connections`` (RC2/RC3 fix from 2f94ab7).
        try:
            from .rag_staleness import get_staleness_tracker

            tracker = get_staleness_tracker()
            # mark_stale first so any concurrent check sees stale and
            # knows a reingest is in flight (avoids double-ingest).
            tracker.mark_stale(reason="auto_hydrate_fire_and_forget")
            # Spawn the daemon thread. Daemon=True so a hard
            # interpreter shutdown doesn't hang waiting for the
            # ingest; the reingest is best-effort in that case.
            import threading

            def _background_reingest() -> None:
                try:
                    from ..infrastructure.hotswap_ingest import (
                        hotswap_ingest,
                    )

                    # Apply the low-limit background posture (config SSOT via
                    # AutoHydrationConfig): small batches + inter-batch pause
                    # so the chunked ingest stays slow but continuous.
                    from ..infrastructure.services import embedding as _emb

                    _emb.apply_background_posture(
                        batch_size=config.background_embed_batch_size,
                        pause_s=config.background_embed_pause_s,
                    )
                    logger.info(
                        f"[AutoHydration] Background reingest starting (project_dir={project_dir})"
                    )
                    result_inner = hotswap_ingest(str(project_dir), keep_backup=True)
                    if result_inner.get("status") == "ok":
                        tracker.mark_fresh(codebase_path=str(project_dir))
                        logger.info(
                            f"[AutoHydration] Background reingest "
                            f"complete: {result_inner.get('stage', {}).get('files_processed', '?')} files, "
                            f"{result_inner.get('stage', {}).get('chunks_created', '?')} chunks"
                        )
                    else:
                        logger.error(
                            f"[AutoHydration] Background reingest "
                            f"failed: {result_inner.get('error', 'unknown')}"
                        )
                except Exception as bg_exc:  # broad catch intentional
                    logger.error(
                        f"[AutoHydration] Background reingest crashed: {bg_exc}",
                        exc_info=True,
                    )

            thread_name = f"beagle.rag_reingest.{Path(project_dir).name}"
            bg_thread = threading.Thread(
                target=_background_reingest,
                name=thread_name,
                daemon=True,
            )
            bg_thread.start()
            result.status = "reingest_scheduled"
            result.reingest_task = thread_name
            logger.info(
                f"[AutoHydration] Scheduled background reingest (thread={thread_name}, daemon=True)"
            )
            # Skip the blocking path; return early.
            result.elapsed_seconds = time.monotonic() - start
            return result
        except ImportError as e:
            logger.error(
                f"[AutoHydration] Cannot import rag_staleness: {e}; "
                "falling back to blocking hotswap_ingest"
            )
            # Fall through to blocking path
        except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
            logger.error(
                f"[AutoHydration] Fire-and-forget path failed: {e}; "
                "falling back to blocking hotswap_ingest"
            )
            # Fall through to blocking path

    try:
        from ..infrastructure.hotswap_ingest import hotswap_ingest

        ingest_result = hotswap_ingest(str(project_dir))

        if ingest_result.get("status") == "ok":
            result.status = "reingested"
            result.chunks_created = ingest_result.get("chunks_created", 0)
            result.files_processed = ingest_result.get("files_processed", 0)
            result.relations_extracted = ingest_result.get("relations_extracted", 0)

            # Mark as fresh in the staleness tracker. This is bookkeeping
            # *after* a successful ingest, so it gets its own guard: without
            # one, an unavailable rag_staleness module raised into the outer
            # `except ImportError` below, which overwrote status="reingested"
            # with status="error" and threw away a completed ingestion. The
            # work succeeded; only the record of it failed.
            try:
                from .rag_staleness import get_staleness_tracker

                tracker = get_staleness_tracker()
                tracker.mark_fresh(codebase_path=str(project_dir))
            except (
                ImportError,
                AttributeError,
                OSError,
            ) as exc:  # catch: NARROWED  # RATIONALE=three-tuple: ImportError/AttributeError for a missing or partial rag_staleness module, OSError when the freshness record cannot be written. None of these invalidate the ingest that already succeeded.
                logger.warning(
                    "[AutoHydration] Ingest succeeded but marking freshness failed: %s", exc
                )
                result.errors.append(f"Freshness bookkeeping failed: {exc}")

            logger.info(
                f"[AutoHydration] Reingestion successful: "
                f"{result.chunks_created} chunks, {result.files_processed} files"
            )
        else:
            result.status = "error"
            result.errors.append(f"Reingestion failed: {ingest_result.get('error', 'unknown')}")
            logger.error(f"[AutoHydration] Reingestion failed: {ingest_result}")

    except ImportError as e:
        result.status = "error"
        result.errors.append(f"Import error: {e}")
        logger.error(f"[AutoHydration] Cannot import hotswap_ingest: {e}")
    except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
        result.status = "error"
        result.errors.append(f"Unexpected error: {e}")
        logger.error(f"[AutoHydration] Reingestion error: {e}")

    # Step 3: Verify Kùzu health after reingestion
    nodes, edges, kuzu_errors = _check_kuzu_health()
    result.kuzu_nodes = nodes
    result.kuzu_edges = edges
    result.errors.extend(kuzu_errors)

    # Step 4: Check CLAUDE.md if requested
    if config.check_claude_md:
        claude_md = project_dir / "CLAUDE.md"
        if not claude_md.exists():
            result.errors.append("CLAUDE.md not found in project root")
        else:
            # Check if CLAUDE.md is stale (> 7 days old modification)
            # wall-clock-ok: compares against a persisted file mtime (wall-clock
            # epoch). time.monotonic() would be WRONG here — file mtimes are
            # wall-clock, not monotonic. Same timestamp-comparison pattern as
            # semantic_knowledge.py / render.py / tools/_impl.py.
            md_mtime = claude_md.stat().st_mtime
            md_age = time.time() - md_mtime  # nosemgrep: aeca-walltime-for-interval
            if md_age > 7 * 86400:
                logger.warning(f"[AutoHydration] CLAUDE.md is {md_age / 86400:.1f} days old")

    result.elapsed_seconds = time.monotonic() - start
    logger.info(
        f"[AutoHydration] Complete: status={result.status}, "
        f"nodes={result.kuzu_nodes}, edges={result.kuzu_edges}, "
        f"elapsed={result.elapsed_seconds:.1f}s"
    )
    return result


def auto_hydrate_sync(config: AutoHydrationConfig | None = None) -> HydrationResult:
    """Synchronous wrapper for auto_hydrate."""
    try:
        asyncio.get_running_loop()
        # We're already in an async context — create a task
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(asyncio.run, auto_hydrate(config))
            return future.result(timeout=300)
    except RuntimeError:
        # No running loop — safe to use asyncio.run
        return asyncio.run(auto_hydrate(config))
