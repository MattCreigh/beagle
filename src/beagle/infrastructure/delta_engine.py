"""Delta Engine — Incremental RAG ingestion state tracking and diff computation.

Replaces the simple mtime-based skip in cast_ingestion.py with a full
state-tracking system that identifies modified, added, and deleted files,
enabling surgical LanceDB upserts and Kùzu graph mutations instead of
destructive full-table rebuilds.

Design:
  - State file: .beagle/rag_state.json maps absolute file paths to
    {mtime, sha256_hash, chunk_count}.
  - On each ingestion trigger, compute the diff against the live filesystem.
  - If no files changed → no-op (immediate exit, <10ms).
  - If state file is corrupted or schema mismatches → fallback to full re-index.

v13.12.4: Initial implementation.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from beagle.config.paths import get_data_root as _get_data_root

logger = logging.getLogger("Beagle.DeltaEngine")

# ── State file location ───────────────────────────────────────────────────────
# The state file honours $BEAGLE_DATA_ROOT / config [paths].data_root /
# XDG_DATA_HOME, matching every other Beagle runtime-state file. A test that
# sets BEAGLE_DATA_ROOT (or BEAGLE_KNOWLEDGE_DIR) therefore isolates the delta
# state too, instead of wiping the operator's real incremental cache.
_STATE_DIR = _get_data_root()
_STATE_FILE = _STATE_DIR / "rag_state.json"


# ── Data structures ───────────────────────────────────────────────────────────


@dataclass
class FileState:
    """Per-file tracking entry in rag_state.json."""

    mtime: str  # ISO timestamp or raw mtime float string
    sha256_hash: str  # First 16 hex chars
    chunk_count: int = 0


@dataclass
class DeltaResult:
    """Diff computed between the state file and the live filesystem."""

    modified: list[str] = field(default_factory=list)  # file changed
    added: list[str] = field(default_factory=list)  # new file not in state
    deleted: list[str] = field(default_factory=list)  # in state but gone from disk
    unchanged_count: int = 0  # present in both, no change
    total_files: int = 0
    fallback_required: bool = False  # True if state is corrupt, force full re-index
    fallback_reason: str = ""


# ── Lock (module-level, serializes state read/write across threads) ───────────
_state_lock = threading.RLock()


# ── Core delta API ────────────────────────────────────────────────────────────


def compute_delta(_target_dir: str | Path, scan_files: list[str]) -> DeltaResult:
    """Compute the diff between saved state and the live filesystem.

    Args:
        _target_dir: Root directory being ingested (reserved; the state file
            path is resolved independently via ``get_data_root()``).
        scan_files: List of absolute file paths discovered by scan_codebase().
                    Expected to be sorted and filtered by SUPPORTED_EXTENSIONS.

    Returns:
        DeltaResult with modified/added/deleted lists and fallback flag.

    """
    result = DeltaResult()

    # Load state
    state = _load_state()
    if not state:
        # No state exists — first run, need full ingestion
        result.fallback_required = True
        result.fallback_reason = "No state file found (first ingestion)"
        result.total_files = len(scan_files)
        return result

    # Validate state schema (must be dict of str → dict with expected keys)
    if not _validate_state_schema(state):
        result.fallback_required = True
        result.fallback_reason = "State file schema invalid or corrupted"
        result.total_files = len(scan_files)
        logger.warning("[Delta] State schema validation failed — triggering full re-index")
        return result

    # Build live index: {abs_path → (mtime, sha256_quick)}
    live_index: dict[str, tuple[str, str]] = {}
    for fpath_str in scan_files:
        fpath = Path(fpath_str)
        try:
            stat_info = fpath.stat()
            mtime = str(stat_info.st_mtime)
            # Quick hash: use mtime + size as fingerprint to avoid reading every file
            # If mtime matches cache, we consider it unchanged (mtime is the primary key)
            quick_hash = f"{mtime}:{stat_info.st_size}"
            live_index[fpath_str] = (mtime, quick_hash)
        except OSError:
            # File not readable — skip, will be picked up next run
            logger.debug(f"[Delta] Skipping unreadable file: {fpath_str}")
            continue

    result.total_files = len(live_index)

    # Compute diff
    state_paths = set(state.keys())
    live_paths = set(live_index.keys())

    # Deleted: in state but not on disk
    for deleted_path in state_paths - live_paths:
        result.deleted.append(deleted_path)

    # Added: on disk but not in state
    for added_path in live_paths - state_paths:
        result.added.append(added_path)

    # Modified: in both, but mtime differs
    for common_path in state_paths & live_paths:
        live_mtime, _ = live_index[common_path]
        cached_entry = state[common_path]
        cached_mtime = cached_entry.get("mtime", "")
        if cached_mtime == live_mtime:
            result.unchanged_count += 1
            continue
        # v13.22.4 heat fix: mtime alone is NOT proof of modification.
        # Bulk redeploys/copies rewrite every mtime without changing a
        # byte of content (e.g. reinstalling site-packages), which made
        # this branch declare ~100% of files modified and forced a full
        # 5k-chunk re-embed on the next hourly tick. Verify against the
        # persisted sha256_hash before paying for a rebuild.
        cached_hash = cached_entry.get("sha256_hash", "")
        if cached_hash and _content_hash(common_path) == cached_hash:
            result.unchanged_count += 1
            continue
        result.modified.append(common_path)

    # If only deletes (no modifies or adds), we can surgical-delete
    # If everything is unchanged → no-op
    _log_delta_summary(result)

    return result


def update_state_after_ingestion(
    scan_files: list[str],
    chunk_counts: dict[str, int],
    merge: bool = False,
) -> None:
    """Update the state file after a successful ingestion pass.

    Args:
        scan_files: Files that were processed. With ``merge=False`` this must
            be the COMPLETE scan set — anything absent is treated as no longer
            indexed and disappears from the state.
        chunk_counts: Dict mapping file path → number of chunks created for it.
        merge: When True, keep existing entries for files not in
            ``scan_files`` and only refresh the ones listed. Required by the
            incremental path, which re-parses a subset: overwriting would
            drop every skipped file from the state and make the *next* run
            see them all as "added" (a permanent full re-index loop).

    """
    state: dict[str, dict[str, str]] = _load_state() if merge else {}

    for fpath_str in scan_files:
        fpath = Path(fpath_str)
        try:
            stat_info = fpath.stat()
            source = fpath.read_text(encoding="utf-8", errors="replace")
            file_hash = hashlib.sha256(source.encode()).hexdigest()[:16]
            state[fpath_str] = {
                "mtime": str(stat_info.st_mtime),
                "sha256_hash": file_hash,
                "chunk_count": str(chunk_counts.get(fpath_str, 0)),
            }
        except OSError:
            logger.debug(f"[Delta] Skipping unreadable file for state update: {fpath_str}")
            continue

    _save_state(state)


def remove_from_state(deleted_paths: list[str]) -> None:
    """Remove specific files from the state (after surgical Kùzu/LanceDB deletion)."""
    state = _load_state()
    for path in deleted_paths:
        state.pop(path, None)
    _save_state(state)


def is_noop(result: DeltaResult) -> bool:
    """Return True if the delta indicates no work is needed."""
    return (
        not result.fallback_required
        and not result.modified
        and not result.added
        and not result.deleted
    )


def _content_hash(path: str) -> str:
    """First 16 hex chars of the file's sha256.

    Must mirror ``update_state_after_ingestion``'s hash exactly
    (read_text utf-8 errors=replace → sha256[:16]) or unchanged files
    would compare unequal and trigger spurious rebuilds.

    Returns "" on OSError; callers treat that as modified (fail-closed).
    """
    try:
        source = Path(path).read_text(encoding="utf-8", errors="replace")
        return hashlib.sha256(source.encode()).hexdigest()[:16]
    except OSError:
        return ""


# ── Internal: state file I/O ──────────────────────────────────────────────────


def _load_state() -> dict[str, dict[str, str]]:
    """Load and return the state dict. Returns empty dict on any error."""
    with _state_lock:
        try:
            if _STATE_FILE.exists():
                raw = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    return cast(dict[str, dict[str, str]], raw)
            return {}
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"[Delta] Failed to load state file: {e}")
            return {}


def _save_state(state: dict[str, dict[str, str]]) -> None:
    """Atomically write the state file (write to temp, then rename)."""
    with _state_lock:
        try:
            _STATE_DIR.mkdir(parents=True, exist_ok=True)
            tmp_path = _STATE_FILE.with_suffix(".json.tmp")
            tmp_path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
            tmp_path.replace(_STATE_FILE)
            logger.debug(f"[Delta] State saved: {len(state)} entries")
        except OSError as e:
            logger.warning(f"[Delta] Failed to save state file: {e}")


def _validate_state_schema(state: dict) -> bool:
    """Validate that the state dict has the expected structure.

    Must be: {str → {str → str}} with at least 'mtime' key in each file entry.
    """
    if not isinstance(state, dict):
        return False
    # Sample a few entries to validate schema without O(n) full scan
    check_count = min(5, len(state))
    for checked, (key, value) in enumerate(state.items()):
        if not isinstance(key, str) or not isinstance(value, dict):
            return False
        if "mtime" not in value:
            return False
        if checked + 1 >= check_count:
            break
    return True


def _log_delta_summary(result: DeltaResult) -> None:
    """Log a human-readable summary of the delta computation."""
    changed = len(result.modified) + len(result.added) + len(result.deleted)
    if result.fallback_required:
        logger.info(
            f"[Delta] Fallback triggered: {result.fallback_reason} "
            f"({result.total_files} files to process)"
        )
    elif changed == 0:
        logger.info(
            f"[Delta] NO-OP: {result.unchanged_count} files unchanged, "
            f"0 files changed — skipping ingestion"
        )
    else:
        logger.info(
            f"[Delta] Diff: {len(result.modified)} modified, "
            f"{len(result.added)} added, {len(result.deleted)} deleted, "
            f"{result.unchanged_count} unchanged "
            f"(total: {result.total_files})"
        )
