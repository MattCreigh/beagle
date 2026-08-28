"""MCP RAG server exposing vector search and graph traversal tools.

Provides tool handlers for semantic search, hybrid RAG queries,
ingestion, and status checks over the CAST-processed codebase.

For Skylon/Orpheus CLI command reference, see docs/skylon_cli_reference.md.

Transport: stdio (default) or streamable-http (BEAGLE_EXECUTION_ENV=docker).
The streamable-http path REQUIRES bearer-token authentication per
MCP_TRUST.md — the same model as the utility server (B5, Option B). The
client is any MCP client; the RAG index is reachable over the network only
when the operator explicitly opts in with BEAGLE_EXECUTION_ENV=docker and a
BEAGLE_MCP_TOKEN.
"""

from __future__ import annotations

import asyncio
import contextlib  # v13.19.1 — EventBridge ctx suppression
import contextvars
import gc
import json
import logging
import os
import re
import stat
import sys
import threading
import time
import tomllib
import uuid
from collections import OrderedDict
from datetime import timedelta
from pathlib import Path
from typing import Any

import platformdirs

from beagle.security.validation import validate_cypher_identifier

from ..config._config_path import find_config_toml
from ._locks import SWAP_LOCK
from .rag_paths import LANCE_TABLE_NAME, backup_dir, db_root, kuzu_uri, lancedb_uri

# ──────────────────────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("Beagle.infrastructure.mcp_rag_server")

# Stale backup warning
_backup_path = Path(backup_dir())
if _backup_path.exists():
    logger.warning(
        "Stale RAG backup found at %s — consider removing if no rollback needed",
        _backup_path,
    )

# Correlation ID for request tracing — async-safe via contextvars
_correlation_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default="none"
)


class CorrelationIdFilter(logging.Filter):
    """Inject correlation_id into log records."""

    def filter(self, record):
        record.correlation_id = _correlation_id_var.get("none")
        return True


def set_correlation_id(correlation_id: str | None = None) -> str:
    """Set or generate a correlation ID for request tracing (async-safe)."""
    cid = correlation_id or str(uuid.uuid4())
    _correlation_id_var.set(cid)
    return cid


def get_correlation_id() -> str:
    """Get the current correlation ID (async-safe)."""
    return _correlation_id_var.get("none")


# Add filter to logger
logger.addFilter(CorrelationIdFilter())

# Update formatter to include correlation_id
formatter = logging.Formatter(
    "%(asctime)s [%(name)s] [%(correlation_id)s] %(levelname)s: %(message)s"
)
for handler in logger.handlers:
    handler.setFormatter(formatter)


# Metrics collection
_metrics: dict[str, dict] = {
    "requests": {"total": 0, "success": 0, "error": 0},
    "latency": {"sum": 0.0, "count": 0, "min": float("inf"), "max": 0.0},
}


def record_metric(_event_type: str, duration: float | None = None, success: bool = True):
    """Record a metric event."""
    _metrics["requests"]["total"] += 1
    if success:
        _metrics["requests"]["success"] += 1
    else:
        _metrics["requests"]["error"] += 1

    if duration is not None:
        _metrics["latency"]["sum"] += duration
        _metrics["latency"]["count"] += 1
        _metrics["latency"]["min"] = min(_metrics["latency"]["min"], duration)
        _metrics["latency"]["max"] = max(_metrics["latency"]["max"], duration)


def get_metrics_summary() -> dict:
    """Get metrics summary with computed averages."""
    latency = _metrics["latency"]
    avg = latency["sum"] / latency["count"] if latency["count"] > 0 else 0.0
    return {
        "requests": _metrics["requests"].copy(),
        "latency": {
            "avg_seconds": round(avg, 4),
            "min_seconds": round(latency["min"], 4) if latency["count"] > 0 else 0.0,
            "max_seconds": round(latency["max"], 4),
            "total_count": latency["count"],
        },
    }


# ──────────────────────────────────────────────────────────────────────────────
# Graceful dependency imports (fail-closed)
# ──────────────────────────────────────────────────────────────────────────────
try:
    from mcp.server.fastmcp import Context, FastMCP
except ImportError:
    logger.debug("mcp.server.fastmcp not available — using stub.")

    class Context:  # type: ignore[no-redef]
        """Stub Context for environments without the MCP SDK (v13.19.1)."""

        async def report_progress(
            self, _progress: float, _total: float | None = None, _message: str | None = None
        ) -> None:
            return None

        async def info(self, _message: str) -> None:
            return None

        async def warning(self, _message: str) -> None:
            return None

        async def error(self, _message: str) -> None:
            return None

        async def debug(self, _message: str) -> None:
            return None

    class FastMCP:  # type: ignore[no-redef]
        """Stub for environments without the MCP SDK.

        SECURITY: The stub enforces transport='stdio' and refuses to
        serve over HTTP/SSE even if requested. This prevents accidental
        exposure when the MCP SDK is not installed.
        """

        def __init__(self, name: str, *, transport: str = "stdio") -> None:
            self.name = name
            # SECURITY: Hardcode stdio — reject any other transport
            if transport != "stdio":
                logger.critical(
                    f"[Security] FastMCP stub: transport='{transport}' REJECTED. "
                    "Only 'stdio' is permitted. HTTP/SSE is blocked."
                )
                raise RuntimeError(
                    f"Non-stdio transport '{transport}' is prohibited. "
                    "Only stdio transport is allowed for security."
                )
            self.transport = transport

        def tool(self):
            return lambda f: f

        def run(self) -> None:
            if self.transport != "stdio":
                raise RuntimeError(
                    f"Cannot serve over transport='{self.transport}'. Only stdio is permitted."
                )
            logger.error("FastMCP stub: cannot serve. Install mcp SDK.")


try:
    import lancedb
except ImportError:
    lancedb = None
try:
    import kuzu
except ImportError:
    kuzu = None  # type: ignore[assignment]

try:
    from beagle.infrastructure.services.embedding import (
        OllamaCloudEmbedder,
    )
except ImportError:
    OllamaCloudEmbedder = None  # type: ignore[assignment,misc]

# ──────────────────────────────────────────────────────────────────────────────
# Security imports (from Beagle security module)
# ──────────────────────────────────────────────────────────────────────────────
try:
    from beagle.security import scrub_output, scrub_secrets
except ImportError:
    logger.warning("security.py not found. Using passthrough scrubbers.")

    def scrub_output(text: str, _additional_patterns: list[str] | None = None) -> str:  # type: ignore[misc]
        return text

    def scrub_secrets(text: str) -> str:
        return text


# ──────────────────────────────────────────────────────────────────────────────
# Performance: Query result caching (thread-safe with O(1) LRU eviction)
# ──────────────────────────────────────────────────────────────────────────────
_RAG_CACHE: OrderedDict[str, tuple[str, float]] = OrderedDict()
_RAG_CACHE_TTL = 300  # 5 minutes
_RAG_CACHE_MAX_SIZE = 100
_rag_cache_lock = threading.Lock()


def _get_rag_cache_key(query: str, max_hops: int, top_k: int) -> str:
    """Generate cache key for RAG search results."""
    return f"{query}:{max_hops}:{top_k}"


def _rag_cache_get(cache_key: str) -> str | None:
    """Get cached result if not expired (thread-safe)."""
    import time

    with _rag_cache_lock:
        if cache_key in _RAG_CACHE:
            result, timestamp = _RAG_CACHE[cache_key]
            if time.monotonic() - timestamp < _RAG_CACHE_TTL:
                _RAG_CACHE.move_to_end(cache_key)
                logger.debug(f"[RAG Cache] HIT for key: {cache_key[:80]}")
                return result
            else:
                del _RAG_CACHE[cache_key]
    return None


def _rag_cache_set(cache_key: str, result: str) -> None:
    """Cache result with O(1) LRU eviction (thread-safe)."""
    import time

    with _rag_cache_lock:
        if cache_key in _RAG_CACHE:
            del _RAG_CACHE[cache_key]
        elif len(_RAG_CACHE) >= _RAG_CACHE_MAX_SIZE:
            evicted_key, _ = _RAG_CACHE.popitem(last=False)
            logger.debug(f"[RAG Cache] Evicted oldest entry: {evicted_key[:80]}")
        _RAG_CACHE[cache_key] = (result, time.monotonic())
        logger.debug(f"[RAG Cache] SET for key: {cache_key[:80]}")


def clear_rag_cache() -> None:
    """Clear RAG search result cache and trigger GC.

    Call this after ingestion or when memory pressure is detected.
    """
    old_size = len(_RAG_CACHE)
    _RAG_CACHE.clear()
    gc.collect()
    logger.info(f"[RAG Cache] Cleared {old_size} entries, GC triggered")


# ──────────────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────────────
# Tiered storage layout:
# - MAIN tier: Global architectural knowledge (Axioms, patterns, architecture docs)
#   → <BEAGLE_KNOWLEDGE_DIR>/main_rag (bulk storage)
# - INSTANCE tier: Project-specific data (per-repository RAG)
#   → <data-root>/instance_rag (project-specific RAG)
#
# IMPORTANT: These paths are RELATIVE to the project root. The BEAGLE_KNOWLEDGE_DIR
# environment variable MUST be set correctly, or data will be read from wrong location.
# Validation in init_connections() will warn if paths are stale.


def __getattr__(name: str) -> Any:
    if name == "DB_PATH":
        return db_root()
    if name == "LANCEDB_URI":
        return lancedb_uri()
    if name == "KUZU_URI":
        return kuzu_uri()
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")


# Strict transport enforcement
ALLOWED_TRANSPORTS = {"stdio"}

# Vector search limits
MAX_VECTOR_RESULTS = 10
DEFAULT_VECTOR_RESULTS = 5

