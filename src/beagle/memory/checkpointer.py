"""LangGraph checkpoint persistence for Beagle v12.0.

Wraps AsyncSqliteSaver for automatic state persistence
across workflow runs, enabling resume from failure.

Phase 6 enhancement: Factory pattern supporting both SQLite (local)
and PostgreSQL (LangGraph Cloud) based on BEAGLE_EXECUTION_ENV.

SECURITY (DevSecOps pass):
- Replaced JsonPlusSerializer(pickle_fallback=True) with
  JsonPlusSerializer(pickle_fallback=False) (CVE-2025-64439 —
  default allows arbitrary code execution on deserialization
  fallback via pickle/torch/numpy handlers).
- SQLite PRAGMA: journal_mode=WAL, synchronous=NORMAL for thread-safe
  concurrent reads without lock contention.
- Restrictive chmod (0o700) on the checkpoint database directory.
"""

from __future__ import annotations

import contextlib
import os
import re
import stat
from pathlib import Path
from typing import Any

from beagle.config.paths import get_checkpoint_dir

try:
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
except ImportError:
    AsyncSqliteSaver = None  # type: ignore[assignment,misc]

# SECURITY (DevSecOps CVE-2025-64439 fix):
# JsonPlusSerializer defaults to pickle_fallback=True, which allows
# arbitrary code execution during deserialization (RCE vector).
# We instantiate JsonPlusSerializer(pickle_fallback=False) to
# explicitly disable pickle deserialization — this is the safe mode.
# If JsonPlusSerializer is unavailable, we fail-closed (RuntimeError).
try:
    from langgraph.checkpoint.serde.jsonplus import (
        JsonPlusSerializer as _JsonPlusSerializer,
    )
except ImportError:  # pragma: no cover — langgraph is a hard dependency
    _JsonPlusSerializer = None  # type: ignore[assignment,misc]


def _make_secure_serde():
    """Create a serializer with pickle fallback DISABLED (CVE-2025-64439).

    Fail-closed: if JsonPlusSerializer is unavailable, raise RuntimeError
    rather than silently degrading.
    """
    if _JsonPlusSerializer is None:
        raise RuntimeError("langgraph JsonPlusSerializer unavailable")
    return _JsonPlusSerializer(pickle_fallback=False)


def _secure_db_directory(db_path: Path) -> None:
    """Apply restrictive permissions to the checkpoint database directory.

    Sets directory to mode 0o700 (owner rwx only) to prevent other users
    from reading or tampering with checkpoint data.

    Args:
        db_path: Path to the database file (directory will be secured).

    """
    db_dir = db_path.parent
    db_dir.mkdir(parents=True, exist_ok=True)
    # Best effort — may fail on NFS or readonly filesystems
    with contextlib.suppress(OSError):
        db_dir.chmod(stat.S_IRWXU)  # 0o700 — owner only


def _apply_sqlite_pragmas(conn: Any) -> None:
    """Apply security and performance PRAGMAs to a SQLite connection.

    - journal_mode=WAL: Write-Ahead Logging allows concurrent reads
      without blocking writers, eliminating lock contention under
      multi-threaded LangGraph orchestration.
    - synchronous=NORMAL: Writes are durable enough for checkpoints
      while avoiding the full fsync stall of FULL mode.

    Args:
        conn: A sqlite3.Connection or aiosqlite connection.

    """
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    except (OSError, RuntimeError, ValueError) as exc:  # catch: NARROWED
        import logging as _ckpt_log

        # Not critical to correctness, but an operator should know the database
        # is running without WAL: concurrent readers will block on writers.
        _ckpt_log.getLogger("Beagle.checkpointer").warning(
            "Cannot apply the WAL/synchronous PRAGMAs to the checkpoint database (%s); "
            "it runs with SQLite's default journal mode.",
            exc,
        )


def create_checkpointer(db_path: str | Path | None = None) -> Any:
    """Factory: return the appropriate checkpointer for the execution environment.

    Backend selection priority:
      1. Explicit ``db_path`` argument (SQLite only — back-compat).
      2. ``BEAGLE_EXECUTION_ENV=cloud`` env var → PostgreSQL.
      3. ``config.state.type`` from config.toml ("sqlite" | "postgresql").

    Args:
        db_path: Explicit SQLite path (overrides config when provided).

    Returns:
        Checkpointer context manager (SQLite or Postgres).

    """
    env_mode = os.environ.get("BEAGLE_EXECUTION_ENV", "local")

    if env_mode == "cloud":
        return _create_postgres_checkpointer()

    # Honor [state].type from config.toml when no explicit db_path was passed.
    if db_path is None:
        try:
            from beagle.config.config import get_config

            st = get_config().state
        except (
            ImportError,
            AttributeError,
            RuntimeError,
            OSError,
        ):  # catch: NARROWED  # RATIONALE=four-tuple: lazy import of sql backend, attribute on backend attr, runtime guard, OS path errors on checkpoint dir
            st = None
        if st is not None and st.type == "postgresql":
            return _create_postgres_checkpointer(conn_string=st.connection_string)

    return _create_sqlite_checkpointer(db_path)


