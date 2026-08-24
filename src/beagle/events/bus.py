"""Thread-safe event bus with topic-based routing for Beagle."""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import sys
import threading
import uuid
from collections import deque
from collections.abc import Callable
from typing import Any

from .events import BeagleEvent

logger = logging.getLogger("Beagle.events")

EventCallback = Callable[[BeagleEvent], Any]

# v0.3.0: Size-based ring buffer cap to prevent memory exhaustion from large events
_MAX_RING_BUFFER_BYTES = 10 * 1024 * 1024  # 10 MB
_CALLBACK_TIMEOUT_SECONDS = 5.0


class EventBus:
    """Thread-safe event bus with topic-based routing."""

    def __init__(self) -> None:
        # sub_id -> (pattern, callback)
        self._subscribers: dict[str, tuple[str, EventCallback]] = {}
        self._lock = threading.Lock()
        self._ring_buffer: deque[BeagleEvent] = deque(maxlen=1000)
        self._ring_buffer_bytes: int = 0
        # Hold strong references to pending async callback tasks so they
        # are not garbage-collected before completion (RUF006).
        self._pending_tasks: set[asyncio.Task[None]] = set()

    def publish(self, event: BeagleEvent) -> None:
        """Publish an event to all matching subscribers.

        Non-blocking, never raises. Swallows callback exceptions.
        """
        try:
            event_size = sys.getsizeof(event)
            with self._lock:
                self._ring_buffer.append(event)
                self._ring_buffer_bytes += event_size
                # Evict oldest events if total size exceeds budget
                while (
                    self._ring_buffer_bytes > _MAX_RING_BUFFER_BYTES and len(self._ring_buffer) > 1
                ):
                    evicted = self._ring_buffer.popleft()
                    self._ring_buffer_bytes -= sys.getsizeof(evicted)
                self._ring_buffer_bytes = max(0, self._ring_buffer_bytes)
                # Take a snapshot of subscribers to avoid holding lock during execution
                subs = list(self._subscribers.values())

            for pattern, callback in subs:
                if fnmatch.fnmatch(event.event_type, pattern):
                    self._execute_callback_safe(callback, event)
        except (RuntimeError, ValueError, TypeError, AttributeError, OSError, KeyError) as e:
            logger.error(f"EventBus critical error during publish: {e}", exc_info=True)
        return None

    def _execute_callback_safe(self, callback: EventCallback, event: BeagleEvent) -> None:
        """Execute a callback safely, handling both sync and async."""
        try:
            if asyncio.iscoroutinefunction(callback):
                try:
                    loop = asyncio.get_running_loop()
                    _task = loop.create_task(self._run_async_callback_with_timeout(callback, event))
                    self._pending_tasks.add(_task)
                    _task.add_done_callback(self._pending_tasks.discard)
                except RuntimeError:
                    # No running loop — run directly with timeout via asyncio.run
                    asyncio.run(self._run_async_callback_with_timeout(callback, event))
            else:
                callback(event)
        except (RuntimeError, ValueError, TypeError, AttributeError, OSError) as e:
            cb_name = getattr(callback, "__name__", repr(callback))
            logger.error(f"EventBus caught exception from callback {cb_name}: {e}")

    @staticmethod
    async def _run_async_callback_with_timeout(callback: EventCallback, event: BeagleEvent) -> None:
        """Run an async callback with a timeout guard."""
        try:
            await asyncio.wait_for(callback(event), timeout=_CALLBACK_TIMEOUT_SECONDS)
        except TimeoutError:
            cb_name = getattr(callback, "__name__", repr(callback))
            logger.warning(
                f"EventBus async callback {cb_name} timed out after {_CALLBACK_TIMEOUT_SECONDS}s"
            )
        except (RuntimeError, ValueError, TypeError, AttributeError, OSError) as e:
            cb_name = getattr(callback, "__name__", repr(callback))
            logger.error(f"EventBus async callback {cb_name} raised: {e}")

    def subscribe(self, pattern: str, callback: EventCallback) -> str:
        """Subscribe to events matching the pattern.

        Returns:
            Subscription ID (UUID string).

        """
        sub_id = str(uuid.uuid4())

        with self._lock:
            if len(self._subscribers) >= 1000:
                logger.warning("[EventBus] Max subscribers (1000) reached, rejecting")
                return ""

            self._subscribers[sub_id] = (pattern, callback)

            # Replay past events from ring buffer
            replay_events = [e for e in self._ring_buffer if fnmatch.fnmatch(e.event_type, pattern)]

        # Execute replay outside the lock
        for event in replay_events:
            self._execute_callback_safe(callback, event)

        return sub_id

    def unsubscribe(self, sub_id: str) -> None:
        """Remove a subscription by ID."""
        with self._lock:
            if sub_id in self._subscribers:
                del self._subscribers[sub_id]


# Module-level singleton
_bus: EventBus | None = None
_bus_lock = threading.Lock()


def get_event_bus() -> EventBus:
    """Get the global event bus instance."""
    global _bus
    with _bus_lock:
        if _bus is None:
            _bus = EventBus()
    return _bus


def reset_event_bus() -> EventBus:
    """Reset the global event bus singleton — primarily for test isolation.

    Creates a fresh EventBus with an empty ring buffer and no subscribers.
    Returns the new instance so callers can chain.
    """
    global _bus
    with _bus_lock:
        _bus = EventBus()
    return _bus
