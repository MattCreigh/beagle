"""Intent-Based Context Hydration Node for Beagle v13.

This module provides the hydration_node that runs BEFORE execution_node
to automatically load relevant context based on the current query intent.

Hydration sources:
1. LanceDB vector search (semantic code chunks)
2. ConstraintRegistry (user-defined constraints)
3. Kuzu graph traversal (structural relationships)
4. ContextManifest (skill-specific documentation)
5. SessionMemory (episodic context)

The goal is to inject the 5 most relevant "Cognitive Representations"
without exceeding token budgets.
"""

from __future__ import annotations

import asyncio
import json as json_module
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from ..config.config import get_config
from ..config.paths import get_workspace_root
from ..security import scrub_secrets

logger = logging.getLogger("Beagle.hydration")


# ── Speculative Context Prefetch (v13.5.2) ──────────────────────────────────
#
# LRU cache for recent query embeddings and their RAG results.  When a new
# query arrives, its embedding is compared (cosine similarity) against cached
# queries.  If similarity > 0.9, the cached chunks are returned immediately
# without hitting LanceDB/Kùzu, reducing latency for repeated or very similar
# queries (e.g., iterative development loops where the same area of code is
# queried repeatedly).

# Module-level prefetch cache: stores (embedding, query, results, max_results, timestamp)
_prefetch_entries: list[dict[str, Any]] = []
_PREFETCH_MAX_ENTRIES: int = 5
_PREFETCH_SIMILARITY_THRESHOLD: float = 0.9
_PREFETCH_TTL_SECONDS: float = 300.0  # 5 minutes


def _prefetch_find_similar(
    query_embedding: Any,
    max_results: int,
) -> list[dict[str, Any]] | None:
    """Check the prefetch cache for a similar query embedding.

    Compares the given embedding against all cached entries using cosine
    similarity.  Returns cached results if similarity exceeds threshold
    AND max_results matches (or is fewer than) what was cached.

    Args:
        query_embedding: Numpy array of the query embedding.
        max_results: Maximum number of results requested.

    Returns:
        Cached result list if a match is found, else None.

    """
    now = time.time()
    # Purge expired entries
    global _prefetch_entries
    _prefetch_entries = [
        e for e in _prefetch_entries if now - e["timestamp"] < _PREFETCH_TTL_SECONDS
    ]

    try:
        from ..context.embedding_adapter import EmbeddingAdapter

        EmbeddingAdapter()
    except ImportError:
        return None

    for entry in _prefetch_entries:
        try:
            sim = EmbeddingAdapter.similarity(query_embedding, entry["embedding"])
        except (ValueError, TypeError, AttributeError, KeyError) as exc:
            logger.warning(
                "Cannot score prefetch entry %r for similarity (%s); skipping it, so a "
                "cached result that would have matched may be missed.",
                entry.get("query", "<unknown>"),
                exc,
            )
            continue
        if sim >= _PREFETCH_SIMILARITY_THRESHOLD and max_results <= entry.get("max_results", 0):
            logger.info(
                f"[prefetch] Cache hit: similarity={sim:.4f}, "
                f"returning {len(entry['results'])} cached chunks"
            )
            return entry["results"][:max_results]  # type: ignore[no-any-return]

    return None


def _prefetch_store(
    query: str,
    query_embedding: Any,
    results: list[dict[str, Any]],
    max_results: int,
) -> None:
    """Store a query result in the prefetch cache.

    Args:
        query: Original query string (for logging).
        query_embedding: Numpy array of the query embedding.
        results: RAG search results to cache.
        max_results: The max_results parameter used.

    """
    global _prefetch_entries
    entry = {
        "query": query,
        "embedding": query_embedding,
        "results": results,
        "max_results": max_results,
        "timestamp": time.time(),
    }
    _prefetch_entries.append(entry)
    # Evict oldest if over capacity
    while len(_prefetch_entries) > _PREFETCH_MAX_ENTRIES:
        evicted = _prefetch_entries.pop(0)
        logger.debug(f"[prefetch] Evicted cache entry for: {evicted['query'][:60]}")
    logger.info(
        f"[prefetch] Cached {len(results)} chunks for query: {query[:60]}  "
        f"(cache size: {len(_prefetch_entries)}/{_PREFETCH_MAX_ENTRIES})"
    )


