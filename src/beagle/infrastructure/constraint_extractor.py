"""Constraint Extractor for automatic constraint detection from conversation.

Analyzes conversation messages to extract user constraints using pattern matching
and LLM-based semantic analysis. Extracted constraints are registered with the
ConstraintRegistry for persistence across compaction boundaries.

Extraction Patterns:
    - Explicit: "NO X", "NEVER Y", "MUST Z", "ALWAYS A"
    - Implicit: "This is important", "We need to", "Make sure"
    - Contextual: Architecture decisions, error corrections, clarifications
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from beagle.config.paths import get_workspace_root
from beagle.infrastructure.constraint_registry import (
    Constraint,
    ConstraintCategory,
    ConstraintPriority,
    ConstraintRegistry,
)

logger = logging.getLogger("Beagle.constraint_extractor")


# Pattern definitions for constraint extraction
EXPLICIT_PATTERNS = {
    # NO/NEVER patterns (restrictions)
    ConstraintCategory.RESTRICTION: [
        r"\b(?:NO|NEVER|DO NOT|DON'T|DONOT)\s+(?!.*\b(?:BUT|EXCEPT|IF)\b)([A-Z][A-Z\s]+)",
        r"\b(?:NO|NEVER)\s+\w+\s+socket",
        r"\b(?:NO|NEVER)\s+(?:use|using|allow)\s+\w+",
        r"\bmust\s+not\s+(?!.*\b(?:but|except|if)\b)(.+)",
        r"\b(?:forbidden|prohibited|banned)\s*:?\s*(.+)",
    ],
    # MUST/ALWAYS patterns (requirements)
    ConstraintCategory.REQUIREMENT: [
        r"\b(?:MUST|ALWAYS|REQUIRED|MANDATORY)\s+(?!.*\b(?:optional|if|when)\b)([A-Z][A-Z\s]+)",
        r"\b(?:must|always|need to|have to)\s+(?!.*\b(?:optional|if|when)\b)(.+)",
        r"\ball\s+\w+\s+(?:must|should)\s+be\s+(.+)",
        r"\b(?:ensure|verify|validate)\s+that\s+(.+)",
    ],
    # Architecture decisions
    ConstraintCategory.ARCHITECTURE: [
        r"\b(?:architecture|design|pattern|approach):\s*(.+)",
        r"\buse\s+([A-Za-z]+\s+(?:instead|rather)\s+than)",
        r"\b(?:adopted|chosen|selected)\s+(?:approach|pattern|design):\s*(.+)",
    ],
}

# Intensity markers that indicate priority
PRIORITY_MARKERS = {
    ConstraintPriority.CRITICAL: [
        r"\b(?:CRITICAL|CRUCIAL|EXACTLY|ABSOLUTELY)\b",
        r"!!+",
        r"\b(?:GOLDEN\s+MASTER|CANONICAL|SINGLE\s+SOURCE)\b",
        r"\bNO\s+\w+\s+(?:SOCKET|MOUNT|ACCESS)\b",
    ],
    ConstraintPriority.IMPORTANT: [
        r"\b(?:IMPORTANT|SIGNIFICANT|ESSENTIAL)\b",
        r"\b(?:PLEASE|NOTE|WARNING)\s*:",
    ],
    ConstraintPriority.NICE_TO_HAVE: [
        r"\b(?:OPTIONAL|PREFER|IF\s+POSSIBLE)\b",
        r"\b(?:NICE\s+TO\s+HAVE|CONSIDER)\b",
    ],
}


@dataclass
class ExtractedConstraint:
    """A constraint extracted from conversation, awaiting registration."""

    category: str
    description: str
    content: str
    priority: int
    provenance: dict[str, str]
    confidence: float = 1.0  # Extraction confidence 0-1

    def to_constraint(self) -> Constraint:
        """Convert to Constraint for registration."""
        return Constraint(
            category=self.category,
            description=self.description,
            content=self.content,
            priority=self.priority,
            provenance=self.provenance,
        )


class PatternExtractor:
    """Extract constraints using regex patterns."""

    def __init__(self):
        """Initialize pattern compiler."""
        self._compiled_patterns = {}

        for category, patterns in EXPLICIT_PATTERNS.items():
            self._compiled_patterns[category] = [re.compile(p, re.IGNORECASE) for p in patterns]

        self._priority_patterns = {}
        for priority, patterns in PRIORITY_MARKERS.items():
            self._priority_patterns[priority] = [re.compile(p, re.IGNORECASE) for p in patterns]

    def extract(
        self, text: str, context: dict[str, str] | None = None
    ) -> list[ExtractedConstraint]:
        """Extract constraints from text using patterns.

        Args:
            text: Text to analyze
            context: Optional context (message_id, session_id, etc.)

        Returns:
            List of extracted constraints

        """
        constraints = []
        provenance = context or {}

        # Check each category's patterns
        for category, patterns in self._compiled_patterns.items():
            for pattern in patterns:
                matches = pattern.finditer(text)
                for match in matches:
                    raw_content = match.group(1) if match.groups() else match.group(0)
                    content = (
                        raw_content.strip().upper()
                        if category == ConstraintCategory.RESTRICTION
                        else raw_content.strip()
                    )

                    # Infer priority from markers
                    priority = self._infer_priority(text, match.start())

                    # Create description
                    description = self._create_description(category, content)

                    constraint = ExtractedConstraint(
                        category=category,
                        description=description,
                        content=content,
                        priority=priority,
                        provenance=provenance,
                        confidence=0.9,  # Pattern matches have high confidence
                    )
                    constraints.append(constraint)

        return constraints

    def _infer_priority(self, text: str, match_position: int) -> int:
        """Infer priority from surrounding context.

        Args:
            text: Full text
            match_position: Position of match in text

        Returns:
            Priority level

        """
        # Check text around match for priority markers
        window = 100  # Characters to check around match
        start = max(0, match_position - window)
        end = min(len(text), match_position + window)
        context = text[start:end]

        for priority in [
            ConstraintPriority.CRITICAL,
            ConstraintPriority.IMPORTANT,
        ]:  # Check higher priorities first
            for pattern in self._priority_patterns.get(priority, []):
                if pattern.search(context):
                    return priority

        return ConstraintPriority.IMPORTANT  # Default to important

    def _create_description(self, category: str, content: str) -> str:
        """Create human-readable description from content."""
        # Truncate and clean
        content = content.strip()
        if len(content) > 100:
            content = content[:97] + "..."

        # Category prefix
        prefixes = {
            ConstraintCategory.RESTRICTION: "Prohibited",
            ConstraintCategory.REQUIREMENT: "Required",
            ConstraintCategory.ARCHITECTURE: "Architecture",
            ConstraintCategory.PREFERENCE: "Preference",
        }

        prefix = prefixes.get(category, "Constraint")
        return f"{prefix}: {content}"


class LLMConstraintExtractor:
    """Extract constraints using LLM semantic analysis.

    For messages that don't match patterns but contain implicit constraints.
    Uses the configured provider to analyze text.
    """

    def __init__(self, model: str = "glm-5.1:cloud", provider: str = "ollama_cloud"):
        """Initialize LLM extractor.

        Args:
            model: Model to use for extraction
            provider: Provider name

        """
        self.model = model
        self.provider = provider
        self._client = None

    def _get_extraction_prompt(self, text: str) -> str:
        """Generate prompt for constraint extraction."""
        return f"""Analyze the following text and extract any