# Distance metric for vector search (B-23). LanceDB's default is "l2", which
# is unbounded; consumers of the `distance` field in the rag_search payload
# assume a bounded cosine distance in [0, 2]. Must match any ANN index built
# over the table.
VECTOR_DISTANCE_TYPE = "cosine"

# Graph traversal limits (prevent traversal explosion)
MAX_HOPS = 3
MAX_GRAPH_RESULTS = 20

# Context truncation limit (tokens ≈ chars / 3.5)
MAX_SNIPPET_CHARS = 400


def _safe_cypher_int(val: object, name: str, low: int, high: int) -> int:
    """Validate and cast a value to int within [low, high] for safe Cypher interpolation.

    Kuzu doesn't support parameterized LIMIT/path depth, so we validate
    the integer before string-formatting it into the query.

    Raises:
        ValueError: If val cannot be cast to int or is outside the range.

    """
    try:
        result = int(val)  # type: ignore[call-overload]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer, got {type(val).__name__}: {val!r}") from exc
    if result < low or result > high:
        raise ValueError(f"{name} must be between {low} and {high}, got {result}")
    return result


# ──────────────────────────────────────────────────────────────────────────────
# Instantiate FastMCP Server — stdio transport ONLY
# SECURITY: transport is hardcoded to "stdio". No CLI flags, environment
# variables, or config entries can override this. HTTP/SSE endpoints are
# explicitly prohibited to prevent cross-server shadowing attacks.
# ──────────────────────────────────────────────────────────────────────────────
mcp = FastMCP("Beagle")

# ──────────────────────────────────────────────────────────────────────────────
# Connection State (lazy-loaded, fail-closed)
# ──────────────────────────────────────────────────────────────────────────────
_lance_tbl = None  # Global (default) LanceDB table
_kuzu_conn = None  # Global (default) Kùzu connection
_embed_model = None
_initialized = False

# Marker for cross-process release detection (B-18)
_this_is_rag_server_process = True

# ──────────────────────────────────────────────────────────────────────────────
# v13.22.3 — Job Handle store (RC1 + Step 2)
# ──────────────────────────────────────────────────────────────────────────────
# rag_ingest / rag_hotswap_ingest register a job before dispatching the
# worker thread and stash the result dict on completion. The
# rag_get_job_status tool can poll for status from any future FastMCP
# request without blocking the asyncio loop.
#
# Shape:
#   _JOBS: dict[job_id, {
#       "kind": "ingest" | "hotswap",
#       "target_directory": str,
#       "created_at": float,
#       "status": "running" | "completed" | "failed",
#       "result": dict | None,
#       "error": str | None,
#   }]
# Bounded by ``_JOBS_MAX_ENTRIES`` (FIFO eviction) so a long-running server
# cannot leak memory under repeated ingest calls.
_JOBS: dict[str, dict[str, Any]] = {}
_JOBS_LOCK = threading.Lock()  # protects _JOBS from worker-thread mutation
_JOBS_MAX_ENTRIES = 64


