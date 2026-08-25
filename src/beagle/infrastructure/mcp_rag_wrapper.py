#!/usr/bin/env python3
"""Wrapper to run the MCP RAG server with Docker healthcheck support.

Starts the MCP RAG server in the background, writes a health file, and
monitors the process, terminating gracefully on SIGTERM/SIGINT.
"""

from __future__ import annotations

import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Use the system temp directory (respecting TMPDIR) so the wrapper is not
# coupled to a literal /tmp mount and a shared path is found across hosts.
# A healthcheck/companion process resolves these through the same temp root.
_HEALTH_BASENAME = "mcp_rag_health"
_PID_BASENAME = "mcp_rag_pid"
HEALTH_FILE = Path(tempfile.gettempdir()) / _HEALTH_BASENAME
PID_FILE = Path(tempfile.gettempdir()) / _PID_BASENAME

_server: subprocess.Popen | None = None


def _write_health(state: str) -> None:
    """Write the health state to the health file.

    Args:
        state: The health state string.

    """
    HEALTH_FILE.write_text(state, encoding="utf-8")


def _cleanup(_signum: int, _frame: object) -> None:
    """Gracefully stop the server on signal.

    Args:
        _signum: The signal number.
        _frame: The current stack frame.

    """
    global _server
    _write_health("stopping")
    if _server is not None and _server.poll() is None:
        _server.terminate()
        try:
            _server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _server.kill()
            _server.wait()
    _write_health("stopped")
    print("[Wrapper] MCP RAG server stopped")
    sys.exit(0)


def main() -> int:
    """Run the wrapper.

    Returns:
        Exit code (0 on success).

    """
    global _server
    signal.signal(signal.SIGTERM, _cleanup)
    signal.signal(signal.SIGINT, _cleanup)

    _write_health("starting")
    print("[Wrapper] Starting MCP RAG server in background...")

    server_dir = Path(__file__).resolve().parent
    _server = subprocess.Popen(
        [sys.executable, str(server_dir / "mcp_rag_server.py")],
        stdin=subprocess.DEVNULL,
        cwd=server_dir,
    )
    PID_FILE.write_text(str(_server.pid), encoding="utf-8")

    time.sleep(2)
    if _server.poll() is not None:
        print("[Wrapper] ERROR: MCP RAG server failed to start")
        _write_health("failed")
        return 1

    _write_health("ready")
    print(f"[Wrapper] MCP RAG server running (PID: {_server.pid}), health file: {HEALTH_FILE}")

    while True:
        time.sleep(5)
        if _server.poll() is not None:
            print("[Wrapper] MCP RAG server process died unexpectedly")
            _write_health("failed")
            return 1
        HEALTH_FILE.touch()


if __name__ == "__main__":
    # Consistent --version across dev-tool entry points.
    from .mcp_common import maybe_print_version

    if maybe_print_version():
        raise SystemExit(0)
    sys.exit(main())