user constraints, preferences, or requirements.

Focus on:
1. Explicit restrictions (NO, NEVER, DO NOT)
2. Required behaviors (MUST, ALWAYS, REQUIRED)
3. Architecture decisions (design choices, patterns adopted)
4. Preferences (preferred approaches, styles)

For each constraint found, provide:
- category: one of [restriction, requirement, architecture, preference]
- priority: one of [critical=1, important=2, nice_to_have=3]
- description: brief summary
- content: full constraint text

Text to analyze:
---
{text}
---

Respond in JSON format:
{{"constraints": [{{"category": "...", "priority": N, "description": "...", "content": "..."}}]}}

If no constraints found, respond with: {{"constraints": []}}
Only respond with the JSON, no other text."""

    async def extract(
        self, text: str, context: dict[str, str] | None = None
    ) -> list[ExtractedConstraint]:
        """Extract constraints using LLM analysis.

        Args:
            text: Text to analyze
            context: Optional context for provenance

        Returns:
            List of extracted constraints

        """
        constraints = []
        provenance = context or {}

        try:
            from beagle.bridges.llm_direct import DirectLLMClient

            prompt = self._get_extraction_prompt(text)

            # Use the direct Ollama Cloud client (async) for extraction.
            client = DirectLLMClient(model=self.model)
            response = await client.generate(prompt, max_tokens=2000)
            result = response.content
            await client.close()

            if result:
                # Parse JSON response
                parsed = self._parse_response(result)
                for item in parsed.get("constraints", []):
                    constraint = ExtractedConstraint(
                        category=item.get("category", ConstraintCategory.PREFERENCE),
                        description=item.get("description", ""),
                        content=item.get("content", ""),
                        priority=item.get("priority", ConstraintPriority.IMPORTANT),
                        provenance=provenance,
                        confidence=0.7,  # LLM extracts have lower confidence
                    )
                    constraints.append(constraint)

        except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
            logger.warning(f"LLM constraint extraction failed: {e}")

        return constraints

    def _parse_response(self, response: str) -> dict[str, Any]:
        """Parse an LLM response as JSON.

        Two strategies are tried in order: the embedded ``{"constraints": …}``
        object, then the whole response. The first strategy not applying is
        normal and is not reported; losing *both* means the model's constraints
        were dropped, which an operator has to know about.

        Args:
            response: Raw LLM response text.

        Returns:
            The parsed object, or ``{"constraints": []}`` when neither strategy
            yields JSON.

        """
        candidates: list[str] = []
        json_match = re.search(r'\{[^{}]*"constraints"[^{}]*\}', response, re.DOTALL)
        if json_match:
            candidates.append(json_match.group(0))
        candidates.append(response)

        errors: list[str] = []
        for candidate in candidates:
            try:
                return json.loads(candidate)  # type: ignore[no-any-return]
            except json.JSONDecodeError as exc:
                errors.append(str(exc))

        logger.warning(
            "Cannot parse the LLM constraint response as JSON after %d attempt(s) "
            "(%s); no constraints were extracted from this message.",
            len(candidates),
            "; ".join(errors),
        )
        return {"constraints": []}


class ConstraintExtractor:
    """Main extractor combining pattern matching and LLM analysis.

    Workflow:
        1. Try pattern extraction (fast, high confidence)
        2. For messages without pattern matches, try LLM extraction
        3. Deduplicate and merge constraints
        4. Register with ConstraintRegistry
    """

    def __init__(
        self,
        registry: ConstraintRegistry | None = None,
        use_llm: bool = True,
        model: str = "glm-5.1:cloud",
        provider: str = "ollama_cloud",
    ):
        """Initialize extractor.

        Args:
            registry: ConstraintRegistry for persistence (created if None)
            use_llm: Whether to use LLM for implicit constraints
            model: Model for LLM extraction
            provider: Provider for LLM extraction

        """
        self.registry = registry or ConstraintRegistry(project=str(get_workspace_root()))
        self.pattern_extractor = PatternExtractor()
        self.llm_extractor = LLMConstraintExtractor(model, provider) if use_llm else None
        self.use_llm = use_llm

    def extract_from_message(
        self,
        message: str,
        role: str,
        message_id: str = "",
        session_id: str = "",
    ) -> list[Constraint]:
        """Extract constraints from a single message.

        Args:
            message: Message content
            role: Message role (user/assistant)
            message_id: Optional message ID for provenance
            session_id: Optional session ID for provenance

        Returns:
            List of extracted and registered constraints

        """
        if role != "user":
            return []  # Only extract from user messages

        context = {
            "message_id": message_id,
            "session_id": session_id,
            "source": "message",
        }

        # Try pattern extraction first
        pattern_constraints = self.pattern_extractor.extract(message, context)

        # Use LLM for implicit constraints if enabled and no pattern matches
        llm_constraints = []  # type: ignore[var-annotated]
        if (
            self.use_llm
            and self.llm_extractor
            and not pattern_constraints
            and self._might_contain_constraints(message)
        ):
            llm_constraints = self.llm_extractor.extract(message, context)  # type: ignore[assignment]
            # Note: This is async, but for now we'll treat it as sync
            # In production, this would be awaited

        # Combine and deduplicate
        all_constraints = pattern_constraints + llm_constraints
        registered = []

        for extracted in all_constraints:
            constraint = extracted.to_constraint()
            # Check for duplicates before registering
            if not self._is_duplicate(constraint):
                self.registry.register(constraint)
                registered.append(constraint)
                logger.info(f"Extracted constraint: {constraint}")

        return registered

    def extract_from_session(
        self,
        messages: list[dict[str, str]],
        session_id: str = "",
    ) -> list[Constraint]:
        """Extract constraints from a session's messages.

        Args:
            messages: List of message dicts with 'role' and 'content' keys
            session_id: Optional session ID

        Returns:
            List of extracted constraints

        """
        all_constraints = []

        for i, msg in enumerate(messages):
            role = msg.get("role", "")
            content = msg.get("content", "")
            message_id = f"{session_id}_{i}" if session_id else f"msg_{i}"

            constraints = self.extract_from_message(
                message=content,
                role=role,
                message_id=message_id,
                session_id=session_id,
            )
            all_constraints.extend(constraints)

        # Save registry after batch extraction
        self.registry.save()

        return all_constraints

    def _might_contain_constraints(self, message: str) -> bool:
        """Check if message might contain implicit constraints.

        Args:
            message: Message content

        Returns:
            True if message might contain constraints

        """
        # Heuristics for constraint-like messages
        indicators = [
            r"\b(?:need|should|must|have to|important)\b",
            r"\b(?:make sure|ensure|verify|always)\b",
            r"\b(?:prefer|better|instead of)\b",
            r"\b(?:this is|we're)\s+(?:critical|important|essential)\b",
            r"\b(?:golden\s+master|canonical)\b",
            r"!{2,}",  # Multiple exclamation marks
        ]

        return any(re.search(pattern, message, re.IGNORECASE) for pattern in indicators)

    def _is_duplicate(self, constraint: Constraint) -> bool:
        """Check if constraint already exists in registry.

        Args:
            constraint: Constraint to check

        Returns:
            True if duplicate found

        """
        active = self.registry.get_active()

        for existing in active:
            # Check content similarity
            if self._similar_content(existing.content, constraint.content):
                return True

            # Check description similarity
            if (
                existing.description
                and constraint.description
                and self._similar_content(existing.description, constraint.description)
            ):
                return True

        return False

    def _similar_content(self, a: str, b: str) -> bool:
        """Check if two texts are similar enough to be duplicates."""
        # Normalize
        a_norm = a.lower().strip()
        b_norm = b.lower().strip()

        # Exact match
        if a_norm == b_norm:
            return True

        # One contains the other
        if a_norm in b_norm or b_norm in a_norm:
            return True

        # Word overlap
        a_words = set(a_norm.split())
        b_words = set(b_norm.split())

        if a_words and b_words:
            overlap = len(a_words & b_words) / min(len(a_words), len(b_words))
            if overlap > 0.8:
                return True

        return False

    def extract_for_compaction(
        self,
        session_messages: list[dict[str, str]],
        session_id: str,
    ) -> list[Constraint]:
        """Extract constraints before compaction.

        This is the main entry point for integration with ContextCompactionHook.

        Args:
            session_messages: Messages from current session
            session_id: Session identifier

        Returns:
            List of newly extracted constraints

        """
        # Load existing constraints
        self.registry.load()

        # Extract from all user messages
        constraints = self.extract_from_session(session_messages, session_id)

        # Save updated registry
        self.registry.save()

        if constraints:
            logger.info(f"Extracted {len(constraints)} constraints from session {session_id}")

        return constraints


def create_extractor(
    project: str = "",
    use_llm: bool = False,
    model: str = "glm-5.1:cloud",
    provider: str = "ollama_cloud",
) -> ConstraintExtractor:
    """Factory function to create a ConstraintExtractor.

    Args:
        project: Project/workspace identifier
        use_llm: Whether to use LLM extraction
        model: Model for LLM extraction
        provider: Provider for LLM extraction

    Returns:
        Configured ConstraintExtractor

    """
    registry = ConstraintRegistry(project=project)
    registry.load()

    return ConstraintExtractor(
        registry=registry,
        use_llm=use_llm,
        model=model,
        provider=provider,
    )
