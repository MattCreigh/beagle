# tags: ["events", "pub-sub", "thread-safety"]
# description: Thread-safe event bus with topic routing
# source: beagle/events/bus.py
#
# This example shows the Beagle event bus pattern for decoupled communication.
# Subscribers register for topics; publishers emit events without knowing
# who receives them. Thread safety is ensured via locks.

import contextlib
import threading
from collections import defaultdict
from collections.abc import Callable
from typing import Any


class EventBus:
    """Thread-safe pub-sub event bus with topic-based routing.

    Usage:
        bus = EventBus()
        bus.subscribe("workflow.started", lambda e: print(f"Started: {e}"))
        bus.publish("workflow.started", {"workflow": "research"})
    """

    def __init__(self, max_subscribers: int = 1000) -> None:
        self._subscribers: dict[str, list[Callable[[Any], None]]] = defaultdict(list)
        self._lock = threading.Lock()
        self._max_subscribers = max_subscribers

    def subscribe(self, topic: str, callback: Callable[[Any], None]) -> None:
        """Register a callback for a topic."""
        with self._lock:
            if len(self._subscribers[topic]) >= self._max_subscribers:
                raise ValueError(f"Subscriber cap ({self._max_subscribers}) reached for '{topic}'")
            self._subscribers[topic].append(callback)

    def unsubscribe(self, topic: str, callback: Callable[[Any], None]) -> None:
        """Remove a callback from a topic."""
        with self._lock:
            self._subscribers[topic] = [cb for cb in self._subscribers[topic] if cb != callback]

    def publish(self, topic: str, event: Any) -> None:
        """Emit an event to all subscribers of the topic."""
        with self._lock:
            callbacks = list(self._subscribers.get(topic, []))

        for callback in callbacks:
            with contextlib.suppress(Exception):  # Don't let one failing callback break others
                callback(event)
