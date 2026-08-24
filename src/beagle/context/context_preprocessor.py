"""Context Preprocessor - Prevents context overflow from large files.

Pre-processes files before they're sent to Goose by:
- Detecting size limits
- Chunking large files intelligently
- Preserving semantic boundaries
- Maintaining context continuity across chunks
"""

from __future__ import annotations

import contextlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("Beagle.context.context_preprocessor")

# Goose's actual limits (from strings extraction)
# Default ~500KB for file references, but can vary
DEFAULT_FILE_SIZE_LIMIT = 500_000  # 500KB
DEFAULT_LINE_LIMIT = 10_000  # Lines before chunking

# Context window thresholds
DEFAULT_CONTEXT_WARNING = 0.70  # 70% - compress here
DEFAULT_CONTEXT_CRITICAL = 0.80  # 80% - aggressive action


@dataclass
class ChunkMetadata:
    """Metadata for a file chunk."""

    chunk_index: int
    total_chunks: int
    start_line: int
    end_line: int
    original_size: int
    chunk_size: int
    boundary_type: str  # 'function', 'class', 'section', 'line'
    is_significant: bool  # Contains important definitions


@dataclass
class FileChunkPlan:
    """Plan for chunking a file."""

    file_path: str
    original_size: int
    original_lines: int
    chunks: list[ChunkMetadata]
    strategy: str  # 'semantic', 'line', 'section'
    estimated_tokens: int


@dataclass
class ProcessedFile:
    """Result of preprocessing a file."""

    original_path: str
    chunks: list[str]  # Actual chunk contents
    metadata: list[ChunkMetadata]
    total_tokens: int
    compression_ratio: float = 1.0
    needs_chunking: bool = False


