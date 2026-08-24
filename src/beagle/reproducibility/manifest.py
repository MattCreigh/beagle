"""Replay manifest — complete input record for replaying a workflow execution.

Captures all inputs needed to reproduce a workflow run:
query, steering prompt, config snapshot, and per-node inputs.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger("Beagle.reproducibility")

MANIFEST_VERSION = "1.0"


@dataclass(frozen=True)
class NodeInput:
    """Recorded input for a single node execution."""

    node_name: str
    prompt: str
    system_directive: str
    model: str
    temperature: float
    timestamp: float  # Original wall-clock time
    attempt: int


@dataclass
class ReplayManifest:
    """Complete input record for replaying a workflow execution."""

    manifest_version: str = MANIFEST_VERSION
    beagle_version: str = ""
    workflow_id: str = ""
    workflow_name: str = ""
    query: str = ""
    steering_prompt: str = ""
    mode: str = "audit"
    seed: str = ""  # Deterministic seed for this execution
    config_snapshot: dict = field(default_factory=dict)
    node_inputs: list[NodeInput] = field(default_factory=list)
    started_at: float = 0.0
    completed_at: float = 0.0

    def to_json(self) -> str:
        """Serialize to JSON."""
        return json.dumps(asdict(self), indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, data: str) -> ReplayManifest:
        """Deserialize from JSON."""
        raw = json.loads(data)
        # Reconstruct NodeInput objects from plain dicts
        node_inputs = [NodeInput(**ni) for ni in raw.pop("node_inputs", [])]
        return cls(**raw, node_inputs=node_inputs)

    def save(self, path: Path) -> None:
        """Save manifest to file (atomic write, 0o600 permissions)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        data = self.to_json()

        # Atomic write via temp file in same directory
        fd, tmp_path = tempfile.mkstemp(
            dir=str(path.parent),
            prefix=".manifest_",
            suffix=".tmp",
        )
        try:
            os.write(fd, data.encode("utf-8"))
            os.close(fd)
            fd = -1  # Mark as closed
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, str(path))
            logger.info(f"Manifest saved to {path} (0o600)")
        except Exception as err:  # broad catch intentional
            # Clean up temp file on failure
            if fd >= 0:
                os.close(fd)
            with suppress(OSError):
                os.unlink(tmp_path)
            raise RuntimeError(f"Failed to save manifest to {path}") from err

    @classmethod
    def load(cls, path: Path) -> ReplayManifest:
        """Load manifest from file."""
        try:
            data = path.read_text(encoding="utf-8")
        except OSError as err:
            raise RuntimeError(f"Failed to load manifest from {path}") from err

        return cls.from_json(data)


__all__ = [
    "MANIFEST_VERSION",
    "NodeInput",
    "ReplayManifest",
]
