"""Virtual File System Archive for Large Context Objects.

Provides archival of large tool outputs and reasoning chains that exceed
token budgets. Archived content is replaced with URI pointers for later
retrieval.

Storage Path: ~/.cache/goose/vfs_archive/
File Format: JSON with metadata + content

Usage:
    - Pre-compaction: Archive large outputs (>2000 tokens)
    - Post-compaction: Retrieve via URI if needed
    - Context window: Replace with compact pointer
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("Beagle.vfs_archive")


@dataclass
class ArchivedContent:
    """Archived content with metadata.

    Attributes:
        uri: Unique identifier (vfs://archive/{hash})
        content_type: Type of content (tool_output, reasoning, etc.)
        original_tokens: Token count before archival
        content: The actual content (compressed)
        created_at: Archival timestamp
        session_id: Source session
        workflow_id: Source workflow
        tags: Searchable tags
        metadata: Additional metadata

    """

    uri: str
    content_type: str
    original_tokens: int
    content: str
    created_at: float = field(default_factory=time.time)
    session_id: str = ""
    workflow_id: str = ""
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        """Serialize to JSON."""
        return {
            "uri": self.uri,
            "content_type": self.content_type,
            "original_tokens": self.original_tokens,
            "content": self.content,
            "created_at": self.created_at,
            "session_id": self.session_id,
            "workflow_id": self.workflow_id,
            "tags": self.tags,
            "metadata": self.metadata,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ArchivedContent:
        """Deserialize from JSON."""
        return cls(
            uri=data.get("uri", ""),
            content_type=data.get("content_type", "unknown"),
            original_tokens=data.get("original_tokens", 0),
            content=data.get("content", ""),
            created_at=data.get("created_at", time.time()),
            session_id=data.get("session_id", ""),
            workflow_id=data.get("workflow_id", ""),
            tags=data.get("tags", []),
            metadata=data.get("metadata", {}),
        )


class VFSArchive:
    """Virtual File System Archive for large context objects.

    Provides content-addressable storage with compression.
    Archives are stored in ~/.cache/goose/vfs_archive/ and can be
    retrieved by URI.
    """

    def __init__(self, archive_dir: Path | None = None):
        """Initialize the archive.

        Args:
            archive_dir: Directory for archive storage (default: ~/.cache/goose/vfs_archive/)

        """
        if archive_dir:
            self._archive_dir = archive_dir
        else:
            self._archive_dir = Path.home() / ".cache" / "goose" / "vfs_archive"

        self._archive_dir.mkdir(parents=True, exist_ok=True)
        self._index: dict[str, str] = {}  # uri -> file path
        self._load_index()

    def _load_index(self) -> None:
        """Load archive index from disk."""
        index_path = self._archive_dir / "index.json"
        if index_path.exists():
            try:
                with open(index_path, encoding="utf-8") as f:
                    self._index = json.load(f)
                logger.debug(f"Loaded VFS index with {len(self._index)} entries")
            except (OSError, ValueError) as e:
                logger.warning(f"Failed to load VFS index: {e}")
                self._index = {}

    def _save_index(self) -> None:
        """Save archive index to disk."""
        index_path = self._archive_dir / "index.json"
        try:
            temp_path = index_path.with_suffix(".tmp")
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(self._index, f, indent=2)
            temp_path.replace(index_path)
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as e:
            logger.error(f"Failed to save VFS index: {e}")

    def _content_hash(self, content: str) -> str:
        """Generate content-addressable hash.

        Args:
            content: Content to hash

        Returns:
            SHA-256 hash of content

        """
        return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]

    def _compress_content(self, content: str) -> str:
        """Compress content for storage.

        Args:
            content: Content to compress

        Returns:
            Base64-encoded compressed content

        """
        import base64

        compressed = gzip.compress(content.encode("utf-8"), compresslevel=6)
        return base64.b64encode(compressed).decode("utf-8")

    def _decompress_content(self, compressed: str) -> str:
        """Decompress content from storage.

        Args:
            compressed: Base64-encoded compressed content

        Returns:
            Decompressed content string

        """
        import base64

        data = base64.b64decode(compressed.encode("utf-8"))
        return gzip.decompress(data).decode("utf-8")

    def archive(
        self,
        content: str,
        content_type: str = "tool_output",
        session_id: str = "",
        workflow_id: str = "",
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Archive content to VFS.

        Args:
            content: Content to archive
            content_type: Type of content
            session_id: Source session
            workflow_id: Source workflow
            tags: Searchable tags
            metadata: Additional metadata

        Returns:
            URI for archived content (vfs://archive/{hash})

        """
        # Generate content-addressable URI
        content_hash = self._content_hash(content)
        uri = f"vfs://archive/{content_hash}"

        # Check if already archived
        if uri in self._index:
            logger.debug(f"Content already archived: {uri}")
            return uri

        # Estimate tokens
        original_tokens = len(content) // 4  # ~4 chars per token

        # Compress content
        compressed = self._compress_content(content)

        # Create archived content
        archived = ArchivedContent(
            uri=uri,
            content_type=content_type,
            original_tokens=original_tokens,
            content=compressed,
            session_id=session_id,
            workflow_id=workflow_id,
            tags=tags or [],
            metadata=metadata or {},
        )

        # Save to disk
        archive_path = self._archive_dir / f"{content_hash}.json"
        try:
            temp_path = archive_path.with_suffix(".tmp")
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(archived.to_json(), f, indent=2)
            temp_path.replace(archive_path)

            # Update index
            self._index[uri] = str(archive_path)
            self._save_index()

            compression_ratio = len(compressed) / len(content) if len(content) > 0 else 0
            logger.info(
                f"Archived {original_tokens} tokens ({content_type}) to {uri} "
                f"(compression: {compression_ratio:.1%})"
            )

            return uri

        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as e:
            logger.error(f"Failed to archive content: {e}")
            return ""

    def retrieve(self, uri: str) -> str | None:
        """Retrieve archived content by URI.

        Args:
            uri: URI of archived content

        Returns:
            Content string if found, None otherwise

        """
        if uri not in self._index:
            logger.warning(f"Archive not found: {uri}")
            return None

        archive_path = Path(self._index[uri])
        if not archive_path.exists():
            logger.warning(f"Archive file missing: {archive_path}")
            del self._index[uri]
            self._save_index()
            return None

        try:
            with open(archive_path, encoding="utf-8") as f:
                data = json.load(f)

            archived = ArchivedContent.from_json(data)
            content = self._decompress_content(archived.content)

            logger.debug(f"Retrieved {archived.original_tokens} tokens from {uri}")
            return content

        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as e:
            logger.error(f"Failed to retrieve archive {uri}: {e}")
            return None

    def get_pointer(self, uri: str, preview_lines: int = 5) -> str:
        """Get a compact pointer for context injection.

        Replaces full content with a pointer + preview that fits in context.

        Args:
            uri: URI of archived content
            preview_lines: Number of lines to show in preview

        Returns:
            Compact pointer string

        """
        content = self.retrieve(uri)
        if not content:
            return f"[ARCHIVE NOT FOUND: {uri}]"

        lines = content.splitlines()
        header = "\n".join(lines[:preview_lines])
        footer = "\n".join(lines[-preview_lines:]) if len(lines) > preview_lines * 2 else ""

        total_lines = len(lines)
        hidden_lines = total_lines - (2 * preview_lines)

        pointer = f"""
<archived_content uri="{uri}" original_tokens="{len(content) // 4}">
<preview>
{header}
</preview>
<hidden lines="{hidden_lines}">
... [{hidden_lines} lines archived - use URI to retrieve full content] ...
</hidden>
<footer>
{footer}
</footer>
</archived_content>
"""
        return pointer.strip()

    def archive_if_large(
        self,
        content: str,
        token_threshold: int = 2000,
        **kwargs,
    ) -> tuple[str, bool]:
        """Archive content if it exceeds token threshold.

        Args:
            content: Content to potentially archive
            token_threshold: Token threshold for archival
            **kwargs: Additional arguments for archive()

        Returns:
            Tuple of (content_or_pointer, was_archived)

        """
        estimated_tokens = len(content) // 4

        if estimated_tokens > token_threshold:
            uri = self.archive(content, **kwargs)
            if uri:
                pointer = self.get_pointer(uri)
                return pointer, True

        return content, False

    def cleanup_old_archives(self, max_age_days: int = 30) -> int:
        """Clean up old archives to free disk space.

        Args:
            max_age_days: Maximum age in days (default: 30)

        Returns:
            Number of archives removed

        """
        # wall-clock-ok: compares against a persisted timestamp
        cutoff = time.time() - (max_age_days * 86400)
        removed = 0

        for uri, path_str in list(self._index.items()):
            try:
                path = Path(path_str)
                if not path.exists():
                    del self._index[uri]
                    continue

                mtime = path.stat().st_mtime
                if mtime < cutoff:
                    path.unlink()
                    del self._index[uri]
                    removed += 1
            except OSError as e:
                logger.warning(f"Failed to cleanup {uri}: {e}")

        if removed > 0:
            self._save_index()
            logger.info(f"Cleaned up {removed} old archives")

        return removed

    def get_stats(self) -> dict[str, Any]:
        """Get archive statistics.

        Returns:
            Dict with archive stats

        """
        total_size = 0
        total_tokens = 0
        by_type: dict[str, int] = {}

        for _uri, path_str in self._index.items():
            try:
                path = Path(path_str)
                if path.exists():
                    stat = path.stat()
                    total_size += stat.st_size

                    with open(path, encoding="utf-8") as f:
                        data = json.load(f)

                    archived = ArchivedContent.from_json(data)
                    total_tokens += archived.original_tokens
                    by_type[archived.content_type] = by_type.get(archived.content_type, 0) + 1
            except (OSError, ValueError) as exc:
                logger.warning(
                    "Cannot read archived content at %s (%s); it is excluded from the "
                    "reported size, token and type totals, which are therefore low.",
                    path_str,
                    exc,
                )

        return {
            "total_archives": len(self._index),
            "total_size_bytes": total_size,
            "total_size_mb": round(total_size / (1024 * 1024), 2),
            "total_tokens": total_tokens,
            "by_type": by_type,
        }


# ── Module-level singleton ────────────────────────────────────────────────────

_vfs_archive: VFSArchive | None = None


def get_vfs_archive() -> VFSArchive:
    """Get the VFS archive singleton."""
    global _vfs_archive
    if _vfs_archive is None:
        _vfs_archive = VFSArchive()
    return _vfs_archive


# ── Convenience functions for context compaction hook ───────────────────────────


def archive_tool_outputs(
    outputs: dict[str, str],
    token_threshold: int = 2000,
    session_id: str = "",
    workflow_id: str = "",
) -> dict[str, str]:
    """Archive large tool outputs, replacing with pointers.

    Args:
        outputs: Dict of output_key -> content
        token_threshold: Token threshold for archival
        session_id: Source session
        workflow_id: Source workflow

    Returns:
        Dict with large outputs replaced by URI pointers

    """
    archive = get_vfs_archive()
    result: dict[str, str] = {}

    for key, content in outputs.items():
        if not content:
            result[key] = content
            continue

        archived_content, was_archived = archive.archive_if_large(
            content,
            token_threshold=token_threshold,
            content_type="tool_output",
            session_id=session_id,
            workflow_id=workflow_id,
            tags=[key],
            metadata={"output_key": key},
        )

        result[key] = archived_content

        if was_archived:
            logger.info(f"Archived large tool output: {key}")

    return result


if __name__ == "__main__":
    # Demo: Test VFS archive

    archive = get_vfs_archive()

    # Create large content
    large_content = "x" * 5000  # 5000 chars = ~1250 tokens

    # Archive it
    uri = archive.archive(
        content=large_content,
        content_type="test",
        session_id="demo",
    )

    logger.info(f"Archived to: {uri}")

    # Get compact pointer
    pointer = archive.get_pointer(uri)
    logger.info(f"\nCompact pointer ({len(pointer)} chars):")
    logger.info(pointer)

    # Retrieve full content
    retrieved = archive.retrieve(uri)
    logger.info(f"\nRetrieved content matches: {retrieved == large_content}")

    # Stats
    logger.info(f"\nArchive stats: {archive.get_stats()}")
