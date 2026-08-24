# Copyright (c) 2026 Matt Creigh. All rights reserved.
"""Data records held in the Beacon store.

See plans/beagle-beacon-coordination.xml WP-2. The field list on
:class:`AgentRecord` is closed by hard constraint C-03: no secret material,
no environment dict, no raw command line — ever. Extending it later is a
deliberate decision, not a drive-by addition.
"""

from __future__ import annotations

from dataclasses import dataclass

_COLOUR_PALETTE = (
    "red",
    "green",
    "yellow",
    "blue",
    "magenta",
    "cyan",
    "orange",
    "purple",
)


def stable_colour(agent_id: str) -> str:
    """Return a stable colour name for an agent id.

    The same agent id always maps to the same colour, so every observer
    (the CLI, another agent's roster view) draws the same agent the same
    way across independent processes.

    Args:
        agent_id: The full uuid4() agent identifier.

    Returns:
        One of a fixed eight-colour palette.

    """
    return _COLOUR_PALETTE[hash(agent_id) % len(_COLOUR_PALETTE)]


@dataclass(frozen=True)
class AgentRecord:
    """Live metadata for one connected agent. Held as ``agent:<id>`` in the store.

    This field list is closed (hard constraint C-03). Do not add an
    environment dict, a token, or a raw command line — the store and its
    archive are not a secret-safe boundary.
    """

    agent_id: str
    session_id: str
    pid: int
    uid: int
    host: str
    connected_at: str
    last_seen: str
    model: str
    phase: str
    current_plan: str
    current_work: str
    files: tuple[str, ...]
    colour: str

    def to_hash(self) -> dict[str, str]:
        """Serialise to the flat string-to-string mapping Redis HSET expects."""
        return {
            "agent_id": self.agent_id,
            "session_id": self.session_id,
            "pid": str(self.pid),
            "uid": str(self.uid),
            "host": self.host,
            "connected_at": self.connected_at,
            "last_seen": self.last_seen,
            "model": self.model,
            "phase": self.phase,
            "current_plan": self.current_plan,
            "current_work": self.current_work,
            "files": ",".join(self.files),
            "colour": self.colour,
        }

    @classmethod
    def from_hash(cls, data: dict[str, str]) -> AgentRecord:
        """Deserialise from a Redis HGETALL result."""
        files = tuple(f for f in data.get("files", "").split(",") if f)
        return cls(
            agent_id=data["agent_id"],
            session_id=data["session_id"],
            pid=int(data["pid"]),
            uid=int(data["uid"]),
            host=data["host"],
            connected_at=data["connected_at"],
            last_seen=data["last_seen"],
            model=data["model"],
            phase=data["phase"],
            current_plan=data["current_plan"],
            current_work=data["current_work"],
            files=files,
            colour=data["colour"],
        )
