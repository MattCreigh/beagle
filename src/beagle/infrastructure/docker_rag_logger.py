"""Docker RAG Logger - Captures all Docker operations and learnings to RAG.

This module monitors Docker operations and automatically captures:
- Container startup configuration
- Service dependencies and topology
- Resource allocation decisions
- Performance metrics
- Architectural insights
- Troubleshooting discoveries

All captured data is written to RAG ingestion logs for automatic indexing.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("Beagle.infrastructure.docker_rag_logger")

# Configuration
# v1.2.0 (RG-6, BGL-009): resolve the RAG log dir from the canonical data
# root instead of a hardcoded host path.
from beagle.config.paths import get_data_root as _get_data_root  # ruff: ignore[E402]

RAG_LOG_DIR = _get_data_root() / "rag_logs"


def _get_project_root() -> Path:
    """Lazily resolve project root via canonical env_manager."""
    from ..utils.env_manager import get_workspace_root

    return get_workspace_root()


PROJECT_ROOT = _get_project_root()


class EventType(StrEnum):
    """Types of events to capture to RAG."""

    CONTAINER_START = "container_start"
    CONTAINER_STOP = "container_stop"
    CONTAINER_ERROR = "container_error"
    RING_CREATED = "ring_created"
    RING_ERROR = "ring_error"
    AGENT_TASK = "agent_task"
    AGENT_ERROR = "agent_error"
    ARCHITECTURE_DECISION = "architecture_decision"
    PERFORMANCE_METRIC = "performance_metric"
    TROUBLESHOOTING = "troubleshooting"
    CONFIGURATION = "configuration"


@dataclass
class LearningCapture:
    """Captured learning or insight from Docker operations."""

    event_type: EventType
    source: str  # agent_entrypoint, docker_agent_wrapper, etc.
    agent_type: str | None = None
    component: str = ""
    title: str = ""
    description: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    tags: list[str] = field(default_factory=list)


class DockerRAGLogger:
    """Main logger for Docker operations and learnings."""

    def __init__(self, log_dir: Path | None = None):
        self.log_dir = log_dir or RAG_LOG_DIR
        self.log_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"[DockerRAGLogger] Initialized with log dir: {self.log_dir}")

    def capture(self, learning: LearningCapture) -> None:
        """Capture and store a learning to RAG.

        Args:
            learning: The learning capture to store.

        """
        # Build log entry
        log_entry = {
            "timestamp": learning.timestamp,
            "event_type": learning.event_type.value,
            "source": learning.source,
            "agent_type": learning.agent_type,
            "component": learning.component,
            "title": learning.title,
            "description": learning.description,
            "data": learning.data,
            "tags": learning.tags,
        }

        # Write to log file (JSONL for easy incremental ingestion)
        log_file = self.log_dir / f"docker_{learning.event_type.value}_{int(time.time())}.jsonl"
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(log_entry) + "\n")

        logger.debug(
            f"[DockerRAGLogger] Captured {learning.event_type.value} from {learning.source}"
        )

    def capture_container_start(
        self,
        agent_type: str,
        env_vars: dict[str, str],
        cwd: str,
    ) -> None:
        """Capture container startup information."""
        # Filter sensitive env vars
        safe_env = {
            k: v
            for k, v in env_vars.items()
            if not any(
                sensitive in k.upper()
                for sensitive in ["KEY", "SECRET", "TOKEN", "PASSWORD", "API"]
            )
        }

        learning = LearningCapture(
            event_type=EventType.CONTAINER_START,
            source="agent_entrypoint",
            agent_type=agent_type,
            component="container",
            title=f"Container started: beagle-{agent_type}",
            description=f"Beagle {agent_type} agent container initialized",
            data={
                "cwd": cwd,
                "env_count": len(safe_env),
                "env_sample": list(safe_env.keys())[:10],
                "python_version": (
                    f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
                ),
            },
            tags=["container", "startup", agent_type],
        )

        self.capture(learning)

    def capture_architecture_decision(
        self,
        title: str,
        description: str,
        rationale: str,
        component: str = "",
        data: dict[str, Any] | None = None,
    ) -> None:
        """Capture an architectural decision to RAG.

        Args:
            title: Title of the decision.
            description: Detailed description.
            rationale: Why this decision was made.
            component: Affected component.
            data: Additional data.

        """
        learning = LearningCapture(
            event_type=EventType.ARCHITECTURE_DECISION,
            source="docker_rag_logger",
            title=title,
            description=description,
            component=component,
            data={
                "rationale": rationale,
                **(data or {}),
            },
            tags=["architecture", "design", component],
        )

        self.capture(learning)

    def capture_troubleshooting(
        self,
        issue: str,
        solution: str,
        component: str = "",
        root_cause: str = "",
        learning: str = "",
    ) -> None:
        """Capture troubleshooting information to RAG.

        Args:
            issue: Description of the issue.
            solution: How it was fixed.
            component: Affected component.
            root_cause: Root cause analysis (if known).
            learning: What we learned from this.

        """
        learning = LearningCapture(  # type: ignore[assignment]
            event_type=EventType.TROUBLESHOOTING,
            source="docker_rag_logger",
            title=f"Troubleshooting: {issue}",
            description=solution,
            component=component,
            data={
                "issue": issue,
                "root_cause": root_cause,
                "learning": learning,
            },
            tags=["troubleshooting", "fix", component],
        )

        self.capture(learning)  # type: ignore[arg-type]

    def capture_performance_metric(
        self,
        metric_name: str,
        value: float | int,
        unit: str = "",
        component: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Capture a performance metric to RAG.

        Args:
            metric_name: Name of the metric.
            value: Metric value.
            unit: Unit of measurement.
            component: Component being measured.
            metadata: Additional metadata.

        """
        learning = LearningCapture(
            event_type=EventType.PERFORMANCE_METRIC,
            source="docker_rag_logger",
            title=f"Performance: {metric_name}",
            description=f"{metric_name} = {value} {unit}",
            component=component,
            data={
                "metric_name": metric_name,
                "value": value,
                "unit": unit,
                **(metadata or {}),
            },
            tags=["performance", "metrics", component, metric_name],
        )

        self.capture(learning)

    def capture_configuration(
        self,
        config_type: str,
        config_data: dict[str, Any],
        component: str = "",
    ) -> None:
        """Capture configuration information to RAG.

        Args:
            config_type: Type of configuration (docker, agent, orpheus, etc.)
            config_data: Configuration data.
            component: Component being configured.

        """
        # Filter sensitive values
        safe_config = {}
        for key, value in config_data.items():
            if isinstance(value, str) and any(
                sensitive in key.upper() for sensitive in ["KEY", "SECRET", "TOKEN", "PASSWORD"]
            ):
                safe_config[key] = "[REDACTED]"
            else:
                safe_config[key] = value

        learning = LearningCapture(
            event_type=EventType.CONFIGURATION,
            source="docker_rag_logger",
            title=f"Configuration: {config_type}",
            description=f"{config_type} configuration for {component or 'system'}",
            component=component,
            data={
                "config_type": config_type,
                "config": safe_config,
            },
            tags=["configuration", config_type, component],
        )

        self.capture(learning)


