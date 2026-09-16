"""DAG scheduler fault-recovery hardening — public API.

This package implements the three-phase fault-recovery hardening for the Beagle
DAG scheduler:

**Phase 1 — WAL/Outbox**: :mod:`.outbox` (Redis Streams write-ahead log) plus
:mod:`.reconciliation` (SQLite materialisation daemon).

**Phase 2 — Backpressure + DLQ**: :mod:`.dlq` (SQLite dead-letter queue) is
populated by the circuit breaker when it trips on consecutive 429/504
responses.

**Phase 3 — Sandbox layering**: :mod:`.sandbox` routes untrusted payloads
through the ``[sandbox] mode`` config flag, layering wasmtime over the AST
validator as a best-effort stub (no hard wasmtime dependency).

Every component degrades softly: if Redis, wasmtime, or the config layer is
unavailable, the existing workflow behaviour is preserved.
"""

from __future__ import annotations

from .dlq import (
    DEFAULT_DB_NAME as DLQ_DEFAULT_DB_NAME,
)
from .dlq import (
    DeadLetterQueue,
)
from .dlq import (
    default_db_path as dlq_default_db_path,
)
from .outbox import (
    DEFAULT_GROUP,
    DEFAULT_STREAM,
    EVENT_COMPLETED,
    EVENT_PENDING,
    OutboxClient,
    OutboxError,
)
from .reconciliation import (
    DEFAULT_DB_NAME as RECON_DEFAULT_DB_NAME,
)
from .reconciliation import (
    ReconciliationDaemon,
    ReconciliationStore,
)
from .reconciliation import (
    default_db_path as recon_default_db_path,
)
from .sandbox import (
    DEFAULT_MODE as SANDBOX_DEFAULT_MODE,
)
from .sandbox import (
    VALID_MODES,
    get_sandbox_mode,
    route_payload,
)

__all__ = [
    "DLQ_DEFAULT_DB_NAME",
    "DEFAULT_GROUP",
    "DEFAULT_STREAM",
    "DeadLetterQueue",
    "EVENT_COMPLETED",
    "EVENT_PENDING",
    "OutboxClient",
    "OutboxError",
    "RECON_DEFAULT_DB_NAME",
    "ReconciliationDaemon",
    "ReconciliationStore",
    "SANDBOX_DEFAULT_MODE",
    "VALID_MODES",
    "dlq_default_db_path",
    "get_sandbox_mode",
    "recon_default_db_path",
    "route_payload",
]
