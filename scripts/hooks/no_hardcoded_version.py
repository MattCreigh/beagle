#!/usr/bin/env python3
"""Reject hardcoded version strings of the form ``# v1`` / ``# v2`` ...

Beagle's policy: the **single source of truth** for the package version
is ``beagle/__version__`` (re-exported from
``beagle.constants.PACKAGE_VERSION``). New code must
not introduce its own version markers. Audit-trail comments in existing
code (of the form ``# v13.x: <justification>``) are exempt and are
crucial for the change-history doctrine; this hook only blocks *new*
short-form markers.

Usage (as a pre-commit hook):
    python3 scripts/hooks/no_hardcoded_version.py <files...>

Exit code 0 → all files clean. Exit code 1 → at least one violation.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Short-form version markers that we forbid in *new* code. These are
# 1- or 2-digit "# vN" or "# vN.M" annotations that drift away from
# the canonical __version__ and confuse readers.
# Beagle's policy: the package version lives in one place —
# ``beagle/__version__``. New code must not introduce
# its own version markers. Audit-trail comments of the form
# ``# v13.21.3: <justification>`` are part of the change-history
# doctrine and are exempt.
#
# This regex catches the *short-form* markers that historically caused
# drift (e.g. ``# v1:``, ``# v2.5``). The rule is:
#
#   * The major version is NOT a recognised release of Beagle. Recognised
#     release majors are: 0 (the v0.3.0 line, before the project hit
#     1.0) and 10+ (the v13.x line, post-1.0).
#   * So a marker like ``# v3.0:`` or ``# v5.0.1:`` (major=3 or 5) is
#     a forbidden drift; ``# v0.3.0:`` and ``# v13.21.5:`` are
#     legitimate audit trails.
#
# Examples (status: ❌ = reject, ✅ = allow):
#   ❌ "# v1: this is forbidden"
#   ❌ "# v2.5 fix"
#   ❌ "# v3.0: ..."
#   ❌ "# v5.0.1: ..."
#   ✅ "# v0.3.0: ..."   (recognised v0.3.x line)
#   ✅ "# v13.21.3: ..."
#   ✅ "# v13.21 (R2.3): ..."
#   ✅ "# v13.4 fix: ..."
#   ✅ "# v10.0: ..."
_SHORT_VERSION_RE = re.compile(
    r"#[ \t]+v(\d{1,2})(?:\.(\d{1,2}))?(?:\.(\d{1,2}))?(?![.\d])",
    re.MULTILINE,
)

# Beagle's recognised release majors. Audit-trail comments referencing
# these versions are legitimate history and must not be rejected.
#
# v1.0.0 (2026-07-29) added major 1: the golden-master release reset the
# version from 13.22.3 to 1.0.0, so `# v1.0.0: <justification>` is now a
# legitimate audit-trail marker. Majors 2-9 stay forbidden — the project
# has never shipped them, so such a marker is still drift.
_RECOGNISED_MAJORS = frozenset({0, 1, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20})

# Files that are allowed to mention version strings (SSOT, audit
# trails, build config, this hook itself).
EXEMPT_FILES = {
    # v1.0.0: repo-root src-layout — the package dir is `src/beagle/`.
    # These paths were stale after the move, which un-exempted
    # the version SSOT itself and made the hook reject its own source.
    "src/beagle/__init__.py",
    "src/beagle/constants.py",
    "pyproject.toml",
    "README.md",
    "CHANGELOG.md",
    "scripts/hooks/no_hardcoded_version.py",
    ".pre-commit-config.yaml",
    "LICENSE",
    "CONTRIBUTING.md",
}

# Directories whose `# v13.x: <justification>` audit comments are
# exempt because they document the change history of security- and
# doctrine-enforcing code.
EXEMPT_DIRS = (
    "tests/",
    "src/beagle/security/",
    "src/beagle/secrets_loader.py",
    "src/beagle/cost_tracker.py",
    "src/beagle/blocks/python_blocks/io.py",
    "src/beagle/cli/",
    "src/beagle/lifecycle/",
    "src/beagle/validation/",
    "src/beagle/infrastructure/mcp_rag_server.py",
    "src/beagle/infrastructure/mcp_utility_server.py",
    "src/beagle/bridges/tool_node.py",
    "src/beagle/tracking/database.py",
)


def _is_exempt(path: Path) -> bool:
    """Return True if the file is on the exempt list."""
    p = str(path).replace("\\", "/")
    if p in EXEMPT_FILES:
        return True
    return any(p.startswith(d) for d in EXEMPT_DIRS)


def _is_short_marker(m: re.Match[str]) -> bool:
    """Return True if the match is a *forbidden* short-form marker.

    A marker is allowed if its major version is in
    :data:`_RECOGNISED_MAJORS` (i.e., a real Beagle release line). All
    other markers — ``v1``, ``v2.5``, ``v5.0.1``, ``v9.x`` — are
    forbidden drift and should be removed or rewritten.
    """
    major_s = m.group(1)
    try:
        major = int(major_s)
    except (TypeError, ValueError):
        return False
    return major not in _RECOGNISED_MAJORS


def scan(path: Path) -> list[tuple[int, str]]:
    """Return ``(line_number, line)`` for every short-form version marker."""
    if _is_exempt(path):
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []
    if not text:
        return []
    findings: list[tuple[int, str]] = []
    for i, line in enumerate(text.splitlines(), 1):
        m = _SHORT_VERSION_RE.search(line)
        if m and _is_short_marker(m):
            findings.append((i, line.rstrip()))
    return findings


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        # pre-commit passes filenames as positional args.
        print("no-hardcoded-version-string: no files given", file=sys.stderr)
        return 0
    violations: list[tuple[Path, int, str]] = []
    for arg in argv[1:]:
        p = Path(arg)
        if not p.is_file():
            continue
        for line_no, line in scan(p):
            violations.append((p, line_no, line))
    if not violations:
        return 0
    print("no-hardcoded-version-string: REJECTED", file=sys.stderr)
    print("", file=sys.stderr)
    print(
        "Beagle policy forbids short-form version markers like `# v1` / `# v2`.",
        file=sys.stderr,
    )
    print(
        "Use beagle/__version__ (PACKAGE_VERSION) as the SSOT.",
        file=sys.stderr,
    )
    print(
        "If this is a legitimate audit-trail comment, move the file under an",
        file=sys.stderr,
    )
    print("exempt path (tests/, src/beagle/security/) or rename to a full version", file=sys.stderr)
    print("(e.g. `# v13.21.3: was ...`).", file=sys.stderr)
    print("", file=sys.stderr)
    for p, n, line in violations[:50]:
        print(f"  {p}:{n}: {line}", file=sys.stderr)
    if len(violations) > 50:
        print(f"  ... and {len(violations) - 50} more", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
