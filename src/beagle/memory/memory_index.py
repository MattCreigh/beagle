"""Three-layer memory index for Beagle.

Layer 1 — Semantic Index (always in context):
    A lightweight MEMORY_INDEX.md file with ~150 char/line pointers.
Layer 2 — Detailed Notes (pulled on demand via RAG):
    Stored in the RAG knowledge graph (LanceDB + Kùzu).
Layer 3 — Session History (searched selectively):
    Stored in the tracking database.

Configuration:
    TOKEN_BUDGET is configurable via:
    1. ``[memory] index_token_budget`` in config.toml
    2. ``BEAGLE_MEMORY_INDEX_TOKEN_BUDGET`` environment variable
    3. Default: 2000

    Prune strategy is configurable via:
    1. ``[memory] index_prune_strategy`` in config.toml
       Values: "oldest_first", "relevance_weighted", "hybrid"
    2. Default: "oldest_first"
"""

from __future__ import annotations

import logging
import os
import re
import time
from enum import StrEnum
from pathlib import Path
from typing import Any

from ..cost_tracker import estimate_tokens_agnostic
from ..output.schema import Finding
from ..tracking.database import TrackingDatabase
from ..utils.atomic import atomic_write_text

logger = logging.getLogger("Beagle.memory.index")

INDEX_HEADER = "# Beagle Memory Index (auto-maintained, do not edit manually)\n\n"

# ── Configurable token budget ─────────────────────────────────────────────────
# Precedence: BEAGLE_MEMORY_INDEX_TOKEN_BUDGET env var > config.toml > default (2000)
_DEFAULT_TOKEN_BUDGET = 2000

# Backward-compatible module-level constant.
# Tests and callers that patch ``beagle.memory.memory_index.TOKEN_BUDGET``
# will still work; the actual value is resolved at runtime by _get_token_budget().
TOKEN_BUDGET = _DEFAULT_TOKEN_BUDGET


class PruneStrategy(StrEnum):
    """Strategy for pruning memory index entries when over budget.

    - ``oldest_first``: Remove oldest "Recent Findings" entries first (original behavior).
    - ``relevance_weighted``: Score entries by recency * relevance, remove lowest scores.  # noqa: RUF002 — math notation
    - ``hybrid``: Use relevance_weighted first, fall back to oldest_first when scores cluster.
    """

    OLDEST_FIRST = "oldest_first"
    RELEVANCE_WEIGHTED = "relevance_weighted"
    HYBRID = "hybrid"


def _get_token_budget() -> int:
    """Load token budget from config, falling back to env var, then default.

    Returns:
        Token budget as integer (minimum: 500).

    """
    # 1. Environment variable takes highest precedence
    env_val = os.environ.get("BEAGLE_MEMORY_INDEX_TOKEN_BUDGET")
    if env_val is not None:
        try:
            budget = int(env_val)
            if budget < 500:
                logger.warning(
                    f"BEAGLE_MEMORY_INDEX_TOKEN_BUDGET={budget} is below minimum (500). "
                    "Clamping to 500."
                )
                return 500
            return budget
        except ValueError:
            logger.warning(
                f"Invalid BEAGLE_MEMORY_INDEX_TOKEN_BUDGET={env_val!r}, using config/default."
            )

    # 2. Try loading from config.toml
    try:
        from ..config.config import get_config

        config_budget = getattr(get_config().memory, "index_token_budget", None)
        if config_budget is not None:
            return max(int(config_budget), 500)
    except (
        ImportError,
        AttributeError,
        RuntimeError,
        OSError,
    ) as exc:  # catch: NARROWED  # RATIONALE=four-tuple: lazy import, attribute lookup on index schema, runtime guard on store size, OS errors on index file
        logger.warning(
            "Cannot read [memory].index_token_budget from configuration (%s); using "
            "the built-in default budget.",
            exc,
        )

    # 3. Default
    return _DEFAULT_TOKEN_BUDGET


