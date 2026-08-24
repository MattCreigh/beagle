"""Beagle Lifecycle — Graceful self-restart with coordinated checkpoint/shutdown.

Provides checkpoint save/restore, coordinated shutdown, and health-driven
restart triggering so Beagle can cleanly recover from degraded health states.
"""

from .checkpoint import Checkpoint, CheckpointManager, get_checkpoint_manager
from .restart import RestartTrigger, graceful_restart
from .restore import restore_from_checkpoint
from .shutdown import ShutdownCoordinator, get_shutdown_coordinator

__all__ = [
    "Checkpoint",
    "CheckpointManager",
    "RestartTrigger",
    "ShutdownCoordinator",
    "get_checkpoint_manager",
    "get_shutdown_coordinator",
    "graceful_restart",
    "restore_from_checkpoint",
]
