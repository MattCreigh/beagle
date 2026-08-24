"""Configurable thresholds for health scoring.

Loads from config.toml under [health] section, with sensible defaults
so it works without configuration.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("Beagle.health")


class HealthThresholds:
    """Configurable thresholds for health scoring.

    All thresholds have sensible defaults so the system works without
    any configuration file. Load via :meth:`from_config` to read from
    config.toml [health] section.
    """

    def __init__(
        self,
        rss_warn_mb: float = 1024.0,
        rss_critical_mb: float = 2048.0,
        fd_warn_pct: float = 0.80,
        fd_critical_pct: float = 0.95,
        thread_warn: int = 100,
        thread_critical: int = 200,
        cache_hit_min: float = 0.30,
        cache_hit_min_lookups: int = 100,
        pool_fail_rate_max: float = 0.20,
        pool_fail_min_runs: int = 10,
        zombie_warn: int = 1,
        check_interval_seconds: int = 60,
        degraded_score: float = 0.6,
        critical_score: float = 0.3,
    ) -> None:
        self.rss_warn_mb = rss_warn_mb
        self.rss_critical_mb = rss_critical_mb
        self.fd_warn_pct = fd_warn_pct
        self.fd_critical_pct = fd_critical_pct
        self.thread_warn = thread_warn
        self.thread_critical = thread_critical
        self.cache_hit_min = cache_hit_min
        self.cache_hit_min_lookups = cache_hit_min_lookups
        self.pool_fail_rate_max = pool_fail_rate_max
        self.pool_fail_min_runs = pool_fail_min_runs
        self.zombie_warn = zombie_warn
        self.check_interval_seconds = check_interval_seconds
        self.degraded_score = degraded_score
        self.critical_score = critical_score

    @classmethod
    def from_config(cls, data: dict | None = None) -> HealthThresholds:
        """Create thresholds from a parsed [health] config dict.

        Missing keys fall back to defaults. Pass None to get defaults.
        """
        if data is None:
            return cls()

        try:
            return cls(
                rss_warn_mb=float(data.get("rss_warn_mb", 1024.0)),
                rss_critical_mb=float(data.get("rss_critical_mb", 2048.0)),
                fd_warn_pct=float(data.get("fd_warn_pct", 0.80)),
                fd_critical_pct=float(data.get("fd_critical_pct", 0.95)),
                thread_warn=int(data.get("thread_warn", 100)),
                thread_critical=int(data.get("thread_critical", 200)),
                cache_hit_min=float(data.get("cache_hit_min", 0.30)),
                cache_hit_min_lookups=int(data.get("cache_hit_min_lookups", 100)),
                pool_fail_rate_max=float(data.get("pool_fail_rate_max", 0.20)),
                pool_fail_min_runs=int(data.get("pool_fail_min_runs", 10)),
                zombie_warn=int(data.get("zombie_warn", 1)),
                check_interval_seconds=int(data.get("check_interval_seconds", 60)),
                degraded_score=float(data.get("degraded_score", 0.6)),
                critical_score=float(data.get("critical_score", 0.3)),
            )
        except (ValueError, TypeError) as exc:
            logger.warning("Failed to parse health thresholds from config: %s", exc)
            return cls()

    def __repr__(self) -> str:
        return (
            f"HealthThresholds("
            f"rss_warn={self.rss_warn_mb}MB, "
            f"rss_critical={self.rss_critical_mb}MB, "
            f"fd_warn={self.fd_warn_pct:.0%}, "
            f"fd_critical={self.fd_critical_pct:.0%}, "
            f"degraded<{self.degraded_score}, "
            f"critical<{self.critical_score})"
        )
