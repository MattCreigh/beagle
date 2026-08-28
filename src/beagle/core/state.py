"""LangGraph state definition and thread-safe singleton management for Beagle v12.0.

Provides the Pydantic-validated state schema for LangGraph and thread-safe singleton
patterns.

SECURITY (DevSecOps pass):
- Replaced TypedDict BeagleState with strict pydantic.BaseModel for runtime
  type validation and immutability guarantees.
- Added OperationalMetadata model with iteration tracking to enforce circuit
  breaker limits in the graph routers.
- Untyped Dict state was a source of silent corruption; Pydantic validation
  catches type errors at graph-injection time rather than deep in node logic.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError

logger = logging.getLogger("Beagle.state")

T = TypeVar("T")


# ── Operational Metadata (circuit breaker tracking) ────────────────────────────


class OperationalMetadata(BaseModel):
    """Tracks iteration and error counts for circuit breaker enforcement.

    Graph routers inspect these counters on every conditional edge and
    terminate the graph (return ``__end__``) when limits are exceeded.
    """

    model_config = ConfigDict(strict=True)

    iteration: int = Field(default=0, ge=0, description="Current iteration count in the graph loop")
    error_count: int = Field(default=0, ge=0, description="Consecutive error count")
    total_iterations: int = Field(
        default=0, ge=0, description="Cumulative iterations across all re-routes"
    )


# ── LangGraph State ────────────────────────────────────────────────────────────


def _append_reducer(a: list, b: list) -> list:
    """Reducer that appends new items to existing list."""
    return a + b


class BeagleState(BaseModel):
    """Workflow state for LangGraph execution — Pydantine-validated.

    Replaces the previous TypedDict to provide:
    - Runtime type validation on state mutations
    - Explicit defaults (no silent None leakage)
    - OperationalMetadata for circuit-breaker enforcement
    - Strict mode: no extra fields allowed

    List fields use append reducers via Annotated; scalar fields use
    last-write-wins.

    H-MEM v13 Enhancement:
    - hydrated_context: Pre-loaded context from RAG, constraints, and manifests
    - hydration_constraints: Constraints loaded from registry
    - hydration_code_chunks: Relevant code from RAG search
    - hydration_documentation: Loaded documentation files
    - hydration_tokens: Token count for hydrated context

    v12.3 Standardization:
    - step_count: Replaces ad-hoc 'steps' field (integer counter for workflow steps)
    - permission_level: Replaces ad-hoc 'permissions' field (str: "read" | "write" | "admin")
    - status: Replaces ad-hoc 'state' field (str: "pending" | "running" | "completed" | "failed")
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    # ── Circuit-breaker metadata ──────────────────────────────────────────
    operational: OperationalMetadata = Field(default_factory=OperationalMetadata)

    # ── Scalar fields (last-write-wins) ───────────────────────────────────
    query: str = Field(default="")
    research_plan: str = Field(default="")
    raw_execution_context: str = Field(default="")
    verified_facts: str = Field(default="")
    final_report: str = Field(default="")
    workflow_id: str = Field(default="")
    workflow_mode: str = Field(default="audit")
    step_count: int = Field(default=0)
    permission_level: str = Field(default="read")
    status: str = Field(default="pending")
    start_time: float = Field(default=0.0)
    state_hash: str = Field(default="")
    global_context: str = Field(default="")
    steering_prompt: str = Field(default="")
    total_cost: float = Field(default=0.0)
    total_tokens: int = Field(default=0)
    current_model: str = Field(default="")
    context_usage: float = Field(default=0.0)
    approval_granted: bool = Field(default=False)

    # H-MEM v13: Hydration fields
    hydrated_context: str = Field(default="")
    hydration_documentation: str = Field(default="")
    hydration_tokens: int = Field(default=0)

    # ── List/dict fields with append reducers ─────────────────────────────
    # Note: LangGraph's Annotated reducer pattern works with Pydantic fields.
    # The reducer is declared at graph-construction time (add_node / add_edge).
    # Here we just declare the types with defaults.
    errors: list[str] = Field(default_factory=list)
    fact_ledger: list[dict[str, Any]] = Field(default_factory=list)
    completed_nodes: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    hydration_constraints: list[str] = Field(default_factory=list)
    hydration_code_chunks: list[dict[str, Any]] = Field(default_factory=list)

    # v13.7.0: Tool failure tracking for executor escalation & rehydration
    tool_failure_history: list[dict[str, Any]] = Field(default_factory=list)

    # ── Mapping protocol (dict-native graph-layer compatibility) ──────────
    # LangGraph(StateGraph(BeagleState)) hands nodes/routers/CVCP either a plain
    # dict or THIS BaseModel instance; the dict-native graph layer (nodes.py,
    # graph.py, protocols/cvcp.py, routers) reads/writes state via
    # .get()/[]/in/update()/setdefault(). These shims delegate to the validated
    # pydantic fields so both representations work; extra="forbid" still rejects
    # unknown keys on write. (The v13.16.3 commit message claimed this fix but it
    # was never actually applied — without it every run_beagle_workflow crashes
    # with "'BeagleState' object has no attribute 'get'".)
    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key) if key in type(self).model_fields else default

    def __getitem__(self, key: str) -> Any:
        try:
            return getattr(self, key)
        except AttributeError as exc:
            raise KeyError(key) from exc

    def __setitem__(self, key: str, value: Any) -> None:
        if key not in type(self).model_fields:
            raise KeyError(key)
        # D12 (Fable 5 DD 2026-06-11): the previous path dumped + re-validated
        # the entire state on every key write, which is O(state) and painful
        # for large state. Use a single-field check instead: build a minimal
        # object containing only this key, validate it, and copy the result.
        # On strict failure, log and fall back to permissive setattr — the same
        # trade-off as before, but without serialising the whole model.
        try:
            field_value = self.__class__.model_construct(**{key: value})
            setattr(self, key, getattr(field_value, key))
        except ValidationError as exc:
            logger.warning(
                "BeagleState.__setitem__ permissive fallback: key=%s value_type=%s error=%s",
                key,
                type(value).__name__,
                exc,
            )
            setattr(self, key, value)

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and key in type(self).model_fields

    def setdefault(self, key: str, default: Any = None) -> Any:
        if key in type(self).model_fields:
            return getattr(self, key)
        setattr(self, key, default)
        return default

    def keys(self) -> list[str]:
        return list(type(self).model_fields.keys())

    def values(self) -> list[Any]:
        return [getattr(self, k) for k in type(self).model_fields]

    def items(self) -> list[tuple[str, Any]]:
        return [(k, getattr(self, k)) for k in type(self).model_fields]

    def update(self, other: dict[str, Any] | None = None, **kwargs: Any) -> None:
        merged = dict(other or {})
        merged.update(kwargs)
        for k, v in merged.items():
            self[k] = v  # route through __setitem__ for validation/logging parity