def _create_postgres_checkpointer(conn_string: str | None = None) -> Any:
    """Create a PostgreSQL-backed checkpointer for LangGraph Cloud.

    Connection string resolution order:
      1. Explicit ``conn_string`` arg (from ``config.state.connection_string``).
      2. ``POSTGRES_URI`` env var (LangGraph Cloud convention).
    Validates the URI scheme to prevent connection string injection (OWASP A03).
    """
    # v1.0.2: validate the URI BEFORE importing the optional backend.
    #
    # The import used to come first, so on any install without the
    # langgraph-checkpoint-postgres extra — which is the default, it is not a
    # declared dependency — a malformed or injected connection string produced
    # "install langgraph-checkpoint-postgres" instead of "invalid scheme". The
    # OWASP A03 check below was unreachable in exactly the configuration where
    # someone is most likely to be pasting a connection string in by hand, and
    # its two security tests were disabled with a stale note claiming the
    # sqlite backend was missing.
    #
    # Input validation must not depend on an optional import: reject bad input
    # first, then load the backend.
    postgres_uri = conn_string or os.environ.get("POSTGRES_URI", "")
    if not postgres_uri:
        raise RuntimeError(
            "PostgreSQL state backend requires either [state].connection_string "
            "in config.toml or POSTGRES_URI env var."
        )

    # Validate URI scheme to prevent connection string injection (OWASP A03)
    allowed_schemes = ("postgresql://", "postgres://")
    if not any(postgres_uri.lower().startswith(scheme) for scheme in allowed_schemes):
        raise ValueError(
            f"Invalid PostgreSQL URI scheme. Must start with one of {allowed_schemes}. "
            f"Got: {postgres_uri[:50]}... (truncated for security)"
        )

    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    except ImportError as exc:
        raise ImportError(
            "langgraph-checkpoint-postgres is required for PostgreSQL state. "
            "Install with: pip install langgraph-checkpoint-postgres psycopg[binary]"
        ) from exc

    return AsyncPostgresSaver.from_conn_string(postgres_uri)


