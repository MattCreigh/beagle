"""Text skeletonization and structural extraction utilities.

Provides build_skeleton() for extracting structural skeletons from text
while collapsing duplicate structural lines.

Migrated from context/context_integration.py and context/context_optimizer.py
to eliminate duplication (F7 — Structural Quality Remediation).
"""

from __future__ import annotations

import re

__all__ = ["build_skeleton"]

_STRUCTURAL_KEYWORDS_RE: re.Pattern[str] = re.compile(
    r"^(\s*)(class |async def |def |import |from |return |"
    r"raise |@|if __name__|# ---|# ===|# §)"
)

_HEAD_LINES: int = 30
_TAIL_LINES: int = 30
_MIN_LINES: int = 80


def build_skeleton(text: str) -> str:
    """Extract structural skeleton from text.

    Keeps first 30 lines, last 30 lines, and unique structural lines
    (class, def, import, return, raise, etc.). Duplicate structural
    lines are collapsed to single occurrences with omission markers.

    Falls back to head+tail truncation if the skeleton would be
    larger than or equal to the original text.

    Args:
        text: The text to skeletonize.

    Returns:
        A compact representation preserving structural information.

    """
    lines = text.splitlines()
    if len(lines) <= _MIN_LINES:
        return text

    keep_indices: set[int] = set()
    keep_indices.update(range(min(_HEAD_LINES, len(lines))))
    keep_indices.update(range(max(len(lines) - _TAIL_LINES, 0), len(lines)))

    seen_structural: dict[str, int] = {}
    for i, line in enumerate(lines):
        if _STRUCTURAL_KEYWORDS_RE.match(line):
            stripped = line.strip()
            if stripped not in seen_structural:
                seen_structural[stripped] = i
                keep_indices.add(i)

    result: list[str] = []
    in_omit = False
    omit_count = 0

    for i, line in enumerate(lines):
        if i in keep_indices:
            if in_omit:
                result.append(f"  ... [{omit_count} lines omitted] ...")
                in_omit = False
                omit_count = 0
            result.append(line)
        else:
            if not in_omit:
                in_omit = True
                omit_count = 0
            omit_count += 1

    if in_omit and omit_count > 0:
        result.append(f"  ... [{omit_count} lines omitted] ...")

    skeleton = "\n".join(result)

    if len(skeleton) >= len(text):
        head = min(2000, len(text))
        tail = min(2000, len(text))
        return text[:head] + f"\n... [{len(text) - head - tail} chars] ...\n" + text[-tail:]

    return skeleton
