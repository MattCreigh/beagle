"""SP-5/SP-12: tests for infrastructure/task_notifier.

beagle-spotless-phase2, work package SP-5. The event-driven task notifier had
no direct tests. These exercise TaskEvent serialization, subscribe/publish
delivery, event history, and the async-callback fire-and-forget behaviour that
the asyncio-dangling-task suppressions cover (the callbacks are intentionally
detached).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from beagle.infrastructure.task_notifier import (
    TaskEvent,
    TaskEventType,
    TaskNotifier,
)


def _event(task_id: str, event_type: TaskEventType = TaskEventType.TASK_COMPLETED) -> TaskEvent:
    return TaskEvent(
        event_id=f"e-{task_id}",
        task_id=task_id,
        event_type=event_type,
        timestamp=datetime.now(UTC),
        data={"result": "ok"},
    )


def test_task_event_to_json_round_trip() -> None:
    """TaskEvent serializes to JSON and back."""
    ev = _event("t1")
    restored = TaskEvent.from_json(ev.to_json())
    assert restored.task_id == "t1"
    assert restored.event_type == TaskEventType.TASK_COMPLETED
    assert restored.data == {"result": "ok"}


def test_publish_delivers_to_task_subscription() -> None:
    """A task-specific subscription receives matching events."""
    n = TaskNotifier()
    received: list[TaskEvent] = []

    def cb(event: TaskEvent) -> None:
        received.append(event)

    n.subscribe(
        task_id="t1",
        event_types=[TaskEventType.TASK_COMPLETED],
        callback=cb,
    )
    delivered = n.publish(_event("t1"))
    assert delivered == 1
    assert received[0].task_id == "t1"


def test_publish_does_not_deliver_non_matching_event() -> None:
    """A subscription for completed does not receive a failed event."""
    n = TaskNotifier()
    received: list[TaskEvent] = []

    n.subscribe("t1", [TaskEventType.TASK_COMPLETED], callback=lambda e: received.append(e))
    delivered = n.publish(_event("t1", TaskEventType.TASK_FAILED))
    assert delivered == 0
    assert received == []


def test_subscribe_all_global() -> None:
    """subscribe_all delivers to every task's matching event."""
    n = TaskNotifier()
    received: list[TaskEvent] = []

    n.subscribe_all([TaskEventType.TASK_COMPLETED], callback=lambda e: received.append(e))
    delivered = n.publish(_event("any-task"))
    assert delivered == 1
    assert received[0].task_id == "any-task"


def test_event_history_retained() -> None:
    """Published events are stored in history for late subscribers."""
    n = TaskNotifier()
    n.publish(_event("t1"))
    n.publish(_event("t1"))
    assert len(n._event_history["t1"]) == 2


def test_async_callback_is_scheduled_not_blocked() -> None:
    """An async callback is scheduled (fire-and-forget) within a running loop.

    This locks in the intentional asyncio-dangling-task behaviour: the
    notifier creates a background task and returns immediately without
    awaiting the callback.
    """

    async def cb(event: TaskEvent) -> None:
        await asyncio.sleep(0.001)

    async def run() -> None:
        n = TaskNotifier()
        n.subscribe("t1", [TaskEventType.TASK_COMPLETED], callback=cb)
        delivered = n.publish(_event("t1"))
        assert delivered == 1
        # Give the background task a chance to run.
        await asyncio.sleep(0.01)

    asyncio.run(run())


def test_callback_error_is_swallowed() -> None:
    """A callback raising does not abort delivery to other subscribers."""
    n = TaskNotifier()

    def bad_cb(event: TaskEvent) -> None:
        raise RuntimeError("boom")

    def good_cb(event: TaskEvent) -> None:
        good_cb.called = True  # type: ignore[attr-defined]

    good_cb.called = False  # type: ignore[attr-defined]
    n.subscribe("t1", [TaskEventType.TASK_COMPLETED], callback=bad_cb)
    n.subscribe("t1", [TaskEventType.TASK_COMPLETED], callback=good_cb)
    delivered = n.publish(_event("t1"))
    # delivered counts callbacks that returned without raising; the good one
    # still ran even though a prior callback raised.
    assert delivered == 1
    assert good_cb.called is True  # type: ignore[attr-defined]
