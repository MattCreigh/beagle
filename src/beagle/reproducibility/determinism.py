"""Deterministic mode for reproducible workflow execution.

When enabled, replaces all non-deterministic sources:
- uuid.uuid4() → uuid.uuid5(BEAGLE_NAMESPACE, seed + context)
- time.time() → fixed epoch + monotonic counter
- hash() → hashlib.sha256
- temperature → forced to 0.0
- random jitter → fixed delay
"""

from __future__ import annotations

import hashlib
import logging
import time
import uuid

logger = logging.getLogger("Beagle.reproducibility")

# Fixed UUID namespace for deterministic UUID generation
BEAGLE_NAMESPACE: uuid.UUID = uuid.UUID("bea91000-0000-4000-8000-000000000000")

# Module-level state
_deterministic_mode: bool = False
_deterministic_seed: str = ""
_timestamp_counter: int = 0
_DETERMINISTIC_EPOCH: float = 1700000000.0  # Fixed epoch for deterministic timestamps
# Fixed default seed so `set_deterministic_mode(True)` with no seed is still
# reproducible across runs. Deriving the seed from uuid.uuid4() (the previous
# behaviour) made the "deterministic" mode non-deterministic — every run got a
# different seed, so replays could not reproduce the original execution.
_DEFAULT_SEED: str = "beagle-deterministic-default"


def set_deterministic_mode(enabled: bool, seed: str = "") -> None:
    """Enable/disable deterministic mode globally.

    When enabled:
    - All uuid.uuid4() calls use uuid.uuid5(NAMESPACE, seed + counter)
    - All time.time() calls in state return a seed-based timestamp
    - Temperature is forced to 0.0
    - Random jitter is replaced with fixed delay
    - TurboQuant uses hashlib instead of hash()
    """
    global _deterministic_mode, _deterministic_seed, _timestamp_counter
    _deterministic_mode = enabled
    if enabled:
        _deterministic_seed = seed or _DEFAULT_SEED
        _timestamp_counter = 0
        logger.info(f"Deterministic mode ENABLED with seed={_deterministic_seed!r}")
    else:
        logger.info("Deterministic mode DISABLED")


def is_deterministic() -> bool:
    """Check if deterministic mode is active."""
    return _deterministic_mode


def get_seed() -> str:
    """Get the current deterministic seed."""
    return _deterministic_seed


def deterministic_uuid(context: str = "") -> str:
    """Generate a deterministic UUID based on seed + context.

    Uses uuid.uuid5(BEAGLE_NAMESPACE, seed + context) for reproducibility.
    Falls back to uuid.uuid4() if deterministic mode is off.
    """
    if not _deterministic_mode:
        return str(uuid.uuid4())

    name = f"{_deterministic_seed}:{context}" if context else _deterministic_seed
    return str(uuid.uuid5(BEAGLE_NAMESPACE, name))


def deterministic_timestamp() -> float:
    """Return a deterministic timestamp in deterministic mode.

    In normal mode: returns time.time()
    In deterministic mode: returns a fixed epoch + monotonic counter
    so timestamps are consistent across replays.
    """
    global _timestamp_counter

    if not _deterministic_mode:
        return time.time()

    _timestamp_counter += 1
    return _DETERMINISTIC_EPOCH + _timestamp_counter * 0.001


def deterministic_hash(data: bytes) -> int:
    """Compute a deterministic hash using hashlib.sha256.

    Replaces Python's built-in hash() which is non-deterministic
    across Python sessions (PYTHONHASHSEED randomization).
    """
    h = hashlib.sha256(data).digest()
    return int.from_bytes(h[:8], "little")


def deterministic_temperature(requested: float) -> float:
    """Force temperature to 0.0 when deterministic mode is active."""
    if _deterministic_mode:
        return 0.0
    return requested


__all__ = [
    "BEAGLE_NAMESPACE",
    "deterministic_hash",
    "deterministic_temperature",
    "deterministic_timestamp",
    "deterministic_uuid",
    "get_seed",
    "is_deterministic",
    "set_deterministic_mode",
]
