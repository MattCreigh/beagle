#!/usr/bin/env python3
"""Reject hardcoded host-specific paths in the Beagle source tree.

A hardcoded host path (``/home/``, ``/opt/``, ``/mnt/``) couples Beagle to
the machine it runs on. Beagle must be portable: a clean clone must build
and run without editing a resolver or a test to point at this host.

This script scans the given paths for those literals and fails (exit 1)
on any hit that the allowlist does not cover. Every allowlist entry names
the specific file and line and states why the literal is legitimate.

Usage (CLI):
    python3 scripts/check_host_paths.py tests/ src/beagle/config
    python3 scripts/check_host_paths.py --selftest

Exit code 0 → all scanned files are clean (or every hit is allowlisted).
Exit code 1 → at least one non-allowlisted host path was found.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Host-specific path prefixes we treat as coupling. ``/mnt/`` covers
# data-drive mounts (ramdisk staging, 4TB corpus) that differ per host.
_HOST_RE = ("/home/", "/opt/", "/mnt/")

# Directories and file suffixes that are build artifacts or VCS noise and
# are never part of the reviewed source.
_SKIP_DIRS = frozenset({"__pycache__", ".git", ".venv", "node_modules", "dist", "build"})
_SKIP_SUFFIXES = frozenset({".pyc", ".pyo", ".so", ".o", ".egg-info"})

# v1.2.0 (RG-6): the scanner's own source and its allowlist are full of host
# paths by design (the allowlist documents them; the scanner's docstring and
# selftest plant them). Scanning them would be self-referential — the gate
# would flag the very file that defines the gate. Skip them explicitly.
# Also skip the auto-generated renderer artifacts (.goosehints, .goose/
# project.json) — they are emitted by `beagle render-hints` and document the
# host environment by design; they are not reviewed source.
_SKIP_FILES = frozenset(
    {
        "check_host_paths.py",
        "host_path_allowlist.txt",
        ".goosehints",
        "project.json",
    }
)

# Path to the allowlist. It is a plain text file with lines of the form::
#
#     path/to/file.py:NNN: reason text
#
# The ``:NNN`` is the exact line number of the allowed hit. A path without
# a line number allowlists the whole file. Lines starting with ``#`` are
# comments.
ALLOWLIST_FILE = Path(__file__).resolve().parent.parent / "scripts" / "host_path_allowlist.txt"

# v1.2.0 (QA-2, BGL-067): the default scan roots.  .agents is included so
# the hook wiring (templates and rendered files) is scanned, not just the
# Python source.  The selftest asserts this list contains .agents.
_DEFAULT_SCAN_ROOTS = ["src/beagle", "tests", "scripts", ".agents"]

# Sentinel for "the whole file is allowlisted". Distinguished from a file
# that is simply absent from the allowlist (where ``get`` returns ``None``).
_WHOLE_FILE = "whole-file"


def _load_allowlist(path: Path = ALLOWLIST_FILE) -> dict[str, set[int] | str]:
    """Read the allowlist into {relative_path: set(line_numbers) | "whole-file"}.

    Args:
        path: Path to the allowlist file.

    Returns:
        Mapping of relative path to either a set of allowlisted line
        numbers, or the ``_WHOLE_FILE`` sentinel meaning the entire file
        is allowlisted. A file absent from the mapping is NOT allowlisted.
    """
    allowlist: dict[str, set[int] | str] = {}
    if not path.is_file():
        return allowlist
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        path_part = line.split(": ", 1)[0]
        if not path_part:
            continue
        if ":" in path_part:
            rel, _, lineno = path_part.rpartition(":")
            if lineno.isdigit():
                allowlist.setdefault(rel.strip(), set()).add(int(lineno))
                continue
        # No line number → whole file allowlisted.
        allowlist[path_part.strip()] = _WHOLE_FILE
    return allowlist


def _is_gitignored(path: Path) -> bool:
    """Return True when ``path`` is ignored by the repository's .gitignore.

    Gitignored files are build artefacts (e.g. a rendered ``hooks.json``
    that names an absolute path) and are not reviewed source.  Scanning them
    would flag the very file the build produces.

    Args:
        path: File or directory to test.

    Returns:
        True when the path is ignored by git, else False.
    """
    import shutil
    import subprocess

    git = shutil.which("git")
    if git is None:
        return False
    try:
        result = subprocess.run(
            [git, "check-ignore", "-q", str(path)],
            cwd=Path(__file__).resolve().parent.parent,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _scan(path: Path, allowlist: dict[str, set[int] | str]) -> list[str]:
    """Scan a file or directory tree for non-allowlisted host paths.

    Args:
        path: File or directory to scan.
        allowlist: Parsed allowlist map.

    Returns:
        List of human-readable violation strings (empty when clean).
    """
    violations: list[str] = []
    targets: list[Path] = []
    if path.is_file():
        targets = [path]
    elif path.is_dir():
        for dirpath, dirnames, filenames in path.walk():
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for fname in filenames:
                if Path(fname).suffix in _SKIP_SUFFIXES:
                    continue
                targets.append(Path(dirpath) / fname)
    else:
        return violations

    for target in targets:
        if _is_gitignored(target):
            continue
        rel = target.as_posix()
        if target.name in _SKIP_FILES:
            continue
        file_allow = allowlist.get(rel)
        try:
            lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            # Unreadable (binary, permission) — not a host-path violation.
            continue
        for lineno, text in enumerate(lines, start=1):
            if not any(h in text for h in _HOST_RE):
                continue
            if file_allow == _WHOLE_FILE:
                continue  # whole file allowlisted
            if isinstance(file_allow, set) and lineno in file_allow:
                continue
            violations.append(f"{rel}:{lineno}: {text.strip()}")
    return violations


def _selftest() -> int:
    """Prove the scanner catches a planted violation.

    v1.2.0 (RG-6, BGL-007): the selftest now also plants a violation in a
    subpackage that was OUTSIDE the old default scan roots
    (``src/beagle/config``, ``src/beagle/style_guides``). This asserts the
    widened default roots actually cover the whole ``src/beagle`` tree — a
    green badge over an unscanned subpackage is the exact defect class this
    package corrects.

    Returns:
        Exit code 1 when a planted violation is NOT caught (broken
        scanner or a scan-root blind spot), else 0.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "__pycache__").mkdir()
        (root / "__pycache__" / "skip.py").write_text(
            "/opt/beagle/beagle_venv/bin/python", encoding="utf-8"
        )
        (root / "hit.py").write_text("PROJ = '/home/example-user/proj'\n", encoding="utf-8")
        found = _scan(root, {})
        if not any("hit.py:1" in v for v in found):
            print("SELFTEST FAILED: planted violation was not caught", file=sys.stderr)
            return 1
        if any("__pycache__" in v for v in found):
            print("SELFTEST FAILED: __pycache__ was scanned", file=sys.stderr)
            return 1

        # Coverage assertion: plant a violation in a subpackage that the OLD
        # default roots did not scan (e.g. src/beagle/infrastructure/). The
        # widened default roots must find it.
        (root / "infrastructure").mkdir()
        (root / "infrastructure" / "deep.py").write_text(
            "VENV = '/opt/beagle/beagle_venv/bin/python'\n", encoding="utf-8"
        )
        found_deep = _scan(root / "infrastructure", {})
        if not any("deep.py:1" in v for v in found_deep):
            print(
                "SELFTEST FAILED: violation in a subpackage outside the old default "
                "roots was not caught",
                file=sys.stderr,
            )
            return 1

        # v1.2.0 (QA-2, BGL-067): the default scan roots must include
        # .agents, or the hook wiring is a blind spot.  Assert it directly
        # so a future narrowing of the defaults fails the selftest.
        if ".agents" not in _DEFAULT_SCAN_ROOTS:
            print(
                "SELFTEST FAILED: .agents is not among the default scan roots",
                file=sys.stderr,
            )
            return 1
    print("selftest ok")
    return 0


