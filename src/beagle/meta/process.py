"""Steerable, observable meta-processes (D1).

A :class:`MetaProcess` is a self-regulating loop that tunes and observes one
of Beagle's meta-level subsystems: context folding, memory consolidation,
budget enforcement, routing, or verification. Each exposes ``tune`` (adjust
a knob) and ``observe`` (return a structured report of its current state,
last run, and decisions). The tuning knobs live in the configuration TOML;
the observations are returned as a structured report so an MCP client can
read and steer the loop without a restart.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class MetaObservation:
    """A structured report of a meta-process's state.

    Attributes:
        process: The process name.
        healthy: Whether the process is functioning.
        metrics: Key/value metrics (e.g. last fold size, consolidation count).
        last_run: ISO timestamp of the last run, or empty if never run.
        decisions: Recent decisions the process made.

    """

    process: str
    healthy: bool = True
    metrics: dict[str, Any] = field(default_factory=dict)
    last_run: str = ""
    decisions: list[str] = field(default_factory=list)


class MetaProcess(Protocol):
    """Protocol for a steerable, observable meta-process.

    A meta-process owns one tuning knob (a threshold, cadence, or budget)
    and reports its state. ``tune`` must take effect without a restart.
    """

    name: str

    def tune(self, knob: str, value: Any) -> None:
        """Adjust a tuning knob.

        Args:
            knob: The knob name (e.g. ``threshold``, ``cadence_seconds``).
            value: The new value.

        Raises:
            KeyError: When the knob is unknown.

        """

    def observe(self) -> MetaObservation:
        """Return a structured report of the process's state.

        Returns:
            A :class:`MetaObservation`.

        """


@dataclass
class Knob:
    """A single tunable parameter of a meta-process.

    Attributes:
        name: The knob name.
        value: The current value.
        description: Human-readable description of what the knob controls.

    """

    name: str
    value: Any
    description: str = ""


class BaseMetaProcess:
    """Concrete base for a meta-process with a knob map.

    Subclasses declare their knobs in :attr:`_knobs` and implement
    :meth:`_apply` to push a knob value into the live subsystem.
    """

    name: str = "base"

    def __init__(self) -> None:
        """Initialise the knob map."""
        self._knobs: dict[str, Knob] = {}

    def _declare(self, name: str, value: Any, description: str = "") -> None:
        """Declare a knob.

        Args:
            name: The knob name.
            value: The initial value.
            description: What the knob controls.

        """
        self._knobs[name] = Knob(name=name, value=value, description=description)

    def tune(self, knob: str, value: Any) -> None:
        """Adjust a knob and apply it to the live subsystem.

        Args:
            knob: The knob name.
            value: The new value.

        Raises:
            KeyError: When the knob is unknown.

        """
        if knob not in self._knobs:
            raise KeyError(f"unknown knob {knob!r} for process {self.name!r}")
        self._knobs[knob].value = value
        self._apply(knob, value)

    def _apply(self, knob: str, value: Any) -> None:
        """Push a knob value into the live subsystem.

        Args:
            knob: The knob name.
            value: The new value.

        """

    def observe(self) -> MetaObservation:
        """Return a structured report of the process's state.

        Returns:
            A :class:`MetaObservation` with the current knob values.

        """
        return MetaObservation(
            process=self.name,
            healthy=True,
            metrics={k: v.value for k, v in self._knobs.items()},
        )


# ── Registry ────────────────────────────────────────────────────────────────

_REGISTRY: dict[str, MetaProcess] = {}


def register(process: MetaProcess) -> None:
    """Register a meta-process by name.

    Args:
        process: The process to register.

    """
    _REGISTRY[process.name] = process


def get_process(name: str) -> MetaProcess:
    """Resolve a meta-process by name.

    Args:
        name: The process name.

    Returns:
        The process.

    Raises:
        KeyError: When the name is not registered.

    """
    if name not in _REGISTRY:
        raise KeyError(f"unknown meta-process {name!r}; available: {sorted(_REGISTRY)}")
    return _REGISTRY[name]


def list_processes() -> list[str]:
    """List registered meta-process names.

    Returns:
        The sorted list of process names.

    """
    return sorted(_REGISTRY)
