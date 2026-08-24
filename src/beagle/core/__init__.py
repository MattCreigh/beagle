"""Core workflow orchestration components."""

from importlib import import_module

from .autonomous_orchestrator import DAGOrchestrator
from .bootstrap_graph import BootstrapGraph, BootstrapStage
from .deferred_init import DeferredInitializer
from .orchestrator_types import (
    AgentPingMessage,
    AgentState,
    DAGNode,
    GooseExecutionError,
)
from .query_config import DEFAULT_QUERY_CONFIG, QueryEngineConfig
from .tool_pool import ToolPool, assemble_tool_pool

__all__ = [
    "DEFAULT_QUERY_CONFIG",
    "AgentPingMessage",
    "AgentState",
    "AutonomousOrchestrator",
    "BootstrapGraph",
    "BootstrapStage",
    "DAGNode",
    "DAGOrchestrator",
    "DeferredInitializer",
    "GooseExecutionError",
    "QueryEngineConfig",
    "ToolPool",
    "assemble_tool_pool",
    "checkpointer",
    "config_watcher",
    "dag_nodes",
    "graph",
    "nodes",
    "router",
    "state",
    "workflow_loader",
]


def __getattr__(name: str):
    """Lazy import to avoid circular dependencies."""
    lazy_imports = {
        "AutonomousOrchestrator": ".autonomous_orchestrator",
        "graph": ".graph",
        "nodes": ".nodes",
        "router": ".router",
        "state": ".state",
        "workflow_loader": ".workflow_loader",
        "checkpointer": "..memory.checkpointer",
        "dag_nodes": ".dag_nodes",
        "config_watcher": ".config_watcher",
        "turboquant": ".turboquant",
        "skill_library": ".skill_library",
        "a2a_protocol": ".a2a_protocol",
    }
    if name in lazy_imports:
        module = import_module(lazy_imports[name], __package__)
        return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
