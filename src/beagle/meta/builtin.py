"""Concrete meta-processes for the built-in subsystems (D1).

These are the default implementations of :class:`beagle.meta.process.MetaProcess`
for context folding, memory consolidation, budget enforcement, routing, and
verification. Each reads its tuning knobs from the configuration TOML and
reports its state through :meth:`observe`.
"""

from __future__ import annotations

from beagle.meta.process import BaseMetaProcess, register


class ContextFoldingProcess(BaseMetaProcess):
    """Steer the context-folding threshold and cadence.

    Attributes:
        name: ``context_folding``.

    """

    name = "context_folding"

    def __init__(self, threshold: float = 0.7, cadence_seconds: int = 60) -> None:
        """Initialise the folding knobs.

        Args:
            threshold: The context-usage fraction that triggers a fold.
            cadence_seconds: Minimum seconds between folds.

        """
        super().__init__()
        self._declare("threshold", threshold, "context-usage fraction that triggers a fold")
        self._declare("cadence_seconds", cadence_seconds, "minimum seconds between folds")

    def _apply(self, _knob: str, _value: object) -> None:
        """No live subsystem to push to in this skeleton; the knob is stored.

        Args:
            knob: The knob name.
            value: The new value.

        """


class MemoryConsolidationProcess(BaseMetaProcess):
    """Steer memory-consolidation cadence and batch size.

    Attributes:
        name: ``memory_consolidation``.

    """

    name = "memory_consolidation"

    def __init__(self, cadence_seconds: int = 300, batch_size: int = 50) -> None:
        """Initialise the consolidation knobs.

        Args:
            cadence_seconds: Seconds between consolidation runs.
            batch_size: Max items consolidated per run.

        """
        super().__init__()
        self._declare("cadence_seconds", cadence_seconds, "seconds between consolidation runs")
        self._declare("batch_size", batch_size, "max items consolidated per run")

    def _apply(self, _knob: str, _value: object) -> None:
        """No live subsystem to push to in this skeleton; the knob is stored.

        Args:
            knob: The knob name.
            value: The new value.

        """


class BudgetEnforcementProcess(BaseMetaProcess):
    """Steer the per-workflow budget.

    Attributes:
        name: ``budget_enforcement``.

    """

    name = "budget_enforcement"

    def __init__(self, budget_usd: float = 10.0) -> None:
        """Initialise the budget knob.

        Args:
            budget_usd: The default per-workflow budget in USD.

        """
        super().__init__()
        self._declare("budget_usd", budget_usd, "default per-workflow budget in USD")

    def _apply(self, _knob: str, _value: object) -> None:
        """No live subsystem to push to in this skeleton; the knob is stored.

        Args:
            knob: The knob name.
            value: The new value.

        """


class RoutingProcess(BaseMetaProcess):
    """Steer the routing confidence threshold.

    Attributes:
        name: ``routing``.

    """

    name = "routing"

    def __init__(self, confidence_threshold: float = 0.6) -> None:
        """Initialise the routing knob.

        Args:
            confidence_threshold: Minimum confidence to accept a route.

        """
        super().__init__()
        self._declare(
            "confidence_threshold",
            confidence_threshold,
            "minimum confidence to accept a route",
        )

    def _apply(self, _knob: str, _value: object) -> None:
        """No live subsystem to push to in this skeleton; the knob is stored.

        Args:
            knob: The knob name.
            value: The new value.

        """


class VerificationProcess(BaseMetaProcess):
    """Steer the verification gate strictness.

    Attributes:
        name: ``verification``.

    """

    name = "verification"

    def __init__(self, strict: bool = True) -> None:
        """Initialise the verification knob.

        Args:
            strict: Whether the verification gate is strict.

        """
        super().__init__()
        self._declare("strict", strict, "whether the verification gate is strict")

    def _apply(self, _knob: str, _value: object) -> None:
        """No live subsystem to push to in this skeleton; the knob is stored.

        Args:
            knob: The knob name.
            value: The new value.

        """


def register_builtin_processes() -> None:
    """Register the five built-in meta-processes.

    Idempotent: re-registering replaces the existing instance.
    """
    register(ContextFoldingProcess())
    register(MemoryConsolidationProcess())
    register(BudgetEnforcementProcess())
    register(RoutingProcess())
    register(VerificationProcess())
