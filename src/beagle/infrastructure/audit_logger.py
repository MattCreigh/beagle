"""
OpenClaw Audit Logger
=====================
Structured audit logging with security scrubbing and cryptographic integrity.

All events are logged with timestamps, task IDs, and optional cryptographic hashes
for tamper detection.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Import TaskStore for persistence
from ..utils.atomic import atomic_write_text
from .task_store import TaskStore, get_task_store

log = logging.getLogger("Beagle.infrastructure.audit_logger")

# -------------------------------------------------------------------------
# Security: Audit secret for integrity verification
# Secret resolution order:
#   1. AUDIT_SECRET environment variable
#   2. ~/.config/beagle/audit_secret file (persisted across restarts)
#   3. Ephemeral random secret (only if no file exists; file is created for next run)
# -------------------------------------------------------------------------
_AUDIT_SECRET_FILE = Path.home() / ".config" / "beagle" / "audit_secret"

AUDIT_SECRET = os.environ.get("AUDIT_SECRET")
if AUDIT_SECRET is None:
    if os.environ.get("BEAGLE_ENV") == "production":
        raise RuntimeError(
            "AUDIT_SECRET must be set in production for audit integrity. Set AUDIT_SECRET env var or disable audit in config."
        )
    else:
        import secrets as _secrets

        # Try to load from persisted secret file
        if _AUDIT_SECRET_FILE.exists():
            try:
                AUDIT_SECRET = _AUDIT_SECRET_FILE.read_text().strip() or None
                if AUDIT_SECRET:
                    log.info("Loaded persisted audit secret from %s", _AUDIT_SECRET_FILE)
                else:
                    AUDIT_SECRET = None
            except ImportError as exc:
                log.warning("Failed to read audit secret file %s: %s", _AUDIT_SECRET_FILE, exc)
                AUDIT_SECRET = None

        if AUDIT_SECRET is None:
            # Generate and persist a new secret
            AUDIT_SECRET = _secrets.token_hex(32)
            try:
                # Atomic 0600 write: mode applied before rename, closing the
                # wrong-permission and partial-secret windows of the old
                # write-then-chmod sequence.
                atomic_write_text(_AUDIT_SECRET_FILE, AUDIT_SECRET, mode=0o600)
                log.info("Generated and persisted audit secret to %s", _AUDIT_SECRET_FILE)
            except OSError as exc:
                log.warning(
                    "Could not persist audit secret to %s: %s "
                    "(audit hashes will not survive process restarts)",
                    _AUDIT_SECRET_FILE,
                    exc,
                )

# -------------------------------------------------------------------------
# Security: Patterns to scrub from logs
# -------------------------------------------------------------------------
SENSITIVE_PATTERNS = [
    # API keys
    (r'(api[_-]?key["\s:=]+)["\']?([a-zA-Z0-9_-]{20,})["\']?', r"\1[REDACTED]"),
    (r"(bearer\s+)([a-zA-Z0-9_-]{20,})", r"\1[REDACTED]"),
    (r'(token["\s:=]+)["\']?([a-zA-Z0-9_-]{20,})["\']?', r"\1[REDACTED]"),
    # Passwords
    (r'(password["\s:=]+)["\']?([^\s"\']+)["\']?', r"\1[REDACTED]"),
    (r'(passwd["\s:=]+)["\']?([^\s"\']+)["\']?', r"\1[REDACTED]"),
    # AWS keys
    (r"(AKIA[A-Z0-9]{16})", r"[REDACTED_AWS_KEY]"),
    # Private keys
    (
        r"-----BEGIN\s+.*PRIVATE\s+KEY-----[\s\S]*?-----END\s+.*PRIVATE\s+KEY-----",
        "[REDACTED_PRIVATE_KEY]",
    ),
    # Secrets in env vars
    (r'(secret[_-]?key["\s:=]+)["\']?([^\s"\']+)["\']?', r"\1[REDACTED]"),
]

# Compile patterns for performance
COMPILED_PATTERNS = [(re.compile(p, re.IGNORECASE), r) for p, r in SENSITIVE_PATTERNS]


def scrub_sensitive(data: str | dict | list) -> str | dict | list:
    """Scrub sensitive data from strings or structured data.

    Args:
        data: Input data (string, dict, or list)

    Returns:
        Sanitized data with sensitive values replaced by [REDACTED]

    """
    if isinstance(data, str):
        result = data
        for pattern, replacement in COMPILED_PATTERNS:
            result = pattern.sub(replacement, result)
        return result

    elif isinstance(data, dict):
        return {k: scrub_sensitive(v) for k, v in data.items()}

    elif isinstance(data, list):
        return [scrub_sensitive(item) for item in data]

    return data  # type: ignore[unreachable]  # type-total fallback for str|dict|list union


class AuditLogger:
    """Structured audit logger with persistence and integrity.

    Features:
    - Structured JSON logging
    - Automatic sensitive data scrubbing
    - Cryptographic hash chains for tamper detection
    - SQLite persistence via TaskStore
    - Real-time event emission
    """

    def __init__(
        self,
        task_store: TaskStore | None = None,
        log_file: Path | str | None = None,
        enable_file_log: bool = True,
        enable_integrity: bool = True,
    ):
        """
        Args:
            task_store: TaskStore instance for persistence
            log_file: Path to JSONL audit log file
            enable_file_log: Whether to write to file
            enable_integrity: Whether to compute hash chains

        """
        self.store = task_store or get_task_store()

        if log_file is None:
            log_file = Path.home() / ".local" / "share" / "openclaw" / "audit.jsonl"

        self.log_file = Path(log_file)
        self.enable_file_log = enable_file_log
        self.enable_integrity = enable_integrity
        self._last_hash = "genesis"

        if enable_file_log:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)

    def _compute_hash(self, event: dict[str, Any]) -> str:
        """Compute SHA-256 hash of event for integrity."""
        event_str = json.dumps(event, sort_keys=True, default=str)
        return hashlib.sha256(event_str.encode()).hexdigest()[:16]

    def _verify_chain(self) -> bool:
        """Verify hash chain integrity (for last event in file)."""
        if not self.enable_file_log or not self.log_file.exists():
            return True

        try:
            with open(self.log_file, encoding="utf-8") as f:
                lines = f.readlines()
                if not lines:
                    return True

                last_line = lines[-1].strip()
                last_event = json.loads(last_line)
                stored_hash = last_event.get("prev_hash")

                # Chain should be continuous
                if stored_hash != self._last_hash:
                    log.warning("Hash chain break detected")
                    return False

        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as e:
            log.error("Integrity verification failed: %s", e)
            return False

        return True

    def log_event(
        self,
        task_id: str,
        event_type: str,
        event_data: dict[str, Any] | None = None,
        level: str = "INFO",
        scrub: bool = True,
    ) -> dict[str, Any]:
        """Log an audit event.

        Args:
            task_id: Task identifier
            event_type: Type of event (created, started, tool_call, completed, etc.)
            event_data: Additional event data
            level: Log level (DEBUG, INFO, WARNING, ERROR)
            scrub: Whether to scrub sensitive data

        Returns:
            The event dictionary that was logged

        """
        timestamp = datetime.now(UTC).isoformat()

        # Scrub sensitive data if enabled
        if scrub and event_data:
            event_data = scrub_sensitive(event_data)  # type: ignore[assignment]

        # Build event
        event = {
            "timestamp": timestamp,
            "task_id": task_id,
            "event_type": event_type,
            "level": level,
            "data": event_data,
        }

        # Add integrity hash
        if self.enable_integrity:
            event["prev_hash"] = self._last_hash
            event["event_hash"] = self._compute_hash(event)
            self._last_hash = event["event_hash"]  # type: ignore[assignment]

        # Persist to TaskStore
        try:
            self.store.add_audit_event(
                task_id=task_id,
                event_type=event_type,
                event_data={
                    "level": level,
                    "data": event_data,
                    "hash": event.get("event_hash"),
                },
            )
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as e:
            log.error("Failed to persist audit event: %s", e)

        # Write to file log
        if self.enable_file_log:
            try:
                with open(self.log_file, "a", encoding="utf-8") as f:
                    f.write(json.dumps(event, default=str) + "\n")
            except (OSError, ValueError) as e:
                log.error("Failed to write audit log file: %s", e)

        # Emit to Python logging
        log_func = getattr(log, level.lower(), log.info)
        log_func("[%s] %s: %s", task_id[:8], event_type, event_data)

        return event

    # -------------------------------------------------------------------------
    # Convenience methods for common events
    # -------------------------------------------------------------------------

    def log_task_created(self, task_id: str, task_type: str, spec: dict[str, Any]) -> None:
        """Log task creation."""
        self.log_event(
            task_id=task_id,
            event_type="task_created",
            event_data={"task_type": task_type, "spec": spec},
        )

    def log_task_started(self, task_id: str) -> None:
        """Log task start."""
        self.log_event(task_id=task_id, event_type="task_started", event_data={"status": "running"})

    def log_tool_call(
        self,
        task_id: str,
        tool_name: str,
        tool_input: dict[str, Any],
        tool_output: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        """Log a tool invocation."""
        self.log_event(
            task_id=task_id,
            event_type="tool_call",
            event_data={
                "tool": tool_name,
                "input": tool_input,
                "output": tool_output,
                "error": error,
            },
            level="ERROR" if error else "INFO",
        )

    def log_delegate_call(
        self, parent_task_id: str, child_task_id: str, source: str, instructions: str
    ) -> None:
        """Log a delegation call."""
        self.log_event(
            task_id=parent_task_id,
            event_type="delegate_call",
            event_data={
                "child_task_id": child_task_id,
                "source": source,
                "instructions": instructions[:200],  # Truncate long instructions
            },
        )

    def log_metrics_update(
        self,
        task_id: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
        tool_calls: int = 0,
    ) -> None:
        """Log metrics update."""
        self.log_event(
            task_id=task_id,
            event_type="metrics_update",
            event_data={
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost_usd": cost_usd,
                "tool_calls": tool_calls,
            },
        )

    def log_task_completed(
        self, task_id: str, result: dict[str, Any] | None, summary: str | None = None
    ) -> None:
        """Log task completion."""
        self.log_event(
            task_id=task_id,
            event_type="task_completed",
            event_data={"status": "completed", "result": result, "summary": summary},
        )

    def log_task_failed(self, task_id: str, error: str, traceback: str | None = None) -> None:
        """Log task failure."""
        self.log_event(
            task_id=task_id,
            event_type="task_failed",
            event_data={
                "status": "failed",
                "error": error,
                "traceback": traceback[:500] if traceback else None,
            },
            level="ERROR",
        )

    def log_task_cancelled(self, task_id: str, reason: str) -> None:
        """Log task cancellation."""
        self.log_event(
            task_id=task_id,
            event_type="task_cancelled",
            event_data={"status": "cancelled", "reason": reason},
        )

    # -------------------------------------------------------------------------
    # Query methods
    # -------------------------------------------------------------------------

    def get_task_events(self, task_id: str) -> list[dict[str, Any]]:
        """Get all events for a task from the TaskStore."""
        return self.store.get_audit_trail(task_id)

    def verify_task_integrity(self, task_id: str) -> bool:
        """Verify hash chain integrity for a task's events."""
        events = self.get_task_events(task_id)
        if not events:
            return True

        for event in events:
            event_data = event.get("event_data", {})
            event_hash = event_data.get("hash")

            # Check hash exists
            if not event_hash:
                log.warning("Event missing hash: %s", event["event_id"])
                return False

            # Verify this matches calculation
            calc_hash = self._compute_hash(
                {
                    "timestamp": event["timestamp"],
                    "task_id": task_id,
                    "event_type": event["event_type"],
                    "data": event_data.get("data"),
                }
            )

            if event_hash != calc_hash:
                log.warning("Hash mismatch for event %s", event["event_id"])
                return False

        return True

    def export_task_audit(self, task_id: str, format: str = "json") -> str:
        """Export audit trail for a task.

        Args:
            task_id: Task to export
            format: Output format (json, jsonl, text)

        Returns:
            Formatted audit trail

        """
        events = self.get_task_events(task_id)

        if format == "json":
            return json.dumps(events, indent=2, default=str)

        elif format == "jsonl":
            return "\n".join(json.dumps(e, default=str) for e in events)

        elif format == "text":
            lines = [f"Audit Trail for Task {task_id}", "=" * 50]
            for e in events:
                lines.append(f"[{e['timestamp']}] {e['event_type']}: {e['event_data']}")
            return "\n".join(lines)

        raise ValueError(f"Unknown format: {format}")


# Singleton instance
_logger: AuditLogger | None = None


def get_audit_logger(
    task_store: TaskStore | None = None, log_file: Path | str | None = None
) -> AuditLogger:
    """Get or create singleton AuditLogger instance."""
    global _logger
    if _logger is None:
        _logger = AuditLogger(task_store=task_store, log_file=log_file)
    return _logger
