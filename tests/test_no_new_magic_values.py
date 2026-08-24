# Copyright (c) 2026 Matt Creigh. All rights reserved.
"""Wire the hardcoded-defaults gate into the pytest suite.

plans/beagle-config-defaults-abstraction.xml CD-4 / AC-3: running the suite
must run the gate, so a new magic literal cannot merge even when the script
is not invoked by hand.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO_ROOT / "scripts" / "check_hardcoded_defaults.py"


def test_gate_selftest_detects_planted_violation() -> None:
    """The scanner still detects both finding kinds (--selftest mode)."""
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "--selftest"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, f"selftest failed:\n{result.stdout}\n{result.stderr}"


def test_src_tree_is_clean_against_registry() -> None:
    """Zero unclassified tunable literals under src/beagle."""
    result = subprocess.run(
        [sys.executable, str(_SCRIPT)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=_REPO_ROOT,
        check=False,
    )
    assert result.returncode == 0, f"gate findings:\n{result.stdout}\n{result.stderr}"
