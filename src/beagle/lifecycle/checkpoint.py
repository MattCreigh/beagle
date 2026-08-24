"""Checkpoint save/restore for Beagle graceful self-restart.

Serializes transient state (daemon cost, health score, circuit breaker
states, active workflow progress) to disk so a restarted process can
recover where it left off.  Writes are atomic (temp file → os.replace)
to prevent partial-write corruption.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger("Beagle.lifecycle")


@dataclass
class Checkpoint:
    """Serializable snapshot of transient state for restart recovery."""

    timestamp: float
    version: str  # Beagle version for compat check

    # Daemon state
    daemon_daily_cost: float = 0.0
    daemon_deferred_triggers: list[str] = field(default_factory=list)

    # Health state
    health_previous_state: str = "normal"
    health_previous_score: float = 1.0

    # Rate limiter backoff
    rate_limiter_backoff: dict[str, float] = field(default_factory=dict)

    # Circuit breaker state
    circuit_states: dict[str, str] = field(default_factory=dict)

    # Active workflow (if mid-execution)
    active_workflow_id: str = ""
    active_workflow_query: str = ""
    active_workflow_completed_nodes: list[str] = field(default_factory=list)
    active_workflow_mode: str = ""

    # Restart metadata
    restart_reason: str = "unknown"
    restart_count: int = 0
    pid_before_restart: int = 0


class CheckpointManager:
    """Manages checkpoint save/restore to .beagle/checkpoints/."""

    CHECKPOINT_DIR = ".beagle/checkpoints"
    CHECKPOINT_FILE = "restart_checkpoint.json"
    MAX_CHECKPOINTS = 5  # Keep last 5 for debugging

    def __init__(self, workspace_root: Path | None = None) -> None:
        if workspace_root is None:
            from beagle.config.paths import get_workspace_root

            workspace_root = get_workspace_root()
        self._workspace_root = workspace_root
        self._checkpoint_dir = workspace_root / self.CHECKPOINT_DIR

    @property
    def checkpoint_path(self) -> Path:
        """Full path to the latest checkpoint file."""
        return self._checkpoint_dir / self.CHECKPOINT_FILE

    def save(self, checkpoint: Checkpoint) -> Path:
        """Atomically write checkpoint to disk.

        Write to temp file first, then os.replace() for atomicity.
        Also rotates old checkpoints (keep MAX_CHECKPOINTS).
        """
        self._checkpoint_dir.mkdir(parents=True, exist_ok=True)

        data = asdict(checkpoint)
        payload = json.dumps(data, default=str)

        # Rotate old checkpoint files (keep last MAX_CHECKPOINTS)
        self._rotate_checkpoints()

        # Atomic write: temp file → os.replace
        tmp_path = self.checkpoint_path.with_suffix(".json.tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())

            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, self.checkpoint_path)
        except BaseException:
            # Clean up temp file on any failure
            import contextlib

            with contextlib.suppress(OSError):
                tmp_path.unlink(missing_ok=True)
            raise

        logger.info(
            "Checkpoint saved: reason=%s restart_count=%d",
            checkpoint.restart_reason,
            checkpoint.restart_count,
        )
        return self.checkpoint_path

    def load(self) -> Checkpoint | None:
        """Load most recent checkpoint, or None if none exists.

        Verify version compatibility. Return None if version mismatch.
        """
        if not self.exists():
            return None

        return self._load_checkpoint_file(self.checkpoint_path)

    def clear(self) -> None:
        """Remove checkpoint file after successful restore."""
        try:
            self.checkpoint_path.unlink(missing_ok=True)
            logger.info("Checkpoint file cleared after successful restore")
        except OSError as exc:
            logger.warning("Failed to clear checkpoint file: %s", exc)

    def exists(self) -> bool:
        """Check if a checkpoint exists."""
        return self.checkpoint_path.exists()

    def list_checkpoints(self) -> list[Checkpoint]:
        """List all available checkpoint snapshots."""
        checkpoints: list[Checkpoint] = []
        if not self._checkpoint_dir.exists():
            return checkpoints

        for path in sorted(
            self._checkpoint_dir.glob("restart_checkpoint.*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        ):
            try:
                checkpoint = self._load_checkpoint_file(path)
                if checkpoint is not None:
                    checkpoints.append(checkpoint)
            except (OSError, ValueError) as exc:
                logger.warning("Skipping unreadable checkpoint %s: %s", path, exc)
        return checkpoints

    def delete_checkpoint(self, checkpoint_id: str) -> None:
        """Delete a single checkpoint snapshot by its timestamp ID."""
        # The checkpoint ID is the timestamp; it may be passed as a float string.
        candidates = [
            self._checkpoint_dir / f"restart_checkpoint.{checkpoint_id}.json",
            self._checkpoint_dir / f"restart_checkpoint.{int(float(checkpoint_id))}.json",
        ]
        for path in candidates:
            if path.exists():
                path.unlink()
                return
        raise FileNotFoundError(f"Checkpoint {checkpoint_id} not found")

    def _load_checkpoint_file(self, path: Path) -> Checkpoint | None:
        """Load a checkpoint from a path, or None if it is unreadable."""
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load checkpoint %s: %s", path, exc)
            return None

        # Version compatibility check
        from beagle import __version__

        saved_version = data.get("version", "")
        if saved_version and saved_version != __version__:
            logger.warning(
                "Checkpoint version mismatch: saved=%s current=%s — skipping restore",
                saved_version,
                __version__,
            )
            return None

        try:
            return Checkpoint(**data)
        except (TypeError, ValueError) as exc:
            logger.warning("Failed to deserialize checkpoint %s: %s", path, exc)
            return None

    def _rotate_checkpoints(self) -> None:
        """Rotate old checkpoint files, keeping MAX_CHECKPOINTS.

        Moves the current checkpoint to a timestamped backup before
        overwriting with a new one.
        """
        if not self.checkpoint_path.exists():
            return

        # List existing backup checkpoints
        backups = sorted(
            self._checkpoint_dir.glob("restart_checkpoint.*.json"),
            key=lambda p: p.stat().st_mtime,
        )

        # Current checkpoint becomes a timestamped backup
        timestamp = int(time.time())
        backup_name = f"restart_checkpoint.{timestamp}.json"
        backup_path = self._checkpoint_dir / backup_name

        try:
            os.replace(self.checkpoint_path, backup_path)
            backups.append(backup_path)
        except OSError as exc:
            logger.warning("Failed to rotate checkpoint: %s", exc)
            return

        # Remove oldest backups beyond MAX_CHECKPOINTS
        while len(backups) > self.MAX_CHECKPOINTS:
            oldest = backups.pop(0)
            try:
                oldest.unlink()
            except OSError as exc:
                logger.warning("Failed to remove old checkpoint %s: %s", oldest, exc)


# ── Module-level helper functions ───────────────────────────────────────────────


def list_checkpoints(workspace_root: Path | None = None) -> list[Checkpoint]:
    """List all available checkpoint snapshots."""
    return CheckpointManager(workspace_root).list_checkpoints()


def delete_checkpoint(checkpoint_id: str, workspace_root: Path | None = None) -> None:
    """Delete a single checkpoint snapshot by its timestamp ID."""
    return CheckpointManager(workspace_root).delete_checkpoint(checkpoint_id)


# ── Module-level singleton ──────────────────────────────────────────────────────

_checkpoint_mgr: CheckpointManager | None = None
_checkpoint_lock = threading.Lock()


def get_checkpoint_manager() -> CheckpointManager:
    """Get the global checkpoint manager singleton."""
    global _checkpoint_mgr
    with _checkpoint_lock:
        if _checkpoint_mgr is None:
            _checkpoint_mgr = CheckpointManager()
    return _checkpoint_mgr
