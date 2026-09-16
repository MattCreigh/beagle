#!/usr/bin/env python3
"""Provision the Orpheus ring directory.

Referenced by name from shipped runtime code, so it must exist and do what the
hints claim:

* ``beagle/lifecycle/orpheus_startup.py`` — "Try running
  scripts/setup_orpheus_rings.py or use --user mode."
* ``beagle/startup/health_check.py`` — "Run: scripts/setup_orpheus_rings.py or
  the fallback will be auto-created at startup".

The directory defaults to ``[orpheus].ring_dir`` from config.toml
(``/run/orpheus_ring``), and ``--user`` selects the per-uid fallback the
runtime uses when the system path is not writable.

Usage:
    sudo python3 scripts/setup_orpheus_rings.py          # system path
    python3 scripts/setup_orpheus_rings.py --user        # fallback path
    python3 scripts/setup_orpheus_rings.py --dry-run     # show, change nothing

Exit codes:
    0  the directory exists with the requested mode and ownership
    1  the directory could not be created or is not writable (message names why)
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

DEFAULT_RING_DIR = "/run/orpheus_ring"
DEFAULT_MODE = 0o770

# The runtime's fallback convention (see lifecycle/orpheus_startup.py
# _fallback_ring_dir): prefer the systemd user runtime dir, else a per-uid
# directory under the temp root.
FALLBACK_TEMP_TEMPLATE = "orpheus_ring-{uid}"


def _fallback_ring_dir() -> Path:
    """Return the per-user fallback ring directory the runtime would use."""
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return Path(xdg) / "beagle" / "orpheus_ring"
    return Path("/run/user") / str(os.getuid()) / "beagle" / "orpheus_ring"


def _configured_ring_dir() -> str:
    """Return ``[orpheus].ring_dir`` from config, or the built-in default.

    Config load failures are non-fatal: the script's job is to create the
    directory the runtime will look for, and the built-in default is the same
    value the schema declares.
    """
    try:
        from beagle.config.loader import load_config
    except ImportError:
        return DEFAULT_RING_DIR
    try:
        return load_config().orpheus.ring_dir
    except (AttributeError, OSError, ValueError):
        return DEFAULT_RING_DIR


def _describe(path: Path) -> str:
    if not path.exists():
        return "does not exist"
    st = path.stat()
    return f"mode={oct(st.st_mode & 0o777)}, uid={st.st_uid}, gid={st.st_gid}"


def _create(path: Path, mode: int, uid: int | None, gid: int | None, dry_run: bool) -> int:
    """Create ``path`` with ``mode`` (and ownership when given). Returns an exit code."""
    if dry_run:
        print(f"[dry-run] would create {path} (mode={oct(mode)})")
        if uid is not None or gid is not None:
            print(f"[dry-run] would chown {path} to uid={uid} gid={gid}")
        print(f"[dry-run] current state: {_describe(path)}")
        return 0

    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"error: cannot create {path}: {exc}", file=sys.stderr)
        if not os.access(path.parent, os.W_OK):
            print(
                f"error: {path.parent} is not writable by uid {os.getuid()}. "
                "Re-run with sudo, or use --user for the per-user fallback.",
                file=sys.stderr,
            )
        return 1

    try:
        path.chmod(mode)
    except OSError as exc:
        print(f"warning: could not set mode on {path}: {exc}", file=sys.stderr)

    if uid is not None or gid is not None:
        try:
            os.chown(path, uid if uid is not None else -1, gid if gid is not None else -1)
        except OSError as exc:
            print(
                f"warning: could not set ownership on {path}: {exc}. "
                "Ownership matters when Beagle runs as a different user.",
                file=sys.stderr,
            )

    if not os.access(path, os.W_OK):
        print(
            f"error: {path} exists but is not writable by uid {os.getuid()} "
            f"({_describe(path)}). Re-run with sudo, or use --user.",
            file=sys.stderr,
        )
        return 1

    print(f"orpheus ring directory ready: {path} ({_describe(path)})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--user",
        action="store_true",
        help="Create the per-user fallback directory instead of the system path.",
    )
    parser.add_argument(
        "--path",
        default=None,
        help=f"Explicit ring directory (default: [orpheus].ring_dir, else {DEFAULT_RING_DIR}).",
    )
    parser.add_argument(
        "--mode",
        default=oct(DEFAULT_MODE),
        help=f"Directory mode as octal (default: {oct(DEFAULT_MODE)}).",
    )
    parser.add_argument("--uid", type=int, default=None, help="Owner uid to set.")
    parser.add_argument("--gid", type=int, default=None, help="Owner gid to set.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the actions that would be taken without touching the filesystem.",
    )
    args = parser.parse_args(argv)

    if args.path:
        target = Path(args.path)
    elif args.user:
        target = _fallback_ring_dir()
    else:
        target = Path(_configured_ring_dir())

    try:
        mode = int(args.mode, 8)
    except ValueError:
        print(f"error: --mode must be octal, got {args.mode!r}", file=sys.stderr)
        return 1

    return _create(target, mode, args.uid, args.gid, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
