"""Hardware startup checks — Ramdisk, CPU governor, I/O scheduler.

Verifies that hardware-level optimizations configured in config.toml
are actually active on the system. Logs warnings with remediation
instructions when settings are missing.

Usage:
    from beagle.infrastructure.hardware_checks import check_ramdisk

    if not check_ramdisk():
        logger.warning("Ramdisk not mounted — SSD write savings disabled")
"""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import MISSING, dataclass
from pathlib import Path

from beagle.config._config_path import find_config_toml

logger = logging.getLogger("Beagle.hardware_checks")


@dataclass
class RamdiskStatus:
    """Status of the ramdisk mount."""

    available: bool = False
    path: str = ""
    size_mb: int = 0
    used_mb: float = 0.0
    free_mb: float = 0.0


def check_ramdisk() -> RamdiskStatus:
    """Check if the configured ramdisk is mounted and accessible.

    Reads ramdisk settings from config.toml [hardware]:
      ramdisk_enabled, ramdisk_path, ramdisk_size_mb

    Returns:
        RamdiskStatus with mount details.

    """
    status = RamdiskStatus()

    # Read config — defaults come from the HardwareConfig schema fields so the
    # literal lives in exactly one place (config-defaults CD-1).
    from dataclasses import fields as _fields

    from beagle.config.schema import HardwareConfig

    hw_defaults = {f.name: f.default for f in _fields(HardwareConfig) if f.default is not MISSING}
    ramdisk_path = str(hw_defaults["ramdisk_path"])
    ramdisk_enabled = bool(hw_defaults["ramdisk_enabled"])
    try:
        config_path = find_config_toml()
        if config_path.exists():
            with open(config_path, "rb") as fh:
                data = tomllib.load(fh)
            hw = data.get("hardware", {})
            ramdisk_enabled = bool(hw.get("ramdisk_enabled", hw_defaults["ramdisk_enabled"]))
            ramdisk_path = str(hw.get("ramdisk_path", hw_defaults["ramdisk_path"]))
    except (OSError, tomllib.TOMLDecodeError, TypeError, AttributeError) as exc:
        logger.warning(
            "Cannot read the [hardware] section of config.toml (%s); "
            "using the built-in ramdisk defaults (enabled=%s, path=%s).",
            exc,
            ramdisk_enabled,
            ramdisk_path,
        )

    if not ramdisk_enabled:
        status.available = False
        logger.debug("[Hardware] Ramdisk disabled in config")
        return status

    status.path = ramdisk_path
    rpath = Path(ramdisk_path)

    if not rpath.exists():
        logger.warning(
            f"[Hardware] Ramdisk path {ramdisk_path} does not exist. "
            f"Create it with:\n"
            f"  sudo mkdir -p {ramdisk_path}\n"
            f"  sudo mount -t tmpfs -o size=6G tmpfs {ramdisk_path}\n"
            f"  echo 'tmpfs {ramdisk_path} tmpfs defaults,size=6G 0 0' | sudo tee -a /etc/fstab"
        )
        return status

    # Check if it's actually a tmpfs/ramdisk mount
    try:
        with open("/proc/mounts", encoding="utf-8") as f:
            mounts = f.read()
        is_tmpfs = (
            ramdisk_path in mounts and "tmpfs" in mounts.split(ramdisk_path)[0].split("\n")[-1]
        )
    except (OSError, ValueError, IndexError) as exc:
        logger.warning(
            "Cannot read /proc/mounts to confirm %s is a tmpfs (%s); reporting it as "
            "not a ramdisk.",
            ramdisk_path,
            exc,
        )
        is_tmpfs = False

    if not is_tmpfs:
        logger.warning(
            f"[Hardware] {ramdisk_path} exists but is NOT a tmpfs mount. "
            f"SSD write savings will not be effective. Mount as tmpfs:\n"
            f"  sudo mount -t tmpfs -o size=6G tmpfs {ramdisk_path}"
        )
        status.available = True  # Path exists, just not tmpfs
        return status

    # Get size info
    try:
        stat = os.statvfs(ramdisk_path)
        block_size = stat.f_bsize
        total_blocks = stat.f_blocks
        free_blocks = stat.f_bavail
        status.size_mb = int(total_blocks * block_size / (1024 * 1024))
        status.free_mb = round(free_blocks * block_size / (1024 * 1024), 1)
        status.used_mb = round(status.size_mb - status.free_mb, 1)
    except OSError as exc:
        logger.warning(
            "Cannot stat the ramdisk at %s (%s); size, free and used stay unset "
            "in the reported status.",
            ramdisk_path,
            exc,
        )

    status.available = True
    logger.info(
        f"[Hardware] Ramdisk OK: {ramdisk_path} ({status.size_mb}MB total, {status.free_mb}MB free)"
    )
    return status


