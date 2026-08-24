"""Tests for beagle.events.bus — EventBus publish/subscribe/unsubscribe, topic routing, thread safety."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

from beagle.events.bus import EventBus, get_event_bus
from beagle.events.events import WorkflowStarted

# ── EventBus subscribe / publish / unsubscribe ────────────────────────────


class TestEventBusSubscribePublish:
    """Core subscribe, publish, and unsubscribe behaviour."""

    def test_subscribe_returns_uuid_string(self):
        bus = EventBus()
        callback = MagicMock()
        sub_id = bus.subscribe("workflow.*", callback)
        assert isinstance(sub_id, str)
        assert len(sub_id) > 0

    def test_publish_calls_matching_subscriber(self):
        bus = EventBus()
        callback = MagicMock()
        bus.subscribe("workflow.*", callback)

        event = WorkflowStarted(workflow_id="w1", query="test query")
        bus.publish(event)

        callback.assert_called_once()
        call_event = callback.call_args[0][0]
        assert call_event.event_type == "workflow.started"

    def test_publish_does_not_call_non_matching_subscriber(self):
        bus = EventBus()
        callback = MagicMock()
        bus.subscribe("node.*", callback)

        event = WorkflowStarted(workflow_id="w1", query="test query")
        bus.publish(event)

        callback.assert_not_called()

    def test_unsubscribe_removes_callback(self):
        bus = EventBus()
        callback = MagicMock()
        sub_id = bus.subscribe("workflow.*", callback)

        bus.unsubscribe(sub_id)
        event = WorkflowStarted(workflow_id="w1", query="q")
        bus.publish(event)

        callback.assert_not_called()

    def test_unsubscribe_nonexistent_id_is_noop(self):
        bus = EventBus()
        # Should not raise
        bus.unsubscribe("nonexistent-id")

    def test_multiple_subscribers_receive_event(self):
        bus = EventBus()
        cb1 = MagicMock()
        cb2 = MagicMock()
        bus.subscribe("*", cb1)
        bus.subscribe("workflow.*", cb2)

        event = WorkflowStarted(workflow_id="w1")
        bus.publish(event)

        cb1.assert_called_once()
        cb2.assert_called_once()

    def test_callback_exception_does_not_crash_publish(self):
        bus = EventBus()
        bad_cb = MagicMock(side_effect=RuntimeError("oops"))
        good_cb = MagicMock()
        bus.subscribe("*", bad_cb)
        bus.subscribe("*", good_cb)

        event = WorkflowStarted(workflow_id="w1")
        # Should not raise
        bus.publish(event)

        good_cb.assert_called_once()


# ── Topic routing (fnmatch patterns) ──────────────────────────────────────


class TestEventBusTopicRouting:
    """Wildcard and pattern matching for event types."""

    def test_star_matches_any_event_type(self):
        bus = EventBus()
        callback = MagicMock()
        bus.subscribe("*", callback)

        event = WorkflowStarted(workflow_id="w1")
        bus.publish(event)

        callback.assert_called_once()

    def test_fnmatch_wildcard_prefix(self):
        bus = EventBus()
        callback = MagicMock()
        bus.subscribe("workflow.*", callback)

        event = WorkflowStarted(workflow_id="w1")
        bus.publish(event)

        callback.assert_called_once()

    def test_fnmatch_no_match(self):
        bus = EventBus()
        callback = MagicMock()
        bus.subscribe("node.*", callback)

        event = WorkflowStarted(workflow_id="w1")
        bus.publish(event)

        callback.assert_not_called()

    def test_exact_match_pattern(self):
        bus = EventBus()
        callback = MagicMock()
        bus.subscribe("workflow.started", callback)

        event = WorkflowStarted(workflow_id="w1")
        bus.publish(event)

        callback.assert_called_once()


# ── Ring buffer replay on subscribe ───────────────────────────────────────


class TestEventBusRingBuffer:
    """Events in the ring buffer are replayed to new subscribers."""

    def test_replay_on_subscribe(self):
        bus = EventBus()
        # Publish before anyone subscribes — goes into ring buffer
        event = WorkflowStarted(workflow_id="w1")
        bus.publish(event)

        callback = MagicMock()
        bus.subscribe("workflow.*", callback)

        # Callback should be called due to replay
        callback.assert_called_once()

    def test_no_replay_for_non_matching_pattern(self):
        bus = EventBus()
        event = WorkflowStarted(workflow_id="w1")
        bus.publish(event)

        callback = MagicMock()
        bus.subscribe("node.*", callback)

        callback.assert_not_called()


# ── Thread safety ─────────────────────────────────────────────────────────


class TestEventBusThreadSafety:
    """Concurrent publish and subscribe should be safe."""

    def test_concurrent_publish(self):
        bus = EventBus()
        callback = MagicMock()
        bus.subscribe("*", callback)

        errors = []

        def publish_many(n):
            try:
                for _i in range(n):
                    bus.publish(WorkflowStarted(workflow_id=f"w{_i}"))
            except Exception as e:  # ruff: ignore[BLE001]
                errors.append(e)

        threads = [threading.Thread(target=publish_many, args=(50,)) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        # All 200 events should have been delivered
        assert callback.call_count == 200

    def test_concurrent_subscribe_and_publish(self):
        bus = EventBus()
        errors = []

        def subscribe_many():
            try:
                for _i in range(50):
                    bus.subscribe("*", MagicMock())
            except Exception as e:  # ruff: ignore[BLE001]
                errors.append(e)

        def publish_many():
            try:
                for _i in range(50):
                    bus.publish(WorkflowStarted(workflow_id=f"w{_i}"))
            except Exception as e:  # ruff: ignore[BLE001]
                errors.append(e)

        t1 = threading.Thread(target=subscribe_many)
        t2 = threading.Thread(target=publish_many)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert len(errors) == 0


# ── get_event_bus singleton ────────────────────────────────────────────────


class TestGetEventBus:
    """Module-level singleton factory."""

    def test_get_event_bus_returns_event_bus(self):
        bus = get_event_bus()
        assert isinstance(bus, EventBus)

    def test_get_event_bus_returns_singleton(self):
        bus1 = get_event_bus()
        bus2 = get_event_bus()
        assert bus1 is bus2
