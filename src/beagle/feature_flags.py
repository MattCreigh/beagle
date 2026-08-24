"""File-based feature flags from config.toml.

Backwards compatible: all flags default to false.
Read once at startup from [feature_flags] section.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import Any

logger = logging.getLogger("Beagle.feature_flags")


class FeatureFlag(StrEnum):
    ENABLE_GRAPH_RAG = "enable_graph_rag"
    ENABLE_MULTI_TENANCY = "enable_multi_tenancy"
    ENABLE_GENAI_METRICS = "enable_genai_metrics"
    ENABLE_DEGRADATION_MANAGER = "enable_degradation_manager"


class FeatureFlags:
    """Runtime feature flag manager.

    Loads from config.toml [feature_flags] section:
    ```toml
    [feature_flags]
    enable_graph_rag = false
    enable_multi_tenancy = false
    enable_genai_metrics = true
    enable_degradation_manager = false
    ```
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._config = config or self._load_from_file()

    @staticmethod
    def _load_from_file() -> dict[str, Any]:
        try:
            import tomllib

            # v1.1.1 (S9): config.toml is detached to the canonical config
            # root; resolve it via the single canonical resolver, not a
            # brittle relative walk.
            from beagle.config._config_path import find_config_toml

            config_path = find_config_toml()
            if config_path.exists():
                with open(config_path, "rb") as f:
                    data = tomllib.load(f)
                flags = data.get("feature_flags", {})
                return dict(flags) if isinstance(flags, dict) else {}
        except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
            logger.debug(f"Could not load feature flags from config.toml: {e}")
        return {}

    def is_enabled(self, flag: FeatureFlag) -> bool:
        """Check if a feature flag is enabled."""
        value = self._config.get(flag.value, False)
        return bool(value)

    def enable(self, flag: FeatureFlag) -> None:
        """Enable a feature flag at runtime."""
        self._config[flag.value] = True

    def disable(self, flag: FeatureFlag) -> None:
        """Disable a feature flag at runtime."""
        self._config[flag.value] = False

    def as_dict(self) -> dict[str, bool]:
        """Return all flags as a dict."""
        return {flag.value: self.is_enabled(flag) for flag in FeatureFlag}


# Global singleton for runtime access
_flags: FeatureFlags | None = None


def get_flags() -> FeatureFlags:
    """Get the global feature flags instance."""
    global _flags
    if _flags is None:
        _flags = FeatureFlags()
    return _flags
