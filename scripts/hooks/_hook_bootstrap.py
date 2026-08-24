#!/usr/bin/env python3
"""Interpreter bootstrap for the Beagle goose hooks.

goose invokes a ``"type": "command"`` hook through ``sh``, which honours the
shebang. The shebang cannot name the Beagle virtualenv directly: an absolute
interpreter path in ``scripts/`` is a host-coupling violation that
``scripts/check_host_paths.py`` rejects (BGL-007, BGL-009). So the hooks keep
a portable ``#!/usr/bin/env python3`` shebang and re-exec themselves once,
under an interpreter that can import ``beagle``.

<invariant>
  The re-exec happens BEFORE the hook reads its stdin payload. os.execv keeps
  file descriptor 0 open, so the payload survives the exec, but only if no
  byte of it has been consumed yet.
</invariant>

<invariant>
  The re-exec happens at most once per invocation. _SENTINEL guards it. An
  interpreter that still cannot import beagle after the exec must produce a
  diagnostic and exit, never a second exec.
</invariant>

<invariant>
  A hook never blocks the tool stream. Every failure path here exits 0 —
  but it exits 0 LOUDLY. v1.2.0 (CC-1, BGL-029): the previous code caught
  ImportError and called sys.exit(0) with no output, so a hook running under
  the wrong interpreter reported success on every tool call and folded
  nothing. A silent success is the defect; the exit code is not.
</invariant>

Resolution order for the interpreter:

    1. ``BEAGLE_HOOK_PYTHON`` environment variable.
    2. ``[hook].interpreter`` in the context-management TOML.
    3. Nothing — report and exit.

Step 2 duplicates a minimal form of ``beagle.config._config_path`` on purpose:
this module runs when ``beagle`` is NOT importable, so it cannot use it.
"""

from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path

#: Set in the child environment so the re-exec cannot loop.
_SENTINEL = "BEAGLE_HOOK_REEXEC"

#: Environment override for the hook interpreter.
_ENV_INTERPRETER = "BEAGLE_HOOK_PYTHON"


def _config_candidates() -> list[Path]:
    """Return the context-management TOML paths to try, in order.

    Returns:
        Absolute paths. Each may or may not exist.
    """
    candidates: list[Path] = []
    env_dir = os.environ.get("BEAGLE_CONFIG_DIR", "")
    if env_dir:
        candidates.append(Path(env_dir) / "beagle_core_config" / "context_management.toml")
    xdg = os.environ.get("XDG_CONFIG_HOME", "") or str(Path.home() / ".config")
    candidates.append(Path(xdg) / "beagle" / "beagle_core_config" / "context_management.toml")
    return candidates


def _interpreter_from_config() -> tuple[str, str]:
    """Read ``[hook].interpreter`` from the context-management TOML.

    Returns:
        A ``(interpreter, source)`` pair. ``interpreter`` is an empty string
        when no config declares one; ``source`` names the file that answered,
        or the reason it did not.
    """
    for path in _config_candidates():
        if not path.is_file():
            continue
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            return "", f"{path}: unreadable ({exc})"
        interpreter = str(data.get("hook", {}).get("interpreter", "") or "")
        if interpreter:
            return interpreter, str(path)
        return "", f"{path}: no [hook].interpreter key"
    return "", "no context_management.toml found"


def _resolve_interpreter() -> tuple[str, str]:
    """Find an interpreter that should be able to import beagle.

    Returns:
        A ``(interpreter, source)`` pair. ``interpreter`` is an empty string
        when nothing usable was declared.
    """
    env_value = os.environ.get(_ENV_INTERPRETER, "")
    if env_value:
        return env_value, f"${_ENV_INTERPRETER}"
    return _interpreter_from_config()


def _report(hook: str, reason: str, interpreter: str, source: str) -> None:
    """Write a diagnostic that names every input to the decision.

    Args:
        hook: The hook's short name, for the log prefix.
        reason: Why the hook cannot proceed.
        interpreter: The interpreter that was tried, if any.
        source: Where that interpreter came from.
    """
    print(
        f"[Beagle Hook: {hook}] INERT — {reason}\n"
        f"  running under : {sys.executable}\n"
        f"  interpreter   : {interpreter or '(none resolved)'}\n"
        f"  resolved from : {source}\n"
        f"  set {_ENV_INTERPRETER}, or [hook].interpreter in context_management.toml",
        file=sys.stderr,
    )


def ensure_beagle_interpreter(hook: str) -> None:
    """Re-exec under an interpreter that can import ``beagle``, at most once.

    Call this as the first statement of the hook's ``main()``, before any read
    of ``sys.stdin``. Returns normally when ``beagle`` is already importable.

    Args:
        hook: The hook's short name, used in the diagnostic prefix.

    Raises:
        SystemExit: With code 0, when no usable interpreter exists. A hook
            must not block the tool stream, so this is an exit and not an
            exception — but it is always preceded by a stderr diagnostic.
    """
    try:
        import beagle  # noqa: F401  (probe only)
    except ImportError:
        pass
    else:
        return

    interpreter, source = _resolve_interpreter()

    if os.environ.get(_SENTINEL) == "1":
        _report(
            hook,
            "re-exec already attempted and beagle is still not importable",
            interpreter,
            source,
        )
        raise SystemExit(0)

    if not interpreter:
        _report(hook, "no interpreter is configured", interpreter, source)
        raise SystemExit(0)

    if not os.access(interpreter, os.X_OK):
        _report(hook, "the configured interpreter is not executable", interpreter, source)
        raise SystemExit(0)

    os.environ[_SENTINEL] = "1"
    script = str(Path(sys.argv[0]).resolve())
    try:
        os.execv(interpreter, [interpreter, script, *sys.argv[1:]])
    except OSError as exc:
        _report(hook, f"execv failed: {exc}", interpreter, source)
        raise SystemExit(0) from exc
