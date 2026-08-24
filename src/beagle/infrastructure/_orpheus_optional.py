"""Optional proprietary-transport compatibility shims.

The open-source beagle distribution ships NO proprietary transport code.
The ``beagle-orpheus`` wheel (separately licensed — free for evaluation,
paid for commercial use) provides the native FlatBuffers-over-ring-buffer
implementation and exposes it through :mod:`beagle_orpheus.compat`.

Every public module that historically imported these symbols now imports
them *from here*; without the wheel installed the stubs raise an
informative error at first USE (never at import time), so a default
install keeps working on the built-in HTTP transport.

<invariant>
Stub symbols must never raise at import time and must preserve the exact
names/arity of the wheel-provided counterparts, so installing
``beagle-orpheus`` is a drop-in activation.
</invariant>
"""

from __future__ import annotations

from typing import Any

_INSTALL_HINT = (
    "beagle-orpheus is not installed. The native Orpheus ring-buffer "
    "transport is separately licensed (free for evaluation; commercial use "
    "requires a paid license). Install it explicitly after making that "
    "decision:  pip install beagle_orpheus-*.whl"
)


def _raise_unavailable(symbol: str) -> RuntimeError:
    return RuntimeError(f"{symbol}: {_INSTALL_HINT}")


class OrpheusClient:  # pragma: no cover - presence guard only
    """Stub replaced by ``beagle_orpheus.compat.OrpheusClient`` when installed."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise _raise_unavailable("OrpheusClient")


class TaskType:  # pragma: no cover - presence guard only
    """Enum-like stub; any attribute access raises with install guidance."""

    def __getattr__(self, name: str) -> Any:
        raise _raise_unavailable(f"TaskType.{name}")


class A2AMessage:  # pragma: no cover - presence guard only
    """Stub replaced by ``beagle_orpheus.compat.A2AMessage`` when installed."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise _raise_unavailable("A2AMessage")


class MessageType:  # pragma: no cover - presence guard only
    def __getattr__(self, name: str) -> Any:
        raise _raise_unavailable(f"MessageType.{name}")


def get_orpheus_client(*args: Any, **kwargs: Any) -> Any:
    raise _raise_unavailable("get_orpheus_client")


def get_ipc(*args: Any, **kwargs: Any) -> Any:
    raise _raise_unavailable("get_ipc")


def create_rings(*args: Any, **kwargs: Any) -> dict[str, Any]:
    raise _raise_unavailable("create_rings")


def cleanup_rings(*args: Any, **kwargs: Any) -> dict[str, Any]:
    raise _raise_unavailable("cleanup_rings")


__all__ = [
    "A2AMessage",
    "MessageType",
    "OrpheusClient",
    "TaskType",
    "cleanup_rings",
    "create_rings",
    "get_ipc",
    "get_orpheus_client",
]
