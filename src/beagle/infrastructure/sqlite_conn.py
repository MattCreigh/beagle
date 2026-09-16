"""Shared SQLite connection ownership for the thread-local stores.

D-19: three stores — the dead-letter queue, the reconciliation store and the
task store — each kept their connection in a ``threading.local()`` and closed
only the calling thread's handle. A store whose connection was opened on a
worker thread therefore leaked that descriptor for the life of the process, and
``close()``, the only public teardown, silently did nothing.

Two measured facts made the old shape unreachable rather than merely
incomplete:

* ``sqlite3.connect`` defaults to ``check_same_thread=True``, which makes
  ``close()`` from a foreign thread raise ``ProgrammingError``. The old code
  could not have closed a foreign connection even if it had tried.
* ``sqlite3.threadsafety`` is 3 on the supported interpreter, so the module
  serialises concurrent access and a connection may safely be created with
  ``check_same_thread=False``.

The base below therefore connects with ``check_same_thread=False``, keeps one
connection per thread in a plain dict keyed by thread id, and closes every one
of them from any thread. A dict rather than ``threading.local`` is deliberate:
``threading.local`` cannot be enumerated, so a closing thread cannot reach a
peer thread's handle — which is precisely the defect.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path

logger = logging.getLogger("Beagle.infrastructure.sqlite_conn")


class ThreadLocalSQLite:
    """One SQLite connection per thread, all of them closable from anywhere.

    Subclasses call ``super().__init__(db_path)`` and then create their schema.
    They inherit ``_get_conn()`` and ``close()``.
    """

    def __init__(self, db_path: Path | str) -> None:
        """Store the path and prepare the connection registry.

        Args:
            db_path: Path to the SQLite database file. Parent directories are
                created if they do not exist.
        """
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # Keyed by ``threading.get_ident()``. Held under ``_lock`` because
        # ``close()`` may run on a different thread than the one that opened a
        # connection, and it must clear the whole map, not just its own slot.
        self._conns: dict[int, sqlite3.Connection] = {}
        self._lock = threading.RLock()

    def _connect(self) -> sqlite3.Connection:
        """Open a new connection with the shared pragmas applied."""
        # RATIONALE=check_same_thread=False is required for close() to be total:
        # with the default True, closing a connection from another thread raises
        # ProgrammingError, which is the defect this base exists to fix.
        # sqlite3.threadsafety == 3 guarantees the module serialises access.
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _get_conn(self) -> sqlite3.Connection:
        """Return this thread's connection, opening it on first use."""
        thread_id = threading.get_ident()
        with self._lock:
            conn = self._conns.get(thread_id)
            if conn is None:
                conn = self._connect()
                self._conns[thread_id] = conn
            return conn

    def close(self) -> None:
        """Close every connection this store opened, from any thread.

        The registry is cleared first, so a later ``_get_conn`` reconnects
        rather than handing back a closed handle. A connection another thread
        closed between the snapshot and the call is skipped: its descriptor is
        already released, and raising here would make teardown fail on a
        success path.
        """
        with self._lock:
            conns = list(self._conns.values())
            self._conns.clear()
        for conn in conns:
            try:
                conn.close()
            except sqlite3.ProgrammingError:
                # Already closed by its owning thread. Dropping the last
                # reference is all that remains, and the loop below does it.
                logger.debug("SQLite connection already closed during teardown")

    @property
    def open_connection_count(self) -> int:
        """Number of live connections the store is holding.

        Exposed so teardown can be asserted directly instead of inferred from a
        raised exception.
        """
        with self._lock:
            return len(self._conns)


__all__ = ["ThreadLocalSQLite"]