def prefetch_stats() -> dict[str, Any]:
    """Return prefetch cache statistics for observability."""
    return {
        "entries": len(_prefetch_entries),
        "max_entries": _PREFETCH_MAX_ENTRIES,
        "similarity_threshold": _PREFETCH_SIMILARITY_THRESHOLD,
        "ttl_seconds": _PREFETCH_TTL_SECONDS,
    }


@dataclass
class HydrationResult:
    """Result of context hydration operation.

    Attributes:
        constraints: Extracted active constraints
        code_chunks: Relevant code from RAG search
        structural_context: Graph-derived context
        documentation: Loaded documentation
        session_episodes: Recent session history
        total_tokens: Approximate token count

    """

    constraints: list[str] = field(default_factory=list)
    code_chunks: list[dict[str, Any]] = field(default_factory=list)
    structural_context: str = ""
    documentation: str = ""
    session_episodes: list[dict[str, Any]] = field(default_factory=list)
    total_tokens: int = 0

    def to_context_block(self) -> str:
        """Convert hydration result to injectable context block.

        Applies secret scrubbing to all text content before inclusion.

        Returns:
            Formatted XML-style context block

        """
        sections: list[str] = []

        # 1. Active constraints (HIGHEST PRIORITY) - with secret scrubbing
        if self.constraints:
            sections.append("<active_constraints>")
            sections.append("The following constraints MUST be respected:\n")
            for i, constraint in enumerate(self.constraints[:10], 1):
                # Scrub secrets from constraint text
                scrubbed_constraint = scrub_secrets(constraint)
                sections.append(f"{i}. {scrubbed_constraint}")
            sections.append("</active_constraints>")

        # 2. Relevant code context - snippets already from RAG, but scrub path/file
        if self.code_chunks:
            sections.append("\n<code_context>")
            sections.append("Relevant code from codebase:\n")
            for chunk in self.code_chunks[:5]:
                file_path = chunk.get("file", chunk.get("path", "unknown"))
                snippet = chunk.get("snippet", chunk.get("content", ""))
                score = chunk.get("score", chunk.get("relevance", 0))
                if snippet:
                    # Truncate long snippets
                    if len(snippet) > 500:
                        snippet = snippet[:497] + "..."
                    # Scrub file path (may contain sensitive dir names)
                    file_path = scrub_secrets(file_path)
                    sections.append(f"\n### {file_path} (relevance: {score:.2f})")
                    sections.append("```")
                    sections.append(snippet)
                    sections.append("```")
            sections.append("</code_context>")

        # 3. Structural context - scrubbed
        if self.structural_context:
            sections.append("\n<structural_context>")
            sections.append(scrub_secrets(self.structural_context))
            sections.append("</structural_context>")

        # 4. Documentation - already from manifest, but scrub as precaution
        if self.documentation:
            sections.append("\n<documentation>")
            # Truncate documentation if too large
            doc_text = self.documentation
            if len(doc_text) > 2000:
                doc_text = doc_text[:1997] + "..."
            sections.append(scrub_secrets(doc_text))
            sections.append("</documentation>")

        # 5. Session episodes (EPISODIC memory) - with secret scrubbing
        if self.session_episodes:
            sections.append("\n<session_context>")
            sections.append("Recent session activity:\n")
            for episode in self.session_episodes[:3]:
                phase = episode.get("phase", "unknown")
                summary = episode.get("summary", "")[:200]
                if summary:
                    # Scrub episode summary for sensitive data
                    scrubbed_summary = scrub_secrets(summary)
                    sections.append(f"- [{phase.upper()}] {scrubbed_summary}")
            sections.append("</session_context>")

        if not sections:
            return ""

        return "\n".join(sections)


