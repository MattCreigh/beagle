"""Knowledge Extractor for semantic knowledge extraction from session content.

Extracts structured knowledge from conversation messages and file content.
Identifies concepts, patterns, decisions, errors, and other knowledge types.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from beagle.infrastructure.semantic_knowledge import (
    KnowledgeCategory,
    KnowledgeEntry,
    SemanticKnowledgeIndex,
)

logger = logging.getLogger("Beagle.knowledge_extractor")


# Patterns for knowledge extraction
KNOWLEDGE_PATTERNS = {
    KnowledgeCategory.DECISION: [
        # Decision patterns
        r"(?:we |I |let's )?(?:decided|chose|selected|went with)\s+(.+?)(?:\.|because|since)",
        r"(?:architecture|design|approach):\s*(.+?)(?:\.|$)",
        r"(?:the |this )?(?:pattern|approach|method)\s+(?:is|was)\s+(.+?)(?:\.|because)",
        r"(?:using|adopting|implementing)\s+(.+?)\s+(?:instead|rather)\s+than",
    ],
    KnowledgeCategory.PATTERN: [
        # Pattern patterns
        r"(?:pattern|practice|convention):\s*(.+?)(?:\.|$)",
        r"(?:always|never|must|should)\s+(.+?)(?:when|for|if)",
        r"(?:best practice|recommended|prefer)\s+(?:to\s+)?(.+?)(?:\.|$)",
        r"(?:use|using|follow)\s+(?:the\s+)?(.+?)\s+pattern",
    ],
    KnowledgeCategory.ERROR: [
        # Error patterns
        r"(?:error|exception|failure|bug):\s*(.+?)(?:\.|$)",
        r"(?:fix|fixed|resolved|solved)\s+(?:by|with|using)\s+(.+?)(?:\.|$)",
        r"(?:problem|issue|bug)\s+(?:was|is)\s+(.+?)(?:\.|because)",
        r"(?:workaround|solution):\s*(.+?)(?:\.|$)",
    ],
    KnowledgeCategory.API: [
        # API patterns
        r"(?:function|method|class)\s+(\w+)\s+(?:takes|accepts|returns)\s+(.+)",
        r"(?:api|interface|endpoint):\s*(.+?)(?:\.|$)",
        r"(?:call|invoke|use)\s+(?:the\s+)?(\w+)\s+(?:with|using)\s+(.+)",
    ],
    KnowledgeCategory.CONCEPT: [
        # Concept patterns
        r"(?:concept|idea|principle):\s*(.+?)(?:\.|$)",
        r"(?:key|important|critical)\s+(?:point|concept):\s*(.+?)(?:\.|$)",
        r"(?:understanding|note|remember):\s*(.+?)(?:\.|$)",
    ],
    KnowledgeCategory.INSIGHT: [
        # Insight patterns
        r"(?:insight|discovery|realization):\s*(.+?)(?:\.|$)",
        r"(?:importantly|notably|interestingly),?\s+(.+?)(?:\.|$)",
        r"(?:this means|it follows|therefore)\s+(?:that\s+)?(.+?)(?:\.|$)",
    ],
}


@dataclass
class ExtractedKnowledge:
    """Temporarily holds extracted knowledge before indexing."""

    category: str
    title: str
    content: str
    confidence: float = 0.8
    source: str = ""
    tags: list[str] | None = None

    def to_entry(self, project: str = "") -> KnowledgeEntry:
        """Convert to KnowledgeEntry."""
        return KnowledgeEntry(
            category=self.category,
            title=self.title,
            content=self.content,
            confidence=self.confidence,
            source=self.source,
            project=project,
            tags=self.tags or [],
        )


class KnowledgeExtractor:
    """Extracts knowledge from session content.

    Uses pattern matching for structured extraction and optional
    LLM-based semantic extraction for complex content.
    """

    def __init__(
        self,
        index: SemanticKnowledgeIndex | None = None,
        use_llm: bool = False,
        model: str = "glm-5.1:cloud",
        project: str = "",
    ):
        """Initialize extractor.

        Args:
            index: Knowledge index for storage
            use_llm: Whether to use LLM for extraction
            model: Model for LLM extraction
            project: Project identifier

        """
        self.index = index or SemanticKnowledgeIndex(project=project)
        self.use_llm = use_llm
        self.model = model
        self.project = project

        # Compile patterns
        self._compiled_patterns = {}
        for category, patterns in KNOWLEDGE_PATTERNS.items():
            self._compiled_patterns[category] = [re.compile(p, re.IGNORECASE) for p in patterns]

    def extract_from_message(
        self,
        message: str,
        role: str,
        source: str = "",
    ) -> list[KnowledgeEntry]:
        """Extract knowledge from a message.

        Args:
            message: Message content
            role: Message role (user/assistant)
            source: Source identifier

        Returns:
            List of extracted knowledge entries

        """
        # Only extract from assistant messages (learned knowledge)
        if role != "assistant":
            return []

        extracted = self._extract_patterns(message, source)

        # Convert to entries
        entries = []
        for ex in extracted:
            entry = ex.to_entry(project=self.project)
            if self.index.add(entry):
                entries.append(entry)

        if entries:
            logger.info(f"Extracted {len(entries)} knowledge entries from message")

        return entries

    def _extract_patterns(self, text: str, source: str) -> list[ExtractedKnowledge]:
        """Extract knowledge using patterns.

        Args:
            text: Text to extract from
            source: Source identifier

        Returns:
            List of extracted knowledge

        """
        results = []

        for category, patterns in self._compiled_patterns.items():
            for pattern in patterns:
                matches = pattern.finditer(text)
                for match in matches:
                    # Get the captured group
                    content = match.group(1) if match.groups() else match.group(0)
                    content = content.strip()

                    if len(content) < 10:  # Skip very short extractions
                        continue

                    # Generate title from content
                    title = self._generate_title(content, category)

                    extracted = ExtractedKnowledge(
                        category=category,
                        title=title,
                        content=content,
                        confidence=0.7,  # Pattern matches have decent confidence
                        source=source,
                        tags=self._extract_tags(text, content),
                    )
                    results.append(extracted)

        return results

    def _generate_title(self, content: str, category: str) -> str:
        """Generate a title for extracted content.

        Args:
            content: Extracted content
            category: Knowledge category

        Returns:
            Brief title

        """
        # Truncate to first sentence or 50 chars
        first_sentence = content.split(".")[0]
        if len(first_sentence) > 50:
            first_sentence = first_sentence[:47] + "..."

        # Capitalize first letter
        if first_sentence:
            first_sentence = first_sentence[0].upper() + first_sentence[1:]

        return first_sentence

    def _extract_tags(self, full_text: str, content: str) -> list[str]:
        """Extract relevant tags for knowledge.

        Args:
            full_text: Full text context
            content: Extracted content

        Returns:
            List of tags

        """
        tags = []

        # Technical keywords
        tech_keywords = [
            "python",
            "rust",
            "go",
            "javascript",
            "typescript",
            "docker",
            "kubernetes",
            "database",
            "api",
            "rest",
            "async",
            "thread",
            "process",
            "memory",
            "performance",
            "security",
            "auth",
            "token",
            "cache",
            "queue",
        ]

        text_lower = (full_text + " " + content).lower()
        for keyword in tech_keywords:
            if keyword in text_lower:
                tags.append(keyword)

        return tags[:5]  # Limit to 5 tags

    def extract_from_session(
        self,
        messages: list[dict[str, str]],
        session_id: str = "",
    ) -> list[KnowledgeEntry]:
        """Extract knowledge from a full session.

        Args:
            messages: List of message dicts with 'role' and 'content'
            session_id: Session identifier

        Returns:
            List of extracted knowledge entries

        """
        all_entries = []

        for i, msg in enumerate(messages):
            role = msg.get("role", "")
            content = msg.get("content", "")
            source = f"{session_id}_msg_{i}" if session_id else f"msg_{i}"

            entries = self.extract_from_message(content, role, source)
            all_entries.extend(entries)

        return all_entries

    def extract_from_file(
        self,
        filepath: str,
        content: str,
    ) -> list[KnowledgeEntry]:
        """Extract knowledge from a file.

        Args:
            filepath: File path
            content: File content

        Returns:
            List of extracted knowledge entries

        """
        entries = []

        # For files, we primarily look for patterns in comments
        # Extract code comments and docstrings
        comments = self._extract_comments(content)

        for comment in comments:
            extracted = self._extract_patterns(comment, source=filepath)
            for ex in extracted:
                entry = ex.to_entry(project=self.project)
                # Files have higher confidence
                entry.confidence = 0.9
                if self.index.add(entry):
                    entries.append(entry)

        return entries

    def _extract_comments(self, content: str) -> list[str]:
        """Extract comments from code content.

        Args:
            content: Code content

        Returns:
            List of comment strings

        """
        comments = []

        # Python-style comments
        for match in re.finditer(r"#(.+)$", content, re.MULTILINE):
            comments.append(match.group(1).strip())

        # Docstrings
        for match in re.finditer(r'"""(.+?)"""', content, re.DOTALL):
            comments.append(match.group(1).strip())
        for match in re.finditer(r"'''(.+?)'''", content, re.DOTALL):
            comments.append(match.group(1).strip())

        # C-style comments
        for match in re.finditer(r"//(.+)$", content, re.MULTILINE):
            comments.append(match.group(1).strip())
        for match in re.finditer(r"/\*(.+?)\*/", content, re.DOTALL):
            comments.append(match.group(1).strip())

        return comments

    def extract_for_compaction(
        self,
        session_messages: list[dict[str, str]],
        session_id: str,
    ) -> list[KnowledgeEntry]:
        """Extract knowledge before context compaction.

        This is the main entry point for integration with ContextCompactionHook.

        Args:
            session_messages: Messages from current session
            session_id: Session identifier

        Returns:
            List of newly extracted knowledge entries

        """
        # Load existing knowledge
        self.index.load()

        # Extract from assistant messages
        entries = self.extract_from_session(session_messages, session_id)

        # Save updated index
        self.index.save()

        if entries:
            logger.info(f"Extracted {len(entries)} knowledge entries from session {session_id}")

        return entries


def create_extractor(
    project: str = "",
    use_llm: bool = False,
    model: str = "glm-5.1:cloud",
) -> KnowledgeExtractor:
    """Factory function to create a KnowledgeExtractor.

    Args:
        project: Project identifier
        use_llm: Whether to use LLM extraction
        model: Model for LLM extraction

    Returns:
        Configured KnowledgeExtractor

    """
    index = SemanticKnowledgeIndex(project=project)
    index.load()

    return KnowledgeExtractor(
        index=index,
        use_llm=use_llm,
        model=model,
        project=project,
    )
