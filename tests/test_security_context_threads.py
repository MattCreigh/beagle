"""8-thread hammer test for SecurityContext (R4.1, v13.20.6).

Verifies the thread-safety fix for SecurityContext.log_error / log_scrub
/ log_blocked. Without the lock, the prior implementation had a
race: two threads appending to `validation_errors` simultaneously
could lose entries, and `scrubbed_count += 1` is famously
non-atomic on CPython (it is byte-code atomic on 64-bit builds for
small ints, but the list.append is not, and the audit C7 concern
was about the process-global state being inconsistent across
callers).

The test runs 8 threads * 1000 calls each, mixed across the three
mutation methods, and asserts the final state matches the
expected aggregate (8000 total events, no lost updates).
"""

from __future__ import annotations

import threading

from beagle.security.validation import (
    reset_security_context,
)


def test_security_context_8_thread_hammer() -> None:
    """8 threads * 1000 mixed mutations, expect no lost updates."""
    reset_security_context()
    from beagle.security.validation import get_security_context

    ctx = get_security_context()
    errors_per_thread = 1000
    n_threads = 8

    barrier = threading.Barrier(n_threads)

    def worker(thread_id: int) -> None:
        barrier.wait()  # release all threads simultaneously
        for i in range(errors_per_thread):
            # Mix all three mutation paths to exercise the lock uniformly
            ctx.log_error(f"t{thread_id}-e{i}")
            ctx.log_scrub(f"t{thread_id}-s{i}")
            ctx.log_blocked(f"t{thread_id}-b{i}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    summary = ctx.get_summary()
    expected = n_threads * errors_per_thread
    assert summary["validation_errors"] == expected, (
        f"validation_errors: got {summary['validation_errors']}, expected {expected}"
    )
    assert summary["secrets_scrubbed"] == expected, (
        f"secrets_scrubbed: got {summary['secrets_scrubbed']}, expected {expected}"
    )
    assert summary["operations_blocked"] == expected, (
        f"operations_blocked: got {summary['operations_blocked']}, expected {expected}"
    )
    # The first 5 errors should be from thread 0 (barrier release order is
    # not guaranteed, but the *count* is what we care about for thread-safety)
    assert len(summary["errors"]) == 5
    assert len(summary["blocked"]) == 5
    # No exceptions, no lost updates.
