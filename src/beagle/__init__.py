"""Beagle — Autonomous agentic workflow system with secure RAG and MCP integration.

A multi-agent workflow orchestration system built on LangGraph for Goose.

Usage:
    from beagle import AutonomousOrchestrator
    from beagle.config import load_config

    config = load_config()
    orchestrator = AutonomousOrchestrator(config)
    result = await orchestrator.run("Analyze the codebase")

Initialization:
    from beagle import init
    init()  # Syncs recipes→agents, returns available agent list
"""

from importlib import import_module

from .constants import PACKAGE_VERSION

# Lazy imports to avoid circular dependencies

# constants.py declares itself the single source of truth for the version
# ("Do NOT hardcode version strings elsewhere"). This was a hardcoded "1.0.0"
# in violation of that, and constants.PACKAGE_VERSION was left at "13.22.3"
# by the v1.0.0 release — so the package reported two different versions
# depending on which name you read.
__version__ = PACKAGE_VERSION

# v1.0.0: the security/permission names below were re-exported by a nested
# subpackage (`aeca/`, briefly `beagle/beagle/`). That subpackage was a second
# name for this same package, and the duplication is what broke packaging.
# It is dissolved — there is one Beagle, and these are exported from it.
__all__ = [
    "DEFAULT_PERMISSION_CONTEXT",
    "READ_ONLY_PERMISSION_CONTEXT",
    "AutonomousOrchestrator",
    "ContextMonitor",
    "ToolPermissionContext",
    "cache",
    "config",
    "context_compaction_hook",
    "graph",
    "init",
    "nodes",
    "paths",
    "post_compaction_rehydration",
    "rate_limiter",
    "recipe_agent_bridge",
    "router",
    "scrub_secrets",
    "state",
    "validate_agent_type",
    "validate_file_path",
    "validate_prompt",
    "validate_query",
]


def __getattr__(name: str):
    """Lazy import to avoid circular dependencies."""
    lazy_imports = {
        "AutonomousOrchestrator": (".core.autonomous_orchestrator", "AutonomousOrchestrator"),
        "ContextMonitor": (".context.trigger", "ContextMonitor"),
        "graph": (".core.graph", None),
        "nodes": (".core.nodes", None),
        "router": (".core.router", None),
        "state": (".core.state", None),
        "config": (".config.config", None),
        "paths": (".config.paths", None),
        "cache": (".utils.cache", None),
        "rate_limiter": (".utils.rate_limiter", None),
        "context_compaction_hook": (".context.context_compaction_hook", None),
        "recipe_agent_bridge": (".context.recipe_agent_bridge", None),
        "post_compaction_rehydration": (".context.post_compaction_rehydration", None),
        # v1.0.0: formerly re-exported by the nested subpackage.
        # Kept lazy for the same reason as everything above — importing
        # security eagerly at package import time reintroduces a cycle.
        "DEFAULT_PERMISSION_CONTEXT": (".permission_context", "DEFAULT_PERMISSION_CONTEXT"),
        "READ_ONLY_PERMISSION_CONTEXT": (".permission_context", "READ_ONLY_PERMISSION_CONTEXT"),
        "ToolPermissionContext": (".permission_context", "ToolPermissionContext"),
        "scrub_secrets": (".security", "scrub_secrets"),
        "validate_agent_type": (".security", "validate_agent_type"),
        "validate_file_path": (".security", "validate_file_path"),
        "validate_prompt": (".security", "validate_prompt"),
        "validate_query": (".security", "validate_query"),
    }
    if name in lazy_imports:
        mod_path, attr_name = lazy_imports[name]
        module = import_module(mod_path, __package__)
        if attr_name:
            return getattr(module, attr_name)
        return module
    if name == "init":
        # Beagle initialization — syncs recipes→agents and returns summary
        from .context.recipe_agent_bridge import on_beagle_init

        return on_beagle_init
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
