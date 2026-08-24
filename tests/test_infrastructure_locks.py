"""SP-5: tests for infrastructure/_locks (was zero-coverage).

beagle-spotless-phase2, work package SP-5. The process-wide swap lock guards
RAG ingestion/hot-swap against interleaving. These exercise its identity and
re-entrancy contract.
"""

from __future__ import annotations

import threading

from beagle.infrastructure._locks import SWAP_LOCK


def test_swap_lock_is_rlock() -> None:
    """SWAP_LOCK is a re-entrant lock (same-thread re-acquire is safe)."""
    assert isinstance(SWAP_LOCK, type(threading.RLock()))


def test_swap_lock_is_singleton_module_constant() -> None:
    """SWAP_LOCK is exported in __all__ and is the same object."""
    from beagle.infrastructure import _locks

    assert SWAP_LOCK is _locks.SWAP_LOCK
    assert "SWAP_LOCK" in _locks.__all__


def test_swap_lock_acquire_release() -> None:
    """The lock can be acquired and released."""
    acquired = SWAP_LOCK.acquire(timeout=1.0)
    assert acquired is True
    SWAP_LOCK.release()


def test_swap_lock_is_reentrant() -> None:
    """RLock re-entrancy allows nested acquire on the same thread."""
    SWAP_LOCK.acquire(timeout=1.0)
    SWAP_LOCK.acquire(timeout=1.0)  # same thread → succeeds
    SWAP_LOCK.release()
    SWAP_LOCK.release()
