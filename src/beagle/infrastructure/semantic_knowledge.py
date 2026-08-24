"""Semantic Knowledge Index for session knowledge persistence.

Extracts, stores, and retrieves knowledge learned during sessions across
compaction boundaries. Uses RAG-style semantic search for retrieval.

Key concepts:
- KnowledgeEntry: A learned piece of information (concept, pattern, decision)
- SemanticKnowledgeIndex: Collection with semantic search capability
- KnowledgeExtractor: Extracts knowledge from conversation content

Storage:
    ~/.cache/beagle/knowledge/
        ├── entries.jsonl       # All knowledge entries (JSONL for append)
        └── embeddings.npy      # Pre-computed embeddings (optional)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from beagle.config.paths import (
    get_cache_root,
)

logger = logging.getLogger("Beagle.semantic_knowledge")

# Import path management


def get_knowledge_dir() -> Path:
    """Get knowledge storage directory."""
    path = get_cache_root() / "knowledge"
    path.mkdir(parents=True, exist_ok=True)
    return path


class KnowledgeCategory:
    """Categories for knowledge classification."""

    CONCEPT = "concept"  # A learned concept or idea
    PATTERN = "pattern"  # A code pattern or best practice
    DECISION = "decision"  # An architectural decision
    API = "api"  # API usage or signature
    ERROR = "error"  # Error and its resolution
    CONTEXT = "context"  # Project-specific context
    PREFERENCE = "preference"  # User preference learned from interaction
    INSIGHT = "insight"  # Insight derived from analysis


@dataclass
class KnowledgeEntry:
    """A piece of knowledge extracted from session content.

    Attributes:
        id: Unique identifier (hash of content)
        category: Type of knowledge
        title: Brief title/summary
        content: Full knowledge content
        source: Where this knowledge came from (session, file, etc.)
        project: Project this knowledge applies to
        confidence: Confidence score 0-1
        importance: Importance score 0-1 for retrieval ranking
        created_at: When this knowledge was created
        last_accessed: When this knowledge was last retrieved
        access_count: Number of times this knowledge has been retrieved
        tags: List of tags for filtering
        embedding: Optional pre-computed embedding (stored separately)
        related_ids: IDs of related knowledge entries
        metadata: Additional metadata

    """

    id: str = ""
    category: str = KnowledgeCategory.CONCEPT
    title: str = ""
    content: str = ""
    source: str = ""
    project: str = ""
    confidence: float = 0.8
    importance: float = 0.5
    created_at: float = field(default_factory=time.time)
    last_accessed: float = field(default_factory=time.time)
    access_count: int = 0
    tags: list[str] = field(default_factory=list)
    embedding: list[float] | None = None
    related_ids: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        """Generate ID from content if not provided."""
        if not self.id:
            # Generate deterministic ID from content hash.
            # 16 hex chars = 64 bits of entropy — wide enough that a
            # collision (which would silently overwrite an existing entry at
            # index.add) is cryptographically negligible. The earlier [:8]
            # (32 bits) was a live collision risk for large knowledge bases.
            content_hash = hashlib.sha256(self.content.encode()).hexdigest()[:16]
            self.id = f"{self.category[:3]}_{content_hash}"

    def to_json(self) -> dict[str, Any]:
        """Serialize to JSON-compatible dict."""
        return {
            "id": self.id,
            "category": self.category,
            "title": self.title,
            "content": self.content,
            "source": self.source,
            "project": self.project,
            "confidence": self.confidence,
            "importance": self.importance,
            "created_at": self.created_at,
            "last_accessed": self.last_accessed,
            "access_count": self.access_count,
            "tags": self.tags,
            "related_ids": self.related_ids,
            "metadata": self.metadata,
            "version": "1.0",
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> KnowledgeEntry:
        """Deserialize from JSON dict."""
        return cls(
            id=data.get("id", ""),
            category=data.get("category", KnowledgeCategory.CONCEPT),
            title=data.get("title", ""),
            content=data.get("content", ""),
            source=data.get("source", ""),
            project=data.get("project", ""),
            confidence=data.get("confidence", 0.8),
            importance=data.get("importance", 0.5),
            created_at=data.get("created_at", time.time()),
            last_accessed=data.get("last_accessed", time.time()),
            access_count=data.get("access_count", 0),
            tags=data.get("tags", []),
            related_ids=data.get("related_ids", []),
            metadata=data.get("metadata", {}),
        )

    def access(self) -> None:
        """Mark this entry as accessed."""
        self.last_accessed = time.time()
        self.access_count += 1

    def format_for_context(self, max_length: int = 500) -> str:
        """Format for injection into context.

        Args:
            max_length: Maximum content length

        Returns:
            Formatted string for context

        """
        content = self.content
        if len(content) > max_length:
            content = content[: max_length - 3] + "..."

        category_emoji = {
            KnowledgeCategory.CONCEPT: "💡",
            KnowledgeCategory.PATTERN: "🔧",
            KnowledgeCategory.DECISION: "📋",
            KnowledgeCategory.API: "🔌",
            KnowledgeCategory.ERROR: "⚠️",
            KnowledgeCategory.CONTEXT: "📍",
            KnowledgeCategory.PREFERENCE: "👍",
            KnowledgeCategory.INSIGHT: "💭",
        }.get(self.category, "📝")

        return f"{category_emoji} [{self.category.upper()}] {self.title}\n{content}"


class SemanticKnowledgeIndex:
    """Index for semantic knowledge storage and retrieval.

    Uses a combination of:
    - Keyword matching for fast exact lookup
    - Optional embedding similarity for semantic search
    - Importance/confidence scoring for ranking

    Attributes:
        entries: Dictionary of knowledge entries by ID
        project: Project filter for entries

    """

    def __init__(self, project: str = ""):
        """Initialize the knowledge index.

        Args:
            project: Optional project filter

        """
        self.project = project
        self.entries: dict[str, KnowledgeEntry] = {}
        self._dirty = False

    def add(self, entry: KnowledgeEntry, check_duplicates: bool = True) -> bool:
        """Add a knowledge entry to the index.

        Args:
            entry: Entry to add
            check_duplicates: Whether to check for duplicates

        Returns:
            True if added, False if duplicate

        """
        if check_duplicates:
            # Check for duplicate by content similarity
            for existing in self.entries.values():
                if self._is_duplicate(existing, entry):
                    logger.debug(f"Skipping duplicate knowledge: {entry.title}")
                    return False

        # Set project if this index has one
        if self.project and not entry.project:
            entry.project = self.project

        self.entries[entry.id] = entry
        self._dirty = True
        return True

    def _is_duplicate(self, a: KnowledgeEntry, b: KnowledgeEntry) -> bool:
        """Check if two entries are duplicates."""
        # Same content
        if a.content == b.content:
            return True

        # Similar title (fuzzy match)
        if a.title and b.title:
            a_words = set(a.title.lower().split())
            b_words = set(b.title.lower().split())
            overlap = len(a_words & b_words) / max(len(a_words), len(b_words), 1)
            if overlap > 0.8:
                return True

        return False

    def get(self, entry_id: str) -> KnowledgeEntry | None:
        """Get an entry by ID."""
        entry = self.entries.get(entry_id)
        if entry:
            entry.access()
        return entry

    def search(
        self,
        query: str,
        category: str | None = None,
        tags: list[str] | None = None,
        limit: int = 10,
    ) -> list[KnowledgeEntry]:
        """Search for knowledge entries.

        Args:
            query: Search query (keywords)
            category: Optional category filter
            tags: Optional tag filters
            limit: Maximum results

        Returns:
            List of matching entries ranked by relevance

        """
        results = []
        query_lower = query.lower()
        query_words = set(query_lower.split())

        for entry in self.entries.values():
            # Category filter
            if category and entry.category != category:
                continue

            # Tag filter
            if tags and not all(t in entry.tags for t in tags):
                continue

            # Relevance scoring
            score = self._calculate_relevance(entry, query_words)
            if score > 0:
                results.append((entry, score))

        # Sort by relevance, then importance
        results.sort(key=lambda x: (x[1], x[0].importance), reverse=True)

        # Update access for returned entries
        for entry, _ in results[:limit]:
            entry.access()

        return [e for e, _ in results[:limit]]

    def _calculate_relevance(
        self,
        entry: KnowledgeEntry,
        query_words: set[str],
    ) -> float:
        """Calculate relevance score for an entry.

        Args:
            entry: Entry to score
            query_words: Query word set

        Returns:
            Relevance score (0-1)

        """
        title_lower = entry.title.lower()
        content_lower = entry.content.lower()

        # Title matches (weight: 0.4)
        title_words = set(title_lower.split())
        title_overlap = len(query_words & title_words) / max(len(query_words), 1)
        title_score = 0.4 * title_overlap

        # Content matches (weight: 0.3)
        content_words = set(content_lower.split())
        content_overlap = len(query_words & content_words) / max(len(query_words), 1)
        content_score = 0.3 * min(content_overlap, 1.0)

        # Tag matches (weight: 0.2)
        tag_words = set(" ".join(entry.tags).lower().split())
        tag_overlap = len(query_words & tag_words) / max(len(query_words), 1) if query_words else 0
        tag_score = 0.2 * tag_overlap

        # Importance boost (weight: 0.1)
        importance_score = 0.1 * entry.importance

        return title_score + content_score + tag_score + importance_score

    def get_for_context(
        self,
        max_entries: int = 5,
        max_tokens: int = 2000,
        categories: list[str] | None = None,
    ) -> list[KnowledgeEntry]:
        """Get entries for context injection.

        Args:
            max_entries: Maximum number of entries
            max_tokens: Token budget for entries
            categories: Optional category filter

        Returns:
            List of entries sorted by importance

        """
        # Filter by category if specified
        candidates = list(self.entries.values())
        if categories:
            candidates = [e for e in candidates if e.category in categories]

        # Sort by importance and access count
        candidates.sort(key=lambda e: (e.importance, e.access_count), reverse=True)

        # Token budget
        result = []
        token_estimate = 0

        for entry in candidates:
            # Rough token estimate: ~4 chars per token
            entry_tokens = len(entry.content) // 4 + 50  # 50 overhead per entry

            if token_estimate + entry_tokens <= max_tokens:
                result.append(entry)
                token_estimate += entry_tokens
                if len(result) >= max_entries:
                    break

        return result

    def get_recent(self, days: int = 7, limit: int = 20) -> list[KnowledgeEntry]:
        """Get recently created entries.

        Args:
            days: Number of days to look back
            limit: Maximum entries

        Returns:
            List of recent entries

        """
        # wall-clock-ok: compares against a persisted timestamp
        cutoff = time.time() - (days * 24 * 3600)  # nosemgrep: aeca-walltime-for-interval
        recent = [e for e in self.entries.values() if e.created_at >= cutoff]
        recent.sort(key=lambda e: e.created_at, reverse=True)
        return recent[:limit]

    def load(self, path: Path | None = None) -> None:
        """Load knowledge entries from disk.

        Args:
            path: Optional explicit path (default: knowledge_dir/entries.jsonl)

        """
        if path is None:
            path = get_knowledge_dir() / "entries.jsonl"

        if not path.exists():
            return

        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        entry = KnowledgeEntry.from_json(data)
                        # Project filter: load if no project set OR project matches
                        if not self.project or not entry.project or entry.project == self.project:
                            self.entries[entry.id] = entry
                    except json.JSONDecodeError as e:
                        logger.warning(f"Invalid knowledge entry: {e}")

            logger.info(f"Loaded {len(self.entries)} knowledge entries")
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as e:
            logger.error(f"Failed to load knowledge entries: {e}")

    def save(self, path: Path | None = None) -> None:
        """Save knowledge entries to disk.

        Uses JSONL format for append-friendly storage.

        Args:
            path: Optional explicit path

        """
        if not self._dirty:
            return

        if path is None:
            path = get_knowledge_dir() / "entries.jsonl"

        path.parent.mkdir(parents=True, exist_ok=True)

        try:
            # Write all entries (JSONL)
            temp_path = path.with_suffix(".tmp")
            with open(temp_path, "w", encoding="utf-8") as f:
                for entry in self.entries.values():
                    f.write(json.dumps(entry.to_json()) + "\n")
            os.replace(temp_path, path)

            self._dirty = False
            logger.debug(f"Saved {len(self.entries)} knowledge entries")
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as e:
            logger.error(f"Failed to save knowledge entries: {e}")

    def merge(self, other: SemanticKnowledgeIndex) -> int:
        """Merge entries from another index.

        Args:
            other: Index to merge from

        Returns:
            Number of entries added

        """
        added = 0
        for entry in other.entries.values():
            if self.add(entry, check_duplicates=True):
                added += 1
        return added

    def format_for_prompt(self, max_tokens: int = 2000) -> str:
        """Format entries for prompt injection.

        Args:
            max_tokens: Token budget

        Returns:
            Formatted string

        """
        entries = self.get_for_context(max_tokens=max_tokens)

        if not entries:
            return ""

        lines = ["", "## Learned Knowledge", ""]
        lines.append("The following knowledge was learned from previous sessions:")
        lines.append("")

        for entry in entries:
            lines.append(entry.format_for_context())
            lines.append("")

        lines.append("---")
        return "\n".join(lines)

    def __len__(self) -> int:
        """Return number of entries."""
        return len(self.entries)

    def __enter__(self) -> SemanticKnowledgeIndex:
        """Context manager entry."""
        self.load()
        return self

    def __exit__(self, _exc_type, _exc_val, _exc_tb) -> None:
        """Context manager exit."""
        self.save()
