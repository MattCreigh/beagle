"""Prompt template builder — a leaf module (stdlib only, no intra-package imports).

SP-7 (beagle-spotless-phase2): ``_make_prompt_builder`` was defined in
``core/nodes.py`` and used by ``bridges/llm_node.py``. Because ``core.nodes``
lazily imports ``bridges.llm_node`` (the langchain-LLM executor) and
``bridges.llm_node`` lazily imports ``core.nodes._make_prompt_builder``, the two
formed a cycle. Extracting the pure builder here (imports nothing from the
package) lets both depend on this leaf without a cycle.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("Beagle.utils.prompt_builder")


def make_prompt_builder(
    template: str,
    node_name: str = "",
) -> Callable[[dict[str, Any]], str]:
    """Create a prompt builder from a template with {variable} substitution.

    Args:
        template: String template with {variable} placeholders.
        node_name: Optional node identifier for warning context (v13.12.5).

    Returns:
        A callable that takes state dict and returns formatted prompt string.

    Behaviour (v13.12.5):
        Unknown ``{token}`` placeholders are stripped after substitution;
        a WARNING is emitted listing each unresolved variable so silent
        template drift (e.g. a renamed key) is visible in logs instead of
        silently producing an empty substitution.

    """

    def builder(state: dict[str, Any]) -> str:
        subs = {
            "query": state.get("query", ""),
            "research_plan": state.get("research_plan", ""),
            "raw_execution_context": state.get("raw_execution_context", ""),
            "search_results": state.get("raw_execution_context", ""),
            "verified_facts": state.get("verified_facts", ""),
            "final_report": state.get("final_report", ""),
            "project_context": state.get("hydrated_context", ""),
            "project_documentation": state.get("hydration_documentation", ""),
        }
        # Add metadata entries
        for k, v in state.get("metadata", {}).items():
            if isinstance(v, str):
                subs[k] = v
        result = template
        for key, value in subs.items():
            result = result.replace(f"{{{key}}}", str(value) if value else "")
        # Warn on any remaining {token}s before stripping them — silent
        # substitution of unknown vars is a template-drift signal that
        # must be visible in logs (v13.12.5).
        remaining = re.findall(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", result)
        if remaining:
            tag = f"[{node_name}] " if node_name else ""
            logger.warning(
                f"{tag}unresolved variable(s) {remaining} in prompt template — "
                f"check state keys and template for drift."
            )
        result = re.sub(r"\{[a-zA-Z_][a-zA-Z0-9_]*\}", "", result)
        return result.strip()

    return builder
