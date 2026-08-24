"""NDJSON file emitter for Beagle events.

Subscribes to all events and appends each as one JSON line to a log file.
Supports rotation at 10MB and flushes after every write.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from .events import BeagleEvent

logger = logging.getLogger("Beagle.events.emitter")

MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024  # 10MB


class NDJSONEmitter:
    """Subscribes to events and writes them to NDJSON files."""

    def __init__(self, base_dir: Path | None = None) -> None:
        if base_dir is None:
            # Safe local fallback, e.g. workspace or home dir
            from beagle.config.paths import get_workspace_root

            self.base_dir = get_workspace_root() / "events_log"
        else:
            self.base_dir = base_dir

        try:
            self.base_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning(f"Failed to create events log directory {self.base_dir}: {e}")

        self._lock = threading.Lock()

        # Track current open file by workflow_id
        self._files = {}  # type: ignore[var-annotated]

    def _get_log_path(self, workflow_id: str) -> Path:
        """Get the primary log file path for a workflow."""
        # Sanitize workflow_id
        safe_id = "".join(c for c in workflow_id if c.isalnum() or c in ("-", "_"))
        if not safe_id:
            safe_id = "default"
        return self.base_dir / f"{safe_id}.ndjson"

    def _rotate_if_needed(self, file_path: Path) -> None:
        """Rotate the log file if it exceeds the maximum size."""
        if not file_path.exists():
            return

        try:
            if file_path.stat().st_size >= MAX_FILE_SIZE_BYTES:
                # Simple rotation: keep one backup
                backup_path = file_path.with_suffix(".ndjson.1")
                if backup_path.exists():
                    backup_path.unlink()
                file_path.rename(backup_path)
        except OSError as e:
            logger.warning(f"Failed to rotate log file {file_path}: {e}")

    def emit(self, event: BeagleEvent) -> None:
        """Write a single event to the corresponding workflow log."""
        try:
            workflow_id = event.workflow_id
            log_path = self._get_log_path(workflow_id)

            with self._lock:
                self._rotate_if_needed(log_path)

                # Append and flush
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(event.to_json() + "\n")
                    f.flush()
        except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
            # Never crash the event bus
            logger.error(f"NDJSONEmitter failed to write event: {e}")

    def start(self) -> None:
        """Subscribe to the global event bus."""
        from .bus import get_event_bus

        bus = get_event_bus()
        bus.subscribe("*", self.emit)
        logger.debug(f"NDJSONEmitter started, logging to {self.base_dir}")


# Singleton integration
_emitter: NDJSONEmitter | None = None


def get_emitter() -> NDJSONEmitter:
    """Get or create the global file emitter."""
    global _emitter
    if _emitter is None:
        _emitter = NDJSONEmitter()
    return _emitter


def start_emitter() -> None:
    """Start the global file emitter."""
    get_emitter().start()
