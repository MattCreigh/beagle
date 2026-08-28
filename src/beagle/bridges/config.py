"""Bridge configuration loader for Beagle LangChain compatibility.

Reads all settings from config.toml [langchain_bridges] section.
No separate config file — single source of truth.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("Beagle.bridges.config")

# Use tomllib for Python 3.11+, fall back to tomli
import tomllib


def _find_config_path() -> Path:
    """Find config.toml using the same resolution as the main config system."""
    # Respect BEAGLE_CONFIG_PATH env var (same as model_resolver)
    env_path = os.environ.get("BEAGLE_CONFIG_PATH")
    if env_path:
        return Path(env_path)

    # Check WORKSPACE_ROOT
    ws = os.environ.get("WORKSPACE_ROOT")
    if ws:
        candidate = Path(ws) / "config.toml"
        if candidate.exists():
            return candidate

    # Fall back to package-adjacent config
    from ..config.paths import get_workspace_root

    return get_workspace_root() / "config.toml"


_config_cache: dict[str, Any] | None = None


def _load_bridges_config() -> dict[str, Any]:
    """Load the [langchain_bridges] section from config.toml.

    Returns:
        Dict of the entire [langchain_bridges] section, or empty dict if missing.

    """
    global _config_cache
    if _config_cache is not None:
        return _config_cache

    config_path = _find_config_path()
    if not config_path.exists():
        logger.debug(f"config.toml not found at {config_path}")
        _config_cache = {}
        return _config_cache

    try:
        with open(config_path, "rb") as f:
            full_config = tomllib.load(f)
        _config_cache = full_config.get("langchain_bridges", {})
        return _config_cache
    except Exception as exc:  # ruff: ignore[BLE001]  # broad catch intentional
        logger.error(f"Failed to load bridges config from {config_path}: {exc}")
        _config_cache = {}
        return _config_cache


def clear_config_cache() -> None:
    """Clear the cached bridges config (useful for testing)."""
    global _config_cache
    _config_cache = None


# ── Dataclass accessors per bridge ────────────────────────────────────────────


@dataclass
class RetrieverConfig:
    """Configuration for Phase 1: Retriever Bridge."""

    enabled: bool = True
    max_hops: int = 1
    top_k: int = 5
    include_relations: bool = True
    mcp_timeout_seconds: int = 30
    cache_results: bool = True
    cache_ttl_seconds: int = 300
    communication_mode: str = "in_process"  # "in_process" | "mcp_stdio" | "orpheus_ipc"
    metadata_mapping: dict[str, str] = field(
        default_factory=lambda: {
            "snippet": "page_content",
            "file_path": "source",
            "ast_node_type": "node_type",
            "start_line": "line_start",
            "end_line": "line_end",
            "relevance_score": "score",
        }
    )


@dataclass
class ToolsConfig:
    """Configuration for Phase 2: Tool Node Adapter."""

    enabled: bool = False
    lazy_import: bool = True
    cache_instances: bool = True
    default_timeout_seconds: int = 30
    fallback_on_error: bool = True
    allowed_directory: str = ""
    allow_network: bool = False
    registry: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class ChatModelConfig:
    """Configuration for Phase 3: BaseChatModel Bridge."""

    enabled: bool = False
    executor_mode: str = "dual"  # "dual" | "langchain_only" | "goose_only"
    implementation: str = "chat_openai"  # "chat_openai" | "custom"
    respect_model_resolver: bool = True
    stream_responses: bool = True
    max_retries: int = 3
    retry_backoff_base_seconds: float = 2.0
    structured_output: bool = False


@dataclass
class LangSmithConfig:
    """Configuration for Phase 4: LangSmith Observability Bridge."""

    enabled: bool = False
    project_name: str = "beagle-workflows"
    api_key_env: str = "LANGCHAIN_API_KEY"
    trace_goose_nodes: bool = True
    trace_langchain_nodes: bool = True
    sample_rate: float = 1.0
    hide_inputs: bool = False
    hide_outputs: bool = False
    span_mapping: dict[str, str] = field(
        default_factory=lambda: {
            "beagle.node.execute": "chain",
            "beagle.node.planning": "chain",
            "beagle.node.verification": "chain",
            "beagle.cvcp.adversarial": "chain",
            "beagle.rag.search": "retriever",
            "beagle.a2a.send": "tool",
            "beagle.a2a.receive": "tool",
            "beagle.goose.subprocess": "chain",
        }
    )


@dataclass
class A2ABridgeConfig:
    """Configuration for Phase 5: A2A Inter-Framework Federation."""

    enabled: bool = False
    port: int = 8420
    bind_address: str = "127.0.0.1"
    tls_enabled: bool = True
    signing_algorithm: str = "ed25519"
    discovery_cache_ttl_seconds: int = 300
    max_concurrent_tasks: int = 4
    max_task_timeout_seconds: int = 300
    key_path: str = "~/.config/goose/a2a_keys"
    require_signatures: bool = True
    allowed_peers: list[str] = field(default_factory=list)
    remote_agents: dict[str, str] = field(default_factory=dict)


@dataclass
class CloudConfig:
    """Configuration for Phase 6: LangGraph Cloud Readiness."""

    enabled: bool = False
    execution_env: str = "local"  # "local" | "cloud"
    checkpoint_mode: str = "auto"  # "auto" | "sqlite" | "postgres"
    rag_proxy_url: str = ""
    rag_proxy_auth: str = "tailscale"
    graph_export_path: str = str(
        (
            Path(os.environ.get("XDG_RUNTIME_DIR", Path.home() / ".beagle" / "runtime"))
            if os.environ.get("XDG_RUNTIME_DIR")
            else (Path.home() / ".beagle" / "runtime")
        )
        / "beagle_graph_export.py"
    )
    orpheus_transport: str = "unix_socket"  # "unix_socket" | "http_sse"


def _section_config(
    section: str,
    dataclass_type: type,
    pop_keys: tuple[str, ...] = (),
) -> tuple[dict, dict]:
    """Extract a config section and split into dataclass fields + extras.

    Generic helper to DRY the repetitive get_*_config() pattern:
      1. Load section from TOML
      2. Pop any non-constructor sub-sections
      3. Filter remaining keys to only those in dataclass fields
      4. Return (filtered_dict, popped_subsections)

    Args:
        section: TOML section name (e.g., "retriever", "a2a").
        dataclass_type: The dataclass to filter keys against.
        pop_keys: Sub-dict keys to pop before filtering
            (e.g., "metadata_mapping", "registry", "auth").

    Returns:
        Tuple of (kwargs_dict, popped_subsections_dict).

    """
    raw = _load_bridges_config().get(section, {})
    popped = {}
    for key in pop_keys:
        val = raw.pop(key, None)
        if val is not None:
            popped[key] = val
    fields = dataclass_type.__dataclass_fields__  # type: ignore[attr-defined]
    filtered = {k: v for k, v in raw.items() if k in fields}
    return filtered, popped


def get_retriever_config() -> RetrieverConfig:
    """Load and return the retriever bridge configuration."""
    kwargs, extras = _section_config("retriever", RetrieverConfig, pop_keys=("metadata_mapping",))
    cfg = RetrieverConfig(**kwargs)
    if "metadata_mapping" in extras:
        cfg.metadata_mapping = extras["metadata_mapping"]
    return cfg


def get_tools_config() -> ToolsConfig:
    """Load and return the tool node adapter configuration."""
    kwargs, extras = _section_config("tools", ToolsConfig, pop_keys=("registry",))
    cfg = ToolsConfig(**kwargs)
    if "registry" in extras:
        cfg.registry = extras["registry"]
    return cfg


def get_chat_model_config() -> ChatModelConfig:
    """Load and return the chat model bridge configuration."""
    raw = _load_bridges_config().get("chat_model", {})
    raw.pop("openai_compat", None)  # Reference-only, not consumed
    fields = ChatModelConfig.__dataclass_fields__
    return ChatModelConfig(**{k: v for k, v in raw.items() if k in fields})


def get_langsmith_config() -> LangSmithConfig:
    """Load and return the LangSmith observability bridge configuration."""
    kwargs, extras = _section_config("langsmith", LangSmithConfig, pop_keys=("span_mapping",))
    cfg = LangSmithConfig(**kwargs)
    if "span_mapping" in extras:
        cfg.span_mapping = extras["span_mapping"]
    return cfg


def get_a2a_config() -> A2ABridgeConfig:
    """Load and return the A2A federation bridge configuration."""
    kwargs, extras = _section_config("a2a", A2ABridgeConfig, pop_keys=("auth", "remote_agents"))
    cfg = A2ABridgeConfig(**kwargs)
    if "auth" in extras:
        auth = extras["auth"]
        cfg.key_path = auth.get("key_path", cfg.key_path)
        cfg.require_signatures = auth.get("require_signatures", cfg.require_signatures)
        cfg.allowed_peers = auth.get("allowed_peers", cfg.allowed_peers)
    if "remote_agents" in extras:
        cfg.remote_agents = extras["remote_agents"]
    return cfg


def get_cloud_config() -> CloudConfig:
    """Load and return the cloud deployment configuration."""
    kwargs, _ = _section_config("cloud", CloudConfig)
    return CloudConfig(**kwargs)


def is_bridges_enabled() -> bool:
    """Check the master switch for all LangChain bridges."""
    return _load_bridges_config().get("enabled", False)