def _create_sqlite_checkpointer(db_path: str | Path | None = None) -> Any:
    """Create a SQLite-backed checkpointer for local execution.

    SECURITY:
    - Uses JsonPlusSerializer(pickle_fallback=False) instead of
      JsonPlusSerializer(pickle_fallback=True) (CVE-2025-64439 —
      pickle fallback allows arbitrary code execution on deserialization).
    - Applies restrictive chmod (0o700) to the database directory.
    - SQLite PRAGMAs (journal_mode=WAL, synchronous=NORMAL) are applied
      on first connection to prevent lock contention.

    Args:
        db_path: Path to the checkpoint database.

    Returns:
        Async context manager yielding an AsyncSqliteSaver with secure
        serializer and PRAGMAs.

    Raises:
        ImportError: If langgraph-checkpoint-sqlite is not installed.
        RuntimeError: If secure serde factory is not available (fail-closed).

    """
    if AsyncSqliteSaver is None:
        raise ImportError(
            "langgraph-checkpoint-sqlite is required for local checkpointing. "
            "Install with: pip install langgraph-checkpoint-sqlite"
        )

    if db_path is None:
        # Resolution order:
        #   1. BEAGLE_CHECKPOINT_DIR env (legacy escape hatch, highest priority)
        #   2. config.state.connection_string (relative paths anchor on data_root)
        #   3. paths.get_checkpoint_dir() (XDG cache fallback)
        env_dir = os.environ.get("BEAGLE_CHECKPOINT_DIR", "").strip()
        from_env = bool(env_dir)
        cfg_conn = ""
        try:
            from beagle.config.config import get_config

            cfg_conn = (get_config().state.connection_string or "").strip()
        except (
            ImportError,
            AttributeError,
            RuntimeError,
            OSError,
        ):  # catch: NARROWED  # RATIONALE=four-tuple: lazy import of sql backend, attribute on backend attr, runtime guard, OS path errors on checkpoint dir
            cfg_conn = ""

        if from_env and cfg_conn:
            import logging as _ckpt_log

            _ckpt_log.getLogger("Beagle.checkpointer").warning(
                "BEAGLE_CHECKPOINT_DIR and [state].connection_string are both set; "
                "env var wins for backward compatibility. Consider removing "
                "BEAGLE_CHECKPOINT_DIR and relying on config.toml going forward."
            )

        if from_env:
            ck_dir = Path(env_dir)
            ck_dir.mkdir(parents=True, exist_ok=True)
            db_path = ck_dir / "beagle_checkpoints.db"
        elif cfg_conn:
            p = Path(cfg_conn)
            if not p.is_absolute():
                from beagle.config.paths import get_data_root

                p = get_data_root() / p
            p.parent.mkdir(parents=True, exist_ok=True)
            db_path = p
        else:
            checkpoint_dir = get_checkpoint_dir()
            checkpoint_dir.mkdir(parents=True, exist_ok=True)
            db_path = checkpoint_dir / "beagle_checkpoints.db"

    db_path = Path(db_path)

    # Apply restrictive directory permissions
    _secure_db_directory(db_path)

    # Build the checkpointer with secure serde (pickle fallback disabled)
    if _make_secure_serde is None:
        # SECURITY: Fail-closed — we will NOT fall back to default
        # JsonPlusSerializer (which has pickle_fallback=True by default).
        raise RuntimeError(
            "Secure serde factory not available — refusing to use "
            "JsonPlusSerializer with pickle_fallback=True due to "
            "CVE-2025-64439 (RCE via pickle deserialization fallback). "
            "Install langgraph-checkpoint>=3.0.0."
        )

    serde = _make_secure_serde()

    # We must construct the saver manually because from_conn_string()
    # does not accept a serde argument. We create a custom async context
    # manager that opens the connection, applies PRAGMAs, and yields
    # the saver with our secure serde.
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _secure_sqlite_saver():
        import aiosqlite

        async with aiosqlite.connect(str(db_path)) as conn:
            # Apply SQLite PRAGMAs for WAL mode and safe synchronization
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA synchronous=NORMAL")
            # Create the saver with our secure serde
            saver = AsyncSqliteSaver(conn, serde=serde)
            # setup() may fail if tables already exist
            with contextlib.suppress(RuntimeError, OSError, TimeoutError, ValueError):
                await saver.setup()
            yield saver

    return _secure_sqlite_saver()


# ── Legacy API (backward compatible) ────────────────────────────────────────


def get_checkpointer(db_path: str | Path | None = None) -> Any:
    """Get a LangGraph checkpointer context manager.

    Automatically selects SQLite (local) or Postgres (cloud)
    based on BEAGLE_EXECUTION_ENV.

    Args:
        db_path: Path to the checkpoint database (SQLite only).

    Returns:
        AsyncSqliteSaver or AsyncPostgresSaver context manager.

    """
    return create_checkpointer(db_path)


def reset_checkpointer() -> None:
    """Reset the singleton (not used in CM pattern but kept for API)."""
    pass


# ── Checkpoint filter-key allowlist (defense-in-depth for CVE-2025-67644) ────
# CVE-2025-67644: SQL injection via metadata filter keys in
# SqliteSaver.list/alist. Even with langgraph-checkpoint-sqlite>=3.0.1,
# we enforce an explicit key allowlist so that no caller can inject
# arbitrary filter keys into the SQL query.
# (import re is at the top of the module)


_ALLOWED_CHECKPOINT_FILTER_KEYS: frozenset[str] = frozenset(
    {
        "thread_id",
        "user_id",
        "tenant_id",
        "tag",
        "workflow_name",
    }
)
_SAFE_FILTER_KEY = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")


def scrub_checkpoint_filter(filter_dict: dict) -> dict:
    """Validate and return a filter dict safe for SqliteSaver.alist().

    Rejects any key not in the allowlist or any key that doesn't match
    a safe identifier pattern. This prevents SQL injection via filter
    keys (CVE-2025-67644).

    Args:
        filter_dict: Raw filter dict from caller (keys may be user-controlled).

    Returns:
        The same dict if all keys are safe.

    Raises:
        ValueError: If any key is not in the allowlist or has unsafe characters.

    """
    rejected = [
        k
        for k in filter_dict
        if k not in _ALLOWED_CHECKPOINT_FILTER_KEYS or not _SAFE_FILTER_KEY.match(k)
    ]
    if rejected:
        raise ValueError(
            f"Rejected checkpoint filter keys (CVE-2025-67644 defense): {rejected}. "
            f"Allowed keys: {sorted(_ALLOWED_CHECKPOINT_FILTER_KEYS)}"
        )
    return filter_dict
