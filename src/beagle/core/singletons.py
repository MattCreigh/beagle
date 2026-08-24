"""Singleton managers for global mutable state in Beagle.

Replaces bare global variables (_active_processes, _agent_call_counter,
_orchestrator_channel) with thread-safe singleton classes that provide
proper encapsulation, locking, and cleanup semantics.

This module eliminates the "global mutable state" anti-pattern by centralizing
all process tracking, agent call counting, and orchestrator communication into
well-defined singleton instances with explicit locking contracts.
"""

from __future__ import annotations

import asyncio
import logging
import threading

logger = logging.getLogger("Beagle.singletons")


# ═══════════════════════════════════════════════════════════════════════════════
# ProcessRegistry — Thread-safe subprocess tracking
# ═══════════════════════════════════════════════════════════════════════════════


class ProcessRegistry:
    """Thread-safe registry for active subprocess processes.

    Replaces the bare global `_active_processes: set` with a proper singleton
    that provides register/unregister/cleanup/count operations with locking.

    Usage:
        registry = ProcessRegistry.instance()
        await registry.register(proc)
        count = registry.active_count()
        await registry.unregister(proc)
        registry.cleanup_all()
    """

    _instance: ProcessRegistry | None = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self._processes: set[asyncio.subprocess.Process] = set()
        self._async_lock: asyncio.Lock | None = None
        self._sync_lock = threading.Lock()

    @classmethod
    def instance(cls) -> ProcessRegistry:
        """Get or create the singleton ProcessRegistry instance."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def _get_async_lock(self) -> asyncio.Lock:
        """Lazily create the async lock (must be created in an async context)."""
        if self._async_lock is None:
            with self._sync_lock:
                if self._async_lock is None:
                    self._async_lock = asyncio.Lock()
        return self._async_lock

    async def register(self, proc: asyncio.subprocess.Process) -> None:
        """Register an active subprocess.

        Args:
            proc: The subprocess to track.

        """
        async with self._get_async_lock():
            self._processes.add(proc)
            logger.debug(
                f"[ProcessRegistry] Registered process {proc.pid} ({len(self._processes)} active)"
            )

    async def unregister(self, proc: asyncio.subprocess.Process) -> None:
        """Unregister a completed subprocess.

        Args:
            proc: The subprocess to remove from tracking.

        """
        async with self._get_async_lock():
            self._processes.discard(proc)
            logger.debug(
                f"[ProcessRegistry] Unregistered process {proc.pid} ({len(self._processes)} active)"
            )

    @property
    def active_count(self) -> int:
        """Return the number of currently active processes."""
        with self._sync_lock:
            return len(self._processes)

    def cleanup_all(self) -> int:
        """Terminate all active subprocesses and clear the registry.

        v0.3.0: Sends SIGKILL as fallback if SIGTERM is ignored.

        Returns:
            Number of processes terminated.

        """
        import contextlib
        import os
        import signal

        with self._sync_lock:
            count = 0
            for proc in list(self._processes):
                if proc.returncode is None:
                    with contextlib.suppress(OSError, RuntimeError):
                        proc.terminate()
                    # Give process a moment to exit, then force kill
                    try:
                        if proc.pid and proc.returncode is None:
                            os.waitpid(proc.pid, os.WNOHANG)
                    except (ChildProcessError, OSError) as exc:
                        logger.warning(
                            "Cannot reap child process %s (%s); proceeding to the SIGKILL "
                            "escalation below.",
                            proc.pid,
                            exc,
                        )
                    # If still alive, force kill
                    if proc.returncode is None:
                        with contextlib.suppress(OSError, ProcessLookupError):
                            os.kill(proc.pid, signal.SIGKILL)
                    count += 1
            self._processes.clear()
            if count > 0:
                logger.info(f"[ProcessRegistry] Cleaned up {count} processes")
            return count

    @classmethod
    def reset(cls) -> None:
        """Reset the singleton instance (for testing only)."""
        with cls._lock:
            if cls._instance is not None:
                cls._instance.cleanup_all()
            cls._instance = None


# ═══════════════════════════════════════════════════════════════════════════════
# AgentCallTracker — Thread-safe agent call counting
# ═══════════════════════════════════════════════════════════════════════════════


class AgentCallTracker:
    """Thread-safe tracker for agent call counts per workflow.

    Replaces the bare global `_agent_call_counter: dict` with a proper singleton
    that provides increment/get/reset/cleanup operations with locking.

    Usage:
        tracker = AgentCallTracker.instance()
        count = await tracker.increment("workflow-123")
        total = tracker.total_calls
        tracker.reset("workflow-123")
    """

    _instance: AgentCallTracker | None = None
    _lock = threading.Lock()

    _TTL: float = 3600.0  # 1 hour

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}
        self._timestamps: dict[str, float] = {}
        self._async_lock: asyncio.Lock | None = None
        self._sync_lock = threading.Lock()

    @classmethod
    def instance(cls) -> AgentCallTracker:
        """Get or create the singleton AgentCallTracker instance."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def _get_async_lock(self) -> asyncio.Lock:
        """Lazily create the async lock (must be created in an async context)."""
        if self._async_lock is None:
            with self._sync_lock:
                if self._async_lock is None:
                    self._async_lock = asyncio.Lock()
        return self._async_lock

    async def increment(self, workflow_id: str) -> int:
        """Atomically increment the call count for a workflow.

        Includes TTL sweep: entries older than 1 hour are evicted to
        prevent memory leaks in long-running deployments.

        Args:
            workflow_id: The workflow identifier.

        Returns:
            The new count after incrementing.

        """
        import time

        async with self._get_async_lock():
            now = time.time()
            stale = [k for k, ts in self._timestamps.items() if now - ts > self._TTL]
            for k in stale:
                self._counts.pop(k, None)
                self._timestamps.pop(k, None)
            current = self._counts.get(workflow_id, 0) + 1
            self._counts[workflow_id] = current
            self._timestamps[workflow_id] = now
            return current

    async def get_count(self, workflow_id: str) -> int:
        """Get the current call count for a workflow.

        Args:
            workflow_id: The workflow identifier.

        Returns:
            The current call count (0 if not found).

        """
        async with self._get_async_lock():
            return self._counts.get(workflow_id, 0)

    async def reset(self, workflow_id: str) -> None:
        """Reset the call count for a specific workflow.

        Args:
            workflow_id: The workflow identifier to reset.

        """
        async with self._get_async_lock():
            self._counts[workflow_id] = 0

    async def cleanup(self, workflow_id: str) -> None:
        """Remove a workflow's call count to prevent memory leaks.

        Args:
            workflow_id: The workflow identifier to remove.

        """
        async with self._get_async_lock():
            self._counts.pop(workflow_id, None)
            self._timestamps.pop(workflow_id, None)

    @property
    def total_calls(self) -> int:
        """Return the total number of calls across all workflows."""
        with self._sync_lock:
            return sum(self._counts.values())

    @property
    def active_workflows(self) -> int:
        """Return the number of active workflows with calls."""
        with self._sync_lock:
            return len(self._counts)

    @classmethod
    def reset_instance(cls) -> None:
        """Reset the singleton instance (for testing only)."""
        with cls._lock:
            cls._instance = None


