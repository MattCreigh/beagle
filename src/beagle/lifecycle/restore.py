"""Startup restoration from checkpoint after Beagle self-restart.

Called at startup to check for and restore a checkpoint file left by
a previous process that executed the graceful restart cycle.  Restores
health monitor state, circuit breaker states, and publishes a
CheckpointRestored event.
"""

from __future__ import annotations

import logging
import time

from .checkpoint import Checkpoint, get_checkpoint_manager

logger = logging.getLogger("Beagle.lifecycle")


async def restore_from_checkpoint(
    checkpoint_id: str | None = None,
    *,
    skip_errors: bool = False,
) -> bool:
    """Called at startup to check for and restore a checkpoint.

    Args:
        checkpoint_id: Optional specific snapshot ID to restore. If None,
            the latest checkpoint file is restored.
        skip_errors: If True, ignore corrupted snapshots instead of failing.

    Returns:
        True if a checkpoint was restored, False otherwise.

    Steps:
    1. Check if checkpoint exists
    2. Load and validate checkpoint
    3. Restore health monitor state
    4. Restore circuit breaker states
    5. Publish CheckpointRestored event
    6. Clear checkpoint file
    7. Log restoration summary

    """
    mgr = get_checkpoint_manager()

    # 1. Check if checkpoint exists
    if checkpoint_id is None and not mgr.exists():
        logger.debug("No checkpoint found — clean startup")
        return False

    # 2. Load and validate checkpoint
    checkpoint: Checkpoint
    if checkpoint_id is not None:
        # Load a specific snapshot by ID
        candidates = [c for c in mgr.list_checkpoints() if str(c.timestamp) == checkpoint_id]
        if not candidates:
            if skip_errors:
                logger.warning("Checkpoint %s not found — skipping", checkpoint_id)
                return False
            raise FileNotFoundError(f"Checkpoint {checkpoint_id} not found")
        checkpoint = candidates[0]
    else:
        loaded = mgr.load()
        if loaded is None:
            if skip_errors:
                logger.warning("Checkpoint exists but failed to load — clean startup")
                mgr.clear()
                return False
            raise RuntimeError("Checkpoint exists but failed to load")
        checkpoint = loaded

    logger.info(
        "Checkpoint found: version=%s reason=%s restart_count=%d age=%.0fs",
        checkpoint.version,
        checkpoint.restart_reason,
        checkpoint.restart_count,
        # wall-clock-ok: compares against a persisted timestamp
        time.time() - checkpoint.timestamp,
    )

    # 3. Restore health monitor state
    _restore_health_state(checkpoint)

    # 4. Restore circuit breaker states
    _restore_circuit_states(checkpoint)

    # 5. Publish CheckpointRestored event
    _publish_checkpoint_restored(checkpoint)

    # 6. Clear checkpoint file
    mgr.clear()

    # 7. Log restoration summary
    age = (
        # wall-clock-ok: compares against a persisted timestamp
        time.time() - checkpoint.timestamp
    )
    logger.info(
        "Checkpoint restored: reason=%s age=%.1fs restart_count=%d "
        "health_state=%s health_score=%.2f circuits=%d",
        checkpoint.restart_reason,
        age,
        checkpoint.restart_count,
        checkpoint.health_previous_state,
        checkpoint.health_previous_score,
        len(checkpoint.circuit_states),
    )
    return True


def _restore_health_state(checkpoint: Checkpoint) -> None:
    """Restore health monitor state from checkpoint.

    Placeholder: actual health-monitor integration is owned by the daemon
    health module.  The checkpoint object carries the persisted values.
    """
    logger.debug("Restoring health state: %s", checkpoint.health_previous_state)


def _restore_circuit_states(checkpoint: Checkpoint) -> None:
    """Restore circuit breaker states from checkpoint.

    Placeholder: actual circuit-breaker integration is owned by the rate
    limiter module.  The checkpoint object carries the persisted values.
    """
    logger.debug("Restoring circuit states: %d entries", len(checkpoint.circuit_states))


def _publish_checkpoint_restored(checkpoint: object) -> None:
    """Publish a CheckpointRestored event.

    Placeholder: event-bus integration is owned by the events module.
    """
    logger.debug("Publishing checkpoint restored event")