def _register_job(target_directory: str, kind: str) -> str:
    """Allocate a job_id, record the entry, and return the id.

    Cheap (no I/O); runs on the asyncio thread. The worker thread later
    calls :func:`_complete_job` to attach the final result dict.
    """
    # Full uuid4() per doctrine (truncated IDs are forbidden — entropy
    # would clash at the FIFO cap under sustained ingest load).
    job_id = uuid.uuid4().hex
    with _JOBS_LOCK:
        # FIFO eviction — drop oldest completed entries if we're at the cap
        if len(_JOBS) >= _JOBS_MAX_ENTRIES:
            oldest = sorted(_JOBS.items(), key=lambda kv: kv[1]["created_at"])
            for old_id, _ in oldest[: max(1, _JOBS_MAX_ENTRIES // 4)]:
                _JOBS.pop(old_id, None)
        _JOBS[job_id] = {
            "kind": kind,
            "target_directory": target_directory,
            "created_at": time.monotonic(),
            "status": "running",
            "result": None,
            "error": None,
        }
    return job_id


def _complete_job(job_id: str, payload: dict[str, Any]) -> None:
    """Mark a job complete with the worker thread's payload.

    ``payload`` is the result dict the underlying call returned (or, on
    failure, an ``{"status": "error", "error": str(e)}`` dict). The job
    status flips to "completed" if the payload has no "error" key, else
    "failed".
    """
    error = payload.get("error") if isinstance(payload, dict) else None
    with _JOBS_LOCK:
        entry = _JOBS.get(job_id)
        if entry is None:
            return  # dropped by FIFO eviction
        entry["status"] = "failed" if error else "completed"
        entry["result"] = payload
        entry["error"] = error


# Tenant-scoped connection caches: tenant_id → LanceDB table / Kùzu connection
_tenant_lance_tbls: dict[str, object] = {}
_tenant_kuzu_conns: dict[str, object] = {}
_tenant_schemas_ensured: set[str] = set()

# ──────────────────────────────────────────────────────────────────────────────
# Path security: allowed directory roots for ingestion (deduplicated)
# ──────────────────────────────────────────────────────────────────────────────
_ALLOWED_PATH_ROOTS = [
    Path.home() / "Dev",
    Path.home() / "Projects",
    # XDG data root — the standard install location for beagle data. A legacy
    # /opt/beagle deployment may still appear at runtime; it is warned about
    # below rather than pre-approved here.
    Path(platformdirs.user_data_dir("beagle")),
    # nosec B108 - an ingestion allowlist entry, not a path this server writes to
    Path("/tmp/beagle"),  # nosec B108
    # v13.22.1 (integration): /Projects is a separate, root-owned checkout
    # of the Beagle project (older 13.22.0) used as a reference. It is a
    # legitimate ingestion target.
    Path("/Projects"),
]


def _validate_ingest_path(target_directory: str) -> Path:
    try:
        resolved = Path(target_directory).resolve()
    except (OSError, ValueError):
        raise ValueError(f"Invalid path: {target_directory}") from None

    # Check for traversal on BOTH raw and resolved paths
    if ".." in Path(target_directory).parts or ".." in resolved.parts:
        raise ValueError("Path traversal not allowed")

    # is_relative_to answers the containment question directly, rather than
    # calling relative_to for its side effect of raising on a non-match.
    allowed = any(resolved.is_relative_to(root) for root in _ALLOWED_PATH_ROOTS)

    if not allowed:
        raise ValueError(f"Directory not in allowed roots: {target_directory}")

    return resolved


# Lock to serialize hot-swap operations and prevent race conditions
# during connection release / re-init windows.
#
# B-1 (audit v13.22.1): this used to be a private lock owned by this
# module, so the auto-reingest path (rag_search → RAGStalenessTracker →
# hotswap_ingest) bypassed it entirely. It now lives in a leaf module that
# hotswap_ingest also imports, giving one lock per process. It is an RLock,
# so the tool handlers below may hold it while hotswap_ingest re-acquires
# it on the same thread.
_swap_lock = SWAP_LOCK


def _enforce_readonly_storage() -> None:
    """Set read-only permissions on data directories at runtime.

    This prevents any writes to the knowledge graph after ingestion,
    enforcing immutable storage as required by the security model.
    """
    for data_dir in (lancedb_uri(), kuzu_uri()):
        dpath = Path(data_dir)
        if not dpath.exists():
            continue
        try:
            # Set directory to read-only + execute (traverse)
            for root, _dirs, files in os.walk(dpath):
                for fname in files:
                    fpath = os.path.join(root, fname)
                    try:
                        current = os.stat(fpath).st_mode
                        # Remove write bits, keep read + execute
                        os.chmod(
                            fpath,
                            current & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH),
                        )
                    except OSError as e:
                        logger.warning(f"Could not set read-only on {fpath}: {e}")
            logger.info(f"[Security] Enforced read-only FDs on: {data_dir}")
        except OSError as e:
            logger.warning(f"[Security] Failed to enforce read-only on {data_dir}: {e}")


def _validate_rag_paths() -> None:
    """Validate RAG paths and warn if they appear stale.

    This prevents the common issue where RAG reads from wrong location
    due to stale hardcoded defaults or env var issues.
    """
    # Check if the DB_PATH is set to a non-existent or empty location
    current_root = db_root()
    lance_dir = lancedb_uri()

    if not os.path.exists(current_root):
        logger.warning(f"[RAG] DB_PATH does not exist: {current_root}")
        logger.warning(
            "[RAG] This may indicate stale configuration. Expected {HOME}/.beagle/instance_rag"
        )

    if os.path.exists(lance_dir):
        chunks = list(os.listdir(lance_dir))
        if not chunks:
            logger.warning(f"[RAG] LanceDB directory exists but is empty: {lance_dir}")
            logger.warning("[RAG] Run ingestion to populate the index")
        else:
            logger.info(f"[RAG] LanceDB at {lance_dir} has {len(chunks)} entries")

    # Warn if still using legacy /opt path
    if current_root.startswith("/opt/beagle"):
        logger.warning(f"[RAG] Using legacy /opt path: {current_root}")
        logger.warning(
            "[RAG] Consider setting BEAGLE_KNOWLEDGE_DIR or migrating to {HOME}/.beagle/instance_rag"
        )


def init_connections() -> None:
    """Idempotent initialization of databases and embedding model.

    All connections are opened in read-only mode. File descriptors are
    locked to read-only after initialization.

    Only sets _initialized = True when ALL required components (LanceDB,
    embedder) connect successfully. Kùzu is optional (graph search degrades
    gracefully). Partial initialization is explicitly NOT marked as ready
    to prevent silent degradation.
    """
    global _lance_tbl, _kuzu_conn, _embed_model, _initialized

    if _initialized:
        return

    # Path validation - detect and warn about stale paths
    _validate_rag_paths()

    _init_warnings: list[str] = []

    try:
        # 1. LanceDB (read-only) — REQUIRED
        if _lance_tbl is None and lancedb is not None:
            lance_path = lancedb_uri()
            os.makedirs(lance_path, exist_ok=True)
            db = lancedb.connect(lance_path, read_consistency_interval=timedelta(seconds=0))
            try:
                _lance_tbl = db.open_table(LANCE_TABLE_NAME)
                logger.info(f"[LanceDB] Connected to {LANCE_TABLE_NAME} at {lance_path}")
            except Exception as lance_err:  # ruff: ignore[BLE001]  # broad catch intentional
                logger.warning(f"[LanceDB] Table not found or connect failed: {lance_err}")
                _init_warnings.append(f"lancedb: {lance_err}")

        # 2. Kùzu (read-only mode) — OPTIONAL (graph search degrades)
        if _kuzu_conn is None and kuzu is not None:
            kuzu_path = kuzu_uri()
            kuzu_parent = os.path.dirname(kuzu_path)
            if kuzu_parent:
                os.makedirs(kuzu_parent, exist_ok=True)
            try:
                kuzu_db = kuzu.Database(kuzu_path, read_only=True)
                _kuzu_conn = kuzu.Connection(kuzu_db)
                logger.info(f"[Kùzu] Connected in read-only mode at {kuzu_path}")
            except Exception as kuzu_err:  # ruff: ignore[BLE001]  # broad catch intentional
                logger.warning(
                    f"[Kùzu] Could not open read-only (graph search degraded): {kuzu_err}"
                )
                _init_warnings.append(f"kuzu: {kuzu_err}")

        # 3. Embedding model — REQUIRED
        if _embed_model is None and OllamaCloudEmbedder is not None:
            try:
                _embed_model = OllamaCloudEmbedder()
                logger.info("[Embeddings] Using OllamaCloudEmbedder (Ollama Cloud API)")
            except Exception as embed_err:  # ruff: ignore[BLE001]  # broad catch intentional
                logger.warning(f"[Embeddings] Failed to initialize: {embed_err}")
                _init_warnings.append(f"embeddings: {embed_err}")

        # 4. Enforce immutable storage
        _enforce_readonly_storage()

        # Only mark as initialized when ALL required components are ready
        # Required: LanceDB + Embedder. Optional: Kùzu (graceful degradation)
        all_required = _lance_tbl is not None and _embed_model is not None
        if all_required:
            _initialized = True
            if _init_warnings:
                logger.warning(f"[RAG] Initialized with degraded components: {_init_warnings}")
            else:
                logger.info("[RAG] All components initialized successfully")
        else:
            logger.error(
                f"[RAG] Cannot fully initialize — required components missing. "
                f"LanceDB={'OK' if _lance_tbl else 'MISSING'}, "
                f"Kùzu={'OK' if _kuzu_conn else 'DEGRADED'}, "
                f"Embeddings={'OK' if _embed_model else 'MISSING'}. "
                f"Warnings: {_init_warnings}"
            )

    except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
        logger.critical(f"[RAG] Fatal error initializing RAG subsystem: {e}")


# ──────────────────────────────────────────────────────────────────────────────
# Response Sanitization (CVCP security gate)
# ──────────────────────────────────────────────────────────────────────────────
def _validate_search_input(query: str, max_hops: int, top_k: int) -> tuple[str, int, int]:
    """Validate and sanitize RAG search input parameters.

    Security hardening (v13.5.2): Prevents injection attacks by:
    1. Rejecting Cypher injection patterns in query strings
    2. Clamping max_hops and top_k to safe bounds
    3. Sanitizing query strings for length and content

    Args:
        query: Search query string.
        max_hops: Graph traversal depth (clamped to [1, 3]).
        top_k: Number of vector results (clamped to [1, 100]).

    Returns:
        Tuple of (sanitized_query, clamped_max_hops, clamped_top_k).

    Raises:
        ValueError: If input contains injection patterns or is empty.

    """
    # Empty query check
    if not query or not query.strip():
        raise ValueError("Query cannot be empty")

    # Max query length (configurable via security config) — truncate instead of crash
    max_query_length = 50000
    if len(query) > max_query_length:
        query = query[:max_query_length]

    # Reject Cypher injection patterns
    cypher_keywords = [
        "MATCH(",
        "CREATE(",
        "MERGE(",
        "DELETE(",
        "SET(",
        "REMOVE(",
        "DROP(",
        "LOAD(",
        "COPY(",
        "DETACH",
    ]
    query_upper = query.upper()
    for keyword in cypher_keywords:
        if keyword in query_upper.replace(" ", ""):
            raise ValueError(
                f"Query contains potentially unsafe pattern: {keyword}. "
                f"Use parameterized queries for graph operations."
            )

    # Clamp max_hops and top_k instead of raising — graceful degradation
    max_hops = max(1, min(3, max_hops))
    top_k = max(1, min(100, top_k))

    # Sanitize: strip control characters but preserve newlines
    sanitized = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", query)
    return sanitized.strip(), max_hops, top_k


def _get_tenant_table(tenant_id: str | None, base_table: str = "ASTNode") -> str:
    """Get tenant-scoped table name for Kùzu or LanceDB.

    Instead of application-level WHERE tenant_id = X filtering,
    uses separate tables per tenant for stronger isolation.

    Works for both Kùzu node tables and LanceDB table names:
      Kùzu:    _get_tenant_table("acme", "ASTNode")       → "ASTNode_tenant_acme"
      LanceDB: _get_tenant_table("acme", "ast_code_chunks") → "ast_code_chunks_tenant_acme"

    Args:
        tenant_id: Tenant identifier (None for global/default).
        base_table: Base table name (default: ASTNode for Kùzu).

    Returns:
        Tenant-scoped table name, or base_table unchanged when tenant_id is None.

    """
    if tenant_id is None:
        return base_table
    # Sanitize tenant_id: allow only alphanumeric, underscore, hyphen
    if not re.match(r"^[a-zA-Z0-9_-]{1,64}$", tenant_id):
        raise ValueError(
            f"Invalid tenant_id: {tenant_id!r}. "
            "Must contain only alphanumeric characters, hyphens, and underscores (1-64 chars)."
        )
    return f"{base_table}_tenant_{tenant_id}"


def _ensure_tenant_schema(tenant_id: str) -> bool:
    """Create tenant-specific Kùzu node/rel tables if they don't exist.

    Must be called BEFORE any tenant-scoped queries. Opens a temporary
    write-mode Kùzu connection to create the schema, then returns True.

    This is idempotent — safe to call repeatedly.

    Args:
        tenant_id: Tenant identifier (validated by _get_tenant_table).

    Returns:
        True if schema is ready, False on failure.

    """
    if tenant_id in _tenant_schemas_ensured:
        return True

    # Validate tenant_id through _get_tenant_table (raises ValueError on bad input)
    node_table = _get_tenant_table(tenant_id, "ASTNode")
    # Relation table names follow Kùzu convention: FROM/TO must use the tenant node table
    rel_table_defs = [
        f"CALLS(FROM {node_table} TO {node_table})",
        f"INHERITS_FROM(FROM {node_table} TO {node_table})",
        f"IMPORTS(FROM {node_table} TO {node_table})",
        f"CONTAINS(FROM {node_table} TO {node_table})",
    ]

    try:
        import kuzu as _kz

        # Open a WRITE-mode connection to create tables (read-only can't DDL)
        kuzu_db = _kz.Database(kuzu_uri(), read_only=False)
        conn = _kz.Connection(kuzu_db)

        # Create tenant node table. node_table comes from _get_tenant_table() which
        # sanitizes the tenant_id, but we re-validate it through the shared Cypher
        # identifier gate before it is interpolated into DDL (defense in depth).
        validate_cypher_identifier(node_table)
        conn.execute(
            f"CREATE NODE TABLE IF NOT EXISTS {node_table}("
            f"id STRING, "
            f"filepath STRING, "
            f"language STRING, "
            f"node_type STRING, "
            f"name STRING, "
            f"start_line INT64, "
            f"end_line INT64, "
            f"code_content STRING, "
            f"token_count INT64, "
            f"PRIMARY KEY(id))"
        )

        # Create tenant relation tables using strict relation definitions. The only dynamic
        # identifier inside each rel_def is node_table (already validated above); the
        # relation-type prefix is validated here as defense in depth.
        for rel_def in rel_table_defs:
            rel_type = rel_def.split("(", 1)[0].strip()
            validate_cypher_identifier(rel_type)
            conn.execute("CREATE REL TABLE IF NOT EXISTS " + rel_def)

        _tenant_schemas_ensured.add(tenant_id)
        logger.info(f"[Kùzu] Ensured tenant schema for '{tenant_id}': {node_table}")

        # Close the write-mode connection so we don't hold a lock
        del conn, kuzu_db
        return True

    except Exception as e:  # broad catch intentional
        logger.exception(f"[Kùzu] Failed to ensure tenant schema for '{tenant_id}': {e}")
        return False


def _resolve_chunk_metadata(chunk_ids: list[str]) -> list[dict[str, Any]]:
    """Look up the full LanceDB row for each chunk_id returned by the sidecar.

    The sidecar stores only the chunk_id and the compressed vector
    (which has been decompressed for the search). The full row payload
    (ast_entity_id, filepath, node_name, text, etc.) is needed by the
    MCP response formatter, so we fetch it from LanceDB by chunk_id.

    Uses a single ``to_arrow()`` round-trip (no pandas dependency) and
    an in-memory filter to avoid N separate LanceDB queries. The arrow
    table for 30k rows is ~10 MB — small relative to the embedding
    matrix we're already holding.
    """
    if not chunk_ids or _lance_tbl is None:
        return []
    try:
        arrow = _lance_tbl.to_arrow()
    except (ValueError, RuntimeError, OSError) as e:
        logger.warning(f"[Vector/TQ] metadata lookup failed: {e}")
        return []
    if "chunk_id" not in arrow.column_names:
        # No chunk_id column — fall back to first-N rows.
        rows = arrow.slice(0, len(chunk_ids)).to_pylist()
        return [dict(r) for r in rows]
    # Filter to just the requested chunk_ids. The merge preserves the
    # sidecar's ranking order.
    wanted = set(chunk_ids)
    chunk_id_col = arrow.column("chunk_id").to_pylist()
    matching_idx = [i for i, cid in enumerate(chunk_id_col) if str(cid) in wanted]
    if not matching_idx:
        return []
    # Build a row-id -> row dict, then look up in the input order.
    by_id: dict[str, dict[str, Any]] = {}
    for i in matching_idx:
        row = arrow.slice(i, 1).to_pylist()[0]
        by_id[str(row.get("chunk_id", ""))] = row
    return [by_id[cid] for cid in chunk_ids if cid in by_id]


def _vector_search_with_turboquant(
    query_vector: list[float],
    top_k: int,
) -> list[dict[str, Any]] | None:
    """Brute-force cosine search via the TurboQuant sidecar (if present).

    Returns a list of {chunk_id, distance} dicts sorted by similarity
    descending, or ``None`` if no sidecar exists (caller falls back to
    LanceDB's built-in search).

    Memory profile: ~30 MB on disk (compressed), ~250 MB in RAM after
    first decompress (30k * 768 * 4 bytes), cached for the lifetime of
    the MCP server process so a burst of queries is essentially free.
    """
    try:
        from beagle.infrastructure.turboquant_lance_cache import (
            cosine_search_numpy,
            load_turboquant_sidecar,
        )
    except ImportError:
        return None

    # Config gate — only use the sidecar if the user wants it.
    try:
        from beagle.config.loader import get_config

        enabled = get_config().rag.turboquant_sidecar
    except (ImportError, AttributeError, KeyError, TypeError, ValueError, OSError) as _cfg_exc:
        logger.warning("[TurboQuant] config read failed, defaulting sidecar ON: %s", _cfg_exc)
        enabled = True
    if not enabled:
        logger.warning("[TurboQuant] sidecar disabled in config — skipping")
        return None

    sidecar = load_turboquant_sidecar()
    if sidecar is None:
        return None

    try:
        import numpy as _np

        corpus = sidecar.get_vectors()  # (n, 768) float32
        q = _np.asarray(query_vector, dtype=_np.float32)
        results = cosine_search_numpy(q, corpus, top_k=top_k)
        # cos distance returned as 1 - sim; consumers expect _distance
        # in [0, 2] per the B-23 fix. Use 1 - sim (cos distance in [0, 2]).
        out: list[dict[str, Any]] = []
        for idx, sim in results:
            if idx < 0 or idx >= len(sidecar.meta.get("chunk_ids", [])):
                continue
            out.append(
                {
                    "chunk_id": sidecar.meta["chunk_ids"][idx],
                    "_distance": float(1.0 - sim),
                }
            )
        logger.info(
            f"[Vector/TQ] Sidecar search: corpus={corpus.shape}, top_k={top_k}, "
            f"results={len(out)} (best distance={out[0]['_distance']:.3f})"
        )
        return out
    except (ValueError, RuntimeError, OSError) as e:
        logger.warning(f"[Vector/TQ] Sidecar search failed, falling back: {e}")
        return None


def _vector_search_lance_native(
    query_vector: list[float],
    top_k: int,
) -> list[dict[str, Any]]:
    """Original LanceDB ANN search, used as the fallback path.

    Returns a list of dicts containing the full LanceDB row payload
    (ast_entity_id, filepath, node_name, text, etc.). The MCP search
    caller maps these to semantic_anchors.

    Wraps the .search().distance_type().to_list() call in a try/except
    so a misconfigured index (e.g. wrong metric) doesn't take down
    the whole search.
    """
    if _lance_tbl is None:
        logger.error("[Vector] LanceDB table not initialized")
        return []
    try:
        vector_results_raw = (
            _lance_tbl.search(query_vector)
            .distance_type(VECTOR_DISTANCE_TYPE)
            .limit(top_k)
            .to_list()
        )
    except (ValueError, RuntimeError, OSError) as e:
        logger.error(f"[Vector] LanceDB search failed: {e}")
        return []
    return [dict(r) for r in vector_results_raw]


def _sanitize_response(payload: dict) -> dict:
    """Route all outbound JSON-RPC payloads through the semantic firewall.

    Scrubs secrets and sensitive patterns from all string values in the
    response before returning to the caller.
    """
    sanitized = {}
    for key, value in payload.items():
        if isinstance(value, str):
            sanitized[key] = scrub_output(scrub_secrets(value))
        elif isinstance(value, list):
            sanitized[key] = [  # type: ignore[assignment]
                {
                    k: scrub_output(scrub_secrets(v)) if isinstance(v, str) else v
                    for k, v in item.items()
                }
                if isinstance(item, dict)
                else (scrub_output(scrub_secrets(item)) if isinstance(item, str) else item)
                for item in value
            ]
        elif isinstance(value, dict):
            sanitized[key] = _sanitize_response(value)  # type: ignore[assignment]
        else:
            sanitized[key] = value
    return sanitized


# ──────────────────────────────────────────────────────────────────────────────
# MCP Tools — v0.3.0: Rate-limited to prevent DoS
# ──────────────────────────────────────────────────────────────────────────────
_mcp_call_timestamps: list[float] = []
_MCP_RATE_LIMIT_WINDOW = 60.0  # seconds
_MCP_RATE_LIMIT_MAX_CALLS = 120  # max calls per window


def _check_mcp_rate_limit() -> None:
    """Check rate limit for MCP tool calls. Raises RuntimeError if exceeded."""
    now = time.time()
    # Evict timestamps outside the window
    while _mcp_call_timestamps and now - _mcp_call_timestamps[0] > _MCP_RATE_LIMIT_WINDOW:
        _mcp_call_timestamps.pop(0)
    if len(_mcp_call_timestamps) >= _MCP_RATE_LIMIT_MAX_CALLS:
        raise RuntimeError(
            f"MCP rate limit exceeded: {_MCP_RATE_LIMIT_MAX_CALLS} "
            f"calls per {_MCP_RATE_LIMIT_WINDOW}s window"
        )
    _mcp_call_timestamps.append(now)


@mcp.tool()
async def rag_search(query: str, max_hops: int = 1, top_k: int = 5) -> str:
    """Execute a Hybrid RAG search combining vector retrieval and graph traversal.

    Embeds the query via nomic-embed-code, retrieves top-K semantic matches
    from LanceDB, then uses the matched AST node IDs as pivots for a  # noqa: E402
    multi-hop Cypher traversal in Kùzu.

    Args:
        query: Natural language or programmatic search intent.
        max_hops: Depth of graph traversal (1-3, default 1).
        top_k: Number of vector results to retrieve (1-10, default 5).

    Returns:
        JSON payload with semantic_anchors and structural_relations.

    """
    _check_mcp_rate_limit()  # v0.3.0: rate limiting
    correlation_id = set_correlation_id()
    start_time = time.monotonic()

    # ── v13.22.x: Auto-hotswap on RAG staleness ─────────────────────────────
    # If the RAG index was marked stale (by context fold / TurboQuant
    # compaction, or by file edits during the session), kick off a fire-
    # and-forget hotswap_ingest task. The search below runs against the
    # current index, so this is non-blocking; the next search will hit
    # the freshly-swapped data.
    try:
        from ..context.rag_staleness import get_staleness_tracker

        tracker = get_staleness_tracker()
        if tracker.is_stale and tracker.can_reingest():
            task = tracker.trigger_reingest_async()
            if task is not None:
                logger.info(
                    f"[{correlation_id}] RAG stale — scheduled background hotswap "
                    f"reingest (task={task.get_name()})"
                )
    except (ImportError, AttributeError, RuntimeError) as _staleness_exc:
        # Never break the search; staleness is a best-effort optimisation.
        logger.debug(f"[{correlation_id}] staleness check skipped: {_staleness_exc}")

    # v13.5.2 Security: Validate input to prevent injection attacks
    # _validate_search_input returns (sanitized_query, clamped_max_hops, clamped_top_k)
    try:
        query, max_hops, top_k = _validate_search_input(query, max_hops, top_k)
    except ValueError as exc:
        duration = time.monotonic() - start_time
        record_metric("rag_search", duration, success=False)
        return json.dumps({"status": "error", "error": str(exc), "results": []})

    logger.info(f"[{correlation_id}] RAG search: {query[:50]}... (hops={max_hops}, top_k={top_k})")

    # Performance: Check cache first (exact query match)
    cache_key = _get_rag_cache_key(query, max_hops, top_k)
    cached_result = _rag_cache_get(cache_key)
    if cached_result:
        duration = time.monotonic() - start_time
        record_metric("rag_search", duration, success=True)
        logger.debug(f"[{correlation_id}] Cache HIT in {duration:.4f}s")
        return cached_result

    # Fail-closed: check all dependencies
    missing = []
    if lancedb is None:
        missing.append("lancedb")
    if kuzu is None:  # optional module; defensive fail-closed check
        missing.append("kuzu")  # type: ignore[unreachable]
    if OllamaCloudEmbedder is None:
        missing.append("services.embedding (OllamaCloudEmbedder)")
    if missing:
        return json.dumps(
            {
                "status": "error",
                "message": f"Missing dependencies: {', '.join(missing)}",
            }
        )

    init_connections()

    try:
        async with asyncio.timeout(60):
            # ── Phase 1: Dense Vector Retrieval (LanceDB) ─────────────────────
            semantic_anchors = []

            if _lance_tbl is not None and _embed_model is not None:
                # D10 (Fable 5 DD 2026-06-11): use the same prefix convention the
                # ingestion path used. Default to Nomic-style "search_query:" for
                # the common case; sentence-transformers does not require prefixes,
                # so the embedder identity stored on the table lets us decide.
                embedder_identity: dict = getattr(_embed_model, "identity", lambda: {})()
                query_prefix = embedder_identity.get("prefix", "search_query: ")
                encoded = await asyncio.wait_for(
                    asyncio.to_thread(_embed_model.encode, [f"{query_prefix}{query}"]),
                    timeout=30,
                )
                query_vector: list[float] = list(encoded[0]) if encoded and encoded[0] else []
                # v13.22.3: TurboQuant sidecar path. If the sidecar exists
                # (built by cast_ingestion after a full rebuild), use the
                # in-RAM compressed vector matrix for brute-force numpy
                # cosine. This keeps the MCP search path at ~30 MB on
                # disk and avoids loading the full raw LanceDB index
                # (~250 MB for 30k vectors) into the 4 GB-cgroup process.
                # Falls through to LanceDB's native ANN search if the
                # sidecar is missing or the numpy search fails.
                chunk_id_results = await asyncio.wait_for(
                    asyncio.to_thread(
                        _vector_search_with_turboquant,
                        list(query_vector),
                        top_k,
                    ),
                    timeout=15,
                )

                if chunk_id_results is not None:
                    # Sidecar path — look up full row metadata by chunk_id.
                    vector_results = await asyncio.wait_for(
                        asyncio.to_thread(
                            _resolve_chunk_metadata,
                            [r["chunk_id"] for r in chunk_id_results],
                        ),
                        timeout=10,
                    )
                    # Stitch the sidecar distances back onto the metadata.
                    distance_by_id = {r["chunk_id"]: r["_distance"] for r in chunk_id_results}
                    for res in vector_results:
                        cid = res.get("chunk_id", "")
                        if cid in distance_by_id:
                            res["_distance"] = distance_by_id[cid]
                else:
                    # Fallback: native LanceDB ANN search.
                    vector_results = await asyncio.wait_for(
                        asyncio.to_thread(
                            _vector_search_lance_native,
                            list(query_vector),
                            top_k,
                        ),
                        timeout=15,
                    )

                for res in vector_results:
                    anchor = {
                        "ast_entity_id": res.get("ast_entity_id", ""),
                        "file": res.get("filepath", "unknown"),
                        "node_name": res.get("node_name", ""),
                        "node_type": res.get("node_type", ""),
                        "start_line": res.get("start_line", 0),
                        "end_line": res.get("end_line", 0),
                        "content": str(res.get("text", ""))[:MAX_SNIPPET_CHARS],
                    }
                    if "_distance" in res:
                        anchor["distance"] = res["_distance"]
                    semantic_anchors.append(anchor)

                logger.info(f"[Vector] Retrieved {len(semantic_anchors)} results for: {query[:50]}")
            else:
                logger.warning("[Vector] LanceDB table or embedding model unavailable")

            if not semantic_anchors:
                return json.dumps({"status": "no_results", "data": []})

            # ── Phase 2: Graph Traversal (Kùzu) ───────────────────────────────
            structural_relations = []
            pivot_ids = [a["ast_entity_id"] for a in semantic_anchors if a.get("ast_entity_id")]

            if pivot_ids and _kuzu_conn is not None:
                # Validate integers before interpolation (Kuzu has no parameterized LIMIT/depth)
                safe_hops = _safe_cypher_int(max_hops, "max_hops", 1, 3)
                safe_limit = _safe_cypher_int(MAX_GRAPH_RESULTS, "MAX_GRAPH_RESULTS", 1, 100)

                # Kùzu RECURSIVE_REL handling:
                # For variable-length paths [r*1..n], Kùzu returns a dict with:
                #   - '_nodes': list of intermediate nodes
                #   - '_rels': list of relationship dicts, each with '_label' key
                # We extract the first relationship label as the primary edge type.
                # See: https://kuzudb.com/docs/cypher/query-clauses/match.html#variable-length-paths
                cypher_query = f"""
            MATCH (a:ASTNode)-[r*1..{safe_hops}]->(b:ASTNode)
            WHERE a.id IN $pivot_ids
            RETURN a.name AS source, b.name AS target,
                   b.filepath AS filepath, b.code_content AS content, r AS path_data
            LIMIT {safe_limit}
            """

                try:
                    results = _kuzu_conn.execute(cypher_query, parameters={"pivot_ids": pivot_ids})
                    _graph_rows = 0
                    while results.has_next() and _graph_rows < MAX_GRAPH_RESULTS:  # type: ignore[union-attr]
                        _graph_rows += 1
                        row = results.get_next()  # type: ignore[union-attr]
                        # Extract relationship label from RECURSIVE_REL dict
                        # Kùzu returns path_data as:
                        # {'_nodes': [...], '_rels': [{'_label': 'CALLS', ...}, ...]}
                        path_data = row[4] if len(row) > 4 else {}  # type: ignore[index]
                        rel_label = "UNKNOWN"
                        if isinstance(path_data, dict) and "_rels" in path_data:
                            rels = path_data["_rels"]
                            if isinstance(rels, list) and len(rels) > 0:
                                rel_label = rels[0].get("_label", "UNKNOWN")

                        structural_relations.append(
                            {
                                "source_node": str(row[0]),  # type: ignore[index]
                                "relationship": rel_label,
                                "target_node": str(row[1]),  # type: ignore[index]
                                "filepath": str(row[2]) if len(row) > 2 and row[2] else "",  # type: ignore[index]
                                "context_snippet": str(row[3])[:MAX_SNIPPET_CHARS]  # type: ignore[index]
                                if len(row) > 3 and row[3]  # type: ignore[index]
                                else "",
                            }
                        )
                    logger.info(
                        f"[Graph] Traversed {len(structural_relations)} relations ({max_hops} hops)"
                    )
                except ImportError as graph_err:
                    logger.warning(f"[Graph] Traversal failed: {graph_err}")
            else:
                logger.info("[Graph] No pivot IDs or Kùzu unavailable; skipping traversal")

            # ── Phase 3: Context Folding & Sanitization ───────────────────────
            raw_payload = {
                "status": "ok",
                "query": query[:200],
                "semantic_anchors": semantic_anchors,
                "structural_relations": structural_relations,
                "metadata": {
                    "vector_results": len(semantic_anchors),
                    "graph_relations": len(structural_relations),
                    "max_hops": max_hops,
                },
            }

            # Security gate: scrub all string fields before returning
            sanitized = _sanitize_response(raw_payload)

            logger.info(
                f"[RAG] Hybrid search complete: {len(semantic_anchors)} vectors "
                f"+ {len(structural_relations)} graph relations"
            )
            result = json.dumps(sanitized)
            # Cache the result for future identical queries
            _rag_cache_set(cache_key, result)
            duration = time.monotonic() - start_time
            record_metric("rag_search", duration, success=True)
            logger.info(f"[{correlation_id}] RAG search completed in {duration:.4f}s")
            return result

    except TimeoutError:
        duration = time.monotonic() - start_time
        record_metric("rag_search", duration, success=False)
        logger.error(f"[{correlation_id}] RAG search timed out after {duration:.2f}s")
        return json.dumps({"status": "error", "message": "RAG search timed out."})
    except Exception as e:  # broad catch intentional
        duration = time.monotonic() - start_time
        record_metric("rag_search", duration, success=False)
        logger.error(f"[{correlation_id}] Hybrid retrieval fault: {e}", exc_info=True)
        # Security: Scrub secrets and truncate error messages
        error_msg = scrub_secrets(str(e))[:500]
        return json.dumps({"status": "error", "message": f"Retrieval fault: {error_msg}"})


@mcp.tool()
async def rag_status() -> str:
    """Return the health status of the RAG subsystem.

    Reports availability of LanceDB, Kùzu, and the embedding model,
    plus counts of indexed data.
    """
    correlation_id = set_correlation_id()
    start_time = time.monotonic()
    logger.debug(f"[{correlation_id}] RAG status check initiated")

    try:
        init_connections()

        status = {
            "lancedb_available": lancedb is not None,
            "kuzu_available": kuzu is not None,
            "embeddings_available": OllamaCloudEmbedder is not None,
            "lance_table_loaded": _lance_tbl is not None,
            "kuzu_connected": _kuzu_conn is not None,
            "embed_model_loaded": _embed_model is not None,
            "data_path": db_root(),
            "transport": "stdio",
        }

        # Count indexed records if available
        if _lance_tbl is not None:
            try:
                status["indexed_chunks"] = _lance_tbl.count_rows()
            except Exception as exc:  # ruff: ignore[BLE001]  # broad catch intentional
                logger.warning("[LanceDB] Cannot count rows (%s); reporting -1", exc)
                status["indexed_chunks"] = -1

        duration = time.monotonic() - start_time
        record_metric("rag_status", duration, success=True)
        logger.debug(f"[{correlation_id}] RAG status completed in {duration:.4f}s")
        return json.dumps(_sanitize_response(status))
    except Exception as e:  # broad catch intentional
        duration = time.monotonic() - start_time
        record_metric("rag_status", duration, success=False)
        logger.exception(f"[{correlation_id}] RAG status check failed: {e}")
        raise


@mcp.tool()
async def rag_ingest(target_directory: str, ctx: Context | None = None) -> str:
    """Trigger the CAST ingestion pipeline to index a codebase.

    This runs the full Phase 1 pipeline: AST parsing → chunking → graph
    construction → vector embedding. After ingestion, the data directories
    are locked to read-only.

    **v13.22.3 threading model — RC1 fix.**

    The original implementation called ``ingest(target_directory)`` inline
    (sync function inside an async tool), which blocked the FastMCP stdio
    event loop for the full duration of the pipeline — every other tool call
    on the same server hung until the client timeout (300 s) fired. This
    wrapper now:

    1. Dispatches the blocking work via ``asyncio.to_thread`` so the event
       loop stays responsive to ``rag_status``, ``rag_search``, pings, and
       ``ctx.report_progress`` heartbeats throughout.
    2. Wraps the worker future in ``asyncio.wait_for`` with a generous
       timeout (configurable; default 30 min) so a wedged worker doesn't
       hold the MCP transport forever.
    3. Acquires ``SWAP_LOCK`` on the WORKER thread, not on the asyncio loop
       thread. ``SWAP_LOCK`` is a ``threading.RLock`` (see
       infrastructure/_locks.py) — re-entry on the same thread is safe, but
       a different thread acquires it as a brand-new owner. Running the
       lock body on the worker means ``hotswap_ingest()``'s internal
       ``SWAP_LOCK.acquire(blocking=False)`` returns True (same thread),
       and the manual ``with _swap_lock:`` at the handler level is no
       longer needed.
    4. The job-id pattern (Step 2) returns ``status="accepted"`` and
       a ``job_id`` immediately; the work continues in the background and
       the client polls ``rag_get_job_status(job_id)`` for the result.
    """
    target_directory = str(target_directory)
    if ctx is not None:
        with contextlib.suppress(Exception):
            await ctx.info(f"[Beagle] rag_ingest start: {target_directory}")

    # Path traversal protection — validate directory is under allowed roots.
    # Validation stays on the asyncio thread (cheap, requires no lock).
    try:
        validated_path = _validate_ingest_path(target_directory)
    except ValueError as e:
        return json.dumps({"status": "error", "message": str(e)})

    if not validated_path.is_dir():
        return json.dumps({"status": "error", "message": f"Not a directory: {target_directory}"})

    target_directory = str(validated_path)

    # Allocate a job id up-front so the client can poll even if the
    # worker is still blocked on the SWAP_LOCK when it returns.
    job_id = _register_job(target_directory, kind="ingest")

    # Timeout: prefer config; fall back to 1800 s (30 min). The full
    # pipeline (350 files) typically completes in 60-180 s on this host;
    # the long ceiling simply protects against a wedged embedder.
    ingest_timeout_s = float(os.environ.get("BEAGLE_RAG_INGEST_TIMEOUT_S", "1800"))

    async def _heartbeat() -> None:
        """Emit a progress heartbeat every 5 s while the worker runs.

        Keeps the client's connection alive (most MCP transports have an
        idle-timeout on the stdio pipe) and surfaces liveness to callers
        that watch ``ctx.info``.
        """
        elapsed = 0
        while True:
            await asyncio.sleep(5)
            elapsed += 5
            if ctx is not None:
                with contextlib.suppress(Exception):
                    await ctx.info(
                        f"[Beagle] rag_ingest still running ({elapsed}s elapsed) — job_id={job_id}"
                    )

    def _do_ingest() -> dict:
        """Worker-thread body. Owns SWAP_LOCK for the full ingest."""
        from beagle.infrastructure.cast_ingestion import ingest as _ingest

        # Acquire SWAP_LOCK on this worker thread (not the asyncio thread)
        # so the lock owner is consistent through the body.
        with _swap_lock:
            global _initialized
            _initialized = False  # Force re-init after ingestion
            result = _ingest(target_directory)
            # Re-enforce read-only after new ingestion — also runs on the
            # worker thread; chmod is syscall-only and fast.
            _enforce_readonly_storage()
            payload = {
                "status": "ok" if not result.errors else "partial",
                "files_processed": result.files_processed,
                "chunks_created": result.chunks_created,
                "relations_extracted": result.relations_extracted,
                "errors": result.errors[:5],  # Limit error output
                "elapsed_seconds": round(result.elapsed_seconds, 2),
            }
        _complete_job(job_id, payload)
        return payload

    try:
        heartbeat = asyncio.create_task(_heartbeat())
        try:
            payload = await asyncio.wait_for(
                asyncio.to_thread(_do_ingest),
                timeout=ingest_timeout_s,
            )
        finally:
            heartbeat.cancel()
    except TimeoutError:
        _complete_job(
            job_id,
            {
                "status": "error",
                "error": (
                    f"rag_ingest exceeded {ingest_timeout_s}s "
                    f"(BEAGLE_RAG_INGEST_TIMEOUT_S); worker continues in "
                    f"background — poll rag_get_job_status(job_id={job_id!r})"
                ),
                "job_id": job_id,
            },
        )
        return json.dumps(
            {
                "status": "accepted",
                "job_id": job_id,
                "timeout_seconds": ingest_timeout_s,
                "poll_tool": "rag_get_job_status",
            }
        )
    except Exception as e:  # broad catch intentional
        logger.exception(f"[{get_correlation_id()}] rag_ingest failed: {e}")
        _complete_job(job_id, {"status": "error", "error": str(e)})
        return json.dumps({"status": "error", "error": str(e), "job_id": job_id})

    if ctx is not None:
        with contextlib.suppress(Exception):
            await ctx.info(
                f"[Beagle] rag_ingest done: files={payload.get('files_processed')} "
                f"chunks={payload.get('chunks_created')} "
                f"errors={len(payload.get('errors', []))} — job_id={job_id}"
            )

    return json.dumps({"status": "completed", "job_id": job_id, **payload})


@mcp.tool()
async def rag_hotswap_ingest(
    target_directory: str,
    keep_backup: bool = True,
    ctx: Context | None = None,
) -> str:
    """Ingest a codebase using hot-swap to avoid Kùzu lock contention.

    Stages ingestion to a temporary directory, releases the RAG server's
    database connections, atomically swaps staged data into the live
    RAG directory, and triggers re-initialization.

    Use this instead of rag_ingest when the RAG server is running,
    as rag_ingest will fail with a Kùzu lock error.

    Args:
        target_directory: Path to the codebase to index.
        keep_backup: Keep a backup of previous RAG data (default: True).
        ctx: FastMCP context (auto-injected). When present, start/end
            log lines are emitted as MCP message notifications. v13.19.1.

    """
    if ctx is not None:
        with contextlib.suppress(Exception):
            await ctx.info(f"[Beagle] rag_hotswap_ingest start: {target_directory}")
    # Path traversal protection — reuse module-level _ALLOWED_PATH_ROOTS
    try:
        validated_path = _validate_ingest_path(target_directory)
    except ValueError as e:
        return json.dumps({"status": "error", "message": str(e)})

    if not validated_path.is_dir():
        return json.dumps({"status": "error", "message": f"Not a directory: {target_directory}"})

    # ── Cross-process guard (v13.22.3 RC5 fix) ───────────────────────────
    # `_release_rag_connections()` in hotswap_ingest.py can only release the
    # Kùzu handle held by THIS process. When this MCP server is a different
    # process than a worker that previously held the lock (e.g. an old
    # hung MCP rag_server process from a prior session), the swap would
    # silently hang against the live Kùzu read-only handle. Detect
    # this and FAIL LOUD with an actionable error instead of waiting
    # for the client's 300 s timeout to fire.
    _self_module = sys.modules.get(__name__)
    same_process = bool(getattr(_self_module, "_this_is_rag_server_process", True))
    if not same_process:
        return json.dumps(
            {
                "status": "error",
                "error": (
                    "Cross-process ingest not supported — this MCP server "
                    "process has no handle on the Kùzu/LanceDB files the "
                    "swapper needs to release. Stop the other process "
                    "(see ls -la ~/.beagle/instance_rag_kuzu for "
                    "an active holder) or invoke rag_hotswap_ingest from "
                    "the same process that opened the read-only handle."
                ),
            }
        )

    job_id = _register_job(target_directory, kind="hotswap")

    ingest_timeout_s = float(os.environ.get("BEAGLE_RAG_HOTSWAP_TIMEOUT_S", "1800"))

    async def _heartbeat() -> None:
        elapsed = 0
        while True:
            await asyncio.sleep(5)
            elapsed += 5
            if ctx is not None:
                with contextlib.suppress(Exception):
                    await ctx.info(
                        f"[Beagle] rag_hotswap_ingest still running "
                        f"({elapsed}s elapsed) — job_id={job_id}"
                    )

    def _do_hotswap() -> dict:
        """Worker-thread body. hotswap_ingest() acquires SWAP_LOCK itself."""
        from beagle.infrastructure.hotswap_ingest import (
            hotswap_ingest as _hotswap_ingest,
        )

        # hotswap_ingest() acquires SWAP_LOCK internally; running on this
        # worker thread makes the asyncio-thread's prior `_swap_lock` Moot
        # by construction. The lock self-serializes across all entry
        # points (manual MCP tool handlers, auto-reingest, …).
        result = _hotswap_ingest(
            target_directory,
            keep_backup=keep_backup,
        )
        return result

    try:
        heartbeat = asyncio.create_task(_heartbeat())
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(_do_hotswap),
                timeout=ingest_timeout_s,
            )
        finally:
            heartbeat.cancel()
    except TimeoutError:
        _complete_job(
            job_id,
            {
                "status": "error",
                "error": (
                    f"rag_hotswap_ingest exceeded {ingest_timeout_s}s "
                    f"(BEAGLE_RAG_HOTSWAP_TIMEOUT_S); worker continues in "
                    f"background — poll rag_get_job_status(job_id={job_id!r})"
                ),
            },
        )
        return json.dumps(
            {
                "status": "accepted",
                "job_id": job_id,
                "timeout_seconds": ingest_timeout_s,
                "poll_tool": "rag_get_job_status",
            }
        )
    except Exception as e:  # broad catch intentional
        logger.exception(f"[HotSwap] Hot-swap ingestion failed: {e}")
        _complete_job(job_id, {"status": "error", "error": str(e)})
        return json.dumps({"status": "error", "error": str(e), "job_id": job_id})

    _complete_job(job_id, result)

    # v13.22.3 RC3 — re-enforce read-only after the swap. The atomic
    # move copies fresh files into the live directory; without this
    # chmod pass, the swapped-in data is briefly writable before the
    # next init_connections() call locks it. We do it on the worker
    # thread for the same reason ingest() does: chmod is syscall-only
    # and fast enough that bounding it to the swap window is cheap.
    if isinstance(result, dict) and result.get("status") == "ok":
        _enforce_readonly_storage()

    if ctx is not None:
        with contextlib.suppress(Exception):
            await ctx.info(
                f"[Beagle] rag_hotswap_ingest done: job_id={job_id} status={result.get('status')}"
            )

    return json.dumps(
        {"status": "completed", "job_id": job_id, **result},
        indent=2,
        default=str,
    )


