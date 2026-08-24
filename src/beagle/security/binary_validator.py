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
from pathlib import Path


def validate_goose_binary(path: str | None = None) -> bool:
    """Validate Goose binary exists, is executable, and is owned by current user.

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
        return stat_info.st_uid in (os.getuid(), 0)
    except OSError:
        return False