class ContextPreprocessor:
    """Pre-processes files to prevent context overflow."""

    def __init__(
        self,
        file_size_limit: int = DEFAULT_FILE_SIZE_LIMIT,
        line_limit: int = DEFAULT_LINE_LIMIT,
        context_warning: float = DEFAULT_CONTEXT_WARNING,
        context_critical: float = DEFAULT_CONTEXT_CRITICAL,
    ):
        self.file_size_limit = file_size_limit
        self.line_limit = line_limit
        self.context_warning = context_warning
        self.context_critical = context_critical

        # Cache for already processed files (bounded to prevent memory leaks)
        self._cache: dict[str, ProcessedFile] = {}
        self._cache_max_entries = 500

    def estimate_tokens(self, text: str) -> int:
        """Estimate token count for text (rough: 1 token ≈ 4 chars)."""
        # More accurate: use tiktoken if available
        try:
            import tiktoken

            enc = tiktoken.get_encoding("o200k_base")
            return len(enc.encode(text))
        except ImportError:
            # Fallback: rough estimation
            # Code: ~3.5 chars/token, prose: ~4.5 chars/token
            # Use 4 as average
            return len(text) // 4

    def analyze_file(
        self,
        file_path: str,
        content: str | None = None,
    ) -> FileChunkPlan:
        """Analyze a file and create a chunking plan.

        Args:
            file_path: Path to the file
            content: Optional content (if already loaded)

        Returns:
            Chunking plan for the file

        """
        path = Path(file_path)

        if content is None:
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
                logger.warning(f"Could not read {file_path}: {e}")
                return FileChunkPlan(
                    file_path=file_path,
                    original_size=0,
                    original_lines=0,
                    chunks=[],
                    strategy="none",
                    estimated_tokens=0,
                )

        lines = content.splitlines()
        original_size = len(content)
        original_lines = len(lines)
        estimated_tokens = self.estimate_tokens(content)

        # Determine if chunking is needed
        needs_chunking = original_size > self.file_size_limit or original_lines > self.line_limit

        if not needs_chunking:
            # Single chunk
            return FileChunkPlan(
                file_path=file_path,
                original_size=original_size,
                original_lines=original_lines,
                chunks=[
                    ChunkMetadata(
                        chunk_index=0,
                        total_chunks=1,
                        start_line=0,
                        end_line=original_lines,
                        original_size=original_size,
                        chunk_size=original_size,
                        boundary_type="full",
                        is_significant=True,
                    )
                ],
                strategy="none",
                estimated_tokens=estimated_tokens,
            )

        # Create semantic chunking plan
        return self._create_semantic_chunk_plan(file_path, content, lines)

    def _create_semantic_chunk_plan(
        self,
        file_path: str,
        content: str,
        lines: list[str],
    ) -> FileChunkPlan:
        """Create a semantic-aware chunking plan.

        Respects:
        - Function/class boundaries
        - Section markers (comments)
        - Logical divisions
        """
        # Find semantic boundaries
        boundaries = self._find_semantic_boundaries(file_path, lines)

        # Calculate optimal chunk size based on file size limit
        optimal_lines = min(self.line_limit, len(lines))
        if len(content) > self.file_size_limit:
            # Reduce lines to fit size limit
            avg_line_length = len(content) / len(lines) if lines else 80
            optimal_lines = int(self.file_size_limit / avg_line_length * 0.9)  # 10% buffer

        # Create chunks respecting boundaries
        chunks = self._create_chunks_from_boundaries(boundaries, len(lines), optimal_lines)

        total_tokens = sum(self.estimate_tokens(content[c.start_line : c.end_line]) for c in chunks)

        return FileChunkPlan(
            file_path=file_path,
            original_size=len(content),
            original_lines=len(lines),
            chunks=chunks,
            strategy="semantic",
            estimated_tokens=total_tokens,
        )

    def _find_semantic_boundaries(
        self,
        file_path: str,
        lines: list[str],
    ) -> list[int]:
        """Find semantic boundaries in code files."""
        boundaries = [0]  # Start of file
        ext = Path(file_path).suffix.lower()

        # Python boundaries
        if ext == ".py":
            for i, line in enumerate(lines):
                # Class definitions
                if (
                    re.match(r"^(class|async\s+class)\s+\w+", line)
                    or re.match(r"^(async\s+)?def\s+\w+", line)
                    or re.match(r"^#\s*={3,}|^#\s*-{3,}", line)
                ):
                    boundaries.append(i)

        # JavaScript/TypeScript boundaries
        elif ext in (".js", ".ts", ".jsx", ".tsx"):
            for i, line in enumerate(lines):
                if re.match(
                    r"^(export\s+)?(class|function|async\s+function|const|interface|type)\s+",
                    line,
                ):
                    boundaries.append(i)

        # Generic boundaries for other languages
        else:
            for i, line in enumerate(lines):
                # Section markers
                if re.match(r"^[#/;\-]{3,}", line) or re.match(
                    r"^(class|struct|fn|func|def|function|public|private)\s+",
                    line,
                    re.IGNORECASE,
                ):
                    boundaries.append(i)

        boundaries.append(len(lines))  # End of file
        return sorted(set(boundaries))

    def _create_chunks_from_boundaries(
        self,
        boundaries: list[int],
        total_lines: int,
        target_lines: int,
    ) -> list[ChunkMetadata]:
        """Create chunks from boundary positions."""
        chunks = []  # type: ignore[var-annotated]
        chunk_start = boundaries[0]

        for i, boundary in enumerate(boundaries[1:], start=1):
            # Check if we should create a new chunk
            lines_since_start = boundary - chunk_start

            # Create chunk if:
            # 1. We've reached target lines
            # 2. Next boundary is far (don't extend too much)
            # 3. This is the last boundary
            remaining_boundaries = len(boundaries) - i
            is_last = remaining_boundaries == 0

            if is_last or lines_since_start >= target_lines:
                chunk_end = boundary
                chunk_lines = lines_since_start

                # Determine if chunk contains significant content
                is_significant = chunk_lines > target_lines * 0.3

                chunks.append(
                    ChunkMetadata(
                        chunk_index=len(chunks),
                        total_chunks=0,  # Will update after
                        start_line=chunk_start,
                        end_line=chunk_end,
                        original_size=0,  # Will calculate
                        chunk_size=0,  # Will calculate
                        boundary_type="semantic",
                        is_significant=is_significant,
                    )
                )
                chunk_start = boundary

        # Update total_chunks and sizes
        for chunk in chunks:
            chunk.total_chunks = len(chunks)

        return chunks

    def preprocess_file(
        self,
        file_path: str,
        content: str | None = None,
        _max_chunk_tokens: int | None = None,
    ) -> ProcessedFile:
        """Preprocess a file into chunks if needed.

        Args:
            file_path: Path to the file
            content: Optional content (if already loaded)
            _max_chunk_tokens: Maximum tokens per chunk (default: auto)

        Returns:
            ProcessedFile with chunks or original content

        """
        # Check cache
        cache_key = file_path
        if cache_key in self._cache:
            return self._cache[cache_key]

        path = Path(file_path)

        if content is None:
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
                logger.warning(f"Could not read {file_path}: {e}")
                return ProcessedFile(
                    original_path=file_path,
                    chunks=[],
                    metadata=[],
                    total_tokens=0,
                    needs_chunking=False,
                )

        # Analyze file
        plan = self.analyze_file(file_path, content)

        if not plan.chunks:
            # Empty or unreadable file
            return ProcessedFile(
                original_path=file_path,
                chunks=[],
                metadata=[],
                total_tokens=0,
                needs_chunking=False,
            )

        # File doesn't need chunking if it has only one chunk and original size is within limits
        if len(plan.chunks) == 1 and plan.original_size <= self.file_size_limit:
            # File doesn't need chunking
            result = ProcessedFile(
                original_path=file_path,
                chunks=[content],
                metadata=plan.chunks,
                total_tokens=plan.estimated_tokens,
                needs_chunking=False,
            )
            if len(self._cache) >= self._cache_max_entries:
                with contextlib.suppress(StopIteration):
                    del self._cache[next(iter(self._cache))]
            self._cache[cache_key] = result
            return result

        # Create actual chunks
        lines = content.splitlines(keepends=True)
        chunks = []
        metadata = []

        for chunk_meta in plan.chunks:
            chunk_content = "".join(lines[chunk_meta.start_line : chunk_meta.end_line])
            chunk_meta.chunk_size = len(chunk_content)
            chunk_meta.original_size = len(content)
            chunks.append(chunk_content)
            metadata.append(chunk_meta)

        result = ProcessedFile(
            original_path=file_path,
            chunks=chunks,
            metadata=metadata,
            total_tokens=plan.estimated_tokens,
            needs_chunking=True,
        )

        if len(self._cache) >= self._cache_max_entries:
            with contextlib.suppress(StopIteration):
                del self._cache[next(iter(self._cache))]
        self._cache[cache_key] = result
        return result

    def get_chunk_summary(
        self,
        processed: ProcessedFile,
        current_chunk: int = 0,
    ) -> str:
        """Generate a summary header for a chunked file presentation.

        Args:
            processed: Processed file
            current_chunk: Current chunk index being shown

        Returns:
            Summary string for context

        """
        if not processed.needs_chunking or not processed.metadata:
            return ""

        meta = processed.metadata[current_chunk]
        total = len(processed.chunks)

        return (
            f'<file_chunk file="{processed.original_path}" '
            f'chunk="{current_chunk + 1}/{total}" '
            f'lines="{meta.start_line + 1}-{meta.end_line}">\n'
        )

    def get_navigation_hints(
        self,
        processed: ProcessedFile,
        current_chunk: int = 0,
    ) -> str:
        """Generate navigation hints for chunked file.

        Args:
            processed: Processed file
            current_chunk: Current chunk index

        Returns:
            Navigation hint string

        """
        if not processed.needs_chunking or len(processed.chunks) <= 1:
            return ""

        processed.metadata[current_chunk]
        total = len(processed.chunks)

        hints = []
        if current_chunk > 0:
            prev_meta = processed.metadata[current_chunk - 1]
            hints.append(
                f"Previous chunk (lines {prev_meta.start_line + 1}-{prev_meta.end_line}) available"
            )

        if current_chunk < total - 1:
            next_meta = processed.metadata[current_chunk + 1]
            hints.append(
                f"Next chunk (lines {next_meta.start_line + 1}-{next_meta.end_line}) available"
            )

        return "\n".join(hints)