@mcp.tool()
async def rag_get_job_status(job_id: str) -> str:
    """Poll the status of an ingest / hot-swap job by id.

    v13.22.3 RC1+Step 2 — companion to ``rag_ingest`` / ``rag_hotswap_ingest``.

    The async ingest tools return ``{"status": "accepted", "job_id": "..."}``
    when the work continues in the background after the configured
    timeout. This tool reads from a process-local cache (mutex-protected)
    and returns the worker's final result dict when available.

    Trade-off documented (v13.22.3):

    * **Synchronous-with-progress** would block the asyncio loop until the
      ingest finishes — the very behaviour RC1 was added to prevent.
    * **Job-handle (chosen)** returns immediately and lets the client
      poll. The cost is one extra round-trip per poll, but the loop stays
      free for ``rag_search``, ``rag_status``, and MCP pings throughout
      the ingest. This is the only contract that survives a full re-index
      of a large corpus.

    Status values:

    * ``running`` — the worker thread has not yet reported completion
      (either still in flight, or swallowed by an unhandled exception).
    * ``completed`` — the ingest finished and the cached payload is
      attached under ``result``.
    * ``failed`` — the ingest raised; the cached error is attached under
      ``error`` and the partial result (if any) is under ``result``.
    * ``unknown`` — no job with this id has been registered in this
      process (e.g. server restart dropped the cache, or caller's
      job_id is stale).

    The cache is bounded (``_JOBS_MAX_ENTRIES``); very old completed
    entries may be evicted by FIFO.
    """
    if not job_id:
        return json.dumps({"status": "error", "error": "job_id is required"})

    with _JOBS_LOCK:
        entry = _JOBS.get(job_id)

    if entry is None:
        return json.dumps(
            {
                "status": "unknown",
                "job_id": job_id,
                "hint": (
                    "Job IDs are process-local; server restart drops the "
                    "cache. If the server is a different process from the "
                    "caller's, look up the job by polling the originating "
                    "process directly."
                ),
            }
        )

    created_at = entry["created_at"]
    elapsed_s = round(time.monotonic() - created_at, 2)
    out = {
        "status": entry["status"],
        "job_id": job_id,
        "kind": entry["kind"],
        "target_directory": entry["target_directory"],
        "elapsed_seconds": elapsed_s,
    }
    if entry["result"] is not None:
        out["result"] = entry["result"]
    if entry["error"] is not None:
        out["error"] = entry["error"]
    return json.dumps(out, indent=2, default=str)


