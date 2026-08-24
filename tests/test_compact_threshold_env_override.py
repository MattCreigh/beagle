"""v13.15.3 regression: GOOSE_AUTO_COMPACT_THRESHOLD must override the TOML
fallback for the compaction threshold. Prior to v13.15.3 the env var was
documented in CLAUDE.md as required but was never read anywhere in the
codebase, so operators who set it expecting it to take effect were silently
running on the TOML default.
"""

from __future__ import annotations

import pytest

from beagle.config.schema import ContextThresholdConfig
from beagle.context.context_compaction_hook import ContextStatus


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("GOOSE_AUTO_COMPACT_THRESHOLD", raising=False)
    yield


def test_no_env_var_falls_back_to_toml_default():
    cfg = ContextThresholdConfig()
    assert cfg.effective_compact == cfg.compact


def test_valid_env_var_overrides_toml(monkeypatch):
    monkeypatch.setenv("GOOSE_AUTO_COMPACT_THRESHOLD", "0.7")
    cfg = ContextThresholdConfig()
    assert cfg.effective_compact == pytest.approx(0.7)


def test_unparseable_env_var_falls_back_silently(monkeypatch):
    """Invalid env var values must NOT crash — fall back to TOML."""
    monkeypatch.setenv("GOOSE_AUTO_COMPACT_THRESHOLD", "not-a-number")
    cfg = ContextThresholdConfig()
    assert cfg.effective_compact == cfg.compact


def test_out_of_range_env_var_falls_back(monkeypatch):
    """Values outside (0.0, 1.0) are nonsensical and must be rejected."""
    cfg = ContextThresholdConfig()
    for bad in ("1.5", "-0.2", "0.0", "1.0"):
        monkeypatch.setenv("GOOSE_AUTO_COMPACT_THRESHOLD", bad)
        assert cfg.effective_compact == cfg.compact, f"failed for {bad!r}"


def test_status_should_compact_respects_env_override(monkeypatch):
    """The ContextStatus.should_compact property must use effective_compact,
    not the raw TOML compact field. This is the load-bearing assertion —
    the actual decision point in the orchestrator's compaction logic.
    """
    # TOML default 0.65; env set to 0.7. A status at 67% should NOT compact.
    monkeypatch.setenv("GOOSE_AUTO_COMPACT_THRESHOLD", "0.7")
    status = ContextStatus(
        used_tokens=67_000, max_tokens=100_000, percentage=0.67, warning_level="warning"
    )
    assert status.should_compact is False, (
        "Compaction fired below env-var-pinned threshold — env override not respected"
    )

    # And at 72% it SHOULD compact (above env-var threshold).
    status_high = ContextStatus(
        used_tokens=72_000, max_tokens=100_000, percentage=0.72, warning_level="compact"
    )
    assert status_high.should_compact is True
