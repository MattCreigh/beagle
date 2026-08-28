"""eBPF Tracing Backend Stub.

Provides a stub implementation for the eBPF tracing backend config option.
When `tracing.backend = "ebpf"`, this module logs a notice that eBPF is not
yet implemented and falls back to OpenTelemetry (the current backend).

Usage:
    from beagle.infrastructure.ebpf_tracer import is_ebpf_enabled, init_ebpf_tracer

    if is_ebpf_enabled():
        init_ebpf_tracer()  # Logs stub message, falls back to OpenTelemetry
"""

from __future__ import annotations

import logging
import tomllib

from beagle.config._config_path import find_config_toml

logger = logging.getLogger("Beagle.ebpf_tracer")


def is_ebpf_enabled() -> bool:
    """Check if eBPF tracing is configured as the backend.

    Reads from config.toml [tracing] backend setting.
    """
    try:
        config_path = find_config_toml()
        if config_path.exists():
            with open(config_path, "rb") as f:
                data = tomllib.load(f)
            return data.get("tracing", {}).get("backend") == "ebpf"
    except (OSError, tomllib.TOMLDecodeError, TypeError, AttributeError) as exc:
        logger.warning(
            "Cannot read [tracing].backend from config.toml (%s); "
            "treating the eBPF backend as disabled.",
            exc,
        )
    return False


def is_ebpf_stub_mode() -> bool:
    """Check if eBPF stub mode is enabled (logs stub message only)."""
    try:
        config_path = find_config_toml()
        if config_path.exists():
            with open(config_path, "rb") as f:
                data = tomllib.load(f)
            return data.get("tracing", {}).get("ebpf_stub", True)
    except (OSError, tomllib.TOMLDecodeError, TypeError, AttributeError) as exc:
        logger.warning(
            "Cannot read [tracing].ebpf_stub from config.toml (%s); "
            "defaulting to stub mode (no real eBPF attachment).",
            exc,
        )
    return True


def init_ebpf_tracer() -> bool:
    """Initialize the eBPF tracing backend (stub).

    Currently logs a stub message and falls back to OpenTelemetry.
    This function provides the config hook for future eBPF integration.

    Returns:
        False (eBPF not yet implemented).

    """
    logger.info(
        "[eBPF] eBPF tracing backend is NOT YET IMPLEMENTED. "
        "Falling back to OpenTelemetry. "
        "Set [tracing].backend = 'opentelemetry' to suppress this notice."
    )
    return False


__all__ = ["init_ebpf_tracer", "is_ebpf_enabled", "is_ebpf_stub_mode"]
