"""Skill Library for Memento-Skills inspired agent improvement.

This module implements:
- Stateful skill storage as structured XML
- Skill router for behavior-based selection
- Read-Write Reflective Learning mechanism

Based on "Memento-Skills: Let Agents Design Agents" (arXiv:2603.18743)
https://arxiv.org/abs/2603.18743
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("Beagle.skill_library")


# Canonical workspace root — delegates to env_manager for consistency
def _get_skills_dir() -> Path:
    from ..utils.env_manager import get_workspace_root

    return get_workspace_root() / "skills"


SKILLS_DIR = _get_skills_dir()
SKILL_ROUTER_MODEL = "minimax-m3:cloud"


@dataclass
class SkillMetadata:
    """Metadata for a stored skill."""

    name: str
    description: str
    created_at: float
    updated_at: float
    use_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    avg_execution_time: float = 0.0
    tags: list[str] = field(default_factory=list)
    trigger_conditions: list[str] = field(default_factory=list)
    skill_hash: str = ""


@dataclass
class Skill:
    """A reusable skill stored as structured XML."""

    metadata: SkillMetadata
    prompt_template: str
    system_directive: str = ""
    examples: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)

    def to_xml(self) -> str:
        """Convert skill to XML format for storage.

        Returns:
            A well-formed XML document with a ``<skill>`` root carrying the
            skill's name, description, metadata, system directive, prompt
            template, examples and dependencies.

        """
        from defusedxml import ElementTree as ET

        root = ET.Element("skill", {"name": self.metadata.name})
        ET.SubElement(root, "description").text = self.metadata.description

        meta = ET.SubElement(root, "metadata")
        ET.SubElement(meta, "created_at").text = str(self.metadata.created_at)
        ET.SubElement(meta, "updated_at").text = str(self.metadata.updated_at)
        ET.SubElement(meta, "use_count").text = str(self.metadata.use_count)
        ET.SubElement(meta, "success_count").text = str(self.metadata.success_count)
        ET.SubElement(meta, "failure_count").text = str(self.metadata.failure_count)
        ET.SubElement(meta, "avg_execution_time").text = str(self.metadata.avg_execution_time)
        ET.SubElement(meta, "tags").text = ", ".join(self.metadata.tags)
        ET.SubElement(meta, "trigger_conditions").text = ", ".join(self.metadata.trigger_conditions)
        ET.SubElement(meta, "skill_hash").text = self.metadata.skill_hash

        ET.SubElement(root, "system_directive").text = self.system_directive or "(none)"
        ET.SubElement(root, "prompt_template").text = self.prompt_template

        if self.examples:
            examples = ET.SubElement(root, "examples")
            for ex in self.examples:
                ET.SubElement(examples, "example").text = ex
        if self.dependencies:
            deps = ET.SubElement(root, "dependencies")
            for d in self.dependencies:
                ET.SubElement(deps, "dependency").text = d

        ET.indent(root, space="  ")
        text = ET.tostring(root, encoding="unicode", xml_declaration=True)
        assert isinstance(text, str)  # encoding="unicode" ⇒ str (typeshed: str | bytes)
        return text

    @classmethod
    def from_xml(cls, xml_text: str, name: str) -> Skill:
        """Parse a skill from XML format.

        Args:
            xml_text: The XML document produced by ``to_xml``.
            name: The skill name (used as a fallback when the root lacks it).

        Returns:
            A Skill parsed from the XML.

        Raises:
            ValueError: When the XML is malformed or lacks a ``<skill>`` root.

        """
        from defusedxml import ElementTree as ET

        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            raise ValueError(f"malformed skill XML: {exc}") from exc
        if root.tag != "skill":
            raise ValueError(f"expected <skill> root, got <{root.tag}>")

        skill_name = root.get("name") or name

        def _text(tag: str) -> str:
            el = root.find(tag)
            return (el.text or "").strip() if el is not None else ""

        meta_el = root.find("metadata")
        created_at = time.time()
        updated_at = time.time()
        use_count = 0
        success_count = 0
        failure_count = 0
        avg_execution_time = 0.0
        tags: list[str] = []
        trigger_conditions: list[str] = []
        skill_hash = ""
        if meta_el is not None:

            def _meta(tag: str) -> str:
                el = meta_el.find(tag)
                return (el.text or "").strip() if el is not None else ""

            with contextlib.suppress(ValueError):
                created_at = float(_meta("created_at") or created_at)
            with contextlib.suppress(ValueError):
                updated_at = float(_meta("updated_at") or updated_at)
            with contextlib.suppress(ValueError):
                use_count = int(_meta("use_count") or 0)
            with contextlib.suppress(ValueError):
                success_count = int(_meta("success_count") or 0)
            with contextlib.suppress(ValueError):
                failure_count = int(_meta("failure_count") or 0)
            with contextlib.suppress(ValueError):
                avg_execution_time = float(_meta("avg_execution_time") or 0.0)
            tags = [t.strip() for t in _meta("tags").split(",") if t.strip()]
            trigger_conditions = [
                t.strip() for t in _meta("trigger_conditions").split(",") if t.strip()
            ]
            skill_hash = _meta("skill_hash")

        examples: list[str] = []
        examples_el = root.find("examples")
        if examples_el is not None:
            for ex in examples_el.findall("example"):
                if ex.text:
                    examples.append(ex.text.strip())

        dependencies: list[str] = []
        deps_el = root.find("dependencies")
        if deps_el is not None:
            for d in deps_el.findall("dependency"):
                if d.text:
                    dependencies.append(d.text.strip())

        return cls(
            metadata=SkillMetadata(
                name=skill_name,
                description=_text("description"),
                created_at=created_at,
                updated_at=updated_at,
                use_count=use_count,
                success_count=success_count,
                failure_count=failure_count,
                avg_execution_time=avg_execution_time,
                tags=tags,
                trigger_conditions=trigger_conditions,
                skill_hash=skill_hash,
            ),
            prompt_template=_text("prompt_template"),
            system_directive=_text("system_directive"),
            examples=examples,
            dependencies=dependencies,
        )

    @classmethod
    def from_markdown(cls, markdown: str, name: str) -> Skill:
        """Parse skill from markdown format."""
        lines = markdown.split("\n")
        description = []
        system_directive = []
        prompt_template = []
        in_section = None
        tags = []
        trigger_conditions = []
        created_at = time.time()
        updated_at = time.time()
        use_count = 0
        success_count = 0

        for _i, line in enumerate(lines):
            if line.startswith("## Description"):
                in_section = "description"
            elif line.startswith("## Metadata"):
                in_section = "metadata"
            elif line.startswith("## Trigger Conditions"):
                in_section = "triggers"
            elif line.startswith("## System Directive"):
                in_section = "system"
            elif line.startswith("## Prompt Template"):
                in_section = "prompt"
            elif line.startswith("## Examples"):
                in_section = "examples"
            elif line.startswith("## Dependencies"):
                in_section = "dependencies"
            elif line.startswith("## "):
                in_section = None
            elif in_section == "description":
                description.append(line)
            elif in_section == "system":
                system_directive.append(line)
            elif in_section == "prompt":
                prompt_template.append(line)
            elif in_section == "metadata":
                if "Tags:" in line:
                    tags = [t.strip() for t in line.split("Tags:")[1].split(",")]
                elif "Use Count:" in line:
                    use_count = int(line.split("Use Count:")[1].strip())
                elif "Success Rate:" in line:
                    success_count = int(
                        use_count * float(line.split("Success Rate:")[1].strip().rstrip("%")) / 100
                    )
            elif in_section == "triggers" and line.strip().startswith("-"):
                trigger_conditions.append(line.strip()[1:].strip())

        return cls(
            metadata=SkillMetadata(
                name=name,
                description="\n".join(description).strip(),
                created_at=created_at,
                updated_at=updated_at,
                use_count=use_count,
                success_count=success_count,
                tags=tags,
                trigger_conditions=trigger_conditions,
            ),
            prompt_template="\n".join(prompt_template).strip(),
            system_directive="\n".join(system_directive).strip(),
        )

    def _success_rate(self) -> float:
        total = self.metadata.success_count + self.metadata.failure_count
        return self.metadata.success_count / total if total > 0 else 0.0


class SkillLibrary:
    """Persistent skill storage with Read-Write Reflective Learning.

    Implements the Memento-Skills approach where:
    - Skills are stored as structured XML files
    - Skills encode both behavior and context
    - Agent improves via Read-Write Reflective Learning
    """

    def __init__(self, skills_dir: Path | None = None):
        self.skills_dir = skills_dir or SKILLS_DIR
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        self._index: dict[str, SkillMetadata] = {}
        self._skills: dict[str, Skill] = {}
        self._lock = asyncio.Lock()
        self._load_index()

    def _load_index(self) -> None:
        """Build the skill index by scanning the skills directory.

        The directory is the single source of truth.  Every ``*.xml`` file in
        ``self.skills_dir`` is parsed and its metadata is entered into the
        index under the file stem.  A file that does not parse is logged and
        skipped; it does not abort the scan.

        ``*.md`` files are also indexed for backward compatibility, but XML
        is preferred: when both ``<stem>.xml`` and ``<stem>.md`` exist, the
        XML file wins and the Markdown file is ignored.

        <invariant>
          The set of keys in ``self._index`` equals the set of ``*.xml`` file
          stems in ``self.skills_dir`` that parse successfully, plus any
          ``*.md`` stems that have no XML twin.  No sidecar index file
          participates.
        </invariant>
        """
        from defusedxml import ElementTree as ET

        self._index = {}
        self._skills = {}
        xml_stems: set[str] = set()
        for path in sorted(self.skills_dir.glob("*.xml")):
            xml_stems.add(path.stem)
            try:
                skill = Skill.from_xml(path.read_text(encoding="utf-8"), path.stem)
            except (OSError, ValueError, ET.ParseError) as exc:
                logger.warning(
                    "Skill %s failed to parse (%s: %s); skipping it",
                    path.name,
                    type(exc).__name__,
                    exc,
                )
                continue
            self._skills[skill.metadata.name] = skill
            self._index[skill.metadata.name] = skill.metadata

        # Backward compatibility: index Markdown skills that have no XML twin.
        for path in sorted(self.skills_dir.glob("*.md")):
            if path.stem in xml_stems:
                continue
            try:
                skill = Skill.from_markdown(path.read_text(encoding="utf-8"), path.stem)
            except (OSError, ValueError) as exc:
                logger.warning(
                    "Skill %s failed to parse (%s: %s); skipping it",
                    path.name,
                    type(exc).__name__,
                    exc,
                )
                continue
            self._skills[skill.metadata.name] = skill
            self._index[skill.metadata.name] = skill.metadata

    async def register_skill(self, skill: Skill) -> None:
        """Register a new skill in the library."""
        async with self._lock:
            # B-11 (audit v13.22.0): use blake2b (non-cryptographic-but-modern)
            # instead of md5. The output length is unchanged at 12 hex chars;
            # the input is unchanged. This is a content-derived identifier
            # only, never a security primitive.
            skill.metadata.skill_hash = hashlib.blake2b(
                (skill.prompt_template + skill.system_directive).encode(),
                digest_size=6,
            ).hexdigest()

            self._skills[skill.metadata.name] = skill
            self._index[skill.metadata.name] = skill.metadata

            skill_path = self.skills_dir / f"{skill.metadata.name}.xml"
            skill_path.write_text(skill.to_xml())

            logger.info(f"Registered skill: {skill.metadata.name}")

    async def get_skill(self, name: str) -> Skill | None:
        """Get a skill by name."""
        if name in self._skills:
            return self._skills[name]

        skill_path = self.skills_dir / f"{name}.xml"
        if skill_path.exists():
            xml_text = skill_path.read_text()
            skill = Skill.from_xml(xml_text, name)
            self._skills[name] = skill
            return skill

        return None

    async def update_skill(self, skill: Skill) -> None:
        """Update an existing skill (Write phase of R-WRL)."""
        async with self._lock:
            skill.metadata.updated_at = time.time()

            old_skill = await self.get_skill(skill.metadata.name)
            if old_skill:
                skill.metadata.created_at = old_skill.metadata.created_at
                skill.metadata.use_count = old_skill.metadata.use_count
                skill.metadata.success_count = old_skill.metadata.success_count
                skill.metadata.failure_count = old_skill.metadata.failure_count
                skill.metadata.avg_execution_time = old_skill.metadata.avg_execution_time

            self._skills[skill.metadata.name] = skill
            self._index[skill.metadata.name] = skill.metadata

            skill_path = self.skills_dir / f"{skill.metadata.name}.xml"
            skill_path.write_text(skill.to_xml())

            logger.info(f"Updated skill: {skill.metadata.name}")

    async def record_execution(self, skill_name: str, success: bool, execution_time: float) -> None:
        """Record skill execution result for learning."""
        async with self._lock:
            if skill_name in self._index:
                meta = self._index[skill_name]
                meta.use_count += 1
                if success:
                    meta.success_count += 1
                else:
                    meta.failure_count += 1

                # Update rolling average
                total_time = meta.avg_execution_time * (meta.use_count - 1)
                meta.avg_execution_time = (total_time + execution_time) / meta.use_count

    async def list_skills(self, tag: str | None = None) -> list[SkillMetadata]:
        """List all skills, optionally filtered by tag."""
        skills = list(self._index.values())
        if tag:
            skills = [s for s in skills if tag in s.tags]
        return sorted(skills, key=lambda s: s.use_count, reverse=True)

    async def search_skills(self, query: str) -> list[SkillMetadata]:
        """Search skills by query (description, tags, triggers)."""
        query_lower = query.lower()
        results = []

        for meta in self._index.values():
            score = 0
            if query_lower in meta.description.lower():
                score += 3
            if any(query_lower in tag.lower() for tag in meta.tags):
                score += 2
            if any(query_lower in cond.lower() for cond in meta.trigger_conditions):
                score += 2
            if query_lower in meta.name.lower():
                score += 5

            if score > 0:
                results.append((score, meta))

        results.sort(key=lambda x: x[0], reverse=True)
        return [meta for _, meta in results]


class SkillRouter:
    """Behavior-trainable skill router for Memento-Skills.

    The Read phase of Read-Write Reflective Learning:
    - Selects most relevant skill based on current state
    - Uses scoring from Memento-Skills paper
    """

    def __init__(self, skill_library: SkillLibrary | None = None):
        self.library = skill_library or SkillLibrary()
        self._routing_cache: dict[str, list[tuple[str, float]]] = {}

    async def select_skills(
        self,
        state: dict[str, Any],
        max_skills: int = 3,
    ) -> list[tuple[Skill, float]]:
        """Select most relevant skills for current state.

        Args:
            state: Current AgentState or BeagleState
            max_skills: Maximum number of skills to return

        Returns:
            List of (skill, relevance_score) tuples

        """
        query = self._build_query_from_state(state)
        scores = await self._score_skills(query, state)

        selected = []
        for skill_name, score in sorted(scores.items(), key=lambda x: x[1], reverse=True)[
            :max_skills
        ]:
            skill = await self.library.get_skill(skill_name)
            if skill:
                selected.append((skill, score))

        return selected

    async def route(
        self,
        state: dict[str, Any],
        confidence_threshold: float = 0.6,
    ) -> tuple[str, float, str]:
        """Route to the best skill, returning confidence and a reason.

        D3: returns a confidence score and the reason the match holds, and
        falls back to the generic path when confidence is below the
        threshold. This prevents routing on embedding distance alone.

        Args:
            state: Current AgentState or BeagleState.
            confidence_threshold: Minimum confidence to accept a route.

        Returns:
            A ``(skill_name, confidence, reason)`` tuple. When no skill
            clears the threshold, ``skill_name`` is ``"generic"``.

        """
        query = self._build_query_from_state(state)
        scores = await self._score_skills(query, state)
        if not scores:
            return "generic", 0.0, "no skill matched the query"

        best_name, best_score = max(scores.items(), key=lambda x: x[1])
        # Normalise the raw score to a 0..1 confidence.
        confidence = min(1.0, best_score / 5.0)
        if confidence < confidence_threshold:
            return (
                "generic",
                confidence,
                f"best skill {best_name!r} confidence {confidence:.2f} below "
                f"threshold {confidence_threshold}",
            )
        return best_name, confidence, f"structural+lexical match on {best_name!r}"

    async def _score_skills(
        self,
        query: str,
        state: dict[str, Any],
    ) -> dict[str, float]:
        """Score all skills for relevance to current state.

        Combines the cosine/lexical relevance from ``search_skills`` with a
        structural/analogical match (D3): a skill whose trigger conditions
        or tags share a structural token with the query is boosted, and the
        score is normalised to a 0..1 confidence. The structural signal
        prevents routing on embedding distance alone.
        """
        scores = {}

        # Get matching skills from library
        matched = await self.library.search_skills(query)

        for meta in matched:
            score = 0.0

            # Base relevance score
            score += 1.0

            # Boost by use count (proven skills)
            score += min(meta.use_count / 100, 1.0)

            # Boost by success rate
            total = meta.success_count + meta.failure_count
            if total > 0:
                score += (meta.success_count / total) * 2

            # Boost by recent usage (recency). `updated_at` is a persisted
            # wall-clock timestamp, so recency must compare against the wall
            # clock (monotonic resets on reboot and cannot be persisted).
            now = time.time()
            recency = now - meta.updated_at
            if recency < 86400:  # Used in last 24h
                score += 0.5
            elif recency < 604800:  # Used in last week
                score += 0.2

            # D3: structural/analogical match — a shared structural token
            # between the query and the skill's triggers/tags boosts the
            # score beyond what embedding distance alone would give.
            score += self._structural_match(query, meta)

            scores[meta.name] = score

        return scores

    @staticmethod
    def _structural_match(query: str, meta: SkillMetadata) -> float:
        """Return a structural-match boost for a skill against a query.

        Tokenises the query and the skill's trigger conditions and tags,
        then scores the overlap of structural tokens (words that are not
        pure stopwords). This is the analogical signal that complements the
        cosine/lexical score.

        Args:
            query: The routing query.
            meta: The skill metadata.

        Returns:
            A boost in [0, 2.0].

        """
        import re

        stopwords = {
            "the",
            "a",
            "an",
            "and",
            "or",
            "of",
            "to",
            "in",
            "for",
            "on",
            "with",
            "is",
            "are",
            "this",
            "that",
            "it",
            "be",
        }
        query_tokens = {
            t for t in re.findall(r"[a-z0-9_]+", query.lower()) if t not in stopwords and len(t) > 2
        }
        skill_tokens: set[str] = set()
        for cond in meta.trigger_conditions:
            skill_tokens.update(
                t
                for t in re.findall(r"[a-z0-9_]+", cond.lower())
                if t not in stopwords and len(t) > 2
            )
        for tag in meta.tags:
            skill_tokens.update(
                t
                for t in re.findall(r"[a-z0-9_]+", tag.lower())
                if t not in stopwords and len(t) > 2
            )
        if not query_tokens or not skill_tokens:
            return 0.0
        overlap = len(query_tokens & skill_tokens)
        # Normalise to [0, 2.0]: full overlap of a small query is a strong
        # structural signal.
        return min(2.0, overlap * 0.5)

    def _build_query_from_state(self, state: dict[str, Any]) -> str:
        """Build search query from current state."""
        parts = []

        if query := state.get("query", ""):
            parts.append(query)
        if plan := state.get("research_plan", ""):
            parts.append(plan)
        if context := state.get("raw_execution_context", ""):
            parts.append(context[:500])

        return " ".join(parts)[:1000]

    async def create_new_skill(
        self,
        name: str,
        description: str,
        prompt_template: str,
        system_directive: str = "",
        tags: list[str] | None = None,
        trigger_conditions: list[str] | None = None,
    ) -> Skill:
        """Create a new skill (Write phase of R-WRL).

        This is the core of Memento-Skills: agents designing agents.
        """
        skill = Skill(
            metadata=SkillMetadata(
                name=name,
                description=description,
                created_at=time.time(),
                updated_at=time.time(),
                tags=tags or [],
                trigger_conditions=trigger_conditions or [],
            ),
            prompt_template=prompt_template,
            system_directive=system_directive,
        )

        await self.library.register_skill(skill)
        return skill


# ── Global instances ──────────────────────────────────────────────────────────

_library: SkillLibrary | None = None
_router: SkillRouter | None = None


async def get_skill_library() -> SkillLibrary:
    """Get global skill library instance."""
    global _library
    if _library is None:
        _library = SkillLibrary()
    return _library


async def get_skill_router() -> SkillRouter:
    """Get global skill router instance."""
    global _router
    if _router is None:
        _router = SkillRouter(await get_skill_library())
    return _router


async def create_elementary_skills() -> None:
    """Create initial elementary skills (Web search, terminal, etc.).

    These are the seed skills from which Memento-Skills builds more complex ones.
    """
    elementary_skills = [
        {
            "name": "web-search",
            "description": "Search the web for information",
            "tags": ["search", "web", "information"],
            "trigger_conditions": ["search", "find information", "look up"],
            "prompt_template": "Search the web for: {query}\nReturn the most relevant findings.",
            "system_directive": "You are a web search specialist. Use search tools effectively.",
        },
        {
            "name": "file-read",
            "description": "Read and analyze files in the codebase",
            "tags": ["file", "read", "code"],
            "trigger_conditions": ["read file", "examine", "analyze code"],
            "prompt_template": (
                "Read and analyze this file: {path}\nProvide key findings relevant to: {query}"
            ),
            "system_directive": (
                "You are a code analysis specialist. Use read tools to examine files."
            ),
        },
        {
            "name": "code-write",
            "description": "Write or modify code files",
            "tags": ["code", "write", "modify"],
            "trigger_conditions": ["write code", "create file", "modify", "implement"],
            "prompt_template": (
                "Implement the following in {path}:\n{requirement}\nContext: {context}"
            ),
            "system_directive": (
                "You are a code implementation specialist. Use write tools to create/modify files."
            ),
        },
    ]

    library = await get_skill_library()
    for skill_data in elementary_skills:
        existing = await library.get_skill(skill_data["name"])  # type: ignore[arg-type]
        if not existing:
            await library.register_skill(
                Skill(
                    metadata=SkillMetadata(
                        name=skill_data["name"],  # type: ignore[arg-type]
                        description=skill_data["description"],  # type: ignore[arg-type]
                        created_at=time.time(),
                        updated_at=time.time(),
                        tags=skill_data["tags"],  # type: ignore[arg-type]
                        trigger_conditions=skill_data["trigger_conditions"],  # type: ignore[arg-type]
                    ),
                    prompt_template=skill_data["prompt_template"],  # type: ignore[arg-type]
                    system_directive=skill_data["system_directive"],  # type: ignore[arg-type]
                )
            )
            logger.info(f"Created elementary skill: {skill_data['name']}")


if __name__ == "__main__":

    async def demo():
        # Create elementary skills
        await create_elementary_skills()

        # Get router
        router = await get_skill_router()

        # Test skill selection
        state = {"query": "search for information about Python", "research_plan": ""}
        skills = await router.select_skills(state)

        logger.info(f"\nSelected skills for query '{state['query']}':")
        for skill, score in skills:
            logger.info(f"  - {skill.metadata.name} (score: {score:.2f})")

    asyncio.run(demo())
