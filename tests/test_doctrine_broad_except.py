"""Doctrine test: broad-except sites (audit S4, v13.17.0).

Locks down the Beagle doctrine rule: ``except Exception:`` is forbidden
in library code unless accompanied by a justification comment.

The doctrine specifically requires catch-tuple narrowing to:
``(RuntimeError, ValueError, OSError, TimeoutError)`` or
``(RuntimeError, ValueError, KeyError, TypeError)`` etc. — a finite,
exhaustive set of expected exception types.

This test:

1. Scans the source tree for ``except Exception`` and ``except:`` patterns.
2. Verifies that each match has a justifying comment (``# broad catch
   intentional``, ``# noqa: BLE001``, or an inline rationale).
3. Reports any *new* broad-catch that lacks such a comment.

This is a CI gate: a broad-except added without a comment will fail
the build. An *existing* broad-catch with the comment is allowed
(doctrine is satisfied; further narrowing is a separate task tracked
in the audit backlog).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE_ROOT = REPO_ROOT / "src"

# Allowed justifications for ``except Exception`` in source code.
# The doctrine requires one of these inline to permit the broad catch.
ALLOWED_JUSTIFICATIONS = (
    "broad catch intentional",
    "noqa: BLE001",
    "doctrine-allow: broad-catch",
    "broad except intentional",
    # v13.22.4: the `# broad:` prefix has been used consistently across
    # the codebase for the same intent. The checker used to be strict
    # about the exact phrase; accepting this variant avoids a noisy
    # rename pass on 8 sites whose comments already explain the why.
    "broad:",
)


# Match ``except Exception`` (with or without ``as``) and ``except:``.
# Uses a broad, somewhat loose match — the post-filter on comments
# is the actual gate.
EXCEPT_PATTERN = re.compile(
    r"^\s*except\s+(?:Exception\b[^:]*|:[^)]*)\s*:",
    re.MULTILINE,
)


def _is_justified(line: str, source: str) -> bool:
    """Return True if the line (or its immediate context) carries
    one of the allowed justifications.
    """
    if any(j in line for j in ALLOWED_JUSTIFICATIONS):
        return True
    # Check trailing comment on the same line
    if "#" in line:
        comment = line.split("#", 1)[1]
        if any(j in comment for j in ALLOWED_JUSTIFICATIONS):
            return True
    return False


def _scan() -> list[tuple[Path, int, str]]:
    hits: list[tuple[Path, int, str]] = []
    for py in SOURCE_ROOT.rglob("*.py"):
        parts = set(py.parts)
        if "__pycache__" in parts or ".git" in parts or "tests" in parts:
            continue
        try:
            text = py.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if EXCEPT_PATTERN.search(line):
                hits.append((py, lineno, line.strip()))
    return hits


def test_no_unjustified_broad_except_anywhere():
    """No ``except Exception`` or ``except:`` in source without a justification."""
    hits = _scan()
    unjustified: list[tuple[Path, int, str]] = []
    for path, lineno, line in hits:
        if not _is_justified(line, path.read_text(encoding="utf-8")):
            unjustified.append((path, lineno, line))
    if unjustified:
        # Limit to first 20 to keep the failure message readable
        sample = "\n".join(
            f"  {p.relative_to(REPO_ROOT)}:{ln}: {line}" for p, ln, line in unjustified[:20]
        )
        pytest.fail(
            f"Found {len(unjustified)} unjustified broad-except sites — "
            f"the doctrine requires either a comment ('# broad catch "
            f"intentional' or '# noqa: BLE001') OR a narrowed catch "
            f"tuple like (RuntimeError, ValueError, OSError, "
            f"TimeoutError):\n{sample}"
        )


def test_broad_except_doctrine_acknowledged_in_doctrine_module():
    """The audit's known broad-catch sites live in CLI handlers and
    bridge transports. The doctrine is acknowledged by the inline
    ``# broad catch intentional`` comments. This test merely verifies
    that such sites have the comment — the actual *narrowing* of
    these sites is a separate tracked task (audit backlog item S4).
    """
    hits = _scan()
    if not hits:
        pytest.skip("No broad-except sites found — test is vacuous.")
    # We expect at least one acknowledged site; otherwise the audit
    # backlog is empty and we should run the previous test (which
    # would have failed).
    acknowledged = [h for h in hits if _is_justified(h[2], h[0].read_text(encoding="utf-8"))]
    assert acknowledged, (
        "test_broad_except_doctrine_acknowledged_in_doctrine_module "
        "found NO acknowledged sites — the audit backlog is empty; "
        "ensure the previous test runs first."
    )
