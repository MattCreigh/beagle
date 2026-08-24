"""Environment variable overrides for Goose Agentic Workflow configuration.

Applies environment variable overrides to configuration objects,
with per-section methods for maintainability.
"""

from __future__ import annotations

import contextlib
import os

from .schema import WorkflowConfig

# Module-level logger for env override methods
_cfg_log = __import__("logging").getLogger("Beagle.config")


def _apply_goose_env(config: WorkflowConfig) -> None:
    """Apply GOOSE_BIN, GOOSE_MODEL, GOOSE_PROVIDER, GOOSE_HOST overrides."""
    if "GOOSE_BIN" in os.environ:
        config.goose.binary_path = os.environ["GOOSE_BIN"]
    if "GOOSE_MODEL" in os.environ:
        config.goose.default_model = os.environ["GOOSE_MODEL"]
    if "GOOSE_PROVIDER" in os.environ:
        config.goose.provider = os.environ["GOOSE_PROVIDER"]
    if "GOOSE_HOST" in os.environ:
        config.goose.host = os.environ["GOOSE_HOST"]


def _apply_budget_env(config: WorkflowConfig) -> None:
    """Apply BEAGLE_BUDGET_USD override."""
    if "BEAGLE_BUDGET_USD" in os.environ:
        try:
            val = float(os.environ["BEAGLE_BUDGET_USD"])
            if val < 0:
                raise ValueError("Budget cannot be negative")
            config.budget.default_usd = val
        except ValueError:
            _cfg_log.warning(
                f"Invalid BEAGLE_BUDGET_USD value: {os.environ['BEAGLE_BUDGET_USD']!r}, using default"
            )


def _apply_cache_env(config: WorkflowConfig) -> None:
    """Apply BEAGLE_CACHE_ENABLED override."""
    if "BEAGLE_CACHE_ENABLED" in os.environ:
        config.cache.enabled = os.environ["BEAGLE_CACHE_ENABLED"].lower() in (
            "true",
            "1",
            "yes",
        )


def _apply_logging_env(config: WorkflowConfig) -> None:
    """Apply BEAGLE_LOG_LEVEL and BEAGLE_LOG_JSON overrides."""
    if "BEAGLE_LOG_LEVEL" in os.environ:
        _level = os.environ["BEAGLE_LOG_LEVEL"].upper()
        if _level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            config.logging.level = _level
        else:
            _cfg_log.warning(f"Invalid BEAGLE_LOG_LEVEL: {_level!r}, using default")
    if "BEAGLE_LOG_JSON" in os.environ:
        config.logging.json_format = os.environ["BEAGLE_LOG_JSON"].lower() in (
            "true",
            "1",
            "yes",
        )


def _apply_memory_env(config: WorkflowConfig) -> None:
    """Apply BEAGLE_MEMORY_INDEX_TOKEN_BUDGET and BEAGLE_MEMORY_INDEX_PRUNE_STRATEGY."""
    if "BEAGLE_MEMORY_INDEX_TOKEN_BUDGET" in os.environ:
        try:
            val = int(os.environ["BEAGLE_MEMORY_INDEX_TOKEN_BUDGET"])
            if val < 500:
                _cfg_log.warning(
                    f"BEAGLE_MEMORY_INDEX_TOKEN_BUDGET={val} is below minimum (500). Clamping."
                )
                val = 500
            config.memory.index_token_budget = val
        except ValueError:
            _cfg_log.warning(
                f"Invalid BEAGLE_MEMORY_INDEX_TOKEN_BUDGET: "
                f"{os.environ['BEAGLE_MEMORY_INDEX_TOKEN_BUDGET']!r}, using default"
            )
    if "BEAGLE_MEMORY_INDEX_PRUNE_STRATEGY" in os.environ:
        strategy = os.environ["BEAGLE_MEMORY_INDEX_PRUNE_STRATEGY"].lower()
        valid_strategies = ("oldest_first", "relevance_weighted", "hybrid")
        if strategy in valid_strategies:
            config.memory.index_prune_strategy = strategy
        else:
            _cfg_log.warning(
                f"Invalid BEAGLE_MEMORY_INDEX_PRUNE_STRATEGY: {strategy!r}. "
                f"Valid values: {valid_strategies}. Using default."
            )


