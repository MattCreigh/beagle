"""Process-wide locks shared by the ingestion / hot-swap subsystem.

Why this module exists (audit B-1, v13.22.2)
--------------------------------------------
``mcp_rag_server`` used to own ``_swap_lock`` privately, and only its two
MCP tool handlers (``rag_ingest``, ``rag_hotswap_ingest``) acquired it.
The v13.22.x auto-reingest path — ``rag_search`` → ``RAGStalenessTracker.
trigger_reingest_async`` → ``asyncio.to_thread(_sync_reingest)`` →
``hotswap_ingest`` — bypassed the lock entirely. A manual hot-swap and an
automatic one could therefore interleave inside
``swap_staged_to_live()``, where the second run's backup step moves the
first run's freshly-swapped data into the backup directory before moving
staging on top. The live index ends up in an undefined state.

Putting the lock in a leaf module means every entry point can import it
without creating an import cycle, and there is exactly one lock object
per process.

``SWAP_LOCK`` is an ``RLock`` on purpose: the MCP tool handlers acquire it
and then call ``hotswap_ingest()``, which acquires it again on the same
thread. Re-entrancy makes that nesting safe while still rejecting a
*different* thread's non-blocking acquire.
"""

from __future__ import annotations

import threading

# Serializes ingestion and hot-swap operations (staging writes, connection
# release, and the atomic swap) across the whole process.
SWAP_LOCK = threading.RLock()

__all__ = ["SWAP_LOCK"]
