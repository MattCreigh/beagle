"""Orchestrator package — split from autonomous_orchestrator.py monolith.

Submodules:
- system_directive: SYSTEM_DIRECTIVE_TEMPLATE, model fallbacks, feature flags
- executor: BeagleDAGNode, EVH validation, process lifecycle management
- state_manager: CompressedAgentState, KV pool, agent call tracking, channel comms

The DAGOrchestrator class remains in core/autonomous_orchestrator.py (the
facade) to preserve backward compatibility — it imports execution and state
management from this package.

All public symbols are re-exported here so that both import paths work::

    from beagle.core.autonomous_orchestrator import DAGOrchestrator
    from beagle.core.orchestrator import DAGOrchestrator  # also works

SP-12: the re-exported names are listed explicitly and repeated in ``__all__``
so the public surface is the code rather than a set of ``unused-import``
suppression comments. DAGOrchestrator stays a lazy import (via __getattr__) to
break the import cycle with the facade.
"""

from __future__ import annotations

from beagle.core.orchestrator.executor import (
    DEFAULT_SUBPROCESS_TIMEOUT,
    DEFAULT_VALIDATION_TIMEOUT,
    SUBPROCESS_MEMORY_LIMIT,
    BeagleDAGNode,
    _cleanup_processes,
    _run_evh_validation,
    _signal_handler,
)
from beagle.core.orchestrator.state_manager import (
    CompressedAgentState,
    CompressedKVPool,
    _add_process,
    _remove_process,
    cleanup_agent_call_counter,
    get_agent_call_count,
    get_kv_pool,
    increment_agent_call,
    ping_orchestrator,
    reset_agent_call_counter,
    set_orchestrator_channel,
)
from beagle.core.orchestrator.system_directive import (
    DEFAULT_MAX_NESTED_AGENTS,
    ENHANCED_MODES,
    MODEL_FALLBACKS,
    SYSTEM_DIRECTIVE_TEMPLATE,
    _load_model_fallbacks,
)

__all__ = [
    "DEFAULT_MAX_NESTED_AGENTS",
    "DEFAULT_SUBPROCESS_TIMEOUT",
    "DEFAULT_VALIDATION_TIMEOUT",
    "ENHANCED_MODES",
    "MODEL_FALLBACKS",
    "SUBPROCESS_MEMORY_LIMIT",
    "SYSTEM_DIRECTIVE_TEMPLATE",
    "BeagleDAGNode",
    "CompressedAgentState",
    "CompressedKVPool",
    "DAGOrchestrator",
    "_add_process",
    "_cleanup_processes",
    "_load_model_fallbacks",
    "_remove_process",
    "_run_evh_validation",
    "_signal_handler",
    "cleanup_agent_call_counter",
    "get_agent_call_count",
    "get_kv_pool",
    "increment_agent_call",
    "ping_orchestrator",
    "reset_agent_call_counter",
    "set_orchestrator_channel",
]


def __getattr__(name: str):
    """Lazy import DAGOrchestrator to break the cycle with the facade."""
    if name == "DAGOrchestrator":
        from beagle.core.autonomous_orchestrator import (
            DAGOrchestrator,
        )

        return DAGOrchestrator
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
