"""autoDream — Background memory consolidation for Beagle.

Prunes, merges, and refreshes the tiered memory stores.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..config.config import get_config
from ..cost_tracker import estimate_tokens_agnostic
from ..events import (
    AutoDreamCompleted,
    AutoDreamMerged,
    AutoDreamPruned,
    AutoDreamRefreshed,
    get_event_bus,
)
from ..tracking.database import TrackingDatabase
from .memory_index import MemoryIndex

logger = logging.getLogger("Beagle.memory.autodream")


@dataclass
class ConsolidationReport:
    """Result of an autoDream consolidation run."""

    pruned_count: int = 0
    merged_count: int = 0
    refreshed_count: int = 0
    index_tokens_before: int = 0
    index_tokens_after: int = 0
    duration_seconds: float = 0.0
    errors: list[str] = field(default_factory=list)


class AutoDream:
    """Handles background memory maintenance."""

    def __init__(self, workspace_root: Path | str) -> None:
        self.workspace_root = Path(workspace_root)
        self.memory_index = MemoryIndex(self.workspace_root)
        self.db = TrackingDatabase.get_instance()
        self._cfg = get_config().memory_consolidation

    async def consolidate(self) -> ConsolidationReport:
        """Run full consolidation suite."""
        start_time = time.monotonic()
        report = ConsolidationReport()

        try:
            report.index_tokens_before = estimate_tokens_agnostic(
                self.memory_index.get_semantic_layer()
            )

            # 1. PRUNE
            report.pruned_count = await self.prune()

            # 2. MERGE
            report.merged_count = await self.merge()

            # 3. REFRESH
            report.refreshed_count = await self.refresh()

            report.index_tokens_after = estimate_tokens_agnostic(
                self.memory_index.get_semantic_layer()
            )
            report.duration_seconds = time.monotonic() - start_time

            get_event_bus().publish(
                AutoDreamCompleted(
                    workflow_id="autodream",
                    pruned=report.pruned_count,
                    merged=report.merged_count,
                    refreshed=report.refreshed_count,
                    index_tokens_before=report.index_tokens_before,
                    index_tokens_after=report.index_tokens_after,
                )
            )

            return report
        except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
            logger.error(f"Consolidation failed: {e}")
            report.errors.append(str(e))
            return report

    # ── Relevance scoring for prune decisions ─────────────────────────────

    @staticmethod
    def _score_entry_relevance(entry: str, position: int, section_length: int) -> float:
        """Score a memory index entry for relevance-based pruning.

        Uses recency (position in list), information density, and
        uniqueness signals to decide whether to keep or prune.

        Args:
            entry: The memory index entry string.
            position: Position in the section (0-based).
            section_length: Total entries in this section.

        Returns:
            Float score 0-10. Lower scores are candidates for pruning.

        """
        score = 5.0  # neutral baseline

        # ── Recency: entries near the end are more recent ──
        if section_length > 0:
            recency_ratio = position / section_length
            score += recency_ratio * 2.0  # up to +2.0

        # ── Information density: longer, more specific entries are valuable ──
        word_count = len(entry.split())
        if word_count > 5:
            score += min(word_count * 0.15, 1.5)
        elif word_count <= 2:
            score -= 1.0  # very short entries likely low-value

        # ── Specificity: entries with file paths, line refs, or specifics ──
        if re.search(r"\.\w{1,4}[:\]]", entry):  # e.g. ".py]", ".js:"
            score += 1.0  # file references boost relevance
        if re.search(r"line\s*\d+", entry, re.IGNORECASE):
            score += 0.5  # line references add specificity

        # ── Type signals: findings/problems are more valuable than status ──
        entry_lower = entry.lower()
        if any(kw in entry_lower for kw in ("error", "bug", "fix", "issue", "vuln")):
            score += 1.0
        if any(kw in entry_lower for kw in ("success", "completed", "ok", "done")):
            score -= 0.5  # status reports are less actionable

        return max(0.0, min(10.0, score))

    async def prune(self) -> int:
        """Remove stale, redundant, or low-relevance findings.

        Enhanced with relevance scoring: entries are scored and those
        below the relevance threshold are pruned in addition to
        date-based and deduplication checks.
        """
        data = self.memory_index._read_index()
        recent_findings = data.get("Recent Findings", [])

        if not recent_findings:
            get_event_bus().publish(AutoDreamPruned(workflow_id="autodream", count=0))
            return 0

        pruned_indices: set[int] = set()
        current_time = time.time()
        processed_pointers: list[str] = []

        for i, pointer in enumerate(recent_findings):
            # ── 1. Date-based staleness (> 30 days) ──
            date_match = re.search(r"\[(\d{4}-\d{2}-\d{2})\]", pointer)
            if date_match:
                try:
                    p_time = time.mktime(time.strptime(date_match.group(1), "%Y-%m-%d"))
                    if (current_time - p_time) > (self._cfg.prune_staleness_days * 86400):
                        pruned_indices.add(i)
                        continue
                except ValueError as exc:
                    logger.warning(
                        "Memory pointer carries an unparseable date %r (%s); it is "
                        "exempt from staleness pruning and will accumulate.",
                        date_match.group(1),
                        exc,
                    )

            # ── 2. Exact deduplication (date-agnostic) ──
            if self._cfg.prune_dedup_enabled:
                clean_pointer = re.sub(r"\[\d{4}-\d{2}-\d{2}\]", "", pointer).strip()
                if clean_pointer in processed_pointers:
                    pruned_indices.add(i)
                    continue
                processed_pointers.append(clean_pointer)

            # ── 3. Relevance scoring (keep high-value entries) ──
            relevance = self._score_entry_relevance(pointer, i, len(recent_findings))
            if relevance < self._cfg.prune_relevance_threshold:
                pruned_indices.add(i)

        # Update the list
        data["Recent Findings"] = [
            p for i, p in enumerate(recent_findings) if i not in pruned_indices
        ]
        self.memory_index._write_index(data)

        count = len(pruned_indices)
        get_event_bus().publish(AutoDreamPruned(workflow_id="autodream", count=count))
        return count

    async def merge(self) -> int:
        """Semantic merge: consolidate similar episodic memory entries.

        Groups entries by topic similarity (shared key terms), then
        merges each group into a single summarized entry. Reduces
        episodic memory size while preserving coverage.
        """
        if not self._cfg.merge_enabled:
            get_event_bus().publish(AutoDreamMerged(workflow_id="autodream", count=0))
            return 0

        try:
            from .hierarchical_memory import (
                MemoryLevel,
                get_hierarchical_memory,
            )

            mem = await get_hierarchical_memory()
        except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
            logger.debug(f"[autodream/merge] HierarchicalMemory unavailable: {e}")
            get_event_bus().publish(AutoDreamMerged(workflow_id="autodream", count=0))
            return 0

        # Collect all working + episodic entries for merge consideration
        async with mem._lock:
            entries = list(mem._working.values()) + list(mem._episodic)
            if len(entries) < 2:
                get_event_bus().publish(AutoDreamMerged(workflow_id="autodream", count=0))
                return 0

        # ── Group entries by topic (shared key terms) ──
        groups: dict[str, list[int]] = {}
        for idx, entry in enumerate(entries):
            # Extract top 3 keyword-like terms as a topic key
            words = re.findall(r"[a-z]{3,}", entry.content.lower())
            # Remove common stop words
            stop = {
                "the",
                "and",
                "for",
                "that",
                "this",
                "with",
                "are",
                "was",
                "not",
                "but",
                "has",
                "all",
                "from",
                "been",
                "have",
                "will",
                "they",
                "their",
            }
            keywords = [w for w in words if w not in stop]
            key = " ".join(sorted(set(keywords[:3])))
            if key not in groups:
                groups[key] = []
            groups[key].append(idx)

        # ── Merge groups with 2+ entries into a single summary entry ──
        merged_count = 0
        entries_to_remove: set[int] = set()

        for _key, indices in groups.items():
            if len(indices) < self._cfg.merge_min_group_size:
                continue  # single-entry groups don't need merging

            # Create merged content from group
            group_entries = [entries[i] for i in indices]
            merged_tokens = sum(len(e.content.split()) for e in group_entries)

            # Build concise merged summary
            summary_parts = []
            for entry in group_entries:
                # Take first sentence (up to first period) from each
                first_sentence = entry.content.split(".")[0].strip()
                if first_sentence and len(first_sentence) > 20:
                    summary_parts.append(first_sentence)

            if not summary_parts:
                continue

            merged_content = ". ".join(
                summary_parts[: self._cfg.merge_max_summary_parts]
            )  # Cap at config limit
            if len(merged_content) > 500:
                merged_content = merged_content[:497] + "..."

            # Store merged entry as a new episodic
            await mem.store(
                content=merged_content,
                level=MemoryLevel.EPISODIC,
                metadata={
                    "type": "consolidated",
                    "merged_from": len(group_entries),
                    "original_tokens": merged_tokens,
                },
            )

            # Mark originals for removal
            for i in indices:
                entries_to_remove.add(i)
            merged_count += len(indices)

        # Remove merged originals from working and episodic
        if entries_to_remove:
            async with mem._lock:
                # Remove from working
                working_to_remove = {
                    k
                    for k, v in mem._working.items()
                    if v.id in {entries[i].id for i in entries_to_remove if i < len(entries)}
                }
                for k in working_to_remove:
                    del mem._working[k]

                # Remove from episodic
                ids_to_remove = {entries[i].id for i in entries_to_remove if i < len(entries)}
                mem._episodic = [e for e in mem._episodic if e.id not in ids_to_remove]

            mem._save_episodic()

        get_event_bus().publish(AutoDreamMerged(workflow_id="autodream", count=merged_count))
        return merged_count

    async def refresh(self) -> int:
        """Update index from recent tracking database findings."""
        runs = self.db.get_workflow_runs(limit=5)
        new_findings_count = 0

        all_findings = []
        for run in runs:
            findings = self.db.get_findings_for_run(run.id)
            all_findings.extend(findings)

        if all_findings:
            # Update memory index with these findings
            self.memory_index.update_from_findings(all_findings)  # type: ignore[arg-type]
            new_findings_count = len(all_findings)

        get_event_bus().publish(
            AutoDreamRefreshed(
                workflow_id="autodream",
                count=new_findings_count,
                new_pointers=new_findings_count,
            )
        )
        return new_findings_count
