"""Execution context and result dataclasses for block composition."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class BlockStatus(StrEnum):
    """Execution status of a block.

    Salvaged from the retired ``beagle.blocks`` package
    (``BaseBlock`` contract). ``SKIPPED`` exists so a non-fatal outcome
    ("this block did not run, and that is acceptable") is representable
    without being conflated with ``FAILURE`` — the precondition for
    optional (``required=False``) bindings to mean anything.
    """

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILURE = "failure"
    SKIPPED = "skipped"


@dataclass
class ExecutionContext:
    """Mutable context passed through block execution."""

    inputs: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    depth: int = 0

    def set(self, key: str, value: Any) -> None:
        self.outputs[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        return self.outputs.get(key, self.inputs.get(key, default))


@dataclass
class BlockResult:
    """Result from a single block execution.

    Carries block identity (``block_name``) and a list of error messages
    (``errors``) so callers can attribute and inspect failures without
    parsing a traceback. ``status`` is the single source of truth;
    ``success``/``error`` are derived back-compat shims.
    """

    status: BlockStatus
    block_name: str = ""
    output: Any = None
    errors: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0
    cost_usd: float = 0.0
    tokens: int = 0

    @property
    def success(self) -> bool:
        """True iff the block completed successfully (back-compat shim)."""
        return self.status == BlockStatus.SUCCESS

    @property
    def error(self) -> str:
        """First error message, or "" when none (back-compat shim)."""
        return self.errors[0] if self.errors else ""


@dataclass
class ExecutionResult:
    """Result from a full agent execution."""

    success: bool
    final_output: Any = None
    block_results: list[BlockResult] = field(default_factory=list)
    total_duration_seconds: float = 0.0
    total_cost_usd: float = 0.0
    errors: list[str] = field(default_factory=list)
