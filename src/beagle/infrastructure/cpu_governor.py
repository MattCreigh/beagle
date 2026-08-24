"""CPU Governor Management — Dynamic performance/powersave switching.

Sets CPU governor to 'performance' during active workflows and
'powersave' when idle >5 minutes. Gracefully degrades when
permissions are insufficient.

Usage:
    from beagle.infrastructure.cpu_governor import (
        set_performance_governor, set_powersave_governor
    )
    set_performance_governor()  # Before workflow
    set_powersave_governor()    # After idle period
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

logger = logging.getLogger("Beagle.cpu_governor")

_GOVERNOR_BASE = Path("/sys/devices/system/cpu")
_last_activity_time: float = time.monotonic()
_idle_threshold_seconds: float = 300.0  # 5 minutes


def set_governor(governor: str) -> bool:
    """Set the CPU scaling governor for all CPUs.

    Args:
        governor: One of: performance, powersave, ondemand, conservative, userspace, schedutil.

    Returns:
        True if successfully set on at least one CPU.

    """
    valid_governors = {
        "performance",
        "powersave",
        "ondemand",
        "conservative",
        "userspace",
        "schedutil",
    }
    if governor not in valid_governors:
        logger.error(f"[CPU-Governor] Invalid governor: {governor}")
        return False

    success_count = 0
    if _GOVERNOR_BASE.exists():
        for cpu_dir in sorted(_GOVERNOR_BASE.iterdir()):
            if cpu_dir.name.startswith("cpu") and cpu_dir.name[3:].isdigit():
                governor_file = cpu_dir / "cpufreq" / "scaling_governor"
                if governor_file.exists():
                    try:
                        governor_file.write_text(governor)
                        success_count += 1
                    except PermissionError:
                        logger.debug(
                            f"[CPU-Governor] Permission denied for {cpu_dir.name} — "
                            f"run as root or use: echo {governor} | sudo tee {governor_file}"
                        )
                    except OSError as e:
                        logger.warning(f"[CPU-Governor] Failed for {cpu_dir.name}: {e}")

    if success_count > 0:
        logger.info(f"[CPU-Governor] Set to '{governor}' on {success_count} CPUs")
        return True

    logger.debug(f"[CPU-Governor] Could not set governor to '{governor}' — permissions issue")
    return False


def set_performance_governor() -> bool:
    """Set CPU governor to 'performance' for active workflows."""
    return set_governor("performance")


def set_powersave_governor() -> bool:
    """Set CPU governor to 'powersave' for idle periods."""
    return set_governor("powersave")


def auto_governor(is_active: bool = True) -> str:
    """Automatically select governor based on activity state.

    If system is active, use 'performance'. If idle >5 minutes, use 'powersave'.

    Args:
        is_active: Whether the system is currently active.

    Returns:
        The governor that was set, or 'unknown'.

    """
    global _last_activity_time

    if is_active:
        _last_activity_time = time.monotonic()
        if set_performance_governor():
            return "performance"
        return "unknown"

    # Check idle time
    idle_seconds = time.monotonic() - _last_activity_time
    if idle_seconds > _idle_threshold_seconds:
        if set_powersave_governor():
            return "powersave"
        return "unknown"

    return "performance"


def get_current_governor() -> str:
    """Get the current CPU governor.

    Returns:
        Current governor name, or 'unknown'.

    """
    try:
        governor_file = _GOVERNOR_BASE / "cpu0" / "cpufreq" / "scaling_governor"
        if governor_file.exists():
            return governor_file.read_text().strip()
    except OSError as exc:
        # sysfs is absent in a container and unreadable without the cpufreq
        # driver; both surface as OSError. Anything else here is a defect and
        # must propagate rather than be reported as "unknown".
        logger.warning(
            "Cannot read the CPU scaling governor from %s (%s); reporting 'unknown'.",
            _GOVERNOR_BASE,
            exc,
        )
    return "unknown"


__all__ = [
    "auto_governor",
    "get_current_governor",
    "set_performance_governor",
    "set_powersave_governor",
]
