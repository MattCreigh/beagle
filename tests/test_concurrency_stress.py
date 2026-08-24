"""Concurrency stress tests for Beagle — Section 7.

Validates thread safety, data integrity, and liveness under concurrent load for:
- 7.1: Concurrent workflow execution (DAGOrchestrator)
- 7.2: SQLite WAL concurrent access (TaskStore)
- 7.3: EventBus contention under heavy publish/subscribe
- 7.4: Rate limiter parallel load (WorkflowRateLimiter + TokenBucket)
"""

from __future__ import annotations

import asyncio
import threading
import time
from unittest.mock import MagicMock

from beagle.events.bus import EventBus
from beagle.events.events import (
    WorkflowCompleted,
    WorkflowStarted,
)
from beagle.infrastructure.task_store import TaskStore
from beagle.utils.rate_limiter import (
    TokenBucket,
    WorkflowRateLimiter,
)

# ═══════════════════════════════════════════════════════════════════════════
# Section 7.1: Concurrent workflow execution
# ═══════════════════════════════════════════════════════════════════════════


class TestConcurrentWorkflowExecution:
    """Test multiple DAGOrchestrator instances running simultaneously."""

    def test_multiple_orchestrators_instantiate_concurrently(self):
        """Multiple DAGOrchestrator instances can be created in parallel."""
        from beagle.core.autonomous_orchestrator import DAGOrchestrator

        orchestrators = []
        errors = []

        def create_orchestrator(wid):
            try:
                dag = DAGOrchestrator(
                    budget_usd=1.0, workflow_id=f"concurrent-{wid}", model="test-model"
                )
                orchestrators.append(dag)
            except Exception as e:  # ruff: ignore[BLE001]
                errors.append(e)

        threads = [threading.Thread(target=create_orchestrator, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0, f"Errors during concurrent creation: {errors}"
        assert len(orchestrators) == 10
        # Each should have a unique workflow ID
        wf_ids = {o.workflow_id for o in orchestrators}
        assert len(wf_ids) == 10, "Workflow IDs should be unique"

    def test_orchestrator_state_isolation(self):
        """Each DAGOrchestrator's state is independent under concurrent access."""
        from beagle.core.autonomous_orchestrator import DAGOrchestrator

        dag_a = DAGOrchestrator(budget_usd=5.0, workflow_id="isolated-a")
        dag_b = DAGOrchestrator(budget_usd=10.0, workflow_id="isolated-b")

        # Modify state on each
        dag_a.state.errors.append("error-a")
        dag_b.state.errors.append("error-b")

        # Verify isolation
        assert "error-a" in dag_a.state.errors
        assert "error-b" in dag_b.state.errors
        assert "error-b" not in dag_a.state.errors
        assert "error-a" not in dag_b.state.errors

    def test_agent_call_counter_concurrent_increment(self):
        """Agent call counter handles concurrent increments correctly."""

        async def _test():
            from beagle.core.autonomous_orchestrator import (
                cleanup_agent_call_counter,
                increment_agent_call,
                reset_agent_call_counter,
            )

            await reset_agent_call_counter("concurrent-test")
            errors = []

            async def increment_many(n):
                for _ in range(n):
                    try:
                        await increment_agent_call("concurrent-test")
                    except Exception as e:  # ruff: ignore[BLE001]
                        errors.append(e)

            # 5 coroutines each incrementing 20 times = 100 total
            tasks = [increment_many(20) for _ in range(5)]
            await asyncio.gather(*tasks)

            from beagle.core.autonomous_orchestrator import (
                get_agent_call_count,
            )

            count = await get_agent_call_count("concurrent-test")
            assert count == 100, f"Expected 100 increments, got {count}"
            assert len(errors) == 0
            await cleanup_agent_call_counter("concurrent-test")

        asyncio.run(_test())

    def test_concurrent_publish_to_shared_event_bus(self):
        """Multiple orchestrators can publish events to the same EventBus safely."""
        bus = EventBus()
        received = []
        lock = threading.Lock()

        def collect(event):
            with lock:
                received.append(event)

        bus.subscribe("*", collect)

        errors = []

        def publish_batch(n):
            try:
                for i in range(n):
                    bus.publish(
                        WorkflowStarted(
                            workflow_id=f"wf-{threading.get_ident()}-{i}",
                            query="stress test",
                        )
                    )
            except Exception as e:  # ruff: ignore[BLE001]
                errors.append(e)

        threads = [threading.Thread(target=publish_batch, args=(25,)) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0, f"Errors during concurrent publish: {errors}"
        # 8 threads x 25 events = 200 total
        with lock:
            assert len(received) == 200, f"Expected 200 events, got {len(received)}"


# ═══════════════════════════════════════════════════════════════════════════
# Section 7.2: SQLite WAL concurrent access
# ═══════════════════════════════════════════════════════════════════════════


class TestSQLiteWALConcurrency:
    """Test TaskStore under concurrent read/write with WAL mode."""

    def test_concurrent_task_creation(self, tmp_path):
        """Multiple threads can create tasks in the same SQLite DB simultaneously."""
        db_path = tmp_path / "test.db"
        store = TaskStore(db_path)
        errors = []
        task_ids = []
        lock = threading.Lock()

        def create_task(n):
            try:
                tid = store.create_task(
                    task_type="workflow",
                    spec={"query": f"concurrent-task-{n}"},
                    constraints=None,
                )
                with lock:
                    task_ids.append(tid)
            except Exception as e:  # ruff: ignore[BLE001]
                errors.append(e)

        threads = [threading.Thread(target=create_task, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0, f"Errors during concurrent task creation: {errors}"
        assert len(task_ids) == 20
        # All task IDs should be unique
        assert len(set(task_ids)) == 20, "Duplicate task IDs detected"
        store.close()

    def test_concurrent_read_write(self, tmp_path):
        """Reads and writes can happen concurrently without corruption."""
        db_path = tmp_path / "test_rw.db"
        store = TaskStore(db_path)

        # Pre-create tasks
        task_ids = []
        for i in range(5):
            tid = store.create_task(
                task_type="workflow",
                spec={"query": f"preload-{i}"},
            )
            task_ids.append(tid)

        read_results = []
        errors = []
        lock = threading.Lock()

        def reader():
            try:
                for tid in task_ids:
                    task = store.get_task(tid)
                    with lock:
                        read_results.append(task is not None)
            except Exception as e:  # ruff: ignore[BLE001]
                errors.append(e)

        def writer():
            try:
                for i in range(5):
                    store.create_task(
                        task_type="skill",
                        spec={"query": f"new-task-{i}"},
                    )
            except Exception as e:  # ruff: ignore[BLE001]
                errors.append(e)

        threads = [
            threading.Thread(target=reader),
            threading.Thread(target=writer),
            threading.Thread(target=reader),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0, f"Errors during concurrent read/write: {errors}"
        # All reads of pre-created tasks should succeed
        assert all(read_results), "Some reads returned None unexpectedly"
        store.close()

    def test_concurrent_status_updates_no_corruption(self, tmp_path):
        """Concurrent status updates on different tasks don't corrupt data."""
        db_path = tmp_path / "test_status.db"
        store = TaskStore(db_path)

        # Create tasks first
        task_ids = []
        for i in range(10):
            tid = store.create_task(
                task_type="workflow",
                spec={"query": f"status-test-{i}"},
            )
            task_ids.append(tid)

        errors = []

        def update_status(tid, status):
            try:
                store.update_task_status(tid, status)
            except Exception as e:  # ruff: ignore[BLE001]
                errors.append(e)

        # Update all 10 tasks to "running" concurrently
        threads = [
            threading.Thread(target=update_status, args=(tid, "running")) for tid in task_ids
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0, f"Errors during status updates: {errors}"

        # Verify all tasks are now "running"
        for tid in task_ids:
            task = store.get_task(tid)
            assert task is not None
            assert task["status"] == "running", f"Task {tid} status not 'running'"
        store.close()


# ═══════════════════════════════════════════════════════════════════════════
# Section 7.3: EventBus contention under heavy load
# ═══════════════════════════════════════════════════════════════════════════


class TestEventBusContention:
    """Stress-test EventBus publish/subscribe under contention."""

    def test_high_volume_publish_no_loss(self):
        """200 events from 10 threads with 1 subscriber — no events lost."""
        bus = EventBus()
        received = []
        lock = threading.Lock()

        def collect(event):
            with lock:
                received.append(event)

        bus.subscribe("*", collect)

        errors = []

        def publish_batch(thread_id):
            try:
                for i in range(20):
                    bus.publish(
                        WorkflowStarted(
                            workflow_id=f"t{thread_id}-{i}",
                            query="stress",
                        )
                    )
            except Exception as e:  # ruff: ignore[BLE001]
                errors.append(e)

        threads = [threading.Thread(target=publish_batch, args=(t,)) for t in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert len(received) == 200, f"Expected 200 events, got {len(received)}"

    def test_concurrent_subscribe_and_publish(self):
        """Subscribing while publishing doesn't deadlock or crash."""
        bus = EventBus()
        publish_errors = []
        subscribe_errors = []

        def publish_loop():
            try:
                for i in range(100):
                    bus.publish(WorkflowStarted(workflow_id=f"p-{i}", query="q"))
            except Exception as e:  # ruff: ignore[BLE001]
                publish_errors.append(e)

        def subscribe_loop():
            try:
                for _i in range(20):
                    bus.subscribe("workflow.*", MagicMock())
            except Exception as e:  # ruff: ignore[BLE001]
                subscribe_errors.append(e)

        pub_threads = [threading.Thread(target=publish_loop) for _ in range(3)]
        sub_threads = [threading.Thread(target=subscribe_loop) for _ in range(2)]
        all_threads = pub_threads + sub_threads

        for t in all_threads:
            t.start()
        for t in all_threads:
            t.join()

        assert len(publish_errors) == 0, f"Publish errors: {publish_errors}"
        assert len(subscribe_errors) == 0, f"Subscribe errors: {subscribe_errors}"

    def test_ring_buffer_overflow_does_not_crash(self):
        """Publishing more events than ring buffer capacity doesn't crash."""
        bus = EventBus()
        # Ring buffer maxlen=1000, publish 2000 events
        for i in range(2000):
            bus.publish(WorkflowStarted(workflow_id=f"overflow-{i}", query="q"))

        # Should still function — subscribe and receive new events
        received = []
        lock = threading.Lock()

        def collect(event):
            with lock:
                received.append(event)

        bus.subscribe("*", collect)
        bus.publish(WorkflowStarted(workflow_id="post-overflow", query="q"))

        assert len(received) > 0, "Should receive at least 1 event after overflow"

    def test_multiple_event_types_filtering(self):
        """Different event types route to correct subscribers under load."""
        bus = EventBus()
        started_received = []
        completed_received = []
        all_received = []
        lock = threading.Lock()

        def on_started(event):
            with lock:
                started_received.append(event)

        def on_completed(event):
            with lock:
                completed_received.append(event)

        def on_all(event):
            with lock:
                all_received.append(event)

        bus.subscribe("workflow.started", on_started)
        bus.subscribe("workflow.completed", on_completed)
        bus.subscribe("*", on_all)

        # Publish 50 started + 50 completed
        for i in range(50):
            bus.publish(WorkflowStarted(workflow_id=f"w-{i}"))
            bus.publish(WorkflowCompleted(workflow_id=f"w-{i}"))

        with lock:
            assert len(started_received) == 50, f"Expected 50 started, got {len(started_received)}"
            assert len(completed_received) == 50, (
                f"Expected 50 completed, got {len(completed_received)}"
            )
            assert len(all_received) == 100, f"Expected 100 total, got {len(all_received)}"


# ═══════════════════════════════════════════════════════════════════════════
# Section 7.4: Rate limiter parallel load
# ═══════════════════════════════════════════════════════════════════════════


class TestTokenBucketConcurrency:
    """TokenBucket under concurrent access from multiple threads."""

    def test_concurrent_consume_no_overdraft(self):
        """Multiple threads consuming from the same bucket never exceed capacity."""
        bucket = TokenBucket(capacity=100.0, refill_rate=1000.0)
        consumed_counts = []
        lock = threading.Lock()
        errors = []

        def consume_batch(n):
            local_consumed = 0
            try:
                for _ in range(n):
                    if bucket.consume(1):
                        local_consumed += 1
            except Exception as e:  # ruff: ignore[BLE001]
                errors.append(e)
            with lock:
                consumed_counts.append(local_consumed)

        # 10 threads each trying to consume 20 tokens from a bucket of 100
        threads = [threading.Thread(target=consume_batch, args=(20,)) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0, f"Errors during consume: {errors}"
        total_consumed = sum(consumed_counts)
        # Can never consume more than initial capacity + refill
        # With 100 initial + refill at 1000/s for ~0.01s = up to ~10 extra
        assert total_consumed <= 120, f"Over-consumption: {total_consumed} tokens consumed"

    def test_concurrent_consume_and_refill(self):
        """Refill and consume happening simultaneously stays consistent."""
        bucket = TokenBucket(capacity=50.0, refill_rate=50.0)
        results = []
        lock = threading.Lock()
        errors = []

        def consume_loop():
            try:
                for _ in range(100):
                    result = bucket.consume(1)
                    with lock:
                        results.append(result)
            except Exception as e:  # ruff: ignore[BLE001]
                errors.append(e)

        # Start consuming thread while time passes (refills happen)
        t = threading.Thread(target=consume_loop)
        t.start()
        time.sleep(0.1)  # Allow some refills
        t.join()

        assert len(errors) == 0
        # Some consumes should succeed, some may fail (bucket drained)
        successes = sum(1 for r in results if r)
        assert successes > 0, "At least some consumes should succeed"

    def test_available_is_thread_safe(self):
        """available() calls from multiple threads don't crash or corrupt state."""
        bucket = TokenBucket(capacity=10.0, refill_rate=100.0)
        results = []
        lock = threading.Lock()
        errors = []

        def check_available():
            try:
                for _ in range(50):
                    avail = bucket.available()
                    with lock:
                        results.append(avail)
            except Exception as e:  # ruff: ignore[BLE001]
                errors.append(e)

        threads = [threading.Thread(target=check_available) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert len(results) == 250  # 5 threads x 50 checks
        # All available values should be non-negative
        assert all(v >= 0 for v in results), "Available tokens went negative"


class TestWorkflowRateLimiterConcurrency:
    """WorkflowRateLimiter under parallel load."""

    def test_concurrent_acquire_different_workflows(self):
        """Multiple workflows can acquire rate limit slots concurrently."""
        limiter = WorkflowRateLimiter(
            default_requests_per_second=100.0,
            default_burst_size=50,
        )
        results = []
        errors = []
        lock = threading.Lock()

        def acquire_workflow(wid):
            try:
                wait = limiter.acquire(estimated_tokens=10, workflow_id=wid)
                with lock:
                    results.append((wid, wait))
            except Exception as e:  # ruff: ignore[BLE001]
                with lock:
                    errors.append((wid, e))

        # 20 threads, each acquiring for a different workflow
        threads = [threading.Thread(target=acquire_workflow, args=(f"wf-{i}",)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0, f"Acquire errors: {errors}"
        assert len(results) == 20, f"Expected 20 results, got {len(results)}"
        # All waits should be >= 0 (0 for immediate, >0 if rate-limited)
        for wid, wait_time in results:
            assert wait_time >= 0, f"Negative wait for {wid}: {wait_time}"

        # Cleanup
        for i in range(20):
            limiter.cleanup_workflow(f"wf-{i}")

    def test_circuit_breaker_under_concurrent_failures(self):
        """Circuit breaker state transitions correctly under concurrent failures."""
        limiter = WorkflowRateLimiter(
            default_requests_per_second=100.0,
            default_burst_size=50,
            circuit_breaker_threshold=3,
            circuit_breaker_timeout=1.0,
        )

        errors = []

        def record_failure(entity):
            try:
                for _ in range(2):
                    limiter.record_failure(entity)
            except Exception as e:  # ruff: ignore[BLE001]
                errors.append(e)

        # 3 threads each recording 2 failures for the same entity
        threads = [threading.Thread(target=record_failure, args=("svc-a",)) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        # After 6 failures, circuit should be open for svc-a
        assert limiter.is_circuit_open("svc-a"), "Circuit should be open after 6 failures"

    def test_rate_limiter_stats_consistency(self):
        """Stats remain consistent under concurrent acquire/release cycles."""
        limiter = WorkflowRateLimiter(
            default_requests_per_second=50.0,
            default_burst_size=20,
        )
        errors = []
        lock = threading.Lock()

        def acquire_and_release(wid):
            try:
                limiter.acquire(estimated_tokens=5, workflow_id=wid)
                limiter.record_success(wid)
            except Exception as e:  # ruff: ignore[BLE001]
                with lock:
                    errors.append((wid, e))

        # 10 threads, each acquiring and recording success
        threads = [
            threading.Thread(target=acquire_and_release, args=(f"wf-{i}",)) for i in range(10)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0, f"Acquire/release errors: {errors}"

        # 10 workflows, each recorded 1 success
        for i in range(10):
            wid = f"wf-{i}"
            assert limiter._success_counts.get(wid, 0) == 1, f"Expected 1 success for {wid}"

        for i in range(10):
            limiter.cleanup_workflow(f"wf-{i}")
