"""Round-trip tests for the context report file (schema version 2).

The context report has exactly one writer (``context_reporter.write_report``)
and one schema.  These tests pin the contract that the compaction hook and
``read_report()`` both dereference: percentage, used_tokens, max_tokens.

A file with no ``schema_version`` came from a producer this code does not
know, so ``read_report()`` must reject it rather than guess its keys.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


@pytest.fixture
def isolated_report(tmp_path, monkeypatch):
    """Point the reporter at a temp state dir so tests don't touch ~/.beagle.

    Returns the reloaded module so tests exercise the module-level
    ``_REPORT_PATH`` that the env override produced.
    """
    monkeypatch.setenv("BEAGLE_STATE_DIR", str(tmp_path))
    # Re-import so the module-level _REPORT_DIR picks up the env override.
    import importlib

    from beagle.context import context_reporter

    importlib.reload(context_reporter)
    return context_reporter


def test_write_report_roundtrip_contains_hook_keys(isolated_report):
    """Every key auto_compact.py dereferences must survive a round trip."""
    write_report = isolated_report.write_report
    read_report = isolated_report.read_report

    write_report(percentage=0.42, used_tokens=42000, max_tokens=100000, source="selftest")
    r = read_report()
    assert r is not None, "read_report returned None on a file it just wrote"
    assert r["schema_version"] == 2
    for key in ("percentage", "used_tokens", "max_tokens"):
        assert key in r, f"missing {key}"


def test_write_report_roundtrip_subscriber_diagnostics(isolated_report):
    """The subscriber's diagnostic keys survive a round trip."""
    write_report = isolated_report.write_report
    read_report = isolated_report.read_report

    write_report(
        percentage=0.61,
        used_tokens=61000,
        max_tokens=100000,
        source="token_counter_subscriber",
        diagnostics={
            "subscriber_verified": True,
            "events_seen": 3,
            "fires_triggered": 1,
        },
    )
    r = read_report()
    assert r is not None
    assert r["source"] == "token_counter_subscriber"
    assert r["percentage"] == 0.61
    assert r["used_tokens"] == 61000
    assert r["subscriber_verified"] is True
    assert r["events_seen"] == 3
    assert r["fires_triggered"] == 1


def test_read_report_rejects_versionless_file(isolated_report, tmp_path):
    """A file with no schema_version must be rejected, not guessed."""
    read_report = isolated_report.read_report

    target = tmp_path / "context_report.json"
    target.write_text(
        json.dumps(
            {
                "utilization": 0.30,
                "current_tokens": 30000,
                "max_tokens": 100000,
            }
        )
    )
    assert read_report() is None