def _get_prune_strategy() -> PruneStrategy:
    """Load prune strategy from config, falling back to env var, then default.

    Returns:
        PruneStrategy enum value.

    """
    # 1. Environment variable
    env_val = os.environ.get("BEAGLE_MEMORY_INDEX_PRUNE_STRATEGY")
    if env_val is not None:
        try:
            return PruneStrategy(env_val.lower())
        except ValueError:
            logger.warning(
                f"Invalid BEAGLE_MEMORY_INDEX_PRUNE_STRATEGY={env_val!r}. "
                f"Valid values: {[s.value for s in PruneStrategy]}. Using default."
            )

    # 2. Try config
    try:
        from ..config.config import get_config

        config_strategy = getattr(get_config().memory, "index_prune_strategy", None)
        if config_strategy is not None:
            return PruneStrategy(config_strategy)
    except (
        ImportError,
        AttributeError,
        RuntimeError,
        OSError,
    ) as exc:  # catch: NARROWED  # RATIONALE=four-tuple: lazy import, attribute lookup on index schema, runtime guard on store size, OS errors on index file
        logger.warning(
            "Cannot read [memory].index_prune_strategy from configuration (%s); using "
            "the built-in default strategy OLDEST_FIRST.",
            exc,
        )

    # 3. Default
    return PruneStrategy.OLDEST_FIRST


