"""Tests for Reproducibility/Replay integration.

Validates that the recorder captures node inputs, manifests save/load,
and the replay engine can load and describe a manifest.
"""

import json
import tempfile
from pathlib import Path

from beagle.events import reset_event_bus
from beagle.reproducibility.manifest import (
    NodeInput,
    ReplayManifest,
)
from beagle.reproducibility.recorder import (
    ReplayRecorder,
    get_replay_recorder,
)


class TestReplayRecorder:
    """Tests for the ReplayRecorder."""

    def setup_method(self):
        """Reset the global event bus before each test.

        The EventBus is a process-global singleton with a ring buffer that
        accumulates events published by other tests (e.g. test_integration's
        BeagleDAGNode.execute() publishes NodeInputCaptured). When
        ReplayRecorder.start_recording() subscribes to "node.input.captured",
        the bus replays all matching events from the ring buffer into the
        new recorder's manifest, inflating node_inputs counts.
        """
        reset_event_bus()

    def test_start_recording_creates_manifest(self):
        recorder = ReplayRecorder()
        recorder.start_recording(
            workflow_id="test-wf-001",
            query="Summarize project status",
        )
        assert recorder.is_recording
        manifest = recorder.manifest
        assert manifest is not None
        assert manifest.workflow_id == "test-wf-001"
        assert "Summarize" in manifest.query

    def test_stop_recording_returns_manifest(self):
        recorder = ReplayRecorder()
        recorder.start_recording(
            workflow_id="test-wf-002",
            query="Test query",
        )
        manifest = recorder.stop_recording()
        assert manifest is not None
        assert manifest.workflow_id == "test-wf-002"
        assert not recorder.is_recording

    def test_record_node_input(self):
        recorder = ReplayRecorder()
        recorder.start_recording(
            workflow_id="test-wf-003",
            query="Run analysis",
        )
        recorder.record_node_input(
            node_name="researcher",
            prompt="Research: {query}",
            system_directive="You are a researcher",
            model="deepseek-v3.2",
            temperature=0.7,
        )
        manifest = recorder.manifest
        assert len(manifest.node_inputs) == 1
        assert manifest.node_inputs[0].node_name == "researcher"
        assert manifest.node_inputs[0].model == "deepseek-v3.2"

    def test_multiple_node_inputs(self):
        recorder = ReplayRecorder()
        recorder.start_recording(
            workflow_id="test-wf-004",
            query="Multi-step query",
        )
        recorder.record_node_input(
            "research", "Research prompt", "You are a researcher", "model-a", 0.5
        )
        recorder.record_node_input(
            "synthesis", "Synthesize prompt", "You are a writer", "model-b", 0.3
        )
        recorder.record_node_input("review", "Review prompt", "You are a reviewer", "model-a", 0.0)
        manifest = recorder.manifest
        assert len(manifest.node_inputs) == 3

    def test_get_replay_recorder_returns_singleton(self):
        """get_replay_recorder should return the same instance."""
        r1 = get_replay_recorder()
        r2 = get_replay_recorder()
        assert r1 is r2


class TestReplayManifest:
    """Tests for ReplayManifest serialization."""

    def test_manifest_save_load_roundtrip(self):
        manifest = ReplayManifest(
            workflow_id="test-manifest-001",
            query="Test query for roundtrip",
            mode="audit",
            seed="abc123",
            node_inputs=[
                NodeInput(
                    node_name="researcher",
                    prompt="Research: {query}",
                    system_directive="You are a researcher",
                    model="deepseek-v3.2",
                    temperature=0.5,
                    timestamp=1234567890.0,
                    attempt=1,
                ),
                NodeInput(
                    node_name="writer",
                    prompt="Write: {research_plan}",
                    system_directive="You are a writer",
                    model="deepseek-v3.2",
                    temperature=0.3,
                    timestamp=1234567891.0,
                    attempt=1,
                ),
            ],
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "test_manifest.json"
            manifest.save(path)

            # Verify file exists and is valid JSON
            assert path.exists()
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            assert data["workflow_id"] == "test-manifest-001"
            assert len(data["node_inputs"]) == 2

            # Load roundtrip
            loaded = ReplayManifest.load(path)
            assert loaded.workflow_id == manifest.workflow_id
            assert loaded.query == manifest.query
            assert len(loaded.node_inputs) == 2
            assert loaded.node_inputs[0].node_name == "researcher"
            assert loaded.node_inputs[1].node_name == "writer"

    def test_manifest_to_json(self):
        manifest = ReplayManifest(
            workflow_id="json-test",
            query="JSON roundtrip test",
            mode="audit",
            seed="",
            node_inputs=[],
        )
        json_str = manifest.to_json()
        parsed = json.loads(json_str)
        assert parsed["workflow_id"] == "json-test"

    def test_manifest_from_json(self):
        json_str = json.dumps(
            {
                "workflow_id": "from-json",
                "query": "From JSON test",
                "mode": "audit",
                "seed": "test-seed",
                "started_at": 0.0,
                "completed_at": 0.0,
                "node_inputs": [],
            }
        )
        manifest = ReplayManifest.from_json(json_str)
        assert manifest.workflow_id == "from-json"
        assert manifest.seed == "test-seed"


class TestReplayEngineConstruction:
    """Tests for ReplayEngine initialization."""

    def test_replay_engine_constructs_from_manifest(self):
        from beagle.reproducibility.replay import ReplayEngine

        manifest = ReplayManifest(
            workflow_id="replay-test",
            query="Replay test query",
            mode="audit",
            seed="replay-seed",
            node_inputs=[
                NodeInput(
                    node_name="researcher",
                    prompt="Research: test",
                    system_directive="",
                    model="deepseek-v3.2",
                    temperature=0.5,
                    timestamp=0.0,
                    attempt=1,
                ),
            ],
        )
        engine = ReplayEngine(manifest)
        assert engine is not None
        assert engine._manifest.workflow_id == "replay-test"

    def test_replay_engine_diff(self):
        """ReplayEngine.diff() should compare two manifests and report differences."""
        from beagle.reproducibility.replay import ReplayEngine

        manifest_a = ReplayManifest(
            workflow_id="diff-test",
            query="Diff test A",
            mode="audit",
            seed="same-seed",
            node_inputs=[
                NodeInput(
                    node_name="researcher",
                    prompt="Research: test",
                    system_directive="",
                    model="deepseek-v3.2",
                    temperature=0.5,
                    timestamp=0.0,
                    attempt=1,
                ),
            ],
        )
        manifest_b = ReplayManifest(
            workflow_id="diff-test",
            query="Diff test B",
            mode="audit",
            seed="same-seed",
            node_inputs=[
                NodeInput(
                    node_name="researcher",
                    prompt="Research: other",
                    system_directive="",
                    model="deepseek-v3.2",
                    temperature=0.5,
                    timestamp=0.0,
                    attempt=1,
                ),
            ],
        )
        engine = ReplayEngine(manifest_a)
        diffs = engine.diff(manifest_a, manifest_b)
        assert isinstance(diffs, list)
        assert len(diffs) > 0  # query and prompt differ
        assert any("query" in d for d in diffs)
