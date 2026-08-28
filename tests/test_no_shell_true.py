"""Test that no subprocess call in the codebase uses shell=True (R4.4, v13.20.9).

Beagle doctrine: 'Never execute or suggest commands from the codebase
with shell=True. Use subprocess.run/Popen with argument lists only.'

The single historical offender is a docstring warning in
beagle/blocks/python_blocks/tool.py:4 that contains the literal
phrase 'shell=True=False' (negated, in a code example explaining
WHY to avoid shell=True). This test must distinguish that
legitimate documentation from an actual call site.

Strategy: AST-scan every .py file under beagle/,
find every Call node where the func is one of {subprocess.run,
subprocess.Popen, subprocess.call, subprocess.check_output,
subprocess.check_call, os.system, os.popen}, and assert that
none of them have a keyword argument `shell=True`.

Docstrings and comments are not parsed (they don't appear in the
AST), so the docstring warning at python_blocks/tool.py:4 is
correctly ignored.
"""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Audit scope: first-party source and tests. The project venv
# (.venv/) and any virtualenv are third-party build artifacts —
# vendored packages (e.g. sympy's own test-suite) legitimately use
# shell=True and are not under beagle doctrine. Ruff runs the same
# scoping rule for this repo.
SCAN_ROOTS = ("src", "tests", "scripts")

# Functions that accept `shell=` as a kwarg (or, in os.system's case, are
# inherently shell-invoking). These are the only call sites we audit.
DANGEROUS_FUNCS = {
    "subprocess.run",
    "subprocess.Popen",
    "subprocess.call",
    "subprocess.check_output",
    "subprocess.check_call",
    "os.system",
    "os.popen",
}


def _call_uses_shell_true(node: ast.Call) -> bool:
    """Return True if a Call node has shell=True as a keyword argument."""
    for kw in node.keywords:
        if kw.arg == "shell":
            # `shell=True` literal — the dangerous pattern
            if isinstance(kw.value, ast.Constant) and kw.value.value is True:
                return True
            # `shell=<anything truthy>` — also dangerous (e.g. shell=1)
            if isinstance(kw.value, ast.Constant) and bool(kw.value.value):
                return True
    return False


def _format_qualified_name(node: ast.Call) -> str:
    """Best-effort 'module.func' for a Call node (handles 'subprocess.run')."""
    func = node.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return f"{func.value.id}.{func.attr}"
    if isinstance(func, ast.Name):
        return func.id
    return "<unknown>"


def test_no_subprocess_shell_true() -> None:
    """No subprocess.* or os.system/popen call may use shell=True."""
    offenders: list[tuple[str, int, str]] = []  # (file, line, call)
    py_files: list[Path] = []
    for root in SCAN_ROOTS:
        base = PROJECT_ROOT / root
        if base.is_dir():
            py_files.extend(base.rglob("*.py"))
    for py_file in py_files:
        try:
            source = py_file.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=str(py_file))
        except SyntaxError:
            # Not our problem; ruff handles syntax errors.
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            qualname = _format_qualified_name(node)
            if qualname not in DANGEROUS_FUNCS:
                continue
            if _call_uses_shell_true(node):
                offenders.append((str(py_file.relative_to(PROJECT_ROOT)), node.lineno, qualname))

    assert not offenders, (
        "Found subprocess call(s) with shell=True — Beagle doctrine forbids this.\n"
        "Use subprocess.run([...]) with an argument list instead of a shell string.\n"
        f"Offenders (file:line, function): {offenders}"
    )
