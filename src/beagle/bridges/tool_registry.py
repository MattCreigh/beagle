"""TOML-driven tool registry for LangChain Tool Node Adapter.

Phase 2 of the LangChain Ecosystem Compatibility Plan.
Reads tool configurations from config.toml [langchain_bridges.tools.registry]
and provides lazy-imported, cached tool instances.

Each tool entry specifies:
  - class_path: Dotted import path to a LangChain BaseTool subclass
  - auth_method: How to load credentials ("env" | "secrets_yaml" | "none")
  - auth_key: Env var name or secrets.yaml key for auth
  - enabled: Whether the tool is active
  - config: Per-tool configuration dict (e.g., allowed_directory)
"""

from __future__ import annotations

import importlib
import logging
import threading
from typing import Any

from ..secrets_loader import load_secret
from .config import get_tools_config

logger = logging.getLogger("Beagle.bridges.tool_registry")


class ToolRegistry:
    """TOML-driven LangChain tool registry with lazy import and caching.

    Thread-safe singleton pattern — all tool instances are cached
    on first use and reused thereafter.

    Usage:
        registry = ToolRegistry()
        tool = registry.get_tool("file_system")
        result = await tool.ainvoke({"file_path": "/tmp/test.py"})
    """

    _instance: ToolRegistry | None = None
    _lock = threading.Lock()

    def __new__(cls) -> ToolRegistry:
        """Singleton — return the shared registry instance."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False  # type: ignore[has-type]
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:  # type: ignore[has-type]
            return
        self._tools: dict[str, Any] = {}  # Cached tool instances
        self._tool_classes: dict[str, type] = {}  # Imported but not instantiated
        self._tool_lock = threading.Lock()
        self._initialized = True

    def _resolve_auth(self, auth_method: str, auth_key: str) -> dict[str, str]:
        """Resolve authentication credentials based on the configured method.

        Args:
            auth_method: One of "env", "secrets_yaml", "none".
            auth_key: The key/env var name for the credential.

        Returns:
            Dict of kwargs to pass to the tool constructor.

        """
        if auth_method == "none":
            return {}
        elif auth_method == "env":
            value = load_secret(auth_key, allow_file=False)
            if not value:
                # Also try secrets.yaml
                value = load_secret(auth_key, allow_env=True, allow_file=True)
            if value:
                return {auth_key.lower(): value}
            logger.warning(f"Auth key '{auth_key}' not found (method={auth_method})")
            return {}
        elif auth_method == "secrets_yaml":
            value = load_secret(auth_key, allow_env=False, allow_file=True)
            if value:
                return {auth_key.lower(): value}
            logger.warning(f"Auth key '{auth_key}' not found in secrets.yaml")
            return {}
        else:
            logger.warning(f"Unknown auth_method '{auth_method}' for key '{auth_key}'")
            return {}

    def _import_tool_class(self, class_path: str) -> type | None:
        """Lazy-import a tool class from its dotted path.

        Args:
            class_path: Dotted import path
                (e.g., "langchain_community.tools.ReadFileTool")

        Returns:
            The imported class, or None if import fails.

        """
        try:
            module_path, class_name = class_path.rsplit(".", 1)
            module = importlib.import_module(module_path)
            return getattr(module, class_name)  # type: ignore[no-any-return]
        except ImportError as exc:
            logger.error(f"Failed to import tool '{class_path}': {exc}")
            return None
        except AttributeError as exc:
            logger.error(f"Class '{class_name}' not found in module '{module_path}': {exc}")
            return None

    def get_tool(self, tool_name: str) -> Any | None:
        """Get a tool instance by name.

        Lazy-imports the tool class, instantiates with auth/config,
        and caches the instance for reuse.

        Args:
            tool_name: Name of the tool as defined in config.toml registry.

        Returns:
            A LangChain BaseTool instance, or None if unavailable.

        """
        config = get_tools_config()

        if tool_name in self._tools:
            return self._tools[tool_name]

        # Check if tool is defined in registry
        tool_cfg = config.registry.get(tool_name)
        if tool_cfg is None:
            logger.warning(f"Tool '{tool_name}' not found in registry")
            return None

        # Check if enabled
        if not tool_cfg.get("enabled", False):
            logger.info(f"Tool '{tool_name}' is disabled in config")
            return None

        class_path = tool_cfg.get("class_path", "")
        if not class_path:
            logger.error(f"Tool '{tool_name}' has no class_path configured")
            return None

        # Lazy import
        tool_cls = self._import_tool_class(class_path)
        if tool_cls is None:
            return None

        # Resolve auth
        auth_method = tool_cfg.get("auth_method", "none")
        auth_key = tool_cfg.get("auth_key", "")
        auth_kwargs = self._resolve_auth(auth_method, auth_key)

        # Per-tool config
        tool_config = tool_cfg.get("config", {})

        # Merge kwargs
        init_kwargs = {**auth_kwargs, **tool_config}

        try:
            instance = tool_cls(**init_kwargs)
        except Exception as exc:  # ruff: ignore[BLE001]  # broad catch intentional
            logger.error(
                f"Failed to instantiate tool '{tool_name}' "
                f"(class={class_path}): {type(exc).__name__}: {exc}"
            )
            if self._cache_instances:  # type: ignore[attr-defined]
                # Store None to avoid repeated instantiation attempts
                with self._tool_lock:
                    self._tools[tool_name] = None
            return None

        # Cache instance
        with self._tool_lock:
            self._tools[tool_name] = instance

        logger.info(f"Tool '{tool_name}' instantiated and cached ({class_path})")
        return instance

    def get_available_tools(self) -> list[str]:
        """List all enabled tool names from the registry."""
        config = get_tools_config()
        return [name for name, cfg in config.registry.items() if cfg.get("enabled", False)]

    def is_tool_available(self, tool_name: str) -> bool:
        """Check if a tool is both configured and importable."""
        config = get_tools_config()
        if tool_name not in config.registry:
            return False
        return config.registry[tool_name].get("enabled", False)  # type: ignore[no-any-return]

    def reset(self) -> None:
        """Clear all cached tool instances (useful for testing)."""
        with self._tool_lock:
            self._tools.clear()
            self._tool_classes.clear()


def get_tool_registry() -> ToolRegistry:
    """Get the global tool registry singleton."""
    return ToolRegistry()


def register_tool(
    name: str,
    class_path: str,
    auth_method: str = "none",
    auth_key: str = "",
    enabled: bool = True,
    **extra_config: Any,
) -> None:
    """Programmatically register a tool without editing config.toml.

    Args:
        name: Tool name (used in YAML `tool_name` field).
        class_path: Dotted import path to the BaseTool class.
        auth_method: How to load credentials.
        auth_key: Env var / secrets.yaml key.
        enabled: Whether the tool is active.
        **extra_config: Additional tool configuration.

    """
    config = get_tools_config()
    config.registry[name] = {
        "class_path": class_path,
        "auth_method": auth_method,
        "auth_key": auth_key,
        "enabled": enabled,
        **extra_config,
    }
    # Invalidate any cached instance for this tool
    registry = get_tool_registry()
    with registry._tool_lock:
        registry._tools.pop(name, None)
    logger.info(f"Registered tool '{name}' -> {class_path}")