def _apply_pool_env(config: WorkflowConfig) -> None:
    """Apply BEAGLE_POOL_WORKERS override."""
    if "BEAGLE_POOL_WORKERS" in os.environ:
        with contextlib.suppress(ValueError):
            val = int(os.environ["BEAGLE_POOL_WORKERS"])
            if val > 0:
                config.pool.max_workers = val


def _apply_timeout_env(config: WorkflowConfig) -> None:
    """Apply BEAGLE_PLANNING_TIMEOUT and BEAGLE_EXECUTION_TIMEOUT overrides."""
    if "BEAGLE_PLANNING_TIMEOUT" in os.environ:
        with contextlib.suppress(ValueError):
            config.node_timeout.planning_seconds = int(os.environ["BEAGLE_PLANNING_TIMEOUT"])
    if "BEAGLE_EXECUTION_TIMEOUT" in os.environ:
        with contextlib.suppress(ValueError):
            config.node_timeout.execution_seconds = int(os.environ["BEAGLE_EXECUTION_TIMEOUT"])


def _apply_context_env(config: WorkflowConfig) -> None:
    """Apply BEAGLE_CONTEXT_* and GOOSE_CONTEXT_MAX overrides."""
    if "BEAGLE_CONTEXT_WARNING" in os.environ:
        with contextlib.suppress(ValueError):
            config.context_threshold.warning = float(os.environ["BEAGLE_CONTEXT_WARNING"])
    if "BEAGLE_CONTEXT_PRE_COMPACT" in os.environ:
        with contextlib.suppress(ValueError):
            config.context_threshold.pre_compact = float(os.environ["BEAGLE_CONTEXT_PRE_COMPACT"])
    if "BEAGLE_CONTEXT_COMPACT" in os.environ:
        with contextlib.suppress(ValueError):
            config.context_threshold.compact = float(os.environ["BEAGLE_CONTEXT_COMPACT"])
    if "BEAGLE_CONTEXT_HARD_COMPACT" in os.environ:
        with contextlib.suppress(ValueError):
            config.context_threshold.hard_compact = float(os.environ["BEAGLE_CONTEXT_HARD_COMPACT"])
    if "BEAGLE_CONTEXT_CRITICAL" in os.environ:
        with contextlib.suppress(ValueError):
            config.context_threshold.critical = float(os.environ["BEAGLE_CONTEXT_CRITICAL"])
    if "BEAGLE_CONTEXT_WATCHDOG_SECONDS" in os.environ:
        with contextlib.suppress(ValueError):
            config.context_threshold.watchdog_seconds = int(
                os.environ["BEAGLE_CONTEXT_WATCHDOG_SECONDS"]
            )
    if "GOOSE_CONTEXT_MAX" in os.environ:
        with contextlib.suppress(ValueError):
            config.context_threshold.max_tokens = int(os.environ["GOOSE_CONTEXT_MAX"])
    # GOOSE_AUTO_COMPACT_THRESHOLD — the canonical env-var knob for compaction
    # Takes precedence over all other compact thresholds (per doctrine)
    if "GOOSE_AUTO_COMPACT_THRESHOLD" in os.environ:
        with contextlib.suppress(ValueError):
            val = float(os.environ["GOOSE_AUTO_COMPACT_THRESHOLD"])
            if 0.0 < val < 1.0:
                config.context_threshold.compact = val


