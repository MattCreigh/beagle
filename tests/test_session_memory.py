"""Tests for Session Memory.

Tests for session-scoped memory management.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


class TestMessageRecord:
    """Test MessageRecord dataclass."""

    def test_message_record_creation(self):
        """MessageRecord can be created."""
        try:
            from beagle.infrastructure.session_memory import (
                MessageRecord,
            )

            msg = MessageRecord(
                role="user",
                content="Hello world",
            )
            assert msg.role == "user"
            assert msg.content == "Hello world"
        except ImportError:
            pytest.skip("session_memory requires hierarchical_memory")

    def test_message_record_with_metadata(self):
        """MessageRecord can have metadata."""
        try:
            from beagle.infrastructure.session_memory import (
                MessageRecord,
            )

            msg = MessageRecord(
                role="assistant",
                content="Response",
                metadata={"model": "gpt-4", "tokens": 100},
            )
            assert msg.metadata["model"] == "gpt-4"
            assert msg.metadata["tokens"] == 100
        except ImportError:
            pytest.skip("session_memory requires hierarchical_memory")

    def test_message_record_to_json(self):
        """MessageRecord can convert to JSON."""
        try:
            from beagle.infrastructure.session_memory import (
                MessageRecord,
            )

            msg = MessageRecord(
                role="user",
                content="Test",
                timestamp=1000.0,
            )
            json_dict = msg.to_json()

            assert json_dict["role"] == "user"
            assert json_dict["content"] == "Test"
            assert json_dict["timestamp"] == 1000.0
        except ImportError:
            pytest.skip("session_memory requires hierarchical_memory")


class TestSessionEpisode:
    """Test SessionEpisode dataclass."""

    def test_session_episode_creation(self):
        """SessionEpisode can be created."""
        try:
            from beagle.infrastructure.session_memory import (
                MessageRecord,
                SessionEpisode,
            )

            episode = SessionEpisode(
                id="test-id",
                session_id="test-session",
                messages=[
                    MessageRecord(role="user", content="Hello"),
                    MessageRecord(role="assistant", content="Hi there"),
                ],
            )
            assert episode.session_id == "test-session"
            assert len(episode.messages) == 2
        except ImportError:
            pytest.skip("session_memory requires hierarchical_memory")

    def test_session_episode_add_message(self):
        """SessionEpisode can add messages."""
        try:
            from beagle.infrastructure.session_memory import (
                MessageRecord,
                SessionEpisode,
            )

            episode = SessionEpisode(id="test-id", session_id="test", messages=[])
            episode.messages.append(MessageRecord(role="user", content="Test"))
            assert len(episode.messages) == 1
        except ImportError:
            pytest.skip("session_memory requires hierarchical_memory")


class TestSessionMemory:
    """Test SessionMemory class."""

    def test_session_memory_creation(self):
        """SessionMemory can be created."""
        try:
            from beagle.infrastructure.session_memory import (
                SessionMemory,
            )

            memory = SessionMemory(session_id="test-session")
            assert memory.session_id == "test-session"
        except ImportError:
            pytest.skip("session_memory requires hierarchical_memory")

    def test_session_memory_add_message(self):
        """SessionMemory can add messages."""
        try:
            from beagle.infrastructure.session_memory import (
                SessionMemory,
            )

            memory = SessionMemory(session_id="test")

            memory.add_message(role="user", content="Hello")
            memory.add_message(role="assistant", content="Hi there")

            messages = memory.get_session_messages()
            assert len(messages) == 2
            assert messages[0]["role"] == "user"
            assert messages[1]["role"] == "assistant"
        except ImportError:
            pytest.skip("session_memory requires hierarchical_memory")

    def test_session_memory_clear(self):
        """SessionMemory can be cleared."""
        try:
            from beagle.infrastructure.session_memory import (
                SessionMemory,
            )

            memory = SessionMemory(session_id="test")
            memory.add_message(role="user", content="Test")
            memory.clear()

            messages = memory.get_session_messages()
            assert len(messages) == 0
        except ImportError:
            pytest.skip("session_memory requires hierarchical_memory")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
