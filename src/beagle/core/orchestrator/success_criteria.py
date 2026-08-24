"""Evaluator for workflow success_criteria.

Supported forms (mix and match):

    success_criteria:
      - "implementation_report is not empty"           # free-text (informational)
      - field: verification_report.verdict             # structured
        equals: "PASS"
      - field: verification_report.pytest.failed
        equals: 0
      - field: cvcp_verdict
        contains: "PASS"

Only structured criteria gate the workflow. Free-text criteria are recorded as
informational and surface in the final report.
"""

from __future__ import annotations

import json
import re
from typing import Any

import structlog

logger = structlog.get_logger("Beagle.orchestrator.criteria")


def _resolve_field(state: dict[str, Any], dotted: str) -> Any:
    """Resolve a dotted path against the workflow state.

    Supports:
      - dotted dict keys (`verification_report.pytest.failed`)
      - if the value at any segment is a JSON-encoded string, parse it lazily
        and continue traversal.
    """
    cur: Any = state
    for part in dotted.split("."):
        if isinstance(cur, str):
            try:
                cur = json.loads(cur)
            except (ValueError, TypeError):
                # Fall back: search for a final_answer JSON block
                m = re.search(r"<final_answer>\s*(\{.*?\})\s*</final_answer>", cur, re.DOTALL)
                if m:
                    try:
                        cur = json.loads(m.group(1))
                    except (ValueError, TypeError):
                        return None
                else:
                    return None
        if isinstance(cur, dict):
            if part not in cur:
                return None
            cur = cur[part]
        else:
            return None
    return cur


def evaluate(
    criteria: list[str | dict[str, Any]],
    state: dict[str, Any],
) -> tuple[bool, list[str]]:
    """Evaluate workflow success_criteria against the final state.

    Returns:
        (passed, failure_messages). passed is True iff all structured criteria
        passed. Free-text criteria do not affect passed.

    """
    failures: list[str] = []
    for c in criteria:
        if isinstance(c, str):
            continue  # informational
        if not isinstance(c, dict):
            failures.append(f"unparseable success_criterion: {c!r}")
            continue
        field = c.get("field")
        if not field:
            failures.append(f"success_criterion missing 'field': {c!r}")
            continue
        value = _resolve_field(state, field)
        if "equals" in c:
            if value != c["equals"]:
                failures.append(f"{field}={value!r} does not equal {c['equals']!r}")
        elif "contains" in c:
            if not isinstance(value, str) or c["contains"] not in value:
                failures.append(f"{field}={str(value)[:80]!r} does not contain {c['contains']!r}")
        elif c.get("not_empty"):
            if value is None or (isinstance(value, str | list | dict) and len(value) == 0):
                failures.append(f"{field} is empty")
        else:
            failures.append(f"success_criterion has no recognised predicate: {c!r}")
    return (not failures, failures)