def _apply_mcp_env(config: WorkflowConfig) -> None:
    """Apply BEAGLE_KNOWLEDGE_DIR, BEAGLE_MCP_TRANSPORT overrides."""
    if "BEAGLE_KNOWLEDGE_DIR" in os.environ:
        config.mcp.knowledge_dir = os.environ["BEAGLE_KNOWLEDGE_DIR"]
    if "BEAGLE_MCP_TRANSPORT" in os.environ:
        transport = os.environ["BEAGLE_MCP_TRANSPORT"].lower()
        if transport != "stdio":
            _cfg_log.warning(f"Blocked non-stdio MCP transport override: {transport}")
        else:
            config.mcp.transport = transport


def _apply_behavior_env(config: WorkflowConfig) -> None:
    """Apply BEAGLE_AUTO_CREATE_MISSING_FILES and BEAGLE_SAFE_FILE_OPS."""
    if "BEAGLE_AUTO_CREATE_MISSING_FILES" in os.environ:
        val = os.environ["BEAGLE_AUTO_CREATE_MISSING_FILES"].lower() in (
            "true",
            "1",
            "yes",
        )
        config.behavior.auto_create_missing_files = val
    if "BEAGLE_SAFE_FILE_OPS" in os.environ:
        val = os.environ["BEAGLE_SAFE_FILE_OPS"].lower() in (
            "true",
            "1",
            "yes",
        )
        config.behavior.safe_file_operations = val


def _apply_mcp_auth_env(config: WorkflowConfig) -> None:
    """Apply BEAGLE_MCP_AUTH_ENABLED, BEAGLE_MCP_TOKEN, REQUIRE_HTTPS, BIND_ADDRESS."""
    if "BEAGLE_MCP_AUTH_ENABLED" in os.environ:
        config.mcp_auth.enabled = os.environ["BEAGLE_MCP_AUTH_ENABLED"].lower() in (
            "true",
            "1",
            "yes",
        )
    if (
        "BEAGLE_MCP_TOKEN" in os.environ
        and os.environ["BEAGLE_MCP_TOKEN"] not in config.mcp_auth.tokens
    ):
        config.mcp_auth.tokens.append(os.environ["BEAGLE_MCP_TOKEN"])
    if "BEAGLE_MCP_REQUIRE_HTTPS" in os.environ:
        config.mcp_auth.require_https = os.environ["BEAGLE_MCP_REQUIRE_HTTPS"].lower() in (
            "true",
            "1",
            "yes",
        )
    if "BEAGLE_MCP_BIND_ADDRESS" in os.environ:
        addr = os.environ["BEAGLE_MCP_BIND_ADDRESS"]
        # v13.21 (F3 remediation): Validate bind address to prevent
        # accidental exposure on 0.0.0.0 or public interfaces. Only
        # loopback addresses are allowed unless the operator explicitly
        # sets BEAGLE_MCP_ALLOW_EXTERNAL=true — mirrors the transport
        # safety gate at lines 151-158.
        allowed_loopback = {"127.0.0.1", "::1", "localhost"}
        if addr not in allowed_loopback:
            if os.environ.get("BEAGLE_MCP_ALLOW_EXTERNAL", "").lower() not in (
                "true",
                "1",
                "yes",
            ):
                _cfg_log.warning(
                    f"Blocked non-loopback MCP bind address: {addr}. "
                    f"Set BEAGLE_MCP_ALLOW_EXTERNAL=true to allow."
                )
            else:
                config.mcp_auth.bind_address = addr
        else:
            config.mcp_auth.bind_address = addr


def apply_env_overrides(config: WorkflowConfig) -> WorkflowConfig:
    """Apply environment variable overrides to config.

    Environment variables take precedence over config file.
    Delegates to per-section methods for maintainability.

    Args:
        config: Base configuration

    Returns:
        Configuration with env overrides applied

    """
    _apply_goose_env(config)
    _apply_budget_env(config)
    _apply_cache_env(config)
    _apply_logging_env(config)
    _apply_memory_env(config)
    _apply_pool_env(config)
    _apply_timeout_env(config)
    _apply_context_env(config)
    _apply_mcp_env(config)
    _apply_behavior_env(config)
    _apply_mcp_auth_env(config)
    return config
