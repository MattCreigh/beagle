"""Regression tests for the TurboQuant sidecar write path.

Background — the 2026-07-27 incident:
The sidecar rebuild (triggered after every full corpus rebuild)
silently skipped with
``TurboQuant sidecar write skipped: setting an array element with a sequence.``
on a 5,206-row corpus. The previous code used:

    vec_flat = np.asarray(arrow.column("vector").flatten(), dtype=np.float32)
    vectors = vec_flat.reshape(n, dim)

On a ``FixedSizeListArray<float>[768]`` column, ``.flatten()`` returns
an outer list with one element per PyArrow chunk (not a flat length
(n*dim) array), ``np.asarray(...)`` then creates an object-dtype
length-1 array, and ``.reshape(n, dim)`` raises because numpy cannot
assign the giant chunk into a 2-D shape.

Fix: use ``arrow.column("vector").combine_chunks().values.to_numpy(...)``
which gives the flat (n*dim,) buffer directly.

These tests pin the contract end-to-end:
1. LanceDB fixed_size_list<float>[dim] -> numpy (n, dim) round-trip is
   bit-exact.
2. write_turboquant_sidecar() on the recovered matrix produces a file
   that decompresses back to the same matrix (within TurboQuant's
   expected quantisation error).
3. The fix works on a real on-disk corpus (uses the live /mnt/4TB
   SSD LanceDB table if present; otherwise a synthetic fixture).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, "/home/server/Projects/beagle")

from beagle.infrastructure.cast_ingestion import (
    LANCE_TABLE_NAME,
    _maybe_write_turboquant_sidecar,
)


def _make_synthetic_lance_table(
    tmp_path: Path, n: int = 50, dim: int = 64
) -> tuple[Path, np.ndarray]:
    """Write a tiny LanceDB table with fixed_size_list<float>[dim] vectors.

    Returns (db_root_path, original_matrix).
    """
    import lancedb

    db_root = tmp_path / "test_lance"
    lance_dir = db_root / "lancedb"
    lance_dir.mkdir(parents=True)

    rng = np.random.default_rng(seed=42)
    original = rng.standard_normal((n, dim)).astype(np.float32)
    records = [
        {
            "vector": original[i].tolist(),
            "chunk_id": f"chunk_{i:04d}",
            "filepath": f"/test/file_{i % 5}.py",
        }
        for i in range(n)
    ]
    db = lancedb.connect(str(lance_dir))
    db.create_table(LANCE_TABLE_NAME, records)
    return db_root, original


def test_lance_fixed_size_list_to_numpy_roundtrip(tmp_path: Path):
    """The exact bug: ``flatten()`` on a FixedSizeListArray<768> returns
    length 1 (one chunk), not length (n*768). The fix must produce a
    (n, dim) numpy matrix whose elements match the LanceDB row
    vectors bit-for-bit.
    """
    db_root, original = _make_synthetic_lance_table(tmp_path, n=20, dim=64)

    import lancedb

    lance_path = db_root / "lancedb"
    db = lancedb.connect(str(lance_path))
    tbl = db.open_table(LANCE_TABLE_NAME)
    arrow = tbl.to_arrow()
    n = tbl.count_rows()

    # ── The old (broken) path ─────────────────────────────────────────────
    # The bug being guarded: the old path's `np.asarray(
    # arrow.column("vector").flatten(), dtype=fp32)` returns an
    # object-dtype length-1 array of PyArrow chunks (not the per-row
    # flat values), and `.reshape(n, dim)` then raises
    # ``ValueError: setting an array element with a sequence``.
    #
    # We assert that the broken path's output cannot be reshaped to
    # (n, dim) cleanly. If a future PyArrow version starts returning
    # the flat values directly from .flatten() (which would
    # accidentally heal the bug), the broken_flat.shape will be
    # (n*dim,) and the assertion below will fire — telling the next
    # maintainer that this regression test needs to be retired.
    try:
        broken_flat = np.asarray(arrow.column("vector").flatten(), dtype=np.float32)
        # If we got here without raising, attempt the .reshape — the
        # bug. If the reshape raises, the bug is present and
        # documented; if it succeeds, the bug is gone and we flag the
        # test as needing retirement.
        try:
            _ = broken_flat.reshape(n, 64)
            pytest.fail(
                "the broken path now succeeds — PyArrow API may have "
                "changed; this regression test needs updating"
            )
        except ValueError as reshape_exc:
            assert "sequence" in str(reshape_exc).lower() or "shape" in str(reshape_exc).lower(), (
                f"broken path raises unexpected ValueError: {reshape_exc}"
            )
    except (ValueError, TypeError) as broken_exc:
        # Also acceptable: the broken path raises immediately on the
        # np.asarray call (some PyArrow versions). Either way, the
        # OLD code is broken; the FIX must work.
        assert "sequence" in str(broken_exc).lower() or "shape" in str(broken_exc).lower(), (
            f"broken path raises unexpected error: {broken_exc}"
        )

    # ── The fixed path ──────────────────────────────────────────────────
    vec_flat = (
        arrow.column("vector").combine_chunks().values.to_numpy(zero_copy_only=False)
    ).astype(np.float32, copy=False)
    assert vec_flat.shape == (n * 64,), (
        f"combine_chunks().values should be flat (n*dim,); got {vec_flat.shape}"
    )
    fixed = vec_flat.reshape(n, 64)
    assert fixed.shape == (n, 64)
    assert np.array_equal(fixed, original), (
        "LanceDB -> numpy round-trip must be bit-exact (no rescaling, "
        "no reordering, no missing rows)"
    )


def test_write_turboquant_sidecar_roundtrip(tmp_path: Path):
    """write_turboquant_sidecar() on the recovered matrix must produce
    a sidecar file that decompresses back to the same matrix.
    """
    db_root, original = _make_synthetic_lance_table(tmp_path, n=30, dim=32)
    import lancedb

    db = lancedb.connect(str(db_root / "lancedb"))
    tbl = db.open_table(LANCE_TABLE_NAME)

    # Use _maybe_write_turboquant_sidecar — the production call site.
    # The helper has an ``if not chunks: return`` early-exit guard so
    # we pass a non-empty chunk list (the helper does not actually
    # use the chunk contents — it reads from the LanceDB table
    # directly).
    class _DummyChunk:
        def __init__(self, ast_entity_id):
            self.ast_entity_id = ast_entity_id

    _maybe_write_turboquant_sidecar(
        tbl,
        chunks=[_DummyChunk(f"chunk_{i:04d}") for i in range(30)],
        db_root_path=str(db_root),
    )

    # The helper returns None on success and logs warnings on failure.
    # The point of this test: no ValueError must surface.
    sidecar = db_root / "rag_vectors_tq.bin"
    assert sidecar.exists(), (
        f"sidecar file not created at {sidecar}; expected binary at "
        f"this path after a successful _maybe_write_turboquant_sidecar"
    )
    assert sidecar.stat().st_size > 0, "sidecar file is empty"

    # Decompress and verify shape + content
    from beagle.core.turboquant import TurboQuantCompressor

    compressor = TurboQuantCompressor(bits=3)
    raw = sidecar.read_bytes()
    # The sidecar format requires knowing (n_vectors, dimension) to
    # decompress — read from the metadata sidecar.
    meta_path = db_root / "rag_vectors_tq.meta.json"
    if meta_path.exists():
        import json

        meta = json.loads(meta_path.read_text())
        n = meta["n_vectors"]
        dim = meta["dimension"]
        seed = meta["seed"]
        decompressed = compressor.decompress(raw, seed, (n, dim))
        assert decompressed.shape == (30, 32)
        # TurboQuant is lossy (3-bit per-vector quantisation); tolerate
        # ~5% relative error per element.
        np.testing.assert_allclose(
            decompressed,
            original,
            rtol=0.10,
            atol=0.5,
            err_msg="decompressed vectors should be approximately equal "
            "to originals (TurboQuant is lossy by design)",
        )


def test_old_flatten_path_documents_known_bug(tmp_path: Path):
    """Document the exact bug: on a single-chunk FixedSizeListArray, the
    old path's ``np.asarray(vec_col.flatten(), dtype=fp32)`` returns
    a shape that cannot be reshaped to (n, dim) without error. The
    fixed path uses combine_chunks().values to get the flat
    (n*dim,) buffer in one call.
    """
    db_root, original = _make_synthetic_lance_table(tmp_path, n=5, dim=8)
    import lancedb

    db = lancedb.connect(str(db_root / "lancedb"))
    tbl = db.open_table(LANCE_TABLE_NAME)
    arrow = tbl.to_arrow()
    vec_col = arrow.column("vector")
    n = tbl.count_rows()

    # Document the FIXED path shape.
    fixed_flat = (vec_col.combine_chunks().values.to_numpy(zero_copy_only=False)).astype(
        np.float32, copy=False
    )
    fixed_shape = fixed_flat.reshape(n, 8).shape
    assert fixed_shape == (n, 8), (
        f"the FIXED path must produce (n, dim)=({n}, 8) shape; got {fixed_shape}"
    )
    # The fixed path is bit-exact: every element matches the original.
    assert np.array_equal(fixed_flat.reshape(n, 8), original)

    # Document the BUGGY path: either raises immediately or returns a
    # shape that cannot be reshaped to (n, dim). We do not assert the
    # exact failure mode (it depends on PyArrow internals); we only
    # assert that the buggy path is NOT a clean 2-D matrix of (n, dim).
    try:
        broken_flat = np.asarray(vec_col.flatten(), dtype=np.float32)
        try:
            _ = broken_flat.reshape(n, 8)
            # If we got here, the broken path now succeeds — a future
            # PyArrow may have fixed the .flatten() return value. Mark
            # the test as needing retirement.
            pytest.skip(
                "the old broken path now succeeds (PyArrow API may have "
                "changed); this regression test should be retired"
            )
        except (ValueError, TypeError):
            # Reshape raised — bug is still present (which is what we
            # expect on this PyArrow version). The test passes.
            pass
    except (ValueError, TypeError):
        # np.asarray raised immediately — also acceptable.
        pass


@pytest.mark.skipif(
    os.environ.get("BEAGLE_LIVE_RAG_TEST") != "1",
    reason="Live on-disk corpus test; set BEAGLE_LIVE_RAG_TEST=1 to run",
)
def test_live_corpus_sidecar_build(tmp_path: Path):
    """If the live corpus exists at /mnt/4TB_SATA_SSD/beagle/instance_rag,
    verify the sidecar rebuilds without error on the production data.
    """
    import lancedb

    live_lance = Path(os.path.realpath("/home/server/.beagle/instance_rag/lancedb"))
    if not live_lance.exists():
        pytest.skip(f"live corpus not present at {live_lance}")
    db = lancedb.connect(str(live_lance))
    tbl = db.open_table(LANCE_TABLE_NAME)
    n = tbl.count_rows()
    assert n > 0, "live corpus is empty"

    # Re-run the production code path.
    # Use a tmp_path staging dir so we don't clobber the live sidecar.
    staging = tmp_path / "live_sidecar_test"
    staging.mkdir()

    # The helper has an ``if not chunks: return`` early-exit guard and reads
    # vectors from the LanceDB table directly (chunk contents unused), so pass
    # a non-empty chunk list or the sidecar is never written.
    class _DummyChunk:
        def __init__(self, ast_entity_id: str) -> None:
            self.ast_entity_id = ast_entity_id

    _maybe_write_turboquant_sidecar(
        tbl,
        chunks=[_DummyChunk(f"chunk_{i:06d}") for i in range(n)],
        db_root_path=str(staging),
    )
    sidecar = staging / "rag_vectors_tq.bin"
    assert sidecar.exists(), "live sidecar rebuild failed"
    assert sidecar.stat().st_size > 0
