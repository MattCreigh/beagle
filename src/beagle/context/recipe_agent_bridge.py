"""Recipe-to-Agent Discovery Bridge — Auto-creates agent profiles from recipes.

When goose initializes Beagle, this module scans the recipes/ directory
and ensures every recipe has a corresponding entry in agents.toml.
This allows goose to discover and use Beagle agents through the
standard get_agent() interface.

The bridge is called:
1. On goose session start (via on_session_start)
2. When listing agents (via list_agents)
3. When the CLI is initialized

Recipes that already exist in agents.toml are left unchanged.
New recipes get a profile with:
- model: resolved from recipe metadata or default
- temperature: 0.4 (standard) or 0.1 (verification/audit)
- description: extracted from recipe frontmatter or filename
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("Beagle.recipe_agent_bridge")


@dataclass
class RecipeAgentProfile:
    """Agent profile derived from a recipe file."""

    name: str
    description: str
    provider: str = "ollama_cloud"
    model: str = "minimax-m3:cloud"  # overwritten by _infer_model_for_phase
    temperature: float = 0.4
    recipe_path: Path = field(default_factory=Path)
    phase: str = "unknown"  # planning / execution / verification / synthesis

    def to_toml_section(self) -> str:
        """Generate a TOML section for this agent profile."""
        lines = [
            f"[{self.name}]",
            f'provider = "{self.provider}"',
            f'model = "{self.model}"',
            f"temperature = {self.temperature}",
            f'description = "{self.description}"',
        ]
        return "\n".join(lines)


def _infer_phase_from_name(name: str) -> str:
    """Infer workflow phase from recipe/agent name."""
    name_lower = name.lower()
    if any(kw in name_lower for kw in ("plan", "research", "deep-plan", "architect", "strategist")):
        return "planning"
    if any(kw in name_lower for kw in ("verif", "check", "audit", "valid", "fact", "ground-truth")):
        return "verification"
    if any(kw in name_lower for kw in ("synth", "report", "summar", "writ", "doc")):
        return "synthesis"
    if any(
        kw in name_lower for kw in ("execut", "search", "cod", "implement", "dev", "fix", "sota")
    ):
        return "execution"
    return "unknown"


def _infer_model_for_phase(phase: str) -> str:
    """Select appropriate model based on workflow phase.

    v13.22.4: Reads from config.toml [model_presets] instead of hardcoding
    model names. Falls back to the default preset if config is unavailable.

    v1.0.0: the TOML read is delegated to
    :func:`beagle.config.model_resolver.get_preset` — this function owns the
    phase→preset *mapping*, not the config access, so the last-resort model
    name lives in exactly one place.
    """
    from beagle.config.model_resolver import get_preset

    phase_to_preset = {
        "planning": "orchestration",
        "execution": "default",
        "verification": "fact_checking",
        "synthesis": "writing",
        "unknown": "default",
    }
    return get_preset(phase_to_preset.get(phase, "default"))


def _infer_temperature_for_phase(phase: str) -> float:
    """Select appropriate temperature based on workflow phase."""
    phase_temps = {
        "planning": 0.3,
        "execution": 0.2,
        "verification": 0.1,
        "synthesis": 0.5,
        "unknown": 0.4,
    }
    return phase_temps.get(phase, 0.4)


def _extract_description(content: str, filename: str) -> str:
    """Extract description from recipe content.

    Tries YAML frontmatter first, then first markdown heading,
    then first paragraph, then falls back to filename.
    """
    # Try YAML frontmatter
    if content.startswith("---"):
        try:
            end = content.index("---", 3)
            frontmatter = content[3:end]
            # Simple extraction — look for description: line
            for line in frontmatter.splitlines():
                line = line.strip()
                if line.lower().startswith("description:"):
                    desc = line.split(":", 1)[1].strip().strip('"').strip("'")
                    if desc:
                        return desc
        except ValueError as exc:
            logger.warning(
                "Cannot parse the recipe front matter for a description (%s); falling "
                "back to the first markdown heading.",
                exc,
            )

    # Try first markdown heading content (## Description or first #)
    in_description = False
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("## description"):
            in_description = True
            continue
        if in_description:
            if stripped.startswith("## ") or stripped.startswith("# "):
                break
            if stripped and not stripped.startswith("#"):
                return stripped[:200]

    # Try first non-empty, non-heading paragraph
    for line in content.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and len(stripped) > 10:
            return stripped[:200]

    # Fallback: clean up filename
    name = (
        filename.replace("-", " ")
        .replace("_", " ")
        .replace(".md", "")
        .replace(".yaml", "")
        .replace(".xml", "")
    )
    return f"Agent: {name}"


def scan_recipes(recipes_dir: Path | None = None) -> list[RecipeAgentProfile]:
    """Scan the recipes directory and build agent profiles.

    Args:
        recipes_dir: Path to recipes directory. Auto-detected if None.

    Returns:
        List of RecipeAgentProfile objects.

    """
    if recipes_dir is None:
        # v1.1.1 (S5): recipes moved to the canonical config root; resolve
        # them through find_recipes_dir().
        from ..config._config_path import find_recipes_dir

        recipes_dir = find_recipes_dir()
        if not recipes_dir.is_dir():
            logger.warning("Cannot resolve recipes directory for recipe scan")
            return []

    if not recipes_dir.exists():
        logger.warning(f"Recipes directory not found: {recipes_dir}")
        return []

    profiles: list[RecipeAgentProfile] = []

    # Scan both .xml and .yaml recipes
    for recipe_path in sorted(recipes_dir.glob("*.xml")):
        try:
            content = recipe_path.read_text(encoding="utf-8")
            name = recipe_path.stem
            description = _extract_description(content, name)
            phase = _infer_phase_from_name(name)
            model = _infer_model_for_phase(phase)
            temperature = _infer_temperature_for_phase(phase)

            profiles.append(
                RecipeAgentProfile(
                    name=name,
                    description=description,
                    model=model,
                    temperature=temperature,
                    recipe_path=recipe_path,
                    phase=phase,
                )
            )
        except (OSError, ValueError, KeyError, TypeError) as e:
            logger.debug(f"Failed to parse recipe {recipe_path}: {e}")

    logger.info(f"Scanned {len(profiles)} recipes from {recipes_dir}")
    return profiles


def load_existing_agent_names(agents_toml_path: Path | None = None) -> set[str]:
    """Load existing agent profile names from agents.toml.

    Args:
        agents_toml_path: Path to agents.toml. Auto-detected if None.

    Returns:
        Set of existing agent profile names.

    """
    if agents_toml_path is None:
        # v1.1.1 (S5): agents.toml moved to the canonical config root.
        from ..config._config_path import find_agents_toml

        agents_toml_path = find_agents_toml()

    if not agents_toml_path.exists():
        return set()

    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib  # type: ignore[no-redef]

    try:
        with open(agents_toml_path, "rb") as f:
            data = tomllib.load(f)
        return {k for k, v in data.items() if isinstance(v, dict)}
    except (OSError, ValueError, KeyError, TypeError, ImportError) as e:
        logger.error(f"Failed to parse agents.toml: {e}")
        return set()


def sync_recipes_to_agents_toml(
    recipes_dir: Path | None = None,
    agents_toml_path: Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Synchronize recipes to agents.toml, adding missing agent profiles.

    This is the main entry point for recipe-to-agent discovery.
    It reads all recipes, compares against existing agents.toml,
    and appends any new agent profiles.

    Args:
        recipes_dir: Path to recipes directory.
        agents_toml_path: Path to agents.toml.
        dry_run: If True, don't write changes — just report what would change.

    Returns:
        Dict with stats: added, existing, skipped, total_recipes.

    """
    if agents_toml_path is None:
        # v1.1.1 (S5): agents.toml moved to the canonical config root.
        from ..config._config_path import find_agents_toml

        agents_toml_path = find_agents_toml()

    # Scan recipes
    profiles = scan_recipes(recipes_dir)
    existing_names = load_existing_agent_names(agents_toml_path)

    # Filter to only new profiles
    new_profiles = [p for p in profiles if p.name not in existing_names]

    stats = {
        "total_recipes": len(profiles),
        "existing": len(profiles) - len(new_profiles),
        "added": len(new_profiles),
        "skipped": 0,
        "new_agents": [p.name for p in new_profiles],
    }

    if not new_profiles:
        logger.info(f"All {len(profiles)} recipes already registered in agents.toml")
        return stats

    if dry_run:
        logger.info(
            f"[DRY RUN] Would add {len(new_profiles)} agent profiles: "
            f"{[p.name for p in new_profiles]}"
        )
        return stats

    # Append new profiles to agents.toml
    try:
        # Read existing content
        if agents_toml_path.exists():
            existing_content = agents_toml_path.read_text(encoding="utf-8")
        else:
            existing_content = "# Beagle Agent Profile Configuration\n# Auto-generated + manually curated profiles\n"

        # Build new sections
        new_sections: list[str] = []
        for profile in sorted(new_profiles, key=lambda p: p.name):
            # Group by phase for readability
            section = f"\n\n# Phase: {profile.phase}\n{profile.to_toml_section()}"
            new_sections.append(section)

        # Append to file
        addition = "\n".join(new_sections)
        agents_toml_path.write_text(
            existing_content.rstrip() + "\n" + addition + "\n",
            encoding="utf-8",
        )

        logger.info(
            f"Added {len(new_profiles)} agent profiles to agents.toml: "
            f"{[p.name for p in new_profiles]}"
        )

        # Invalidate the agent_config cache so new profiles are picked up
        try:
            from ..config.agent_config import invalidate_cache

            invalidate_cache()
        except ImportError as exc:
            logger.warning(
                "Cannot invalidate the agent-config cache (%s); newly written agent "
                "profiles will not be visible until the process restarts.",
                exc,
            )

    except (OSError, ValueError, KeyError, TypeError, ImportError) as e:
        logger.error(f"Failed to update agents.toml: {e}")
        stats["skipped"] = len(new_profiles)
        stats["added"] = 0

    return stats


