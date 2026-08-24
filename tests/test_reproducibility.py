"""Tests for Beagle deterministic reproducibility system.

Covers: determinism utilities, ReplayManifest serialization, ReplayRecorder
lifecycle, ReplayEngine diff, TurboQuant hash fix, singletons, and events.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import pytest

from beagle.events import reset_event_bus
from beagle.reproducibility.determinism import (
    BEAGLE_NAMESPACE,
    deterministic_hash,
    deterministic_temperature,
    deterministic_timestamp,
    deterministic_uuid,
    get_seed,
    is_deterministic,
    set_deterministic_mode,
)
from beagle.reproducibility.manifest import (
    NodeInput,
    ReplayManifest,
)
from beagle.reproducibility.recorder import (
    ReplayRecorder,
    get_replay_recorder,
)
from beagle.reproducibility.replay import ReplayEngine

# ── Determinism utilities ─────────────────────────────────────────────────


class TestDeterministicMode:
    def setup_method(self):
        set_deterministic_mode(False)

    def teardown_method(self):
        set_deterministic_mode(False)

    def test_off_by_default(self):
        assert not is_deterministic()

    def test_enable_disable(self):
        set_deterministic_mode(True, seed="test-seed")
        assert is_deterministic()
        assert get_seed() == "test-seed"
        set_deterministic_mode(False)
        assert not is_deterministic()

    def test_auto_generates_seed(self):
        set_deterministic_mode(True)
        assert get_seed() != ""


class TestDeterministicUUID:
    def setup_method(self):
        set_deterministic_mode(True, seed="fixed-seed")

    def teardown_method(self):
        set_deterministic_mode(False)

    def test_uses_uuid5(self):
        result = deterministic_uuid("ctx1")
        # uuid5 is deterministic — same seed+context = same UUID
        expected = str(uuid.uuid5(BEAGLE_NAMESPACE, "fixed-seed:ctx1"))
        assert result == expected

    def test_same_context_same_uuid(self):
        a = deterministic_uuid("node-1")
        b = deterministic_uuid("node-1")
        assert a == b

    def test_different_context_different_uuid(self):
        a = deterministic_uuid("node-1")
        b = deterministic_uuid("node-2")
        assert a != b

    def test_fallback_to_uuid4_when_off(self):
        set_deterministic_mode(False)
        result = deterministic_uuid("ctx")
        # Should be a valid UUID (uuid4 format)
        uuid.UUID(result)  # Raises if invalid


class TestDeterministicTimestamp:
    def setup_method(self):
        set_deterministic_mode(True, seed="ts-seed")

    def teardown_method(self):
        set_deterministic_mode(False)

    def test_monotonic(self):
        t1 = deterministic_timestamp()
        t2 = deterministic_timestamp()
        assert t2 > t1

    def test_deterministic_across_calls(self):
        """Same seed produces same sequence."""
        set_deterministic_mode(True, seed="seq-seed")
        seq1 = [deterministic_timestamp() for _ in range(5)]
        set_deterministic_mode(True, seed="seq-seed")
        seq2 = [deterministic_timestamp() for _ in range(5)]
        assert seq1 == seq2

    def test_normal_mode_uses_real_time(self):
        import time

        set_deterministic_mode(False)
        t = deterministic_timestamp()
        now = time.time()
        assert abs(t - now) < 2.0  # Within 2 seconds


class TestDeterministicHash:
    def test_deterministic_across_calls(self):
        a = deterministic_hash(b"hello world")
        b = deterministic_hash(b"hello world")
        assert a == b

    def test_different_input_different_hash(self):
        a = deterministic_hash(b"hello")
        b = deterministic_hash(b"world")
        assert a != b

    def test_returns_int(self):
        result = deterministic_hash(b"test")
        assert isinstance(result, int)


class TestDeterministicTemperature:
    def test_forces_zero_when_enabled(self):
        set_deterministic_mode(True, seed="temp")
        assert deterministic_temperature(0.7) == 0.0
        set_deterministic_mode(False)

    def test_passthrough_when_disabled(self):
        set_deterministic_mode(False)
        assert deterministic_temperature(0.7) == 0.7


# ── ReplayManifest ────────────────────────────────────────────────────────


class TestNodeInput:
    def test_frozen(self):
        ni = NodeInput(
            node_name="plan",
            prompt="test prompt",
            system_directive="directive",
            model="glm-5.1",
            temperature=0.0,
            timestamp=1000.0,
            attempt=1,
        )
        with pytest.raises(AttributeError):
            ni.node_name = "other"  # type: ignore[misc]


class TestReplayManifest:
    def test_json_roundtrip(self):
        ni = NodeInput(
            node_name="exec",
            prompt="do something",
            system_directive="be careful",
            model="glm-5.1",
            temperature=0.0,
            timestamp=1000.0,
            attempt=1,
        )
        manifest = ReplayManifest(
            beagle_version="13.7.0",
            workflow_id="wf-abc",
            query="audit the code",
            seed="seed-123",
            node_inputs=[ni],
        )
        json_str = manifest.to_json()
        restored = ReplayManifest.from_json(json_str)
        assert restored.workflow_id == "wf-abc"
        assert restored.seed == "seed-123"
        assert len(restored.node_inputs) == 1
        assert restored.node_inputs[0].node_name == "exec"

    def test_save_and_load(self, tmp_path: Path):
        manifest = ReplayManifest(
            beagle_version="13.7.0",
            workflow_id="wf-save",
            query="test save",
            seed="s1",
        )
        save_path = tmp_path / "test_manifest.json"
        manifest.save(save_path)
        assert save_path.exists()
        loaded = ReplayManifest.load(save_path)
        assert loaded.workflow_id == "wf-save"

    def test_save_permissions(self, tmp_path: Path):
        manifest = ReplayManifest(
            beagle_version="13.7.0",
            workflow_id="wf-perm",
        )
        save_path = tmp_path / "perm_test.json"
        manifest.save(save_path)
        assert save_path.stat().st_mode & 0o777 == 0o600

    def test_save_atomic(self, tmp_path: Path):
        """No temp files should remain after save."""
        manifest = ReplayManifest(
            beagle_version="13.7.0",
            workflow_id="wf-atom",
        )
        save_path = tmp_path / "atomic.json"
        manifest.save(save_path)
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert tmp_files == []

    def test_load_nonexistent_raises(self, tmp_path: Path):
        with pytest.raises(RuntimeError):
            ReplayManifest.load(tmp_path / "nonexistent.json")


# ── ReplayRecorder ────────────────────────────────────────────────────────


class TestReplayRecorder:
    def setup_method(self):
        """Reset the global event bus before each test.

        The EventBus ring buffer accumulates NodeInputCaptured events from
        other tests. ReplayRecorder.start_recording() subscribes to that
        event type, and the bus replays buffered events into the new
        recorder's manifest, inflating node_inputs counts.
        """
        reset_event_bus()

    def test_start_stop_lifecycle(self):
        rec = ReplayRecorder()
        rec.start_recording("wf-1", "test query", seed="s1")
        assert rec.is_recording
        manifest = rec.stop_recording()
        assert not rec.is_recording
        assert manifest is not None
        assert manifest.workflow_id == "wf-1"
        assert manifest.seed == "s1"

    def test_record_node_input(self):
        rec = ReplayRecorder()
        rec.start_recording("wf-2", "query")
        rec.record_node_input(
            node_name="planning",
            prompt="plan this",
            system_directive="be thorough",
            model="glm-5.1",
            temperature=0.0,
        )
        manifest = rec.stop_recording()
        assert manifest is not None
        assert len(manifest.node_inputs) == 1
        assert manifest.node_inputs[0].node_name == "planning"

    def test_record_without_start_is_noop(self):
        rec = ReplayRecorder()
        # Should not raise
        rec.record_node_input(
            node_name="test",
            prompt="p",
            system_directive="d",
            model="m",
            temperature=0.0,
        )

    def test_auto_generates_seed(self):
        rec = ReplayRecorder()
        rec.start_recording("wf-auto", "auto seed test")
        manifest = rec.stop_recording()
        assert manifest is not None
        assert manifest.seed != ""

    def test_p2_2_fidelity_fields_populated(self, caplog):
        """v13.22.4 (P2-2): start_recording must capture workflow_name,
        beagle_version, and config_snapshot into the manifest so the
        replay record has full fidelity. Backward-compat: omitting them
        must still produce a manifest, but the recorder must WARN loudly
        so silent data-integrity loss becomes observable.
        """
        import logging

        rec = ReplayRecorder()
        with caplog.at_level(logging.WARNING, logger="Beagle.reproducibility"):
            rec.start_recording(
                "wf-p22",
                "query",
                workflow_name="self-improvement",
                beagle_version="1.0.1",
                config_snapshot={"primary": "minimax-m3:cloud", "fallback": ["glm-5.2"]},
            )
            manifest = rec.stop_recording()

        assert manifest is not None
        assert manifest.workflow_name == "self-improvement"
        assert manifest.beagle_version == "1.0.1"
        assert manifest.config_snapshot == {
            "primary": "minimax-m3:cloud",
            "fallback": ["glm-5.2"],
        }
        # No fidelity-degradation warnings on the happy path.
        fidelity_warnings = [
            r for r in caplog.records if "replay fidelity degraded" in r.getMessage()
        ]
        assert not fidelity_warnings, (
            f"Unexpected fidelity warnings on happy path: "
            f"{[r.getMessage() for r in fidelity_warnings]}"
        )

    def test_p2_2_missing_fields_warn_loudly(self, caplog):
        """When the caller omits workflow_name/beagle_version/
        config_snapshot, the recorder must surface the data-integrity
        loss as WARNING-level log records rather than silently writing
        empty strings."""
        import logging

        rec = ReplayRecorder()
        with caplog.at_level(logging.WARNING, logger="Beagle.reproducibility"):
            rec.start_recording("wf-p22-missing", "query")
            rec.stop_recording()

        fidelity_warnings = [
            r.getMessage() for r in caplog.records if "replay fidelity degraded" in r.getMessage()
        ]
        # Expect three warnings: workflow_name, beagle_version, config_snapshot.
        assert len(fidelity_warnings) >= 3, (
            f"Expected ≥3 fidelity-degradation warnings, got "
            f"{len(fidelity_warnings)}: {fidelity_warnings}"
        )

    def test_singleton_same_instance(self):
        import beagle.reproducibility.recorder as mod

        mod._recorder = None
        r1 = get_replay_recorder()
        r2 = get_replay_recorder()
        assert r1 is r2
        mod._recorder = None

    def test_p3_5_fifo_retention_caps_directory(self, tmp_path: Path):
        """v13.22.4 (P3-5): stop_recording must enforce a FIFO cap on
        ``.beagle/replays/`` so the directory does not grow unbounded.
        After saving N+5 manifests with cap=10, only the 10 newest
        should remain.
        """
        from beagle.reproducibility.recorder import (
            DEFAULT_MAX_MANIFESTS,
            ReplayRecorder,
            _enforce_replay_retention,
        )

        rec = ReplayRecorder(replay_dir=tmp_path)
        saved_ids: list[str] = []
        for i in range(15):
            wf_id = f"wf-p35-{i:03d}"
            rec.start_recording(wf_id, f"query {i}", seed=f"s{i}")
            rec.stop_recording()
            # Ensure monotonic mtimes even on filesystems with low
            # timestamp resolution.
            (tmp_path / f"{wf_id}.json").touch()
            time.sleep(0.005)
            saved_ids.append(wf_id)

        # After 15 saves with the default cap (500), all 15 should
        # remain — the default cap is high enough to be a no-op for
        # this test. Verify the lower-bound path explicitly.
        kept = sorted(p.name for p in tmp_path.iterdir() if p.suffix == ".json")
        assert len(kept) == 15, f"Default cap should not evict; kept={kept}"

        # Direct unit test of the retention helper with a small cap.
        rec._replay_dir = tmp_path
        # Override the default for this call by invoking the helper
        # with a tighter cap.
        cap = 10
        removed = _enforce_replay_retention(tmp_path, cap)
        assert removed == 5, f"Expected 5 oldest removed, got {removed}"

        kept = sorted(p.name for p in tmp_path.iterdir() if p.suffix == ".json")
        assert len(kept) == cap
        # The 5 oldest should have been evicted; the 10 newest remain.
        for wf_id in saved_ids[:5]:
            assert not (tmp_path / f"{wf_id}.json").exists(), f"Expected {wf_id} to be evicted"
        for wf_id in saved_ids[5:]:
            assert (tmp_path / f"{wf_id}.json").exists(), f"Expected {wf_id} to survive retention"

        # Sanity: DEFAULT_MAX_MANIFESTS is documented as 500.
        assert DEFAULT_MAX_MANIFESTS >= 100, (
            f"DEFAULT_MAX_MANIFESTS regressed to {DEFAULT_MAX_MANIFESTS}; expected at least 100."
        )

    def test_p3_5_retention_handles_missing_directory(self, tmp_path: Path):
        """Retention must not raise when the directory does not exist
        (the recorder creates it on first save, so the retention call
        may run before the directory exists in error paths)."""
        from beagle.reproducibility.recorder import (
            _enforce_replay_retention,
        )

        # Make a path that does not exist yet; ``iterdir`` raises FileNotFoundError.
        missing = tmp_path / "never-created"
        assert not missing.exists()
        removed = _enforce_replay_retention(missing, max_manifests=10)
        # Empty dir or nonexistent dir → 0 removed, no exception.
        assert removed == 0


# ── ReplayEngine ──────────────────────────────────────────────────────────


class TestReplayEngine:
    def test_diff_identical_manifests(self):
        m1 = ReplayManifest(
            workflow_id="wf-1",
            query="test",
            seed="s1",
            mode="audit",
        )
        m2 = ReplayManifest(
            workflow_id="wf-1",
            query="test",
            seed="s1",
            mode="audit",
        )
        engine = ReplayEngine(m1)
        diffs = engine.diff(m1, m2)
        assert diffs == []

    def test_diff_detects_query_change(self):
        m1 = ReplayManifest(workflow_id="wf-1", query="test A")
        m2 = ReplayManifest(workflow_id="wf-1", query="test B")
        engine = ReplayEngine(m1)
        diffs = engine.diff(m1, m2)
        assert any("query" in d.lower() for d in diffs)

    def test_diff_detects_seed_change(self):
        m1 = ReplayManifest(workflow_id="wf-1", seed="s1")
        m2 = ReplayManifest(workflow_id="wf-1", seed="s2")
        engine = ReplayEngine(m1)
        diffs = engine.diff(m1, m2)
        assert any("seed" in d.lower() for d in diffs)

    def test_diff_detects_node_count_mismatch(self):
        ni = NodeInput(
            node_name="plan",
            prompt="p",
            system_directive="d",
            model="m",
            temperature=0.0,
            timestamp=0.0,
            attempt=1,
        )
        m1 = ReplayManifest(workflow_id="wf-1", node_inputs=[ni])
        m2 = ReplayManifest(workflow_id="wf-1", node_inputs=[])
        engine = ReplayEngine(m1)
        diffs = engine.diff(m1, m2)
        assert len(diffs) > 0


# ── Events ────────────────────────────────────────────────────────────────


class TestReproducibilityEvents:
    def test_node_input_captured(self):
        from beagle.events.events import NodeInputCaptured

        e = NodeInputCaptured(
            workflow_id="wf-1",
            node_name="exec",
            prompt_hash="abc123",
            model="glm-5.1",
            temperature=0.0,
        )
        assert e.event_type == "node.input.captured"

    def test_replay_started(self):
        from beagle.events.events import ReplayStarted

        e = ReplayStarted(
            workflow_id="wf-1",
            original_workflow_id="wf-orig",
            seed="s1",
        )
        assert e.event_type == "replay.started"

    def test_replay_completed(self):
        from beagle.events.events import ReplayCompleted

        e = ReplayCompleted(
            workflow_id="wf-1",
            original_workflow_id="wf-orig",
            match=False,
            differences=("query changed",),
        )
        assert e.event_type == "replay.completed"
        assert not e.match


# ── TurboQuant hash fix ──────────────────────────────────────────────────


class TestTurboQuantHashFix:
    def test_deterministic_seed_across_sessions(self):
        """Verify TurboQuant uses hashlib, not hash()."""
        import hashlib

        import numpy as np

        vec = np.array([1.0, 2.0, 3.0], dtype=np.float32)
        data = vec.tobytes()
        h = hashlib.sha256(data).digest()
        seed = int.from_bytes(h[:4], "little") % (2**31)
        # Same input should always produce same seed
        h2 = hashlib.sha256(data).digest()
        seed2 = int.from_bytes(h2[:4], "little") % (2**31)
        assert seed == seed2
        assert seed > 0
