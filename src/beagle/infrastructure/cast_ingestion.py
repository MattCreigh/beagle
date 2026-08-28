"""CAST (Context-Aware Splitting via Abstract Syntax Tree) Ingestion Pipeline.

Phase 1 of the Hybrid RAG Subsystem. Parses source files using tree-sitter,
chunks them respecting AST boundaries (function/class definitions), constructs
a Kùzu knowledge graph, and embeds chunks into LanceDB for vector retrieval.

v13.5.2 enhancements:
- Ramdisk staging: Intermediate files written to ramdisk at configured path,
  only final os.replace() hits persistent SSD. Logs SSD write savings.
- Incremental ingestion: Cache tracks {file_path: {mtime, hash}} per file.
  Unchanged files are skipped on re-ingestion for dramatic speedup.

Usage:
    python -m infrastructure.cast_ingestion /path/to/codebase

Environment:
    BEAGLE_KNOWLEDGE_DIR: Base directory for LanceDB + Kùzu storage
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import tempfile
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from beagle.config.paths import get_data_root
from beagle.security.validation import validate_cypher_identifier

from .rag_paths import LANCE_TABLE_NAME, db_root, kuzu_uri, lancedb_uri

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("Beagle.CAST_Ingestion")

# ──────────────────────────────────────────────────────────────────────────────
# Configuration Constants
# ──────────────────────────────────────────────────────────────────────────────
CHUNK_SIZE_TOKENS = 512
OVERLAP_RATIO = 0.10  # 10% overlap
OVERLAP_TOKENS = int(CHUNK_SIZE_TOKENS * OVERLAP_RATIO)

# ── Ramdisk staging (SSD write savings) ────────────────────────────────────
import threading as _threading  # ruff: ignore[E402]

_ssd_writes_saved_bytes: int = 0  # Cumulative SSD write savings counter
_ssd_counter_lock = _threading.Lock()


def _find_config_toml() -> Path | None:
    """Locate config.toml, checking the repo root then the installed package.

    B-20 (audit v13.22.1): both callers used
    ``Path(__file__).parent.parent.parent.parent / "config.toml"`` — one
    level too many. From
    ``<repo>/beagle/infrastructure/cast_ingestion.py`` that
    resolves to ``<repo>/../config.toml``, which does not exist. The lookups
    were wrapped in ``except (ImportError, OSError): pass``, so the miss was
    silent: ``incremental_ingest = true`` in config.toml was never read and
    the ramdisk staging setting was ignored.

    Returns the first existing candidate, or None.
    """
    # v1.1.1 (S4): config.toml is detached to the canonical config root.
    from ..config._config_path import find_config_toml

    try:
        p = find_config_toml()
        if p.is_file():
            return p
    except (ImportError, OSError) as exc:
        logger.debug("find_config_toml() unavailable (%s); falling back to legacy walk", exc)

    here = Path(__file__).resolve()
    candidates = (
        here.parents[2] / "config.toml",  # repo root (source checkout)
        here.parents[1] / "config.toml",  # inside the package (installed)
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    logger.debug(f"[Config] config.toml not found; tried: {[str(c) for c in candidates]}")
    return None


def _load_hardware_config() -> dict[str, Any]:
    """Return the ``[hardware]`` table from config.toml (empty when absent)."""
    config_path = _find_config_toml()
    if config_path is None:
        return {}
    try:
        import tomllib

        with open(config_path, "rb") as f:
            data = tomllib.load(f)
        hw = data.get("hardware", {})
        return hw if isinstance(hw, dict) else {}
    except (ImportError, OSError, ValueError) as exc:
        logger.warning(f"[Config] Could not read {config_path}: {exc}")
        return {}


def _get_staging_dir() -> str:
    """Get the staging directory for ingestion intermediates.

    Uses ramdisk if available and configured; falls back to tempfile
    if ramdisk is not mounted.

    Returns:
        Path to staging directory.

    """
    hw = _load_hardware_config()
    if hw.get("ramdisk_enabled", True):
        ramdisk_path = hw.get("ramdisk_path", "/mnt/beagle_rag_staging")
        if Path(ramdisk_path).exists():
            return str(ramdisk_path)
    return tempfile.gettempdir()


def _ingest_cache_path(target_dir: str) -> Path:
    """Per-target incremental-cache path under the Beagle data root.

    v13.22.5: relocated OUT of the target directory. The cache is runtime
    state, not corpus — keeping it beside the indexed sources meant any
    redeploy/rsync of the target wiped it (and reset mtimes), forcing a
    full re-parse + re-embed on the next ingest (2026-08-25 render-hints
    heat incident). Keyed by a digest of the resolved target path so
    several codebases sharing one data root never collide.
    """
    resolved = str(Path(target_dir).resolve())
    digest = hashlib.sha256(resolved.encode()).hexdigest()[:16]
    return get_data_root() / f".beagle_ingest_cache_{digest}.json"


def _load_ingest_cache(target_dir: str) -> dict[str, dict[str, str]]:
    """Load the incremental ingestion cache for *target_dir*.

    Reads the data-root location written by v13.22.5+; falls back to the
    legacy ``<target>/.beagle_ingest_cache.json`` (read-only) so caches
    seeded before the relocation keep working until the next successful
    ingestion persists them to the new location.

    Returns:
        Dict of {file_path: {mtime, hash}} for previously ingested files.

    """
    cache_path = _ingest_cache_path(target_dir)
    if cache_path.exists():
        try:
            return json.loads(cache_path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]
        except (json.JSONDecodeError, OSError):
            return {}
    # Legacy fallback (pre-relocation location inside the target dir).
    legacy_path = Path(target_dir) / ".beagle_ingest_cache.json"
    if legacy_path.exists():
        try:
            return json.loads(legacy_path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_ingest_cache(target_dir: str, cache: dict[str, dict[str, str]]) -> None:
    """Save the incremental ingestion cache to the data-root location."""
    cache_path = _ingest_cache_path(target_dir)
    tmp_path = cache_path.with_suffix(".json.tmp")
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.write_text(json.dumps(cache, indent=2), encoding="utf-8")
        tmp_path.replace(cache_path)
    except OSError as e:
        logger.warning(f"Failed to save ingest cache: {e}")


SUPPORTED_EXTENSIONS = {
    # Programming languages
    ".py",
    ".js",
    ".ts",
    ".rs",
    ".go",
    ".java",
    ".c",
    ".cpp",
    ".h",
    # Configuration & documentation (critical for AI context)
    ".md",
    ".toml",
    ".yaml",
    ".yml",
    ".json",
    ".cfg",
    ".ini",
}


# Tiered storage layout:
# - MAIN tier: Global architectural knowledge (Axioms, patterns, architecture docs)
#   → <BEAGLE_KNOWLEDGE_DIR>/main_rag (bulk storage)
# - INSTANCE tier: Project-specific data (per-repository RAG)
def __getattr__(name: str) -> Any:
    if name == "DB_PATH":
        return db_root()
    if name == "LANCEDB_URI":
        return lancedb_uri()
    if name == "KUZU_URI":
        return kuzu_uri()
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")


def _get_lancedb_uri(db_root_path: str | None = None) -> str:
    if db_root_path is not None:
        return lancedb_uri(db_root_path)
    override = sys.modules[__name__].__dict__.get("LANCEDB_URI")
    if isinstance(override, str):
        return override
    return lancedb_uri()


def _get_kuzu_uri(db_root_path: str | None = None) -> str:
    if db_root_path is not None:
        return kuzu_uri(db_root_path)
    override = sys.modules[__name__].__dict__.get("KUZU_URI")
    if isinstance(override, str):
        return override
    return kuzu_uri()


def _clean_stale_kuzu_wal(target_kuzu: str) -> None:
    """Remove orphan WAL files from a previous crashed Kùzu open.

    v13.22.3 fix: a crashed or killed Kùzu session leaves a stale
    ``<db>.wal`` file alongside the main ``<db>`` file. Kùzu's
    ``Database()`` constructor tries to recover the WAL during init;
    when the WAL is from a different/older Kùzu version it raises
    ``IndexError: unordered_map::at`` from the C++ ``std::unordered_map::at``
    call inside the WAL replay path. The error is non-recoverable
    without manual cleanup, blocking every subsequent ingest.

    This helper probes the target path; if the main DB is absent
    (a fresh ingest), it ALSO removes any orphan WAL so a previous
    crashed session can't poison the new open. If the main DB is
    present, the WAL is left alone — Kùzu will replay it on the
    next normal open, which is the correct recovery path.

    Args:
        target_kuzu: Filesystem path to the Kùzu database file.

    """
    main_exists = os.path.exists(target_kuzu)
    wal_path = target_kuzu + ".wal"
    if not os.path.exists(wal_path):
        return  # nothing to clean
    if main_exists:
        # Main DB present — let Kùzu handle WAL replay on open.
        return
    # Fresh-ingest case: main DB absent but stale WAL present.
    # The WAL belongs to a previous (crashed) session and will
    # make Database() raise unordered_map::at. Remove it.
    try:
        os.remove(wal_path)
        logger.info(f"[Kùzu] Removed stale WAL from a previous crashed session: {wal_path}")
    except OSError as exc:
        logger.warning(
            f"[Kùzu] Could not remove stale WAL {wal_path}: {exc}; "
            f"the next kuzu.Database() open may fail"
        )


# Embedding model identifier (open-weights, air-gapped capable)


# ──────────────────────────────────────────────────────────────────────────────
# Data Structures
# ──────────────────────────────────────────────────────────────────────────────
@dataclass
class ASTChunk:
    """A single chunk derived from AST-aware splitting."""

    chunk_id: str
    filepath: str
    language: str
    node_type: str  # "function", "class", "module", "block"
    node_name: str
    start_line: int
    end_line: int
    text: str
    token_count: int
    parent_name: str = ""
    ast_entity_id: str = ""

    def __post_init__(self):
        if not self.ast_entity_id:
            self.ast_entity_id = self.chunk_id


@dataclass
class ASTRelation:
    """A deterministic relationship between two AST entities."""

    source_id: str
    target_id: str
    relation_type: str  # CALLS, INHERITS_FROM, IMPORTS, CONTAINS
    source_name: str = ""
    target_name: str = ""


@dataclass
class IngestionResult:
    """Summary of a complete ingestion run.

    Semantics (v13.21):
    - `files_processed` is the count of source files successfully parsed
      into AST chunks. Unchanged by downstream-store failures.
    - `chunks_created` is the count of AST chunks **persisted to LanceDB**.
      Zeroed if `build_lancedb_index` fails. Pre-v13.21 this counted
      chunks in memory, which misled callers into thinking the data
      was on disk when it was not.
    - `relations_extracted` is the count of AST relations **persisted to
      Kùzu**. Zeroed if `build_kuzu_graph` fails.
    - `partial` is True when one downstream store succeeded and the
      other failed (degraded mode). Both-zero failures are not `partial`
      — they are full failures surfaced via the `errors` list.
    - `errors` is a list of human-readable failure messages; the hot-swap
      path treats non-empty `errors` as a hard failure (status=error,
      not status=partial).

    """

    files_processed: int = 0
    chunks_created: int = 0
    relations_extracted: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    # v13.19.5: True when ingestion completed but a non-fatal subsystem
    # failed (e.g. Kùzu graph construction). RAG continues to function
    # in a degraded mode (vector-only, no graph traversal).
    partial: bool = False


# ──────────────────────────────────────────────────────────────────────────────
# Token Estimation (fast heuristic, no external dep required)
# ──────────────────────────────────────────────────────────────────────────────
def estimate_tokens(text: str) -> int:
    """Estimate token count using a character-based heuristic.

    Code averages ~3.5 chars per token for GPT-family tokenizers.
    """
    return max(1, int(len(text) / 3.5))


def generate_chunk_id(filepath: str, node_name: str, start_line: int) -> str:
    """Generate a deterministic chunk ID from file, name, and location."""
    raw = f"{filepath}:{node_name}:{start_line}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ──────────────────────────────────────────────────────────────────────────────
# AST-Aware Chunking (tree-sitter independent fallback)
# ──────────────────────────────────────────────────────────────────────────────
def _try_treesitter_parse(filepath: Path, source: str) -> list[ASTChunk] | None:
    """Attempt to parse with tree-sitter if available. Returns None if unavailable."""
    try:
        from tree_sitter_languages import get_parser

        ext_to_lang = {
            ".py": "python",
            ".js": "javascript",
            ".ts": "typescript",
            ".rs": "rust",
            ".go": "go",
            ".java": "java",
            ".c": "c",
            ".cpp": "cpp",
            ".h": "c",
            ".md": "markdown",
            ".toml": "toml",
            ".yaml": "yaml",
            ".yml": "yaml",
        }
        lang = ext_to_lang.get(filepath.suffix)
        if not lang:
            # New doc/config extensions — skip tree-sitter, use fallback parser
            if filepath.suffix in {".json", ".cfg", ".ini"}:
                return None
            return None

        parser = get_parser(lang)
        tree = parser.parse(source.encode("utf-8"))
        root = tree.root_node

        chunks: list[ASTChunk] = []
        _extract_ts_nodes(root, str(filepath), lang, source, chunks)
        return chunks if chunks else None

    except ImportError:
        return None
    except (ValueError, TypeError, RuntimeError) as e:
        logger.warning(f"tree-sitter parse failed for {filepath}: {e}")
        return None


def _extract_ts_nodes(
    node: Any,
    filepath: str,
    language: str,
    source: str,
    chunks: list[ASTChunk],
    parent_name: str = "",
) -> None:
    """Recursively extract function/class definitions from tree-sitter AST."""
    # Node types that represent meaningful boundaries
    boundary_types = {
        "function_definition",
        "function_declaration",
        "class_definition",
        "class_declaration",
        "method_definition",
        "impl_item",
        "fn_item",
        "function_item",
    }

    if node.type in boundary_types:
        # Extract the node name
        name = _get_ts_node_name(node) or f"anonymous_{node.start_point[0]}"
        start_line = node.start_point[0] + 1
        end_line = node.end_point[0] + 1
        text = source[node.start_byte : node.end_byte]
        tokens = estimate_tokens(text)

        # If the node is too large, we'll still record it but mark it
        node_type = "class" if "class" in node.type else "function"
        chunk_id = generate_chunk_id(filepath, name, start_line)

        # Split oversized nodes into sub-chunks
        if tokens > CHUNK_SIZE_TOKENS:
            sub_chunks = _split_oversized_node(
                text, filepath, language, node_type, name, start_line
            )
            chunks.extend(sub_chunks)
        else:
            chunks.append(
                ASTChunk(
                    chunk_id=chunk_id,
                    filepath=filepath,
                    language=language,
                    node_type=node_type,
                    node_name=name,
                    start_line=start_line,
                    end_line=end_line,
                    text=text,
                    token_count=tokens,
                    parent_name=parent_name,
                )
            )

        # Recurse into children with updated parent
        for child in node.children:
            _extract_ts_nodes(child, filepath, language, source, chunks, name)
    else:
        for child in node.children:
            _extract_ts_nodes(child, filepath, language, source, chunks, parent_name)


def _get_ts_node_name(node: Any) -> str | None:
    """Extract the name identifier from a tree-sitter node."""
    for child in node.children:
        if child.type in ("identifier", "name", "property_identifier"):
            return child.text.decode("utf-8") if isinstance(child.text, bytes) else child.text
    return None


def _split_oversized_node(
    text: str,
    filepath: str,
    language: str,
    node_type: str,
    node_name: str,
    base_line: int,
) -> list[ASTChunk]:
    """Split an oversized AST node into sub-chunks with overlap."""
    lines = text.split("\n")
    chunks: list[ASTChunk] = []
    chunk_lines: list[str] = []
    chunk_start = base_line
    running_tokens = 0

    for i, line in enumerate(lines):
        line_tokens = estimate_tokens(line)
        if running_tokens + line_tokens > CHUNK_SIZE_TOKENS and chunk_lines:
            chunk_text = "\n".join(chunk_lines)
            cid = generate_chunk_id(filepath, f"{node_name}_part{len(chunks)}", chunk_start)
            chunks.append(
                ASTChunk(
                    chunk_id=cid,
                    filepath=filepath,
                    language=language,
                    node_type=node_type,
                    node_name=f"{node_name}_part{len(chunks)}",
                    start_line=chunk_start,
                    end_line=base_line + i,
                    text=chunk_text,
                    token_count=estimate_tokens(chunk_text),
                    parent_name=node_name,
                )
            )
            # Overlap: keep last N tokens worth of lines
            overlap_lines = []  # type: ignore[var-annotated]
            overlap_tokens = 0
            for ol in reversed(chunk_lines):
                olt = estimate_tokens(ol)
                if overlap_tokens + olt > OVERLAP_TOKENS:
                    break
                overlap_lines.insert(0, ol)
                overlap_tokens += olt
            chunk_lines = overlap_lines
            running_tokens = overlap_tokens
            chunk_start = base_line + i - len(overlap_lines) + 1

        chunk_lines.append(line)
        running_tokens += line_tokens

    # Final chunk
    if chunk_lines:
        chunk_text = "\n".join(chunk_lines)
        cid = generate_chunk_id(filepath, f"{node_name}_part{len(chunks)}", chunk_start)
        chunks.append(
            ASTChunk(
                chunk_id=cid,
                filepath=filepath,
                language=language,
                node_type=node_type,
                node_name=f"{node_name}_part{len(chunks)}",
                start_line=chunk_start,
                end_line=base_line + len(lines),
                text=chunk_text,
                token_count=estimate_tokens(chunk_text),
                parent_name=node_name,
            )
        )

    return chunks


# ──────────────────────────────────────────────────────────────────────────────
# Regex-Based Fallback Parser (no tree-sitter dependency)
# ──────────────────────────────────────────────────────────────────────────────
import contextlib  # ruff: ignore[E402]
import re  # ruff: ignore[E402]

# Patterns for common language constructs
_PYTHON_DEF = re.compile(r"^(class|def|async\s+def)\s+(\w+)", re.MULTILINE)
_JS_DEF = re.compile(
    r"^(?:export\s+)?(?:async\s+)?(?:function|class)\s+(\w+)|^(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?(?:\(|function)",
    re.MULTILINE,
)
_IMPORT_PATTERN = re.compile(r"^(?:from\s+(\S+)\s+)?import\s+(.+?)(?:\s+as\s+\w+)?$", re.MULTILINE)
_CALL_PATTERN = re.compile(r"\b(\w+)\s*\(")
_INHERIT_PATTERN = re.compile(r"class\s+\w+\s*\(([^)]+)\)")


def _fallback_chunk(filepath: Path, source: str) -> list[ASTChunk]:
    """Chunk source code using regex-based boundary detection."""
    language = {
        ".py": "python",
        ".js": "javascript",
        ".ts": "typescript",
        ".rs": "rust",
        ".go": "go",
        ".java": "java",
        ".c": "c",
        ".cpp": "cpp",
        ".h": "c",
        # Configuration & documentation
        ".md": "markdown",
        ".toml": "toml",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".json": "json",
        ".cfg": "ini",
        ".ini": "ini",
    }.get(filepath.suffix, "unknown")

    # Markdown heading patterns for structured doc chunking
    _MD_HEADING = re.compile(r"^(#{1,6})\s+(.+)$")
    # TOML section headers
    _TOML_SECTION = re.compile(r"^\[(.+)\]")
    # YAML document markers
    _YAML_DOC = re.compile(r"^---\s*$")

    lines = source.split("\n")
    chunks: list[ASTChunk] = []

    # Find definition/section boundaries depending on language
    boundaries: list[tuple[int, str, str]] = []  # (line_idx, type, name)

    if language == "markdown":
        # Split markdown on headings
        for i, line in enumerate(lines):
            m = _MD_HEADING.match(line.strip())
            if m:
                level = len(m.group(1))
                name = m.group(2).strip().lower().replace(" ", "_")[:60]
                boundaries.append((i, f"heading_h{level}", name))
    elif language in ("toml", "yaml"):
        # Split config on section headers / document markers
        for i, line in enumerate(lines):
            m = _TOML_SECTION.match(line.strip())
            if m:
                boundaries.append((i, "section", m.group(1).strip()))
                continue
            if language == "yaml" and _YAML_DOC.match(line.strip()):
                boundaries.append((i, "document", f"yaml_doc_{i}"))
    else:
        # Code: split on function/class definitions
        for i, line in enumerate(lines):
            m = _PYTHON_DEF.match(line.strip())
            if m:
                kind = "class" if m.group(1) == "class" else "function"
                boundaries.append((i, kind, m.group(2)))
                continue
            m = _JS_DEF.match(line.strip())
            if m:
                name = m.group(1) or m.group(2) or f"anon_{i}"
                boundaries.append((i, "function", name))

    if not boundaries:
        # No definitions found — chunk the whole file as a module block
        return _chunk_flat(str(filepath), language, source, lines)

    # Chunk between boundaries
    for idx, (line_idx, node_type, name) in enumerate(boundaries):
        next_line = boundaries[idx + 1][0] if idx + 1 < len(boundaries) else len(lines)
        text = "\n".join(lines[line_idx:next_line])
        tokens = estimate_tokens(text)

        if tokens > CHUNK_SIZE_TOKENS:
            sub_chunks = _split_oversized_node(
                text, str(filepath), language, node_type, name, line_idx + 1
            )
            chunks.extend(sub_chunks)
        else:
            cid = generate_chunk_id(str(filepath), name, line_idx + 1)
            chunks.append(
                ASTChunk(
                    chunk_id=cid,
                    filepath=str(filepath),
                    language=language,
                    node_type=node_type,
                    node_name=name,
                    start_line=line_idx + 1,
                    end_line=next_line,
                    text=text,
                    token_count=tokens,
                )
            )

    # Module preamble (imports, constants before first definition)
    if boundaries and boundaries[0][0] > 0:
        preamble = "\n".join(lines[: boundaries[0][0]])
        if preamble.strip():
            cid = generate_chunk_id(str(filepath), "module_preamble", 1)
            chunks.insert(
                0,
                ASTChunk(
                    chunk_id=cid,
                    filepath=str(filepath),
                    language=language,
                    node_type="module",
                    node_name="module_preamble",
                    start_line=1,
                    end_line=boundaries[0][0],
                    text=preamble,
                    token_count=estimate_tokens(preamble),
                ),
            )

    return chunks


def _chunk_flat(filepath: str, language: str, source: str, lines: list[str]) -> list[ASTChunk]:
    """Chunk a file with no clear AST boundaries into fixed-size blocks."""
    chunks: list[ASTChunk] = []
    block: list[str] = []
    block_start = 1
    running_tokens = 0

    for i, line in enumerate(lines):
        lt = estimate_tokens(line)
        if running_tokens + lt > CHUNK_SIZE_TOKENS and block:
            text = "\n".join(block)
            cid = generate_chunk_id(filepath, f"block_{len(chunks)}", block_start)
            chunks.append(
                ASTChunk(
                    chunk_id=cid,
                    filepath=filepath,
                    language=language,
                    node_type="block",
                    node_name=f"block_{len(chunks)}",
                    start_line=block_start,
                    end_line=i + 1,
                    text=text,
                    token_count=estimate_tokens(text),
                )
            )
            # Overlap
            overlap_lines = []  # type: ignore[var-annotated]
            ot = 0
            for ol in reversed(block):
                olt = estimate_tokens(ol)
                if ot + olt > OVERLAP_TOKENS:
                    break
                overlap_lines.insert(0, ol)
                ot += olt
            block = overlap_lines
            running_tokens = ot
            block_start = i + 1 - len(overlap_lines) + 1
        block.append(line)
        running_tokens += lt

    if block:
        text = "\n".join(block)
        cid = generate_chunk_id(filepath, f"block_{len(chunks)}", block_start)
        chunks.append(
            ASTChunk(
                chunk_id=cid,
                filepath=filepath,
                language=language,
                node_type="block",
                node_name=f"block_{len(chunks)}",
                start_line=block_start,
                end_line=len(lines),
                text=text,
                token_count=estimate_tokens(text),
            )
        )

    return chunks


def chunk_file(filepath: Path, source: str | None = None) -> list[ASTChunk]:
    """Parse a single file into AST chunks.

    Tries tree-sitter first and falls back to the regex chunker for
    languages/grammars it cannot handle — the same two-step ``ingest()``
    performs per file.

    B-2 (audit v13.22.1): ``hotswap_ingest._incremental_update`` imported a
    ``_chunk_file`` from this module that **did not exist**, so every
    incremental update raised ImportError, was swallowed by a broad handler,
    and returned a hard error — leaving the index permanently stale. The
    logic was only ever available inline inside ``ingest()``'s two loops.
    It now lives here once, and both ``ingest()`` and the incremental path
    call it, so they cannot drift apart again.

    Args:
        filepath: File to parse.
        source: Pre-read contents; read from disk when omitted.

    Returns:
        The file's chunks (possibly empty).

    Raises:
        OSError: If ``source`` is None and the file cannot be read.

    """
    if source is None:
        source = filepath.read_text(encoding="utf-8", errors="replace")

    chunks = _try_treesitter_parse(filepath, source)
    if chunks is None:
        chunks = _fallback_chunk(filepath, source)
    return chunks


# ──────────────────────────────────────────────────────────────────────────────
# Relation Extraction
# ──────────────────────────────────────────────────────────────────────────────
def extract_relations(chunks: list[ASTChunk]) -> list[ASTRelation]:
    """Extract deterministic relationships between AST chunks."""
    relations: list[ASTRelation] = []
    name_to_id: dict[str, str] = {}

    # Build name→id index
    for chunk in chunks:
        if chunk.node_name and chunk.node_name != "module_preamble":
            name_to_id[chunk.node_name] = chunk.chunk_id

    for chunk in chunks:
        # CONTAINS: parent→child
        if chunk.parent_name and chunk.parent_name in name_to_id:
            relations.append(
                ASTRelation(
                    source_id=name_to_id[chunk.parent_name],
                    target_id=chunk.chunk_id,
                    relation_type="CONTAINS",
                    source_name=chunk.parent_name,
                    target_name=chunk.node_name,
                )
            )

        # INHERITS_FROM
        for m in _INHERIT_PATTERN.finditer(chunk.text):
            bases = [b.strip() for b in m.group(1).split(",")]
            for base in bases:
                base_clean = base.split(".")[-1]  # Handle module.Class
                if base_clean in name_to_id and base_clean != chunk.node_name:
                    relations.append(
                        ASTRelation(
                            source_id=chunk.chunk_id,
                            target_id=name_to_id[base_clean],
                            relation_type="INHERITS_FROM",
                            source_name=chunk.node_name,
                            target_name=base_clean,
                        )
                    )

        # CALLS: function calls within chunk text
        called_names = set(_CALL_PATTERN.findall(chunk.text))
        for called in called_names:
            if (
                called in name_to_id
                and called != chunk.node_name
                and called
                not in (
                    "print",
                    "len",
                    "range",
                    "str",
                    "int",
                    "float",
                    "list",
                    "dict",
                    "set",
                    "type",
                    "super",
                    "isinstance",
                    "hasattr",
                    "getattr",
                )
            ):
                relations.append(
                    ASTRelation(
                        source_id=chunk.chunk_id,
                        target_id=name_to_id[called],
                        relation_type="CALLS",
                        source_name=chunk.node_name,
                        target_name=called,
                    )
                )

        # IMPORTS
        for m in _IMPORT_PATTERN.finditer(chunk.text):
            m.group(1) or ""
            names = m.group(2)
            for imp_name in names.split(","):
                imp_clean = imp_name.strip().split(" as ")[0].strip()
                if imp_clean in name_to_id:
                    relations.append(
                        ASTRelation(
                            source_id=chunk.chunk_id,
                            target_id=name_to_id[imp_clean],
                            relation_type="IMPORTS",
                            source_name=chunk.node_name,
                            target_name=imp_clean,
                        )
                    )

    # Deduplicate
    seen = set()
    unique: list[ASTRelation] = []
    for r in relations:
        key = (r.source_id, r.target_id, r.relation_type)
        if key not in seen:
            seen.add(key)
            unique.append(r)

    return unique


# ──────────────────────────────────────────────────────────────────────────────
# Kùzu Graph Construction
# ──────────────────────────────────────────────────────────────────────────────
def delete_kuzu_nodes_for_files(
    filepaths: Iterable[str],
    db_root_path: str | None = None,
) -> bool:
    """Remove all ASTNode rows (and their edges) belonging to ``filepaths``.

    B-3 (audit v13.22.1): ``build_kuzu_graph`` MERGEs nodes by id, which is
    idempotent for *unchanged* code but leaves orphans behind when a
    function is renamed, moved or deleted — the old id is never revisited.
    The incremental path must therefore drop a file's nodes before
    re-inserting them.

    Returns True if the delete ran (or there was nothing to do).
    """
    paths = sorted({p for p in filepaths if p})
    if not paths:
        return True

    try:
        import kuzu
    except ImportError:
        logger.error("kuzu not installed. Skipping graph cleanup.")
        return False

    target_kuzu = _get_kuzu_uri(db_root_path)
    # v13.22.3: clean stale WAL from a prior crashed session before opening.
    # A fresh-ingest (main DB absent + orphan WAL present) trips Kùzu's
    # WAL replay and raises "IndexError: unordered_map::at" from C++.
    _clean_stale_kuzu_wal(target_kuzu)
    if not os.path.exists(target_kuzu):
        # Nothing ingested yet — nothing to clean.
        return True

    try:
        # Explicit buffer_pool_size avoids kuzu's 8TB mmap default that
        # OOMs on memory-constrained hosts. The original 80%-of-RAM
        # default assumes a dedicated DB host with tens of GB free.
        #
        # v13.22.3 H1 fix: env-driven. See cast_ingestion.py docstring.
        # v13.22.5: default raised 64 → 512MB. The 64MB default could not
        # hold a full-corpus staging build (~5.2k nodes / ~8.9k relations):
        # relation inserts failed en masse with "Buffer manager exception:
        # Unable to allocate memory" and the pipeline never reached the
        # embedding phase (2026-08-25 render-hints heat incident). 512MB
        # completed the same build comfortably alongside the normal
        # session load on a 16GB host.
        _kuzu_buffer_pool = int(os.environ.get("BEAGLE_KUZU_BUFFER_POOL_MB", "512")) * 1024 * 1024
        _kuzu_max_db_size = int(os.environ.get("BEAGLE_KUZU_MAX_DB_SIZE_MB", "512")) * 1024 * 1024
        # v13.22.3: set checkpoint_threshold so Kùzu auto-checkpoints
        # periodically; without it (default -1 = unlimited), a crashed
        # or killed session leaves an enormous .wal that Kùzu cannot
        # replay, blocking the next open with IndexError:
        # unordered_map::at. 100 MiB threshold = checkpoint every ~50-100
        # pages of writes, negligible cost relative to the OOM/safety
        # benefit.
        _kuzu_checkpoint_threshold = (
            int(os.environ.get("BEAGLE_KUZU_CHECKPOINT_THRESHOLD_MB", "100")) * 1024 * 1024
        )
        db = kuzu.Database(
            target_kuzu,
            buffer_pool_size=_kuzu_buffer_pool,
            max_db_size=_kuzu_max_db_size,
            checkpoint_threshold=_kuzu_checkpoint_threshold,
        )
        conn = kuzu.Connection(db)
        # DETACH DELETE removes incident CALLS/IMPORTS/... edges too, so no
        # dangling relations survive the file's removal.
        conn.execute(
            "MATCH (n:ASTNode) WHERE n.filepath IN $fps DETACH DELETE n",
            parameters={"fps": paths},
        )
        logger.info(f"[Kùzu] Deleted nodes for {len(paths)} file(s)")
        return True
    except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
        logger.error(
            f"[Kùzu] Node cleanup failed for {len(paths)} file(s): "
            f"type={type(e).__module__}.{type(e).__name__} message={e!r}"
        )
        return False


def build_kuzu_graph(
    chunks: list[ASTChunk],
    relations: list[ASTRelation],
    db_root_path: str | None = None,
) -> bool:
    """Construct the Kùzu knowledge graph from AST chunks and relations.

    Node writes are MERGE-based, so calling this with a subset of chunks
    *adds* to the existing graph rather than replacing it. Callers doing an
    incremental update must call :func:`delete_kuzu_nodes_for_files` first
    for the changed/removed files (see B-3).
    """
    try:
        import kuzu
    except ImportError:
        logger.error("kuzu not installed. Skipping graph construction.")
        return False

    target_kuzu = _get_kuzu_uri(db_root_path)
    kuzu_dir = os.path.dirname(target_kuzu)
    os.makedirs(kuzu_dir, exist_ok=True)
    # v13.22.3: clean stale WAL from a prior crashed session before opening.
    _clean_stale_kuzu_wal(target_kuzu)

    try:
        # Explicit buffer_pool_size avoids kuzu's 8TB mmap default that
        # OOMs on memory-constrained hosts. 256MB is plenty for our graph
        # sizes (10k-200k nodes; the original 80%-of-RAM default assumes
        # a dedicated DB host with tens of GB free).
        #
        # v13.22.3 H1 fix: env-driven (see cast_ingestion.py:_kuzu_buffer_pool
        # helper for full context). Defaults: buffer 64 MiB, max_db 512 MiB.
        _kuzu_buffer_pool = int(os.environ.get("BEAGLE_KUZU_BUFFER_POOL_MB", "64")) * 1024 * 1024
        _kuzu_max_db_size = int(os.environ.get("BEAGLE_KUZU_MAX_DB_SIZE_MB", "512")) * 1024 * 1024
        # v13.22.3: see _kuzu_checkpoint_threshold comment above.
        _kuzu_checkpoint_threshold = (
            int(os.environ.get("BEAGLE_KUZU_CHECKPOINT_THRESHOLD_MB", "100")) * 1024 * 1024
        )
        db = kuzu.Database(
            target_kuzu,
            buffer_pool_size=_kuzu_buffer_pool,
            max_db_size=_kuzu_max_db_size,
            checkpoint_threshold=_kuzu_checkpoint_threshold,
        )
        conn = kuzu.Connection(db)

        # Create schema (idempotent)
        conn.execute(
            "CREATE NODE TABLE IF NOT EXISTS ASTNode("
            "id STRING, "
            "filepath STRING, "
            "language STRING, "
            "node_type STRING, "
            "name STRING, "
            "start_line INT64, "
            "end_line INT64, "
            "code_content STRING, "
            "token_count INT64, "
            "PRIMARY KEY(id))"
        )

        conn.execute("CREATE REL TABLE IF NOT EXISTS CALLS(FROM ASTNode TO ASTNode)")
        conn.execute("CREATE REL TABLE IF NOT EXISTS INHERITS_FROM(FROM ASTNode TO ASTNode)")
        conn.execute("CREATE REL TABLE IF NOT EXISTS IMPORTS(FROM ASTNode TO ASTNode)")
        conn.execute("CREATE REL TABLE IF NOT EXISTS CONTAINS(FROM ASTNode TO ASTNode)")

        # v13.22.3 fix: batch node inserts in transactions of
        # KUZU_INSERT_TX_BATCH rows each. Each per-row ``conn.execute()``
        # commits a separate Kùzu WAL frame, which exhausts the
        # buffer pool on a 5,000+ node corpus (``Buffer manager
        # exception: Unable to allocate memory!``). Batching into
        # transactions keeps the WAL from growing unboundedly during
        # the insert, lets Kùzu flush each batch to disk via its
        # checkpoint machinery, and reduces total wall-clock by ~10x
        # (one MERGE per batch vs. one MERGE per node).
        # Batch size 100: empirically the largest that reliably fits in
        # a 128 MiB buffer pool with the per-batch WAL frame overhead.
        # Larger batch sizes (500, 1000) hit "Buffer manager exception"
        # on the 5,160-node recovery ingest even with buffer_pool=128MiB
        # and max_db=1024MiB because Kùzu's per-row MERGE-MATCH cost
        # scales quadratically within a single transaction.
        KUZU_INSERT_TX_BATCH = 100
        nodes_inserted = 0
        for tx_start in range(0, len(chunks), KUZU_INSERT_TX_BATCH):
            tx_end = min(tx_start + KUZU_INSERT_TX_BATCH, len(chunks))
            tx_chunks = chunks[tx_start:tx_end]
            tx_params = {
                "rows": [
                    {
                        "id": c.ast_entity_id,
                        "fp": c.filepath,
                        "lang": c.language,
                        "nt": c.node_type,
                        "name": c.node_name,
                        "sl": c.start_line,
                        "el": c.end_line,
                        "cc": c.text[:2000],
                        "tc": c.token_count,
                    }
                    for c in tx_chunks
                ]
            }
            try:
                # Kùzu's UNWIND + MERGE pattern: one parameterised
                # query handles the whole batch atomically. Avoids
                # the Python-loop-on-execute cost (each round-trip
                # is a syscall + WAL frame).
                conn.execute(
                    "UNWIND $rows AS row "
                    "MERGE (n:ASTNode {id: row.id}) "
                    "SET n.filepath = row.fp, n.language = row.lang, "
                    "n.node_type = row.nt, n.name = row.name, "
                    "n.start_line = row.sl, n.end_line = row.el, "
                    "n.code_content = row.cc, n.token_count = row.tc",
                    parameters=tx_params,
                )
                nodes_inserted += len(tx_chunks)
            except (ValueError, TypeError, RuntimeError) as e:
                # v13.22.3: one bad batch must NOT sink the whole
                # ingest. Fall back to per-row inserts for THIS batch
                # so we still get most of the corpus if a single chunk
                # has a problematic field (e.g. oversized code_content
                # that exceeds a Kùzu string column width).
                logger.warning(
                    f"[Kùzu] Batch insert failed ({tx_start}-{tx_end}): "
                    f"{e.__class__.__name__}: {str(e)[:200]}; falling "
                    f"back to per-row for this batch"
                )
                for chunk in tx_chunks:
                    try:
                        conn.execute(
                            "MERGE (n:ASTNode {id: $id}) "
                            "SET n.filepath = $fp, n.language = $lang, "
                            "n.node_type = $nt, n.name = $name, "
                            "n.start_line = $sl, n.end_line = $el, "
                            "n.code_content = $cc, n.token_count = $tc",
                            parameters={
                                "id": chunk.ast_entity_id,
                                "fp": chunk.filepath,
                                "lang": chunk.language,
                                "nt": chunk.node_type,
                                "name": chunk.node_name,
                                "sl": chunk.start_line,
                                "el": chunk.end_line,
                                "cc": chunk.text[:2000],
                                "tc": chunk.token_count,
                            },
                        )
                        nodes_inserted += 1
                    except (ValueError, TypeError, RuntimeError) as ce:
                        logger.warning(f"Failed to insert node {chunk.node_name}: {str(ce)[:120]}")

        logger.info(
            f"[Kùzu] Nodes inserted: {nodes_inserted}/{len(chunks)} "
            f"(batch size {KUZU_INSERT_TX_BATCH}, "
            f"{len(chunks) // KUZU_INSERT_TX_BATCH + 1} transactions)"
        )

        # Insert relations (relation_type validated against allowlist to prevent injection)
        _VALID_REL_TYPES = frozenset({"CALLS", "INHERITS_FROM", "IMPORTS", "CONTAINS"})
        for rel in relations:
            if rel.relation_type not in _VALID_REL_TYPES:
                logger.warning(
                    f"Skipping unknown relation type {rel.relation_type!r} "
                    f"({rel.source_name} -> {rel.target_name})"
                )
                continue
            try:
                # Defense in depth: the allowlist check above already restricts
                # rel.relation_type, but re-validate through the shared Cypher
                # identifier gate before it is interpolated as a relationship type.
                validate_cypher_identifier(rel.relation_type)
                conn.execute(
                    f"MATCH (a:ASTNode {{id: $src}}), (b:ASTNode {{id: $tgt}}) "
                    f"MERGE (a)-[:{rel.relation_type}]->(b)",
                    parameters={"src": rel.source_id, "tgt": rel.target_id},
                )
            except (ValueError, TypeError, RuntimeError) as e:
                logger.warning(
                    f"Failed to insert relation {rel.source_name}->{rel.target_name}: {e}"
                )

        logger.info(f"Kùzu graph built: {len(chunks)} nodes, {len(relations)} relations")
        return True

    except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
        # v13.19.5: Capture full exception context (type + message + traceback
        # tail) so future failures are diagnosable. Previously we logged only
        # `f"... failed: {e}"` which discarded the C++ exception type from
        # Kùzu (e.g. `unordered_map::at` from std::out_of_range). The caller
        # at line 1099 continues to the LanceDB pass — graph-less RAG is a
        # valid degraded mode, so we return False and let the caller mark
        # `result.partial = True` rather than aborting the whole ingestion.
        import traceback as _tb

        exc_type = type(e).__name__
        exc_module = type(e).__module__
        tb_tail = "".join(_tb.format_exception(type(e), e, e.__traceback__)[-3:]).strip()
        logger.error(
            f"Kùzu graph construction failed: "
            f"type={exc_module}.{exc_type} "
            f"message={e!r} "
            f"chunks={len(chunks)} relations={len(relations)}"
        )
        logger.debug(f"Kùzu graph construction traceback tail:\n{tb_tail}")
        return False


# ──────────────────────────────────────────────────────────────────────────────
# LanceDB Vector Embedding
# ──────────────────────────────────────────────────────────────────────────────


def _resolve_embedder() -> Any | None:
    """Return the shared embedder singleton, or None if unavailable."""
    try:
        from beagle.infrastructure.services.embedding import (
            get_embedder,
        )

        return get_embedder()
    except ImportError as exc:
        logger.error(f"Embedding service unavailable: {exc}. Skipping vector index.")
        return None
    except (ValueError, TypeError, RuntimeError) as exc:
        logger.error(f"Embedding service initialization failed: {exc}. Skipping vector index.")
        return None


def _iter_batches(iterable: Iterable[Any], size: int) -> Iterator[list[Any]]:
    """Yield successive ``size``-sized lists from an iterable.

    Used by the streaming write path in :func:`rebuild_lancedb_index` to
    keep per-batch memory bounded for large corpora. Stops cleanly when
    the upstream iterable is exhausted.
    """
    batch: list[Any] = []
    for item in iterable:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def _embed_chunk_records(chunks: list[ASTChunk], embedder: Any) -> Iterator[dict[str, Any]]:
    """Yield LanceDB record dicts, embedding in batches of 1024 chunks."""
    # D10 (Fable 5 DD 2026-06-11): prefix convention must match the search
    # path. Both ingestion and search resolve it from the same embedder
    # identity, so a model/prefix/dimension mismatch fails loudly.
    embedder_identity: dict = getattr(embedder, "identity", lambda: {})()
    prefix = embedder_identity.get("prefix", "search_query: ")
    texts = [f"{prefix}{c.text[:1500]}" for c in chunks]

    # v13.19.4: report the actual resolved provider, not a hard-coded label.
    provider_label = embedder.provider if hasattr(embedder, "provider") else "unknown"
    logger.info(
        f"Generating embeddings for {len(texts)} chunks "
        f"(provider={provider_label}, model={embedder_identity.get('model', 'unknown')})..."
    )

    # Batch the encoding call AND yield records per batch so the caller can
    # flush to LanceDB in chunks instead of holding 34k vectors in memory
    # at once. Without streaming, peak memory for a 30k+ chunk corpus is:
    #   - 30k * (768 floats * 4 bytes + ~1KB metadata) ~= 250MB just for records
    #   - 1.2GB for the sentence-transformers model
    #   - 1GB+ for Python runtime + chunk text
    # That OOMs small memory cgroups on constrained hosts.
    # Returning a generator keeps peak per-batch memory bounded at ~5MB
    # while letting the caller materialize only the active batch.
    # v13.22.3: Tightened batch_size from 32 to 8 for memory-constrained
    # hosts). The sentence-transformers internal
    # forward pass allocates a ~216 MB intermediate tensor per batch; at
    # batch_size=32 we OOM'd mid-ingest, and the broad-except handler
    # silently substituted zero vectors — the corpus was rebuilt as
    # 10,975 zero-vectors, which is useless for search. 8 is small
    # enough to stay under the cgroup limit, large enough that the
    # 10,975 vectors still encode in reasonable wall-clock (~5-7 min).
    # v13.22.3: BATCH = 256 (was 1024). Smaller batches keep the in-flight
    # numpy matrix small enough that the segfault-prone pyarrow background
    # thread doesn't trip on a long-running ingest. 10,975 chunks / 256
    # is ~43 batches. With Ollama doing ~0.3s per chunk, total ~55 min.
    BATCH = 256
    SENTENCE_TRANSFORMERS_BATCH = 8
    for batch_start in range(0, len(chunks), BATCH):
        batch_end = min(batch_start + BATCH, len(chunks))
        batch_chunks = chunks[batch_start:batch_end]
        batch_texts = [f"{prefix}{c.text[:1500]}" for c in batch_chunks]
        batch_vectors = embedder.encode(
            batch_texts,
            show_progress_bar=(batch_start == 0),
            batch_size=SENTENCE_TRANSFORMERS_BATCH,
        )
        if not isinstance(batch_vectors, list):
            batch_vectors = [emb.tolist() for emb in batch_vectors]
        for i, chunk in enumerate(batch_chunks):
            yield {
                "vector": batch_vectors[i],
                "ast_entity_id": chunk.ast_entity_id,
                "chunk_id": chunk.chunk_id,
                "filepath": chunk.filepath,
                "language": chunk.language,
                "node_type": chunk.node_type,
                "node_name": chunk.node_name,
                "start_line": chunk.start_line,
                "end_line": chunk.end_line,
                "text": chunk.text[:2000],
                "token_count": chunk.token_count,
            }
        # v13.22.3: periodic GC to keep RSS bounded. The segfault in the
        # previous run was likely in a delayed pyarrow cleanup; forcing
        # the free of per-batch intermediates here reduces long-lived
        # references and keeps RSS below the cgroup cap.
        del batch_vectors
        del batch_chunks
        del batch_texts
        import gc

        gc.collect()
        if (batch_start // BATCH) % 4 == 0:
            logger.debug(f"[Embed] Batch {batch_start}-{batch_end}/{len(chunks)} done")


def _maybe_write_turboquant_sidecar(
    lance_tbl: Any,
    chunks: list[ASTChunk],
    db_root_path: str | None = None,
) -> None:
    """Optionally build a TurboQuant sidecar after a full LanceDB rebuild.

    Reads the vectors back from LanceDB in one pass, compresses with
    TurboQuant, and writes the sidecar at ``<db_root>/rag_vectors_tq.bin``.
    Non-fatal on any failure: the raw LanceDB index is the source of
    truth; the sidecar is an optional memory-saving optimisation.

    Gated on ``[rag].turboquant_sidecar = true`` in config.toml so the
    behaviour can be disabled without code changes.
    """
    try:
        from beagle.infrastructure.turboquant_lance_cache import (
            write_turboquant_sidecar,
        )
    except ImportError as e:
        logger.debug(f"[TurboQuant] sidecar module unavailable: {e}")
        return

    # Config gate — only run if explicitly enabled.
    try:
        from beagle.config.loader import get_config

        enabled = get_config().rag.turboquant_sidecar
    except (ImportError, AttributeError, KeyError, TypeError, ValueError, OSError) as _cfg_exc:
        logger.warning("[TurboQuant] config read failed, defaulting sidecar ON: %s", _cfg_exc)
        enabled = True
    if not enabled:
        logger.warning("[TurboQuant] sidecar disabled in config — skipping")
        return

    if not chunks:
        return

    try:
        # Read the full table back as a numpy matrix. We avoid pandas
        # entirely because the test/minimal venv doesn't ship it; pyarrow
        # is the dependency floor. count_rows() is constant-time on
        # LanceDB; to_arrow() materializes the rows.
        n = lance_tbl.count_rows()
        if n == 0:
            return
        arrow = lance_tbl.to_arrow()
        if "vector" not in arrow.column_names:
            logger.warning(
                "[TurboQuant] LanceDB table has no 'vector' column — cannot build sidecar"
            )
            return
        # PyArrow fixed_size_list<float>[768] -> numpy ndarray via chunked
        # concatenation. flatten() converts fixed-size lists to a flat
        # array; reshape restores (n, dim).
        #
        # v13.22.3 fix: ``arrow.column("vector").flatten()`` on a
        # ``FixedSizeListArray<float>[768]`` does NOT return a flat
        # length-(n*768) 1-D array — it returns an outer list with
        # one element per PyArrow chunk, each element itself being
        # the per-chunk flat array. ``np.asarray(..., dtype=np.float32)``
        # then sees an object-dtype array of length 1 (one chunk),
        # and the subsequent ``.reshape(n, dim)`` raises
        # ``ValueError: setting an array element with a sequence.``
        # because numpy cannot assign the giant chunk into a 2-D
        # shape.
        #
        # The fix is to take the FixedSizeListArray's underlying
        # storage array directly via ``.combine_chunks().values.to_numpy()``
        # — that path gives us the flat (n*dim,) buffer in one zero-copy
        # call, which reshapes cleanly. Validated empirically on a
        # 5,206-row / 768-dim corpus:
        #   arr_col.flatten()             -> len=1   (broken)
        #   combine_chunks().values        -> (3998208,) flat  (correct)
        #   .reshape(5206, 768)            -> (5206, 768)
        #   element-by-element equal to to_pylist()+asarray()
        #   write_turboquant_sidecar():   OK in 2.35s
        try:
            vec_flat = (
                arrow.column("vector").combine_chunks().values.to_numpy(zero_copy_only=False)
            ).astype(np.float32, copy=False)
        except (AttributeError, ValueError, TypeError) as _flatten_exc:
            # Older PyArrow versions (<8.0) may not expose
            # ``FixedSizeListArray.values``; fall back to the slower
            # but universally-supported ``to_pylist`` path. Same final
            # shape, ~3-5x slower on 5k+ row corpora.
            logger.debug(
                f"[TurboQuant] combine_chunks().values path failed "
                f"({_flatten_exc.__class__.__name__}); falling back to "
                f"to_pylist() — slower but compatible with older PyArrow"
            )
            vec_flat = np.asarray(
                arrow.column("vector").to_pylist(),
                dtype=np.float32,
            )
        dim = arrow.schema.field("vector").type.list_size
        if dim <= 0:
            dim = 768  # fallback
        vectors = vec_flat.reshape(n, dim)
        # Build a parallel chunk_id list in the same row order.
        if "chunk_id" in arrow.column_names:
            chunk_ids = [str(v) for v in arrow.column("chunk_id").to_pylist()]
        else:
            chunk_ids = [str(i) for i in range(n)]
        write_turboquant_sidecar(
            vectors,
            chunk_ids,
            db_root_path=db_root_path,
        )
    except (ValueError, RuntimeError, OSError) as e:
        logger.warning(f"[TurboQuant] sidecar write skipped: {e}")


def _sql_quote_list(values: Iterable[str]) -> str:
    """Render an SQL ``IN`` list, escaping embedded single quotes.

    LanceDB's ``delete()`` takes an SQL predicate string with no parameter
    binding, so the quoting has to happen here. Paths come from
    ``scan_codebase`` (i.e. the filesystem), but a filename may legitimately
    contain a quote, and an unescaped one would silently truncate the
    predicate and delete the wrong rows.
    """
    return ", ".join("'" + v.replace("'", "''") + "'" for v in values)


def _list_lance_tables(db: Any) -> list[str]:
    """Return existing table names, across lancedb API generations.

    ``table_names()`` is deprecated in lancedb 0.25+ in favour of
    ``list_tables()``, but the replacement returns a
    ``ListTablesResponse(tables=[...], page_token=...)`` rather than a plain
    list — iterating it does **not** yield table names. Getting this wrong
    silently reports "table missing", which turns an incremental upsert into
    a no-op (deletes never happen). Normalise both shapes here.
    """
    lister = getattr(db, "list_tables", None)
    if lister is not None:
        resp = lister()
        tables = getattr(resp, "tables", resp)
        return [str(t) for t in tables]
    return [str(t) for t in db.table_names()]


def rebuild_lancedb_index(
    chunks: list[ASTChunk],
    db_root_path: str | None = None,
) -> bool:
    """DESTRUCTIVE full rebuild: drop the table and recreate it from ``chunks``.

    Only correct when ``chunks`` is the **complete** corpus. Passing a subset
    deletes everything else.

    B-3 (audit v13.22.1): this used to be called ``build_lancedb_index`` and
    two callers passed subsets to it — ``hotswap_ingest._incremental_update``
    (delta chunks only) and ``ingest()``'s incremental skip path (which omits
    unchanged files). Either would have truncated the index to just the
    changed files. The destructive semantics are now in the name, and
    subset callers must use :func:`upsert_lancedb_chunks`.
    """
    try:
        import lancedb
    except ImportError:
        logger.error("lancedb not installed. Skipping vector index.")
        return False

    target_lance = _get_lancedb_uri(db_root_path)
    os.makedirs(target_lance, exist_ok=True)
    embedder = _resolve_embedder()
    if embedder is None:
        return False

    db = lancedb.connect(target_lance)

    try:
        records_iter = _embed_chunk_records(chunks, embedder)

        # Overwrite existing table (suppress only "not found" errors)
        try:
            db.drop_table(LANCE_TABLE_NAME)
        except (ValueError, RuntimeError) as e:
            if "not found" not in str(e).lower() and "does not exist" not in str(e).lower():
                logger.warning(f"Could not drop existing table: {e}")

        # Stream the records into LanceDB. create_table() materializes
        # whatever iterable you pass, so for large corpora we must write
        # in batches. The first batch creates the table schema; subsequent
        # batches append via add().
        LANCE_WRITE_BATCH = 1024
        first_batch: list[dict[str, Any]] = []
        for _ in range(LANCE_WRITE_BATCH):
            try:
                first_batch.append(next(records_iter))
            except StopIteration:
                break
        if not first_batch:
            logger.warning("No records to write — skipping LanceDB rebuild")
            return True
        tbl = db.create_table(LANCE_TABLE_NAME, first_batch)
        written = len(first_batch)
        for batch in _iter_batches(records_iter, LANCE_WRITE_BATCH):
            tbl.add(batch)
            written += len(batch)
        logger.info(f"LanceDB index rebuilt: {written} vectors in '{LANCE_TABLE_NAME}'")

        # v13.22.3: After writing the raw vectors, also build a
        # TurboQuant-compressed sidecar at <db_root>/rag_vectors_tq.bin
        # so the MCP search path can do a brute-force numpy cosine
        # on a 30k x 768 matrix held in ~30 MB instead of ~250 MB.
        # Reading back from LanceDB (one pass, np.column_stack) keeps
        # peak memory bounded — we never hold the 30k raw vectors AND
        # the compressed version at the same time. Failures here are
        # non-fatal: the index is still usable, just without the
        # memory-saving sidecar.
        _maybe_write_turboquant_sidecar(
            tbl,
            chunks,
            db_root_path=db_root_path,
        )
        return True

    except (ValueError, RuntimeError, OSError) as e:
        logger.error(f"LanceDB index construction failed: {e}")
        return False


def upsert_lancedb_chunks(
    chunks: list[ASTChunk],
    deleted_filepaths: Iterable[str] = (),
    db_root_path: str | None = None,
) -> bool:
    """Incrementally update the vector index in place.

    Deletes every row belonging to a changed or removed file, then appends
    the freshly embedded chunks. Creates the table if it does not exist yet,
    so a first incremental run degrades to an append rather than failing.

    Args:
        chunks: Chunks for added/modified files only.
        deleted_filepaths: Files removed from the codebase. Their rows are
            dropped and nothing is re-added. Rows for the files present in
            ``chunks`` are dropped too — that is what makes a modification
            a replace rather than a duplicate.
        db_root_path: Optional explicit database root path.

    Returns:
        True on success.

    """
    try:
        import lancedb
    except ImportError:
        logger.error("lancedb not installed. Skipping vector index update.")
        return False

    target_lance = _get_lancedb_uri(db_root_path)
    os.makedirs(target_lance, exist_ok=True)
    db = lancedb.connect(target_lance)

    changed = sorted({c.filepath for c in chunks})
    removed = sorted(set(deleted_filepaths))
    stale_paths = sorted(set(changed) | set(removed))

    try:
        existing = _list_lance_tables(db)
    except (ValueError, RuntimeError, OSError) as e:
        logger.error(f"LanceDB connect failed: {e}")
        return False

    if LANCE_TABLE_NAME not in existing:
        if not chunks:
            logger.info("[LanceDB] Nothing to upsert and no table yet — noop")
            return True
        logger.info(f"[LanceDB] '{LANCE_TABLE_NAME}' missing — creating from upsert batch")
        return rebuild_lancedb_index(chunks, db_root_path=db_root_path)

    embedder = _resolve_embedder() if chunks else None
    if chunks and embedder is None:
        return False

    try:
        tbl = db.open_table(LANCE_TABLE_NAME)

        # 1. Drop rows for files that changed or disappeared.
        if stale_paths:
            tbl.delete(f"filepath IN ({_sql_quote_list(stale_paths)})")
            logger.info(
                f"[LanceDB] Removed rows for {len(stale_paths)} file(s) "
                f"({len(changed)} changed, {len(removed)} deleted)"
            )

        # 2. Append the new chunks.
        if chunks:
            records_iter = _embed_chunk_records(chunks, embedder)
            written = 0
            for batch in _iter_batches(records_iter, 1024):
                tbl.add(batch)
                written += len(batch)
            logger.info(f"[LanceDB] Added {written} vectors")

        logger.info(f"[LanceDB] Incremental update complete: {tbl.count_rows()} rows total")
        return True

    except (ValueError, RuntimeError, OSError) as e:
        logger.error(f"LanceDB incremental update failed: {e}")
        return False


def _write_vector_store(
    chunks: list[ASTChunk],
    partial_corpus: bool,
    db_root_path: str | None = None,
) -> bool:
    """Persist ``chunks``, choosing rebuild vs upsert by corpus completeness.

    ``ingest()`` skips unchanged files when incremental ingestion is enabled,
    which makes ``chunks`` a partial corpus. Rebuilding from a partial corpus
    deletes every skipped file's rows (B-3), so route those runs through the
    in-place upsert instead.
    """
    if partial_corpus:
        return upsert_lancedb_chunks(chunks, db_root_path=db_root_path)
    return rebuild_lancedb_index(chunks, db_root_path=db_root_path)


def build_lancedb_index(
    chunks: list[ASTChunk],
    db_root_path: str | None = None,
) -> bool:
    """Deprecated alias for :func:`rebuild_lancedb_index`.

    Kept so external callers keep working. New code must choose explicitly
    between ``rebuild_lancedb_index`` (full corpus) and
    ``upsert_lancedb_chunks`` (delta) — the ambiguity of this name is what
    made B-3 possible.
    """
    return rebuild_lancedb_index(chunks, db_root_path=db_root_path)


# ──────────────────────────────────────────────────────────────────────────────
# Main Ingestion Pipeline
# ──────────────────────────────────────────────────────────────────────────────
def scan_codebase(root: Path) -> list[Path]:
    """Discover all supported source files under root, respecting .gitignore-like exclusions.

    v13.22.3: Tightened the excluded_dirs set to drop the runtime-state,
    audit, and adjacent-project noise that was bloating the RAG index
    on constrained hosts. The corpus should be "the codebase and its
    config" — not the session state, audit reports, or sibling projects
    that happen to live next door. The excluded list below is the
    empirical "what's not code" for this monorepo; tighten further by
    adding to excluded_doc_patterns or via a per-project allowlist.
    """
    excluded_dirs = {
        # VCS / build / cache (already excluded in earlier versions)
        ".git",
        "__pycache__",
        "node_modules",
        ".venv",
        "venv",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        "dist",
        "build",
        ".eggs",
        "cache",
        ".ruff_cache",
        ".hypothesis",
        "site-packages",
        # Runtime / session state (NEW v13.22.3) — not code, not config
        ".beagle",  # Beagle runtime state (cache, folds, progress.xml)
        ".goose",  # goose session state
        ".agents",  # goose agent config (read by goose, not by RAG)
        ".claude",  # claude session state
        ".devcontainer",  # devcontainer config (build infra)
        # Operational / planning / audit artefacts (NEW v13.22.3) — low
        # signal for semantic retrieval, blow up the corpus
        "audits",  # audit reports (per-fix markdown)
        "benchmarks",  # benchmark scripts — separate workflow
        "plans",  # planning docs
        "examples",  # example snippets, often stale
        ".github",  # CI workflows
        # Adjacent projects living in this monorepo — NOT part of the
        # beagle codebase. Add to this set when more
        # sibling projects appear rather than re-ingesting them.
        "beagle_containerisation",
        "beagle_dockeriser",
        "hooks",  # separate hook scripts (different repo area)
        "ai",  # dev notes
    }
    # Exclude large/generated doc files that are not primary context
    excluded_doc_patterns = {
        "CHANGELOG.md",  # too noisy, low signal
        # The incremental-ingest cache is a build artifact, not corpus:
        # scanning it back in would re-introduce stale keys into the index.
        ".beagle_ingest_cache.json",
    }
    # NEW v13.22.3: Tooling files at the project root that look like code
    # but aren't — they're for AI tools / single-machine workflow glue.
    # We keep .md / .toml / .yaml / .json as code-bearing; we skip the
    # ones that are *only* for the agent runtime.
    excluded_root_filenames = {
        "CLAUDE.md",  # guidance for the Claude agent, not the RAG corpus
        "AGENTS.xml",  # goose agent manifest (sibling of CLAUDE.md)
        "BEAGLE_CLI_CATALOG.md",  # reference for operators, not codebase
        "ARCH_REPORT.md",  # one-shot audit, often stale
        "CODEBASE_AUDIT_REPORT.md",  # same
    }
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in excluded_dirs]
        is_root = Path(dirpath) == root
        for fname in filenames:
            fpath = Path(dirpath) / fname
            if fpath.suffix in SUPPORTED_EXTENSIONS:
                # Skip excluded doc patterns (auto-generated, low signal files)
                if fname in excluded_doc_patterns:
                    continue
                # v13.22.3: At the project root, drop agent-tooling files
                # that aren't part of the codebase (CLAUDE.md, AGENTS.xml,
                # audit reports). These look like code but are for the AI
                # agent runtime, not the project proper.
                if is_root and fname in excluded_root_filenames:
                    continue
                files.append(fpath)
    return sorted(files)


def ingest(
    target_dir: str | Path,
    db_root_path: str | None = None,
) -> IngestionResult:
    """Execute the full CAST ingestion pipeline.

    v13.5.2: When running interactively (TTY), displays a Rich progress bar
    for file processing and embedding generation. In headless/CI mode,
    falls back to plain logging.
    """
    target = Path(target_dir)
    if not target.is_dir():
        return IngestionResult(errors=[f"Target directory not found: {target}"])

    result = IngestionResult()
    start = time.monotonic()

    # Check ramdisk staging availability
    staging_dir = _get_staging_dir()
    # nosec B108 - "/tmp" is compared against, not written to: this asks whether
    # the staging dir is the ramdisk or the plain temp fallback.
    using_ramdisk = staging_dir != "/tmp" and "beagle_rag_staging" in staging_dir  # nosec B108
    if using_ramdisk:
        logger.info(f"[Ramdisk] Using ramdisk staging: {staging_dir}")

    # Load incremental ingestion cache (B-20: config lookup was off by one
    # directory and silently disabled this whole feature).
    ingest_cache: dict[str, dict[str, str]] = {}
    use_incremental = bool(_load_hardware_config().get("incremental_ingest", True))
    if use_incremental:
        ingest_cache = _load_ingest_cache(str(target))
        logger.info(f"[Incremental] Cache has {len(ingest_cache)} entries")

    logger.info(f"CAST Ingestion Pipeline starting for: {target}")
    logger.info(f"Config: chunk_size={CHUNK_SIZE_TOKENS}, overlap={OVERLAP_RATIO * 100:.0f}%")

    # 1. Discover files
    files = scan_codebase(target)
    logger.info(f"Discovered {len(files)} source files")

    if not files:
        result.errors.append("No supported source files found.")
        return result

    # ── Determine if we should show a Rich progress bar ──
    is_tty = sys.stdout.isatty()

    # 2. Parse and chunk (with incremental skip)
    global _ssd_writes_saved_bytes
    all_chunks: list[ASTChunk] = []
    skipped_count = 0
    # Per-file chunk counts, for the delta-engine state file (B-4).
    chunk_counts: dict[str, int] = {}

    if is_tty:
        # ── Interactive mode: Rich progress bar ──
        from rich.progress import (
            BarColumn,
            MofNCompleteColumn,
            Progress,
            SpinnerColumn,
            TextColumn,
            TimeElapsedColumn,
        )

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=None,  # Use default Rich console
        ) as progress:
            file_task = progress.add_task("📄 Parsing files", total=len(files))

            for fpath in files:
                try:
                    source = fpath.read_text(encoding="utf-8", errors="replace")
                except OSError as e:
                    result.warnings.append(f"Failed to read {fpath}: {e}")
                    progress.advance(file_task)
                    continue

                # Incremental: skip unchanged files
                if use_incremental and str(fpath) in ingest_cache:
                    cached_entry = ingest_cache[str(fpath)]
                    try:
                        current_mtime = str(fpath.stat().st_mtime)
                        if cached_entry.get("mtime") == current_mtime:
                            file_size = fpath.stat().st_size
                            with _ssd_counter_lock:
                                _ssd_writes_saved_bytes += file_size
                            skipped_count += 1
                            progress.advance(file_task)
                            continue
                    except OSError as exc:
                        logger.warning(
                            "Cannot stat %s (%s); ignoring its incremental-cache "
                            "entry and re-parsing the file.",
                            fpath,
                            exc,
                        )

                chunks = chunk_file(fpath, source)

                all_chunks.extend(chunks)
                chunk_counts[str(fpath)] = len(chunks)
                result.files_processed += 1

                # Update ingest cache entry
                if use_incremental:
                    try:
                        file_hash = hashlib.sha256(source.encode()).hexdigest()[:16]
                        ingest_cache[str(fpath)] = {
                            "mtime": str(fpath.stat().st_mtime),
                            "hash": file_hash,
                        }
                    except OSError as exc:
                        logger.warning(
                            "Cannot record an incremental-cache entry for %s (%s); "
                            "the file will be re-parsed on the next ingest.",
                            fpath,
                            exc,
                        )

                progress.advance(file_task)

            progress.update(file_task, description="📄 Parsing complete")

    else:
        # ── Headless/CI mode: plain logging ──
        for fpath in files:
            try:
                source = fpath.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                result.warnings.append(f"Failed to read {fpath}: {e}")
                continue

            # Incremental: skip unchanged files
            if use_incremental and str(fpath) in ingest_cache:
                cached_entry = ingest_cache[str(fpath)]
                try:
                    current_mtime = str(fpath.stat().st_mtime)
                    if cached_entry.get("mtime") == current_mtime:
                        file_size = fpath.stat().st_size
                        with _ssd_counter_lock:
                            _ssd_writes_saved_bytes += file_size
                        skipped_count += 1
                        continue
                except OSError as exc:
                    logger.warning(
                        "Cannot stat %s (%s); ignoring its incremental-cache "
                        "entry and re-parsing the file.",
                        fpath,
                        exc,
                    )

            chunks = chunk_file(fpath, source)

            all_chunks.extend(chunks)
            chunk_counts[str(fpath)] = len(chunks)
            result.files_processed += 1

            # Update ingest cache entry
            if use_incremental:
                try:
                    file_hash = hashlib.sha256(source.encode()).hexdigest()[:16]
                    ingest_cache[str(fpath)] = {
                        "mtime": str(fpath.stat().st_mtime),
                        "hash": file_hash,
                    }
                except OSError as exc:
                    logger.warning(
                        "Cannot record an incremental-cache entry for %s (%s); "
                        "the file will be re-parsed on the next ingest.",
                        fpath,
                        exc,
                    )

    logger.info(f"Chunked {result.files_processed} files into {len(all_chunks)} AST chunks")
    result.chunks_created = len(all_chunks)

    # B-3/B-20 interaction: with the incremental skip working again (B-20),
    # `all_chunks` is a PARTIAL corpus whenever files were skipped — it holds
    # only the files that were re-parsed. Rebuilding the vector index from a
    # partial corpus would delete every skipped file's rows. Decide the write
    # mode here, once, from whether anything was actually skipped.
    partial_corpus = skipped_count > 0
    reparsed_paths = sorted(chunk_counts.keys())
    if partial_corpus:
        logger.info(
            f"[Incremental] {skipped_count} unchanged file(s) skipped — "
            f"updating stores in place for {len(reparsed_paths)} re-parsed file(s)"
        )

    # 3. Extract relations
    all_relations = extract_relations(all_chunks)
    result.relations_extracted = len(all_relations)
    logger.info(f"Extracted {len(all_relations)} deterministic relations")

    # 4. Build Kùzu graph
    # v13.19.5: When Kùzu graph construction fails (e.g. unordered_map::at
    # from Kùzu's C++ core on a stale KUZU_URI schema mismatch), we
    # DEGRADE to vector-only RAG instead of aborting the ingestion.
    # LanceDB still holds all the chunks, so retrieval works; we just
    # lose graph-traversal augmentation. Mark `result.partial = True`
    # and log a structured error so callers and RAGStaleness can act.
    if partial_corpus:
        # Drop the re-parsed files' old nodes so renames/removals inside them
        # do not survive as orphans (MERGE alone cannot remove anything).
        delete_kuzu_nodes_for_files(reparsed_paths, db_root_path=db_root_path)
    graph_ok = build_kuzu_graph(all_chunks, all_relations, db_root_path=db_root_path)
    if not graph_ok:
        result.partial = True
        result.warnings.append(
            "Kùzu graph construction failed — RAG will operate in "
            "vector-only mode (no AST relation traversal). "
            "This is non-fatal; check prior log lines for the exception type."
        )
        # v13.21: Clear relations count on Kùzu failure — relations cannot
        # be persisted without the graph store. Previously reported non-zero
        # relations even when 0 were actually written, which misled the
        # hot-swap path into thinking the data was on disk.
        result.relations_extracted = 0

    # 5. Build LanceDB vector index (with optional progress bar)
    if is_tty:
        from rich.progress import (
            BarColumn,
            MofNCompleteColumn,
            Progress,
            SpinnerColumn,
            TextColumn,
            TimeElapsedColumn,
        )

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TimeElapsedColumn(),
            console=None,
        ) as progress:
            embed_task = progress.add_task("🧮 Embedding chunks", total=len(all_chunks))
            # Monkey-patch a progress callback into the embedder if possible
            _original_encode = None
            try:
                from beagle.infrastructure.services.embedding import (
                    get_embedder,
                )

                embedder = get_embedder()
                _original_encode = embedder.encode

                def _progress_encode(*args, **kwargs):  # type: ignore[no-untyped-def]
                    result_vectors = _original_encode(*args, **kwargs)
                    progress.advance(embed_task, advance=len(args[0]) if args else 1)
                    return result_vectors

                embedder.encode = _progress_encode  # type: ignore[method-assign]  # runtime monkey-patch
            except ImportError as exc:
                logger.warning(
                    "Cannot import the embedder to attach progress reporting (%s); "
                    "embedding proceeds without a progress bar.",
                    exc,
                )

            vector_ok = _write_vector_store(all_chunks, partial_corpus, db_root_path=db_root_path)

            # Restore original encode if we patched it
            if _original_encode is not None:
                with contextlib.suppress(Exception):
                    embedder.encode = _original_encode  # type: ignore[method-assign]  # runtime monkey-patch restore

            progress.update(
                embed_task,
                completed=len(all_chunks),
                description="🧮 Embedding complete",
            )
    else:
        vector_ok = _write_vector_store(all_chunks, partial_corpus, db_root_path=db_root_path)

    if not vector_ok:
        result.errors.append("LanceDB index construction failed (dependency missing or error)")
        # v13.21: Clear chunks count on LanceDB failure — chunks cannot be
        # persisted without the vector store. Previously reported non-zero
        # chunks even when 0 were actually written, which misled the
        # hot-swap path into thinking the data was on disk. The cascading
        # effect (downstream consumers trusting the count) is the primary
        # reason RAG stayed at the 4-chunk stub corpus.
        result.chunks_created = 0

    result.elapsed_seconds = time.monotonic() - start
    logger.info(
        f"CAST Ingestion complete: {result.files_processed} files, "
        f"{result.chunks_created} chunks, {result.relations_extracted} relations "
        f"in {result.elapsed_seconds:.2f}s"
    )

    # SSD write savings report
    if use_incremental and skipped_count > 0:
        saved_mb = round(_ssd_writes_saved_bytes / (1024 * 1024), 1)
        logger.info(
            f"[INFO] SSD writes saved this session: {saved_mb} MB "
            f"thanks to incremental ingestion ({skipped_count} files skipped)."
        )
    if using_ramdisk and _ssd_writes_saved_bytes > 0:
        saved_mb = round(_ssd_writes_saved_bytes / (1024 * 1024), 1)
        logger.info(
            f"[INFO] SSD writes saved this session: {saved_mb} MB thanks to ramdisk staging."
        )

    # Save incremental cache
    if use_incremental:
        _save_ingest_cache(str(target), ingest_cache)

    # B-4 (audit v13.22.1): record the delta-engine state so the next run can
    # compute a real diff. `update_state_after_ingestion` and
    # `remove_from_state` previously had ZERO callers anywhere in the tree, so
    # ~/.beagle/rag_state.json never existed, `compute_delta` always reported
    # "No state file found (first ingestion)", and the incremental path could
    # never be taken — every trigger ran the full multi-minute re-index.
    #
    # Only write state when the stores actually accepted the data: state that
    # claims a file is indexed when it is not would suppress the re-index that
    # would fix it.
    if vector_ok:
        try:
            from .delta_engine import update_state_after_ingestion

            if partial_corpus:
                # Merge: unchanged files keep their existing state entries.
                update_state_after_ingestion(reparsed_paths, chunk_counts, merge=True)
            else:
                update_state_after_ingestion([str(f) for f in files], chunk_counts, merge=False)
        except ImportError as exc:
            logger.warning(f"[Delta] State not recorded (delta_engine unavailable): {exc}")
        except OSError as exc:
            logger.warning(f"[Delta] State not recorded: {exc}")

    return result


# ──────────────────────────────────────────────────────────────────────────────
# CLI Entry Point
# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if len(sys.argv) < 2:
        logger.info("Usage: python -m infrastructure.cast_ingestion <target_directory>")
        logger.info("  Indexes a codebase into LanceDB + Kùzu for Hybrid RAG retrieval.")
        logger.info(f"  Output: {db_root()}")
        sys.exit(1)

    target = sys.argv[1]
    result = ingest(target)

    logger.info(
        json.dumps(
            {
                "files_processed": result.files_processed,
                "chunks_created": result.chunks_created,
                "relations_extracted": result.relations_extracted,
                "errors": result.errors,
                "elapsed_seconds": round(result.elapsed_seconds, 2),
            },
            indent=2,
        )
    )

    sys.exit(1 if result.errors else 0)
