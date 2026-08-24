"""BeagleRetriever — LangChain BaseRetriever for CAST-indexed RAG.

Phase 1 of the LangChain Ecosystem Compatibility Plan.
Exposes Beagle's LanceDB+Kùzu hybrid RAG as a standard
langchain_core.retrievers.BaseRetriever so any LangChain
application can query your codebase knowledge.

Communication modes:
  - in_process: Import rag_search() directly (fastest, zero IPC)
  - mcp_stdio: Spawn MCP server subprocess (isolated, matches goose pattern)
  - orpheus_ipc: Via Orpheus ring buffer (lowest latency)

All configuration read from config.toml [langchain_bridges.retriever].
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
import time
from collections import OrderedDict
from typing import Any, ClassVar

from langchain_core.callbacks import (
    AsyncCallbackManagerForRetrieverRun,
    CallbackManagerForRetrieverRun,
)
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever

from .config import RetrieverConfig, get_retriever_config

logger = logging.getLogger("Beagle.bridges.retriever")

# ── Query result cache ────────────────────────────────────────────────────────

_cache_lock = threading.Lock()
_result_cache: OrderedDict[str, tuple[float, list[Document]]] = OrderedDict()
_MAX_CACHE_SIZE = 200


def _cache_get(key: str, ttl: int) -> list[Document] | None:
    """Thread-safe cache lookup with TTL expiry and LRU ordering."""
    with _cache_lock:
        if key in _result_cache:
            ts, docs = _result_cache[key]
            if time.monotonic() - ts < ttl:
                _result_cache.move_to_end(key)  # Mark as recently used
                return docs
            del _result_cache[key]
    return None


def _cache_put(key: str, docs: list[Document]) -> None:
    """Thread-safe cache insert with LRU eviction."""
    with _cache_lock:
        _result_cache[key] = (time.monotonic(), docs)
        while len(_result_cache) > _MAX_CACHE_SIZE:
            _result_cache.popitem(last=False)  # Evict oldest (FIFO)


def clear_cache() -> None:
    """Clear the retriever result cache (thread-safe)."""
    with _cache_lock:
        _result_cache.clear()


# ── RAG search wrappers ──────────────────────────────────────────────────────


async def _rag_search_in_process(query: str, max_hops: int, top_k: int) -> dict[str, Any]:
    """Call rag_search() directly from the infrastructure module.

    Fastest path — no IPC overhead, shares the process-space with
    the RAG server's LanceDB + Kùzu connections.
    """
    from ..infrastructure.mcp_rag_server import rag_search

    raw = await rag_search(query=query, max_hops=max_hops, top_k=top_k)
    return json.loads(raw)  # type: ignore[no-any-return]


async def _rag_search_mcp_stdio(
    query: str, max_hops: int, top_k: int, timeout: int = 30
) -> dict[str, Any]:
    """Call rag_search via MCP stdio subprocess (isolated mode).

    Matches the existing goose-to-MCP-server communication pattern.
    Involves ~500ms process spawn overhead per query.
    """

    from ..config.config import get_config

    config = get_config()
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "rag_search",
        "params": {"query": query, "max_hops": max_hops, "top_k": top_k},
    }

    cmd = [
        config._raw.get("mcp", {}).get("rag_server_binary", "python3"),  # type: ignore[attr-defined]
        config._raw.get("mcp", {}).get("rag_server_script", "infrastructure/mcp_rag_server.py"),  # type: ignore[attr-defined]
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(
            proc.communicate(json.dumps(request).encode()),
            timeout=timeout,
        )
        return json.loads(stdout.decode())  # type: ignore[no-any-return]
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()  # type: ignore[union-attr]
        raise
    except Exception as exc:  # broad catch intentional
        raise RuntimeError(f"MCP stdio RAG search failed: {exc}") from exc


async def _rag_search_orpheus(query: str, max_hops: int, top_k: int) -> dict[str, Any]:
    """Call rag_search via Orpheus ring buffer IPC.

    Ultra-low latency, uses existing Orpheus bus.
    NOTE: Placeholder — Orpheus IPC transport for RAG is not yet implemented.
    Fallback to in_process for now.
    """
    logger.debug("Orpheus IPC transport not yet available, falling back to in_process")
    return await _rag_search_in_process(query, max_hops, top_k)


# ── Result → Document mapping ────────────────────────────────────────────────


def _map_results_to_documents(
    raw_results: dict[str, Any],
    config: RetrieverConfig,
) -> list[Document]:
    """Map Beagle RAG search results into LangChain Document objects.

    The raw_results dict has keys:
      - semantic_anchors: list of {file_path, snippet, ast_node_type, start_line, end_line, ...}
      - structural_relations: list of {source, target, relation_type, ...}

    Each anchor becomes a Document; relations are embedded in metadata
    if config.include_relations is True.
    """
    documents: list[Document] = []

    # Build a lookup of file_path → relations for enriching metadata
    relations_by_file: dict[str, list[dict]] = {}
    if config.include_relations:
        for rel in raw_results.get("structural_relations", []):
            src = rel.get("source", "")
            relations_by_file.setdefault(src, []).append(rel)

    # Map semantic anchors → Documents
    for anchor in raw_results.get("semantic_anchors", []):
        # Build metadata using the configured mapping
        metadata: dict[str, Any] = {}
        mapping = config.metadata_mapping

        for src_key, dst_key in mapping.items():
            if src_key == "page_content":
                continue  # page_content goes to the main field
            val = anchor.get(src_key)
            if val is not None:
                metadata[dst_key] = val

        # Attach structural relations for this file
        file_path = anchor.get("file_path", "")
        if file_path in relations_by_file:
            metadata["relations"] = relations_by_file[file_path]

        # page_content from snippet
        snippet = anchor.get("snippet", "")

        doc = Document(
            page_content=snippet,
            metadata=metadata,
        )
        documents.append(doc)

    # Sort by relevance score (highest first) if available
    def _score(doc: Document) -> float:
        return float(doc.metadata.get("score", 0.0))

    documents.sort(key=_score, reverse=True)
    return documents


# ── BeagleRetriever ─────────────────────────────────────────────────────────────


class BeagleRetriever(BaseRetriever):
    """LangChain BaseRetriever backed by Beagle's CAST-indexed RAG.

    Exposes LanceDB vector similarity + Kùzu graph traversal
    through the standard LangChain retriever interface.

    Usage:
        retriever = BeagleRetriever()
        docs = retriever.invoke("A2A protocol implementation")
        # Or in a chain:
        chain = retriever | ChatOpenAI() | StrOutputParser()

    All config read from config.toml [langchain_bridges.retriever].
    """

    # Pydantic v2 model configuration (replaces legacy class Config)
    model_config: ClassVar[dict[str, bool]] = {  # type: ignore[assignment]
        "arbitrary_types_allowed": True,
        "populate_by_name": True,
    }

    # Pydantic-validated fields (BaseRetriever uses Pydantic v2)
    kuzu_hops: int = 1
    top_k: int = 5
    include_relations: bool = True
    communication_mode: str = "in_process"
    mcp_timeout_seconds: int = 30
    cache_results: bool = True
    cache_ttl_seconds: int = 300

    def __init__(self, **kwargs: Any) -> None:
        """Initialize retriever, reading defaults from config.toml."""
        cfg = get_retriever_config()
        # Config.toml values as defaults, kwargs override
        field_defaults = {
            "kuzu_hops": cfg.max_hops,
            "top_k": cfg.top_k,
            "include_relations": cfg.include_relations,
            "communication_mode": cfg.communication_mode,
            "mcp_timeout_seconds": cfg.mcp_timeout_seconds,
            "cache_results": cfg.cache_results,
            "cache_ttl_seconds": cfg.cache_ttl_seconds,
        }
        # Only apply defaults for fields not explicitly passed
        for key, default in field_defaults.items():
            if key not in kwargs:
                kwargs[key] = default
        super().__init__(**kwargs)

    def _get_relevant_documents(
        self,
        query: str,
        *,
        run_manager: CallbackManagerForRetrieverRun | None = None,
    ) -> list[Document]:
        _ = run_manager
        """Synchronous retrieval — calls async version via event loop.

        Args:
            query: Natural language search query.
            run_manager: LangChain callback manager (unused, required by BaseRetriever).

        Returns:
            List of Document objects from Beagle's RAG.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            # We're inside an existing event loop — create a task
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(asyncio.run, self._aget_relevant_documents(query))
                return future.result()
        else:
            return asyncio.run(self._aget_relevant_documents(query))

    async def _aget_relevant_documents(
        self,
        query: str,
        *,
        run_manager: AsyncCallbackManagerForRetrieverRun | None = None,
    ) -> list[Document]:
        _ = run_manager
        """Async retrieval — primary implementation.

        Args:
            query: Natural language search query.
            run_manager: LangChain callback manager (unused, required by BaseRetriever).

        Returns:
            List of Document objects from Beagle's RAG.
        """
        cfg = get_retriever_config()

        # Check cache
        cache_key = f"{query}:{self.kuzu_hops}:{self.top_k}:{self.include_relations}"
        if self.cache_results:
            cached = _cache_get(cache_key, self.cache_ttl_seconds)
            if cached is not None:
                logger.debug(f"Retriever cache HIT for: {query[:50]}")
                return cached

        # Dispatch to appropriate search backend
        try:
            if self.communication_mode == "in_process":
                raw = await _rag_search_in_process(query, self.kuzu_hops, self.top_k)
            elif self.communication_mode == "mcp_stdio":
                raw = await _rag_search_mcp_stdio(
                    query, self.kuzu_hops, self.top_k, self.mcp_timeout_seconds
                )
            elif self.communication_mode == "orpheus_ipc":
                raw = await _rag_search_orpheus(query, self.kuzu_hops, self.top_k)
            else:
                logger.warning(
                    f"Unknown communication_mode '{self.communication_mode}', "
                    f"falling back to in_process"
                )
                raw = await _rag_search_in_process(query, self.kuzu_hops, self.top_k)
        except Exception as exc:  # ruff: ignore[BLE001]  # broad catch intentional
            logger.error(f"RAG search failed for query '{query[:50]}': {exc}")
            return []

        # Map Beagle results → LangChain Documents
        documents = _map_results_to_documents(raw, cfg)

        # Cache result
        if self.cache_results and documents:
            _cache_put(cache_key, documents)

        logger.info(f"Retriever returned {len(documents)} documents for: {query[:50]}")
        return documents
