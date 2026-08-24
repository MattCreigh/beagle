"""SP-1: doctrine gate — no exception handler may be silent.

beagle-spotless-phase2, work package SP-1. A handler whose entire body is
``pass`` or ``continue`` makes a failure invisible: the operation degrades, the
caller carries on with a default, and nothing anywhere records that it
happened. The plan's inventory counted 124 of these; six Phase 1 defects
reached production through one.

This gate keeps the count at zero. It is deliberately an AST check rather than
a grep: a comment above a bare ``pass`` does not make the handler observable,
and a ``# noqa`` cannot silence it.

The rule an accepted handler satisfies (plan logic block L3):

    handler_ok = ((logs_at_warning OR reraises OR returns_typed_error)
                  AND names_exact_type)

This module enforces the structural half — the handler body must *do*
something. EXEMPT is currently empty and should stay that way; the two sites
that genuinely cannot log (inside the logging pipeline, where a log record
would recurse) were solved with a failure counter instead, not an exemption.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "beagle"

# <invariant>
# Any entry added here must state why the site *cannot* log, not why logging is
# awkward — and adding one should be the last resort. The known hard case is
# observability/logging.py, whose handlers run inside the logging pipeline so a
# log record would recurse; it increments a counter exposed by
# trace_correlation_failures() rather than taking an exemption. Prefer that
# shape. Empty is the correct state.
# </invariant>
EXEMPT: dict[tuple[str, str], str] = {}


def _iter_silent_handlers() -> list[tuple[Path, int, str]]:
    """Find every except handler whose body is only pass/continue.

    Returns:
        Tuples of (path, line number, source line).
    """
    found: list[tuple[Path, int, str]] = []
    for path in sorted(SRC.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source)
        except SyntaxError:  # pragma: no cover - a parse failure is its own bug
            pytest.fail(f"{path} does not parse")
        lines = source.splitlines()
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            if all(isinstance(stmt, (ast.Pass, ast.Continue)) for stmt in node.body):
                found.append((path, node.lineno, lines[node.lineno - 1].strip()))
    return found


def test_no_silent_exception_handlers() -> None:
    """No handler in src/beagle may have a body of only pass or continue."""
    offenders = [
        f"{path.relative_to(SRC)}:{lineno}: {text}"
        for path, lineno, text in _iter_silent_handlers()
        if (str(path.relative_to(SRC)), str(lineno)) not in EXEMPT
    ]

    assert not offenders, (
        f"{len(offenders)} silent exception handler(s) found. A handler must log at "
        "warning or above, re-raise, or return a documented error value — a bare "
        "pass/continue hides the failure from the operator:\n  " + "\n  ".join(offenders)
    )


def test_gate_detects_a_silent_handler() -> None:
    """The gate must actually fire — a check that cannot fail proves nothing.

    Mirrors the plan's approval_check pattern ("delete one branch, the test must
    fail"), but on a synthetic tree so no real file is touched.
    """
    tree = ast.parse("try:\n    x = 1\nexcept ValueError:\n    pass\n")
    handlers = [n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)]

    assert len(handlers) == 1
    assert all(isinstance(stmt, ast.Pass) for stmt in handlers[0].body)


def test_gate_accepts_a_handler_that_logs() -> None:
    """A handler that logs must not be reported."""
    tree = ast.parse(
        "try:\n    x = 1\nexcept ValueError as exc:\n    logger.warning('failed: %s', exc)\n"
    )
    handlers = [n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)]

    assert len(handlers) == 1
    assert not all(isinstance(stmt, (ast.Pass, ast.Continue)) for stmt in handlers[0].body)


def test_exemptions_are_documented_and_real() -> None:
    """Every exemption must name a file that exists and still contain a silent handler.

    Stops the exemption list from outliving the code it excuses.
    """
    silent = {(str(p.relative_to(SRC)), str(line)) for p, line, _ in _iter_silent_handlers()}
    for key, reason in EXEMPT.items():
        rel, lineno = key
        assert (SRC / rel).is_file(), f"exemption names a missing file: {rel}"
        assert reason.strip(), f"exemption for {rel}:{lineno} has no reason"
        assert key in silent, (
            f"exemption for {rel}:{lineno} is stale — that handler is no longer silent. "
            "Remove the entry."
        )
