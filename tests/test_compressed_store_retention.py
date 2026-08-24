"""Tests for CompressedStore retention policy — capacity-based eviction with min-age guard.

Validates:
- Under-cap: no eviction
- Over-cap, all old: evict oldest first
- Over-cap, all young: no eviction (min-age guard)
- Over-cap, mixed: partial eviction (old evicted, young protected)
- Eviction logs at INFO level
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import pytest

from beagle.context.compressed_store import (
    DEFAULT_MAX_FOLDS,
    DEFAULT_MIN_AGE_SECONDS,
    CompressedStore,
    reset_compressed_store,
)


@pytest.fixture(autouse=True)
def _reset():
    reset_compressed_store()
    yield
    reset_compressed_store()


@pytest.fixture()
def store_dir(tmp_path):
    """Isolated temp directory for fold data."""
    folds = tmp_path / "folds"
    folds.mkdir()
    return folds


def _create_fold(store_dir: Path, fold_id: str, mtime: float) -> None:
    """Create a minimal fold sidecar pair (manifest + embeddings).

    Args:
        mtime: The modification time to set on the files. Lower values
            mean the fold is *older*.
    """
    import os

    manifest = store_dir / f"{fold_id}_manifest.json"
    emb = store_dir / f"{fold_id}_embeddings.bin"
    manifest.write_text(json.dumps({"fold_id": fold_id, "n_chunks": 1}))
    emb.write_bytes(b"\x00" * 16)
    os.utime(manifest, (mtime, mtime))
    os.utime(emb, (mtime, mtime))


# ── Under cap: no eviction ────────────────────────────────────────────────────


class TestUnderCapNoEviction:
    def test_under_cap_no_eviction(self, store_dir):
        """100 folds with cap=200: no evictions."""
        now = time.time()
        for i in range(100):
            _create_fold(store_dir, f"fold{i:010d}", now - 100000 - i * 100)

        store = CompressedStore(store_dir=store_dir, max_folds=200, min_age_seconds=10)
        removed = store.cleanup_old_folds()
        assert removed == 0
        manifests = list(store_dir.glob("*_manifest.json"))
        assert len(manifests) == 100


# ── Over cap, all old: evict oldest first ──────────────────────────────────────


class TestOverCapEvictsOldest:
    def test_over_cap_evicts_oldest(self, store_dir):
        """250 folds all >24h old, cap=200: exact 50 evicted, oldest first."""
        now = time.time()
        for i in range(250):
            # i=0 gets mtime now-90000 (oldest in real terms gets lowest i)
            # i=249 gets mtime now-90000-249*60 (even older)
            # Lower mtime = older → i=249 is the very oldest
            mtime = now - 90000 - (249 - i) * 60
            _create_fold(store_dir, f"fold{i:010d}", mtime)

        store = CompressedStore(store_dir=store_dir, max_folds=200, min_age_seconds=86400)
        removed = store.cleanup_old_folds()
        assert removed == 50

        manifests = list(store_dir.glob("*_manifest.json"))
        assert len(manifests) == 200

        # The 50 OLDEST (lowest mtime) should be gone.
        # fold0 has lowest mtime (oldest), fold249 has highest (newest).
        # So folds 0-49 get evicted, folds 50-249 survive.
        surviving_ids = sorted(p.stem.replace("_manifest", "") for p in manifests)
        assert "fold0000000050" in surviving_ids
        assert "fold0000000249" in surviving_ids
        assert "fold0000000000" not in surviving_ids
        assert "fold0000000049" not in surviving_ids


# ── Over cap, all young: min-age guard protects ────────────────────────────────


class TestMinAgeGuardProtectsYoungFolds:
    def test_all_young_no_eviction(self, store_dir):
        """250 folds all <24h old, cap=200: no evictions due to min-age guard."""
        now = time.time()
        for i in range(250):
            _create_fold(store_dir, f"fold{i:010d}", now - i * 60)

        store = CompressedStore(store_dir=store_dir, max_folds=200, min_age_seconds=86400)
        removed = store.cleanup_old_folds()
        assert removed == 0

        manifests = list(store_dir.glob("*_manifest.json"))
        assert len(manifests) == 250


# ── Over cap, mixed ages: partial eviction ────────────────────────────────────


class TestMinAgeGuardPartialEviction:
    def test_mixed_old_young(self, store_dir):
        """100 old + 150 young, cap=200: evict 50 old, all 150 young survive."""
        now = time.time()

        # 100 folds that are 25h+ old (eligible)
        for i in range(100):
            _create_fold(store_dir, f"old{i:010d}", now - 90000 - i * 60)

        # 150 folds that are <1h old (protected)
        for i in range(150):
            _create_fold(store_dir, f"new{i:010d}", now - i * 10)

        store = CompressedStore(store_dir=store_dir, max_folds=200, min_age_seconds=86400)
        removed = store.cleanup_old_folds()

        # Total 250, cap 200 → need to evict 50
        # Only 100 are eligible (old), so evict 50 of those
        assert removed == 50

        # Verify young folds survived
        young_manifests = list(store_dir.glob("new*_manifest.json"))
        assert len(young_manifests) == 150

        # Verify oldest old folds were evicted (50 old→50 remaining)
        old_manifests = list(store_dir.glob("old*_manifest.json"))
        assert len(old_manifests) == 50


# ── Eviction logs at INFO level ────────────────────────────────────────────────


class TestEvictionLogsAtInfoLevel:
    def test_eviction_logged_at_info(self, store_dir, caplog):
        """Each eviction should produce an INFO-level log."""
        now = time.time()
        for i in range(5):
            _create_fold(store_dir, f"fold{i:010d}", now - 100000 - i * 60)

        store = CompressedStore(store_dir=store_dir, max_folds=2, min_age_seconds=10)
        with caplog.at_level(logging.INFO, logger="Beagle.context.compressed_store"):
            removed = store.cleanup_old_folds()

        assert removed == 3
        info_logs = [
            r for r in caplog.records if r.levelno == logging.INFO and "Evicted fold" in r.message
        ]
        assert len(info_logs) == 3


# ── Defaults are correct ──────────────────────────────────────────────────────


class TestDefaults:
    def test_default_max_folds_is_200(self):
        assert DEFAULT_MAX_FOLDS == 200

    def test_default_min_age_is_24h(self):
        assert DEFAULT_MIN_AGE_SECONDS == 86400

    def test_store_picks_up_defaults(self, store_dir):
        store = CompressedStore(store_dir=store_dir)
        assert store._max_folds == 200
        assert store._min_age_seconds == 86400
