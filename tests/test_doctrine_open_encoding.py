"""SP-12: doctrine gate — every text-mode open() names its encoding.

This gate replaces ruff's ``PLW1514`` (unspecified-encoding), which the project
selected while ``preview = true``. SP-12 requires ``preview = false`` — "a
stable configuration must not depend on a preview feature" — but PLW1514 is
preview-only and the doctrine ruff profile does not cover it. Turning preview
off without this test would have silently dropped the rule, which the plan
forbids just as firmly ("do not make a term true when you disable a tool").

Why it matters: ``open(path)`` with no encoding uses
``locale.getpreferredencoding()``. That is UTF-8 on this host and on CI, and
something else on a machine with a different locale — so a file that round-trips
in development raises UnicodeDecodeError in a different environment, or worse,
silently mojibakes. Binary mode has no encoding, so it is exempt.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "beagle"


def _is_binary_mode(call: ast.Call, *, mode_index: int) -> bool:
    """Report whether an open() call is in binary mode.

    Args:
        call: The ``open(...)`` call node.
        mode_index: Positional index of the mode argument. Builtin ``open``
            takes ``(file, mode)`` so it is 1; ``Path.open`` takes ``(mode)``
            so it is 0.

    Returns:
        True if a literal mode argument contains "b".
    """
    mode: ast.expr | None = None
    if len(call.args) > mode_index:
        mode = call.args[mode_index]
    for kw in call.keywords:
        if kw.arg == "mode":
            mode = kw.value
    if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
        return "b" in mode.value
    # A non-literal mode cannot be judged here; treat it as text so the call
    # still has to pass an encoding. That is the safe direction.
    return False


def _has_encoding(call: ast.Call) -> bool:
    """Report whether an open() call passes an encoding (or **kwargs)."""
    return any(kw.arg == "encoding" or kw.arg is None for kw in call.keywords)


def _iter_offenders() -> list[str]:
    """Find text-mode open() calls with no encoding argument."""
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name):
                # builtins.open(file, mode, ...)
                if func.id != "open":
                    continue
                mode_index = 1
            elif isinstance(func, ast.Attribute) and func.attr == "open":
                # os.open() is the POSIX syscall: it takes flags, returns a raw
                # file descriptor, and has no encoding concept at all.
                if isinstance(func.value, ast.Name) and func.value.id == "os":
                    continue
                # Path.open(mode, ...) — mode is the FIRST positional argument.
                mode_index = 0
            else:
                continue

            if _is_binary_mode(node, mode_index=mode_index) or _has_encoding(node):
                continue
            offenders.append(f"{path.relative_to(SRC)}:{node.lineno}")
    return offenders


def test_every_text_open_declares_an_encoding() -> None:
    """No text-mode open() in src/beagle may rely on the platform locale."""
    offenders = _iter_offenders()
    assert not offenders, (
        f"{len(offenders)} text-mode open() call(s) without an explicit encoding. "
        "The default is the platform locale, so these read differently on a host "
        "with a different LANG:\n  " + "\n  ".join(offenders)
    )


def test_gate_flags_a_bare_open() -> None:
    """The gate must actually fire — a check that cannot fail proves nothing."""
    tree = ast.parse("open('f')\n")
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))

    assert not _has_encoding(call)
    assert not _is_binary_mode(call, mode_index=1)


def test_gate_accepts_binary_and_encoded_opens() -> None:
    """Binary mode is exempt; an explicit encoding satisfies the rule."""
    binary = next(n for n in ast.walk(ast.parse("open('f', 'rb')\n")) if isinstance(n, ast.Call))
    path_binary = next(n for n in ast.walk(ast.parse("p.open('rb')\n")) if isinstance(n, ast.Call))
    encoded = next(
        n for n in ast.walk(ast.parse("open('f', encoding='utf-8')\n")) if isinstance(n, ast.Call)
    )

    assert _is_binary_mode(binary, mode_index=1)
    # Path.open puts mode first — the index must follow the call shape.
    assert _is_binary_mode(path_binary, mode_index=0)
    assert _has_encoding(encoded)
