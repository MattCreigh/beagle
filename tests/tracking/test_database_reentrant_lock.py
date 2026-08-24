"""Pin v13.19.3 fix: TrackingDatabase._lock MUST be a non-blocking
reentrant lock so get_instance() → _init_db() → _get_conn() does
not deadlock during shutdown's _flush_database path.

This test would deadlock (then fail by timeout) under
`threading.Lock`. It passes quickly under `threading.RLock`.
"""

from __future__ import annotations

import threading


def test_tracking_database_lock_is_reentrant() -> None:
    """Direct invariant: _lock must allow reentrant acquisition."""
    from beagle.tracking.database import (
        TrackingDatabase,
    )

    lock = TrackingDatabase._lock
    # Acquire twice from the same thread. Lock() would deadlock
    # on the second acquire. RLock() returns immediately.
    acquired_once = lock.acquire(timeout=2.0)
    assert acquired_once, "could not acquire lock at all"
    try:
        acquired_twice = lock.acquire(timeout=2.0)
        assert acquired_twice, (
            "TrackingDatabase._lock is NOT reentrant; "
            "second acquire from the same thread deadlocked. "
            "This is the v13.19.3 bug — change Lock() to RLock() "
            "at tracking/database.py:23."
        )
        lock.release()
    finally:
        lock.release()


def test_get_instance_then_get_conn_does_not_deadlock() -> None:
    """Functional invariant: the actual code path that deadlocks
    under Lock() must complete under RLock()."""
    from beagle.tracking.database import (
        TrackingDatabase,
    )

    completed = threading.Event()

    def call_chain() -> None:
        try:
            db = TrackingDatabase.get_instance()
            # _get_conn is internal but is the actual reentrant
            # caller in shutdown's _flush_database path.
            conn = db._get_conn()
            assert conn is not None
        finally:
            completed.set()

    t = threading.Thread(target=call_chain, daemon=True)
    t.start()
    finished = completed.wait(timeout=5.0)
    assert finished, (
        "TrackingDatabase.get_instance() + _get_conn() hung >5s. "
        "v13.19.3 regression — Lock() reverted? Check "
        "tracking/database.py:23 for `threading.RLock()`."
    )
