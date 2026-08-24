"""Prometheus exporter for Beagle metrics.

v13.21.5: Optional Prometheus scrape endpoint. The exporter is
*opt-in* — it activates only when the ``prometheus_client`` package
is installed and the ``BEAGLE_PROMETHEUS_PORT`` environment variable
is set (or the ``[observability].prometheus_port`` config is set to
a non-zero value).

Why opt-in rather than always-on?

  - Most Beagle workflows run as a single CLI invocation. Spinning up
    a background HTTP server just to expose a `/metrics` endpoint is
    wasted overhead in that case.
  - Long-running Beagle deployments (daemon mode, TUI, MCP server) do
    benefit from Prometheus scraping — they opt in by setting the
    port.

Usage:

    from beagle.observability.prometheus_exporter import start_prometheus
    start_prometheus(port=9090)  # starts an HTTP server in a daemon thread
    # ... later, on shutdown:
    stop_prometheus()

Or, in a long-running Beagle daemon, set in ``config.toml``::

    [observability]
    prometheus_port = 9090

The exporter reads from the same in-process ``MetricsCollector``
that the rest of Beagle uses, so every metric already recorded is
exported without further instrumentation.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

logger = logging.getLogger("Beagle.observability.prometheus")

# Module-level state. The HTTP server runs in a daemon thread.
_server: Any = None
_thread: threading.Thread | None = None
_lock = threading.Lock()


def _is_enabled() -> bool:
    """Return True if the Prometheus exporter should start.

    The exporter is enabled if either:
      - the ``BEAGLE_PROMETHEUS_PORT`` env var is set to a non-zero
        integer, or
      - the config has ``[observability].prometheus_port`` set.
    """
    if os.environ.get("BEAGLE_PROMETHEUS_PORT"):
        try:
            return int(os.environ["BEAGLE_PROMETHEUS_PORT"]) > 0
        except ValueError:
            return False
    try:
        from beagle.config import get_config

        cfg = get_config()
        port = getattr(getattr(cfg, "observability", None), "prometheus_port", 0)
        return bool(port and port > 0)
    except Exception:  # ruff: ignore[BLE001]  # broad catch intentional — config may not be loaded
        return False


def _resolve_port() -> int:
    """Return the configured port, or 9090 as a sane default."""
    if os.environ.get("BEAGLE_PROMETHEUS_PORT"):
        try:
            return int(os.environ["BEAGLE_PROMETHEUS_PORT"])
        except ValueError as exc:
            logger.warning(
                "BEAGLE_PROMETHEUS_PORT=%r is not an integer (%s); falling through to "
                "the configured port, then to 9090.",
                os.environ["BEAGLE_PROMETHEUS_PORT"],
                exc,
            )
    try:
        from beagle.config import get_config

        cfg = get_config()
        port = getattr(getattr(cfg, "observability", None), "prometheus_port", 0)
        if port and port > 0:
            return int(port)
    except (ImportError, AttributeError, TypeError, ValueError, OSError) as exc:
        logger.warning(
            "Cannot read [observability].prometheus_port from configuration (%s); "
            "using the default port 9090.",
            exc,
        )
    return 9090


def start_prometheus(port: int | None = None) -> bool:
    """Start the Prometheus exporter.

    Args:
        port: TCP port to bind. If None, the value is read from the
              env var / config (see :func:`_resolve_port`).

    Returns:
        True if the exporter was started, False if it was already
        running, or if ``prometheus_client`` is not installed.

    """
    global _server, _thread

    with _lock:
        if _server is not None:
            logger.debug("Prometheus exporter already running")
            return False

        try:
            from prometheus_client import (
                CollectorRegistry,
                start_http_server,
            )
        except ImportError:
            logger.warning(
                "prometheus_client not installed — Prometheus exporter disabled. "
                "Install with: pip install prometheus_client"
            )
            return False

        if port is None:
            port = _resolve_port()

        try:
            registry = CollectorRegistry()
            _server, _thread = start_http_server(port, registry=registry)
            logger.info("Prometheus exporter listening on :%d/metrics", port)
            return True
        except OSError as exc:
            # Port already in use is the most common failure
            logger.warning("Could not bind Prometheus exporter to :%d: %s", port, exc)
            return False


def stop_prometheus() -> None:
    """Stop the Prometheus exporter if running.

    Idempotent: safe to call when the exporter is not running.
    """
    global _server, _thread

    with _lock:
        if _server is None:
            return
        try:
            _server.shutdown()
        except Exception as exc:  # ruff: ignore[BLE001]  # broad catch intentional
            logger.debug("Prometheus shutdown raised: %s", exc)
        finally:
            _server = None
            _thread = None


def is_running() -> bool:
    """Return True if the exporter is currently running."""
    return _server is not None


__all__ = [
    "_is_enabled",
    "is_running",
    "start_prometheus",
    "stop_prometheus",
]
