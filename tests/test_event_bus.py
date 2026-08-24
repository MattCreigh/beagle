"""Tests for the Beagle Event Bus and File Emitter."""

import asyncio
import json
import threading
from pathlib import Path

import pytest

from beagle.events import (
    BeagleEvent,
    EventBus,
    NodeCompleted,
    NodeStarted,
    WorkflowStarted,
    get_event_bus,
)
from beagle.events.file_emitter import NDJSONEmitter


def test_get_event_bus_singleton():
    """Test that get_event_bus returns the same instance."""
    bus1 = get_event_bus()
    bus2 = get_event_bus()
    assert bus1 is bus2
    assert isinstance(bus1, EventBus)


def test_event_serialization_roundtrip():
    """Test BeagleEvent to_json and from_dict roundtrip."""
    event = WorkflowStarted(
        workflow_id="test_wf_1",
        query="test query",
        budget_usd=10.0,
        mode="audit",
        metadata={"key": "value"},
    )

    json_str = event.to_json()
    assert isinstance(json_str, str)

    data = json.loads(json_str)
    assert data["event_type"] == "workflow.started"
    assert data["query"] == "test query"

    # Reconstruct
    reconstructed = WorkflowStarted.from_dict(data)
    assert reconstructed.workflow_id == "test_wf_1"
    assert reconstructed.budget_usd == 10.0
    assert reconstructed.metadata == {"key": "value"}


def test_publish_subscribe_exact_match():
    """Test exact topic matching."""
    bus = EventBus()
    received = []

    def callback(e):
        received.append(e)

    bus.subscribe("node.started", callback)

    e1 = NodeStarted(workflow_id="w1", node_name="n1", model="m1")
    e2 = NodeCompleted(workflow_id="w1", node_name="n1", cost=0.1, tokens=100)

    bus.publish(e1)
    bus.publish(e2)  # Should not trigger

    assert len(received) == 1
    assert received[0].node_name == "n1"


def test_publish_subscribe_wildcard():
    """Test wildcard topic matching."""
    bus = EventBus()
    received = []

    def callback(e):
        received.append(e)

    bus.subscribe("node.*", callback)

    e1 = NodeStarted(workflow_id="w1", node_name="n1", model="m1")
    e2 = NodeCompleted(workflow_id="w1", node_name="n1", cost=0.1, tokens=100)
    e3 = WorkflowStarted(workflow_id="w1", query="q")

    bus.publish(e1)
    bus.publish(e2)
    bus.publish(e3)  # Should not trigger

    assert len(received) == 2
    assert isinstance(received[0], NodeStarted)
    assert isinstance(received[1], NodeCompleted)


def test_callback_exception_isolation():
    """Test that a bad callback doesn't crash the bus or other callbacks."""
    bus = EventBus()
    received = []

    def bad_callback(e):
        raise ValueError("I crash")

    def good_callback(e):
        received.append(e)

    bus.subscribe("node.*", bad_callback)
    bus.subscribe("node.*", good_callback)

    bus.publish(NodeStarted(workflow_id="w1", node_name="n1", model="m1"))

    # Bus survived and good callback fired
    assert len(received) == 1


def test_thread_safety_concurrent_publish():
    """Test publishing from multiple threads concurrently."""
    bus = EventBus()
    received = []

    def callback(e):
        received.append(e)

    bus.subscribe("*", callback)

    def publisher(thread_id: int):
        for i in range(100):
            bus.publish(NodeStarted(workflow_id=f"w{thread_id}", node_name=f"n{i}"))

    threads = []
    for i in range(10):
        t = threading.Thread(target=publisher, args=(i,))
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    # 10 threads * 100 events
    assert len(received) == 1000


def test_ring_buffer_replay():
    """Test that late subscribers receive past events from the ring buffer."""
    bus = EventBus()

    # Publish BEFORE subscribing
    bus.publish(NodeStarted(workflow_id="w1", node_name="n1"))
    bus.publish(NodeCompleted(workflow_id="w1", node_name="n1"))
    bus.publish(WorkflowStarted(workflow_id="w1", query="q"))

    received = []

    def callback(e):
        received.append(e)

    # Subscribe to node.* - should replay the first two
    bus.subscribe("node.*", callback)

    assert len(received) == 2
    assert isinstance(received[0], NodeStarted)
    assert isinstance(received[1], NodeCompleted)