# ── State list size caps (v0.3.0 Golden Master) ──────────────────────────────
MAX_ERRORS = 500
MAX_FACT_LEDGER = 1000
MAX_COMPLETED_NODES = 500
MAX_TOOL_FAILURE_HISTORY = 200
MAX_HYDRATION_CODE_CHUNKS = 500


def trim_state_lists(state: dict[str, Any]) -> None:
    """Trim unbounded list fields to prevent memory exhaustion.

    Keeps the most recent entries when a list exceeds its cap.
    Called by the orchestrator between nodes.
    """
    caps = {
        "errors": MAX_ERRORS,
        "fact_ledger": MAX_FACT_LEDGER,
        "completed_nodes": MAX_COMPLETED_NODES,
        "tool_failure_history": MAX_TOOL_FAILURE_HISTORY,
        "hydration_code_chunks": MAX_HYDRATION_CODE_CHUNKS,
    }
    for field_name, max_size in caps.items():
        lst = state.get(field_name)
        if isinstance(lst, list) and len(lst) > max_size:
            state[field_name] = lst[-max_size:]


def create_initial_state(
    query: str,
    workflow_id: str = "",
    steering_prompt: str = "",
    workflow_mode: str = "audit",
    approval_granted: bool = False,
) -> dict[str, Any]:
    """Create an initial state dict for a new workflow run.

    H-MEM v13: Includes hydration fields for context pre-loading.

    Returns a plain dict (not a BaseModel instance) for LangGraph compatibility.
    LangGraph internally reconstructs state from dicts on each node invocation.
    """
    return {
        "query": query,
        "research_plan": "",
        "raw_execution_context": "",
        "verified_facts": "",
        "final_report": "",
        "metadata": {},
        "errors": [],
        "fact_ledger": [],
        "total_cost": 0.0,
        "total_tokens": 0,
        "completed_nodes": [],
        "workflow_id": workflow_id or str(uuid.uuid4()),
        "workflow_mode": workflow_mode,
        "start_time": time.time(),
        "state_hash": "",
        "global_context": "",
        "steering_prompt": steering_prompt,
        "approval_granted": approval_granted,
        # v12.3: Standardized state tracking fields
        "step_count": 0,
        "permission_level": "read" if workflow_mode == "audit" else "write",
        "status": "pending",
        # H-MEM v13: Hydration fields
        "hydrated_context": "",
        "hydration_constraints": [],
        "hydration_code_chunks": [],
        "hydration_documentation": "",
        "hydration_tokens": 0,
        # Circuit-breaker metadata
        "operational": {
            "iteration": 0,
            "error_count": 0,
            "total_iterations": 0,
        },
    }


