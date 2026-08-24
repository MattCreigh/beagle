"""Hot-Swap Ingestion Module for Beagle RAG Server.

Solves the Kùzu lock contention problem: the RAG MCP server holds a
read-only lock on the Kùzu database, preventing ingestion while the
server is running. This module implements a zero-downtime swap pattern:

  1. STAGE: Run CAST ingestion to a temporary staging directory
  2. RELEASE: Signal the RAG server to release DB connections
  3. SWAP: Atomically move staged data into the live RAG directory
  4. REINIT: Trigger RAG server to re-initialize connections

The RAG server's `init_connections()` is idempotent — calling it after
setting `_initialized = False` causes a full reconnect on next query.

Usage:
    from beagle.infrastructure.hotswap_ingest import (
        stage_ingest, hotswap_ingest
    )

    # Full hot-swap in one call:
    result = hotswap_ingest("/path/to/codebase")

    # Or step-by-step:
    stage_result = stage_ingest("/path/to/codebase")
    swap_result = swap_staged_to_live()
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any

# Import the rag_paths module object, not individual names, so tests can
# monkeypatch the module-level functions (e.g. db_root) and have them take
# effect here without a reload.
from . import rag_paths as _rag_paths
from ._locks import SWAP_LOCK


def staging_dir(override: str | None = None) -> str:
    return _rag_paths.staging_dir(override)


def backup_dir(override: str | None = None) -> str:
    return _rag_paths.backup_dir(override)


def db_root(root: str | None = None) -> str:
    return _rag_paths.db_root(root)


def lancedb_uri(root: str | None = None) -> str:
    return _rag_paths.lancedb_uri(root)


def kuzu_uri(root: str | None = None) -> str:
    return _rag_paths.kuzu_uri(root)


logger = logging.getLogger("Beagle.HotSwapIngest")


def _live_lance_is_healthy(live_table: Path) -> bool:
    """Return True iff the live LanceDB table opens AND full-scans cleanly.

    ``count_rows`` alone is NOT sufficient: a torn dataset (manifest
    referencing a missing fragment file — the 2026-08-23 failure mode) still
    counts rows but explodes on any scan. The probe therefore reads the
    table's first row, which forces fragment materialisation.

    Args:
        live_table: Path to the ``.lance`` table directory.

    Returns:
        False on ANY error — callers must treat False as "do not pre-seed;
        let CAST rebuild from scratch".
    """
    try:
        import lancedb

        db = lancedb.connect(str(live_table.parent))
        tbl = db.open_table(live_table.stem)
        if tbl.count_rows() == 0:
            return True  # empty-but-valid: seeding is a harmless no-op
        _ = len(tbl.to_arrow())  # forces every fragment to be read
        return True
    except Exception as err:  # noqa: BLE001 — health probe: any failure = unhealthy
        logger.warning("[HotSwap] Live LanceDB health probe failed: %s", str(err)[:160])
        return False


def _staged_kuzu_is_healthy(s_kuzu: Path) -> bool:
    """Return True iff a staged Kùzu DB opens read-only with ≥1 ASTNode.

    Guards swap_staged_to_live against promoting a partial/failed staging
    graph (e.g. buffer-pool RuntimeError mid-build) over a healthy live one.

    """
    try:
        import kuzu

        db = kuzu.Database(str(s_kuzu), read_only=True)
        conn = kuzu.Connection(db)
        query_result: Any = conn.execute("MATCH (n:ASTNode) RETURN count(n)")
        if isinstance(query_result, list):
            # Multi-statement strings return one QueryResult per statement;
            # our single statement yields at most one.
            query_result = query_result[0] if query_result else None
        if query_result is None or not query_result.has_next():
            return False
        row = query_result.get_next()
        if isinstance(row, dict):
            values = list(row.values())
        elif isinstance(row, (list, tuple)):
            values = list(row)
        else:
            return False
        return bool(values) and int(values[0]) > 0
    except Exception as err:  # noqa: BLE001 — health probe: any failure = unhealthy
        logger.warning("[HotSwap] Staged Kùzu health probe failed: %s", str(err)[:160])
        return False


def _seed_staging_from_live(staging_dir: Path) -> None:
    """Pre-seed the staging LanceDB with rows from the live LanceDB.

    v13.22.3 RC2 follow-up — incremental-hotswap row preservation.

    A hotswap's swap path runs:

        1. ``cast_ingestion.ingest(target, db_root_path=staging)`` —
           re-parses changed files, writes only those chunks to the
           staging LanceDB.
        2. ``swap_staged_to_live`` — atomically moves the staging
           table into the live directory.

    Without pre-seeding, step 1's staging table contains only the
    changed files' chunks — every unchanged file's row is missing.
    Step 2 then promotes that truncated table. The previous corpus
    is gone.

    With pre-seeding, we copy the LIVE LanceDB table (the entire
    ``.lance`` directory) into the staging dir BEFORE step 1 runs.
    Step 1's ``upsert_lancedb_chunks`` then opens the pre-seeded
    table, deletes rows for the changed files, and appends the
    freshly-embedded chunks. Rows for unchanged files survive the
    upsert because they are never touched.

    For the very first hotswap (no live LanceDB yet), the live
    table simply doesn't exist and we silently skip — the caller's
    behaviour is unchanged. cast_ingestion.ingest() falls back to
    the destructive rebuild on its partial→empty chunks list, which
    for a first run IS the full corpus (nothing in the cache), so
    the staging table is built correctly from scratch.

    Args:
        staging_dir: Empty staging directory created by stage_ingest.
            On return, ``staging_dir/lancedb/ast_code_chunks.lance/``
            is populated from the live table iff one exists.

    """
    from .rag_paths import LANCE_TABLE_NAME
    from .rag_paths import db_root as _db_root

    live_root = Path(_db_root())
    live_lance_dir = live_root / "lancedb"
    live_table = live_lance_dir / f"{LANCE_TABLE_NAME}.lance"
    if not live_table.is_dir():
        # No live data yet — first-ever ingest. Nothing to seed.
        logger.info("[HotSwap] No live LanceDB to seed from (first run)")
        return

    staging_lance_dir = staging_dir / "lancedb"
    staging_table = staging_lance_dir / f"{LANCE_TABLE_NAME}.lance"

    if os.environ.get("BEAGLE_RAG_FULL_REBUILD", "").strip().lower() in {"1", "true", "yes"}:
        logger.info(
            "[HotSwap] BEAGLE_RAG_FULL_REBUILD set — skipping pre-seed; "
            "CAST will rebuild the corpus from scratch"
        )
        return

    if not _live_lance_is_healthy(live_table):
        logger.warning(
            "[HotSwap] Live LanceDB is torn/unreadable — skipping pre-seed "
            "so CAST performs a full from-scratch build (poison will NOT "
            "be carried forward)"
        )
        return

    if staging_table.exists():
        # Defensive: stage_ingest just rmtree'd the staging dir; if
        # the live lance dir is a symlink to the staging dir (very
        # unlikely but worth guarding), bail rather than recursing.
        logger.warning(
            f"[HotSwap] staging_table already exists at {staging_table}; "
            "skipping pre-seed (would corrupt the live data)"
        )
        return

    staging_lance_dir.mkdir(parents=True, exist_ok=True)
    # copytree(..., symlinks=False) so the staging table is a fully
    # independent copy — modifications during cast_ingestion.ingest
    # must NOT bleed back into the live directory via symlink.
    shutil.copytree(live_table, staging_table, symlinks=False)
    seeded_rows = (
        sum(1 for _ in (staging_table / "data").iterdir())
        if (staging_table / "data").is_dir()
        else 0
    )
    logger.info(
        f"[HotSwap] Pre-seeded staging LanceDB from live: "
        f"{live_table} → {staging_table} "
        f"({seeded_rows} data file(s) carried forward)"
    )


def stage_ingest(
    target_directory: str,
    staging_dir: str | None = None,
) -> dict:
    """Run CAST ingestion to a staging directory.

    Passes explicit staging root to cast_ingestion.ingest without
    mutating environment variables or module globals.

    Args:
        target_directory: Path to codebase to ingest.
        staging_dir: Override staging directory.

    Returns:
        Dict with status, files_processed, chunks_created, errors.

    """
    from .rag_paths import staging_dir as _staging_dir_fn

    target = Path(target_directory)
    if not target.is_dir():
        return {"status": "error", "error": f"Target directory not found: {target}"}

    stg_dir = _staging_dir_fn(staging_dir)
    staging = Path(stg_dir)
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)

    logger.info(f"[HotSwap] Staging ingestion: {target} → {staging}")

    # v13.22.3 RC2 follow-up: PRESERVE unchanged rows across the hotswap.
    #
    # Background — the bug that bit us hard:
    #   cast_ingestion.ingest() runs with ``db_root_path=str(staging)``,
    #   which routes writes to the (empty) staging LanceDB. When the
    #   incremental cache is warm (the normal case for a hotswap),
    #   ``ingest()`` only re-parses files whose mtime+hash changed;
    #   the chunk list passed to ``_write_vector_store()`` is therefore
    #   a PARTIAL corpus. ``_write_vector_store`` correctly routes a
    #   partial corpus through ``upsert_lancedb_chunks`` (rather than
    #   ``rebuild_lancedb_index``) — but ``upsert_lancedb_chunks`` opens
    #   the staging table; if the table does not exist yet, it falls
    #   back to ``rebuild_lancedb_index(chunks)``, which builds the
    #   table from the PARTIAL chunks list. Every unchanged file's
    #   chunks are silently dropped, and the swap brings the truncated
    #   table over as the new live corpus.
    #
    # The root fix: PRE-SEED the staging LanceDB from the live
    # LanceDB BEFORE invoking ``cast_ingestion.ingest``. Then
    # ``upsert_lancedb_chunks`` opens the pre-seeded table, deletes
    # rows for changed files, appends the new chunks, and — critically
    # — leaves the rows for unchanged files intact. The swap then
    # promotes the merged corpus. This is true incremental semantics:
    # only the changed files' chunks are re-embedded; the rest are
    # carried forward verbatim from the live data.
    #
    # For the very first ingest (live LanceDB absent), this step is a
    # no-op — cast_ingestion.ingest() falls back to rebuild on the
    # full corpus, exactly as before.
    try:
        _seed_staging_from_live(staging)
    except Exception as seed_exc:  # ruff: ignore[BLE001]  # broad: pre-seed failure is non-fatal (empty staging fallback)
        logger.warning(
            f"[HotSwap] Could not pre-seed staging LanceDB from live: "
            f"{seed_exc}; proceeding with empty staging (first-ever "
            f"ingest semantics)"
        )

    try:
        from .cast_ingestion import ingest

        result = ingest(str(target), db_root_path=str(staging))

        warnings = getattr(result, "warnings", [])
        fatal_error = bool(result.errors) or (
            result.chunks_created == 0 and result.files_processed > 0
        )

        if fatal_error:
            return {
                "status": "error",
                "files_processed": result.files_processed,
                "chunks_created": result.chunks_created,
                "relations_extracted": result.relations_extracted,
                "errors": result.errors[:5],
                "warnings": warnings,
                "elapsed_seconds": round(result.elapsed_seconds, 2),
                "staging_dir": str(staging),
                "hint": (
                    "Downstream store(s) failed. Inspect the prior log "
                    "lines for the exception type. Common causes: Kùzu "
                    "schema mismatch, LanceDB write permission denied, "
                    "or tree-sitter parse failure on an unsupported file."
                ),
            }

        return {
            "status": "ok",
            "files_processed": result.files_processed,
            "chunks_created": result.chunks_created,
            "relations_extracted": result.relations_extracted,
            "errors": result.errors[:5],
            "warnings": warnings,
            "elapsed_seconds": round(result.elapsed_seconds, 2),
            "staging_dir": str(staging),
        }
    except (OSError, ValueError, RuntimeError) as e:
        logger.error(f"[HotSwap] Staging ingestion failed: {e}")
        return {"status": "error", "error": str(e)}


def _incremental_update(target_directory: str) -> dict:
    """Fast incremental update: parse only changed files, mutate live stores.

    Uses the delta engine to identify modified/added/deleted files, then
    surgically updates LanceDB and Kùzu in place. This avoids the
    multi-minute full re-index that ``stage_ingest`` requires.

    For corrupt state or huge changesets (>40% of files modified), it
    falls back to the full ``stage_ingest`` path.

    Returns:
        Dict with status, modified_count, added_count, deleted_count,
        elapsed_seconds, and (on fallback) a "fallback" key.

    """
    start = time.monotonic()
    target = Path(target_directory)
    if not target.is_dir():
        return {"status": "error", "error": f"Target directory not found: {target}"}

    try:
        # v13.22.3: only scan_codebase / compute_delta / is_noop remain;
        # the in-place write helpers (chunk_file, build_kuzu_graph,
        # extract_relations, upsert_lancedb_chunks,
        # delete_kuzu_nodes_for_files, update_state_after_ingestion,
        # remove_from_state) are no longer referenced from
        # _incremental_update — they were the in-place branch's toolchain.
        # Keep `delta_engine.compute_delta / is_noop` so the cheap no-op
        # fast path still works without going through the swap.
        from .cast_ingestion import scan_codebase
        from .delta_engine import (
            compute_delta,
            is_noop,
        )
    except ImportError as exc:
        return {"status": "error", "error": f"incremental deps not available: {exc}"}

    try:
        # 1. Scan the codebase
        files = [str(p) for p in scan_codebase(target)]
        if not files:
            return {"status": "ok", "noop": True, "elapsed_seconds": 0.0}

        # 2. Compute the diff
        delta = compute_delta(str(target), files)
        if is_noop(delta):
            elapsed = time.monotonic() - start
            logger.info(f"[HotSwap:inc] noop in {elapsed:.3f}s (0 changes)")
            return {
                "status": "ok",
                "noop": True,
                "elapsed_seconds": round(elapsed, 3),
                "unchanged": delta.unchanged_count,
            }

        # 3. v13.22.3 RC2/RC3 fix — REMOVED IN-PLACE BRANCH.
        #
        # The original code here wrote directly to the LIVE LanceDB and
        # Kùzu files via ``upsert_lancedb_chunks`` /
        # ``delete_kuzu_nodes_for_files`` / ``build_kuzu_graph``. That
        # collided with the RAG server's held read-only Kùzu handle
        # (mcp_rag_server.py:516) and with its read-only chmod
        # (mcp_rag_server.py:414); the in-place path always failed under
        # a live server, and degraded to the full stage→swap path
        # anyway. The two paths are now COLLAPSED: every change, no
        # matter how small, goes through ``stage_ingest`` →
        # ``swap_staged_to_live``. The server's Kùzu handle is never
        # re-opened, the read-only chmod is never fought, and the
        # RLock-protected swap is the only write path.
        #
        # The "fast noop" detection (delta unchanged_count == files) is
        # still honored above (line ~190) — pure noops never reach here.
        # We only fall back when the changeset is non-empty, because we
        # no longer carry an in-place write path.
        #
        # v13.22.3 REGRESSION GUARD: the defensive "all lists empty ⇒
        # noop" return below was previously the only fall-through for
        # the "no state file" case — but in that case
        # ``compute_delta`` sets ``fallback_required=True`` while
        # leaving modified/added/deleted as empty lists. The
        # ``is_noop`` check above sees ``fallback_required=True`` and
        # returns False (correctly), but the fall-through then matches
        # the empty-lists pattern and returns noop — masking the
        # fallback-required signal. The fix is to require
        # ``not delta.fallback_required`` in the empty-lists check.
        if not (delta.modified or delta.added or delta.deleted) and not delta.fallback_required:
            # Empty delta defensively; treat as noop (already handled
            # by is_noop above, but be explicit).
            elapsed = time.monotonic() - start
            return {
                "status": "ok",
                "noop": True,
                "elapsed_seconds": round(elapsed, 3),
            }

        logger.info(
            f"[HotSwap:inc] non-empty changeset "
            f"(+{len(delta.added)} ~{len(delta.modified)} "
            f"-{len(delta.deleted)}); deferring to stage→swap "
            f"(in-place branch removed in v13.22.3)"
        )
        return {
            "status": "fallback",
            "fallback_reason": "in_place_branch_removed_v13_22_3",
            "fallback_detail": (
                "v13.22.3 collapsed the in-place write path; all "
                "non-empty changesets route through stage_ingest → "
                "swap_staged_to_live (RLock-protected)."
            ),
            "modified": len(delta.modified),
            "added": len(delta.added),
            "deleted": len(delta.deleted),
            "unchanged": delta.unchanged_count,
        }
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        logger.error(f"[HotSwap:inc] incremental update failed: {exc}", exc_info=True)
        return {"status": "error", "error": str(exc)}


def _release_rag_connections() -> bool:
    """Signal the RAG server to release its database connections.

    Sets the module-level _initialized flag to False on the RAG server
    and clears the connection globals. On the next query, init_connections()
    will re-open the databases from the new (swapped) data.

    Cross-process detection: this only works when hotswap_ingest runs in the
    SAME process as the RAG server. If called from a separate process (e.g.
    an ingestion worker), the global manipulation is a no-op and this returns
    False; the caller must arrange connection release by other means (pid
    file, IPC, or simply wait for the server to restart).

    Returns:
        True if connections were released in this process.

    """
    try:
        from . import mcp_rag_server

        # Cross-process guard: if we are not inside the RAG server process,
        # its globals live in a different address space.
        if getattr(mcp_rag_server, "_this_is_rag_server_process", False) is not True:
            logger.warning("[HotSwap] Cannot release RAG connections from a different process")
            return False

        # Close the underlying connection objects explicitly.
        try:
            lance = getattr(mcp_rag_server, "_lance_tbl", None)
            if lance is not None and hasattr(lance, "close"):
                lance.close()  # type: ignore[union-attr]
        except (OSError, RuntimeError) as e:
            logger.warning(f"[HotSwap] LanceDB close raised: {e}")

        try:
            kuzu = getattr(mcp_rag_server, "_kuzu_conn", None)
            if kuzu is not None and hasattr(kuzu, "close"):
                kuzu.close()  # type: ignore[union-attr]
        except (OSError, RuntimeError) as e:
            logger.warning(f"[HotSwap] Kùzu close raised: {e}")

        # Force re-init on next query
        mcp_rag_server._initialized = False
        mcp_rag_server._lance_tbl = None
        mcp_rag_server._kuzu_conn = None
        # Keep _embed_model — it's not tied to DB files
        logger.info("[HotSwap] RAG server connections released for swap")
        return True
    except (OSError, ValueError, RuntimeError, ImportError) as e:
        logger.error(f"[HotSwap] Failed to release RAG connections: {e}")
        return False


def _atomic_move_on_same_fs(src: Path, dst: Path, remove_src: bool = True) -> None:
    """Move src to dst atomically using sibling tmp + os.rename().

    Both paths must reside on the same filesystem so os.rename() is atomic.
    If src does not exist, this is a no-op. Raises OSError on failure.

    The atomic replacement sequence for non-empty directory targets is:
        1. Copy src to a sibling ``<dst>.new`` (guaranteed-unique so no
           collision with a leftover from a previous attempt).
        2. If ``<dst>.old`` exists (from the prior swap), remove it.
        3. Rename ``dst`` to ``<dst>.old``.
        4. Rename ``<dst>.new`` to ``dst``.
        5. Optionally remove src and ``<dst>.old``.

    Linux's ``os.rename()`` returns ``ENOTEMPTY`` when overwriting a
    non-empty directory, which breaks the naive "copy to .new then rename
    .new to dst" pattern whenever dst is a populated directory. The
    dance-move pattern (steps 2-4) sidesteps that and remains atomic: at
    any crash point, dst is either the old contents (steps 1-3 before the
    second rename), the new contents (after step 4), or a recoverable
    ``<dst>.new``+``<dst>.old`` pair that a rollback can finish.
    """
    if not src.exists():
        return
    parent = dst.parent
    parent.mkdir(parents=True, exist_ok=True)

    # Use a per-call unique temp name so a previous aborted attempt's
    # leftover ``<dst>.new`` doesn't collide. The trailing suffix avoids
    # Path.with_suffix(".tmp") collisions of the B-05 class — see
    # /tmp/claude-1000/.../REM-010_METAPLAN.md for the failure mode.
    tmp_new = parent / f"{dst.name}.new.{os.getpid()}.{int(time.time() * 1_000_000)}"
    dst_old = parent / f"{dst.name}.old.{os.getpid()}.{int(time.time() * 1_000_000)}"

    # Clean up any leftover from a prior attempt (same-pid only).
    if tmp_new.exists():
        shutil.rmtree(tmp_new, ignore_errors=True) if tmp_new.is_dir() else tmp_new.unlink(
            missing_ok=True
        )
    if dst_old.exists():
        shutil.rmtree(dst_old, ignore_errors=True) if dst_old.is_dir() else dst_old.unlink(
            missing_ok=True
        )

    # Step 1: stage the new contents.
    if src.is_dir():
        shutil.copytree(src, tmp_new)
    else:
        shutil.copy2(src, tmp_new)

    # Fsync the parent directory so the renames are crash-safe.
    fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

    # Step 2-3: two-phase atomic replacement. At this point, dst is the old
    # (possibly non-empty) target and tmp_new holds the staged replacement.
    # We move dst out of the way first so step 4's rename can succeed even
    # when dst was non-empty (os.rename returns ENOTEMPTY for non-empty
    # directory overwrites on Linux).
    if dst.exists():
        if dst.is_dir():
            shutil.rmtree(dst_old, ignore_errors=True)
            os.rename(dst, dst_old)
        else:
            os.rename(dst, dst_old)
    os.rename(tmp_new, dst)

    if remove_src:
        if src.is_dir():
            shutil.rmtree(src, ignore_errors=True)
        else:
            src.unlink(missing_ok=True)
        # Best-effort cleanup of the moved-aside old target.
        if dst_old.exists():
            if dst_old.is_dir():
                shutil.rmtree(dst_old, ignore_errors=True)
            else:
                dst_old.unlink(missing_ok=True)


def swap_staged_to_live(
    staging_dir: str | None = None,
    live_rag_dir: str | None = None,
    keep_backup: bool = True,
) -> dict:
    """Crash-safely swap staged data into the live RAG directory.

    Steps:
    1. Release RAG server DB connections
    2. Back up current live data (optional) via atomic rename on the live fs
    3. Atomically promote staged data into the live directory
    4. RAG server re-initializes on next query

    A crash after the live data has been moved to backup but before the new
    data is renamed into place is recoverable: rollback() restores from backup.

    Args:
        staging_dir: Override staging directory.
        live_rag_dir: Override live RAG directory.
        keep_backup: Keep a backup of the previous live data.

    Returns:
        Dict with status and details.

    """
    from .rag_paths import staging_dir as _staging_dir_fn

    s_dir = staging_dir if staging_dir is not None else _staging_dir_fn()
    l_dir = live_rag_dir if live_rag_dir is not None else db_root()
    b_dir = backup_dir()

    s_lancedb = lancedb_uri(s_dir)
    s_kuzu = kuzu_uri(s_dir)
    l_lancedb = lancedb_uri(l_dir)
    l_kuzu = kuzu_uri(l_dir)

    # Validate staging data exists
    staging_path = Path(s_dir)
    if not staging_path.exists():
        return {"status": "error", "error": f"Staging directory not found: {s_dir}"}

    has_lance = Path(s_lancedb).exists()
    has_kuzu = Path(s_kuzu).exists()
    if not has_lance and not has_kuzu:
        return {
            "status": "error",
            "error": "No LanceDB or Kùzu data in staging directory",
        }
    if has_kuzu and not _staged_kuzu_is_healthy(Path(s_kuzu)):
        # Never promote a partial/failed staging graph over a healthy live
        # one — keep the live Kùzu DB and note the skip in the result.
        logger.warning(
            "[HotSwap] Staged Kùzu failed health check — keeping live graph, swapping LanceDB only"
        )
        has_kuzu = False

    start = time.monotonic()

    # Step 1: Release RAG server connections
    logger.info("[HotSwap] Step 1: Releasing RAG server connections")
    released = _release_rag_connections()
    if not released:
        # v13.22.3 RC5 — fail loud instead of warning+proceeding. The
        # cross-process release is a documented no-op
        # (hotswap_ingest.py:303), so proceeding would silently
        # race the live Kùzu read-only handle and either wedge the
        # OS-level flock or corrupt the staged→live swap. Refuse to
        # run rather than hang the MCP client.
        logger.error("[HotSwap] Cannot release RAG connections from this process")
        return {
            "status": "error",
            "phase": "release",
            "error": (
                "cross_process_release_unsupported: this hot-swap "
                "process does not hold the RAG server's Kùzu "
                "read-only handle. Stop the OTHER process that "
                "opened the live handle (find via "
                f"`lsof {_rag_paths.kuzu_uri()}`), then "
                "re-run rag_hotswap_ingest from the SAME process "
                "that owns the read-only Kùzu connection. "
                "Auto-reingest from RAGStalenessTracker is fine — "
                "it runs in the RAG server process; manual CLI "
                "ingestion needs to kill the running server first."
            ),
            "rolled_back": False,
        }

    # Step 2: Back up current live data atomically on the live fs.
    # The backup dir lives on the same filesystem as the live DB root so the
    # swap can use atomic os.rename().  We back up directly into the canonical
    # backup dir; rollback() reads from there.
    backup_path = Path(b_dir)
    if backup_path.exists():
        shutil.rmtree(backup_path, ignore_errors=True)
    backup_path.mkdir(parents=True, exist_ok=True)

    if keep_backup:
        if Path(l_lancedb).exists():
            _atomic_move_on_same_fs(Path(l_lancedb), backup_path / "lancedb", remove_src=False)
            logger.info("[HotSwap] Backed up live LanceDB")
        if Path(l_kuzu).exists():
            _atomic_move_on_same_fs(Path(l_kuzu), backup_path / "kuzu_bak", remove_src=False)
            logger.info("[HotSwap] Backed up live Kùzu DB")
    else:
        # Remove live data without backup
        if Path(l_lancedb).exists():
            shutil.rmtree(l_lancedb, ignore_errors=True)
        if Path(l_kuzu).exists():
            Path(l_kuzu).unlink(missing_ok=True)

    # Step 3: Promote staged data into live directory atomically
    logger.info("[HotSwap] Step 3: Promoting staged data to live directory")
    Path(l_dir).mkdir(parents=True, exist_ok=True)

    swaps_done = []
    try:
        if has_lance:
            _atomic_move_on_same_fs(Path(s_lancedb), Path(l_lancedb))
            swaps_done.append("lancedb")
        if has_kuzu:
            _atomic_move_on_same_fs(Path(s_kuzu), Path(l_kuzu))
            swaps_done.append("kuzu")
    except OSError:
        logger.exception("[HotSwap] Atomic promotion failed — attempting rollback")
        rollback()
        raise

    # Clean up any remaining staging directory
    remaining_staging = Path(s_dir)
    if remaining_staging.exists():
        shutil.rmtree(remaining_staging, ignore_errors=True)

    elapsed = time.monotonic() - start

    logger.info(
        f"[HotSwap] Swap complete: {', '.join(swaps_done)} "
        f"in {elapsed:.2f}s — RAG will re-initialize on next query"
    )

    return {
        "status": "ok",
        "swapped": swaps_done,
        "backup_dir": b_dir if keep_backup else None,
        "elapsed_seconds": round(elapsed, 2),
    }


def hotswap_ingest(
    target_directory: str,
    staging_dir: str | None = None,
    live_rag_dir: str | None = None,
    keep_backup: bool = True,
    prefer_incremental: bool = True,
) -> dict:
    """Hot-swap ingestion: fast incremental by default, full re-index on fallback.

    v13.22.x: Added ``prefer_incremental`` (default True). When True:
      1. Try the in-place delta update first (typically <1s).
      2. If the delta engine returns ``status="fallback"`` (corrupt state
         or >40% of files changed), automatically fall back to the full
         stage → release → swap path.
      3. If ``prefer_incremental=False``, skip the delta and run the full
         path (used by manual ``rag_hotswap_ingest`` calls that want a
         guaranteed clean rebuild).

    This is the main entry point. For per-context-fold triggers (which
    fire repeatedly during a session) the incremental path is the right
    one — it only re-parses files that actually changed. The full path
    is only used on the first ingest of a brand-new codebase, or after
    a major refactor.

    Args:
        target_directory: Path to codebase to ingest.
        staging_dir: Override staging directory.
        live_rag_dir: Override live RAG directory.
        keep_backup: Keep backup of previous live data.
        prefer_incremental: Use delta engine if True (default), full reindex if False.

    Returns:
        Dict with combined stage + swap results, or ``status="skipped"`` if
        another ingestion/swap is already running in this process.

    """
    # B-1: serialize at the entry point, not just in the MCP tool handlers.
    # The auto-reingest path reaches this function without going through
    # `rag_hotswap_ingest`, so guarding only there left the destructive swap
    # open to interleaving. RLock ⇒ re-acquiring on the same thread (the
    # tool handler already holds it) is safe.
    if not SWAP_LOCK.acquire(blocking=False):
        logger.warning("[HotSwap] Skipped — another ingestion/swap is already in progress")
        return {"status": "skipped", "reason": "swap_in_progress"}
    try:
        return _hotswap_ingest_locked(
            target_directory,
            staging_dir=staging_dir,
            live_rag_dir=live_rag_dir,
            keep_backup=keep_backup,
            prefer_incremental=prefer_incremental,
        )
    finally:
        SWAP_LOCK.release()


def _hotswap_ingest_locked(
    target_directory: str,
    staging_dir: str | None = None,
    live_rag_dir: str | None = None,
    keep_backup: bool = True,
    prefer_incremental: bool = True,
) -> dict:
    """Body of :func:`hotswap_ingest`; caller must hold ``SWAP_LOCK``."""
    logger.info(f"[HotSwap] Starting hot-swap for: {target_directory}")

    # Fast path: try incremental first
    incremental_error: str | None = None
    if prefer_incremental:
        inc_result = _incremental_update(target_directory)
        if inc_result.get("status") == "ok":
            return {
                "status": "ok",
                "mode": "incremental",
                "result": inc_result,
            }
        if inc_result.get("status") == "fallback":
            logger.info(f"[HotSwap] incremental → fallback: {inc_result.get('fallback_reason')}")
        else:
            # B-2 (audit v13.22.1): a hard error from the incremental path
            # used to be returned to the caller, which meant a single
            # missing symbol or an unreadable file left the index
            # permanently stale with no attempt at the full rebuild. Degrade
            # instead of aborting — but keep the error in the payload so the
            # failure is observable rather than silent.
            incremental_error = str(inc_result.get("error"))
            logger.warning(
                f"[HotSwap] incremental failed ({incremental_error}); degrading to full re-index"
            )

    # Slow path: full stage → release → swap
    stage_result = stage_ingest(target_directory, staging_dir=staging_dir)
    if stage_result.get("status") == "error":
        return {
            "status": "error",
            "phase": "stage",
            "error": stage_result.get("error"),
            "incremental_error": incremental_error,
        }

    # Swap
    swap_result = swap_staged_to_live(
        staging_dir=staging_dir,
        live_rag_dir=live_rag_dir,
        keep_backup=keep_backup,
    )
    if swap_result.get("status") == "error":
        return {
            "status": "error",
            "phase": "swap",
            "stage_result": stage_result,
            "error": swap_result.get("error"),
            "incremental_error": incremental_error,
        }

    return {
        "status": "ok",
        "mode": "full",
        "stage": stage_result,
        "swap": swap_result,
        "incremental_error": incremental_error,
    }


def rollback() -> dict:
    """Crash-safe rollback to the backup data.

    Restores the previously backed-up LanceDB and Kùzu data using atomic
    rename. Useful if the newly ingested data is corrupt or incomplete, or
    if atomic promotion failed midway.

    Returns:
        Dict with rollback status.

    """
    b_dir = backup_dir()
    backup_path = Path(b_dir)
    if not backup_path.exists():
        return {"status": "error", "error": "No backup directory found"}

    l_dir = db_root()
    l_lancedb = lancedb_uri(l_dir)
    l_kuzu = kuzu_uri(l_dir)

    # Release connections first
    _release_rag_connections()

    # Remove current (possibly partial) live data
    if Path(l_lancedb).exists():
        shutil.rmtree(l_lancedb, ignore_errors=True)
    if Path(l_kuzu).exists():
        Path(l_kuzu).unlink(missing_ok=True)

    # Restore from backup atomically.  Because _atomic_move_on_same_fs
    # leaves the source directory intact after copying+renaming, we can
    # directly move the backup contents back.
    restored = []
    b_lance = backup_path / "lancedb"
    b_kuzu = backup_path / "kuzu_bak"

    if b_lance.exists():
        _atomic_move_on_same_fs(b_lance, Path(l_lancedb))
        restored.append("lancedb")
    if b_kuzu.exists():
        _atomic_move_on_same_fs(b_kuzu, Path(l_kuzu))
        restored.append("kuzu")

    logger.info(f"[HotSwap] Rollback complete: {', '.join(restored)}")

    return {
        "status": "ok",
        "restored": restored,
    }
