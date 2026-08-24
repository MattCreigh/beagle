"""Prompt injection logic for mid-workflow steering.

Injects steering guidance into prompts at appropriate points to influence
agent behavior without disrupting the existing prompt structure.
"""

from __future__ import annotations

import re

from .types import SteeringDirective


def inject_steering(prompt: str, directive: SteeringDirective) -> str:
    """Inject steering guidance into a prompt.

    Places a <steering> block in the prompt at the canonical location:
    after </recipe> (if present) or before <system_directive>.

    Args:
        prompt: Original prompt template
        directive: Parsed steering directive with guidance

    Returns:
        Modified prompt with steering block injected, or original if no guidance

    """
    if not directive.has_guidance or not directive.priority_guidance:
        return prompt

    steering_block = _format_steering_block(directive)

    # Primary insertion: after </recipe>
    if "</recipe>" in prompt:
        parts = prompt.split("</recipe>", 1)
        return f"{parts[0]}</recipe>\n{steering_block}\n{parts[1]}"

    # Secondary: before <system_directive>
    if "<system_directive>" in prompt:
        parts = prompt.split("<system_directive>", 1)
        return f"{parts[0]}{steering_block}\n<system_directive>{parts[1]}"

    # Tertiary: before <context>
    if "<context>" in prompt:
        parts = prompt.split("<context>", 1)
        return f"{parts[0]}{steering_block}\n<context>{parts[1]}"

    # Final fallback: prepend with clear marker
    return f"{steering_block}\n\n{prompt}"


def _format_steering_block(directive: SteeringDirective) -> str:
    """Format steering guidance as an XML-style block."""
    lines = [
        "<steering>",
        f"<!-- Source: {directive.source} -->",
        directive.priority_guidance,
        "</steering>",
    ]
    return "\n".join(lines)


def inject_steering_metadata(prompt: str, directive: SteeringDirective) -> str:
    """Inject steering metadata without full guidance block.

    Useful for adding low-priority hints that don't warrant a full block.
    Adds a comment-style marker at the end of the prompt.
    """
    if not directive.has_guidance:
        return prompt

    parts = []

    if directive.skip_nodes:
        parts.append(f"NOTE: Skip the following nodes: {', '.join(directive.skip_nodes)}")

    if directive.stop_after_node:
        parts.append(f"NOTE: Stop workflow after completing: {directive.stop_after_node}")

    if directive.budget_override_usd is not None:
        parts.append(f"NOTE: Budget adjusted to ${directive.budget_override_usd:.2f}")

    if not parts:
        return prompt

    metadata_block = "\n<!-- Steering Hints -->\n" + "\n".join(f"<!-- {p} -->" for p in parts)
    return f"{prompt}{metadata_block}"


def extract_steering_tags(prompt: str) -> str | None:
    """Extract existing steering block from a prompt.

    Useful for debugging or forwarding steering to sub-agents.
    """
    match = re.search(r"<steering>(.*?)</steering>", prompt, re.DOTALL)
    if match:
        return match.group(1).strip()
    return None


def strip_steering_tags(prompt: str) -> str:
    """Remove steering block from prompt.

    Useful when forwarding prompts to sub-agents that shouldn't
    receive steering intended only for the parent workflow.
    """
    return re.sub(r"\s*<steering>.*?</steering>\s*", "\n", prompt, flags=re.DOTALL)