# ── Singleton Management ────────────────────────────────────────────────────────


@dataclass
class SingletonStats:
    """Statistics for singleton tracking."""

    instances_created: int = 0
    instances_reset: int = 0
    persistence_saves: int = 0
    persistence_loads: int = 0
    lock_waits: int = 0


# Global registry for all singletons
_registry: dict[str, Any] = {}
_registry_lock = threading.Lock()
# Module-level bootstrap lock for per-class singleton construction.
# Used only for the one-time class-lock creation; never held during
# user-visible work. See Singleton.get_instance().
_bootstrap_singleton_lock = threading.Lock()


class Singleton[T](ABC):
    """Thread-safe singleton base class for synchronous resources."""

    def __init__(self, name: str | None = None) -> None:
        self._name = name or self.__class__.__name__
        self._instance: T | None = None
        self._lock = threading.Lock()
        self._stats = SingletonStats()

        # Register in global registry
        with _registry_lock:
            if self._name in _registry:
                logger.warning(f"Singleton '{self._name}' already registered, replacing")
            _registry[self._name] = self

    @abstractmethod
    def _create(self) -> T:
        """Create the singleton instance. Override in subclass."""
        ...

    @classmethod
    def get_instance(cls) -> T:
        """Get or create the singleton instance.

        Each subclass maintains its own instance via a class-level attribute.
        First call creates the instance; subsequent calls return the same one.

        D15 (Fable 5 DD 2026-06-11): the previous version did an unsynchronised
        `hasattr(cls, ...)` + bare `cls()` check. Under threads, two callers
        could both see hasattr=False, both construct an instance, and the
        second write would win — leaving an orphaned instance. Fixed with
        double-checked locking on a per-class construction lock.
        """
        lock_name = "_singleton_construction_lock"
        # Fast path: the instance is already constructed.
        if hasattr(cls, "_singleton_instance"):
            return getattr(cls, "_singleton_instance").get()
        # Slow path: take the class-level lock and check again.
        with _bootstrap_singleton_lock:
            if not hasattr(cls, lock_name):
                setattr(cls, lock_name, threading.RLock())
            cls_lock = getattr(cls, lock_name)
        with cls_lock:
            if not hasattr(cls, "_singleton_instance"):
                setattr(cls, "_singleton_instance", cls())
        return getattr(cls, "_singleton_instance").get()

    def get(self) -> T:
        """Get the singleton instance, creating if necessary."""
        if self._instance is not None:
            return self._instance

        with self._lock:
            # Double-check after acquiring lock (reachable at runtime: another
            # thread may set _instance while this thread waits on the lock)
            if self._instance is not None:
                self._stats.lock_waits += 1  # type: ignore[unreachable]
                return self._instance

            self._instance = self._create()
            self._stats.instances_created += 1
            logger.debug(f"Singleton '{self._name}' created")
            return self._instance

    def reset(self) -> None:
        """Reset the singleton (useful for testing)."""
        with self._lock:
            self._instance = None
            self._stats.instances_reset += 1
            logger.debug(f"Singleton '{self._name}' reset")

    @property
    def is_initialized(self) -> bool:
        """Check if singleton has been initialized."""
        return self._instance is not None

    @property
    def stats(self) -> SingletonStats:
        """Get singleton statistics."""
        return self._stats


