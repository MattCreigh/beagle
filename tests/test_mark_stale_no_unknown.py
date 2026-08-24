"""v13.15.1 P2: no caller of mark_stale() may use the default reason. Every
call site must pass a precise reason string, so the next time RAG is marked
stale we can identify why from the log."""

from __future__ import annotations

import re
from pathlib import Path


def test_no_mark_stale_defaults():
    pkg_root = Path(__file__).resolve().parents[1] / "src"
    offenders = []
    pattern = re.compile(r"\.mark_stale\(\s*\)")  # no-args call
    for py in pkg_root.rglob("*.py"):
        if "/tests/" in str(py):
            continue
        text = py.read_text(errors="replace")
        for i, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                offenders.append(f"{py}:{i}: {line.strip()}")
    assert not offenders, "mark_stale() called without a reason; offenders:\n" + "\n".join(
        offenders
    )
