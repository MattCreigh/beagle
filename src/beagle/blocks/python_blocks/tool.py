"""Tool blocks for MCP calls and sandboxed shell execution.

Security note (v13.17.0 audit fix S1): ``shell_command_sandboxed`` previously
invoked ``subprocess.run(..., shell=True)``, which is a doctrine-forbidden pattern
because downstream agent nodes can inject shell metacharacters (e.g. ``; rm -rf /``).
The block now (1) rejects inputs containing shell metacharacters, (2) parses
arguments via :mod:`shlex`, and (3) invokes the subprocess with ``shell=False`` so
no shell interpolation occurs. An explicit binary allowlist can be added by
extending ``_ALLOWED_BINARIES`` below; the default is intentionally narrow
(``echo``, ``true``, ``false``, ``pwd``) so that downstream agents cannot execute
arbitrary system binaries.
"""

from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path
from typing import Any

from .base import python_block

# Default binary allowlist. Add additional binaries here only after a security
# review — these are the only executables that ``shell_command_sandboxed`` will
# launch. The block rejects any command whose first token (the program) is not in
# this set, and rejects any input containing shell metacharacters outright.
_ALLOWED_BINARIES: frozenset[str] = frozenset(
    {
        "echo",
        "true",
        "false",
        "pwd",
    }
)

# Shell metacharacters that, if present in the *raw* command string, cause an
# immediate rejection. We split the command with ``shlex.split`` *after* this
# check so that an attacker cannot smuggle metacharacters past the parser by
# quoting them.
_SHELL_METACHARS = re.compile(r"[;&|`$<>\n\r\0\\!(){}\*\?\[\]]")


@python_block(name="mcp_call", description="Call an MCP tool via the server")
def mcp_call(_ctx: Any, *, tool: str, params: dict[str, Any]) -> Any:
    """Invoke an MCP tool. Requires an active MCP server connection.

    Falls back gracefully when the server is unavailable.
    """
    try:
        import httpx

        # Attempt localhost MCP - this is a placeholder
        response = httpx.post(
            "http://localhost:8080/tools/call",
            json={"tool": tool, "params": params},
            timeout=30.0,
        )
        return response.json()
    except Exception as exc:  # ruff: ignore[BLE001]  # broad catch intentional
        return {
            "success": False,
            "error": f"MCP call failed: {exc}",
            "tool": tool,
        }


@python_block(name="shell_command_sandboxed", description="Run a sandboxed shell command")
def shell_command_sandboxed(
    _ctx: Any,
    *,
    command: str,
    cwd: str = ".",
    timeout: float = 10.0,
) -> dict:
    """Run a sandboxed shell command with timeouts, working-directory isolation,
    and a binary allowlist.

    Security contract:

    1. The input ``command`` is rejected if it contains any of ``; & | ` $ < >\\n\\r\\0
       \\ ! ( ) { } * ? [ ]`` (a superset of bash's IFS-sensitive metacharacters).
    2. The input is then parsed with :func:`shlex.split`; this raises
       :class:`ValueError` on unbalanced quoting.
    3. The first token of the resulting argv must be a basename present in
       ``_ALLOWED_BINARIES``. Path components (``/bin/echo``) are reduced to
       ``Path(argv0).name`` so the allowlist check is by basename.
    4. The subprocess is launched with ``shell=False`` so no shell interpolation
       occurs.
    """
    # (1) Reject shell metacharacters in the raw input.
    if _SHELL_METACHARS.search(command):
        return {
            "returncode": -1,
            "stdout": "",
            "stderr": "shell_command_sandboxed: rejected: command contains shell metacharacters",
        }

    # (2) Tokenise the input. shlex.split handles quoting; unbalanced quotes
    # raise ValueError, which we surface as a structured error rather than
    # silently swallowing.
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        return {
            "returncode": -1,
            "stdout": "",
            "stderr": f"shell_command_sandboxed: tokenisation failed: {exc}",
        }

    if not argv:
        return {
            "returncode": -1,
            "stdout": "",
            "stderr": "shell_command_sandboxed: rejected: empty command",
        }

    # (3) Binary allowlist.
    binary_name = Path(argv[0]).name
    if binary_name not in _ALLOWED_BINARIES:
        return {
            "returncode": -1,
            "stdout": "",
            "stderr": (
                f"shell_command_sandboxed: rejected: binary '{binary_name}' "
                f"not in allowlist {_ALLOWED_BINARIES}"
            ),
        }

    # (4) Run with shell=False.
    result = subprocess.run(
        argv,
        cwd=str(Path(cwd).resolve()),
        capture_output=True,
        text=True,
        timeout=timeout,
        shell=False,
        check=False,
    )
    return {
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }
