"""Embedding Adapter — reusable embedding service for context compression.

Provides a unified interface to the embedding infrastructure used by RAG,
but adapted for context folding/compression workflows:
  - embed_text(text) -> np.ndarray: single text embedding
  - embed_batch(texts) -> np.ndarray: batch embedding
  - similarity(vec_a, vec_b) -> float: cosine similarity

Reuses the OllamaCloudEmbedder / SentenceTransformerEmbedder from
infrastructure.services.embedding but returns numpy arrays (not lists)
for direct pipeline into TurboQuant compression.

Falls back to zero vectors if embedding is unavailable, so context
compression degrades gracefully rather than crashing.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

try:
    import numpy as np

    NUMPY_AVAILABLE = True
except ImportError:
    NUMPY_AVAILABLE = False

try:
    from beagle.infrastructure.services.embedding import (
        get_embedder,
    )

    EMBEDDER_AVAILABLE = True
except ImportError:
    EMBEDDER_AVAILABLE = False

try:
    from beagle.core.turboquant import turboquant_available

    TURBOQUANT_AVAILABLE = turboquant_available()
except ImportError:
    TURBOQUANT_AVAILABLE = False

logger = logging.getLogger("BEAGLE_EmbedAdapter")

# Default embedding dimension (nomic-embed-text and all-mpnet-base-v2 both use 768)
DEFAULT_EMBEDDING_DIM = 768


class EmbeddingAdapter:
    """Unified embedding adapter for context compression pipelines.

    Wraps the existing embedding service (OllamaCloudEmbedder or
    SentenceTransformerEmbedder) and returns numpy arrays suitable
    for direct feeding into TurboQuant compression.

    Falls back to deterministic hash-based pseudo-embeddings when
    the real embedder is unavailable, so the pipeline never crashes
    due to missing embeddings — it just loses similarity quality.
    """

    _instance: EmbeddingAdapter | None = None
    _initialized: bool = False

    def __new__(cls, *args: Any, **kwargs: Any) -> EmbeddingAdapter:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, dimension: int = DEFAULT_EMBEDDING_DIM) -> None:
        if self._initialized and self._dimension == dimension:  # type: ignore[has-type]
            return
        self._dimension = dimension
        self._embedder = None
        self._available = False
        self._init_attempts = 0
        self._max_init_attempts = 3
        self._initialized = True
        self._try_init()

    def _try_init(self) -> None:
        """Attempt to initialize the real embedder (lazy, with retry cap)."""
        if self._available:
            return
        if self._init_attempts >= self._max_init_attempts:
            return
        self._init_attempts += 1

        if not EMBEDDER_AVAILABLE:
            logger.debug("[EmbedAdapter] Embedding service not importable")
            return

        try:
            self._embedder = get_embedder()  # type: ignore[assignment]
            # Quick smoke test: encode a single string
            result = self._embedder.encode(["test"])  # type: ignore[attr-defined]
            if result and len(result[0]) == self._dimension:
                self._available = True
                logger.info(
                    f"[EmbedAdapter] Embedder initialized "
                    f"(dim={self._dimension}, "
                    f"provider={getattr(self._embedder, '_provider', 'unknown')})"
                )
            else:
                logger.warning(
                    f"[EmbedAdapter] Embedder returned wrong dimension: "
                    f"got {len(result[0]) if result else 0}, expected {self._dimension}"
                )
        except Exception as exc:  # ruff: ignore[BLE001]  # broad catch intentional
            logger.debug(
                f"[EmbedAdapter] Embedder init failed (attempt {self._init_attempts}): {exc}"
            )

    @property
    def available(self) -> bool:
        """True if real embeddings are available."""
        return self._available

    @property
    def dimension(self) -> int:
        """Embedding vector dimension."""
        return self._dimension

    def embed_text(self, text: str) -> np.ndarray:
        """Embed a single text string into a numpy array.

        Args:
            text: Text to embed.

        Returns:
            1-D numpy float32 array of shape (dimension,).
            Falls back to deterministic pseudo-embedding if embedder unavailable.

        """
        if NUMPY_AVAILABLE:
            return self.embed_batch([text])[0]  # type: ignore[no-any-return]
        # Shouldn't happen (NumPy is required for TurboQuant)
        raise RuntimeError(
            "NumPy not available — cannot produce embeddings. Install with: pip install numpy"
        )

    def embed_batch(self, texts: list[str]) -> np.ndarray:
        """Embed a batch of texts into numpy arrays.

        Args:
            texts: List of text strings to embed.

        Returns:
            2-D numpy float32 array of shape (len(texts), dimension).
            Falls back to deterministic pseudo-embeddings if embedder unavailable.

        """
        if not texts:
            if NUMPY_AVAILABLE:
                return np.zeros((0, self._dimension), dtype=np.float32)
            return np.array([])

        # Try real embedder
        if self._available and self._embedder is not None:
            try:
                raw = self._embedder.encode(texts)
                if NUMPY_AVAILABLE:
                    return np.array(raw, dtype=np.float32)
            except Exception as exc:  # ruff: ignore[BLE001]  # broad catch intentional
                logger.warning(f"[EmbedAdapter] Real embed failed, using pseudo: {exc}")
                self._available = False  # Mark down for this session

        # Fallback: deterministic pseudo-embeddings from hash
        return self._pseudo_embed_batch(texts)

    def _pseudo_embed_batch(self, texts: list[str]) -> np.ndarray:
        """Generate deterministic pseudo-embeddings from text hash.

        These are NOT semantically meaningful but provide:
        - Consistent vectors for the same text (idempotent)
        - Non-zero output so TurboQuant can compress them
        - Approximate similarity (same text → same vector)

        NOT suitable for semantic search — only for compression pipeline
        continuity when real embeddings are unavailable.
        """
        if not NUMPY_AVAILABLE:
            raise RuntimeError(
                "NumPy required for pseudo-embeddings. Install with: pip install numpy"
            )

        result = np.zeros((len(texts), self._dimension), dtype=np.float32)
        for i, text in enumerate(texts):
            # D11 (Fable 5 DD 2026-06-11): Python's builtin `hash()` is salted
            # per-process since 3.3, so this fallback produced different
            # vectors on every restart — defeating the "deterministic,
            # idempotent" contract. Use hashlib.blake2b (a stable,
            # cross-process, cross-version hash) seeded by the text bytes.
            # Mask the digest to 32 bits because numpy.RandomState only
            # accepts seeds in [0, 2**32 - 1].
            digest = hashlib.blake2b(text.encode("utf-8"), digest_size=8).digest()
            seed = int.from_bytes(digest, "big") & 0xFFFFFFFF
            rng = np.random.RandomState(seed)
            vec = rng.randn(self._dimension).astype(np.float32)
            # Normalize to unit vector (like real embeddings)
            norm = np.linalg.norm(vec)
            if norm > 0:
                vec /= norm
            result[i] = vec
        return result

    @staticmethod
    def similarity(vec_a: np.ndarray, vec_b: np.ndarray) -> float:
        """Compute cosine similarity between two embedding vectors.

        Args:
            vec_a: First embedding vector (1-D).
            vec_b: Second embedding vector (1-D).

        Returns:
            Cosine similarity in [-1, 1]. Returns 0.0 if either vector is zero.

        """
        if not NUMPY_AVAILABLE:
            return 0.0
        norm_a = np.linalg.norm(vec_a)
        norm_b = np.linalg.norm(vec_b)
        if norm_a < 1e-10 or norm_b < 1e-10:
            return 0.0
        return float(np.dot(vec_a, vec_b) / (norm_a * norm_b))

    @staticmethod
    def top_k_by_similarity(
        query_vec: np.ndarray,
        candidate_vecs: np.ndarray,
        k: int = 5,
    ) -> list[tuple[int, float]]:
        """Find top-k candidates by cosine similarity to query.

        Args:
            query_vec: Query embedding (1-D, shape=(dim,)).
            candidate_vecs: Candidate embeddings (2-D, shape=(n, dim)).
            k: Number of top results to return.

        Returns:
            List of (index, similarity_score) tuples, sorted descending.

        """
        if not NUMPY_AVAILABLE or len(candidate_vecs) == 0:
            return []

        # Vectorized cosine similarity
        norms = np.linalg.norm(candidate_vecs, axis=1)
        query_norm = np.linalg.norm(query_vec)
        if query_norm < 1e-10:
            return []

        dots = candidate_vecs @ query_vec
        valid = norms > 1e-10
        sims = np.zeros(len(candidate_vecs), dtype=np.float32)
        sims[valid] = dots[valid] / (norms[valid] * query_norm)

        # Top-k indices
        top_k = min(k, len(sims))
        indices = np.argpartition(sims, -top_k)[-top_k:]
        indices = indices[np.argsort(sims[indices])[::-1]]

        return [(int(idx), float(sims[idx])) for idx in indices]


def get_embedding_adapter(dimension: int = DEFAULT_EMBEDDING_DIM) -> EmbeddingAdapter:
    """Get or create the global EmbeddingAdapter singleton."""
    return EmbeddingAdapter(dimension=dimension)


def reset_embedding_adapter() -> None:
    """Reset the global adapter (for testing)."""
    EmbeddingAdapter._instance = None
    EmbeddingAdapter._initialized = False