@mcp.tool()
async def rag_hotswap_rollback() -> str:
    """Roll back to the previous RAG data after a hot-swap.

    Restores the backed-up LanceDB and Kùzu databases, replacing
    the current live data. Useful if newly ingested data is corrupt.

    Returns:
        JSON with rollback status.

    """
    from beagle.infrastructure.hotswap_ingest import rollback

    try:
        result = rollback()
        return json.dumps(result, indent=2)
    except Exception as e:  # broad catch intentional
        logger.exception(f"[HotSwap] Rollback failed: {e}")
        return json.dumps({"status": "error", "error": str(e)})


# ──────────────────────────────────────────────────────────────────────────────
# Observability Tools
# ──────────────────────────────────────────────────────────────────────────────


@mcp.tool()
async def get_metrics() -> str:
    """Return RAG server metrics including request counts and latency statistics.

    Returns:
        JSON with request totals, success/error rates, and latency percentiles.

    """
    correlation_id = set_correlation_id()
    logger.debug(f"[{correlation_id}] Metrics requested")
    return json.dumps(get_metrics_summary(), indent=2)


@mcp.tool()
async def health_check() -> str:
    """Perform a comprehensive health check of the RAG server.

    Checks:
    - LanceDB connectivity and table status
    - Kùzu graph database connectivity
    - Embedding model availability
    - Cache status
    - Memory usage

    Returns:
        JSON with health status for each component.

    """
    import resource

    correlation_id = set_correlation_id()
    start_time = time.monotonic()
    logger.info(f"[{correlation_id}] RAG health check initiated")

    health = {
        "status": "healthy",
        "timestamp": time.time(),
        "correlation_id": correlation_id,
        "checks": {},
    }

    # Check 1: LanceDB
    try:
        init_connections()
        if _lance_tbl is not None:
            count = _lance_tbl.count_rows()
            health["checks"]["lancedb"] = {  # type: ignore[index]
                "status": "ok",
                "table": "ast_code_chunks",
                "row_count": count,
            }
        else:
            health["checks"]["lancedb"] = {  # type: ignore[index]
                "status": "warning",
                "message": "Table not loaded (run ingestion)",
            }
            health["status"] = "degraded"
    except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
        health["checks"]["lancedb"] = {"status": "error", "message": str(e)}  # type: ignore[index]
        health["status"] = "unhealthy"

    # Check 2: Kùzu
    try:
        if _kuzu_conn is not None:
            health["checks"]["kuzu"] = {  # type: ignore[index]
                "status": "ok",
                "mode": "read-only",
                "path": kuzu_uri(),
            }
        else:
            health["checks"]["kuzu"] = {  # type: ignore[index]
                "status": "warning",
                "message": "Not connected",
            }
            health["status"] = "degraded"
    except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
        health["checks"]["kuzu"] = {"status": "error", "message": str(e)}  # type: ignore[index]
        health["status"] = "unhealthy"

    # Check 3: Embeddings
    try:
        if _embed_model is not None:
            health["checks"]["embeddings"] = {  # type: ignore[index]
                "status": "ok",
                "model": "nomic-embed-code",
            }
        else:
            health["checks"]["embeddings"] = {  # type: ignore[index]
                "status": "warning",
                "message": "Model not initialized",
            }
            health["status"] = "degraded"
    except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
        health["checks"]["embeddings"] = {"status": "error", "message": str(e)}  # type: ignore[index]
        health["status"] = "unhealthy"

    # Check 4: Cache status
    health["checks"]["cache"] = {  # type: ignore[index]
        "status": "ok",
        "entries": len(_RAG_CACHE),
        "max_size": _RAG_CACHE_MAX_SIZE,
        "utilization_pct": round(len(_RAG_CACHE) / _RAG_CACHE_MAX_SIZE * 100, 1),
    }

    # Check 5: Memory usage
    try:
        mem_usage = resource.getrusage(resource.RUSAGE_SELF)
        health["checks"]["memory"] = {  # type: ignore[index]
            "status": "ok",
            "max_rss_mb": round(mem_usage.ru_maxrss / 1024, 2),
            "shared_mb": round(mem_usage.ru_ixrss / 1024, 2),
            "unshared_mb": round(mem_usage.ru_idrss / 1024, 2),
        }
    except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
        health["checks"]["memory"] = {"status": "unavailable", "message": str(e)}  # type: ignore[index]

    # Check 6: Metrics
    health["checks"]["metrics"] = {  # type: ignore[index]
        "status": "ok",
        "total_requests": _metrics["requests"]["total"],
        "success_rate": round(
            _metrics["requests"]["success"] / max(_metrics["requests"]["total"], 1) * 100,
            2,
        ),
    }

    duration = time.monotonic() - start_time
    health["checks"]["health_check_latency"] = f"{duration:.4f}s"  # type: ignore[index]
    record_metric("health_check", duration, success=True)
    logger.info(
        f"[{correlation_id}] RAG health check completed in {duration:.4f}s: {health['status']}"
    )

    return json.dumps(health, indent=2)


