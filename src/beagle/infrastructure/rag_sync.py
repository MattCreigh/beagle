"""Incremental RAG sync - only updates changed files.

Run via cron: 0 2 * * * <beagle-venv>/bin/python -m beagle.infrastructure.rag_sync
Or manually: python -m beagle.infrastructure.rag_sync
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("Beagle.RAG_SYNC")

# Paths
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
# v1.2.0 (RG-6, BGL-009): resolve the data dir from the canonical data root.
from beagle.config.paths import get_data_root as _get_data_root  # ruff: ignore[E402]
from beagle.utils.atomic import atomic_write_text  # ruff: ignore[E402]

DATA_DIR = _get_data_root()
# Sync targets are operator-specific and intentionally NOT shipped with the
# public repo. Configure them via the environment as a JSON array of
# [absolute_path, name] pairs, e.g.:
#   export BEAGLE_RAG_SYNC_CODEBASES='[["/opt/repos/myproject", "myproject"]]'
def _load_codebases() -> list[tuple[str, str]]:
    raw = os.environ.get("BEAGLE_RAG_SYNC_CODEBASES", "")
    if not raw.strip():
        return []
    try:
        pairs = json.loads(raw)
        return [(str(path_), str(name)) for path_, name in pairs]
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        logger.warning(
            "Invalid BEAGLE_RAG_SYNC_CODEBASES (%s); no sync targets configured", exc
        )
        return []


CODEBASES: list[tuple[str, str]] = _load_codebases()

SYNC_STATE_FILE = DATA_DIR / "sync_state.json"


def load_sync_state() -> dict[str, str]:
    """Load last sync timestamps."""
    if SYNC_STATE_FILE.exists():
        return json.loads(SYNC_STATE_FILE.read_text())  # type: ignore[no-any-return]
    return {}


def save_sync_state(state: dict[str, str]) -> None:
    """Save sync timestamps."""
    # Atomic write: the sync state is read by the next cron invocation; a
    # crash mid-write must never leave a truncated document to parse.
    atomic_write_text(SYNC_STATE_FILE, json.dumps(state, indent=2), mode=0o644)


def get_changed_files(codebase_path: str, last_sync: str | None) -> list[Path]:
    """Find files changed since last sync."""
    path = Path(codebase_path)
    if not path.exists():
        return []

    extensions = {
        ".py",
        ".js",
        ".ts",
        ".rs",
        ".go",
        ".java",
        ".c",
        ".cpp",
        ".h",
        ".yaml",
        ".yml",
    }

    if last_sync:
        last_time = datetime.fromisoformat(last_sync)
        last_timestamp = last_time.timestamp()
    else:
        # First sync - index everything
        return list(path.rglob("*"))

    changed = []
    for ext in extensions:
        for f in path.rglob(f"*{ext}"):
            # Skip hidden, cache, and build dirs
            if any(
                part.startswith(".")
                or part
                in {
                    "__pycache__",
                    "node_modules",
                    "venv",
                    ".venv",
                    "build",
                    "dist",
                    "target",
                }
                for part in f.parts
            ):
                continue
            try:
                if f.stat().st_mtime > last_timestamp:
                    changed.append(f)
            except OSError as exc:
                logger.warning(
                    "Cannot stat %s (%s); excluding it from the changed-file set, "
                    "so any edit to it will not be re-ingested this run.",
                    f,
                    exc,
                )

    return changed


def sync_codebase(codebase_path: str, name: str) -> dict:
    """Sync a single codebase to RAG."""
    # Import here to avoid circular deps
    os.environ["BEAGLE_RAG_TIER"] = "instance"
    os.environ["PYTHONPATH"] = str(PROJECT_ROOT)

    from beagle.infrastructure.cast_ingestion import ingest

    logger.info(f"Syncing {name} ({codebase_path})...")

    start = datetime.now(UTC)
    try:
        result = ingest(codebase_path)
        elapsed = (datetime.now(UTC) - start).total_seconds()

        return {
            "name": name,
            "status": "success",
            "files_processed": result.files_processed,
            "chunks_created": result.chunks_created,
            "elapsed": elapsed,
        }
    except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
        logger.error(f"Failed to sync {name}: {e}")
        return {
            "name": name,
            "status": "error",
            "error": str(e),
        }


def run_sync() -> None:
    """Run the full sync."""
    logger.info("=== H-RAG Nightly Sync Started ===")

    state = load_sync_state()
    results = []

    for codebase_path, name in CODEBASES:
        last_sync = state.get(name)
        changed = get_changed_files(codebase_path, last_sync)

        if not changed:
            logger.info(f"SKIP: {name} - no changes")
            continue

        logger.info(f"{name}: {len(changed)} files changed")

        # Sync
        result = sync_codebase(codebase_path, name)
        results.append(result)

        # Update state
        state[name] = datetime.now(UTC).isoformat()
        save_sync_state(state)

    # Summary
    logger.info("=== Sync Complete ===")
    for r in results:
        if r["status"] == "success":
            logger.info(
                f"  {r['name']}: {r['files_processed']} files, "
                f"{r['chunks_created']} chunks in {r['elapsed']:.1f}s"
            )
        else:
            logger.error(f"  {r['name']}: FAILED - {r.get('error')}")


if __name__ == "__main__":
    run_sync()
