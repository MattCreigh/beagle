"""Context Reporter — Bridge between goose context tracking and Beagle monitoring.

Goose tracks its own context window utilization internally, but does not
expose it as an environment variable.  This module provides a file-based
sidechannel so that the Beagle ContextMonitor can read *actual* context usage
instead of relying on the iteration-count heuristic.

Design:
  - Goose (the LLM agent, via system instruction) writes a JSON snapshot
    to ~/.beagle/context_report.json after every tool call or every N turns.
  - Beagle's ContextMonitor._get_context_usage() reads this file as its
    primary source, falling back to the env var, then the heuristic.

The file format is intentionally minimal:

    {
      "percentage": 0.59,
      "used_tokens": 75264,
      "max_tokens": 128000,
      "timestamp": "2026-04-26T12:29:00Z",
      "source": "goose"
    }

Security: the report file is created with 0600 permissions and ignored
if its mtime is older than 300 s (5 min), preventing stale data from
causing false compaction triggers after a session restarts.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from beagle.utils.atomic import atomic_write_text

logger = logging.getLogger("Beagle.context_reporter")

# ── Constants ──────────────────────────────────────────────────────────────

_REPORT_DIR = Path(os.environ.get("BEAGLE_STATE_DIR", str(Path.home() / ".beagle")))
_REPORT_PATH = _REPORT_DIR / "context_report.json"
_STALENESS_SECONDS = 300  # reports older than 5 min are ignored


def write_report(
    percentage: float,
    used_tokens: int = 0,
    max_tokens: int = 0,
    source: str = "goose",
    *,
    subscriber_verified: bool | None = None,
    diagnostics: dict[str, Any] | None = None,
) -> None:
    """Write a context utilization report for Beagle's monitor to read.

    Called by the goose agent (or an extension hook) after each turn
    to report actual context window utilization.

    This function is the single writer of the context report file.  It
    emits schema version 2.  A consumer that dereferences a key this
    function does not emit is reading a file written by a producer this
    code does not know.

    Args:
        percentage: Fraction of context window used (0.0-1.0).
        used_tokens: Approximate tokens used (optional, derived if 0).
        max_tokens: Maximum context window tokens (optional).
        source: Origin of the report (default "goose").
        subscriber_verified: Whether the token-counter subscriber is
            subscribed (optional diagnostic).
        diagnostics: Optional diagnostic keys merged into the report
            after the fixed keys (e.g. events_seen, fires_triggered).

    """
    # Clamp percentage
    percentage = max(0.0, min(1.0, float(percentage)))

    # Derive token counts if not supplied
    if max_tokens <= 0:
        from beagle.config.config import get_config

        max_tokens = get_config().context_threshold.max_tokens

    if used_tokens <= 0:
        used_tokens = int(percentage * max_tokens)

    report: dict[str, Any] = {
        "schema_version": 2,
        "percentage": round(percentage, 4),
        "used_tokens": used_tokens,
        "max_tokens": max_tokens,
        "timestamp": datetime.now(UTC).isoformat(),
        "source": source,
    }
    if subscriber_verified is not None:
        report["subscriber_verified"] = subscriber_verified
    if diagnostics is not None:
        report.update(diagnostics)

    _REPORT_DIR.mkdir(parents=True, exist_ok=True)

    try:
        atomic_write_text(_REPORT_PATH, json.dumps(report, indent=2), mode=0o600)
    except OSError as exc:
        logger.debug("Failed to write context report: %s", exc)


def read_report() -> dict[str, Any] | None:
    """Read the latest context utilization report.

    Returns None if the file does not exist, is unreadable, or is stale
    (older than _STALENESS_SECONDS).  This prevents a zombie report
    from a previous session from triggering compaction in a new one.

    Returns:
        Report dict with keys: percentage, used_tokens, max_tokens,
        timestamp, source.  None if unavailable or stale.

    """
    if not _REPORT_PATH.exists():
        return None

    try:
        raw = _REPORT_PATH.read_text()
        loaded = json.loads(raw)
        report: dict[str, Any] = loaded if isinstance(loaded, dict) else {}
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        logger.debug("Failed to read context report: %s", exc)
        return None

    # Version gate — a file with no schema_version came from a producer
    # this code does not know.  Reject it rather than guess its keys.
    if report.get("schema_version") is None:
        logger.warning(
            "Context report at %s has no schema_version; refusing to read it",
            _REPORT_PATH,
        )
        return None

    # Staleness check — ignore if older than threshold
    try:
        ts = datetime.fromisoformat(report.get("timestamp", ""))
        age = (datetime.now(UTC) - ts).total_seconds()
        if age > _STALENESS_SECONDS:
            logger.debug(
                "Context report is %.0f s old (stale threshold: %d s), ignoring",
                age,
                _STALENESS_SECONDS,
            )
            return None
    except (ValueError, TypeError):
        # Malformed timestamp — treat as stale
        return None

    return report


def get_context_usage_from_report() -> tuple[int, int] | None:
    """Convenience: return (used_tokens, max_tokens) from the report.

    Returns None if no valid report is available.
    """
    report = read_report()
    if report is None:
        return None

    used = int(report.get("used_tokens", 0))
    max_tok = int(report.get("max_tokens", 0))
    if max_tok <= 0:
        return None

    # Recalculate used from percentage if needed
    if used <= 0:
        pct = float(report.get("percentage", 0))
        used = int(pct * max_tok)

    return used, max_tok


def build_goose_instruction_snippet() -> str:
    """DEPRECATED (v13.14.6): Use MCP tools instead.

    The system prompt directive #6 now mandates
    beagleutilityserver__report_context_usage and
    beagleutilityserver__check_and_fold_context for context reporting.
    These bash-instruction builders are dead code retained only for
    reference; they will be removed in v13.15.0.

    Returns:
        Instruction string (~200 tokens).

    """
    return (
        "## Context Reporting (Automatic)\n"
        "After EVERY tool call, estimate your context window utilization.\n"
        "Then call the shell tool to write it:\n"
        "```bash\n"
        'python3 -c "from beagle.context.context_reporter '
        'import write_report; write_report(0.59)"  # replace 0.59 with '
        "actual fraction\n"
        "```\n"
        "Use the percentage shown at the bottom of each message "
        "('.e.g, ~59% context used'). This lets Beagle's ContextMonitor "
        "detect when compaction is needed and save checkpoints proactively.\n"
    )


def build_goose_instruction_snippet_bash() -> str:
    """DEPRECATED (v13.14.6): Use MCP tools instead. See build_goose_instruction_snippet().

    Alternative version that writes the JSON directly — works even if
    the goose venv isn't on PATH.

    Returns:
        Instruction string (~150 tokens).

    """
    return (
        "## Context Reporting (Automatic)\n"
        "After EVERY tool call, write your context utilization:\n"
        "```bash\n"
        'mkdir -p ~/.beagle && echo \'{"percentage":0.59,'
        '"used_tokens":75264,"max_tokens":128000,'
        '"timestamp":"$(date -u +%Y-%m-%dT%H:%M:%SZ)",'
        '"source":"goose"}\' > ~/.beagle/context_report.json\n'
        "```\n"
        "Replace 0.59 with the actual fraction from the context bar.\n"
        "This enables Beagle's ContextMonitor to trigger compaction "
        "at the right time.\n"
    )