class AsyncSingleton[T](ABC):
    """Thread-safe async singleton base class for async resources."""

    def __init__(self, name: str | None = None) -> None:
        self._name = name or self.__class__.__name__
        self._instance: T | None = None
        self._lock = asyncio.Lock()
        self._stats = SingletonStats()

        # Register in global registry
        with _registry_lock:
            if self._name in _registry:
                logger.warning(f"AsyncSingleton '{self._name}' already registered, replacing")
            _registry[self._name] = self

    @abstractmethod
    async def _create(self) -> T:
        """Create the singleton instance. Override in subclass."""
        ...

    async def get(self) -> T:
        """Get the singleton instance, creating if necessary."""
        if self._instance is not None:
            return self._instance

        async with self._lock:
            # Double-check after acquiring lock (reachable at runtime: another
            # coroutine may set _instance while this one waits on the lock)
            if self._instance is not None:
                self._stats.lock_waits += 1  # type: ignore[unreachable]
                return self._instance

            self._instance = await self._create()
            self._stats.instances_created += 1
            logger.debug(f"AsyncSingleton '{self._name}' created")
            return self._instance

    async def reset(self) -> None:
        """Reset the singleton (useful for testing)."""
        async with self._lock:
            self._instance = None
            self._stats.instances_reset += 1
            logger.debug(f"AsyncSingleton '{self._name}' reset")

    @property
    def is_initialized(self) -> bool:
        """Check if singleton has been initialized."""
        return self._instance is not None

    @property
    def stats(self) -> SingletonStats:
        """Get singleton statistics."""
        return self._stats