def get_recipe_agent_profiles() -> dict[str, RecipeAgentProfile]:
    """Get all recipe-derived agent profiles as a dict.

    This is the convenience function for listing all available agents,
    combining both agents.toml profiles and recipe-discovered profiles.

    Returns:
        Dict mapping agent name to RecipeAgentProfile.

    """
    profiles = scan_recipes()
    return {p.name: p for p in profiles}


# ── Session integration ────────────────────────────────────────────────────────


def on_beagle_init() -> dict[str, Any]:
    """Called when Beagle initializes (goose session start).

    Ensures all recipes are registered as agents and returns
    a summary of available agents.

    Returns:
        Dict with agent discovery stats and available agents list.

    """
    # Sync recipes to agents.toml
    sync_result = sync_recipes_to_agents_toml()

    # Get all available agents (from agents.toml including newly synced)
    try:
        from ..config.agent_config import list_agents

        agents = list_agents()
    except (ImportError, RuntimeError, OSError):
        agents = {}

    # Get recipe profiles for supplementary info
    recipe_profiles = get_recipe_agent_profiles()

    # Build agent list for goose
    agent_list = []
    for name, profile in agents.items():
        phase = recipe_profiles[name].phase if name in recipe_profiles else "unknown"
        agent_list.append(
            {
                "name": name,
                "model": profile.model,
                "provider": profile.provider,
                "description": profile.description,
                "phase": phase,
            }
        )

    result = {
        **sync_result,
        "total_agents": len(agent_list),
        "agents": agent_list,
    }

    logger.info(
        f"[Beagle Init] Recipe→Agent sync: {sync_result['added']} added, "
        f"{sync_result['existing']} existing, {len(agent_list)} total agents available"
    )

    return result


if __name__ == "__main__":
    import json

    result = on_beagle_init()
    logger.info(json.dumps(result, indent=2))