# Global preprocessor instance
_preprocessor: ContextPreprocessor | None = None


def get_preprocessor(
    file_size_limit: int = DEFAULT_FILE_SIZE_LIMIT,
    line_limit: int = DEFAULT_LINE_LIMIT,
) -> ContextPreprocessor:
    """Get or create global preprocessor."""
    global _preprocessor
    if _preprocessor is None:
        _preprocessor = ContextPreprocessor(
            file_size_limit=file_size_limit,
            line_limit=line_limit,
        )
    return _preprocessor


def reset_preprocessor() -> None:
    """Reset global preprocessor."""
    global _preprocessor
    _preprocessor = None


if __name__ == "__main__":
    import json
    import sys

    logging.basicConfig(level=logging.INFO)

    if len(sys.argv) < 2:
        logger.info("Usage: python context_preprocessor.py <file_path> [--analyze]")
        sys.exit(1)

    file_path = sys.argv[1]
    analyze_only = "--analyze" in sys.argv

    preprocessor = get_preprocessor()

    if analyze_only:
        plan = preprocessor.analyze_file(file_path)
        logger.info(
            json.dumps(
                {
                    "file_path": plan.file_path,
                    "original_size": plan.original_size,
                    "original_lines": plan.original_lines,
                    "strategy": plan.strategy,
                    "estimated_tokens": plan.estimated_tokens,
                    "chunks": [
                        {
                            "index": c.chunk_index,
                            "lines": f"{c.start_line}-{c.end_line}",
                            "significant": c.is_significant,
                        }
                        for c in plan.chunks
                    ],
                },
                indent=2,
            )
        )
    else:
        processed = preprocessor.preprocess_file(file_path)
        logger.info(
            json.dumps(
                {
                    "file_path": processed.original_path,
                    "needs_chunking": processed.needs_chunking,
                    "total_chunks": len(processed.chunks),
                    "total_tokens": processed.total_tokens,
                    "chunks": [
                        {
                            "index": m.chunk_index,
                            "lines": f"{m.start_line}-{m.end_line}",
                            "size": m.chunk_size,
                        }
                        for m in processed.metadata
                    ],
                },
                indent=2,
            )
        )
