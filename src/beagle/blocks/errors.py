"""Block composition error hierarchy."""

from __future__ import annotations


class BlockError(Exception):
    """Base error for block composition framework."""

    def __init__(self, message: str, block_name: str = "", details: dict | None = None):
        super().__init__(message)
        self.block_name = block_name
        self.details = details or {}


class SchemaError(BlockError):
    """Raised when a block schema is invalid."""


class BlockNotFoundError(BlockError):
    """Raised when a referenced block is not found."""


class ExecutionError(BlockError):
    """Raised during block execution (non-retryable)."""


class BlockTimeoutError(BlockError):
    """Raised when a block exceeds its execution timeout."""


class BudgetExceededError(BlockError):
    """Raised when a block exceeds its allocated budget."""