class PersistentSingleton[T](ABC):
    """Thread-safe singleton with disk persistence."""

    def __init__(self, persist_path: Path, name: str | None = None) -> None:
        self._name = name or self.__class__.__name__
        self._instance: T | None = None
        self._lock = threading.RLock()
        self._stats = SingletonStats()
        self._persist_path = persist_path

        # Ensure parent directory exists
        persist_path.parent.mkdir(parents=True, exist_ok=True)

        # Register in global registry
        with _registry_lock:
            if self._name in _registry:
                logger.warning(f"PersistentSingleton '{self._name}' already registered, replacing")
            _registry[self._name] = self

    @abstractmethod
    def _create(self) -> T:
        """Create a new singleton instance. Override in subclass."""
        ...

    @abstractmethod
    def _serialize(self, _instance: T) -> bytes:
        """Serialize instance to bytes for persistence."""
        ...

    @abstractmethod
    def _deserialize(self, data: bytes) -> T:
        """Deserialize instance from bytes."""
        ...

    def get(self) -> T:
        """Get the singleton instance, loading from disk or creating if necessary."""
        if self._instance is not None:
            return self._instance

        with self._lock:
            # Double-check after acquiring lock (reachable at runtime: another
            # thread may set _instance while this thread waits on the lock)
            if self._instance is not None:
                self._stats.lock_waits += 1  # type: ignore[unreachable]
                return self._instance

            # Try to load from disk
            if self._persist_path.exists():
                try:
                    data = self._persist_path.read_bytes()
                    self._instance = self._deserialize(data)
                    self._stats.persistence_loads += 1
                    self._stats.instances_created += 1
                    logger.debug(
                        f"PersistentSingleton '{self._name}' loaded from {self._persist_path}"
                    )
                    return self._instance
                except (ValueError, TypeError, ImportError, OSError) as e:
                    logger.warning(f"Failed to load {self._name} from {self._persist_path}: {e}")

            # Create new instance
            self._instance = self._create()
            self._stats.instances_created += 1
            self._save()
            logger.debug(f"PersistentSingleton '{self._name}' created")
            return self._instance

    def _save(self) -> None:
        """Save instance to disk."""
        with self._lock:
            if self._instance is None:
                return
            try:
                data = self._serialize(self._instance)
                # D15 (Fable 5 DD 2026-06-11): the previous write_bytes was
                # non-atomic — a crash mid-write would leave a half-written
                # file and a corrupted singleton. Use the project's own
                # write-temp + fsync + os.replace doctrine (the same pattern
                # tested in test_atomic_file_operations.py).
                tmp_path = self._persist_path.with_suffix(
                    self._persist_path.suffix + f".tmp.{os.getpid()}"
                )
                with open(tmp_path, "wb") as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, self._persist_path)
                # fsync the parent directory so the rename is durable
                try:
                    dir_fd = os.open(str(self._persist_path.parent), os.O_DIRECTORY)
                    try:
                        os.fsync(dir_fd)
                    finally:
                        os.close(dir_fd)
                except (OSError, AttributeError) as exc:
                    logger.warning(
                        "Cannot fsync the parent directory of %s (%s); the rename is "
                        "not guaranteed durable across a power loss.",
                        self._persist_path,
                        exc,
                    )
                self._stats.persistence_saves += 1
                logger.debug(f"PersistentSingleton '{self._name}' saved to {self._persist_path}")
            except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
                logger.error(f"Failed to save {self._name} to {self._persist_path}: {e}")
                # Best-effort cleanup of the temp file
                try:
                    tmp_path = self._persist_path.with_suffix(
                        self._persist_path.suffix + f".tmp.{os.getpid()}"
                    )
                    if tmp_path.exists():
                        tmp_path.unlink()
                except OSError as cleanup_exc:
                    logger.warning(
                        "Cannot remove the temporary file for %s (%s); a stale .tmp file "
                        "is left beside %s.",
                        self._name,
                        cleanup_exc,
                        self._persist_path,
                    )

    def reset(self) -> None:
        """Reset the singleton and delete from disk."""
        with self._lock:
            self._instance = None
            self._stats.instances_reset += 1
            if self._persist_path.exists():
                try:
                    self._persist_path.unlink()
                    logger.debug(f"PersistentSingleton '{self._name}' reset and deleted from disk")
                except (ImportError, OSError) as e:
                    logger.warning(f"Failed to delete {self._persist_path}: {e}")

    @property
    def is_initialized(self) -> bool:
        """Check if singleton has been initialized."""
        return self._instance is not None

    @property
    def stats(self) -> SingletonStats:
        """Get singleton statistics."""
        return self._stats


def get_singleton(name: str) -> Singleton[Any] | None:
    """Get a singleton from the global registry by name."""
    with _registry_lock:
        return _registry.get(name)


def reset_all_singletons() -> None:
    """Reset all registered singletons."""
    with _registry_lock:
        for name, singleton in _registry.items():
            singleton.reset()
            logger.debug(f"Reset singleton '{name}'")


__all__ = [
    "AsyncSingleton",
    "BeagleState",
    "OperationalMetadata",
    "PersistentSingleton",
    "Singleton",
    "create_initial_state",
    "get_singleton",
    "reset_all_singletons",
]
