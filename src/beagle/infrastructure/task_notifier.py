"""Task Notification System - Event-driven callbacks for OpenClaw tasks.

Provides real-time notifications when tasks complete instead of polling.
Uses Orpheus IPC ring buffers for inter-process communication.

Architecture:
┌─────────────┐     ┌──────────────────┐     ┌─────────────────┐
│ OpenClaw    │────▶│ Orpheus Ring     │────▶│ Notification    │
│ (Skylon)    │     │ (Task Events)    │     │ Subscribers     │
└─────────────┘     └──────────────────┘     └─────────────────┘

Event Types:
- TASK_STARTED: Task began execution
- TASK_PROGRESS: Progress update (percentage, checkpoint)
- TASK_COMPLETED: Task finished successfully
- TASK_FAILED: Task failed with error
- TASK_CANCELLED: Task was cancelled
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import uuid
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

logger = logging.getLogger("Beagle.TaskNotifier")

# <invariant>
# Every fire-and-forget task spawned by this module keeps a strong reference in
# _BACKGROUND_TASKS until it finishes. The event loop only holds a weak
# reference to a running task, so a task with no other reference can be garbage
# collected mid-flight and the callback it carries never runs. This set is the
# strong reference; the done-callback removes the entry so the set cannot grow
# without bound.
# </invariant>
_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()


def _spawn_background(
    coro: Any, *, loop: asyncio.AbstractEventLoop | None = None
) -> asyncio.Task[Any]:
    """Schedule a coroutine as a tracked fire-and-forget task.

    Args:
        coro: The coroutine to schedule.
        loop: Event loop to schedule on. When None, the running loop is used.

    Returns:
        The scheduled task. The caller does not have to keep the reference;
        the module holds one until the task completes.

    """
    task = loop.create_task(coro) if loop is not None else asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return task


class TaskEventType(StrEnum):
    """Event types for task notifications."""

    TASK_STARTED = "task_started"
    TASK_PROGRESS = "task_progress"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    TASK_CANCELLED = "task_cancelled"
    TASK_HEARTBEAT = "task_heartbeat"


class TaskState(StrEnum):
    """Task states for matching."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class TaskEvent:
    """A task notification event."""

    event_id: str
    task_id: str
    event_type: TaskEventType
    timestamp: datetime
    data: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        """Serialize to JSON."""
        return json.dumps(
            {
                "event_id": self.event_id,
                "task_id": self.task_id,
                "event_type": self.event_type.value,
                "timestamp": self.timestamp.isoformat(),
                "data": self.data,
            }
        )

    @classmethod
    def from_json(cls, json_str: str) -> TaskEvent:
        """Deserialize from JSON."""
        data = json.loads(json_str)
        return cls(
            event_id=data["event_id"],
            task_id=data["task_id"],
            event_type=TaskEventType(data["event_type"]),
            timestamp=datetime.fromisoformat(data["timestamp"]),
            data=data.get("data", {}),
        )


@dataclass
class Subscription:
    """A subscription to task events."""

    subscription_id: str
    task_id: str | None  # None = all tasks
    event_types: list[TaskEventType]
    callback: Callable[[TaskEvent], None]
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    active: bool = True


