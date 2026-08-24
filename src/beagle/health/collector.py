"""Health snapshot collection from all Beagle subsystems.

Gathers metrics from memory, file descriptors, circuit breakers, rate
limiters, cache, subprocess pool, event bus, and tracking DB into a
single :class:`HealthSnapshot`.  Each subsystem is collected inside a
try/except so one failure cannot prevent collecting the others.
"""

from __future__ import annotations

import asyncio
import logging
import os
import resource
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .thresholds import HealthThresholds

logger = logging.getLogger("Beagle.health")


@dataclass(frozen=True)
class HealthSnapshot:
    """Point-in-time health snapshot of the Beagle process."""

    timestamp: float
    # OS-level
    rss_mb: float
    fd_count: int
    fd_limit: int
    thread_count: int
    zombie_child_count: int
    # Circuit breakers
    circuits: dict  # name -> {state, stats}
    circuits_open: int
    # Rate limiters
    rate_limiter_utilization: float  # 0.0-1.0
    rate_limiter_blocked: int
    # Cache
    cache_hit_rate: float  # 0.0-1.0
    cache_entries: int
    # Subprocess pool
    pool_active: int
    pool_max: int
    pool_completed: int
    pool_failed: int
    # Event bus
    event_bus_subscribers: int
    event_bus_ring_depth: int
    # Tracking DB
    db_stats: dict  # From TrackingDatabase.get_stats()
    # Overall
    health_score: float  # 0.0-1.0 composite score
    degraded_systems: list[str] = field(default_factory=list)
    critical_systems: list[str] = field(default_factory=list)


