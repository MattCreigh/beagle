"""A2A Agent Card Builder — Auto-generate AgentCards from agents.toml.

Phase 5 companion: reads agents.toml and generates A2A AgentCard
objects for each Beagle agent profile so they can be discovered
by external frameworks via the /a2a/discover endpoint.

No changes to agents.toml required — cards are generated from
existing profile metadata.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib  # type: ignore[assignment,no-redef]
    except ImportError:
        tomllib = None  # type: ignore[assignment]

from .a2a_types import AgentCard
from .config import get_a2a_config

logger = logging.getLogger("Beagle.bridges.a2a_card_builder")


def _find_agents_toml() -> Path | None:
    """Find the agents.toml configuration file."""
    # v1.1.1 (S5): agents.toml moved to the canonical config root.
    try:
        from ..config._config_path import find_agents_toml

        p = find_agents_toml()
        return p if p.is_file() else None
    except Exception as exc:  # ruff: ignore[BLE001]  # broad catch intentional
        logger.debug(f"Could not locate agents.toml via canonical resolver: {exc}")

    # Package-adjacent
    try:
        import beagle.config.agent_config as ac

        pkg_dir = Path(ac.__file__).parent
        candidate = pkg_dir / "agents.toml"
        if candidate.exists():
            return candidate
    except Exception as exc:  # ruff: ignore[BLE001]  # broad catch intentional
        logger.debug(f"Could not locate agents.toml via package path: {exc}")

    logger.warning("agents.toml not found — no A2A agent cards will be generated")
    return None


def _load_agents_toml() -> dict[str, Any]:
    """Load and parse agents.toml."""
    path = _find_agents_toml()
    if path is None:
        logger.warning("agents.toml not found — A2A discovery will return empty agent list")
        return {}

    if tomllib is None:
        logger.warning("No TOML parser available — cannot read agents.toml")
        return {}

    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except Exception as exc:  # ruff: ignore[BLE001]  # broad catch intentional
        logger.error(f"Failed to load agents.toml: {exc}")
        return {}


def _infer_capabilities(profile: dict[str, Any]) -> list[str]:
    """Infer A2A capabilities from an agent profile.

    Maps profile fields to A2A capability strings.
    """
    capabilities: list[str] = []

    # All Beagle agents can execute workflows
    capabilities.append("execute_workflow")

    # Check for specific capabilities from profile
    mode = profile.get("mode", "")
    if mode in ("develop", "code"):
        capabilities.append("code_generation")
        capabilities.append("file_modification")
    elif mode in ("audit", "research"):
        capabilities.append("read_only_analysis")

    # Model capabilities
    model = profile.get("model", "")
    if model:
        capabilities.append(f"model:{model}")

    # Tool usage
    tools = profile.get("tools", [])
    if tools:
        capabilities.append("tool_use")
        for tool in tools[:5]:  # Limit to 5 tool capabilities
            if isinstance(tool, str):
                capabilities.append(f"tool:{tool}")

    return capabilities


def _infer_input_schema(profile: dict[str, Any]) -> dict[str, Any]:
    """Generate a JSON Schema for the agent's expected input.

    Beagle agents universally accept a 'query' string.
    Some also accept steering prompts and workflow config.
    """
    return {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The task or query to process",
            },
            "steering_prompt": {
                "type": "string",
                "description": "Optional high-priority directive injected into the agent",
            },
        },
        "required": ["query"],
    }


def _infer_output_schema(profile: dict[str, Any]) -> dict[str, Any]:
    """Generate a JSON Schema for the agent's output.

    Beagle agents return structured reports with findings.
    """
    return {
        "type": "object",
        "properties": {
            "final_report": {
                "type": "string",
                "description": "The complete report or analysis from the agent",
            },
            "completed_nodes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of completed workflow phases",
            },
            "errors": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of error messages (empty if successful)",
            },
        },
    }


def build_agent_cards() -> list:
    """Build A2A AgentCards from agents.toml.

    Reads all agent profiles and auto-generates an AgentCard
    for each one. No modifications to agents.toml needed.

    Returns:
        List of AgentCard objects.

    """
    a2a_config = get_a2a_config()
    base_url = f"http://{a2a_config.bind_address}:{a2a_config.port}"

    raw = _load_agents_toml()
    if not raw:
        logger.warning("No agent profiles found in agents.toml")
        return []

    cards: list[AgentCard] = []

    # agents.toml structure: [profiles.PROFILE_NAME] sections
    profiles = raw.get("profiles", {})
    if not profiles:
        # Try flat structure: [agent_name] sections at root
        for key, value in raw.items():
            if isinstance(value, dict) and "model" in value:
                profiles[key] = value

    for name, profile in profiles.items():
        if not isinstance(profile, dict):
            continue

        card = AgentCard(
            name=name,
            description=profile.get("description", f"Beagle agent: {name}"),
            version="1.0.0",
            capabilities=_infer_capabilities(profile),
            input_schema=_infer_input_schema(profile),
            output_schema=_infer_output_schema(profile),
            endpoint_url=f"{base_url}/a2a/execute",
        )
        cards.append(card)

    logger.info(f"Built {len(cards)} A2A AgentCards from agents.toml")
    return cards
