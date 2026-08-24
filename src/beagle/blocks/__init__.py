"""Block composition framework for Beagle agents.

Phases 1-2: Foundation, Block Contracts, Registry, Context.
"""

from __future__ import annotations

from .context import BlockResult, BlockStatus, ExecutionContext, ExecutionResult
from .errors import (
    BlockError,
    BlockNotFoundError,
    BlockTimeoutError,
    BudgetExceededError,
    ExecutionError,
    SchemaError,
)
from .jinja_env import get_jinja_env, render_template
from .registry import BlockRegistry, PythonBlock
from .schema import AgentDefinition, AgentManifest, BlockRef, SchemaVersion, VariableBinding

__all__ = [
    "AgentDefinition",
    "AgentManifest",
    "BlockError",
    "BlockNotFoundError",
    "BlockRef",
    "BlockRegistry",
    "BlockResult",
    "BlockStatus",
    "BlockTimeoutError",
    "BudgetExceededError",
    "ExecutionContext",
    "ExecutionError",
    "ExecutionResult",
    "PythonBlock",
    "SchemaError",
    "SchemaVersion",
    "VariableBinding",
    "get_jinja_env",
    "render_template",
]
