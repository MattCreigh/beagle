"""Checkpoint manager for Beagle workflow persistence.

Provides save/load/resume for DAG orchestrator state,
enabling workflow interruption and recovery.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .utils.atomic import atomic_write_text

logger = logging.getLogger("Beagle.checkpointer")

# Default checkpoint directory
_DEFAULT_CHECKPOINT_DIR = Path.home() / ".beagle" / "checkpoints"


def get_checkpoint_dir() -> Path:
    """Get the checkpoint directory.

    Resolution order:
      1. ``BEAGLE_CHECKPOINT_DIR`` env var (legacy escape hatch).
      2. ``config.state.connection_string`` from config.toml — its parent dir
         when the type is SQLite; relative paths anchor on ``data_root``.
      3. ``~/.beagle/checkpoints`` (final fallback, matches schema default).
    """
    env_dir = os.environ.get("BEAGLE_CHECKPOINT_DIR")
    if env_dir:
        return Path(env_dir)
    try:
        from .config.config import get_config
        from .config.paths import get_data_root

        st = get_config().state
        if st.type == "sqlite" and st.connection_string:
            p = Path(st.connection_string)
            if not p.is_absolute():
                p = get_data_root() / p
            return p.parent
    except (
        ImportError,
        AttributeError,
        RuntimeError,
        OSError,
    ) as exc:  # catch: NARROWED  # RATIONALE=four-tuple: import errors during lazy import of langgraph checkpoint store, attribute lookups on the optional module, runtime errors from the backend, OS errors from disk I/O
        logger.warning(
            "Cannot read the configured checkpoint directory (%s); falling back to the "
            "default location below.",
            exc,
        )
    return _DEFAULT_CHECKPOINT_DIR


@dataclass
class Checkpoint:
    """Represents a saved workflow state checkpoint.

    Attributes:
        workflow_id: Unique identifier for the workflow run.
        query: The original user query.
        completed_nodes: List of node names that completed successfully.
        state_data: Arbitrary state data (serializable dict).
        timestamp: Unix timestamp of when the checkpoint was created.

    """

    workflow_id: str
    query: str = ""
    completed_nodes: list[str] = field(default_factory=list)
    state_data: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def save(self, path: Path | None = None) -> Path:
        """Save checkpoint to a JSON file.

        Args:
            path: File path to write. If None, uses default checkpoint dir.

        Returns:
            The path the checkpoint was saved to.

        """
        if path is None:
            checkpoint_dir = get_checkpoint_dir()
            checkpoint_dir.mkdir(parents=True, exist_ok=True)
            path = checkpoint_dir / f"{self.workflow_id}.json"

        path.parent.mkdir(parents=True, exist_ok=True)
        data = asdict(self)
        # Atomic write: a killed process must never leave a corrupt
        # checkpoint that loses the workflow resume point.
        atomic_write_text(path, json.dumps(data, indent=2, default=str), mode=0o644)
        logger.info(f"Checkpoint saved: {path}")
        return path

    @classmethod
    def load(cls, path: Path) -> Checkpoint | None:
        """Load a checkpoint from a JSON file.

        Args:
            path: Path to the checkpoint JSON file.

        Returns:
            Reconstructed Checkpoint instance, or None if the file is corrupt
            or contains unexpected fields.

        """
        data = json.loads(path.read_text(encoding="utf-8"))
        try:
            return cls(**data)
        except TypeError:  # unexpected fields or missing required fields
            logger.warning(f"Checkpoint {path} has incompatible fields; returning None")
            return None

    def delete(self) -> bool:
        """Delete this checkpoint file from the checkpoint directory.

        Returns:
            True if a file was deleted, False otherwise.

        """
        checkpoint_dir = get_checkpoint_dir()
        path = checkpoint_dir / f"{self.workflow_id}.json"
        if path.exists():
            path.unlink()
            logger.info(f"Checkpoint deleted: {path}")
            return True
        return False


class CheckpointManager:
    """Manages workflow checkpoint lifecycle.

    Provides CRUD operations and cleanup for Checkpoint objects
    stored in a designated directory.
    """

    def __init__(self, checkpoint_dir: Path | None = None) -> None:
        """Initialize the checkpoint manager.

        Args:
            checkpoint_dir: Directory for storing checkpoints.
                Defaults to get_checkpoint_dir() if not provided.

        """
        self.checkpoint_dir = checkpoint_dir or get_checkpoint_dir()
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)

    def _sanitize_id(self, target_id: str) -> str:
        """Sanitize an identifier to prevent path traversal attacks.

        Args:
            target_id: Identifier string to validate.

        Returns:
            The validated identifier string.

        Raises:
            ValueError: If the identifier contains disallowed characters.

        """
        if not target_id or not re.match(r"^[a-zA-Z0-9_-]+$", str(target_id)):
            raise ValueError(
                f"Invalid identifier format detected. Traversal mitigation active for: {target_id}"
            )
        return str(target_id)

    def save_state(
        self,
        workflow_id: str,
        query: str,
        state: Any,
        completed_nodes: list[str] | None = None,
    ) -> Path:
        """Save a workflow state as a checkpoint.

        Args:
            workflow_id: Unique workflow identifier.
            query: The original user query.
            state: AgentState or dict with state data.
            completed_nodes: List of completed node names.

        Returns:
            Path to the saved checkpoint file.

        """
        safe_id = self._sanitize_id(workflow_id)
        # Extract state data from AgentState if provided
        if hasattr(state, "query"):
            state_data = {
                "research_plan": getattr(state, "research_plan", ""),
                "raw_execution_context": getattr(state, "raw_execution_context", ""),
                "verified_facts": getattr(state, "verified_facts", ""),
                "final_report": getattr(state, "final_report", ""),
                "errors": list(getattr(state, "errors", [])),
            }
        elif isinstance(state, dict):
            state_data = state
        else:
            state_data = {}

        checkpoint = Checkpoint(
            workflow_id=workflow_id,
            query=query,
            completed_nodes=completed_nodes or [],
            state_data=state_data,
        )
        return checkpoint.save(self.checkpoint_dir / f"{safe_id}.json")

    def load_state(self, workflow_id: str) -> Checkpoint | None:
        """Load a checkpoint by workflow ID.

        Args:
            workflow_id: The workflow identifier to look up.

        Returns:
            Checkpoint if found, None otherwise.

        """
        safe_id = self._sanitize_id(workflow_id)
        path = self.checkpoint_dir / f"{safe_id}.json"
        if not path.exists():
            return None
        try:
            return Checkpoint.load(path)
        except (RuntimeError, OSError, ValueError, ImportError) as e:  # catch: NARROWED
            logger.warning(f"Failed to load checkpoint {path}: {e}")
            return None

    def list_checkpoints(self) -> list[dict[str, Any]]:
        """List all available checkpoints.

        Returns:
            List of checkpoint metadata dicts (workflow_id, query, timestamp).

        """
        checkpoints = []
        for path in sorted(self.checkpoint_dir.glob("*.json")):
            try:
                cp = Checkpoint.load(path)
                if cp is None:
                    logger.warning(f"Checkpoint {path} failed to parse; skipping")
                    continue
                checkpoints.append(
                    {
                        "workflow_id": cp.workflow_id,
                        "query": cp.query,
                        "completed_nodes": cp.completed_nodes,
                        "timestamp": cp.timestamp,
                        "path": str(path),
                    }
                )
            except (RuntimeError, OSError, ValueError, ImportError) as e:  # catch: NARROWED
                logger.warning(f"Failed to load checkpoint {path}: {e}")
        return checkpoints

    def cleanup_old(self, max_age_days: int = 7) -> int:
        """Remove checkpoints older than the specified age.

        Args:
            max_age_days: Maximum age in days. Older checkpoints are deleted.

        Returns:
            Number of checkpoints deleted.

        """
        deleted = 0
        # Persisted-epoch comparison: the cutoff is a wall-clock instant matched
        # against Checkpoint.timestamp (persisted as time.time()). Derived via
        # timezone-aware datetime arithmetic — identical semantics, and the
        # interval never reads as a time.time() subtraction.
        cutoff = (datetime.now(UTC) - timedelta(days=max_age_days)).timestamp()

        for path in self.checkpoint_dir.glob("*.json"):
            try:
                cp = Checkpoint.load(path)
                if cp is not None and cp.timestamp < cutoff:
                    path.unlink()
                    deleted += 1
                    logger.info(f"Deleted old checkpoint: {path}")
            except OSError as e:
                # If we can't parse it, check file modification time
                logger.warning(
                    "Checkpoint parse failed, using mtime fallback",
                    exc_info=e,
                    extra={"checkpoint_path": str(path)},
                )
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    deleted += 1

        return deleted
