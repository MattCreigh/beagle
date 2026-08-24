"""Tests for semantic knowledge content-addressed IDs and duplicate detection.

Covers the D3 remediation: the content-addressed ID path in
``KnowledgeEntry.__post_init__`` was dead because the ``id`` field's
``default_factory`` always minted a fresh UUID, so ``not self.id`` was always
false and re-ingesting identical knowledge always minted a duplicate.
"""

from __future__ import annotations

import hashlib

from beagle.infrastructure.semantic_knowledge import (
    KnowledgeCategory,
    KnowledgeEntry,
    SemanticKnowledgeIndex,
)


class TestContentAddressedId:
    """The content-addressed ID path must be reachable and deterministic."""

    def test_identical_content_yields_same_id(self):
        """Two entries with identical content get the same content hash id."""
        e1 = KnowledgeEntry(category=KnowledgeCategory.CONCEPT, title="t", content="same content")
        e2 = KnowledgeEntry(category=KnowledgeCategory.CONCEPT, title="t", content="same content")
        assert e1.id == e2.id

    def test_id_is_content_hash(self):
        """The id is the category prefix + sha256(content) prefix."""
        entry = KnowledgeEntry(
            category=KnowledgeCategory.CONCEPT, title="t", content="same content"
        )
        expected = (
            f"{KnowledgeCategory.CONCEPT[:3]}_{hashlib.sha256(b'same content').hexdigest()[:16]}"
        )
        assert entry.id == expected

    def test_different_content_yields_different_id(self):
        """Different content produces a different content hash id."""
        e1 = KnowledgeEntry(category=KnowledgeCategory.CONCEPT, title="t", content="content A")
        e2 = KnowledgeEntry(category=KnowledgeCategory.CONCEPT, title="t", content="content B")
        assert e1.id != e2.id

    def test_explicit_id_is_preserved(self):
        """An explicitly provided id is not overwritten by the content hash."""
        entry = KnowledgeEntry(
            id="explicit-id",
            category=KnowledgeCategory.CONCEPT,
            title="t",
            content="same content",
        )
        assert entry.id == "explicit-id"


class TestFromJsonContentHash:
    """from_json must derive the content hash when no id is present."""

    def test_from_json_without_id_derives_content_hash(self):
        """from_json with no id derives the same content hash as direct init."""
        direct = KnowledgeEntry(
            category=KnowledgeCategory.CONCEPT, title="t", content="same content"
        )
        from_json = KnowledgeEntry.from_json(
            {"content": "same content", "category": KnowledgeCategory.CONCEPT}
        )
        assert from_json.id == direct.id

    def test_from_json_preserves_explicit_id(self):
        """from_json preserves an explicit id from the data dict."""
        entry = KnowledgeEntry.from_json({"id": "stored-id", "content": "same content"})
        assert entry.id == "stored-id"


class TestDuplicateDetection:
    """The index must reject re-ingesting identical knowledge."""

    def test_identical_content_rejected(self):
        """Adding identical content twice rejects the second add."""
        idx = SemanticKnowledgeIndex()
        e1 = KnowledgeEntry(category=KnowledgeCategory.CONCEPT, title="t", content="same content")
        e2 = KnowledgeEntry(category=KnowledgeCategory.CONCEPT, title="t", content="same content")
        assert idx.add(e1) is True
        assert idx.add(e2) is False

    def test_different_content_accepted(self):
        """Adding different content succeeds."""
        idx = SemanticKnowledgeIndex()
        e1 = KnowledgeEntry(category=KnowledgeCategory.CONCEPT, title="first", content="content A")
        e2 = KnowledgeEntry(category=KnowledgeCategory.CONCEPT, title="second", content="content B")
        assert idx.add(e1) is True
        assert idx.add(e2) is True