async def hydrate_context(
    query: str,
    skill_name: str,
    state: dict[str, Any] | None = None,
    max_tokens: int = 5000,
    project_dir: str | None = None,
) -> HydrationResult:
    """Hydrate context with relevant information.

    This is the main entry point for context hydration. It searches
    multiple sources to find the most relevant context for the query.

    Args:
        query: The user's query or intent
        skill_name: Name of the skill being executed
        state: Current workflow state (optional)
        max_tokens: Maximum tokens to allocate for hydration
        project_dir: Project directory (optional)

    Returns:
        HydrationResult with relevant context

    """
    result = HydrationResult()
    token_budget = max_tokens

    # 1. Load constraints (CRITICAL - always included)
    constraints = await _hydrate_constraints(query, project_dir)
    result.constraints = constraints
    token_budget -= sum(len(c) // 4 for c in constraints)  # ~4 chars per token

    # 2. RAG search for code context
    if token_budget > 1000:
        code_chunks = await _hydrate_rag(query, max_results=5)
        result.code_chunks = code_chunks
        for chunk in code_chunks:
            snippet = chunk.get("snippet", chunk.get("content", ""))
            token_budget -= len(snippet) // 4

    # 2b. Recover context from compressed folds (after compaction)
    if token_budget > 500 and not code_chunks:
        fold_chunks = await _hydrate_from_fold(query, top_k=3)
        if fold_chunks:
            # Merge fold-recovered chunks into code_chunks
            for fc in fold_chunks:
                preview = fc.get("text_preview", "")
                if preview:
                    code_chunks.append(
                        {
                            "source": "compressed_fold",
                            "fold_id": fc.get("fold_id", ""),
                            "similarity": fc.get("similarity", 0),
                            "snippet": preview,
                            "content": preview,
                        }
                    )
                    token_budget -= len(preview) // 4
            result.code_chunks = code_chunks

    # 3. Load documentation from manifest
    if token_budget > 1000:
        documentation = await _hydrate_documentation(skill_name, project_dir)
        # Truncate to token budget
        max_doc_tokens = min(token_budget - 500, 1500)
        if len(documentation) > max_doc_tokens * 4:
            documentation = documentation[: max_doc_tokens * 4] + "\n... [TRUNCATED]"
        result.documentation = documentation
        token_budget -= len(documentation) // 4

    # 4. Load session episodes
    if token_budget > 500:
        _sid = state.get("session_id", "") if state else ""
        episodes = await _hydrate_session_episodes(query, project_dir, session_id=_sid)
        result.session_episodes = episodes
        token_budget -= len(str(episodes)) // 4

    # Estimate total tokens
    result.total_tokens = sum(len(c) // 4 for c in result.constraints)
    result.total_tokens += sum(len(str(e)) // 4 for e in result.code_chunks)
    result.total_tokens += len(result.structural_context) // 4
    result.total_tokens += len(result.documentation) // 4
    result.total_tokens += len(str(result.session_episodes)) // 4

    return result


async def _hydrate_constraints(query: str, project_dir: str | None) -> list[str]:
    """Load constraints from ConstraintRegistry.

    Args:
        query: Query string (for future relevance filtering)
        project_dir: Project directory

    Returns:
        List of constraint strings

    """
    constraints: list[str] = []

    try:
        from ..infrastructure.constraint_registry import (
            ConstraintRegistry,
        )

        # Determine project name (use dir basename for consistent hashing)
        if project_dir:
            from pathlib import Path

            project = Path(project_dir).name
        else:
            try:
                project = str(get_workspace_root().name)
            except Exception:  # ruff: ignore[BLE001]  # broad catch intentional
                project = "default"

        registry = ConstraintRegistry(project=project)
        registry.load()

        # Get all active constraints
        active = registry.get_active()

        # Sort by priority (CRITICAL first)
        active.sort(key=lambda c: c.priority)

        # Convert to strings
        for constraint in active[:15]:  # Limit to 15 constraints
            formatted = constraint.format_for_context()
            constraints.append(formatted)

        if constraints:
            logger.debug(f"Loaded {len(constraints)} constraints from registry")

    except ImportError as e:
        logger.debug(f"ConstraintRegistry not available: {e}")
    except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
        logger.warning(f"Failed to load constraints: {e}")

    return constraints


# ── RAG Query Decomposition (v13.7) ────────────────────────────────────────
#
# Complex multi-part queries often miss relevant RAG results because the
# embedding blends multiple intents.  decompose_query() splits a query into
# focused sub-queries so each targets a narrower semantic region.
# _merge_rag_results() then deduplicates by (file, line) keys.

# Heuristics for detecting composite queries
_COMPOUND_CONJUNCTIONS = (
    " and ",
    " or ",
    " but ",
    " while ",
    " whereas ",
    " as well as ",
    " along with ",
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_COMMA_SPLIT_RE = re.compile(r",\s*(?:and\s+)?")
_SUBQUERY_MIN_LEN = 10  # Skip fragments shorter than this


def decompose_query(
    query: str,
    max_subqueries: int = 3,
) -> list[str]:
    """Split a composite query into focused sub-queries for RAG retrieval.

    Uses heuristic splitting on sentence boundaries, conjunction keywords,
    and comma-separated clauses.  Falls back to the original query if no
    meaningful split is found.

    Args:
        query: The user's full query string.
        max_subqueries: Maximum sub-queries to produce (default 3).
            The original query is always included as the first element.

    Returns:
        List of sub-query strings, always starting with the original query.
        Never empty — at minimum returns [query].

    """
    if not query or not query.strip():
        return [query] if query else []

    original = query.strip()
    subqueries: list[str] = [original]

    # 1. Sentence-level split (periods, question marks, exclamation)
    sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(original) if s.strip()]
    if len(sentences) >= 2:
        for s in sentences:
            if len(s) >= _SUBQUERY_MIN_LEN and s not in subqueries:
                subqueries.append(s)

    # 2. Conjunction split within each sentence
    if len(subqueries) < max_subqueries + 1:
        for conj in _COMPOUND_CONJUNCTIONS:
            if conj in original.lower():
                # Split on the conjunction but keep context on both sides
                parts = re.split(re.escape(conj), original, maxsplit=1, flags=re.IGNORECASE)
                for part in parts:
                    part = part.strip()
                    if len(part) >= _SUBQUERY_MIN_LEN and part not in subqueries:
                        subqueries.append(part)
                break  # Only split on the first conjunction found

    # 3. Comma-separated clauses (e.g., "X, Y, and Z")
    if len(subqueries) < max_subqueries + 1 and "," in original:
        comma_parts = _COMMA_SPLIT_RE.split(original)
        if len(comma_parts) >= 2:
            for part in comma_parts:
                part = part.strip()
                if len(part) >= _SUBQUERY_MIN_LEN and part not in subqueries:
                    subqueries.append(part)

    # If we only have the original, return just that
    if len(subqueries) == 1:
        return subqueries

    # Limit to max_subqueries additional sub-queries (original always first)
    return [original, *subqueries[1 : max_subqueries + 1]]


def _merge_rag_results(
    result_sets: list[list[dict[str, Any]]],
    max_results: int = 10,
) -> list[dict[str, Any]]:
    """Merge and deduplicate RAG results from multiple sub-queries.

    Deduplicates by (file, line_range) identity, keeping the highest-
    scoring occurrence.  Results are sorted by descending relevance.

    Args:
        result_sets: List of result lists, one per sub-query.
        max_results: Maximum results to return after merging.

    Returns:
        Deduplicated, relevance-sorted list of chunk dicts.

    """
    seen_keys: dict[str, dict[str, Any]] = {}

    for results in result_sets:
        for chunk in results:
            # Build a dedup key from file path + line range
            file_path = chunk.get("file", chunk.get("path", ""))
            start_line = chunk.get("start_line", chunk.get("line_start", ""))
            end_line = chunk.get("end_line", chunk.get("line_end", ""))
            key = f"{file_path}:{start_line}-{end_line}"

            # Fallback: use snippet hash if no file info
            if not file_path:
                snippet = chunk.get("snippet", chunk.get("content", ""))
                key = f"snippet:{hash(snippet)}"

            score = float(chunk.get("score", chunk.get("relevance", chunk.get("similarity", 0))))

            if key not in seen_keys or score > float(
                seen_keys[key].get(
                    "score",
                    seen_keys[key].get("relevance", seen_keys[key].get("similarity", 0)),
                )
            ):
                seen_keys[key] = chunk
                # Normalise score field
                if "score" not in chunk and "relevance" in chunk:
                    chunk["score"] = chunk["relevance"]
                elif "score" not in chunk and "similarity" in chunk:
                    chunk["score"] = chunk["similarity"]

    # Sort by score descending
    merged = sorted(seen_keys.values(), key=lambda c: float(c.get("score", 0)), reverse=True)
    return merged[:max_results]


async def _hydrate_rag(query: str, max_results: int = 5) -> list[dict[str, Any]]:
    """Search RAG for relevant code context.

    Uses hybrid search (vector + graph) to find the most relevant
    code chunks for the query. Implements speculative context prefetch
    (v13.5.2): checks an embedding-level LRU cache for semantically
    similar queries before hitting LanceDB/Kùzu, significantly
    reducing latency for repeated or similar queries.

    If the RAG index is stale (e.g., after a context fold), triggers
    hot-swap reingestion first to ensure fresh data before serving results.

    Args:
        query: Query string
        max_results: Maximum results to return

    Returns:
        List of code chunk dictionaries

    """
    # ── Speculative Context Prefetch: check embedding cache first ──
    query_embedding: Any = None
    try:
        from ..context.embedding_adapter import get_embedding_adapter

        adapter = get_embedding_adapter()
        if adapter.available:
            query_embedding = adapter.embed_text(query)
            cached = _prefetch_find_similar(query_embedding, max_results)
            if cached is not None:
                return cached
    except ImportError:
        logger.debug("[hydration/prefetch] EmbeddingAdapter not available")
    except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
        logger.debug(f"[hydration/prefetch] Prefetch check failed: {e}")

    # ── Staleness check: trigger hot-swap reingestion if needed ──
    try:
        from ..context.rag_staleness import get_staleness_tracker

        tracker = get_staleness_tracker()
        if tracker.is_stale and tracker.can_reingest():
            logger.info("[hydration] RAG stale, scheduling background reingest")
            # v13.19.5: Fire-and-forget — do NOT await the reingest.
            # The previous `await tracker.trigger_reingest_if_stale()`
            # blocked the workflow for 30+ seconds (full CAST pipeline
            # on 442 source files), which is what caused the "workflow
            # hung" reports. The async variant runs the reingest in a
            # worker thread and publishes a RAGStale event for
            # telemetry; the workflow continues immediately.
            bg_task = tracker.trigger_reingest_async()
            if bg_task is not None:
                logger.debug(
                    f"[hydration] Background reingest task scheduled: {bg_task.get_name()}"
                )
            else:
                logger.debug(
                    "[hydration] trigger_reingest_async returned None (not stale or throttled)"
                )
    except ImportError:
        logger.debug("[hydration] rag_staleness module not available")
    except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
        logger.debug(f"[hydration] Staleness check failed: {e}")

    try:
        # Try to use LanceDB RAG search
        from ..infrastructure.mcp_rag_server import rag_search

        # ── Query Decomposition (v13.7) ──
        # Split complex queries into sub-queries for better RAG recall.
        decomp_cfg = get_config().decomposition
        if decomp_cfg.enabled and len(query) >= decomp_cfg.min_query_length:
            sub_queries = decompose_query(query, max_subqueries=decomp_cfg.max_subqueries)
        else:
            sub_queries = [query]

        if len(sub_queries) > 1:
            logger.debug(
                f"[hydration] Decomposed query into {len(sub_queries)} sub-queries: {sub_queries}"
            )

        result_sets: list[list[dict[str, Any]]] = []
        for sub_q in sub_queries:
            results_json = await rag_search(sub_q, max_hops=2, top_k=max_results)
            try:
                results = json_module.loads(results_json)
            except (json_module.JSONDecodeError, TypeError):
                logger.debug("RAG search returned invalid JSON for sub-query")
                continue

            # Extract semantic_anchors from the hybrid RAG response
            chunks_for_sub: list[dict[str, Any]] = []
            if results and "semantic_anchors" in results:
                chunks_for_sub = results["semantic_anchors"]
            elif results and "data" in results:
                chunks_for_sub = results["data"]
            elif results and results.get("status") == "no_results":
                continue

            if chunks_for_sub:
                logger.debug(
                    f"[hydration] RAG sub-query '{sub_q[:40]}…' found {len(chunks_for_sub)} chunks"
                )
                result_sets.append(chunks_for_sub)

        # ── Merge & deduplicate results from all sub-queries ──
        if result_sets:
            if len(result_sets) == 1:
                chunks = result_sets[0]
            else:
                decomp_cfg = get_config().decomposition
                chunks = _merge_rag_results(
                    result_sets,
                    max_results=decomp_cfg.merge_max_results,
                )
                logger.debug(
                    f"[hydration] Merged {len(result_sets)} sub-query "
                    f"result sets into {len(chunks)} deduplicated chunks"
                )
        else:
            chunks = []

        # ── Store results in prefetch cache ──
        if chunks and query_embedding is not None:
            _prefetch_store(query, query_embedding, chunks, max_results)

        return chunks[:max_results]

    except ImportError:
        logger.debug("RAG server not available")
    except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
        logger.debug(f"RAG search failed: {e}")

    # Fallback: Return empty list if RAG unavailable
    return []


async def _hydrate_documentation(skill_name: str, project_dir: str | None) -> str:
    """Load documentation from ContextManifest and project doc files.

    Tries ContextManifest first, then falls back to reading CLAUDE.md,
    GEMINI.md, and README.md directly from the project directory.
    This ensures agents always have architectural context even when
    RAG is empty or unavailable.

    Args:
        skill_name: Name of the executing skill
        project_dir: Project directory

    Returns:
        Documentation content string

    """
    doc_parts: list[str] = []

    # 1. Try ContextManifest (structured docs)
    try:
        from pathlib import Path

        from .context_manifest import build_structural_template, get_manifest

        project_path = Path(project_dir) if project_dir else None
        manifest = get_manifest(project_path)

        if manifest:
            template = build_structural_template(
                manifest=manifest,
                skill_name=skill_name,
                project_context="",
                global_context="",
            )
            if template:
                doc_parts.append(template)
                logger.debug(f"Loaded manifest documentation for skill: {skill_name}")

    except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
        logger.debug(f"Manifest documentation failed: {e}")

    # 2. ALWAYS load project doc files (CLAUDE.md, GEMINI.md, README.md)
    # These contain critical architectural context that prevents blind searching
    try:
        from pathlib import Path

        search_dirs: list[Path] = []
        if project_dir:
            search_dirs.append(Path(project_dir))
            search_dirs.append(Path(project_dir).parent)
        else:
            try:
                ws = get_workspace_root()
                search_dirs.append(ws)
                search_dirs.append(ws.parent)
            except (OSError, RuntimeError, ValueError, AttributeError) as exc:
                logger.warning(
                    "Cannot resolve the workspace root (%s); it is excluded from the "
                    "project-doc search path, so CLAUDE.md/GEMINI.md/README.md stored "
                    "there will not be hydrated.",
                    exc,
                )

        doc_files = ["CLAUDE.md", "GEMINI.md", "README.md"]
        loaded_docs: list[str] = []
        seen_paths: set[str] = set()

        for search_dir in search_dirs:
            for doc_name in doc_files:
                doc_path = search_dir / doc_name
                real_path = str(doc_path.resolve())
                if real_path in seen_paths:
                    continue
                seen_paths.add(real_path)

                if doc_path.exists() and doc_path.is_file():
                    try:
                        content = doc_path.read_text(encoding="utf-8")
                        if content.strip():
                            # Cap each doc at 2000 chars to stay within token budget
                            if len(content) > 2000:
                                content = content[:2000] + "\n... [TRUNCATED]"
                            loaded_docs.append(f"### {doc_name} ({search_dir})\n{content}")
                            logger.debug(f"Loaded project doc: {doc_path}")
                    except Exception as read_err:  # ruff: ignore[BLE001]  # broad catch intentional
                        logger.debug(f"Failed to read {doc_path}: {read_err}")

        if loaded_docs:
            doc_parts.append(
                "## Project Documentation (READ THIS FIRST)\n"
                "The following documentation describes the project architecture. "
                "Use this to understand the system BEFORE searching.\n\n" + "\n\n".join(loaded_docs)
            )

    except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
        logger.debug(f"Project doc loading failed: {e}")

    return "\n\n".join(doc_parts)


async def _hydrate_from_fold(
    query: str,
    top_k: int = 5,
) -> list[dict[str, Any]]:
    """Retrieve relevant context from compressed fold data.

    When context has been folded/compacted, this function queries the
    CompressedStore to find relevant chunks from previously folded context.
    The decompressed embeddings enable semantic similarity search against
    the query, recovering information that was truncated during compaction.

    Args:
        query: Query string for semantic search.
        top_k: Maximum chunks to return.

    Returns:
        List of dicts with keys: fold_id, index, similarity,
        text_preview, text_hash, char_start, char_end.

    """
    try:
        from ..context.compressed_store import get_compressed_store

        store = get_compressed_store()
        folds = store.list_folds()

        if not folds:
            logger.debug("[hydration/fold] No compressed folds available")
            return []

        results = store.query_all_folds(query, top_k=top_k)

        if not results:
            logger.debug("[hydration/fold] No relevant chunks found in folds")
            return []

        # Filter by minimum similarity threshold
        min_similarity = 0.5
        filtered = [r for r in results if r.get("similarity", 0) >= min_similarity]

        logger.info(
            f"[hydration/fold] Recovered {len(filtered)}/{len(results)} "
            f"chunks from {len(folds)} fold(s) for query"
        )

        return filtered

    except ImportError:
        logger.debug("[hydration/fold] compressed_store module not available")
    except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
        logger.debug(f"[hydration/fold] Fold retrieval failed: {e}")

    return []


async def _hydrate_session_episodes(
    query: str, project_dir: str | None, session_id: str = ""
) -> list[dict[str, Any]]:
    """Load recent session episodes.

    Args:
        query: Query string
        project_dir: Project directory

    Returns:
        List of episode dictionaries

    """
    try:
        from ..infrastructure.session_memory import SessionMemory

        # Get session memory — use the session_id from state if available
        memory = SessionMemory(session_id=session_id)

        # Get recent episodes (method uses 'count' not 'limit')
        episodes = memory.get_recent_episodes(count=5)

        if episodes:
            logger.debug(f"Loaded {len(episodes)} session episodes")

        return episodes  # type: ignore[return-value]

    except ImportError:
        logger.debug("SessionMemory not available")
    except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
        logger.debug(f"Session episode hydration failed: {e}")

    return []


# ── Integration with LangGraph ─────────────────────────────────────────────────


async def hydration_node(state: dict[str, Any]) -> dict[str, Any]:
    """LangGraph node for context hydration.

    Runs BEFORE execution_node to inject relevant context
    into the state for downstream nodes.

    Args:
        state: Current workflow state

    Returns:
        State update dict with hydrated_context

    """
    query = state.get("query", "")
    skill_name = state.get("skill_name", state.get("current_node", "unknown"))
    project_dir = state.get("project_dir")
    max_tokens = state.get("hydration_token_budget", 5000)

    # Check if hydration is already done
    if state.get("hydrated_context"):
        logger.debug("Context already hydrated, skipping")
        return {}

    logger.info(f"[hydration_node] Hydrating context for: {skill_name}")

    # Perform hydration
    result = await hydrate_context(
        query=query,
        skill_name=skill_name,
        state=state,
        max_tokens=max_tokens,
        project_dir=project_dir,
    )

    # Convert to context block
    context_block = result.to_context_block()

    # Build state update
    updates: dict[str, Any] = {
        "hydrated_context": context_block,
        "hydration_constraints": result.constraints,
        "hydration_code_chunks": result.code_chunks,
        "hydration_documentation": result.documentation,
        "hydration_tokens": result.total_tokens,
        "completed_nodes": ["hydration"],
    }

    # Log hydration stats
    logger.info(
        f"[hydration_node] Hydrated {result.total_tokens} tokens: "
        f"{len(result.constraints)} constraints, "
        f"{len(result.code_chunks)} code chunks, "
        f"{len(result.documentation)} chars docs"
    )

    return updates


def build_hydration_instruction(state: dict[str, Any]) -> str:
    """Build instruction for next node after hydration.

    Creates a prompt instruction that tells the next node
    to use the hydrated context.

    Args:
        state: Current workflow state with hydrated_context

    Returns:
        Instruction string for next node

    """
    hydrated = state.get("hydrated_context", "")

    if not hydrated:
        return ""

    return f"""
<context_hydration>
The following context has been pre-loaded for your use.
Reference this context when executing the task.

{hydrated}
</context_hydration>
"""


# ── Convenience function for nodes.py ─────────────────────────────────────────


def get_hydrated_context_for_skill(
    skill_name: str,
    query: str,
    state: dict[str, Any] | None = None,
) -> str:
    """Synchronous wrapper for context hydration.

    Can be called from nodes.py before execution to get
    hydrated context synchronously.

    Args:
        skill_name: Name of the skill
        query: User query
        state: Current workflow state

    Returns:
        Hydrated context block string

    """
    try:
        # Run async hydration in event loop
        asyncio.get_running_loop()

        # Create a task
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(asyncio.run, hydrate_context(query, skill_name, state))
            result = future.result(timeout=10)
            return result.to_context_block()

    except RuntimeError:
        # No running loop, safe to use asyncio.run
        result = asyncio.run(hydrate_context(query, skill_name, state))
        return result.to_context_block()
    except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
        logger.debug(f"Hydration failed: {e}")
        return ""


if __name__ == "__main__":
    # Demo: Test hydration
    import sys

    async def main():
        query = sys.argv[1] if len(sys.argv) > 1 else "Implement context hydration"
        skill = sys.argv[2] if len(sys.argv) > 2 else "sota-dev"

        logger.info(f"Hydrating context for: {skill}")
        logger.info(f"Query: {query}")
        logger.info("-" * 60)

        result = await hydrate_context(
            query=query,
            skill_name=skill,
            max_tokens=5000,
        )

        logger.info(f"\nConstraints: {len(result.constraints)}")
        logger.info(f"Code chunks: {len(result.code_chunks)}")
        logger.info(f"Documentation: {len(result.documentation)} chars")
        logger.info(f"Session episodes: {len(result.session_episodes)}")
        logger.info(f"Total tokens: ~{result.total_tokens}")
        logger.info("-" * 60)
        logger.info(result.to_context_block())

    asyncio.run(main())
