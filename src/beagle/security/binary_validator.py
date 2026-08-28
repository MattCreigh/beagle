"""Goose binary validation — a leaf module (stdlib only, no intra-package imports).

SP-7 (beagle-spotless-phase2): ``validate_goose_binary`` was defined in
``security/validation.py`` and used by ``security/firewall.py``. Because
``validation`` lazily imports ``firewall.semantic_firewall`` (the LLM query
guard) and ``firewall`` imported ``validation.validate_goose_binary``, the two
formed a cycle. Extracting this pure, stdlib-only helper here lets ``firewall``
depend on the leaf without re-entering ``validation``.

``security.validation`` re-exports ``validate_goose_binary`` for backward
compatibility.
"""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path


def validate_goose_binary(path: str | None = None) -> bool:
    """Validate Goose binary exists, is executable, and is owned by current user.

    D-14 (release-readiness audit 2026-08-28): also rejects a world-writable
    binary or a binary under a world-writable directory. A root-owned but
    world-writable binary is a privilege-escalation vector: any local user can
    replace it and the next elevated invocation runs attacker code.

    Args:
        path: Path to binary (defaults to environment variable or standard location)

    Returns:
        True if binary is valid, executable, and safely owned

    """
    if path is None:
        # Avoid importing the config loader here (heavy, and would re-enter the
        # security package through validation). Mirror the goose-binary
        # resolver's order with stdlib-only shutil.which.
        env_override = os.environ.get("GOOSE_BIN")
        if env_override:
            path = env_override
        else:
            path = (
                shutil.which("goose")
                or shutil.which("goose.orig")
                or str(Path.home() / ".local/bin/goose")
            )
    if not (os.path.isfile(path) and os.access(path, os.X_OK)):
        return False
    try:
        stat_info = os.stat(path)
        # Accept if owned by current user or root
        if stat_info.st_uid not in (os.getuid(), 0):
            return False
        # D-14: a world-writable binary can be swapped by any local user.
        if stat_info.st_mode & 0o002:
            return False
        # D-14: a binary under a world-writable parent directory can be
        # replaced via a rename by any local user — UNLESS the sticky bit is
        # set (a 1777 dir like /tmp only lets the owner rename/delete, so it
        # is not a swap vector). Walk up the tree to the filesystem root and
        # reject any non-sticky world-writable ancestor.
        current = Path(path).resolve().parent
        while True:
            try:
                dstat = os.stat(current)
            except OSError:
                break
            if dstat.st_mode & 0o002 and not (dstat.st_mode & stat.S_ISVTX):
                return False
            parent = current.parent
            if parent == current:
                break
            current = parent
        return True
    except OSError:
        return False