def _collect_rss_mb() -> float:
    """Collect resident set size in MB from /proc or resource module."""
    try:
        # Try /proc/self/status first (Linux, accurate RSS)
        with open("/proc/self/status", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    # VmRSS is in kB
                    return float(line.split()[1]) / 1024.0
    except (OSError, ValueError, IndexError) as exc:
        logger.warning(
            "Cannot read RSS from /proc/self/status (%s); falling back to the resource "
            "module, which reports peak rather than current RSS.",
            exc,
        )

    try:
        # Fallback: resource module (peak RSS, less accurate)
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    except (OSError, AttributeError):
        return 0.0


def _collect_fd_count() -> int:
    """Count open file descriptors from /proc or subprocess."""
    try:
        return len(os.listdir("/proc/self/fd"))
    except OSError:
        return 0


def _collect_fd_limit() -> int:
    """Get soft file descriptor limit."""
    try:
        soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
        return int(soft)
    except (OSError, AttributeError):
        return 1024  # Common default


def _collect_thread_count() -> int:
    """Count active threads."""
    return threading.active_count()


def _collect_zombie_children() -> int:
    """Count zombie child processes by scanning /proc."""
    try:
        zombie_count = 0
        self_pid = os.getpid()
        # Scan all /proc/{pid}/stat entries for zombie state where
        # ppid matches our pid
        unreadable_entries = 0
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                pid = int(entry)
                with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
                    stat_line = f.read()
                # Parse: pid (comm) state ppid ...
                # Find the last ')' to handle comm with parentheses
                close_paren = stat_line.rfind(")")
                fields = stat_line[close_paren + 2 :].split()
                state = fields[0]
                ppid = int(fields[1])
                if state == "Z" and ppid == self_pid:
                    zombie_count += 1
            except (OSError, ValueError, IndexError):
                # The process exited between listdir and the stat read, or its
                # stat line is not in the expected shape. Either way it is not a
                # zombie child of ours; the count below stays correct.
                unreadable_entries += 1

        if unreadable_entries:
            logger.debug(
                "%d /proc entries were unreadable during the zombie scan",
                unreadable_entries,
            )
        return zombie_count
    except asyncio.CancelledError:
        raise
    except Exception:  # ruff: ignore[BLE001]  # broad catch intentional
        return 0


def _collect_rss_mb_standalone() -> float:
    """Standalone RSS collection for testing without imports."""
    return _collect_rss_mb()


def _collect_circuits() -> tuple[dict, int]:
    """Collect circuit breaker states. Returns (circuits_dict, open_count)."""
    try:
        import asyncio

        from beagle.utils.circuit_breaker import get_all_circuits

        # Try to get circuits — may need an event loop
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None and loop.is_running():
            # We're inside an event loop — can't await easily
            # Try synchronous access to _circuits dict
            from beagle.utils.circuit_breaker import _circuits as circuits_ref

            circuits = {}
            open_count = 0
            for name, cb in circuits_ref.items():
                circuits[name] = cb.get_health_report()
                if cb.is_open:
                    open_count += 1
            return circuits, open_count

        # No running loop — safe to use async
        async def _get_circuits() -> tuple[dict, int]:
            all_cbs = await get_all_circuits()
            circuits = {}
            open_count = 0
            for name, cb in all_cbs.items():
                circuits[name] = cb.get_health_report()
                if cb.is_open:
                    open_count += 1
            return circuits, open_count

        try:
            return asyncio.run(_get_circuits())
        except RuntimeError:
            # Nested event loop — fall back to direct dict access
            from beagle.utils.circuit_breaker import _circuits as circuits_ref

            circuits = {}
            open_count = 0
            for name, cb in circuits_ref.items():
                circuits[name] = cb.get_health_report()
                if cb.is_open:
                    open_count += 1
            return circuits, open_count

    except asyncio.CancelledError:
        raise
    except Exception:  # broad catch intentional
        logger.debug("Failed to collect circuit breaker stats", exc_info=True)
        return {}, 0


def _collect_rate_limiter() -> tuple[float, int]:
    """Collect rate limiter utilization and blocked count.

    Returns (utilization 0.0-1.0, blocked_count).
    """
    try:
        from beagle.utils.rate_limiter import get_rate_limiter

        limiter = get_rate_limiter()
        stats = limiter.stats()
        # Calculate utilization across all workflow/model limiters
        total_available = 0
        total_capacity = 0
        for lim_stats in stats.get("workflows", {}).values():
            total_available += lim_stats.get("available_tokens", 0)
            total_capacity += lim_stats.get("max_tokens", 0)
        for lim_stats in stats.get("models", {}).values():
            total_available += lim_stats.get("available_tokens", 0)
            total_capacity += lim_stats.get("max_tokens", 0)

        utilization = 0.0
        if total_capacity > 0:
            utilization = 1.0 - (total_available / total_capacity)

        # Blocked count from failure tracking
        blocked = 0
        for entity_id, count in limiter._failure_counts.items():
            if limiter.is_circuit_open(entity_id):
                blocked += count

        return utilization, blocked
    except asyncio.CancelledError:
        raise
    except Exception:  # broad catch intentional
        logger.debug("Failed to collect rate limiter stats", exc_info=True)
        return 0.0, 0


def _collect_cache() -> tuple[float, int]:
    """Collect cache hit rate and entry count.

    Returns (hit_rate 0.0-1.0, total_entries).
    """
    try:
        from beagle.utils.cache import get_result_cache

        cache = get_result_cache()
        stats = cache.stats()
        hit_rate = float(stats.get("hit_rate", 0.0))
        if hit_rate > 1.0:
            hit_rate = hit_rate / 100.0  # Convert from percentage
        memory_entries = stats.get("memory", {}).get("entries", 0)
        file_entries = stats.get("file", {}).get("entries", 0)
        total_entries = memory_entries + file_entries
        return hit_rate, total_entries
    except asyncio.CancelledError:
        raise
    except Exception:  # broad catch intentional
        logger.debug("Failed to collect cache stats", exc_info=True)
        return 0.0, 0


def _collect_pool() -> tuple[int, int, int, int]:
    """Collect subprocess pool stats.

    Returns (active, max_workers, completed, failed).
    """
    try:
        from beagle.utils.subprocess_pool import get_pool_stats

        stats = get_pool_stats()
        return (
            stats.get("active", 0),
            stats.get("max_workers", 6),
            stats.get("completed", 0),
            stats.get("failed", 0),
        )
    except asyncio.CancelledError:
        raise
    except Exception:  # broad catch intentional
        logger.debug("Failed to collect pool stats", exc_info=True)
        return 0, 6, 0, 0


def _collect_event_bus() -> tuple[int, int]:
    """Collect event bus subscriber count and ring buffer depth.

    Returns (subscribers, ring_depth).
    """
    try:
        from beagle.events import get_event_bus

        bus = get_event_bus()
        with bus._lock:
            subscriber_count = len(bus._subscribers)
            ring_depth = len(bus._ring_buffer)
        return subscriber_count, ring_depth
    except asyncio.CancelledError:
        raise
    except Exception:  # broad catch intentional
        logger.debug("Failed to collect event bus stats", exc_info=True)
        return 0, 0


def _collect_db_stats() -> dict:
    """Collect tracking database stats."""
    try:
        from beagle.tracking.database import TrackingDatabase

        db = TrackingDatabase.get_instance()
        return db.get_stats()
    except asyncio.CancelledError:
        raise
    except Exception:  # broad catch intentional
        logger.debug("Failed to collect tracking DB stats", exc_info=True)
        return {}


def calculate_health_score(
    snapshot_fields: dict,
    thresholds: HealthThresholds,
) -> tuple[float, list[str], list[str]]:
    """Calculate composite health score from snapshot fields.

    Returns (score, degraded_systems, critical_systems).
    """
    score = 1.0
    degraded: list[str] = []
    critical: list[str] = []

    rss_mb = snapshot_fields.get("rss_mb", 0.0)
    if rss_mb > thresholds.rss_critical_mb:
        score -= 0.5
        critical.append("memory")
    elif rss_mb > thresholds.rss_warn_mb:
        score -= 0.2
        degraded.append("memory")

    fd_count = snapshot_fields.get("fd_count", 0)
    fd_limit = snapshot_fields.get("fd_limit", 1)
    if fd_limit > 0:
        fd_pct = fd_count / fd_limit
        if fd_pct > thresholds.fd_critical_pct:
            score -= 0.5
            critical.append("file_descriptors")
        elif fd_pct > thresholds.fd_warn_pct:
            score -= 0.2
            degraded.append("file_descriptors")

    circuits_open = snapshot_fields.get("circuits_open", 0)
    if circuits_open > 0:
        penalty = min(circuits_open * 0.15, 0.45)
        score -= penalty
        if circuits_open >= 2:
            critical.append("circuit_breakers")
        else:
            degraded.append("circuit_breakers")

    cache_hit_rate = snapshot_fields.get("cache_hit_rate", 1.0)
    # Only penalize if cache has had enough lookups (avoid penalizing at startup)
    cache_entries = snapshot_fields.get("cache_entries", 0)
    if (
        cache_entries >= thresholds.cache_hit_min_lookups
        and cache_hit_rate < thresholds.cache_hit_min
    ):
        score -= 0.1
        degraded.append("cache")

    pool_failed = snapshot_fields.get("pool_failed", 0)
    pool_completed = snapshot_fields.get("pool_completed", 0)
    pool_total = pool_failed + pool_completed
    if pool_total >= thresholds.pool_fail_min_runs and pool_total > 0:
        fail_rate = pool_failed / pool_total
        if fail_rate > thresholds.pool_fail_rate_max:
            score -= 0.15
            critical.append("subprocess_pool")

    zombie_count = snapshot_fields.get("zombie_child_count", 0)
    if zombie_count >= thresholds.zombie_warn:
        penalty = min(zombie_count * 0.1, 0.3)
        score -= penalty
        degraded.append("zombie_processes")
        if zombie_count >= 3:
            critical.append("zombie_processes")

    thread_count = snapshot_fields.get("thread_count", 0)
    if thread_count > thresholds.thread_critical:
        score -= 0.3
        critical.append("threads")
    elif thread_count > thresholds.thread_warn:
        score -= 0.1
        degraded.append("threads")

    # Clamp to [0.0, 1.0]
    score = max(0.0, min(1.0, score))

    return score, degraded, critical


def collect_snapshot(thresholds: HealthThresholds) -> HealthSnapshot:
    """Collect a complete health snapshot from all subsystems.

    Each subsystem is collected inside a try/except so one failure
    cannot prevent collecting the others.  Failed subsystems default
    to safe values (0 for counts, empty dicts, etc.).
    """
    # Collect each subsystem independently
    rss_mb = _collect_rss_mb()
    fd_count = _collect_fd_count()
    fd_limit = _collect_fd_limit()
    thread_count = _collect_thread_count()
    zombie_count = _collect_zombie_children()
    circuits, circuits_open = _collect_circuits()
    rate_limiter_util, rate_limiter_blocked = _collect_rate_limiter()
    cache_hit_rate, cache_entries = _collect_cache()
    pool_active, pool_max, pool_completed, pool_failed = _collect_pool()
    event_bus_subs, event_bus_depth = _collect_event_bus()
    db_stats = _collect_db_stats()

    # Build snapshot fields dict for scoring
    fields = {
        "rss_mb": rss_mb,
        "fd_count": fd_count,
        "fd_limit": fd_limit,
        "thread_count": thread_count,
        "zombie_child_count": zombie_count,
        "circuits_open": circuits_open,
        "cache_hit_rate": cache_hit_rate,
        "cache_entries": cache_entries,
        "pool_failed": pool_failed,
        "pool_completed": pool_completed,
        "rate_limiter_utilization": rate_limiter_util,
    }

    health_score, degraded_systems, critical_systems = calculate_health_score(fields, thresholds)

    return HealthSnapshot(
        timestamp=time.time(),
        rss_mb=rss_mb,
        fd_count=fd_count,
        fd_limit=fd_limit,
        thread_count=thread_count,
        zombie_child_count=zombie_count,
        circuits=circuits,
        circuits_open=circuits_open,
        rate_limiter_utilization=rate_limiter_util,
        rate_limiter_blocked=rate_limiter_blocked,
        cache_hit_rate=cache_hit_rate,
        cache_entries=cache_entries,
        pool_active=pool_active,
        pool_max=pool_max,
        pool_completed=pool_completed,
        pool_failed=pool_failed,
        event_bus_subscribers=event_bus_subs,
        event_bus_ring_depth=event_bus_depth,
        db_stats=db_stats,
        health_score=health_score,
        degraded_systems=degraded_systems,
        critical_systems=critical_systems,
    )
