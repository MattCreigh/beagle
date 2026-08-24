"""Unit tests for the Memory Upload Procedure (tide.comet.amber, D7).

These tests are isolated: they pass an explicit ``corpus_dir`` (tmp_path) so no
real memory store, RAG index, or MCP server is touched. The hierarchical-store
and RAG-ingest paths are best-effort and lazily imported, so they no-op cleanly
in the test environment without mocking.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from beagle.memory.memory_upload import (
    MemoryUploadConfig,
    MemoryUploader,
    distill,
    jaccard_similarity,
    redact,
    significance_score,
)


def _cfg(tmp_path: Path) -> MemoryUploadConfig:
    return MemoryUploadConfig(corpus_dir=tmp_path / "uploads", dedup_enabled=True)


# ── significance ──────────────────────────────────────────────────────────────


def test_significance_rewards_decision_rationale():
    high = "We decided to use mtime because reading every file is too slow; this is a constraint."
    low = "running tests now"
    assert significance_score(high) > significance_score(low)


def test_significance_penalises_filler():
    assert significance_score("ok thanks got it") < 0.3


# ── redaction (secrets gate, R4) ──────────────────────────────────────────────


def test_redact_scrubs_bearer_token():
    scrubbed, still = redact("auth header is Bearer abcdef0123456789ABCDEF")
    assert "abcdef0123456789ABCDEF" not in scrubbed
    assert still is False


def test_redact_flags_private_key():
    scrubbed, still = redact(
        "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----"
    )
    # Whether scrubbed or not, the still-matches signal must be honoured by upload.
    assert isinstance(still, bool)
    assert "[REDACTED]" in scrubbed or still is True


# ── dedup ─────────────────────────────────────────────────────────────────────


def test_jaccard_identical_is_one():
    assert jaccard_similarity("alpha beta gamma", "alpha beta gamma") == pytest.approx(1.0)


def test_jaccard_disjoint_is_zero():
    assert jaccard_similarity("alpha beta", "delta epsilon") == 0.0


# ── distillation ──────────────────────────────────────────────────────────────


def test_distill_caps_at_max_points():
    cfg = MemoryUploadConfig(max_points_per_upload=2, min_score=0.0, min_chars=10)
    raw = "\n\n".join(
        f"We must always remember decision number {i} because the rationale matters here."
        for i in range(6)
    )
    points = distill(raw, cfg)
    assert len(points) <= 2


def test_distill_drops_low_significance():
    cfg = MemoryUploadConfig(min_score=0.6, min_chars=5)
    points = distill("ok\n\nthanks\n\ngot it", cfg)
    assert points == []


# ── end-to-end upload (isolated corpus dir) ───────────────────────────────────


def test_remember_writes_corpus_file(tmp_path: Path):
    uploader = MemoryUploader(_cfg(tmp_path))
    result = uploader.remember(
        "We always prefer the canonical scrubber because a partial redaction is a leak.",
        source="unit-test",
    )
    assert result.uploaded, f"expected an upload, got rejections: {result.rejected}"
    assert result.corpus_paths
    written = Path(result.corpus_paths[0])
    assert written.exists()
    body = written.read_text(encoding="utf-8")
    assert "workstream=tide.comet.amber" in body
    assert "uploaded_at_utc" in body


def test_remember_drops_unredactable_secret(tmp_path: Path):
    cfg = _cfg(tmp_path)
    cfg.min_score = 0.0
    cfg.min_chars = 5
    uploader = MemoryUploader(cfg)
    # A bare private-key marker that survives scrubbing must be dropped, not stored.
    result = uploader.remember("-----BEGIN OPENSSH PRIVATE KEY-----", source="unit-test")
    assert not result.uploaded
    assert any("secret" in reason for _, reason in result.rejected)


def test_remember_dedups_against_existing_corpus(tmp_path: Path):
    uploader = MemoryUploader(_cfg(tmp_path))
    text = (
        "We must always commit before deleting because the reversion path requires a pushed commit."
    )
    first = uploader.remember(text, source="unit-test")
    assert first.uploaded
    second = uploader.remember(text, source="unit-test")
    assert not second.uploaded
    assert any("duplicate" in reason for _, reason in second.rejected)
