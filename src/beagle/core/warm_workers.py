"""Warm Worker Pool — Pre-spawned idle goose subprocesses for faster execution.

Maintains 2-3 idle goose subprocesses with pre-loaded static context.
Reuses workers instead of spawning new ones for each workflow node,
reducing cold-start latency significantly on the i7-6700T.

Config via config.toml [hardware]:
  warm_workers_enabled = true
  warm_worker_count = 2

Usage:
    from beagle.core.warm_workers import WarmWorkerPool

    pool = WarmWorkerPool(count=2)
    await pool.initialize()
    worker = await pool.acquire()
    # ... use worker ...
    await pool.release(worker)
    await pool.shutdown()
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from dataclasses import dataclass, field

from beagle.runtime.goose_cli import GooseCliRuntime

logger = logging.getLogger("Beagle.warm_workers")


@dataclass
class WarmWorker:
    """A pre-spawned goose subprocess ready for task assignment."""

    worker_id: int
    process: asyncio.subprocess.Process | None = None
    created_at: float = field(default_factory=time.monotonic)
    in_use: bool = False
    task_count: int = 0

    @property
    def age_seconds(self) -> float:
        return time.monotonic() - self.created_at

    @property
    def is_alive(self) -> bool:
        return self.process is not None and self.process.returncode is None


class WarmWorkerPool:
    """Pool of pre-warmed goose subprocesses for reduced cold-start latency.

    Workers are spawned on initialization and kept idle until needed.
    When a node needs execution, it acquires a worker from the pool
    instead of spawning a fresh subprocess.
    """

    def __init__(self, count: int = 2, max_age_seconds: float = 3600.0) -> None:
        """Initialize the warm worker pool.

        Args:
            count: Number of warm workers to maintain (default: 2).
            max_age_seconds: Maximum worker age before recycling (default: 3600).

        """
        self.count = count
        self.max_age_seconds = max_age_seconds
        self._workers: list[WarmWorker] = []
        self._available: asyncio.Queue[WarmWorker] = asyncio.Queue()
        self._lock = asyncio.Lock()
        self._initialized = False

    async def initialize(self) -> None:
        """Pre-spawn warm workers."""
        if self._initialized:
            return

        logger.info(f"[WarmWorkers] Spawning {self.count} warm workers...")
        for i in range(self.count):
            try:
                worker = await self._spawn_worker(i)
                self._workers.append(worker)
                await self._available.put(worker)
            except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
                logger.warning(f"[WarmWorkers] Failed to spawn worker {i}: {e}")

        self._initialized = True
        logger.info(
            f"[WarmWorkers] Pool initialized: "
            f"{sum(1 for w in self._workers if w.is_alive)}/{self.count} workers alive"
        )

    async def _spawn_worker(self, worker_id: int) -> WarmWorker:
        """Spawn a single warm goose subprocess.

        The subprocess starts with a lightweight initialization prompt that
        pre-loads the system directive and static context.
        """
        # v1.1.1 (B1c): route binary resolution through the sub-agent
        # runtime interface instead of the direct resolver.
        goose_bin = GooseCliRuntime().resolved_binary()
        goose_provider = os.environ.get("GOOSE_PROVIDER", "ollama_cloud")
        goose_model = os.environ.get("GOOSE_MODEL", "minimax-m3:cloud")

        cmd = [
            goose_bin,
            "run",
            "--provider",
            goose_provider,
            "--model",
            goose_model,
            "--with-builtin",
            "developer",
            "-i",
            "-",
            "-q",
        ]

        env = os.environ.copy()
        env["BEAGLE_WARM_WORKER"] = "1"

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        worker = WarmWorker(worker_id=worker_id, process=process)
        logger.debug(f"[WarmWorkers] Worker {worker_id} spawned (pid={process.pid})")
        return worker

    async def acquire(self, timeout: float = 30.0) -> WarmWorker | None:
        """Acquire a warm worker from the pool.

        Args:
            timeout: Max seconds to wait for an available worker.

        Returns:
            A WarmWorker instance, or None if pool is empty/exhausted.

        """
        try:
            worker = await asyncio.wait_for(self._available.get(), timeout=timeout)
            # Check if worker is still alive
            if worker.is_alive:
                worker.in_use = True
                worker.task_count += 1
                logger.debug(
                    f"[WarmWorkers] Acquired worker {worker.worker_id} "
                    f"(tasks={worker.task_count}, age={worker.age_seconds:.0f}s)"
                )
                return worker
            else:
                # Worker died — try to recycle
                logger.debug(f"[WarmWorkers] Worker {worker.worker_id} is dead, recycling...")
                await self._recycle_worker(worker)
                # Try again with the recycled worker
                recycled = await asyncio.wait_for(self._available.get(), timeout=timeout)
                if recycled.is_alive:
                    recycled.in_use = True
                    recycled.task_count += 1
                    return recycled
                return None
        except TimeoutError:
            logger.warning("[WarmWorkers] No workers available within timeout")
            return None

    async def release(self, worker: WarmWorker) -> None:
        """Return a worker to the pool after use.

        If the worker exceeds max age, it is recycled instead.
        """
        worker.in_use = False

        if worker.age_seconds > self.max_age_seconds or not worker.is_alive:
            await self._recycle_worker(worker)
        else:
            await self._available.put(worker)
            logger.debug(f"[WarmWorkers] Released worker {worker.worker_id}")

    async def _recycle_worker(self, worker: WarmWorker) -> None:
        """Replace a stale or dead worker with a fresh one."""
        if worker.process and worker.is_alive:
            try:
                worker.process.terminate()
                await asyncio.wait_for(worker.process.wait(), timeout=5.0)
            except (TimeoutError, OSError, ProcessLookupError):
                # Process may already be gone; force-kill to prevent zombies
                with contextlib.suppress(OSError, RuntimeError):
                    worker.process.kill()

        new_worker = await self._spawn_worker(worker.worker_id)
        # Replace in _workers list
        for i, w in enumerate(self._workers):
            if w.worker_id == worker.worker_id:
                self._workers[i] = new_worker
                break
        await self._available.put(new_worker)
        logger.debug(f"[WarmWorkers] Recycled worker {worker.worker_id}")

    async def shutdown(self) -> int:
        """Terminate all warm workers and clean up.

        Returns:
            Number of workers terminated.

        """
        terminated = 0
        for worker in self._workers:
            if worker.process and worker.is_alive:
                try:
                    worker.process.terminate()
                    await asyncio.wait_for(worker.process.wait(), timeout=5.0)
                    terminated += 1
                except (TimeoutError, OSError, ProcessLookupError):
                    try:
                        worker.process.kill()
                        terminated += 1
                    except (OSError, ProcessLookupError) as exc:
                        logger.warning(
                            "Cannot kill warm worker pid=%s (%s); it may survive shutdown "
                            "as an orphan.",
                            getattr(worker.process, "pid", "<unknown>"),
                            exc,
                        )

        self._workers.clear()
        # Drain the queue
        while not self._available.empty():
            try:
                self._available.get_nowait()
            except asyncio.QueueEmpty:
                break

        self._initialized = False
        logger.info(f"[WarmWorkers] Pool shutdown: {terminated} workers terminated")
        return terminated

    @property
    def active_count(self) -> int:
        """Number of currently alive workers."""
        return sum(1 for w in self._workers if w.is_alive)

    @property
    def available_count(self) -> int:
        """Number of workers available in the queue."""
        return self._available.qsize()


def is_warm_workers_enabled() -> bool:
    """Check if warm workers are enabled via config.toml."""
    try:
        from beagle.config.config import get_config

        config = get_config()
        # Check hardware config section
        return getattr(config, "warm_workers_enabled", True)
    except Exception:  # ruff: ignore[BLE001]  # broad catch intentional
        return False


# Module-level singleton
_pool: WarmWorkerPool | None = None


_DEFAULT_WARM_WORKER_COUNT = 2


def _read_warm_worker_count() -> int:
    """Read ``[hardware].warm_worker_count`` from the resolved config file.

    Uses the resolved config path (``loader.get_config_path``) rather than
    stringifying ``workspace_root``, which may be empty when ``WORKSPACE_ROOT``
    is unset.

    Returns:
        The configured worker count, or the default when the key is absent.

    Raises:
        OSError: The config file cannot be read.
        tomllib.TOMLDecodeError: The config file is not valid TOML.
        ImportError: The config loader cannot be imported.
        TypeError: A config section holds a non-mapping value.
        ValueError: The configured value is not an integer.

    """
    import tomllib

    from beagle.config.loader import get_config_path

    with open(get_config_path(), "rb") as fh:
        data = tomllib.load(fh)
    return int(data.get("hardware", {}).get("warm_worker_count", _DEFAULT_WARM_WORKER_COUNT))


async def get_warm_worker_pool() -> WarmWorkerPool:
    """Get or create the singleton warm worker pool.

    The config read runs on a worker thread; a blocking read on the event loop
    would stall every other coroutine for the duration of the file I/O.

    Returns:
        The process-wide warm worker pool.

    """
    global _pool
    if _pool is None:
        import tomllib

        try:
            count = await asyncio.to_thread(_read_warm_worker_count)
        except (OSError, tomllib.TOMLDecodeError, ImportError, TypeError, ValueError) as exc:
            count = _DEFAULT_WARM_WORKER_COUNT
            logger.warning(
                "[WarmWorkers] Cannot read [hardware].warm_worker_count (%s: %s); "
                "starting the pool with the default of %d workers.",
                type(exc).__name__,
                exc,
                count,
            )

        _pool = WarmWorkerPool(count=count)
        await _pool.initialize()
    return _pool


async def shutdown_warm_worker_pool() -> int:
    """Shutdown the singleton warm worker pool."""
    global _pool
    if _pool is not None:
        result = await _pool.shutdown()
        _pool = None
        return result
    return 0


__all__ = [
    "WarmWorker",
    "WarmWorkerPool",
    "get_warm_worker_pool",
    "is_warm_workers_enabled",
    "shutdown_warm_worker_pool",
]
