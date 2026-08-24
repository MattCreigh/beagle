"""Session usage bridge — read the live goose CLI session's context occupancy.

The context that fills up on the CLI belongs to the goose session, not to a
Beagle process.  Beagle's MCP servers are separate processes and never observe
it.  This module reads the number the harness itself displays: the
``total_tokens`` column of the goose sessions database, divided by the
``GOOSE_CONTEXT_LIMIT`` that goose 1.44.0 actually reads.

Constraint D5: a missing measurement is not a low measurement.  When the
database or the limit is absent, ``read_session_usage()`` returns None and the
caller writes a diagnostic.  It never reports 0.0 percent, because 0.0 percent
means "no fold needed" — the exact failure this module repairs.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("Beagle.context.session_usage")


@dataclass(frozen=True)
class SessionUsage:
    """A single reading of the live goose CLI session's context occupancy.

    Attributes:
        used_tokens: Live window occupancy from the sessions database.
        max_tokens: The declared context limit (GOOSE_CONTEXT_LIMIT).
        session_id: The goose session id the reading came from.
        source: Always "sessions.db".
        limit_source: "$GOOSE_CONTEXT_LIMIT" or the config path.

    """

    used_tokens: int
    max_tokens: int
    session_id: str
    source: str  # "sessions.db"
    limit_source: str  # "$GOOSE_CONTEXT_LIMIT" or the config path

    @property
    def percentage(self) -> float:
        """Return the fraction of the context window in use (0.0-1.0)."""
        if self.max_tokens <= 0:
            return 0.0
        return self.used_tokens / self.max_tokens


def _resolve_db_path() -> Path | None:
    """Resolve the goose sessions database path.

    Priority: the ``GOOSE_SESSIONS_DB`` environment variable; then
    ``$XDG_DATA_HOME/goose/sessions/sessions.db``; then
    ``~/.local/share/goose/sessions/sessions.db``.

    Returns:
        The database path, or None when the file is absent.

    """
    env = os.environ.get("GOOSE_SESSIONS_DB", "")
    if env:
        path = Path(env)
    else:
        data_home = os.environ.get("XDG_DATA_HOME", "")
        if data_home:
            path = Path(data_home) / "goose" / "sessions" / "sessions.db"
        else:
            path = Path.home() / ".local" / "share" / "goose" / "sessions" / "sessions.db"

    if not path.is_file():
        logger.warning("Goose sessions database absent: %s", path)
        return None
    return path


def _resolve_session_id(conn: sqlite3.Connection, session_id: str | None) -> str | None:
    """Resolve the session id to read.

    Priority: the ``session_id`` argument; then the ``AGENT_SESSION_ID``
    environment variable; then the most recently updated row.

    Args:
        conn: Read-only connection to the sessions database.
        session_id: Explicit session id, if any.

    Returns:
        The session id, or None when no row can be resolved.

    """
    if session_id:
        return session_id
    env_id = os.environ.get("AGENT_SESSION_ID", "")
    if env_id:
        return env_id

    try:
        row = conn.execute("SELECT id FROM sessions ORDER BY updated_at DESC LIMIT 1").fetchone()
    except sqlite3.Error as exc:
        logger.warning("Failed to resolve newest session id: %s", exc)
        return None
    if row is None:
        logger.warning("Goose sessions table is empty")
        return None
    return str(row[0])


def _resolve_limit() -> tuple[int, str] | None:
    """Resolve the context limit.

    Priority: the ``GOOSE_CONTEXT_LIMIT`` environment variable; then the
    top-level ``GOOSE_CONTEXT_LIMIT`` key in the goose config YAML.

    Returns:
        A (limit, source) tuple, or None when neither gives a positive int.

    """
    env = os.environ.get("GOOSE_CONTEXT_LIMIT", "")
    if env:
        try:
            value = int(env)
        except ValueError:
            logger.warning("GOOSE_CONTEXT_LIMIT is not an integer: %r", env)
        else:
            if value > 0:
                return value, "$GOOSE_CONTEXT_LIMIT"

    config_home = os.environ.get("XDG_CONFIG_HOME", "")
    if config_home:
        config_path = Path(config_home) / "goose" / "config.yaml"
    else:
        config_path = Path.home() / ".config" / "goose" / "config.yaml"

    if config_path.is_file():
        try:
            import yaml
        except ImportError:
            logger.warning("PyYAML unavailable; cannot read %s", config_path)
        else:
            try:
                data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            except (OSError, yaml.YAMLError) as exc:
                logger.warning("Failed to parse %s: %s", config_path, exc)
            else:
                if isinstance(data, dict):
                    raw = data.get("GOOSE_CONTEXT_LIMIT")
                    if raw is not None:
                        try:
                            value = int(raw)
                        except (TypeError, ValueError):
                            logger.warning(
                                "GOOSE_CONTEXT_LIMIT in %s is not an integer: %r",
                                config_path,
                                raw,
                            )
                        else:
                            if value > 0:
                                return value, str(config_path)

    logger.warning("GOOSE_CONTEXT_LIMIT is unresolved (env and %s)", config_path)
    return None


def read_session_usage(session_id: str | None = None) -> SessionUsage | None:
    """Read the live goose CLI session's context occupancy.

    Reads ``total_tokens`` from the goose sessions database over a read-only
    URI and divides by the declared ``GOOSE_CONTEXT_LIMIT``.  Returns None —
    never a 0.0 reading — when the database, the row, or the limit is missing.

    Args:
        session_id: Explicit session id.  When None, the newest row is used.

    Returns:
        A SessionUsage reading, or None when any input is missing.

    """
    db_path = _resolve_db_path()
    if db_path is None:
        return None

    limit = _resolve_limit()
    if limit is None:
        return None
    max_tokens, limit_source = limit

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error as exc:
        logger.warning("Failed to open goose sessions database read-only: %s", exc)
        return None

    try:
        resolved_id = _resolve_session_id(conn, session_id)
        if resolved_id is None:
            return None

        row = conn.execute(
            "SELECT total_tokens FROM sessions WHERE id = ?", (resolved_id,)
        ).fetchone()
        if row is None:
            logger.warning("No sessions row for id %r", resolved_id)
            return None

        total_tokens = row[0]
        if total_tokens is None or int(total_tokens) <= 0:
            logger.warning("total_tokens for session %r is NULL or 0", resolved_id)
            return None

        return SessionUsage(
            used_tokens=int(total_tokens),
            max_tokens=max_tokens,
            session_id=resolved_id,
            source="sessions.db",
            limit_source=limit_source,
        )
    except sqlite3.Error as exc:
        logger.warning("Failed to read goose session usage: %s", exc)
        return None
    finally:
        conn.close()


__all__ = ["SessionUsage", "read_session_usage"]
