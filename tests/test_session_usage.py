"""Tests for the session-usage bridge (BGL-032).

The bridge reads the live goose CLI session's context occupancy from the
goose sessions database over a read-only URI, and the denominator from
GOOSE_CONTEXT_LIMIT.  A missing source returns None — never a 0.0 reading.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def _make_db(path: Path) -> None:
    """Create a minimal sessions table with one row."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, total_tokens INTEGER, updated_at TEXT)"
    )
    conn.execute(
        "INSERT INTO sessions (id, total_tokens, updated_at) VALUES (?, ?, ?)",
        ("sess-1", 50000, "2026-08-19T00:00:00Z"),
    )
    conn.execute(
        "INSERT INTO sessions (id, total_tokens, updated_at) VALUES (?, ?, ?)",
        ("sess-2", 90000, "2026-08-19T01:00:00Z"),
    )
    conn.commit()
    conn.close()


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    """Create a sessions db and point the bridge at it via env vars.

    Returns the database path so tests can assert against it.
    """
    path = tmp_path / "sessions.db"
    _make_db(path)
    monkeypatch.setenv("GOOSE_SESSIONS_DB", str(path))
    monkeypatch.setenv("GOOSE_CONTEXT_LIMIT", "100000")
    monkeypatch.delenv("AGENT_SESSION_ID", raising=False)
    return path


def test_selects_row_by_id(db_path):
    from beagle.context.session_usage import read_session_usage

    u = read_session_usage(session_id="sess-1")
    assert u is not None
    assert u.session_id == "sess-1"
    assert u.used_tokens == 50000
    assert u.max_tokens == 100000
    assert u.percentage == 0.5


def test_selects_newest_row_with_no_id(db_path):
    from beagle.context.session_usage import read_session_usage

    u = read_session_usage()
    assert u is not None
    assert u.session_id == "sess-2"
    assert u.used_tokens == 90000
    assert u.percentage == 0.9


def test_returns_none_when_file_absent(monkeypatch, tmp_path):
    monkeypatch.setenv("GOOSE_SESSIONS_DB", str(tmp_path / "missing.db"))
    monkeypatch.setenv("GOOSE_CONTEXT_LIMIT", "100000")
    from beagle.context.session_usage import read_session_usage

    assert read_session_usage() is None


def test_returns_none_when_limit_unset(monkeypatch, db_path):
    monkeypatch.setenv("GOOSE_SESSIONS_DB", str(db_path))
    monkeypatch.delenv("GOOSE_CONTEXT_LIMIT", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    # Point the config fallback at a nonexistent path so the limit is truly
    # unresolved.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(Path("/nonexistent-config")))
    from beagle.context.session_usage import read_session_usage

    assert read_session_usage() is None


def test_connection_is_read_only(db_path):
    """An INSERT through the same read-only URI must raise OperationalError."""
    from beagle.context.session_usage import _resolve_db_path

    path = _resolve_db_path()
    assert path is not None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute(
                "INSERT INTO sessions (id, total_tokens, updated_at) VALUES (?, ?, ?)",
                ("sess-3", 1, "2026-08-19T00:00:00Z"),
            )
    finally:
        conn.close()
