"""State management: compression, agent communication, and call tracking.

Contains:
- CompressedAgentState: TurboQuant-compressed state variant
- CompressedKVPool / get_kv_pool(): shared KV cache singleton
- Agent call tracking helpers (delegates to singletons)
- Orchestrator channel communication helpers
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from beagle.core.orchestrator_types import AgentState
from beagle.core.singletons import (
    AgentCallTracker,
    OrchestratorChannelManager,
    ProcessRegistry,
)

logger = logging.getLogger("Beagle.orchestrator")


# ═════════════════════════════════════════════════════════════════════════════
# Delegates to singletons (replaces bare module globals)
# ═════════════════════════════════════════════════════════════════════════════


async def _add_process(proc: asyncio.subprocess.Process) -> None:
    """Thread-safe addition to active processes."""
    await ProcessRegistry.instance().register(proc)


async def _remove_process(proc: asyncio.subprocess.Process) -> None:
    """Thread-safe removal from active processes."""
    await ProcessRegistry.instance().unregister(proc)


async def increment_agent_call(parent_workflow_id: str) -> int:
    """Atomically increment agent call count and return new value."""
    return await AgentCallTracker.instance().increment(parent_workflow_id)


async def get_agent_call_count(parent_workflow_id: str) -> int:
    """Get current agent call count for workflow."""
    return await AgentCallTracker.instance().get_count(parent_workflow_id)


async def reset_agent_call_counter(parent_workflow_id: str) -> None:
    """Reset agent call counter for workflow."""
    await AgentCallTracker.instance().reset(parent_workflow_id)


async def cleanup_agent_call_counter(parent_workflow_id: str) -> None:
    """Remove agent call counter for workflow to prevent memory leaks."""
    await AgentCallTracker.instance().cleanup(parent_workflow_id)


async def set_orchestrator_channel(channel: asyncio.Queue) -> None:
    """Set the global orchestrator communication channel."""
    await OrchestratorChannelManager.instance().set_channel(channel)


async def ping_orchestrator(message: dict) -> bool:
    """Send a message to the orchestrator via the channel.

    Args:
        message: Dict containing agent results, status, research path, etc.

    Returns:
        True if message was sent, False if no channel available

    """
    return await OrchestratorChannelManager.instance().send_message(message)


# ===================================================================
# TURBOQUANT INTEGRATION - Phase 3: Compressed AgentState
# ===================================================================


@dataclass
class CompressedAgentState(AgentState):
    """Context-compressed version of agent state using TurboQuant.

    When context exceeds the safe threshold, compress() embeds the full
    context, passes it through TurboQuant compression, stores the
    compressed sidecar, and keeps only a structural skeleton in memory.

    Decompression is available via decompress() for retrieving specific
    sections by semantic similarity on demand.
    """

    kv_cache_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    compressed_observations: list[str] = field(default_factory=list)
    _fold_id: str = field(default="", repr=False)
    _original_length: int = field(default=0, repr=False)

    def compress(self) -> None:
        """Compress execution context using TurboQuant embedding + quantization.

        Pipeline:
        1. Embed the full context text into chunks (768-dim vectors)
        2. TurboQuant compress the embedding matrix (3-bit quantization)
        3. Build structural skeleton (class def, func def, imports, etc.)
        4. Store compressed embeddings as sidecar file
        5. Replace raw_execution_context with skeleton

        Falls back to head+tail truncation if embeddings/TurboQuant
        are unavailable.
        """
        raw_ctx = getattr(self, "raw_execution_context", "")
        if not raw_ctx or len(raw_ctx) <= 10_000:
            # Too short to meaningfully compress
            return

        self._original_length = len(raw_ctx)

        # Attempt TurboQuant pipeline
        try:
            from beagle.context.context_integration import (
                ContextIntegration,
            )
            from beagle.context.embedding_adapter import (
                get_embedding_adapter,
            )

            # Chunk text using static method from ContextIntegration
            chunks = ContextIntegration._chunk_text(raw_ctx, chunk_size=512)
            if not chunks or len(chunks) < 3:
                # Not enough chunks for meaningful compression
                self._fallback_truncate(raw_ctx)
                return

            # Embed chunks
            adapter = get_embedding_adapter()
            embeddings = adapter.embed_batch(chunks)

            # TurboQuant compress
            from beagle.core.turboquant import TurboQuantCompressor

            compressor = TurboQuantCompressor(bits=3)
            compressed_bytes, seed = compressor.compress(embeddings)

            # Build skeleton
            skeleton = ContextIntegration._build_skeleton(raw_ctx)

            # Store sidecar via compressed store
            manifest = {
                "fold_id": self.kv_cache_id,
                "seed": seed,
                "n_chunks": len(chunks),
                "embedding_dim": (embeddings.shape[1] if len(embeddings.shape) > 1 else 768),
                "chunk_map": [
                    {
                        "index": i,
                        "char_start": i * 512,
                        "char_end": min((i + 1) * 512, len(raw_ctx)),
                        "text_preview": c[:80],
                    }
                    for i, c in enumerate(chunks)
                ],
                "original_tokens": self._original_length // 4,
                "compressed_tokens": len(skeleton) // 4,
                "compression_ratio": len(skeleton) / max(self._original_length, 1),
                "created_at": time.time(),
            }

            from beagle.context.compressed_store import (
                get_compressed_store,
            )

            store = get_compressed_store()
            self._fold_id = store.store_fold(manifest, compressed_bytes)

            # Replace with skeleton
            self.raw_execution_context = skeleton

            self.compressed_observations.append(
                f"TurboQuant compressed: {self._original_length} → "
                f"{len(skeleton)} chars (fold #{self._fold_id})"
            )
            logger.info(
                f"[Context] TurboQuant compressed: "
                f"{self._original_length} → {len(skeleton)} chars, "
                f"fold #{self._fold_id}"
            )
            return

        except ImportError:
            logger.debug("[Context] TurboQuant/embedding not available, falling back")
        except (RuntimeError, ValueError, OSError) as exc:
            logger.warning(f"[Context] TurboQuant compress failed: {exc}, truncating")

        # Fallback: head+tail truncation
        self._fallback_truncate(raw_ctx)

    def _fallback_truncate(self, raw_ctx: str) -> None:
        """Head+tail truncation fallback when TurboQuant unavailable."""
        if len(raw_ctx) <= 10_000:
            return
        original_len = len(raw_ctx)
        self.raw_execution_context = (
            raw_ctx[:2000]
            + f"\n\n[... {original_len - 4000} chars truncated ...]\n\n"
            + raw_ctx[-2000:]
        )
        self.compressed_observations.append(
            f"Context truncated: {original_len} → {len(self.raw_execution_context)} chars"
        )
        logger.info(
            f"[Context] Truncated: {original_len} → {len(self.raw_execution_context)} chars"
        )

    def decompress(self, query: str, top_k: int = 3) -> list[dict[str, Any]]:
        """Retrieve relevant chunks from compressed fold by semantic search.

        Args:
            query: Query to find relevant chunks.
            top_k: Maximum chunks to return.

        Returns:
            List of chunk dicts with similarity scores and previews.

        """
        if not self._fold_id:
            logger.debug("[Context] No compressed fold to decompress from")
            return []

        try:
            from beagle.context.compressed_store import (
                get_compressed_store,
            )

            store = get_compressed_store()
            return store.query_fold(query, self._fold_id, top_k=top_k)
        except (RuntimeError, ValueError, OSError, ImportError) as exc:
            logger.debug(f"[Context] Decompress retrieval failed: {exc}")
            return []


class CompressedKVPool:
    """Shared pool for TurboQuant KV caches across agents."""

    def __init__(self) -> None:
        self._pool: dict[str, Any] = {}

    def get(self, cache_id: str) -> Any:
        return self._pool.get(cache_id)

    def put(self, cache_id: str, data: Any) -> None:
        self._pool[cache_id] = data


_kv_pool: CompressedKVPool | None = None


def get_kv_pool() -> CompressedKVPool:
    """Singleton access to the KV pool."""
    global _kv_pool
    if _kv_pool is None:
        _kv_pool = CompressedKVPool()
    return _kv_pool