class MemoryIndex:
    """Manages the tiered memory index layers.

    Supports configurable token budgets and multiple pruning strategies.
    Budget is loaded from config/env with a minimum floor of 500 tokens.
    Pruning respects section priority: Core Skills and Key Patterns are never
    pruned unless budget is catastrophically exceeded (>150% of budget).
    """

    def __init__(
        self,
        data_root: Path | str,
        token_budget: int | None = None,
        prune_strategy: PruneStrategy | str | None = None,
    ) -> None:
        """Initialize the memory index.

        Args:
            data_root: Writable directory that owns MEMORY_INDEX.md. Typically
                ``get_data_root()`` (defaults to ``~/.beagle``). Callers that
                previously passed ``workspace_root`` should migrate — the file
                is state, not asset, and must not live under a read-only
                site-packages install.
            token_budget: Override token budget (default: loaded from config/env).
            prune_strategy: Override prune strategy (default: loaded from config/env).

        """
        self.data_root = Path(data_root)
        # Keep attribute name `workspace_root` as a deprecated alias so any
        # external code that read it directly continues to work.
        self.workspace_root = self.data_root
        self.index_path = self.data_root / "MEMORY_INDEX.md"
        self.index_path.parent.mkdir(parents=True, exist_ok=True)

        # Resolve token budget: explicit arg > config > env > default
        self.token_budget = token_budget or _get_token_budget()

        # Resolve prune strategy: explicit arg > config > env > default
        if prune_strategy is not None:
            self.prune_strategy = (
                PruneStrategy(prune_strategy) if isinstance(prune_strategy, str) else prune_strategy
            )
        else:
            self.prune_strategy = _get_prune_strategy()

        logger.debug(
            f"MemoryIndex initialized: budget={self.token_budget}, "
            f"strategy={self.prune_strategy.value}"
        )

        if not self.index_path.exists():
            self._write_index({"Architecture": [], "Recent Findings": [], "Active Decisions": []})

    def get_semantic_layer(self) -> str:
        """Get the always-loaded pointer text (Layer 1)."""
        try:
            return self.index_path.read_text(encoding="utf-8")
        except (OSError, FileNotFoundError, PermissionError, ValueError) as e:  # catch: NARROWED
            logger.error(f"Failed to read memory index: {e}")
            return INDEX_HEADER

    def update_from_findings(self, findings: list[Finding]) -> None:
        """Update Layer 1 with new findings pointers."""
        data = self._read_index()
        section = data.get("Recent Findings", [])

        current_date = time.strftime("%Y-%m-%d")
        new_pointers = []

        for f in findings:
            # Format: - category: title [file] [date]
            loc = f" [{f.file_path}]" if f.file_path else ""
            pointer = f"- {f.category}: {f.title}{loc} [{current_date}]"

            # Limit length
            if len(pointer) > 150:
                pointer = pointer[:147] + "..."

            # Check for existing
            exists = False
            for i, p in enumerate(section):
                if f.title in p and (not f.file_path or f.file_path in p):
                    section[i] = pointer  # Update
                    exists = True
                    break

            if not exists:
                new_pointers.append(pointer)

        # Prepend new ones to keep them at top
        data["Recent Findings"] = new_pointers + section

        # Enforce budget
        self._write_index(data)

    def update_from_decision(self, key: str, value: str) -> None:
        """Add or update a pointer in Active Decisions."""
        data = self._read_index()
        section = data.get("Active Decisions", [])

        current_date = time.strftime("%Y-%m-%d")
        pointer = f"- {key}: {value} [{current_date}]"

        exists = False
        for i, p in enumerate(section):
            if p.startswith(f"- {key}:"):
                section[i] = pointer
                exists = True
                break

        if not exists:
            section.append(pointer)

        data["Active Decisions"] = section
        self._write_index(data)

    async def retrieve_detail(self, query: str) -> list[str]:
        """Pull detail for a specific query via RAG (Layer 2)."""
        # Integration point for MCP RAG server
        # For now, return empty or mock
        logger.debug(f"Layer 2 retrieval requested for: {query}")
        return []

    async def search_history(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """Search tracking database for history (Layer 3)."""
        db = TrackingDatabase.get_instance()
        # Simple text search fallback - real implementation would use vector search if available
        runs = db.get_workflow_runs(limit=100)
        matches = []
        for run in runs:
            if query.lower() in run.query.lower() or (
                run.error_summary and query.lower() in run.error_summary.lower()
            ):
                matches.append(run.to_dict())
                if len(matches) >= limit:
                    break
        return matches

    def _read_index(self) -> dict[str, list[str]]:
        """Parse the MEMORY_INDEX.md into sections."""
        if not self.index_path.exists():
            return {}

        content = self.index_path.read_text(encoding="utf-8")
        sections: dict[str, list[str]] = {}
        current_section = None

        for line in content.splitlines():
            if line.startswith("## "):
                current_section = line[3:].strip()
                sections[current_section] = []
            elif line.startswith("- ") and current_section:
                sections[current_section].append(line)

        return sections

    # Sections that are protected from pruning unless budget is catastrophically exceeded
    _PROTECTED_SECTIONS = frozenset({"Core Skills", "Key Patterns"})
    _CATASTROPHIC_THRESHOLD = 1.5  # Prune protected sections only at 150% budget

    def _write_index(self, data: dict[str, list[str]]) -> None:
        """Write sections back to MEMORY_INDEX.md with budget enforcement.

        Uses the configured prune strategy and respects section protection:
        - "Core Skills" and "Key Patterns" are never pruned unless budget
          is exceeded by >150%.
        - "Recent Findings" is pruned first (primary target).
        - Other sections are pruned only when "Recent Findings" is empty.
        """
        content = self._rebuild_content(data)
        current_tokens = estimate_tokens_agnostic(content)

        if current_tokens <= self.token_budget:
            # Under budget, no pruning needed
            atomic_write_text(self.index_path, content, mode=0o644)
            return

        # Budget exceeded — apply pruning based on strategy
        data = self._apply_prune_strategy(data, current_tokens)

        # Final rebuild and write
        content = self._rebuild_content(data)
        atomic_write_text(self.index_path, content, mode=0o644)

    def _apply_prune_strategy(
        self, data: dict[str, list[str]], current_tokens: int
    ) -> dict[str, list[str]]:
        """Apply the configured pruning strategy to reduce index size.

        Args:
            data: Current index sections.
            current_tokens: Current token count.

        Returns:
            Pruned data dictionary.

        """
        if self.prune_strategy == PruneStrategy.OLDEST_FIRST:
            return self._prune_oldest_first(data, current_tokens)
        elif self.prune_strategy == PruneStrategy.RELEVANCE_WEIGHTED:
            return self._prune_relevance_weighted(data, current_tokens)
        elif self.prune_strategy == PruneStrategy.HYBRID:
            # Try relevance-weighted first; fall back to oldest_first if scores cluster
            result = self._prune_relevance_weighted(data, current_tokens)
            recon_tokens = estimate_tokens_agnostic(self._rebuild_content(result))
            if recon_tokens > self.token_budget:
                result = self._prune_oldest_first(result, recon_tokens)
            return result
        else:
            # Unknown strategy, fall back to oldest_first
            return self._prune_oldest_first(data, current_tokens)

    def _prune_oldest_first(
        self, data: dict[str, list[str]], current_tokens: int
    ) -> dict[str, list[str]]:
        """Prune by removing oldest entries from "Recent Findings" first.

        Protected sections ("Core Skills", "Key Patterns") are only pruned
        if budget is exceeded by more than 150%.
        """
        data = {k: list(v) for k, v in data.items()}  # Deep copy

        while estimate_tokens_agnostic(self._rebuild_content(data)) > self.token_budget:
            # 1. Prune from Recent Findings first (bottom = oldest)
            findings = data.get("Recent Findings", [])
            if findings:
                findings.pop()
                continue

            # 2. Prune from non-protected sections
            pruned = False
            for section_name in data:
                if section_name in self._PROTECTED_SECTIONS:
                    continue
                if data[section_name]:
                    data[section_name].pop()
                    pruned = True
                    break
            if pruned:
                continue

            # 3. Only prune protected sections if we're at 150%+ budget
            if current_tokens > self.token_budget * self._CATASTROPHIC_THRESHOLD:
                for section_name in self._PROTECTED_SECTIONS:
                    if data.get(section_name):
                        data[section_name].pop()
                        pruned = True
                        break
                if pruned:
                    continue

            # Nothing left to prune
            break

        return data

    def _prune_relevance_weighted(
        self, data: dict[str, list[str]], current_tokens: int
    ) -> dict[str, list[str]]:
        """Prune by removing least relevant entries (oldest with no recent references).

        Scoring: recency_score * keyword_richness_score.
        Higher-scoring entries are kept; lowest-scoring entries are pruned first.
        Protected sections are only pruned at 150%+ budget.
        """

        data = {k: list(v) for k, v in data.items()}  # Deep copy

        while estimate_tokens_agnostic(self._rebuild_content(data)) > self.token_budget:
            # Find the lowest-scoring entry across all prunable sections
            worst_section = None
            worst_idx = None
            worst_score = float("inf")

            for section_name, entries in data.items():
                is_protected = section_name in self._PROTECTED_SECTIONS
                if (
                    is_protected
                    and current_tokens <= self.token_budget * self._CATASTROPHIC_THRESHOLD
                ):
                    continue  # Skip protected sections unless catastrophically over budget

                for idx, entry in enumerate(entries):
                    score = self._score_entry(entry, idx, len(entries))
                    if score < worst_score:
                        worst_score = score
                        worst_section = section_name
                        worst_idx = idx

            if worst_section is not None and worst_idx is not None:
                data[worst_section].pop(worst_idx)
            else:
                break  # Nothing left to prune

        return data

    @staticmethod
    def _score_entry(entry: str, position: int, section_length: int) -> float:
        """Score a memory index entry by recency and content richness.

        Args:
            entry: The pointer string (e.g., "- bug: null pointer [file.py] [2024-01-01]")
            position: Position in section (0 = newest, end = oldest)
            section_length: Total entries in this section.

        Returns:
            Float score — higher is more relevant, should be pruned last.

        """
        # Recency: newer entries (lower position) score higher
        recency = 1.0 - (position / max(section_length, 1))

        # Content richness: entries with file paths, dates, and keyword diversity score higher
        has_file = 1.0 if "[" in entry else 0.0
        has_date = 1.0 if re.search(r"\d{4}-\d{2}-\d{2}", entry) else 0.0
        keyword_count = min(entry.count(":") + entry.count("|"), 3) / 3.0

        # Recency is weighted most (0.6), then file reference (0.25), then date (0.15)
        return (recency * 0.6) + (has_file * 0.25) + (has_date * 0.1) + (keyword_count * 0.05)

    def _rebuild_content(self, data: dict[str, list[str]]) -> str:
        """Rebuild index content."""
        lines = [INDEX_HEADER]
        for section, pointers in data.items():
            lines.append(f"## {section}")
            lines.extend(pointers)
            lines.append("")
        return "\n".join(lines)
