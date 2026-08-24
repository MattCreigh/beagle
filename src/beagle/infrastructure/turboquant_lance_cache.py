"""TurboQuant sidecar cache for the LanceDB RAG index.

Why this exists
----------------

The default LanceDB RAG index stores raw 768-dim float32 vectors: 3,072 bytes
per vector. A 30k-chunk corpus is 92 MB on disk and 92 MB in RAM when loaded
for search. Fits within typical memory-constrained deployments, but
"barely" — the sentence-transformers embedder (1.2 GB) plus a full-corpus
numpy matrix plus the Kùzu buffer pool leave little headroom for the rest
of the MCP server.

TurboQuant's 3-bit quantization compresses the same 30k vectors to ~30 MB
on disk and ~30 MB in RAM. Search then becomes a brute-force numpy cosine
on the decompressed matrix (fast for 30k, <100 ms).

The trick is that LanceDB itself does NOT support compressed-vector ANN
search — its `.search(vector).distance_type(...).to_list()` C++ core
requires raw float vectors. So:

  - **On write (ingestion)**: After LanceDB writes the raw vectors, also
    write a sidecar file at ``<lancedb_parent>/rag_vectors_tq.bin`` that
    contains the same vectors TurboQuant-compressed with the chunk-id
    lookup table.

  - **On read (search)**: If the sidecar exists, load it (one read of
    ~30 MB), decompress once, embed the query, do numpy cosine
    brute-force. If it does NOT exist (e.g. legacy index), fall back to
    LanceDB's built-in search.

The sidecar uses the same TurboQuantCompressor that powers the context
fold compression (``compressed_store.py``), so the format and algorithm
match what the rest of the codebase already trusts.

v13.22.3 — initial wiring. The cache key is the (db_root, lance_table)
tuple, so each RAG index gets its own sidecar without collisions.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger("Beagle.infrastructure.turboquant_lance_cache")

# Default bit depth — 3 bits is the documented TurboQuant setting
# (5.3x compression on 768-dim embeddings, ~2-5% recall loss).
DEFAULT_BITS = 3

# Sidecar filename, lives in the lance table parent dir.
SIDECAR_BIN = "rag_vectors_tq.bin"
SIDECAR_META = "rag_vectors_tq.meta.json"


@dataclass
class TurboQuantSidecar:
    """Lazy handle to a TurboQuant-compressed vector sidecar.

    Loaded from disk on first ``get_vectors()`` call. Decompression is
    cached in memory for the lifetime of this object (so a burst of
    queries doesn't re-decompress each time). Memory cost:
    ``n_vectors * dimension * 4`` bytes for the decompressed numpy
    matrix (e.g. 30k * 768 * 4 = 92 MB).
    """

    sidecar_path: Path
    meta: dict[str, Any]
    _decompressed: np.ndarray | None = None

    @property
    def n_vectors(self) -> int:
        return int(self.meta["n_vectors"])

    @property
    def dimension(self) -> int:
        return int(self.meta["dimension"])

    @property
    def bits(self) -> int:
        return int(self.meta.get("bits", DEFAULT_BITS))

    @property
    def seed(self) -> int:
        return int(self.meta["seed"])

    def get_vectors(self) -> np.ndarray:
        """Return the decompressed vector matrix (n_vectors, dimension).

        Cached after the first call so a burst of search queries doesn't
        re-decompress each time. The cache is invalidated when
        ``invalidate()`` is called (used by re-ingest).
        """
        if self._decompressed is not None:
            return self._decompressed
        from beagle.core.turboquant import (
            TurboQuantCompressor,
        )

        compressor = TurboQuantCompressor(bits=self.bits)
        raw = self.sidecar_path.read_bytes()
        # Recreate the (n_vectors, dimension) shape that compress() saw.
        # The compressor stores mins/maxes per vector, not per chunk, so
        # the shape is restored from metadata.
        self._decompressed = compressor.decompress(
            raw,
            self.seed,
            (self.n_vectors, self.dimension),
        )
        return self._decompressed

    def invalidate(self) -> None:
        """Drop the in-RAM decompressed cache (e.g. after a re-ingest)."""
        self._decompressed = None


def _resolve_sidecar_dir(db_root_path: str | None) -> Path | None:
    """Where to put the sidecar next to the lance table.

    Mirrors the layout that ``cast_ingestion`` uses: ``<db_root>/lancedb``
    for the lance table, so the sidecar lives at ``<db_root>/``.
    """
    if db_root_path:
        return Path(db_root_path)
    try:
        from beagle.infrastructure.rag_paths import db_root

        return Path(db_root())
    except ImportError:
        return None


def write_turboquant_sidecar(
    vectors: np.ndarray,
    chunk_ids: list[str],
    db_root_path: str | None = None,
    bits: int = DEFAULT_BITS,
) -> Path | None:
    """Compress ``vectors`` and write them to the sidecar location.

    Args:
        vectors: numpy array of shape (n_vectors, dimension), float32.
        chunk_ids: parallel list of chunk IDs (hex strings) for
            cross-referencing with the LanceDB row IDs.
        db_root_path: explicit RAG db root; falls back to
            ``rag_paths.db_root()`` if None.
        bits: TurboQuant bit depth. 3 is the documented default.

    Returns:
        Path to the sidecar .bin file, or None if the inputs are invalid
        (e.g. empty vectors, dimension 0, or numpy unavailable).

    """
    if vectors is None or len(vectors) == 0:
        logger.warning("[TurboQuant] No vectors to write — skipping sidecar")
        return None
    if len(vectors) != len(chunk_ids):
        logger.error(f"[TurboQuant] vectors ({len(vectors)}) != chunk_ids ({len(chunk_ids)})")
        return None
    sidecar_dir = _resolve_sidecar_dir(db_root_path)
    if sidecar_dir is None:
        logger.error("[TurboQuant] Could not resolve sidecar dir — skipping")
        return None
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    bin_path = sidecar_dir / SIDECAR_BIN
    meta_path = sidecar_dir / SIDECAR_META

    try:
        from beagle.core.turboquant import (
            TurboQuantCompressor,
        )
    except ImportError as e:
        logger.warning(f"[TurboQuant] compressor unavailable: {e} — skipping sidecar")
        return None

    # Cast to float32 — the embedder returns float32 already, but be
    # explicit so the compressor sees the right dtype.
    if vectors.dtype != np.float32:
        vectors = vectors.astype(np.float32)

    compressor = TurboQuantCompressor(bits=bits)
    compressed, seed = compressor.compress(vectors)

    # Atomic write: write to .new then rename. Prevents a partial sidecar
    # if the process dies mid-write.
    tmp_bin = bin_path.with_suffix(".bin.new")
    tmp_meta = meta_path.with_suffix(".meta.json.new")
    try:
        with open(tmp_bin, "wb") as f:
            f.write(compressed)
        meta = {
            "n_vectors": int(vectors.shape[0]),
            "dimension": int(vectors.shape[1]),
            "bits": bits,
            "seed": int(seed),
            "original_dtype": "float32",
            "chunk_ids": chunk_ids,
            "schema_version": 1,
        }
        tmp_meta.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        # Atomic swap.
        os.replace(tmp_bin, bin_path)
        os.replace(tmp_meta, meta_path)
    except Exception:  # broad catch intentional: cleanup best-effort on any write failure
        # Clean up partial files on failure.
        if tmp_bin.exists():
            tmp_bin.unlink(missing_ok=True)
        if tmp_meta.exists():
            tmp_meta.unlink(missing_ok=True)
        raise

    original_size = vectors.nbytes
    compressed_size = bin_path.stat().st_size
    ratio = original_size / compressed_size if compressed_size > 0 else 0.0
    logger.info(
        f"[TurboQuant] Sidecar written: {bin_path.name} "
        f"({vectors.shape[0]} vectors x {vectors.shape[1]}d, "
        f"bits={bits}, seed={seed}, ratio={ratio:.1f}x, "
        f"{compressed_size / 1024 / 1024:.1f} MB on disk)"
    )
    return bin_path


def load_turboquant_sidecar(
    db_root_path: str | None = None,
) -> TurboQuantSidecar | None:
    """Load a sidecar from disk. Returns None if it doesn't exist.

    Does NOT decompress the vectors — that happens lazily on
    ``TurboQuantSidecar.get_vectors()`` so callers can check existence
    cheaply before deciding which path to take.
    """
    sidecar_dir = _resolve_sidecar_dir(db_root_path)
    if sidecar_dir is None:
        return None
    bin_path = sidecar_dir / SIDECAR_BIN
    meta_path = sidecar_dir / SIDECAR_META
    if not bin_path.exists() or not meta_path.exists():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"[TurboQuant] Sidecar metadata unreadable: {e}")
        return None
    return TurboQuantSidecar(sidecar_path=bin_path, meta=meta)


def cosine_search_numpy(
    query_vector: np.ndarray,
    corpus: np.ndarray,
    top_k: int = 5,
) -> list[tuple[int, float]]:
    """Brute-force cosine similarity, returns indices of top-k matches.

    ``query_vector`` is shape (dimension,) or (1, dimension).
    ``corpus`` is shape (n_vectors, dimension). Both are float32.

    Returns:
        List of (index, similarity) tuples, sorted by similarity desc.
        Similarity is in [0, 1] (cosine distance -> similarity via 1-d/2).

    """
    if corpus.size == 0:
        return []
    q = np.asarray(query_vector, dtype=np.float32).reshape(-1)
    q_norm = float(np.linalg.norm(q))
    if q_norm == 0.0:  # exact zero-norm check is intentional
        return []
    c_norms = np.linalg.norm(corpus, axis=1)
    # Avoid divide-by-zero on zero-norm rows in the corpus.
    safe_norms = np.where(c_norms > 0, c_norms, 1.0)
    # Cosine similarity matrix: (n, d) @ (d,) -> (n,).
    sims = (corpus @ q) / (safe_norms * q_norm)
    # Zero out the zero-norm rows so they don't dominate the top-k.
    sims = np.where(c_norms > 0, sims, -np.inf)
    k = min(top_k, len(sims))
    if k == 0:
        return []
    # argpartition is O(n) instead of O(n log n) for full sort.
    top_idx = np.argpartition(-sims, kth=k - 1)[:k]
    # Sort just the top-k for the final order.
    top_idx = top_idx[np.argsort(-sims[top_idx])]
    return [(int(i), float(sims[i])) for i in top_idx]