# Global logger instance
_docker_rag_logger: DockerRAGLogger | None = None


def get_docker_rag_logger() -> DockerRAGLogger:
    """Get global Docker RAG logger instance."""
    global _docker_rag_logger
    if _docker_rag_logger is None:
        _docker_rag_logger = DockerRAGLogger()
    return _docker_rag_logger


if __name__ == "__main__":
    # Test the logger
    logger.info("[DockerRAGLogger] Testing capture functionality...")

    rag_logger = get_docker_rag_logger()

    # Test various capture types
    rag_logger.capture_architecture_decision(
        title="Use Orpheus ring buffers for IPC",
        description="Selected Orpheus ring buffers over TCP for inter-agent communication",
        rationale="Zero-copy reads, minimal latency, no network stack overhead",
        component="ipc",
        data={
            "alternative": "TCP sockets",
            "latency_improvement": "~10x",
            "complexity": "medium",
        },
    )

    rag_logger.capture_troubleshooting(
        issue="Ring buffer permission errors during container startup",
        solution="Set 770 permissions on /run/orpheus_ring directory",
        root_cause="Container user not in orpheus-ipc group",
        learning="Always verify group membership and directory permissions for shared memory",
    )

    rag_logger.capture_performance_metric(
        metric_name="container_startup_time",
        value=2.5,
        unit="seconds",
        component="planner",
        metadata={"image_size_mb": 450, "python_version": "3.13"},
    )

    logger.info("[DockerRAGLogger] Test complete - check RAG logs")
