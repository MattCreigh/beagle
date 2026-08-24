"""Orpheus startup integration — initializes ring buffers and transports.

Wires Orpheus subsystem into the Beagle lifecycle:
1. Creates ring buffer directory if not present
2. Initializes Beagle agent ring topology via OrpheusRingManager
3. Starts OrpheusHTTPTransport if cloud mode is configured
4. Registers cleanup hooks with ShutdownCoordinator

This module is called from the CLI startup path and from
the DAGOrchestrator initialization.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger("Beagle.lifecycle.orpheus")


def _fallback_ring_dir() -> Path:
    """Return the private ring directory used when /run/orpheus is unusable.

    Previously this was a fixed distro-specific path. A constant
    path under a world-writable root is predictable, so any local user can
    pre-create it — or plant a symlink at it — and win the race against
    start-up, taking over the IPC rings. The replacement lives under the
    per-user runtime directory, which the OS keeps private.

    Returns:
        The fallback ring directory. It exists and is private to the user.

    """
    from beagle.config.paths import get_runtime_dir

    fallback = get_runtime_dir() / "orpheus_ring"
    fallback.mkdir(parents=True, exist_ok=True)
    fallback.chmod(0o700)
    return fallback


# Track initialization state
_initialized = False
_transport = None  # OrpheusHTTPTransport instance (if started)
_http_task = None  # asyncio.Task for the HTTP transport (kept alive; RUF006)


def ensure_ring_directory(ring_dir: str | None = None) -> dict[str, Any]:
    """Ensure the Orpheus ring buffer directory exists and is writable.

    Creates the directory if missing, validates permissions, and
    sets the ORPHEUS_RING_DIR environment variable for child
    processes and agent containers.

    Args:
        ring_dir: Path to ring directory. Defaults to config value
                  or /run/orpheus_ring.

    Returns:
        Dict with status and directory info.

    """
    ring_path = Path(ring_dir or os.environ.get("ORPHEUS_RING_DIR", "/run/orpheus_ring"))

    result: dict[str, Any] = {
        "ring_dir": str(ring_path),
        "created": False,
        "writable": False,
        "rings_initialized": False,
    }

    try:
        if not ring_path.exists():
            ring_path.mkdir(parents=True, exist_ok=True)
            # Set permissions: rwx------ (single-user IPC, no group write — audit L15)
            ring_path.chmod(0o700)
            result["created"] = True
            logger.info(f"Orpheus ring directory created: {ring_path}")

        # Verify write access
        test_file = ring_path / ".write_test"
        try:
            test_file.touch()
            test_file.unlink()
            result["writable"] = True
        except OSError:
            fallback = _fallback_ring_dir()
            logger.warning(
                f"Orpheus ring directory not writable: {ring_path}. Falling back to {fallback}"
            )
            result["ring_dir"] = str(fallback)
            result["writable"] = True
            ring_path = fallback
            os.environ["ORPHEUS_RING_DIR"] = str(fallback)

        # Set environment variable for child processes
        os.environ["ORPHEUS_RING_DIR"] = str(ring_path)

    except PermissionError:
        fallback = _fallback_ring_dir()
        logger.warning(
            f"Permission denied creating ring directory: {ring_path}. "
            f"Falling back to {fallback}. "
            "Try running scripts/setup_orpheus_rings.py or use --user mode."
        )
        result["ring_dir"] = str(fallback)
        result["writable"] = True
        os.environ["ORPHEUS_RING_DIR"] = str(fallback)

    return result


def initialize_rings(ring_dir: str | None = None) -> dict[str, Any]:
    """Initialize Orpheus ring buffers for Beagle agent topology.

    Creates the full ring topology:
    - orchestrator→planner, planner→executor, executor→verifier,
      verifier→synthesizer, synthesizer→orchestrator, heartbeat

    Args:
        ring_dir: Override ring directory path.

    Returns:
        Dict with ring creation results.

    """
    # Ensure directory first
    dir_result = ensure_ring_directory(ring_dir)

    # Create the ring topology — proprietary implementation from the
    # separately licensed beagle-orpheus wheel; absent, degrade to a no-op.
    try:
        try:
            from beagle_orpheus.compat import create_rings  # type: ignore[import-not-found]
        except ImportError:
            from beagle.infrastructure._orpheus_optional import create_rings

        ring_result = create_rings()
        ring_result["directory"] = dir_result
        logger.info(
            f"Orpheus rings initialized: "
            f"{len(ring_result.get('success', []))} created, "
            f"{len(ring_result.get('failed', []))} failed"
        )
        return ring_result
    except (OSError, ImportError, RuntimeError) as err:
        logger.error(f"Failed to initialize Orpheus rings: {err}")
        return {
            "success": [],
            "failed": [{"error": str(err)}],
            "directory": dir_result,
        }


def start_orpheus(transport: str = "unix_socket") -> dict[str, Any]:
    """Start Orpheus subsystem (rings + optional HTTP transport).

    Called during Beagle startup to initialize all Orpheus components.

    Args:
        transport: Transport mode — "unix_socket" or "http_sse".

    Returns:
        Dict with startup status for each component.

    """
    global _initialized, _transport

    if _initialized:
        logger.debug("Orpheus already initialized — skipping")
        return {"status": "already_initialized"}

    result: dict[str, Any] = {"status": "initialized", "components": {}}

    # 1. Initialize ring buffers
    ring_result = initialize_rings()
    result["components"]["rings"] = ring_result

    # 2. Start HTTP transport if configured
    if transport == "http_sse":
        try:
            import asyncio

            from beagle.bridges.orpheus_http_transport import (
                OrpheusHTTPTransport,
            )

            _transport = OrpheusHTTPTransport()
            # Start in background — don't block startup. Keep a module-level
            # reference so the task is not garbage-collected mid-flight (RUF006).
            global _http_task
            _http_task = asyncio.create_task(_transport.start())
            result["components"]["http_transport"] = {"status": "starting", "port": 8430}
            logger.info("Orpheus HTTP transport starting on port 8430")
        except (ImportError, ModuleNotFoundError, OSError) as err:  # Narrowed from bare Exception
            logger.warning(f"Failed to start Orpheus HTTP transport: {err}")
            result["components"]["http_transport"] = {"status": "failed", "error": str(err)}
    else:
        result["components"]["http_transport"] = {"status": "disabled", "mode": transport}

    # 3. Verify OrpheusClient connectivity
    try:
        try:
            from beagle_orpheus.compat import get_orpheus_client  # type: ignore[import-not-found]
        except ImportError:
            from beagle.infrastructure._orpheus_optional import get_orpheus_client

        client = get_orpheus_client()
        result["components"]["client"] = {
            "connected": client._connected,
        }
    except (ImportError, ModuleNotFoundError, RuntimeError) as err:  # Narrowed from bare Exception
        logger.debug(f"Orpheus client not available: {err}")
        result["components"]["client"] = {"connected": False, "error": str(err)}

    _initialized = True
    logger.info("Orpheus subsystem initialized")
    return result


async def stop_orpheus() -> dict[str, Any]:
    """Stop Orpheus subsystem gracefully.

    Stops the HTTP transport and cleans up ring buffer resources.
    Called during coordinated shutdown.
    """
    global _initialized, _transport

    result: dict[str, Any] = {"status": "stopped", "components": {}}

    # Stop HTTP transport
    if _transport is not None:
        try:
            await _transport.stop()
            result["components"]["http_transport"] = {"status": "stopped"}
        except (RuntimeError, OSError, AttributeError) as err:  # Narrowed from bare Exception
            logger.warning(f"Error stopping Orpheus HTTP transport: {err}")
            result["components"]["http_transport"] = {"status": "error", "error": str(err)}
        _transport = None

    # Clean up ring buffers
    try:
        try:
            from beagle_orpheus.compat import cleanup_rings  # type: ignore[import-not-found]
        except ImportError:
            from beagle.infrastructure._orpheus_optional import (
                cleanup_rings,  # type: ignore[attr-defined]
            )

        cleanup_result = cleanup_rings()
        result["components"]["rings"] = cleanup_result
    except (ImportError, OSError) as err:
        logger.debug(f"Ring cleanup skipped: {err}")
        result["components"]["rings"] = {"status": "skipped"}

    _initialized = False
    logger.info("Orpheus subsystem stopped")
    return result


def is_orpheus_initialized() -> bool:
    """Check if Orpheus subsystem has been initialized."""
    return _initialized


def reset_orpheus() -> None:
    """Reset Orpheus state (for testing)."""
    global _initialized, _transport
    _initialized = False
    _transport = None