def main() -> int:
    """Entry point.

    Returns:
        Exit code for the process.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", help="Files or directories to scan")
    parser.add_argument("--selftest", action="store_true", help="Run the scanner self-test")
    parser.add_argument(
        "--allowlist",
        default=str(ALLOWLIST_FILE),
        help="Path to the allowlist file (default: %(default)s)",
    )
    args = parser.parse_args()

    if args.selftest:
        return _selftest()

    if not args.paths:
        # pre-commit invokes with pass_filenames: false (no positional
        # args), so default to the canonical reviewed source roots.
        # v1.2.0 (RG-6, BGL-007): the gate previously scanned 3 of 45
        # subpackages under src/beagle/ — a green badge over an unscanned
        # tree. Widen to the whole reviewed source tree.
        # v1.2.0 (QA-2, BGL-067): add .agents so the hook wiring is scanned.
        args.paths = _DEFAULT_SCAN_ROOTS

    allowlist = _load_allowlist(Path(args.allowlist))

    violations: list[str] = []
    for p in args.paths:
        violations.extend(_scan(Path(p), allowlist))

    if violations:
        print(f"host-path violations ({len(violations)}):")
        for v in sorted(set(violations)):
            print(f"  {v}")
        print("\nFix each literal or add it to scripts/host_path_allowlist.txt with a reason.")
        return 1
    print("no host-path violations")
    return 0


if __name__ == "__main__":
    sys.exit(main())
