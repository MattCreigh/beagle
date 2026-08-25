#!/usr/bin/env python3
"""Exception-debt guard for the fig-leaf 4-tuple.

The auto-scrub signature ``except (ImportError, AttributeError, RuntimeError,
OSError)`` is ``except Exception`` in disguise. The Makefile ``banned`` target
already HARD-FAILS when this tuple appears with no annotation at all. This script
goes one step further: it distinguishes a *bare* ``# catch: NARROWED`` label
(which proves nothing) from a real ``# RATIONALE=<why these four types>``.

Modes:
  (default)  Print the bare-label debt as a WARNING and exit 0 — non-breaking,
             so it can run inside ``make banned`` without failing existing gates
             while the v13.16 debt (~100 sites) is paid down via the ledger
             ai/v13.16_exception_debt.md.
  --strict   Exit 1 if any bare-label site remains. Wire into ``qa`` once the
             ledger is cleared so the fig-leaf can never silently return.

A site is OK only if it is genuinely narrowed (not this tuple) OR carries both an
annotation and a ``RATIONALE=``. Sites with NEITHER annotation are the Makefile's
job, not this one.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

FIG_LEAF = {"ImportError", "AttributeError", "RuntimeError", "OSError"}
# v1.0.4 (audit E4): the package moved from `beagle/` to `src/` in v1.0.0, so
# this ROOT used to point at a directory that no longer exists and the guard
# scanned nothing — silently passing while doing no work. Point at `src/beagle/`
# and `_find_debt()` now hard-fails if the scan would be vacuous.
ROOT = Path(__file__).resolve().parent.parent / "src" / "beagle"
_EXCEPT_TUPLE = re.compile(r"except\s*\(([^)]*)\)")


def _is_fig_leaf_tuple(inside: str) -> bool:
    names = {tok.strip() for tok in inside.split(",") if tok.strip()}
    return names == FIG_LEAF


def _ensure_not_vacuous() -> None:
    """Hard-fail if the scan root is missing or empty (audit E4).

    A guard that scans zero files "passes" trivially while doing no work. This
    prevents a future layout move from silently neutering the check again.
    """
    if not ROOT.is_dir():
        raise SystemExit(
            f"FAIL: scan root does not exist: {ROOT}. "
            "The package layout changed — repoint ROOT, do not delete this guard."
        )
    scanned = list(ROOT.rglob("*.py"))
    if not scanned:
        raise SystemExit(f"FAIL: no .py files found under {ROOT}. The guard would be vacuous.")


def find_debt() -> list[tuple[Path, int, str]]:
    """Return (path, lineno, text) for fig-leaf tuples with a bare label / no rationale."""
    _ensure_not_vacuous()
    debt: list[tuple[Path, int, str]] = []
    for py in ROOT.rglob("*.py"):
        if "/tests/" in py.as_posix():
            continue
        for lineno, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            m = _EXCEPT_TUPLE.search(line)
            if not m or not _is_fig_leaf_tuple(m.group(1)):
                continue
            if "RATIONALE=" in line:
                continue  # justified — fine
            if "# catch: NARROWED" in line:
                debt.append((py, lineno, line.strip()))  # bare label — fig-leaf debt
            # no annotation at all → the Makefile 'banned' target hard-fails it
    return debt


def main() -> int:
    strict = "--strict" in sys.argv[1:]
    debt = find_debt()
    if not debt:
        print("Exception-debt check: 0 bare-label fig-leaf tuples.")
        return 0
    print(
        f"Exception-debt: {len(debt)} broad 4-tuple(s) carry a bare "
        "'# catch: NARROWED' with no '# RATIONALE='. A label proves nothing — "
        "narrow to the types actually expected, or add "
        "'# RATIONALE=<why these four>'. Track via ai/v13.16_exception_debt.md."
    )
    for path, lineno, text in debt:
        print(f"  {path.relative_to(ROOT.parent)}:{lineno}: {text}")
    if strict:
        print("FAIL (--strict): bare-label fig-leaf debt must be zero.")
        return 1
    print("WARN: non-blocking until the ledger is cleared; enforce with 'make banned-strict'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
