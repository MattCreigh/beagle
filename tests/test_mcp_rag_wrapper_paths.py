"""Tests for mcp_rag_wrapper temp-path resolution (T-SEC, Q-02 S108).

The wrapper used to hardcode ``/tmp/mcp_rag_health`` and ``/tmp/mcp_rag_pid``.
That coupled it to a literal /tmp mount and produced a S108 finding. This test
pins the new contract: the health/PID files resolve under the system temp
directory (honouring TMPDIR), so the wrapper is host-agnostic.
"""

from __future__ import annotations

import os
import tempfile

from beagle.infrastructure import mcp_rag_wrapper


def test_health_and_pid_files_resolve_under_system_temp() -> None:
    """The wrapper's health/PID files live under the system temp root."""
    tmp_root = tempfile.gettempdir()
    for path in (mcp_rag_wrapper.HEALTH_FILE, mcp_rag_wrapper.PID_FILE):
        assert os.path.commonpath([str(path), tmp_root]) == tmp_root
        assert path.name in (
            mcp_rag_wrapper._HEALTH_BASENAME,
            mcp_rag_wrapper._PID_BASENAME,
        )


def test_health_and_pid_basenames_are_distinct() -> None:
    """The two files must not collide; each carries its own basename."""
    assert mcp_rag_wrapper._HEALTH_BASENAME != mcp_rag_wrapper._PID_BASENAME
    assert mcp_rag_wrapper.HEALTH_FILE.name == mcp_rag_wrapper._HEALTH_BASENAME
    assert mcp_rag_wrapper.PID_FILE.name == mcp_rag_wrapper._PID_BASENAME
