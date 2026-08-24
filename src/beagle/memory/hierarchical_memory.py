"""Hierarchical Memory System for Beagle v12.2

Based on research from:
- Memento-Skills (arXiv:2603.18743) - Read-Write Reflective Learning
- Hierarchical Memory Theory (arXiv:2603.21564) - Three operators: alpha, C, tau  # noqa: RUF002 — math notation from cited paper
- MemCollab (arXiv:2603.23234) - Cross-agent memory collaboration

Implements:
- Level 1: Working Memory (current session)
- Level 2: Episodic Memory (completed nodes)
- Level 3: Long-term Memory (skill library)
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger("Beagle.hierarchical_memory")

# Configuration
# Import paths relative to the package
try:
    from ..config.paths import get_memory_dir as _get_mem_dir
except ImportError:
    # Fallback for standalone execution
    def _get_mem_dir() -> None:  # type: ignore[misc]
        """get mem dir."""
        from beagle.config.paths import get_workspace_root

        return get_workspace_root() / "data" / "memory"  # type: ignore[return-value]


MEMORY_DIR = _get_mem_dir()


class MemoryLevel(Enum):
    """Memory hierarchy levels."""

    WORKING = "working"  # Current session context
    EPISODIC = "episodic"  # Completed node executions
    LONG_TERM = "long_term"  # Persistent skill library


@dataclass
class MemoryEntry:
    """A single memory entry."""

    id: str
    level: MemoryLevel
    content: str
    timestamp: float = field(default_factory=time.time)
    access_count: int = 0
    last_access: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def touch(self) -> None:
        """Update access statistics."""
        self.access_count += 1
        self.last_access = time.time()


@dataclass
class ExtractionResult:
    """Result of extraction operator (alpha)."""

    atoms: list[str]  # Atomic information units
    extraction_time_ms: float


@dataclass
class CoarseningResult:
    """Result of coarsening operator (C)."""

    groups: list[list[str]]  # Partitioned atoms
    representatives: list[str]  # Representative per group


class HierarchicalMemory:
    """Hierarchical memory with three operators from theory.

    Three Operators (per arXiv:2603.21564):
    - Extraction (alpha): Raw data → atomic units  # noqa: RUF002 — math notation from cited paper
    - Coarsening (C): Atoms → groups + representatives
    - Traversal (tau): Query + budget → selected units  # noqa: RUF002 — math notation from cited paper
    """

    def __init__(
        self,
        memory_dir: Path | None = None,
        working_ttl: int | None = None,
        episodic_max: int | None = None,
    ) -> None:
        from beagle.config.config import get_config

        config = get_config()
        self.memory_dir = memory_dir or MEMORY_DIR
        self.memory_dir.mkdir(parents=True, exist_ok=True)

        self.working_ttl = (
            working_ttl if working_ttl is not None else config.memory.working_memory_ttl
        )
        self.episodic_max = (
            episodic_max if episodic_max is not None else config.memory.episodic_memory_max
        )

        self._working: dict[str, MemoryEntry] = {}
        self._episodic: list[MemoryEntry] = []
        self._working_max: int = (
            config.memory.working_memory_max
            if hasattr(config.memory, "working_memory_max")
            else 100
        )
        self._lock = asyncio.Lock()

        self._load_episodic()

    # ── Operator alpha: Extraction ────────────────────────────────────

    def extract(self, raw_data: str, query: str = "") -> ExtractionResult:
        """Extract atomic units from raw data.

        Args:
            raw_data: Raw content to extract from
            query: Optional query to guide extraction

        Returns:
            ExtractionResult with list of atomic units

        """
        start = time.monotonic()

        # Simple extraction: split by sentences, then filter by relevance
        sentences = raw_data.replace("\n", " ").split(". ")
        atoms = []

        for sent in sentences:
            sent = sent.strip()
            if len(sent) > 20:  # Filter very short fragments
                # Score by relevance to query
                if query:
                    query_terms = set(query.lower().split())
                    sent_terms = set(sent.lower().split())
                    overlap = len(query_terms & sent_terms)
                    if overlap == 0:
                        continue  # Skip non-relevant atoms

                atoms.append(sent)

        extraction_time_ms = (time.monotonic() - start) * 1000
        return ExtractionResult(atoms=atoms, extraction_time_ms=extraction_time_ms)

    # ── Operator C: Coarsening ──────────────────────────────────────────────────

    def coarsen(
        self,
        atoms: list[str],
        num_groups: int = 5,
    ) -> CoarseningResult:
        """Group atoms into clusters with representatives.

        Args:
            atoms: List of atomic units
            num_groups: Target number of groups

        Returns:
            CoarseningResult with groups and representatives

        """
        if not atoms:
            return CoarseningResult(groups=[], representatives=[])

        # Simple coarsening: chunk atoms into groups
        chunk_size = max(1, len(atoms) // num_groups)
        groups = []

        for i in range(0, len(atoms), chunk_size):
            chunk = atoms[i : i + chunk_size]
            groups.append(chunk)

        # Representatives are the first (most important) atom per group
        representatives = [g[0] if g else "" for g in groups]

        return CoarseningResult(groups=groups, representatives=representatives)

    # ── Operator τ: Traversal ───────────────────────────────────────────────────

    async def traverse(
        self,
        query: str,
        budget_tokens: int = 1000,
    ) -> list[MemoryEntry]:
        """Retrieve relevant memories within token budget.

        Args:
            query: Query string
            budget_tokens: Token budget for retrieval

        Returns:
            List of relevant memory entries

        """
        async with self._lock:
            results = []
            total_tokens = 0

            # Search all levels, preferring recent/relevant
            all_entries = list(self._working.values()) + self._episodic

            # Score and sort
            scored = []
            for entry in all_entries:
                score = self._score_relevance(entry, query)
                scored.append((score, entry))

            scored.sort(key=lambda x: x[0], reverse=True)

            # Select within budget
            for _score, entry in scored:
                entry_tokens = len(entry.content) // 4  # Rough estimate
                if total_tokens + entry_tokens > budget_tokens:
                    break
                entry.touch()
                results.append(entry)
                total_tokens += entry_tokens

            return results

    def _score_relevance(self, entry: MemoryEntry, query: str) -> float:
        """Score memory relevance to query using multi-signal ranking.

        Signals: term overlap (Jaccard), n-gram overlap, metadata,
        recency (exponential decay), access frequency, and level bias.
        """
        score: float = 0.0
        query_lower = query.lower()
        content_lower = entry.content.lower()

        # ── Term-level overlap (Jaccard) ──
        query_terms = set(query_lower.split())
        content_terms = set(content_lower.split())
        intersection = query_terms & content_terms
        union = query_terms | content_terms
        if union:
            jaccard = len(intersection) / len(union)
            score += jaccard * 3.0
        score += len(intersection) * 1.0  # raw overlap bonus

        # ── N-gram overlap (bigrams) for phrase matching ──
        def _bigrams(text: str) -> set[str]:
            words = text.split()
            return {f"{words[i]} {words[i + 1]}" for i in range(len(words) - 1)}

        q_bigrams = _bigrams(query_lower)
        c_bigrams = _bigrams(content_lower)
        if q_bigrams and c_bigrams:
            bigram_overlap = len(q_bigrams & c_bigrams) / max(len(q_bigrams | c_bigrams), 1)
            score += bigram_overlap * 2.0

        # ── Metadata match ──
        if entry.metadata:
            for value in entry.metadata.values():
                if query_lower in str(value).lower():
                    score += 1.5

        # ── Recency bonus (exponential decay, half-life 4 hours) ──
        age_hours = (
            # wall-clock-ok: compares against a persisted timestamp
            time.time() - entry.timestamp
        ) / 3600
        recency = 3.0 * (0.5 ** (age_hours / 4.0))
        score += recency

        # ── Access frequency bonus (logarithmic) ──
        score += min(math.log1p(entry.access_count) * 0.5, 2.0)

        # ── Level bias: working memory slightly preferred ──
        if entry.level == MemoryLevel.WORKING:
            score += 0.5
        elif entry.level == MemoryLevel.EPISODIC:
            score += 0.3

        return score

    # ── Memory Operations ──────────────────────────────────────────────────────

    async def store(
        self,
        content: str,
        level: MemoryLevel,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Store content in memory.

        Args:
            content: Content to store
            level: Memory level
            metadata: Optional metadata

        Returns:
            Memory entry ID

        """
        import uuid

        entry_id = str(uuid.uuid4())
        entry = MemoryEntry(
            id=entry_id,
            level=level,
            content=content,
            metadata=metadata or {},
        )

        async with self._lock:
            if level == MemoryLevel.WORKING:
                self._working[entry_id] = entry
                # v13.22.4: WORKING-level stores must also enforce
                # _working_max via LRU eviction. The previous code
                # only called _evict_working_lru() on EPISODIC stores,
                # so WORKING memory grew unboundedly past _working_max
                # until an unrelated EPISODIC store happened to evict.
                self._evict_working_lru()
            elif level == MemoryLevel.EPISODIC:
                self._episodic.append(entry)
                # Enforce episodic limit
                while len(self._episodic) > self.episodic_max:
                    self._episodic.pop(0)
                # Enforce working memory limit (LRU eviction)
                self._evict_working_lru()

        # Persist episodic memory
        if level == MemoryLevel.EPISODIC:
            self._save_episodic()

        return entry_id

    async def retrieve(
        self,
        entry_id: str,
        level: MemoryLevel | None = None,
    ) -> MemoryEntry | None:
        """Retrieve a specific memory entry.

        Args:
            entry_id: ID of the entry to retrieve
            level: Optional memory level to search in (None = all levels)

        """
        async with self._lock:
            if (level == MemoryLevel.WORKING or level is None) and entry_id in self._working:
                entry = self._working[entry_id]
                entry.touch()
                return entry

            if level == MemoryLevel.EPISODIC or level is None:
                for entry in self._episodic:
                    if entry.id == entry_id:
                        entry.touch()
                        return entry

            return None

    async def consolidate(
        self,
        max_atoms: int = 50,
    ) -> int:
        """Consolidate working memory into episodic.

        Args:
            max_atoms: Maximum atoms to keep per episode

        Returns:
            Number of atoms consolidated

        """
        # v13.22.4: asyncio.Lock is NOT reentrant. The previous
        # comment "we already hold the lock from caller" was an
        # invitation to call consolidate() from inside another
        # critical section — instant deadlock. If a future caller
        # needs nested locking, use a reentrant pattern (e.g.,
        # try-acquire + skip if held) or expose a _consolidate_locked
        # helper. For now: always acquire here, document the trap.
        async with self._lock:
            if not self._working:
                return 0

            # Extract and coarsen working memory
            all_content = "\n".join(e.content for e in self._working.values())
            extraction = self.extract(all_content)

            # Keep only top atoms
            atoms_to_keep = extraction.atoms[:max_atoms]

            if not atoms_to_keep:
                self._working.clear()
                return 0

            consolidated = ". ".join(atoms_to_keep)

            # Create episodic entry directly (don't call self.store to avoid deadlock)
            import uuid

            entry_id = str(uuid.uuid4())
            entry = MemoryEntry(
                id=entry_id,
                level=MemoryLevel.EPISODIC,
                content=consolidated,
                metadata={"type": "consolidated", "atoms": len(atoms_to_keep)},
            )
            self._episodic.append(entry)

            # Enforce episodic limit
            while len(self._episodic) > self.episodic_max:
                self._episodic.pop(0)

            # Clear working memory
            self._working.clear()

            # Save to disk (sync operation, outside lock is fine)
            self._save_episodic()

            return len(atoms_to_keep)

    # ── ZSTD Compression Support (v13.5.2) ──────────────────────────────────
    #
    # Episodic memory is compressed with zstd level 3 on write to reduce
    # disk footprint on the SSD.  Loading detects the magic header and
    # decompresses transparently.  Plain JSON files are still readable
    # for backward compatibility.  Falls back to plain JSON if the
    # `zstandard` package is not installed.

    _ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"  # zstd frame magic number
    _ZSTD_COMPRESSION_LEVEL: int = 3

    @staticmethod
    def _zstd_available() -> bool:
        """Check if the zstandard library is importable."""
        import importlib.util as _ilu

        return _ilu.find_spec("zstandard") is not None

    def _load_episodic(self) -> None:
        """Load episodic memory from disk.

        Supports both compressed (.json.zst) and legacy plain JSON files.
        Detects format via the zstd magic header — compressed content starts
        with bytes 0x28 0xB5 0x2F 0xFD.
        """
        zst_file = self.memory_dir / "episodic.json.zst"
        json_file = self.memory_dir / "episodic.json"

        # Prefer compressed file, fall back to legacy JSON
        load_path = zst_file if zst_file.exists() else json_file
        if not load_path.exists():
            return

        try:
            raw_bytes = load_path.read_bytes()

            # Detect compression via magic header
            is_compressed = raw_bytes[:4] == self._ZSTD_MAGIC

            if is_compressed:
                try:
                    import zstandard as zstd

                    dctx = zstd.ZstdDecompressor()
                    raw_bytes = dctx.decompress(raw_bytes)
                    logger.debug(
                        f"[memory] Decompressed {load_path.name} "
                        f"({len(load_path.read_bytes())} -> {len(raw_bytes)} bytes)"
                    )
                except ImportError:
                    logger.warning(
                        "[memory] zstandard not installed — cannot decompress "
                        f"{load_path.name}. Install with: pip install zstandard"
                    )
                    return

            data = json.loads(raw_bytes.decode("utf-8"))
            self._episodic = [
                MemoryEntry(
                    id=e["id"],
                    level=MemoryLevel.EPISODIC,
                    content=e["content"],
                    timestamp=e.get("timestamp", time.time()),
                    metadata=e.get("metadata", {}),
                )
                for e in data
            ]
            logger.info(f"Loaded {len(self._episodic)} episodic memories from {load_path.name}")

        except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
            logger.warning(f"Failed to load episodic memory: {e}")

    def _evict_working_lru(self) -> None:
        """Evict least-recently-used working memory entries when over limit.

        Must be called with self._lock held. Evicts expired TTL entries first,
        then LRU entries until within the working_max limit.
        """
        now = time.time()
        # Phase 1: Evict TTL-expired entries
        expired = [k for k, v in self._working.items() if (now - v.timestamp) > self.working_ttl]
        for k in expired:
            del self._working[k]
        if expired:
            logger.debug(f"[memory] Evicted {len(expired)} TTL-expired working entries")
        # Phase 2: LRU eviction if still over limit
        while len(self._working) > self._working_max:
            # Find least recently accessed entry
            lru_key = min(
                self._working,
                key=lambda k: self._working[k].last_access or self._working[k].timestamp,
            )
            del self._working[lru_key]

    def _save_episodic(self) -> None:
        """Save episodic memory to disk.

        Compresses with zstd level 3 if zstandard is available,
        writing to `episodic.json.zst`.  Falls back to plain
        `episodic.json` otherwise.
        """
        data = [
            {
                "id": e.id,
                "content": e.content,
                "timestamp": e.timestamp,
                "metadata": e.metadata,
            }
            for e in self._episodic[-self.episodic_max :]
        ]

        json_bytes = json.dumps(data, indent=2).encode("utf-8")

        if self._zstd_available():
            zst_file = self.memory_dir / "episodic.json.zst"
            try:
                import zstandard as zstd

                cctx = zstd.ZstdCompressor(level=self._ZSTD_COMPRESSION_LEVEL)
                compressed = cctx.compress(json_bytes)
                # v13.22.4: atomic write via temp + os.replace. The
                # previous direct write_bytes() left a truncated
                # file on crash/OOM mid-compress; next _load_episodic
                # raised inside dctx.decompress, caught by the broad
                # except, and SILENTLY LOST ALL EPISODIC MEMORY.
                tmp_zst = zst_file.with_suffix(zst_file.suffix + f".tmp.{os.getpid()}")
                tmp_zst.write_bytes(compressed)
                os.replace(str(tmp_zst), str(zst_file))

                ratio = len(json_bytes) / max(len(compressed), 1)
                logger.info(
                    f"[memory] Saved episodic memory: {len(json_bytes)} -> "
                    f"{len(compressed)} bytes (ratio: {ratio:.1f}x) "
                    f"as {zst_file.name}"
                )

                # Remove legacy JSON file if it exists (migration)
                json_file = self.memory_dir / "episodic.json"
                if json_file.exists():
                    try:
                        json_file.unlink()
                        logger.debug("[memory] Removed legacy episodic.json after zst migration")
                    except OSError as exc:
                        logger.warning(
                            "[memory] Cannot remove the legacy episodic.json after the "
                            "zstd migration (%s); it is left on disk and will be read "
                            "again on the next migration attempt.",
                            exc,
                        )

                return
            except OSError as e:
                logger.warning(f"[memory] zstd compression failed, falling back to JSON: {e}")

        # Fallback: write plain JSON (atomic via tmp + os.replace)
        json_file = self.memory_dir / "episodic.json"
        try:
            tmp_json = json_file.with_suffix(json_file.suffix + f".tmp.{os.getpid()}")
            tmp_json.write_text(json_bytes.decode("utf-8"))
            os.replace(str(tmp_json), str(json_file))
            logger.debug(f"[memory] Saved episodic memory as plain JSON ({len(json_bytes)} bytes)")
        except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
            logger.error(f"Failed to save episodic memory: {e}")

    async def get_stats(self) -> dict[str, Any]:
        """Get memory statistics."""
        async with self._lock:
            return {
                "working_count": len(self._working),
                "episodic_count": len(self._episodic),
                "total_entries": len(self._working) + len(self._episodic),
                "memory_dir": str(self.memory_dir),
            }


# ── Global Instance ────────────────────────────────────────────────────────────

_memory: HierarchicalMemory | None = None


async def get_hierarchical_memory() -> HierarchicalMemory:
    """Get global hierarchical memory instance."""
    global _memory
    if _memory is None:
        _memory = HierarchicalMemory()
    return _memory


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":

    async def demo() -> None:
        mem = await get_hierarchical_memory()

        # Store some memories
        await mem.store(
            "Planning phase completed successfully",
            MemoryLevel.EPISODIC,
            metadata={"phase": "planning", "status": "success"},
        )

        await mem.store(
            "Execution found 3 key modules: auth, database, cache",
            MemoryLevel.EPISODIC,
            metadata={"phase": "execution", "modules": 3},
        )

        # Retrieve with query
        results = await mem.traverse("planning execution", budget_tokens=200)

        logger.info(f"Memory stats: {await mem.get_stats()}")
        logger.info(f"Query results: {len(results)} entries")
        for entry in results:
            logger.info(f"  - {entry.content[:50]}...")

    asyncio.run(demo())
