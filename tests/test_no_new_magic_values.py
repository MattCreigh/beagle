# Copyright (c) 2026 Matt Creigh. All rights reserved.
"""Wire the hardcoded-defaults detector into the pytest suite.

Running the suite must prove the scanner works, so a regression in
detection cannot land unnoticed. The classification registry was retired
when all user-editable configuration moved to ``~/.config/beagle``; the
scanner now runs report-only unless an explicit ``--registry`` is given.
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


def test_report_only_scan_runs_clean_process() -> None:
    """Report-only mode over src/beagle completes with exit 0 (no registry)."""
    result = subprocess.run(
        [sys.executable, str(_SCRIPT)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=_REPO_ROOT,
        check=False,
    )
    assert result.returncode == 0, f"report scan failed:\n{result.stdout}\n{result.stderr}"
    assert "report-only" in result.stdout, f"unexpected summary:\n{result.stdout}"