def check_io_scheduler() -> dict[str, str]:
    """Check I/O scheduler settings for NVMe and SATA drives.

    Returns:
        Dict mapping device name to scheduler type.

    """
    schedulers: dict[str, str] = {}
    try:
        for block in Path("/sys/block").iterdir():
            sched_file = block / "queue" / "scheduler"
            if sched_file.exists():
                content = sched_file.read_text().strip()
                # Format: "mq-deadline [none] cfq" — [brackets] = active
                active = ""
                for entry in content.split():
                    if entry.startswith("[") and entry.endswith("]"):
                        active = entry[1:-1]
                        break
                schedulers[block.name] = active or content
    except (OSError, ValueError, IndexError) as e:
        logger.warning(f"[Hardware] Cannot read I/O scheduler settings: {e}")

    # Validate recommendations
    for dev, sched in schedulers.items():
        if dev.startswith("nvme") and sched not in ("none", "noop"):
            logger.info(f"[Hardware] NVMe {dev} using {sched} — recommend 'none' for NVMe")
        elif dev.startswith("sd") and sched not in ("mq-deadline", "deadline"):
            logger.info(f"[Hardware] SATA {dev} using {sched} — recommend 'mq-deadline'")

    return schedulers


def get_cpu_governor() -> str:
    """Get the current CPU governor setting.

    Returns:
        Current governor name, or 'unknown' if unavailable.

    """
    try:
        governor_path = Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")
        if governor_path.exists():
            return governor_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        logger.warning("Cannot read the CPU scaling governor (%s); reporting 'unknown'.", exc)
    return "unknown"


# ── Architecture detection (v13.21.5) ────────────────────────────────────────
# Apple Silicon (M1/M2/M3) and other ARM64 hardware have different
# performance characteristics than the Dell OptiPlex x86_64 reference
# machine. This module exposes a simple enum so the rest of the
# codebase can adapt (e.g. choose a different torch index URL, pick a
# different sentence-transformers backend, etc.).


@dataclass(frozen=True)
class HardwareProfile:
    """Detected hardware characteristics relevant to Beagle.

    Attributes:
        arch: One of "x86_64", "arm64", "unknown".
        is_apple_silicon: True if running on Apple M-series.
        is_linux: True if running on Linux.
        is_macos: True if running on macOS.
        machine: The raw ``platform.machine()`` string.
        torch_index_url: The recommended pip index URL for torch
                         on this platform.

    """

    arch: str
    is_apple_silicon: bool
    is_linux: bool
    is_macos: bool
    machine: str
    torch_index_url: str

    @property
    def label(self) -> str:
        """Human-readable label for the profile."""
        if self.is_apple_silicon:
            return f"Apple Silicon ({self.machine})"
        if self.arch == "x86_64":
            return f"x86_64 ({self.machine})"
        if self.arch == "arm64":
            return f"ARM64 ({self.machine})"
        return f"unknown ({self.machine})"


def detect_hardware_profile() -> HardwareProfile:
    """Return the hardware profile for the current process.

    Cheap to call: reads ``platform.machine()`` and ``sys.platform``
    once, then constructs a frozen dataclass.

    The function is safe to call at import time. It does not invoke
    any external commands and does not require root.
    """
    import platform as _platform
    import sys as _sys

    machine = _platform.machine() or "unknown"
    is_linux = _sys.platform.startswith("linux")
    is_macos = _sys.platform == "darwin"
    # Apple Silicon reports as "arm64" via platform.machine() on macOS.
    is_apple_silicon = is_macos and machine == "arm64"
    if machine in ("x86_64", "amd64"):
        arch = "x86_64"
    elif machine in ("arm64", "aarch64"):
        arch = "arm64"
    else:
        arch = "unknown"

    # Choose the torch index URL. CPU-only by default; the data-root
    # constraints/cpu-only.txt file pins the CPU torch version.
    if is_apple_silicon:
        # macOS arm64 ships with PyTorch's official macOS wheels.
        torch_index_url = "https://download.pytorch.org/whl/cpu"
    elif is_linux and arch == "x86_64":
        torch_index_url = "https://download.pytorch.org/whl/cpu"
    else:
        torch_index_url = "https://download.pytorch.org/whl/cpu"

    return HardwareProfile(
        arch=arch,
        is_apple_silicon=is_apple_silicon,
        is_linux=is_linux,
        is_macos=is_macos,
        machine=machine,
        torch_index_url=torch_index_url,
    )


# Module-level singleton — computed once at import time so callers
# don't pay the platform.* cost on every call.
_CURRENT_PROFILE: HardwareProfile | None = None


def get_hardware_profile() -> HardwareProfile:
    """Return the cached hardware profile.

    The first call computes it; subsequent calls return the cached
    value. This is the API the rest of the codebase should use.
    """
    global _CURRENT_PROFILE
    if _CURRENT_PROFILE is None:
        _CURRENT_PROFILE = detect_hardware_profile()
    return _CURRENT_PROFILE


__all__ = [
    "HardwareProfile",
    "RamdiskStatus",
    "check_io_scheduler",
    "check_ramdisk",
    "detect_hardware_profile",
    "get_cpu_governor",
    "get_hardware_profile",
]

__all__ = ["RamdiskStatus", "check_io_scheduler", "check_ramdisk", "get_cpu_governor"]