def test_unsubscribe():
    """Test unsubscribing from events."""
    bus = EventBus()
    received = []

    def callback(e):
        received.append(e)

    sub_id = bus.subscribe("*", callback)

    bus.publish(NodeStarted(workflow_id="w1", node_name="n1"))
    assert len(received) == 1

    bus.unsubscribe(sub_id)
    bus.publish(NodeStarted(workflow_id="w1", node_name="n2"))

    assert len(received) == 1  # No new events received


@pytest.mark.asyncio
async def test_async_callback():
    """Test that async callbacks work properly."""
    bus = EventBus()
    received = []
    asyncio.get_running_loop()

    async def async_callback(e):
        await asyncio.sleep(0.01)
        received.append(e)

    bus.subscribe("*", async_callback)
    bus.publish(NodeStarted(workflow_id="w1", node_name="n1"))

    # Allow async callback to run
    await asyncio.sleep(0.05)

    assert len(received) == 1


def test_multiple_subscribers():
    """Test multiple subscribers to the same pattern."""
    bus = EventBus()
    r1, r2 = [], []

    bus.subscribe("test.*", lambda e: r1.append(e))
    bus.subscribe("test.*", lambda e: r2.append(e))

    from dataclasses import dataclass

    @dataclass(frozen=True, kw_only=True)
    class TestEvent(BeagleEvent):
        event_type: str = "test.event"

    bus.publish(TestEvent(workflow_id="w1"))

    assert len(r1) == 1
    assert len(r2) == 1


def test_ndjson_emitter_validity(tmp_path):
    """Test that NDJSONEmitter writes valid NDJSON files."""
    emitter = NDJSONEmitter(base_dir=tmp_path)

    e1 = WorkflowStarted(workflow_id="test_wf", query="test")
    e2 = NodeStarted(workflow_id="test_wf", node_name="n1")

    emitter.emit(e1)
    emitter.emit(e2)

    log_file = tmp_path / "test_wf.ndjson"
    assert log_file.exists()

    lines = log_file.read_text().splitlines()
    assert len(lines) == 2

    data1 = json.loads(lines[0])
    data2 = json.loads(lines[1])

    assert data1["event_type"] == "workflow.started"
    assert data2["node_name"] == "n1"


def test_ndjson_emitter_safe_filename(tmp_path):
    """Test that NDJSONEmitter sanitizes workflow IDs for filenames."""
    emitter = NDJSONEmitter(base_dir=tmp_path)

    e1 = WorkflowStarted(workflow_id="../../test/wf#1", query="test")
    emitter.emit(e1)

    # Should strip non-alphanumeric/dash/underscore
    log_file = tmp_path / "testwf1.ndjson"
    assert log_file.exists()


def test_ndjson_emitter_rotation(tmp_path, monkeypatch):
    """Test that NDJSONEmitter rotates files when size exceeded."""
    emitter = NDJSONEmitter(base_dir=tmp_path)

    # Monkeypatch max size to a very small value to force rotation
    monkeypatch.setattr("beagle.events.file_emitter.MAX_FILE_SIZE_BYTES", 50)

    log_file = tmp_path / "test_wf.ndjson"
    backup_file = tmp_path / "test_wf.ndjson.1"

    # Emit first event (creates file)
    e1 = WorkflowStarted(workflow_id="test_wf", query="123")
    emitter.emit(e1)

    assert log_file.exists()
    assert not backup_file.exists()

    # Emit second event (should trigger rotation since limit is 50 bytes)
    e2 = NodeStarted(workflow_id="test_wf", node_name="n1")
    emitter.emit(e2)

    assert log_file.exists()
    assert backup_file.exists()

    # Backup should contain first event
    assert "workflow.started" in backup_file.read_text()
    # Current should contain second event
    assert "node.started" in log_file.read_text()


def test_ndjson_emitter_handles_read_only_dir():
    """Test that NDJSONEmitter doesn't crash on read-only directories."""
    # We simulate read-only by providing a path that's actually a file
    import tempfile

    with tempfile.NamedTemporaryFile() as tmp:
        emitter = NDJSONEmitter(base_dir=Path(tmp.name))

        # This will fail to create dir/file but should be swallowed
        e1 = WorkflowStarted(workflow_id="test", query="q")
        emitter.emit(e1)
        # Reaches here without exception


def test_base_event_dict_filtering():
    """Test that from_dict ignores unknown keys safely."""
    data = {
        "event_type": "workflow.started",
        "workflow_id": "w1",
        "query": "q",
        "unknown_key": "this should be ignored",
    }

    event = WorkflowStarted.from_dict(data)
    assert event.workflow_id == "w1"
    assert not hasattr(event, "unknown_key")
