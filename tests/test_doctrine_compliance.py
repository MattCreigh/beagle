"""Doctrine compliance regression tests (audit Phase 4, v13.17.0).

Locks down three of the package's own style-guide rules:

1. ``datetime.utcnow`` is forbidden in favour of ``datetime.now(timezone.utc)``
   or ``datetime.now(UTC)``. The deprecation lives on in 3.12+; the doctrine
   is the binding rule.

2. Truncated UUID4 strings (``str(uuid.uuid4())[:N]`` or
   ``uuid.uuid4().hex[:N]``) are forbidden. Task IDs use the full
   ``uuid.uuid4()`` to avoid collision risk on routing keys.

3. ``shell=True`` in ``subprocess`` calls is forbidden (the
   ``shell_command_sandboxed`` block in ``beagle/blocks/python_blocks/tool.py``
   is the only legitimate exception, and even there the implementation now
   uses ``shell=False`` post-audit fix S1).

These tests are static-grep based — they scan the source tree ONCE and
report all matches. Performance: O(N) over the source tree, sub-second on
this repo. (We do NOT parametrize per line — that produces 80K+ test cases
and pytest itself is the bottleneck.)
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE_ROOT = REPO_ROOT / "src"


def _scan(pattern: re.Pattern[str], skip_docstrings: bool = True) -> list[tuple[Path, int, str]]:
    """Return a list of (path, lineno, line) tuples for each match.

    If ``skip_docstrings`` is True (the default), lines inside triple-quoted
    docstrings are ignored. This is correct for *doctrine* scans because the
    doctrine applies to executable code, not documentation that *describes*
    the old (forbidden) pattern in a "previously" context.
    """
    hits: list[tuple[Path, int, str]] = []
    for py in SOURCE_ROOT.rglob("*.py"):
        # Skip generated / vendored directories.
        parts = set(py.parts)
        if "__pycache__" in parts or ".git" in parts:
            continue
        try:
            text = py.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        in_docstring = False
        docstring_quote: str | None = None
        for lineno, line in enumerate(text.splitlines(), start=1):
            stripped = line.lstrip()
            if skip_docstrings:
                # Track triple-quoted docstring state.
                if not in_docstring:
                    if '"""' in stripped or "'''" in stripped:
                        # Could be opening on this line.
                        quote = '"""' if '"""' in stripped else "'''"
                        # If both open and close on the same line, no state change.
                        if stripped.count(quote) == 1:
                            in_docstring = True
                            docstring_quote = quote
                            continue
                else:
                    if docstring_quote and docstring_quote in line:
                        in_docstring = False
                        docstring_quote = None
                    continue
            if pattern.search(line):
                hits.append((py, lineno, line.strip()))
    return hits


# =========================================================================
# 1. datetime.utcnow is forbidden
# =========================================================================


_DATETIME_UTCNOW = re.compile(r"datetime\.utcnow")


def test_no_datetime_utcnow_anywhere():
    """``datetime.utcnow`` must not appear in any source file."""
    hits = _scan(_DATETIME_UTCNOW)
    if hits:
        msg = "\n".join(f"  {p.relative_to(REPO_ROOT)}:{ln}: {line}" for p, ln, line in hits[:20])
        pytest.fail(
            f"Found {len(hits)} datetime.utcnow call site(s) — "
            f"doctrine violation. Use datetime.now(timezone.utc) or "
            f"datetime.now(UTC):\n{msg}"
        )


# =========================================================================
# 2. Truncated UUID4 is forbidden
# =========================================================================


_TRUNCATED_UUID = re.compile(
    r"str\(uuid\.uuid4\(\)\)\s*\[\s*:[0-9]+\s*\]"
    r"|uuid\.uuid4\(\)\.hex\s*\[\s*:[0-9]+\s*\]"
)


def test_no_truncated_uuid4_anywhere():
    """Truncated ``uuid4()`` strings are forbidden — use the full UUID."""
    hits = _scan(_TRUNCATED_UUID)
    if hits:
        msg = "\n".join(f"  {p.relative_to(REPO_ROOT)}:{ln}: {line}" for p, ln, line in hits[:20])
        pytest.fail(
            f"Found {len(hits)} truncated UUID4 call site(s) — "
            f"doctrine violation. Use the full str(uuid.uuid4()) or "
            f"uuid.uuid4().hex:\n{msg}"
        )


# =========================================================================
# 3. shell=True in subprocess calls is forbidden
# =========================================================================


_SHELL_TRUE = re.compile(
    r"subprocess\.(run|call|check_output|check_call|Popen)\s*\([^)]*shell\s*=\s*True",
    re.DOTALL,
)


def test_no_shell_true_in_subprocess_anywhere():
    """``subprocess.<>(..., shell=True)`` is forbidden (security baseline).

    Exception: the ``shell_command_sandboxed`` block's docstring may mention
    the *old* pattern in a clearly-attributed "previously used" context.
    Even there, the implementation now uses ``shell=False`` (audit fix S1).
    """
    hits = _scan(_SHELL_TRUE)
    # Filter out the one allowed mention (the docstring of the sandboxed tool).
    real_violations = [
        (p, ln, line)
        for p, ln, line in hits
        if not ("shell_command_sandboxed" in str(p) and "previously" in line.lower())
    ]
    if real_violations:
        msg = "\n".join(
            f"  {p.relative_to(REPO_ROOT)}:{ln}: {line}" for p, ln, line in real_violations[:20]
        )
        pytest.fail(
            f"Found {len(real_violations)} shell=True call site(s) — "
            f"security baseline violation:\n{msg}"
        )
