#!/usr/bin/env python3
"""Install the Firecracker MicroVM components Beagle's sandbox requires.

Referenced by name from shipped runtime code, so it must exist and do what the
hints claim. Two hints name a flag, so those flags are honoured:

* ``sandbox.py`` ``_check_availability`` — "Install with:
  scripts/setup_firecracker.py"; missing kernel → "Run: scripts/setup_firecracker.py
  --kernel"; missing rootfs → "Run: scripts/setup_firecracker.py --rootfs".
* ``config/schema.py`` — "Disabled by default — enable after running
  scripts/setup_firecracker.py."
* ``startup/health_check.py`` — "Install with: scripts/setup_firecracker.py".

Paths come from ``[sandbox.microvm]`` in config.toml
(``firecracker_binary``, ``kernel_image``, ``rootfs_image``) so the installed
locations are exactly the ones the sandbox checks at runtime.

What it does NOT do: enable the MicroVM in config. The fail-closed floor keeps
``sandbox_microvm.enabled = false`` and ``allow_fallback = false`` as shipped
values; flipping those is an operator decision, not an installer side effect.

Usage:
    sudo python3 scripts/setup_firecracker.py             # all components
    sudo python3 scripts/setup_firecracker.py --binary    # firecracker only
    sudo python3 scripts/setup_firecracker.py --kernel    # kernel image only
    sudo python3 scripts/setup_firecracker.py --rootfs    # rootfs image only
    python3 scripts/setup_firecracker.py --dry-run        # show, change nothing

Exit codes:
    0  every requested component is present
    1  a component is missing (the message names it and the path expected)
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

# Pinned upstream release assets. Bump deliberately: a floating "latest" would
# make an installed host unreproducible and silently change the guest kernel.
FIRECRACKER_VERSION = "v1.10.1"
KERNEL_URL = (
    "https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/v1.10/x86_64/vmlinux-5.10.223"
)
ALPINE_ROOTFS_URL = (
    "https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/v1.10/x86_64/"
    "ubuntu-22.04.ext4"
)

_DOWNLOAD_TIMEOUT_S = 120.0


def _defaults() -> tuple[str, str, str]:
    """Return (binary, kernel, rootfs) paths from config, else schema defaults."""
    try:
        from beagle.config.loader import load_config

        cfg = load_config().sandbox_microvm
        return cfg.firecracker_binary, cfg.kernel_image, cfg.rootfs_image
    except (ImportError, AttributeError, OSError, ValueError):
        return "/usr/local/bin/firecracker", "/usr/share/beagle/vmlinux", (
            "/usr/share/beagle/rootfs.ext4"
        )


def _download(url: str, dest: Path, dry_run: bool) -> int:
    """Download ``url`` to ``dest``. Returns an exit code."""
    if dry_run:
        print(f"[dry-run] would download {url} -> {dest}")
        return 0

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    print(f"downloading {url}")
    try:
        with urllib.request.urlopen(url, timeout=_DOWNLOAD_TIMEOUT_S) as response:
            with tmp.open("wb") as fh:
                shutil.copyfileobj(response, fh)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        tmp.unlink(missing_ok=True)
        print(f"error: download failed for {url}: {exc}", file=sys.stderr)
        return 1

    tmp.replace(dest)
    print(f"installed {dest} ({dest.stat().st_size:,} bytes)")
    return 0


def _install_binary(target: str, dry_run: bool) -> int:
    """Install the firecracker binary, preferring the distro package manager."""
    dest = Path(target)
    existing = shutil.which("firecracker") or (str(dest) if dest.exists() else None)
    if existing:
        print(f"firecracker already present: {existing}")
        return 0

    if dry_run:
        print(f"[dry-run] would install firecracker to {dest}")
        return 0

    arch = "x86_64" if os.uname().machine in ("x86_64", "amd64") else "aarch64"
    url = (
        f"https://github.com/firecracker-microvm/firecracker/releases/download/"
        f"{FIRECRACKER_VERSION}/firecracker-{FIRECRACKER_VERSION}-{arch}.tgz"
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(
        "Firecracker is not installed. Fetch it from the release above and place the\n"
        f"binary at {dest} (mode 0755), or install the distro package. Expected asset:\n"
        f"  {url}"
    )
    print(
        f"error: firecracker binary not found at {dest} and not on PATH.",
        file=sys.stderr,
    )
    return 1


def _install_kernel(target: str, dry_run: bool) -> int:
    dest = Path(target)
    if dest.is_file():
        print(f"kernel already present: {dest}")
        return 0
    return _download(KERNEL_URL, dest, dry_run)


def _install_rootfs(target: str, dry_run: bool) -> int:
    dest = Path(target)
    if dest.is_file():
        print(f"rootfs already present: {dest}")
        return 0
    return _download(ALPINE_ROOTFS_URL, dest, dry_run)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    binary_default, kernel_default, rootfs_default = _defaults()
    parser.add_argument("--binary", action="store_true", help="Install firecracker only.")
    parser.add_argument("--kernel", action="store_true", help="Install the kernel image only.")
    parser.add_argument("--rootfs", action="store_true", help="Install the rootfs image only.")
    parser.add_argument("--firecracker-path", default=binary_default, help="Firecracker binary path.")
    parser.add_argument("--kernel-path", default=kernel_default, help="Kernel image path.")
    parser.add_argument("--rootfs-path", default=rootfs_default, help="Rootfs image path.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the actions that would be taken without changing the host.",
    )
    args = parser.parse_args(argv)

    all_requested = not (args.binary or args.kernel or args.rootfs)
    failures: list[str] = []

    if all_requested or args.binary:
        if _install_binary(args.firecracker_path, args.dry_run) != 0:
            failures.append(f"firecracker binary ({args.firecracker_path})")
    if all_requested or args.kernel:
        if _install_kernel(args.kernel_path, args.dry_run) != 0:
            failures.append(f"kernel image ({args.kernel_path})")
    if all_requested or args.rootfs:
        if _install_rootfs(args.rootfs_path, args.dry_run) != 0:
            failures.append(f"rootfs image ({args.rootfs_path})")

    if failures:
        print(
            "error: missing components: " + ", ".join(failures) + ". "
            "The MicroVM sandbox stays unavailable until each is present; "
            "it does NOT silently degrade unless allow_fallback=true.",
            file=sys.stderr,
        )
        return 1

    if args.dry_run:
        print("Dry run complete — nothing was changed. Re-run without --dry-run to install.")
        return 0

    print(
        "Firecracker components ready. To enable the MicroVM sandbox, set\n"
        "  [sandbox.microvm] enabled = true\n"
        "in config.toml. allow_fallback stays false unless you deliberately accept\n"
        "losing KVM isolation."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
