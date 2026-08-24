"""Semantic Prompt Cache — Cache prompts by embedding similarity.

Caches (embedding, tokenized_prompt) pairs. On new prompt, computes embedding
and checks cosine similarity with cached entries. If similarity > 0.99,
reuses the cached response, saving API calls and tokens.

Usage:
    from beagle.context.semantic_prompt_cache import SemanticPromptCache

    cache = SemanticPromptCache()
    cached = cache.lookup(prompt_embedding=[0.1, 0.2, ...], prompt_text="...")
    if cached:
        return cached
    result = await llm_call(...)
    cache.store(prompt_embedding=[0.1, 0.2, ...], prompt_text="...", response=result)
"""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass, field

logger = logging.getLogger("Beagle.semantic_prompt_cache")

# Similarity threshold for cache hits
DEFAULT_SIMILARITY_THRESHOLD = 0.99
DEFAULT_MAX_ENTRIES = 256
DEFAULT_TTL_SECONDS = 3600  # 1 hour


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors.

    Uses pure Python (no NumPy dependency) for lightweight operation.
    """
    if len(a) != len(b):
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for i in range(len(a)):
        dot += a[i] * b[i]
        norm_a += a[i] * a[i]
        norm_b += b[i] * b[i]
    if norm_a == 0.0 or norm_b == 0.0:  # exact float-zero is the intent
        return 0.0
    return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))


@dataclass
class PromptCacheEntry:
    """A cached prompt-response pair with embedding."""

    prompt_text: str
    prompt_embedding: list[float]
    response: str
    created_at: float = field(default_factory=time.monotonic)
    hits: int = 0

    def is_expired(self, ttl: float = DEFAULT_TTL_SECONDS) -> bool:
        return time.monotonic() > (self.created_at + ttl)


class SemanticPromptCache:
    """Semantic prompt cache using embedding similarity for lookups.

    Stores (embedding, prompt_text, response) entries. On lookup, computes
    cosine similarity between the query embedding and all cached embeddings.
    If similarity > threshold, returns the cached response.

    Thread-safe via lock. LRU eviction when at capacity.
    """

    def __init__(
        self,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    ) -> None:
        """Initialize the semantic prompt cache.

        Args:
            max_entries: Maximum cache entries (LRU eviction).
            ttl_seconds: Time-to-live in seconds.
            similarity_threshold: Cosine similarity threshold for cache hits.

        """
        self.max_entries = max_entries
        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self._entries: list[PromptCacheEntry] = []
        self._lock = threading.Lock()
        self._stats = {"hits": 0, "misses": 0, "evictions": 0}

    def lookup(
        self,
        prompt_embedding: list[float],
        prompt_text: str,
    ) -> str | None:
        """Look up a cached response by embedding similarity.

        Args:
            prompt_embedding: Embedding vector of the prompt.
            prompt_text: Prompt text (for exact match check first).

        Returns:
            Cached response if similarity > threshold, else None.

        """
        with self._lock:
            # Quick exact-match check
            for entry in self._entries:
                if entry.prompt_text == prompt_text and not entry.is_expired(self.ttl_seconds):
                    entry.hits += 1
                    entry.created_at = time.monotonic()  # Touch for LRU
                    self._stats["hits"] += 1
                    logger.debug("[SemanticCache] EXACT HIT")
                    return entry.response

            # Semantic similarity search
            best_similarity = 0.0
            best_entry: PromptCacheEntry | None = None
            for entry in self._entries:
                if entry.is_expired(self.ttl_seconds):
                    continue
                sim = _cosine_similarity(prompt_embedding, entry.prompt_embedding)
                if sim > best_similarity:
                    best_similarity = sim
                    best_entry = entry

            if best_entry and best_similarity >= self.similarity_threshold:
                best_entry.hits += 1
                best_entry.created_at = time.monotonic()
                self._stats["hits"] += 1
                logger.debug(f"[SemanticCache] SEMANTIC HIT (similarity={best_similarity:.4f})")
                return best_entry.response

            self._stats["misses"] += 1
            return None

    def store(
        self,
        prompt_embedding: list[float],
        prompt_text: str,
        response: str,
    ) -> None:
        """Store a prompt-response pair in the cache.

        Args:
            prompt_embedding: Embedding vector of the prompt.
            prompt_text: Prompt text.
            response: Response to cache.

        """
        with self._lock:
            # Evict expired entries
            self._entries = [e for e in self._entries if not e.is_expired(self.ttl_seconds)]

            # LRU eviction if at capacity
            while len(self._entries) >= self.max_entries:
                # Remove oldest (lowest created_at)
                oldest_idx = min(
                    range(len(self._entries)), key=lambda i: self._entries[i].created_at
                )
                self._entries.pop(oldest_idx)
                self._stats["evictions"] += 1

            self._entries.append(
                PromptCacheEntry(
                    prompt_text=prompt_text[:500],  # Truncate for memory
                    prompt_embedding=prompt_embedding,
                    response=response[:10000],  # Truncate large responses
                )
            )

    def clear(self) -> int:
        """Clear all cache entries.

        Returns:
            Number of entries cleared.

        """
        with self._lock:
            count = len(self._entries)
            self._entries.clear()
            return count

    @property
    def stats(self) -> dict[str, int]:
        """Get cache statistics."""
        with self._lock:
            return {
                **self._stats,
                "entries": len(self._entries),
            }

    @property
    def hit_rate(self) -> float:
        """Get cache hit rate (0.0 to 1.0)."""
        total = self._stats["hits"] + self._stats["misses"]
        if total == 0:
            return 0.0
        return self._stats["hits"] / total


# Module-level singleton
_semantic_cache: SemanticPromptCache | None = None


def get_semantic_prompt_cache() -> SemanticPromptCache:
    """Get or create the singleton semantic prompt cache."""
    global _semantic_cache
    if _semantic_cache is None:
        _semantic_cache = SemanticPromptCache()
    return _semantic_cache


__all__ = [
    "PromptCacheEntry",
    "SemanticPromptCache",
    "_cosine_similarity",
    "get_semantic_prompt_cache",
]
