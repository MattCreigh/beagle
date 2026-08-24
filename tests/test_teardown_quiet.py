"""Tests for the teardown quietness and connection-ceiling removal (QA-4).

The connection ceiling was compared and never enforced, and the atexit
shutdown logged to a closed stream after pytest closed its captures.  These
tests prove the dead config is gone and the full process exit is quiet.
"""

from __future__ import annotations

import subprocess
import sys


def test_connection_ceiling_is_gone() -> None:
    """The tracking database exposes no connection ceiling."""
    import beagle.tracking.database as d

    assert not hasattr(d, "_MAX_CONNECTIONS")
    assert not hasattr(d.TrackingDatabase, "_conn_count")


def test_full_process_exit_prints_no_logging_error() -> None:
    """A process importing the executor exits quietly."""
    code = "import beagle.core.orchestrator.executor"
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0
    assert "Logging error" not in result.stderr
