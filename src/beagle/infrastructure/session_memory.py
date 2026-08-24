"""Session Memory Integration for Beagle v12.2

Bridges session messages with HierarchicalMemory for persistent episodic memory.
Integrates with ContextCompactionHook for automatic session archival.

Key concepts:
- SessionMemory: Converts session messages into memory entries
- MemoryEpisode: A group of related session messages (one agent turn)
- EpisodeReplay: Reconstructs context from episodic memory

Storage:
    ~/.cache/beagle/memory/
        ├── episodic.json         # HierarchicalMemory episodes
        ├── sessions/             # Session archives
        │   └── {session_id}.json # Individual session cache
        └── knowledge/           # Knowledge entries (from Phase 2)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from beagle.config.paths import get_memory_dir
from beagle.memory.hierarchical_memory import (
    HierarchicalMemory,
    MemoryEntry,
    MemoryLevel,
)

logger = logging.getLogger("Beagle.session_memory")

# v0.3.0: Session memory size caps to prevent unbounded growth
_MAX_SESSION_MESSAGES = 10_000
_MAX_SESSION_EPISODES = 500


def get_sessions_dir() -> Path:
    """Get sessions archive directory."""
    path = get_memory_dir() / "sessions"
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass
class MessageRecord:
    """A single message in a session."""

    role: str
    content: str
    timestamp: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict:
        return {
            "role": self.role,
            "content": self.content,
            "timestamp": self.timestamp,
            "metadata": self.metadata,
        }

    @classmethod
    def from_json(cls, data: dict) -> MessageRecord:
        return cls(
            role=data.get("role", "user"),
            content=data.get("content", ""),
            timestamp=data.get("timestamp", time.time()),
            metadata=data.get("metadata", {}),
        )


@dataclass
class SessionEpisode:
    """A group of related messages forming an episode.

    An episode represents a logical unit of work:
    - A user request + assistant response (Q&A pair)
    - A task execution (multiple turns)
    - A workflow phase (planning, execution, synthesis)
    """

    id: str
    session_id: str
    messages: list[MessageRecord]
    summary: str = ""
    phase: str = ""
    start_time: float = field(default_factory=time.time)
    end_time: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "messages": [m.to_json() for m in self.messages],
            "summary": self.summary,
            "phase": self.phase,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "metadata": self.metadata,
        }

    @classmethod
    def from_json(cls, data: dict) -> SessionEpisode:
        return cls(
            id=data.get("id", ""),
            session_id=data.get("session_id", ""),
            messages=[MessageRecord.from_json(m) for m in data.get("messages", [])],
            summary=data.get("summary", ""),
            phase=data.get("phase", ""),
            start_time=data.get("start_time", time.time()),
            end_time=data.get("end_time", time.time()),
            metadata=data.get("metadata", {}),
        )

    def generate_summary(self) -> str:
        """Generate a brief summary from messages."""
        if self.summary:
            return self.summary

        # Extract key information from messages
        user_msgs = [m.content for m in self.messages if m.role == "user"]
        [m.content for m in self.messages if m.role == "assistant"]

        # First user message is usually the query/intent
        if user_msgs:
            first_user = user_msgs[0][:200]
            return f"[{self.phase}] {first_user}..." if self.phase else first_user[:200]

        return f"Episode {self.id[:8]}"


class SessionMemory:
    """Manages session archival and episodic memory integration.

    Flow:
        1. Session starts → create session record
        2. Messages arrive → add to session buffer
        3. Episode ends → archive to episodic memory
        4. Compaction → consolidate to HierarchicalMemory
        5. Resume → reconstruct context from episodic
    """

    def __init__(
        self,
        session_id: str = "",
        memory: HierarchicalMemory | None = None,
    ):
        """Initialize session memory.

        Args:
            session_id: Unique session identifier
            memory: Optional HierarchicalMemory instance (created if None)

        """
        self.session_id = session_id or self._generate_session_id()
        self._memory = memory

        # Session state
        self._messages: list[MessageRecord] = []
        self._current_episode: SessionEpisode | None = None
        self._episodes: list[SessionEpisode] = []

        # Session archive path
        self._archive_path = get_sessions_dir() / f"{self.session_id}.json"

        # Load existing session if available
        self._load_session()

    def _generate_session_id(self) -> str:
        """Generate a unique session ID."""
        timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        hash_part = hashlib.sha256(os.urandom(16)).hexdigest()[:6]
        return f"{timestamp}_{hash_part}"

    @property
    def memory(self) -> HierarchicalMemory:
        """Get or create Memory instance."""
        if self._memory is None:
            # Synchronous wrapper for async get_hierarchical_memory
            try:
                asyncio.get_running_loop()
                # We're in an async context, need to use create_task
                # For now, create a new instance
                self._memory = HierarchicalMemory()
            except RuntimeError:
                # No running loop, create new instance
                self._memory = HierarchicalMemory()
        return self._memory

    def _load_session(self) -> None:
        """Load session state from archive if exists."""
        if not self._archive_path.exists():
            return

        try:
            data = json.loads(self._archive_path.read_text())

            # Load messages
            self._messages = [MessageRecord.from_json(m) for m in data.get("messages", [])]

            # Load episodes
            self._episodes = [SessionEpisode.from_json(e) for e in data.get("episodes", [])]

            logger.info(
                f"Loaded session {self.session_id}: "
                f"{len(self._messages)} messages, {len(self._episodes)} episodes"
            )

        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as e:
            logger.warning(f"Failed to load session {self.session_id}: {e}")

    def _save_session(self) -> None:
        """Save session state to archive."""
        try:
            data = {
                "session_id": self.session_id,
                "messages": [m.to_json() for m in self._messages],
                "episodes": [e.to_json() for e in self._episodes],
                "updated_at": datetime.now(UTC).isoformat(),
            }

            # Atomic write
            temp_path = self._archive_path.with_suffix(".tmp")
            temp_path.write_text(json.dumps(data, indent=2))
            os.replace(temp_path, self._archive_path)

        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as e:
            logger.error(f"Failed to save session {self.session_id}: {e}")

    def add_message(
        self,
        role: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Add a message to the session.

        Args:
            role: Message role (user/assistant/system)
            content: Message content
            metadata: Optional metadata

        """
        record = MessageRecord(
            role=role,
            content=content,
            metadata=metadata or {},
        )
        self._messages.append(record)

        # v0.3.0: Cap messages to prevent unbounded memory growth
        if len(self._messages) > _MAX_SESSION_MESSAGES:
            self._messages = self._messages[-_MAX_SESSION_MESSAGES:]

        # Start episode on user message
        if role == "user" and self._current_episode is None:
            self._current_episode = SessionEpisode(
                id=hashlib.sha256(f"{self.session_id}{time.time()}".encode()).hexdigest()[:12],
                session_id=self.session_id,
                messages=[record],
                phase=metadata.get("phase", "") if metadata else "",
            )
        elif self._current_episode is not None:
            self._current_episode.messages.append(record)

    def end_episode(self, metadata: dict[str, Any] | None = None) -> SessionEpisode | None:
        """End the current episode and archive it.

        Args:
            metadata: Optional metadata for the episode

        Returns:
            Completed episode or None if no episode active

        """
        if self._current_episode is None:
            return None

        # Finalize episode
        self._current_episode.end_time = time.time()
        if metadata:
            self._current_episode.metadata.update(metadata)

        # Generate summary
        self._current_episode.generate_summary()

        episode = self._current_episode
        self._episodes.append(episode)
        self._current_episode = None

        # v0.3.0: Cap episodes to prevent unbounded memory growth
        if len(self._episodes) > _MAX_SESSION_EPISODES:
            self._episodes = self._episodes[-_MAX_SESSION_EPISODES:]

        # Archive to memory
        self._archive_episode(episode)

        return episode

    def _archive_episode(self, episode: SessionEpisode) -> None:
        """Archive episode to HierarchicalMemory.

        Converts episode content into a format suitable for
        episodic memory storage and later retrieval.
        """
        try:
            # Create memory content from episode
            content = self._episode_to_memory_content(episode)

            # Store in episodic memory (sync for now)
            entry_id = self._store_sync(
                content=content,
                level=MemoryLevel.EPISODIC,
                metadata={
                    "type": "session_episode",
                    "session_id": self.session_id,
                    "episode_id": episode.id,
                    "phase": episode.phase,
                    "message_count": len(episode.messages),
                    "summary": episode.summary[:200],
                },
            )

            logger.info(f"Archived episode {episode.id[:8]} to memory: {entry_id}")

            # Save session
            self._save_session()

        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as e:
            logger.error(f"Failed to archive episode: {e}")

    def _store_sync(self, content: str, level: MemoryLevel, metadata: dict) -> str:
        """Synchronous store to memory.

        Since HierarchicalMemory is async, we use the internal storage.
        """
        import uuid

        entry_id = str(uuid.uuid4())
        entry = MemoryEntry(
            id=entry_id,
            level=level,
            content=content,
            metadata=metadata,
        )

        # Direct access to memory's episodic storage
        if level == MemoryLevel.EPISODIC:
            self.memory._episodic.append(entry)
            # Enforce limit
            while len(self.memory._episodic) > self.memory.episodic_max:
                self.memory._episodic.pop(0)
            self.memory._save_episodic()

        return entry_id

    def _episode_to_memory_content(self, episode: SessionEpisode) -> str:
        """Convert episode to memory-friendly content.

        Args:
            episode: Episode to convert

        Returns:
            Memory content string

        """
        parts = []

        # Header with phase
        if episode.phase:
            parts.append(f"[{episode.phase.upper()}]")

        # Summary
        parts.append(episode.summary or episode.generate_summary())
        parts.append("")

        # Key exchanges (limit to prevent bloat)
        for _i, msg in enumerate(episode.messages[:4]):
            msg.role.upper()
            content = msg.content[:300]
            if msg.role == "user":
                parts.append(f"USER: {content}")
            else:
                parts.append(f"ASSISTANT: {content}")

        # Outcome if in metadata
        if "outcome" in episode.metadata:
            parts.append(f"OUTCOME: {episode.metadata['outcome']}")

        return "\n".join(parts)

    def clear(self) -> None:
        """Clear all session data and delete archive file."""
        self._messages.clear()
        self._episodes.clear()
        self._current_episode = None
        if self._archive_path.exists():
            self._archive_path.unlink()

    def get_session_messages(self) -> list[dict[str, str]]:
        """Get all session messages as dicts.

        Returns:
            List of message dicts with 'role' and 'content'

        """
        return [{"role": m.role, "content": m.content} for m in self._messages]

    def get_recent_episodes(self, count: int = 5) -> list[SessionEpisode]:
        """Get recent episodes.

        Args:
            count: Maximum episodes to return

        Returns:
            List of recent episodes

        """
        return list(reversed(self._episodes[-count:]))

    async def consolidate_to_memory(self) -> int:
        """Consolidate working memory into episodic.

        Returns:
            Number of atoms consolidated

        """
        return await self.memory.consolidate()

    @classmethod
    def load_session(cls, session_id: str) -> SessionMemory:
        """Load a session by ID.

        Args:
            session_id: Session identifier

        Returns:
            SessionMemory instance with loaded state

        """
        return cls(session_id=session_id)

    def format_for_context(
        self,
        max_episodes: int = 3,
        max_tokens: int = 2000,
    ) -> str:
        """Format session context for prompt injection.

        Args:
            max_episodes: Maximum episodes to include
            max_tokens: Token budget

        Returns:
            Formatted context string

        """
        if not self._episodes:
            # Fall back to messages if no episodes
            return self._format_messages_for_context(max_tokens)

        lines = ["", "## Session Context", ""]
        lines.append(f"Session: {self.session_id}")
        lines.append(f"Episodes: {len(self._episodes)}")
        lines.append("")

        # Recent episodes
        recent = self.get_recent_episodes(max_episodes)
        token_estimate = 50  # Header overhead

        for episode in recent:
            episode_text = (
                episode.format_for_context()
                if hasattr(episode, "format_for_context")
                else f"- {episode.summary[:200]}"
            )
            episode_text.split("\n")
            episode_tokens = len(episode_text) // 4

            if token_estimate + episode_tokens > max_tokens:
                break

            lines.append(f"### {episode.phase or 'Episode'}")
            lines.append(episode.summary or episode.generate_summary())
            lines.append("")
            token_estimate += episode_tokens

        lines.append("---")
        return "\n".join(lines)

    def _format_messages_for_context(self, max_tokens: int) -> str:
        """Format messages when no episodes available."""
        lines = ["", "## Recent Messages", ""]

        token_estimate = 50
        for msg in self._messages[-10:]:
            content = msg.content[:200]
            role = msg.role.upper()
            msg_text = f"{role}: {content}"
            msg_tokens = len(msg_text) // 4

            if token_estimate + msg_tokens > max_tokens:
                break

            lines.append(msg_text)
            token_estimate += msg_tokens

        lines.append("")
        lines.append("---")
        return "\n".join(lines)


def create_session_memory(
    session_id: str = "",
    memory: HierarchicalMemory | None = None,
) -> SessionMemory:
    """Factory function to create a SessionMemory.

    Args:
        session_id: Optional session ID (generated if None)
        memory: Optional HierarchicalMemory instance

    Returns:
        Configured SessionMemory

    """
    return SessionMemory(session_id=session_id, memory=memory)
