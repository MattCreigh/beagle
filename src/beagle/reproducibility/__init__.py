"""Beagle Deterministic Reproducibility.

Provides replay recording, deterministic mode control, and replay
execution for reproducible workflow runs.

Usage:
    from beagle.reproducibility import (
        get_replay_recorder,
        set_deterministic_mode,
        ReplayEngine,
        ReplayManifest,
    )

    # Enable deterministic mode for reproducible execution
    set_deterministic_mode(enabled=True, seed="my-seed")

    # Record a workflow execution
    recorder = get_replay_recorder()
    recorder.start_recording(workflow_id="abc123", query="audit auth module")

    # ... workflow runs ...

    manifest = recorder.stop_recording()

    # Replay the workflow
    engine = ReplayEngine(manifest)
    result = await engine.replay()
"""

from __future__ import annotations

from .determinism import (
    BEAGLE_NAMESPACE,
    deterministic_hash,
    deterministic_temperature,
    deterministic_timestamp,
    deterministic_uuid,
    get_seed,
    is_deterministic,
    set_deterministic_mode,
)
from .manifest import MANIFEST_VERSION, NodeInput, ReplayManifest
from .recorder import DEFAULT_REPLAY_DIR, ReplayRecorder, get_replay_recorder
from .replay import ReplayEngine

__all__ = [
    "BEAGLE_NAMESPACE",
    "DEFAULT_REPLAY_DIR",
    "MANIFEST_VERSION",
    "NodeInput",
    "ReplayEngine",
    "ReplayManifest",
    "ReplayRecorder",
    "deterministic_hash",
    "deterministic_temperature",
    "deterministic_timestamp",
    "deterministic_uuid",
    "get_replay_recorder",
    "get_seed",
    "is_deterministic",
    "set_deterministic_mode",
]
