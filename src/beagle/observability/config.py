"""Observability configuration — unified from TelemetryConfig.

Consolidates tracing, metrics, and logging configuration into a single
dataclass loaded from config.toml ``[observability]`` section.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

logger = logging.getLogger("Beagle.observability")

_config: ObservabilityConfig | None = None


@dataclass
class ObservabilityConfig:
    """Unified observability configuration.

    Loaded from ``config.toml`` ``[observability]`` section or environment
    variables with ``BEAGLE_OTEL_`` prefix.
    """

    # Service identification
    service_name: str = "beagle"
    service_version: str = "13.8.1"
    deployment_environment: str = "development"

    # Tracing
    tracing_enabled: bool = True
    trace_exporter: str = "console"  # "console", "otlp", "jaeger", "none"
    otlp_endpoint: str = "http://localhost:4317"
    trace_sample_rate: float = 1.0

    # Metrics
    metrics_enabled: bool = True
    metrics_exporter: str = "internal"  # "internal", "prometheus", "otlp"
    prometheus_port: int = 9090

    # Structured logging
    structured_logging: bool = True
    log_format: str = "json"  # "json", "console", "plain"
    log_level: str = "INFO"

    # GenAI-specific
    genai_spans_enabled: bool = True
    capture_prompts: bool = False  # Security: don't log prompts by default

    # Resource attributes (merged into OTel resource)
    resource_attributes: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> ObservabilityConfig:
        """Build config from environment variables."""
        return cls(
            service_name=os.getenv("BEAGLE_OTEL_SERVICE_NAME", "beagle"),
            service_version=os.getenv("BEAGLE_OTEL_SERVICE_VERSION", "13.8.1"),
            deployment_environment=os.getenv("BEAGLE_ENV", "development"),
            tracing_enabled=os.getenv("BEAGLE_OTEL_TRACING", "true").lower() == "true",
            trace_exporter=os.getenv("BEAGLE_OTEL_TRACE_EXPORTER", "console"),
            otlp_endpoint=os.getenv("BEAGLE_OTEL_ENDPOINT", "http://localhost:4317"),
            metrics_enabled=os.getenv("BEAGLE_OTEL_METRICS", "true").lower() == "true",
            structured_logging=os.getenv("BEAGLE_STRUCTURED_LOGGING", "true").lower() == "true",
            log_format=os.getenv("BEAGLE_LOG_FORMAT", "json"),
            log_level=os.getenv("BEAGLE_LOG_LEVEL", "INFO"),
            genai_spans_enabled=os.getenv("BEAGLE_GENAI_SPANS", "true").lower() == "true",
            capture_prompts=os.getenv("BEAGLE_CAPTURE_PROMPTS", "false").lower() == "true",
        )

    @classmethod
    def from_config_toml(cls) -> ObservabilityConfig:
        """Load from config.toml [observability] section, env vars override."""
        base = cls.from_env()
        try:
            from beagle.config.config import load_config

            config = load_config()
            obs = {}
            if hasattr(config, "get"):
                obs = config.get("observability", {})
            elif hasattr(config, "observability"):
                obs = vars(config.observability)

            if obs:
                for k, v in obs.items():
                    if hasattr(base, k) and not os.getenv(f"BEAGLE_OTEL_{k.upper()}"):
                        setattr(base, k, v)
        except (ImportError, AttributeError, TypeError, KeyError, OSError, ValueError) as exc:
            logger.warning(
                "Cannot apply the [observability] configuration overrides (%s); "
                "telemetry runs with environment and built-in defaults only.",
                exc,
            )
        return base


def configure_observability(
    config: ObservabilityConfig | None = None,
) -> ObservabilityConfig:
    """Initialize the observability stack.

    Call once at startup. Initializes tracing, metrics, and logging
    based on configuration.

    Args:
        config: Explicit config, or auto-loaded from env/config.toml.

    Returns:
        The active ObservabilityConfig.

    """
    global _config
    _config = config or ObservabilityConfig.from_config_toml()

    # Initialize tracing
    if _config.tracing_enabled:
        from beagle.observability.tracing import init_tracing

        init_tracing(
            service_name=_config.service_name,
            service_version=_config.service_version,
            export_to_console=(_config.trace_exporter == "console"),
            otlp_endpoint=(_config.otlp_endpoint if _config.trace_exporter == "otlp" else None),
        )

    # Initialize metrics
    if _config.metrics_enabled:
        from beagle.observability.metrics import (
            get_metrics_collector,
        )

        get_metrics_collector()  # Lazy init

    logger.info(
        f"Observability initialized: tracing={_config.tracing_enabled} "
        f"metrics={_config.metrics_enabled} "
        f"structured_logging={_config.structured_logging} "
        f"genai_spans={_config.genai_spans_enabled}"
    )
    return _config


def get_observability_config() -> ObservabilityConfig:
    """Get the current observability config (auto-creates if needed)."""
    global _config
    if _config is None:
        _config = ObservabilityConfig.from_config_toml()
    return _config
