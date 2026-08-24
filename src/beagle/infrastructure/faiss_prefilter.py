"""Faiss IVF-PQ Pre-filter for accelerated RAG vector search.

Provides approximate nearest-neighbor (ANN) search via Faiss as a front-end
to LanceDB. Retrieves top-K candidates quickly via Faiss, then re-ranks
with full-precision LanceDB + Kùzu for final results.

If Faiss is not installed, gracefully degrades to pure LanceDB search.

Usage:
    from beagle.infrastructure.faiss_prefilter import FaissPrefilter

    pf = FaissPrefilter(dimension=768)
    pf.build_index(vectors, ids)
    candidate_ids, distances = pf.search(query_vector, top_k=100)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("Beagle.faiss_prefilter")

_FAISS_AVAILABLE: bool = False
try:
    import faiss

    _FAISS_AVAILABLE = True
    logger.info("[Faiss] faiss-cpu available — ANN pre-filter enabled")
except ImportError:
    logger.debug("[Faiss] faiss-cpu not installed — using pure LanceDB search")


@dataclass
class FaissSearchResult:
    """Result from Faiss ANN search."""

    ids: list[str]
    distances: list[float]
    used_faiss: bool


class FaissPrefilter:
    """Faiss IVF-PQ index for approximate nearest-neighbor search.

    Builds an IVF-PQ index from LanceDB vectors for faster top-K retrieval.
    Returns candidate IDs that are then re-ranked with full-precision LanceDB.

    Falls back to passthrough when Faiss is unavailable.
    """

    def __init__(self, dimension: int = 768, nlist: int = 100, nprobe: int = 10) -> None:
        """Initialize the Faiss pre-filter.

        Args:
            dimension: Vector dimension (must match LanceDB embeddings).
            nlist: Number of IVF cells (clusters).
            nprobe: Number of cells to probe at query time.

        """
        self.dimension = dimension
        self.nlist = nlist
        self.nprobe = nprobe
        self._index: Any = None
        self._id_map: dict[int, str] = {}  # faiss internal id → ast_entity_id
        self._built = False

    @property
    def available(self) -> bool:
        """Check if Faiss is available."""
        return _FAISS_AVAILABLE

    def build_index(self, vectors: list[list[float]], ids: list[str]) -> bool:
        """Build the IVF-PQ index from vectors and their IDs.

        Args:
            vectors: List of embedding vectors.
            ids: Corresponding AST entity IDs.

        Returns:
            True if index was built successfully.

        """
        if not _FAISS_AVAILABLE:
            logger.debug("[Faiss] Skipping index build — faiss not available")
            return False

        if len(vectors) < self.nlist:
            # Too few vectors for IVF — use flat index instead
            logger.info(f"[Faiss] Only {len(vectors)} vectors — using FlatL2 index")
            return self._build_flat(vectors, ids)

        try:
            import numpy as np

            vecs = np.array(vectors, dtype=np.float32)
            n = vecs.shape[0]

            # IVF-PQ: Inverted file with product quantization
            m = 48  # Number of subquantizers (must divide dimension)
            if self.dimension % m != 0:
                m = 32
            if self.dimension % m != 0:
                m = 16
            if self.dimension % m != 0:
                m = 8

            quantizer = faiss.IndexFlatL2(self.dimension)
            self._index = faiss.IndexIVFPQ(quantizer, self.dimension, self.nlist, m, 8)
            self._index.nprobe = self.nprobe
            self._index.train(vecs)
            self._index.add(vecs)

            self._id_map = dict(enumerate(ids))
            self._built = True
            logger.info(f"[Faiss] IVF-PQ index built: {n} vectors, nlist={self.nlist}, m={m}")
            return True

        except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional — FAISS boundary: the C++ bindings raise bare RuntimeError and are not enumerable
            logger.warning(f"[Faiss] Failed to build IVF-PQ index: {e} — falling back to flat")
            return self._build_flat(vectors, ids)

    def _build_flat(self, vectors: list[list[float]], ids: list[str]) -> bool:
        """Build a flat L2 index (no quantization, exact search)."""
        if not _FAISS_AVAILABLE:
            return False
        try:
            import numpy as np

            vecs = np.array(vectors, dtype=np.float32)
            self._index = faiss.IndexFlatL2(self.dimension)
            self._index.add(vecs)
            self._id_map = dict(enumerate(ids))
            self._built = True
            logger.info(f"[Faiss] FlatL2 index built: {len(vectors)} vectors")
            return True
        except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional — FAISS boundary: the C++ bindings raise bare RuntimeError and are not enumerable
            logger.warning(f"[Faiss] Failed to build flat index: {e}")
            return False

    def search(self, query_vector: list[float], top_k: int = 100) -> FaissSearchResult:
        """Search the Faiss index for approximate nearest neighbors.

        Args:
            query_vector: Query embedding vector.
            top_k: Number of candidates to retrieve.

        Returns:
            FaissSearchResult with candidate IDs and distances.

        """
        if not self._built or not _FAISS_AVAILABLE or self._index is None:
            return FaissSearchResult(ids=[], distances=[], used_faiss=False)

        try:
            import numpy as np

            qv = np.array([query_vector], dtype=np.float32)
            distances, indices = self._index.search(qv, min(top_k, self._index.ntotal))

            ids = []
            dists = []
            for rank, idx in enumerate(indices[0]):
                if idx < 0:
                    continue
                eid = self._id_map.get(int(idx))
                if eid is not None:
                    ids.append(eid)
                    dists.append(float(distances[0][rank]))

            return FaissSearchResult(ids=ids, distances=dists, used_faiss=True)

        except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional — FAISS boundary: the C++ bindings raise bare RuntimeError and are not enumerable
            logger.warning(f"[Faiss] Search failed: {e}")
            return FaissSearchResult(ids=[], distances=[], used_faiss=False)

    def reset(self) -> None:
        """Reset the index."""
        self._index = None
        self._id_map = {}
        self._built = False


def is_faiss_available() -> bool:
    """Check if Faiss is available for use."""
    return _FAISS_AVAILABLE


__all__ = ["FaissPrefilter", "FaissSearchResult", "is_faiss_available"]