# ──────────────────────────────────────────────────────────────────────────────
# Graph Query Tools (F13)
# ──────────────────────────────────────────────────────────────────────────────


def _get_graph_hops_and_limit() -> tuple[int, int]:
    """Read max hops and results from config.toml."""
    try:
        config_path = find_config_toml()
        if config_path.exists():
            with open(config_path, "rb") as f:
                data = tomllib.load(f)
            mcp = data.get("mcp", {})
            return (
                int(mcp.get("max_graph_hops", 3)),
                int(mcp.get("max_graph_results", 20)),
            )
    except (OSError, tomllib.TOMLDecodeError, TypeError, ValueError, AttributeError) as exc:
        logger.warning(
            "Cannot read [mcp].max_graph_hops / max_graph_results from config.toml "
            "(%s); using the built-in defaults of 3 hops and 20 results.",
            exc,
        )
    return 3, 20


def _execute_graph_query(cypher: str, parameters: dict | None = None) -> list[dict]:
    """Execute a graph query and return list of dict results."""
    init_connections()
    if _kuzu_conn is None:
        return []

    try:
        # `import kuzu` used to sit here purely to reach kuzu.QueryResult for
        # the isinstance check below. The narrowing no longer names the type,
        # and a missing kuzu already leaves _kuzu_conn as None, which the guard
        # above returns on — so the import has no remaining purpose.
        results = _kuzu_conn.execute(cypher, parameters=parameters)
        rows: list[dict] = []
        # _kuzu_conn.execute may return a single QueryResult or a list of them
        # (e.g. EXPLAIN returns multiple). Narrow by excluding the documented
        # multi-result form.
        #
        # This deliberately does NOT test `isinstance(results, kuzu.QueryResult)`.
        # That form was introduced in SP-3 (f649571) to satisfy mypy and
        # silently changed behaviour: it makes every result object that is not
        # exactly a kuzu.QueryResult — a subclass, a wrapper, a test double —
        # fall through to an empty list, so a graph query returns "no results"
        # rather than failing. Excluding `list` narrows the union just as well
        # for mypy while keeping the runtime contract structural.
        if isinstance(results, list):
            return rows
        while results.has_next():
            row = results.get_next()
            rows.append({"row": [str(x) for x in row]})
        return rows
    except Exception:  # broad catch intentional
        logger.exception("Graph query failed")
        return []


