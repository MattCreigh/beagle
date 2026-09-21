#!/usr/bin/env python3
# Copyright (c) 2026 Matt Creigh. All rights reserved.
"""Per-backend latency harness for the four coordination operation shapes.

See plans/beagle-coord-backend-slot.xml WP-B6, measured fact MB-7.
The plan lives in the beagle_dev tree (Projects/beagle_dev/plans/), not here.

MB-7: the shipped fakeredis_unix backend costs 295-536 us/op over the
socket and 95-212 us/op in-process (parent plan facts M-2/M-3). A
replacement backend exists to beat those numbers, so this harness measures
the same four operation shapes against any registered backend and prints
that reference row alongside the measured one, so a run is self-describing
without needing this plan open to interpret it.

STATUS (2026-09-21): the replacement backend the plan targets does not exist
yet — ``beagle/beacon/backends/`` holds only ``fakeredis_unix``. This harness
is therefore the measurement side of an open slot, committed so the reference
row and the target numbers are not lost between sessions. It is inert: it
reads, measures, prints, and mutates nothing.

Usage:
    beagle_venv/bin/python3 scripts/bench_coord_backend.py --backend fakeredis_unix --iterations 5000
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from beagle.beacon.backends import get_driver
from beagle.beacon.keys import resolve_paths

_MB7_SOCKET_RANGE_US = (295, 536)
_MB7_INPROCESS_RANGE_US = (95, 212)

_SHAPES = ("heartbeat write", "file-lock acquire", "roster read", "event append")


def _percentile(samples_us: list[float], pct: float) -> float:
    ordered = sorted(samples_us)
    idx = min(len(ordered) - 1, round(pct / 100 * (len(ordered) - 1)))
    return ordered[idx]


def _time_op(fn, iterations: int) -> list[float]:
    """Run fn() `iterations` times, returning each call's cost in microseconds.

    time.perf_counter_ns is a monotonic clock (never time.time — a wall
    clock can jump backwards under an NTP/DST step, corrupting a duration).
    """
    samples_us = []
    for _ in range(iterations):
        start = time.perf_counter_ns()
        fn()
        end = time.perf_counter_ns()
        samples_us.append((end - start) / 1000.0)
    return samples_us


def _report(shape: str, samples_us: list[float]) -> None:
    median = statistics.median(samples_us)
    p99 = _percentile(samples_us, 99)
    ops_per_sec = 1_000_000 / median if median > 0 else float("inf")
    print(
        f"{shape:<20} median={median:8.2f} us/op  p99={p99:8.2f} us/op  {ops_per_sec:10.0f} ops/sec"
    )


def run(backend_name: str, iterations: int) -> None:
    driver = get_driver(backend_name)
    workdir = Path(f"/tmp/bench-coord-backend-{uuid.uuid4()}")
    workdir.mkdir(parents=True, exist_ok=True)
    paths = resolve_paths(workdir)
    server = None
    thread = None
    if driver.capabilities.requires_server:
        import threading

        from beagle.beacon.server import BeaconServer

        server = BeaconServer(workdir)
        server.start()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        deadline = time.monotonic() + 5.0
        gate = threading.Event()
        while time.monotonic() < deadline and not driver.is_live(paths, connect_timeout_s=0.5):
            gate.wait(0.05)

    client = driver.connect(paths, connect_timeout_s=2.0, options={})
    try:
        agent_id = str(uuid.uuid4())
        client.sadd("agent:list", agent_id)
        client.hset(f"agent:{agent_id}", mapping={"phase": "bench"})

        print(f"backend: {backend_name!r} ({driver.capabilities.description})")
        print(f"iterations: {iterations}")
        print(
            f"MB-7 reference: socket {_MB7_SOCKET_RANGE_US[0]}-{_MB7_SOCKET_RANGE_US[1]} us/op, "
            f"in-process {_MB7_INPROCESS_RANGE_US[0]}-{_MB7_INPROCESS_RANGE_US[1]} us/op "
            "(parent plan M-2/M-3 — an aggregate range across all four shapes below, "
            "not a per-shape figure; none was measured to that granularity)"
        )
        print()

        # heartbeat write — mirrors server.py's _apply_heartbeat
        def heartbeat() -> None:
            client.hset(f"agent:{agent_id}", mapping={"phase": "bench"})
            client.expire(f"agent:{agent_id}", 15)
            client.sadd("agent:list", agent_id)

        _report(_SHAPES[0], _time_op(heartbeat, iterations))

        # file-lock acquire — mirrors connector.py's SocketRpcClient.lock_file.
        # Alternates a fresh key per call so every call is a real NX win, not
        # a contended failure after the first (that would measure something
        # else: the "already held" fast-reject path, not acquisition cost).
        lock_counter = [0]

        def lock_acquire() -> None:
            lock_counter[0] += 1
            client.set(f"lock:bench-{lock_counter[0]}", agent_id, nx=True, ex=60)

        _report(_SHAPES[1], _time_op(lock_acquire, iterations))

        # roster read — mirrors connector.py's SocketRpcClient.list_agents
        def roster_read() -> None:
            for aid in client.smembers("agent:list"):
                client.hgetall(f"agent:{aid}")

        _report(_SHAPES[2], _time_op(roster_read, iterations))

        # event append — mirrors server.py's _apply_event
        def event_append() -> None:
            client.lpush("event", '{"kind":"bench"}')
            client.ltrim("event", 0, 499)

        _report(_SHAPES[3], _time_op(event_append, iterations))
    finally:
        client.close()
        if server is not None:
            server.stop()
            thread.join(timeout=5)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", required=True, help="Registered backend name (see REGISTRY)")
    parser.add_argument("--iterations", type=int, default=1000)
    args = parser.parse_args(argv)
    run(args.backend, args.iterations)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