class TaskNotifier:
    """
    Event-driven task notification system.

    Replaces polling with callback-based notifications:

    Usage:
        notifier = TaskNotifier()

        # Subscribe to specific task completion
        async def on_complete(event):
            logger.info(f"Task {event.task_id} completed: {event.data}")

        notifier.subscribe(
            task_id="abc123",
            event_types=[TaskEventType.TASK_COMPLETED, TaskEventType.TASK_FAILED],
            callback=on_complete,
        )

        # Subscribe to all task events
        notifier.subscribe_all([TaskEventType.TASK_COMPLETED], on_complete)

        # Publish an event (called by OpenClaw/Skylon)
        notifier.publish(TaskEvent(
            event_id=str(uuid.uuid4()),
            task_id="abc123",
            event_type=TaskEventType.TASK_COMPLETED,
            timestamp=datetime.now(timezone.utc),
            data={"result": "success"},
        ))
    """

    def __init__(
        self,
        ring_path: str | None = None,
        max_history: int = 1000,
    ):
        """Initialize the notifier.

        Args:
            ring_path: Path to Orpheus ring buffer (optional)
            max_history: Maximum events to keep in history

        """
        self.ring_path = ring_path or os.environ.get("ORPHEUS_RING_DIR", "/run/orpheus_ring")
        self.max_history = max_history

        # Subscriptions indexed by task_id and event_type
        self._subscriptions: dict[str, list[Subscription]] = defaultdict(list)
        self._global_subscriptions: list[Subscription] = []

        # Event history for late subscribers
        self._event_history: dict[str, list[TaskEvent]] = defaultdict(list)

        # Async queue for event processing
        self._event_queue: asyncio.Queue[TaskEvent] = asyncio.Queue()
        self._processing: bool = False
        self._processor_task: asyncio.Task | None = None

    def subscribe(
        self,
        task_id: str,
        event_types: list[TaskEventType],
        callback: Callable[[TaskEvent], None],
    ) -> str:
        """Subscribe to events for a specific task.

        Args:
            task_id: Task ID to subscribe to
            event_types: Event types to receive
            callback: Async or sync callback function

        Returns:
            Subscription ID for unsubscribing

        """
        sub_id = str(uuid.uuid4())
        subscription = Subscription(
            subscription_id=sub_id,
            task_id=task_id,
            event_types=event_types,
            callback=callback,
        )
        self._subscriptions[task_id].append(subscription)

        logger.info(
            f"[Notifier] Subscribed {sub_id} to task {task_id} for {[e.value for e in event_types]}"
        )

        # Replay recent events for late subscribers
        for event in self._event_history.get(task_id, [])[-10:]:
            if event.event_type in event_types:
                try:
                    callback(event)
                except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
                    logger.warning(f"[Notifier] Callback error on replay: {e}")

        return sub_id

    def subscribe_all(
        self,
        event_types: list[TaskEventType],
        callback: Callable[[TaskEvent], None],
    ) -> str:
        """Subscribe to events for ALL tasks.

        Args:
            event_types: Event types to receive
            callback: Async or sync callback function

        Returns:
            Subscription ID for unsubscribing

        """
        sub_id = str(uuid.uuid4())
        subscription = Subscription(
            subscription_id=sub_id,
            task_id=None,  # All tasks
            event_types=event_types,
            callback=callback,
        )
        self._global_subscriptions.append(subscription)

        logger.info(f"[Notifier] Global subscription {sub_id} for {[e.value for e in event_types]}")
        return sub_id

    def unsubscribe(self, subscription_id: str) -> bool:
        """Remove a subscription.

        Args:
            subscription_id: ID returned from subscribe()

        Returns:
            True if subscription was found and removed

        """
        # Check task-specific subscriptions
        for task_id, subs in list(self._subscriptions.items()):
            for i, sub in enumerate(subs):
                if sub.subscription_id == subscription_id:
                    sub.active = False
                    subs.pop(i)
                    logger.info(f"[Notifier] Unsubscribed {subscription_id} from task {task_id}")
                    return True

        # Check global subscriptions
        for i, sub in enumerate(self._global_subscriptions):
            if sub.subscription_id == subscription_id:
                sub.active = False
                self._global_subscriptions.pop(i)
                logger.info(f"[Notifier] Unsubscribed global {subscription_id}")
                return True

        return False

    def publish(self, event: TaskEvent) -> int:
        """Publish a task event.

        Args:
            event: The task event to publish

        Returns:
            Number of subscribers that received the event

        """
        # Store in history
        self._event_history[event.task_id].append(event)
        if len(self._event_history[event.task_id]) > self.max_history:
            self._event_history[event.task_id].pop(0)

        # Queue for async processing
        try:
            self._event_queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning(f"[Notifier] Event queue full, dropping event {event.event_id}")

        # Also process synchronously for immediate callbacks
        return self._dispatch_event(event)

    def _dispatch_event(self, event: TaskEvent) -> int:
        """Dispatch event to matching subscribers."""
        delivered = 0

        # Task-specific subscriptions
        for sub in self._subscriptions.get(event.task_id, []):
            if not sub.active:
                continue
            if event.event_type in sub.event_types:
                try:
                    result = sub.callback(event)  # type: ignore[func-returns-value]
                    if asyncio.iscoroutine(result):
                        _spawn_background(result)
                    delivered += 1
                except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
                    logger.error(f"[Notifier] Callback error for {sub.subscription_id}: {e}")

        # Global subscriptions
        for sub in self._global_subscriptions:
            if not sub.active:
                continue
            if event.event_type in sub.event_types:
                try:
                    result = sub.callback(event)  # type: ignore[func-returns-value]
                    if asyncio.iscoroutine(result):
                        _spawn_background(result)
                    delivered += 1
                except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
                    logger.error(f"[Notifier] Global callback error for {sub.subscription_id}: {e}")

        return delivered

    async def wait_for_task(
        self,
        task_id: str,
        timeout: float = 600.0,
    ) -> TaskEvent:
        """Wait for a task to reach a terminal state.

        This is a convenience method that creates a one-time subscription.

        Args:
            task_id: Task ID to wait for
            timeout: Maximum time to wait in seconds

        Returns:
            The terminal event (COMPLETED, FAILED, or CANCELLED)

        Raises:
            asyncio.TimeoutError: If timeout is reached

        """
        # v1.2.0 (RG-7, BGL-010): this is an async method, so the running loop
        # is the correct loop. get_event_loop() emitted a DeprecationWarning
        # on the main thread and a RuntimeError on a worker thread.
        future: asyncio.Future[TaskEvent] = asyncio.get_running_loop().create_future()

        def on_terminal(event: TaskEvent) -> None:
            if not future.done():
                future.set_result(event)

        # Subscribe to terminal events
        sub_id = self.subscribe(
            task_id=task_id,
            event_types=[
                TaskEventType.TASK_COMPLETED,
                TaskEventType.TASK_FAILED,
                TaskEventType.TASK_CANCELLED,
            ],
            callback=on_terminal,
        )

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self.unsubscribe(sub_id)

    async def start_processor(self) -> None:
        """Start the async event processor.

        This is needed for environments where events are posted
        from synchronous code but callbacks are async.  # noqa: E402
        """
        if self._processing:
            return

        self._processing = True
        self._processor_task = asyncio.create_task(self._process_events())
        logger.info("[Notifier] Event processor started")

    async def stop_processor(self) -> None:
        """Stop the async event processor."""
        self._processing = False
        if self._processor_task:
            self._processor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._processor_task
        logger.info("[Notifier] Event processor stopped")

    async def _next_event(self, timeout: float) -> TaskEvent | None:
        """Return the next queued event, or None if the wait elapses.

        Converts the poll timeout from an exception into a value. "No event
        yet" is the loop's normal idle state, not a fault, and the caller
        should not have to tell the two apart with a handler.

        Args:
            timeout: Seconds to wait before giving up on this tick.

        Returns:
            The next event, or None if none arrived within the timeout.

        """
        try:
            return await asyncio.wait_for(self._event_queue.get(), timeout=timeout)
        except TimeoutError:
            return None

    async def _process_events(self) -> None:
        """Process events from the queue."""
        while self._processing:
            try:
                event = await self._next_event(timeout=1.0)
                if event is None:
                    # Idle tick. The 1s bound is the loop's liveness heartbeat:
                    # it lets the loop re-read self._processing so stop() stays
                    # responsive when no events are arriving. Not a failure, so
                    # nothing is logged.
                    continue
                self._dispatch_event(event)
            except asyncio.CancelledError:
                break
            except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
                logger.error(f"[Notifier] Event processing error: {e}")

    # ── Orpheus Ring Integration ──────────────────────────────────────────────

    def write_to_ring(self, event: TaskEvent) -> bool:
        """Write event to Orpheus ring buffer for inter-process communication.

        Args:
            event: The event to write

        Returns:
            True if write succeeded

        """
        ring_file = Path(self.ring_path) / "task_events.ring"

        try:
            ring_file.parent.mkdir(parents=True, exist_ok=True)

            # Write event as newline-delimited JSON
            with open(ring_file, "a", encoding="utf-8") as f:
                f.write(event.to_json() + "\n")

            logger.debug(f"[Notifier] Wrote event {event.event_id} to ring")
            return True
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as e:
            logger.error(f"[Notifier] Ring write error: {e}")
            return False

    def read_from_ring(self) -> list[TaskEvent]:
        """Read pending events from Orpheus ring.

        Returns:
            List of events (may be empty)

        """
        ring_file = Path(self.ring_path) / "task_events.ring"

        if not ring_file.exists():
            return []

        events = []
        try:
            with open(ring_file, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        events.append(TaskEvent.from_json(line))
                    except json.JSONDecodeError:
                        logger.warning(f"[Notifier] Invalid event in ring: {line[:50]}")

            # Clear the ring after reading
            ring_file.write_text("")

            return events
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as e:
            logger.error(f"[Notifier] Ring read error: {e}")
            return []


# ── Global Instance ───────────────────────────────────────────────────────────

_notifier: TaskNotifier | None = None


def get_notifier(ring_path: str | None = None) -> TaskNotifier:
    """Get or create the global notifier instance."""
    global _notifier
    if _notifier is None:
        _notifier = TaskNotifier(ring_path=ring_path)
    return _notifier


def reset_notifier() -> None:
    """Reset the global notifier."""
    global _notifier
    if _notifier:
        try:
            loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            # Called from synchronous context (tests, shutdown hooks). There is
            # no loop to schedule the stop on; dropping the reference below is
            # the whole reset in that case.
            loop = None
        if loop is not None:
            _spawn_background(_notifier.stop_processor(), loop=loop)
        else:
            logger.info(
                "[Notifier] Reset outside a running event loop; the processor task "
                "was not stopped explicitly."
            )
    _notifier = None


# ── Convenience Functions ──────────────────────────────────────────────────────


def notify_task_started(task_id: str, data: dict[str, Any] | None = None) -> None:
    """Notify that a task has started."""
    notifier = get_notifier()
    event = TaskEvent(
        event_id=str(uuid.uuid4()),
        task_id=task_id,
        event_type=TaskEventType.TASK_STARTED,
        timestamp=datetime.now(UTC),
        data=data or {},
    )
    notifier.publish(event)
    notifier.write_to_ring(event)


def notify_task_progress(task_id: str, progress: float, message: str = "") -> None:
    """Notify task progress update."""
    notifier = get_notifier()
    event = TaskEvent(
        event_id=str(uuid.uuid4()),
        task_id=task_id,
        event_type=TaskEventType.TASK_PROGRESS,
        timestamp=datetime.now(UTC),
        data={"progress": progress, "message": message},
    )
    notifier.publish(event)


def notify_task_completed(task_id: str, result: dict[str, Any]) -> None:
    """Notify that a task completed successfully."""
    notifier = get_notifier()
    event = TaskEvent(
        event_id=str(uuid.uuid4()),
        task_id=task_id,
        event_type=TaskEventType.TASK_COMPLETED,
        timestamp=datetime.now(UTC),
        data={"result": result},
    )
    notifier.publish(event)
    notifier.write_to_ring(event)


def notify_task_failed(task_id: str, error: str, details: dict[str, Any] | None = None) -> None:
    """Notify that a task failed."""
    notifier = get_notifier()
    event = TaskEvent(
        event_id=str(uuid.uuid4()),
        task_id=task_id,
        event_type=TaskEventType.TASK_FAILED,
        timestamp=datetime.now(UTC),
        data={"error": error, "details": details or {}},
    )
    notifier.publish(event)
    notifier.write_to_ring(event)


def notify_task_cancelled(task_id: str, reason: str = "") -> None:
    """Notify that a task was cancelled."""
    notifier = get_notifier()
    event = TaskEvent(
        event_id=str(uuid.uuid4()),
        task_id=task_id,
        event_type=TaskEventType.TASK_CANCELLED,
        timestamp=datetime.now(UTC),
        data={"reason": reason},
    )
    notifier.publish(event)
    notifier.write_to_ring(event)


if __name__ == "__main__":
    import doctest

    doctest.testmod()