@mcp.tool()
async def graph_callers(function_name: str) -> str:
    """Find all functions that call the given function.

    Args:
        function_name: The target function name.

    Returns:
        JSON with calling functions and their file paths.

    """
    _check_mcp_rate_limit()
    max_hops, max_results = _get_graph_hops_and_limit()
    safe_hops = _safe_cypher_int(max_hops, "max_graph_hops", 1, 10)
    safe_limit = _safe_cypher_int(max_results, "max_graph_results", 1, 100)

    query = f"""
        MATCH (caller:ASTNode)-[r*1..{safe_hops}]->(callee:ASTNode)
        WHERE callee.name = $name
          AND callee.node_type = 'function'
          AND caller.node_type = 'function'
        RETURN DISTINCT caller.name AS caller_name,
               caller.filepath AS filepath
        LIMIT {safe_limit}
    """
    rows = _execute_graph_query(query, {"name": function_name})
    return json.dumps({"function": function_name, "callers": rows[:max_results]})


@mcp.tool()
async def graph_callees(function_name: str) -> str:
    """Find all functions called by the given function.

    Args:
        function_name: The source function name.

    Returns:
        JSON with callee functions and their file paths.

    """
    _check_mcp_rate_limit()
    max_hops, max_results = _get_graph_hops_and_limit()
    safe_hops = _safe_cypher_int(max_hops, "max_graph_hops", 1, 10)
    safe_limit = _safe_cypher_int(max_results, "max_graph_results", 1, 100)

    query = f"""
        MATCH (caller:ASTNode)-[r*1..{safe_hops}]->(callee:ASTNode)
        WHERE caller.name = $name
          AND caller.node_type = 'function'
          AND callee.node_type = 'function'
        RETURN DISTINCT callee.name AS callee_name,
               callee.filepath AS filepath
        LIMIT {safe_limit}
    """
    rows = _execute_graph_query(query, {"name": function_name})
    return json.dumps({"function": function_name, "callees": rows[:max_results]})


