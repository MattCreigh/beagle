"""Config hot-reload functionality for Beagle v12.0.

Provides file watching and automatic config reload on changes.
Uses thread-safe singleton pattern with listener callbacks.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..config.config import WorkflowConfig, get_config_path, load_config
from .state import Singleton

logger = logging.getLogger("Beagle.config_watcher")


@dataclass
class WatcherStats:
    """Statistics for config watcher."""

    reloads: int = 0
    errors: int = 0
    last_reload_time: float = 0.0
    last_mtime: float = 0.0


ConfigCallback = Callable[[WorkflowConfig, set[str]], None]


class ConfigWatcher(Singleton["ConfigWatcher"]):
    """Thread-safe config watcher with hot-reload capability.

    Singleton pattern ensures only one watcher instance across the app.
    File modifications trigger automatic reload and notify all listeners.
    """

    def __init__(self) -> None:
        # Avoid double-init if already initialized by Singleton machinery
        if hasattr(self, "_initialized") and self._initialized:  # type: ignore[has-type]
            return

        self._name = "config_watcher"
        self._config: WorkflowConfig | None = None
        self._config_path: Path = get_config_path()
        self._listeners: list[ConfigCallback] = []
        self._listener_lock = threading.Lock()
        self._watch_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._poll_interval: float = 2.0  # seconds
        self._last_hash: str = ""
        self._stats = WatcherStats()  # type: ignore[assignment]
        self._started = False
        self._initialized = True

    @property
    def config(self) -> WorkflowConfig:
        """Get current config (lazy load if needed)."""
        if self._config is None:
            self._config = load_config(self._config_path)
            self._update_hash()
        return self._config

    @property
    def stats(self) -> WatcherStats:  # type: ignore[override]
        """Get watcher statistics."""
        return self._stats  # type: ignore[return-value]

    def set_config_path(self, path: Path | str) -> None:
        """Set custom config path (before start)."""
        if self._started:
            logger.warning("Cannot change config path while watcher is running")
            return
        self._config_path = Path(path)
        self._config = None  # Force reload

    def add_listener(self, callback: ConfigCallback) -> None:
        """Add a callback for config changes.

        Args:
            callback: Function(new_config, changed_keys) called on reload

        """
        with self._listener_lock:
            self._listeners.append(callback)
        logger.debug(f"Added config listener: {len(self._listeners)} total")

    def remove_listener(self, callback: ConfigCallback) -> None:
        """Remove a config change callback."""
        with self._listener_lock:
            if callback in self._listeners:
                self._listeners.remove(callback)
        logger.debug(f"Removed config listener: {len(self._listeners)} total")

    def start(self) -> None:
        """Start watching config file for changes (non-blocking)."""
        if self._started:
            logger.debug("Config watcher already running")
            return

        self._stop_event.clear()
        self._watch_thread = threading.Thread(
            target=self._watch_loop, name="config-watcher", daemon=True
        )
        self._watch_thread.start()
        self._started = True
        logger.info(f"Config watcher started, watching {self._config_path}")

    def stop(self) -> None:
        """Stop watching config file."""
        if not self._started:
            return

        self._stop_event.set()
        if self._watch_thread:
            self._watch_thread.join(timeout=5.0)
        self._started = False
        logger.info("Config watcher stopped")

    def reload_now(self) -> bool:
        """Force immediate config reload.

        Returns:
            True if config changed, False if same

        """
        try:
            old_config = self._config
            old_hash = self._last_hash

            self._config = load_config(self._config_path)
            self._update_hash()
            self._stats.last_reload_time = time.time()  # type: ignore[attr-defined]

            if old_hash != self._last_hash:
                self._stats.reloads += 1  # type: ignore[attr-defined]
                changed_keys = self._diff_configs(old_config, self._config)
                self._notify_listeners(self._config, changed_keys)
                logger.info(f"Config reloaded, {len(changed_keys)} keys changed")
                return True
            else:
                logger.debug("Config unchanged after forced reload")
                return False

        except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
            self._stats.errors += 1  # type: ignore[attr-defined]
            logger.error(f"Failed to reload config: {e}")
            return False

    def _watch_loop(self) -> None:
        """Background thread that polls for config changes."""
        logger.debug(f"Watch loop started, polling every {self._poll_interval}s")

        while not self._stop_event.is_set():
            try:
                self._check_for_changes()
            except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
                logger.error(f"Error checking config: {e}")

            self._stop_event.wait(self._poll_interval)

    def _check_for_changes(self) -> None:
        """Check if config file has been modified."""
        if not self._config_path.exists():
            return

        # Get file modification time
        try:
            mtime = os.path.getmtime(self._config_path)
        except OSError:
            return

        # Check if modified since last check
        if self._stats.last_mtime == 0:  # type: ignore[attr-defined]
            self._stats.last_mtime = mtime  # type: ignore[attr-defined]
            return

        if mtime <= self._stats.last_mtime:  # type: ignore[attr-defined]
            return  # No change

        self._stats.last_mtime = mtime  # type: ignore[attr-defined]

        # File modified - check content hash
        try:
            new_hash = self._compute_hash()
            if new_hash != self._last_hash:
                logger.debug("Config file changed, reloading...")
                self.reload_now()
        except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
            logger.error(f"Error computing hash: {e}")

    def _compute_hash(self) -> str:
        """Compute hash of config file content."""
        if not self._config_path.exists():
            return ""

        content = self._config_path.read_bytes()
        return hashlib.sha256(content).hexdigest()[:16]

    def _update_hash(self) -> None:
        """Update stored hash from current file."""
        self._last_hash = self._compute_hash()

    def _diff_configs(self, old: WorkflowConfig | None, new: WorkflowConfig) -> set[str]:
        """Find changed keys between two configs."""
        if old is None:
            return {"all"}

        changed = set()

        # Compare each section
        sections = [
            "orchestrator",
            "goose",
            "budget",
            "cache",
            "rate_limit",
            "timeout",
            "logging",
            "mcp",
        ]
        for section in sections:
            old_section = getattr(old, section, None)
            new_section = getattr(new, section, None)

            if old_section is None or new_section is None:
                changed.add(section)
                continue

            # Compare each field in section
            for field_name in old_section.__dataclass_fields__:
                old_val = getattr(old_section, field_name, None)
                new_val = getattr(new_section, field_name, None)
                if old_val != new_val:
                    changed.add(f"{section}.{field_name}")

        return changed

    def _notify_listeners(self, config: WorkflowConfig, changed_keys: set[str]) -> None:
        """Notify all listeners of config change."""
        with self._listener_lock:
            listeners = self._listeners.copy()

        for callback in listeners:
            try:
                callback(config, changed_keys)
            except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
                logger.error(f"Config listener callback failed: {e}")


def get_config_watcher() -> ConfigWatcher:
    """Get the global config watcher singleton.

    Returns:
        ConfigWatcher instance

    """
    return ConfigWatcher.get_instance()


def start_config_watcher() -> ConfigWatcher:
    """Start the global config watcher.

    Returns:
        ConfigWatcher instance (now watching for changes)

    """
    watcher = get_config_watcher()
    watcher.start()
    return watcher


def stop_config_watcher() -> None:
    """Stop the global config watcher."""
    watcher = get_config_watcher()
    watcher.stop()


__all__ = [
    "ConfigCallback",
    "ConfigWatcher",
    "WatcherStats",
    "get_config_watcher",
    "start_config_watcher",
    "stop_config_watcher",
]
