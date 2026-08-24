"""Prompt caching with static/dynamic boundary separation.

Static content (recipe, system directive) is tokenised once and reused
across retries and model fallback attempts. Dynamic content (user query,
steering, constraints) is rebuilt per attempt.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any

from ..cost_tracker import estimate_tokens_agnostic

logger = logging.getLogger("Beagle.context.prompt_cache")


@dataclass(frozen=True)
class StaticPromptPart:
    """Holds immutable part of a node prompt."""

    recipe_content: str
    system_directive: str
    token_count: int
    content_hash: str


@dataclass(frozen=True)
class PromptMetadata:
    """Metrics for the assembled prompt."""

    static_tokens: int
    dynamic_tokens: int
    total_tokens: int
    cache_hit: bool


class PromptCache:
    """Manages static/dynamic prompt splitting for token efficiency."""

    def __init__(self) -> None:
        self._static_cache: dict[str, StaticPromptPart] = {}

    def set_static(self, node_name: str, recipe_content: str, system_directive: str) -> None:
        """Cache the static portion of a prompt for a specific node."""
        content = f"{recipe_content}\n{system_directive}"
        content_hash = hashlib.sha256(content.encode()).hexdigest()

        # Avoid re-calculating if already cached and unchanged
        if (
            node_name in self._static_cache
            and self._static_cache[node_name].content_hash == content_hash
        ):
            return

        token_count = estimate_tokens_agnostic(content)

        self._static_cache[node_name] = StaticPromptPart(
            recipe_content=recipe_content,
            system_directive=system_directive,
            token_count=token_count,
            content_hash=content_hash,
        )
        logger.debug(f"Cached static prompt for node '{node_name}' ({token_count} tokens)")

    def build_prompt(
        self,
        node_name: str,
        intent: str,
        steering: str = "",
        constraints: str = "",
        memory_pointers: str | None = None,
    ) -> tuple[str, PromptMetadata]:
        """Assemble the full POML prompt with boundary markers."""
        static = self._static_cache.get(node_name)
        cache_hit = static is not None

        if not static:
            # Fallback if not set (though orchestrator should always set it)
            recipe_val = ""
            sys_val = ""
            static_tokens = 0
        else:
            recipe_val = static.recipe_content
            sys_val = static.system_directive
            static_tokens = static.token_count

        # Build dynamic parts
        dynamic_parts = [f"<intent>{intent}</intent>"]
        if steering:
            dynamic_parts.append(f"<steering>\n{steering}\n</steering>")
        if constraints:
            dynamic_parts.append(f"<constraints>\n{constraints}\n</constraints>")

        dynamic_content = "\n".join(dynamic_parts)
        dynamic_tokens = estimate_tokens_agnostic(dynamic_content)

        # Memory pointers (Phase 8.2 integration point)
        memory_block = ""
        if memory_pointers:
            memory_block = f"\n<memory>\n{memory_pointers}\n</memory>\n"
            dynamic_tokens += estimate_tokens_agnostic(memory_block)

        # Assemble with boundaries
        prompt = f"""<!-- STATIC BOUNDARY START (cached) -->
<recipe>
{recipe_val}
</recipe>
<system_directive>
{sys_val}
</system_directive>
<!-- STATIC BOUNDARY END -->
{memory_block}
<!-- DYNAMIC BOUNDARY START (rebuilt) -->
{dynamic_content}
<!-- DYNAMIC BOUNDARY END -->
"""
        metadata = PromptMetadata(
            static_tokens=static_tokens,
            dynamic_tokens=dynamic_tokens,
            total_tokens=static_tokens + dynamic_tokens,
            cache_hit=cache_hit,
        )

        return prompt.strip(), metadata

    def snapshot(self) -> dict[str, Any]:
        """Return a snapshot of the static cache for forking (Phase 8.5)."""
        return self._static_cache.copy()