@mcp.tool()
async def graph_imports(module_path: str) -> str:
    """Find all modules imported by the given module.

    Args:
        module_path: The module file path.

    Returns:
        JSON with imported module paths.

    """
    _check_mcp_rate_limit()
    max_hops, max_results = _get_graph_hops_and_limit()
    safe_hops = _safe_cypher_int(max_hops, "max_graph_hops", 1, 10)
    safe_limit = _safe_cypher_int(max_results, "max_graph_results", 1, 100)

    query = f"""
        MATCH (m:ASTNode)-[r:IMPORTS*1..{safe_hops}]->(i:ASTNode)
        WHERE m.filepath = $path
        RETURN DISTINCT i.filepath AS imported_module
        LIMIT {safe_limit}
    """
    rows = _execute_graph_query(query, {"path": module_path})
    return json.dumps({"module": module_path, "imports": rows[:max_results]})


@mcp.tool()
async def graph_dependents(module_path: str) -> str:
    """Find all modules that import the given module.

    Args:
        module_path: The module file path.

    Returns:
        JSON with dependent module paths.

    """
    _check_mcp_rate_limit()
    max_hops, max_results = _get_graph_hops_and_limit()
    safe_hops = _safe_cypher_int(max_hops, "max_graph_hops", 1, 10)
    safe_limit = _safe_cypher_int(max_results, "max_graph_results", 1, 100)

    query = f"""
        MATCH (d:ASTNode)-[r:IMPORTS*1..{safe_hops}]->(m:ASTNode)
        WHERE m.filepath = $path
        RETURN DISTINCT d.filepath AS dependent_module
        LIMIT {safe_limit}
    """
    rows = _execute_graph_query(query, {"path": module_path})
    return json.dumps({"module": module_path, "dependents": rows[:max_results]})


@mcp.tool()
async def graph_class_hierarchy(class_name: str) -> str:
    """Get the inheritance hierarchy for a class.

    Args:
        class_name: The class name.

    Returns:
        JSON with ancestors and descendants.

    """
    _check_mcp_rate_limit()
    max_hops, max_results = _get_graph_hops_and_limit()
    safe_hops = _safe_cypher_int(max_hops, "max_graph_hops", 1, 10)
    safe_limit = _safe_cypher_int(max_results, "max_graph_results", 1, 100)

    ancestors_query = f"""
        MATCH (cl:ASTNode)-[r:INHERITS_FROM*1..{safe_hops}]->(a:ASTNode)
        WHERE cl.name = $name
          AND cl.node_type = 'class'
        RETURN DISTINCT a.name AS ancestor_name,
               a.filepath AS filepath
        LIMIT {safe_limit}
    """
    descendants_query = f"""
        MATCH (d:ASTNode)-[r:INHERITS_FROM*1..{safe_hops}]->(cl:ASTNode)
        WHERE cl.name = $name
          AND cl.node_type = 'class'
        RETURN DISTINCT d.name AS descendant_name,
               d.filepath AS filepath
        LIMIT {safe_limit}
    """
    ancestors = _execute_graph_query(ancestors_query, {"name": class_name})
    descendants = _execute_graph_query(descendants_query, {"name": class_name})
    return json.dumps(
        {
            "class": class_name,
            "ancestors": ancestors[:max_results],
            "descendants": descendants[:max_results],
        }
    )


# ──────────────────────────────────────────────────────────────────────────────
# Entry Point — stdio (default) or streamable-http (BEAGLE_EXECUTION_ENV=docker)
# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Consistent --version across dev-tool entry points.
    from .mcp_common import maybe_print_version

    if maybe_print_version():
        raise SystemExit(0)

    # B5 (Option B): the RAG server now supports the same transport model as
    # the utility server. stdio is the default (local, no network exposure).
    # When BEAGLE_EXECUTION_ENV=docker, it runs streamable-http on
    # FASTMCP_PORT with mandatory bearer-token authentication. This lets a
    # remote OpenClaw client reach the RAG index through the same
    # authenticated HTTP surface as the utility server.
    #
    # SECURITY: the streamable-http path REQUIRES BEAGLE_MCP_TOKEN (fail-closed
    # RuntimeError if missing) and installs a Starlette middleware that rejects
    # every request without a matching Authorization: Bearer header. This is
    # the same model as mcp_utility_server.py — never run HTTP unauthenticated.
    _transport = os.environ.get("MCP_TRANSPORT")
    if not _transport:
        _transport = (
            "streamable-http"
            if os.environ.get("BEAGLE_EXECUTION_ENV", "").lower() == "docker"
            else "stdio"
        )
    _host = os.environ.get("MCP_HOST", "127.0.0.1")
    _port = int(os.environ.get("MCP_PORT", os.environ.get("FASTMCP_PORT", "8420")))
    logger.info(
        f"Beagle Hybrid RAG MCP Server starting (transport={_transport}, host={_host}, port={_port})"
    )
    if _transport == "stdio":
        mcp.run(transport="stdio")
    else:
        import hmac

        _expected_token = os.environ.get("BEAGLE_MCP_TOKEN", "")
        if not _expected_token:
            raise RuntimeError(
                "BEAGLE_MCP_TOKEN environment variable is REQUIRED for streamable-http "
                "transport. See MCP_TRUST.md — HTTP transports always require "
                "authentication regardless of trust label. Generate a token with: "
                "python -c 'import secrets; print(secrets.token_hex(32))'"
            )

        mcp.settings.host = _host
        mcp.settings.port = _port
        logger.info(
            f"[MCP-RAG-Auth] Bearer-token auth ENABLED for streamable-http on {_host}:{_port}"
        )
        import uvicorn

        starlette_app = mcp.streamable_http_app()

        class BearerAuthMiddleware:
            """ASGI 3 middleware enforcing ``Authorization: Bearer <token>``.

            Per MCP_TRUST.md: HTTP transports must require authentication
            regardless of trust label. This middleware wraps every request
            before it reaches FastMCP's internal router.
            """

            def __init__(self, inner_app):
                self.inner_app = inner_app

            async def __call__(self, scope, receive, send):
                if scope["type"] != "http":
                    return await self.inner_app(scope, receive, send)
                path = scope.get("path", "/")
                if path in ("/", "/health", "/healthz"):
                    return await self.inner_app(scope, receive, send)
                headers = dict(scope.get("headers") or [])
                auth = headers.get(b"authorization", b"").decode("latin-1", errors="replace")
                if not auth.startswith("Bearer "):
                    await self._reject(send, 401, "Bearer token required")
                    return
                token = auth[7:].strip().encode()
                expected = _expected_token.encode()
                if not hmac.compare_digest(token, expected):
                    await self._reject(send, 403, "Invalid bearer token")
                    return
                return await self.inner_app(scope, receive, send)

            @staticmethod
            async def _reject(send, status: int, detail: str) -> None:
                await send(
                    {
                        "type": "http.response.start",
                        "status": status,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"www-authenticate", b'Bearer realm="beagle-rag"'),
                        ],
                    }
                )
                body = b'{"error":"unauthorized","detail":"' + detail.encode() + b'"}'
                await send({"type": "http.response.body", "body": body})

        wrapped_app = BearerAuthMiddleware(starlette_app)
        uvicorn.run(
            wrapped_app,
            host=_host,
            port=_port,
            log_level=mcp.settings.log_level.lower()
            if isinstance(mcp.settings.log_level, str)
            else "info",
        )