# ═══════════════════════════════════════════════════════════════════════════════
# OrchestratorChannelManager — Thread-safe orchestrator communication
# ═══════════════════════════════════════════════════════════════════════════════


class OrchestratorChannelManager:
    """Thread-safe manager for the orchestrator communication channel.

    Replaces the bare global `_orchestrator_channel: asyncio.Queue` with a proper
    singleton that provides set/get/send operations with proper locking.

    Usage:
        channel_mgr = OrchestratorChannelManager.instance()
        await channel_mgr.set_channel(my_queue)
        success = await channel_mgr.send_message({"type": "result", "data": ...})
    """

    _instance: OrchestratorChannelManager | None = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self._channel: asyncio.Queue | None = None
        self._async_lock: asyncio.Lock | None = None
        self._sync_lock = threading.Lock()
        self._message_count: int = 0

    @classmethod
    def instance(cls) -> OrchestratorChannelManager:
        """Get or create the singleton OrchestratorChannelManager instance."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def _get_async_lock(self) -> asyncio.Lock:
        """Lazily create the async lock."""
        if self._async_lock is None:
            with self._sync_lock:
                if self._async_lock is None:
                    self._async_lock = asyncio.Lock()
        return self._async_lock

    async def set_channel(self, channel: asyncio.Queue) -> None:
        """Set the global orchestrator communication channel.

        Args:
            channel: The asyncio Queue to use for orchestrator communication.

        """
        async with self._get_async_lock():
            self._channel = channel
            self._message_count = 0
            logger.info("[OrchestratorChannel] Channel set")

    async def get_channel(self) -> asyncio.Queue | None:
        """Get the current orchestrator channel.

        Returns:
            The current channel, or None if not set.

        """
        async with self._get_async_lock():
            return self._channel

    async def send_message(self, message: dict) -> bool:
        """Send a message to the orchestrator via the channel.

        Args:
            message: Dict containing agent results, status, etc.

        Returns:
            True if message was sent, False if no channel available.

        """
        async with self._get_async_lock():
            if self._channel is None:
                logger.warning("[OrchestratorChannel] No channel available")
                return False

        try:
            await asyncio.wait_for(self._channel.put(message), timeout=2.0)
            self._message_count += 1
            logger.info(f"[OrchestratorChannel] ✅ Message sent: {message.get('type', 'unknown')}")
            return True
        except TimeoutError:
            logger.warning("[OrchestratorChannel] Failed to send — channel full")
            return False
        except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
            logger.error(f"[OrchestratorChannel] Error sending: {e}")
            return False

    @property
    def message_count(self) -> int:
        """Return the total number of messages sent."""
        return self._message_count

    @property
    def has_channel(self) -> bool:
        """Check if a channel is set."""
        return self._channel is not None

    @classmethod
    def reset_instance(cls) -> None:
        """Reset the singleton instance (for testing only)."""
        with cls._lock:
            cls._instance = None


__all__ = [
    "AgentCallTracker",
    "OrchestratorChannelManager",
    "ProcessRegistry",
]
