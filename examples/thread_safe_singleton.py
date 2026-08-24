# tags: ["singleton", "thread-safety", "design-pattern"]
# description: Thread-safe singleton with double-checked locking
# source: beagle/core/singletons.py
#
# This example shows the Beagle singleton pattern used for global mutable state.
# Double-checked locking avoids the cost of acquiring the lock on every access
# after initialization, while still guaranteeing thread safety.

import threading
from typing import Generic, TypeVar

T = TypeVar("T")


class ThreadSafeSingleton(Generic[T]):
    """Thread-safe singleton base with double-checked locking.

    Usage:
        class MySingleton(ThreadSafeSingleton[MyService]):
            def _create(self) -> MyService:
                return MyService()

        svc = MySingleton()
        instance = svc.get()  # Thread-safe, lazy initialization
    """

    def __init__(self, name: str | None = None) -> None:
        self._name = name or self.__class__.__name__
        self._instance: T | None = None
        self._lock = threading.Lock()

    def get(self) -> T:
        """Get the singleton instance, creating if necessary (double-checked lock)."""
        if self._instance is not None:
            return self._instance

        with self._lock:
            if self._instance is not None:
                return self._instance

            self._instance = self._create()
            return self._instance

    def _create(self) -> T:
        """Override in subclass to create the singleton instance."""
        raise NotImplementedError

    def reset(self) -> None:
        """Reset the singleton (useful for testing)."""
        with self._lock:
            self._instance = None
