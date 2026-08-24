"""Context Manifest Pattern for Intent-Based Context Hydration.

Parses `.goose/context-manifest.json` files to automatically load
documentation, skills, and structural templates based on workflow
skill_name and query intent.

This enables hierarchical context management:
1. Global constraints (survive all compactions)
2. Project manifest (survives session restarts)
3. Session context (survives within session)
4. Working context (can be compacted)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config.paths import get_workspace_root

logger = logging.getLogger("Beagle.context_manifest")


@dataclass
class SkillManifest:
    """Manifest entry for a specific skill/agent.

    Attributes:
        skill_name: Name of the skill (matches recipe name)
        docs: List of documentation paths to load
        skills: List of skill file paths to load
        context_templates: Template paths to inject into context
        constraints: Constraints to apply when this skill is used
        rag_queries: RAG queries to run before execution
        priority: Loading priority (lower = higher priority)

    """

    skill_name: str
    docs: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    context_templates: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    rag_queries: list[str] = field(default_factory=list)
    priority: int = 50

    def to_json(self) -> dict[str, Any]:
        """Serialize to JSON."""
        return {
            "skill_name": self.skill_name,
            "docs": self.docs,
            "skills": self.skills,
            "context_templates": self.context_templates,
            "constraints": self.constraints,
            "rag_queries": self.rag_queries,
            "priority": self.priority,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> SkillManifest:
        """Deserialize from JSON."""
        return cls(
            skill_name=data.get("skill_name", ""),
            docs=data.get("docs", []),
            skills=data.get("skills", []),
            context_templates=data.get("context_templates", []),
            constraints=data.get("constraints", []),
            rag_queries=data.get("rag_queries", []),
            priority=data.get("priority", 50),
        )


@dataclass
class ContextManifest:
    """Full context manifest for a project/workspace.

    The manifest defines which files to load for different skills,
    enabling automatic context hydration based on workflow intent.

    File Location:
        `.goose/context-manifest.json` in the project root

    Example Structure:
        {
          "version": "1.0",
          "project": "my-project",
          "global_docs": ["docs/ARCHITECTURE.md"],
          "global_skills": ["skills/traefik-debug.xml"],
          "skills": {
            "research-planner": {
              "docs": ["docs/RESEARCH_GUIDE.md"],
              "rag_queries": ["architecture documentation"]
            },
            "sota-dev": {
              "docs": ["docs/CODING_STANDARDS.md", "CLAUDE.md"],
              "skills": ["skills/rust-patterns.xml"],
              "constraints": ["NO Docker socket", "Use type hints"]
            }
          }
        }
    """

    version: str = "1.0"
    project: str = ""
    global_docs: list[str] = field(default_factory=list)
    global_skills: list[str] = field(default_factory=list)
    global_constraints: list[str] = field(default_factory=list)
    skills: dict[str, SkillManifest] = field(default_factory=dict)
    _path: Path | None = field(default=None, repr=False)

    def to_json(self) -> dict[str, Any]:
        """Serialize to JSON."""
        return {
            "version": self.version,
            "project": self.project,
            "global_docs": self.global_docs,
            "global_skills": self.global_skills,
            "global_constraints": self.global_constraints,
            "skills": {name: manifest.to_json() for name, manifest in self.skills.items()},
        }

    @classmethod
    def from_json(cls, data: dict[str, Any], path: Path | None = None) -> ContextManifest:
        """Deserialize from JSON."""
        skills = {}
        for name, skill_data in data.get("skills", {}).items():
            skills[name] = SkillManifest.from_json(skill_data)

        return cls(
            version=data.get("version", "1.0"),
            project=data.get("project", ""),
            global_docs=data.get("global_docs", []),
            global_skills=data.get("global_skills", []),
            global_constraints=data.get("global_constraints", []),
            skills=skills,
            _path=path,
        )

    def get_skill_manifest(self, skill_name: str) -> SkillManifest | None:
        """Get manifest for a specific skill.

        Args:
            skill_name: Name of the skill to look up

        Returns:
            SkillManifest if found, None otherwise

        """
        return self.skills.get(skill_name)

    def get_all_docs_for_skill(self, skill_name: str) -> list[str]:
        """Get all documentation paths for a skill.

        Combines global docs with skill-specific docs.

        Args:
            skill_name: Name of the skill

        Returns:
            List of documentation paths to load

        """
        docs = list(self.global_docs)
        skill_manifest = self.skills.get(skill_name)
        if skill_manifest:
            docs.extend(skill_manifest.docs)
        return docs

    def get_all_skills_for_intent(self, skill_name: str) -> list[str]:
        """Get all skill file paths for a skill.

        Combines global skills with skill-specific skills.

        Args:
            skill_name: Name of the skill

        Returns:
            List of skill file paths to load

        """
        skills = list(self.global_skills)
        skill_manifest = self.skills.get(skill_name)
        if skill_manifest:
            skills.extend(skill_manifest.skills)
        return skills

    def get_all_constraints_for_skill(self, skill_name: str) -> list[str]:
        """Get all constraints for a skill.

        Combines global constraints with skill-specific constraints.

        Args:
            skill_name: Name of the skill

        Returns:
            List of constraint strings

        """
        constraints = list(self.global_constraints)
        skill_manifest = self.skills.get(skill_name)
        if skill_manifest:
            constraints.extend(skill_manifest.constraints)
        # Deduplicate while preserving order
        seen = set()
        unique = []
        for c in constraints:
            c_normalized = c.lower().strip()
            if c_normalized not in seen:
                seen.add(c_normalized)
                unique.append(c)
        return unique


# ── Manifest Loading ──────────────────────────────────────────────────────────


def discover_manifest_path(project_dir: Path | None = None) -> Path | None:
    """Discover the context manifest file for a project.

    Searches in order:
    1. Project directory / .goose / context-manifest.json
    2. Current working directory / .goose / context-manifest.json
    3. Workspace root / .goose / context-manifest.json

    Args:
        project_dir: Optional explicit project directory

    Returns:
        Path to manifest file, or None if not found

    """
    search_dirs = []

    if project_dir:
        search_dirs.append(Path(project_dir))

    search_dirs.append(Path.cwd())

    try:
        workspace = get_workspace_root()
        search_dirs.append(workspace)
    except (OSError, RuntimeError, ValueError, AttributeError) as exc:
        logger.warning(
            "Cannot resolve the workspace root (%s); it is excluded from the manifest "
            "search path, so a manifest stored there will not be found.",
            exc,
        )

    for search_dir in search_dirs:
        manifest_path = search_dir / ".goose" / "context-manifest.json"
        if manifest_path.exists():
            # Resolve symlinks for security
            try:
                resolved = manifest_path.resolve()
                search_dir_resolved = search_dir.resolve()
                # Ensure path is within expected directory.
                # ``Path.relative_to`` raises ``ValueError`` if ``resolved`` is
                # outside ``search_dir_resolved`` (audit S3, v13.17.0) — this
                # is the correct containment primitive and supersedes the prior
                # ``str.startswith`` check, which had a symlink-bypass vector.
                try:
                    resolved.relative_to(search_dir_resolved)
                except ValueError:
                    logger.warning(f"Manifest path outside project: {resolved}")
                    continue
                return resolved
            except (OSError, RuntimeError) as e:
                logger.warning(f"Error resolving manifest path: {e}")
                continue

    return None


def load_manifest(project_dir: Path | None = None) -> ContextManifest | None:
    """Load context manifest from file.

    Args:
        project_dir: Optional project directory to search

    Returns:
        ContextManifest if found and valid, None otherwise

    """
    manifest_path = discover_manifest_path(project_dir)

    if not manifest_path:
        logger.debug("No context manifest found")
        return None

    try:
        with open(manifest_path, encoding="utf-8") as f:
            data = json.load(f)

        manifest = ContextManifest.from_json(data, path=manifest_path)
        logger.info(f"Loaded context manifest from {manifest_path}")
        return manifest

    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON in manifest {manifest_path}: {e}")
        return None
    except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
        logger.error(f"Error loading manifest {manifest_path}: {e}")
        return None


def load_manifest_content(
    manifest: ContextManifest,
    skill_name: str,
    max_tokens: int = 10000,
) -> dict[str, str]:
    """Load content from manifest for a specific skill.

    Reads documentation and skill files, combining them into
    a dictionary of content sections.

    Args:
        manifest: ContextManifest to load from
        skill_name: Skill to load content for
        max_tokens: Maximum tokens per file (approximate)

    Returns:
        Dictionary mapping file names to content strings

    """
    content: dict[str, str] = {}

    # Determine base directory
    base_dir = manifest._path.parent.parent if manifest._path else get_workspace_root()

    # Load docs
    docs = manifest.get_all_docs_for_skill(skill_name)
    for doc_path in docs:
        full_path = base_dir / doc_path
        try:
            if full_path.exists():
                text = full_path.read_text(encoding="utf-8")
                # Truncate if too large
                if len(text) > max_tokens * 4:  # ~4 chars per token
                    text = text[: max_tokens * 4] + "\n... [TRUNCATED]"
                content[f"doc:{doc_path}"] = text
                logger.debug(f"Loaded doc: {doc_path} ({len(text)} chars)")
        except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
            logger.warning(f"Failed to load doc {doc_path}: {e}")

    # Load skills
    skills = manifest.get_all_skills_for_intent(skill_name)
    for skill_path in skills:
        full_path = base_dir / skill_path
        try:
            if full_path.exists():
                text = full_path.read_text(encoding="utf-8")
                if len(text) > max_tokens * 4:
                    text = text[: max_tokens * 4] + "\n... [TRUNCATED]"
                content[f"skill:{skill_path}"] = text
                logger.debug(f"Loaded skill: {skill_path} ({len(text)} chars)")
        except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
            logger.warning(f"Failed to load skill {skill_path}: {e}")

    return content


def build_structural_template(
    manifest: ContextManifest | None,
    skill_name: str,
    project_context: str = "",
    global_context: str = "",
) -> str:
    """Build structural template for injection into agent context.

    Combines manifest-derived content with existing context to create
    a unified structural template.

    Args:
        manifest: ContextManifest (may be None)
        skill_name: Name of the skill being executed
        project_context: Project-specific context (e.g., from CLAUDE.md)
        global_context: Global context (e.g., from standards.md)

    Returns:
        Formatted structural template string

    """
    sections: list[str] = []

    # Start with global context
    if global_context:
        sections.append(f"<global_standards>\n{global_context}\n</global_standards>")

    # Add project context
    if project_context:
        sections.append(f"<project_context>\n{project_context}\n</project_context>")

    # Add manifest-derived content
    if manifest:
        # Constraints
        constraints = manifest.get_all_constraints_for_skill(skill_name)
        if constraints:
            sections.append("<active_constraints>\n")
            sections.append("The following constraints MUST be respected:\n")
            for i, constraint in enumerate(constraints, 1):
                sections.append(f"{i}. {constraint}\n")
            sections.append("</active_constraints>\n")

        # Documentation pointers
        docs = manifest.get_all_docs_for_skill(skill_name)
        if docs:
            sections.append("<required_reading>\n")
            sections.append("The following documentation files should be read:\n")
            for doc in docs:
                sections.append(f"- {doc}\n")
            sections.append("</required_reading>\n")

        # Skill-specific pointers
        skills = manifest.get_all_skills_for_intent(skill_name)
        if skills:
            sections.append("<skill_files>\n")
            sections.append("The following skill files are available:\n")
            for skill in skills:
                sections.append(f"- {skill}\n")
            sections.append("</skill_files>\n")

        # RAG queries to run
        skill_manifest = manifest.get_skill_manifest(skill_name)
        if skill_manifest and skill_manifest.rag_queries:
            sections.append("<rag_queries>\n")
            sections.append("Consider running these RAG queries for context:\n")
            for query in skill_manifest.rag_queries:
                sections.append(f"- {query}\n")
            sections.append("</rag_queries>\n")

    if not sections:
        return ""

    return "\n".join(sections)


# ── Module-level cache ────────────────────────────────────────────────────────

_manifest_cache: dict[str, ContextManifest] = {}
_cache_lock: bool = False


def get_manifest(project_dir: Path | None = None, use_cache: bool = True) -> ContextManifest | None:
    """Get manifest with caching.

    Loads manifest once and caches for subsequent calls.

    Args:
        project_dir: Optional project directory
        use_cache: Whether to use cached manifest

    Returns:
        ContextManifest if available, None otherwise

    """
    global _manifest_cache

    cache_key = str(project_dir) if project_dir else "default"

    if use_cache and cache_key in _manifest_cache:
        return _manifest_cache[cache_key]

    manifest = load_manifest(project_dir)

    if manifest:
        _manifest_cache[cache_key] = manifest

    return manifest


def clear_manifest_cache() -> None:
    """Clear the manifest cache."""
    global _manifest_cache
    _manifest_cache = {}


# ── Default Manifest Generation ────────────────────────────────────────────────


def create_default_manifest(project_name: str = "project") -> ContextManifest:
    """Create a default manifest for a project.

    Provides sensible defaults for common workflows.

    Args:
        project_name: Name of the project

    Returns:
        Default ContextManifest

    """
    return ContextManifest(
        version="1.0",
        project=project_name,
        global_docs=[
            "CLAUDE.md",
            "GOOSE.md",
        ],
        global_skills=[],
        global_constraints=[
            "All code must be type-annotated",
            "Follow existing patterns in the codebase",
            "Write tests for new functionality",
        ],
        skills={
            "research-planner": SkillManifest(
                skill_name="research-planner",
                docs=["docs/RESEARCH_GUIDE.md"],
                rag_queries=["architecture", "documentation"],
                priority=30,
            ),
            "search-executor": SkillManifest(
                skill_name="search-executor",
                rag_queries=["implementation", "examples"],
                priority=40,
            ),
            "fact-checker": SkillManifest(
                skill_name="fact-checker",
                constraints=["Verify all claims with sources"],
                priority=50,
            ),
            "synthesis-writer": SkillManifest(
                skill_name="synthesis-writer",
                docs=["docs/WRITING_STYLE.md"],
                constraints=["Cite all sources", "Use clear structure"],
                priority=60,
            ),
            "sota-dev": SkillManifest(
                skill_name="sota-dev",
                docs=["docs/CODING_STANDARDS.md"],
                skills=["skills/code-write.xml"],
                constraints=[
                    "NO Docker socket mounts",
                    "Use type hints in all functions",
                    "Handle errors explicitly",
                ],
                priority=20,
            ),
            "self-improver": SkillManifest(
                skill_name="self-improver",
                docs=["docs/SELF_IMPROVEMENT_GUIDE.md"],
                rag_queries=["improvement", "optimization"],
                priority=70,
            ),
        },
    )


def save_manifest(manifest: ContextManifest, path: Path) -> None:
    """Save manifest to file.

    Args:
        manifest: Manifest to save
        path: Path to save to (should be .goose/context-manifest.json)

    """
    # Ensure directory exists
    path.parent.mkdir(parents=True, exist_ok=True)

    # Write atomically
    temp_path = path.with_suffix(".tmp")
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(manifest.to_json(), f, indent=2)
        temp_path.replace(path)
        logger.info(f"Saved manifest to {path}")
    finally:
        # Clean up temp file if it exists
        if temp_path.exists():
            temp_path.unlink()


if __name__ == "__main__":
    # Demo: Create and save a default manifest
    import sys

    project = sys.argv[1] if len(sys.argv) > 1 else "my-project"
    manifest = create_default_manifest(project)

    logger.info("Default Manifest:")
    logger.info(json.dumps(manifest.to_json(), indent=2))
