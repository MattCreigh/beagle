"""Agent Harness — Single agent execution wrapper with metaprompt support.

Provides structured lifecycle management for individual agents, including:
- Metaprompt loading (YAML/TOML)
- Model configuration
- Budget and timeout enforcement
- Checkpoint/recovery support
- Output collection and logging

Integrates with OpenClaw controller via Orpheus IPC for Dockerized execution.
"""

from __future__ import annotations

import asyncio
import logging

# Local imports
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

try:  # proprietary transport — provided by the separately licensed beagle-orpheus wheel
    from beagle_orpheus.compat import OrpheusClient
except ImportError:
    from beagle.infrastructure._orpheus_optional import OrpheusClient
from beagle.metaprompts.task_schema import ModelConfig, TaskSpec

logger = logging.getLogger("Beagle.Harness")


class HarnessState(StrEnum):
    """Agent harness lifecycle states."""

    IDLE = "idle"
    INITIALIZING = "initializing"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class MetapromptFormat(StrEnum):
    """Supported metaprompt formats."""

    YAML = "yaml"
    TOML = "toml"
    MARKDOWN = "markdown"


@dataclass
class Metaprompt:
    """Loaded metaprompt configuration."""

    name: str
    description: str
    format: MetapromptFormat
    content: str  # Raw content
    compiled: str  # Compiled/templated prompt ready for execution
    variables: dict[str, Any] = field(default_factory=dict)
    phases: list[dict[str, str]] = field(default_factory=list)
    model_override: str | None = None

    @classmethod
    def from_yaml(cls, yaml_path: Path, variables: dict[str, Any] | None = None) -> Metaprompt:
        """Load and compile a YAML metaprompt."""
        import yaml

        with open(yaml_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)

        # Extract phases if present
        phases = data.get("phases", [])

        # Compile steering prompt
        steering = data.get("steering", "")
        compiled = cls._compile_prompt(steering, variables or {})

        return cls(
            name=data.get("name", yaml_path.stem),
            description=data.get("description", ""),
            format=MetapromptFormat.YAML,
            content=str(data),
            compiled=compiled,
            variables=variables or {},
            phases=phases,
            model_override=data.get("model"),
        )

    @classmethod
    def from_toml(cls, toml_path: Path, task_spec: TaskSpec = None) -> Metaprompt:  # type: ignore[assignment]
        """Load and compile a TOML task spec as metaprompt."""
        import tomllib

        with open(toml_path, "rb") as f:
            data = tomllib.load(f)

        # Extract prompt configuration
        prompt_config = data.get("prompt", {})
        content = prompt_config.get("content", "")

        # Compile with task variables
        variables = {}
        if task_spec:
            variables = {
                "name": task_spec.name,
                "priority": task_spec.priority.value,
                "tags": ", ".join(task_spec.tags),
            }

        compiled = cls._compile_prompt(content, variables)

        return cls(
            name=data.get("name", toml_path.stem),
            description=data.get("description", ""),
            format=MetapromptFormat.TOML,
            content=str(data),
            compiled=compiled,
            variables=variables,
        )

    @staticmethod
    def _compile_prompt(template: str, variables: dict[str, Any]) -> str:
        """Compile template with variable substitution."""
        result = template
        for key, value in variables.items():
            placeholder = f"{{{{{key}}}}}"
            result = result.replace(placeholder, str(value))
        return result


@dataclass
class AgentConfig:
    """Configuration for a single agent harness."""

    agent_id: str
    agent_type: str  # "workflow", "skill", "delegate"
    model: ModelConfig
    metaprompt: Metaprompt | None = None
    context_files: list[str] = field(default_factory=list)
    timeout_seconds: int = 600
    budget_usd: float = 5.0
    checkpoint_enabled: bool = True

    # Orpheus IPC configuration
    orpheus_ring: str | None = None  # Named ring for IPC
    skylon_endpoint: str | None = None


@dataclass
class HarnessCheckpoint:
    """Checkpoint data for harness recovery."""

    checkpoint_id: str
    agent_id: str
    state: HarnessState
    timestamp: datetime
    context_snapshot: dict[str, Any]
    pending_actions: list[dict[str, Any]]
    metrics: dict[str, float]


class AgentHarness:
    """
    Single agent execution harness.

    Manages the lifecycle of one agent instance, including:
    - Metaprompt loading and compilation
    - Model configuration
    - Checkpoint/recovery
    - Output collection

    Does NOT spawn processes directly — dispatches via Orpheus IPC.
    """

    def __init__(
        self,
        config: AgentConfig,
        orpheus_client: OrpheusClient | None = None,
    ):
        self.config = config
        self.agent_id = config.agent_id
        self.state = HarnessState.IDLE
        self.orchestra_client = orpheus_client

        # Tracking
        self._task_id: str | None = None
        self._start_time: float | None = None
        self._checkpoints: list[HarnessCheckpoint] = []
        self._output: list[str] = []
        self._metrics: dict[str, float] = {}

        # Callbacks
        self._on_complete: Callable[[dict], Awaitable[None]] | None = None
        self._on_error: Callable[[Exception], Awaitable[None]] | None = None

    # --- Lifecycle Methods ---

    async def initialize(self) -> bool:
        """Initialize harness and prepare for execution."""
        self.state = HarnessState.INITIALIZING
        logger.info(f"[Harness:{self.agent_id}] Initializing...")

        try:
            # Verify Orpheus connectivity if available
            if self.orchestra_client:
                connected = await self.orchestra_client.connect()
                if not connected:
                    logger.warning(
                        f"[Harness:{self.agent_id}] Orpheus not available, will queue locally"
                    )

            self.state = HarnessState.IDLE
            return True

        except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
            logger.error(f"[Harness:{self.agent_id}] Initialization failed: {e}")
            self.state = HarnessState.FAILED
            return False

    async def execute(self, prompt_override: str | None = None) -> dict[str, Any]:
        """
        Execute the agent with the configured metaprompt.

        Returns execution result including output and metrics.
        """
        if self.state == HarnessState.RUNNING:
            raise RuntimeError(f"Harness {self.agent_id} already running")

        self.state = HarnessState.RUNNING
        self._start_time = time.monotonic()
        self._task_id = f"harness_{self.agent_id}_{uuid.uuid4().hex}"

        logger.info(f"[Harness:{self.agent_id}] Starting execution: {self._task_id}")

        # Get compiled prompt
        prompt = prompt_override
        if not prompt and self.config.metaprompt:
            prompt = self.config.metaprompt.compiled

        if not prompt:
            raise ValueError(f"No prompt for harness {self.agent_id}")

        try:
            # Dispatch via Orpheus or use OpenClaw controller
            result = await self._dispatch_to_controller(prompt)

            # Collect output
            self._output.append(result.get("output", ""))
            self._metrics["execution_time"] = time.monotonic() - self._start_time

            self.state = HarnessState.COMPLETED
            logger.info(
                f"[Harness:{self.agent_id}] Completed in {self._metrics['execution_time']:.2f}s"
            )

            if self._on_complete:
                await self._on_complete(result)

            return {
                "agent_id": self.agent_id,
                "task_id": self._task_id,
                "state": self.state.value,
                "output": self._output,
                "metrics": self._metrics,
                "result": result,
            }

        except Exception as e:  # broad catch intentional
            logger.error(f"[Harness:{self.agent_id}] Execution failed: {e}")
            self.state = HarnessState.FAILED
            self._metrics["error_time"] = time.monotonic() - self._start_time

            if self._on_error:
                await self._on_error(e)

            raise

    async def pause(self) -> bool:
        """Pause execution (if supported by running task)."""
        if self.state != HarnessState.RUNNING:
            return False

        # Create checkpoint
        await self._create_checkpoint()

        # Request pause via controller
        if self._task_id and self.orchestra_client:
            await self.orchestra_client.call("pause_task", [self._task_id])

        self.state = HarnessState.PAUSED
        return True

    async def resume(self) -> bool:
        """Resume from paused state."""
        if self.state != HarnessState.PAUSED:
            return False

        # Restore from last checkpoint
        if self._checkpoints:
            await self._restore_checkpoint(self._checkpoints[-1])

        # Resume via controller
        if self._task_id and self.orchestra_client:
            await self.orchestra_client.call("resume_task", [self._task_id])

        self.state = HarnessState.RUNNING
        return True

    async def cancel(self, reason: str = "user_request") -> bool:
        """Cancel execution."""
        if self.state not in (HarnessState.RUNNING, HarnessState.PAUSED):
            return False

        # Cancel via controller
        if self._task_id and self.orchestra_client:
            await self.orchestra_client.call("cancel_task", [self._task_id], {"reason": reason})

        self.state = HarnessState.CANCELLED
        logger.info(f"[Harness:{self.agent_id}] Cancelled: {reason}")
        return True

    # --- Checkpoint Management ---

    async def _create_checkpoint(self) -> HarnessCheckpoint:
        """Create a recovery checkpoint."""
        checkpoint = HarnessCheckpoint(
            checkpoint_id=f"ckpt_{uuid.uuid4().hex}",
            agent_id=self.agent_id,
            state=self.state,
            timestamp=datetime.now(UTC),
            context_snapshot={
                "output_count": len(self._output),
                "metrics": self._metrics.copy(),
            },
            pending_actions=[],  # Populated by subclasses
            metrics=self._metrics.copy(),
        )

        self._checkpoints.append(checkpoint)
        logger.debug(f"[Harness:{self.agent_id}] Created checkpoint: {checkpoint.checkpoint_id}")
        return checkpoint

    async def _restore_checkpoint(self, checkpoint: HarnessCheckpoint) -> None:
        """Restore state from checkpoint."""
        self._metrics.update(checkpoint.metrics)
        logger.info(
            f"[Harness:{self.agent_id}] Restored from checkpoint: {checkpoint.checkpoint_id}"
        )

    # --- Controller Integration ---

    async def _dispatch_to_controller(self, prompt: str) -> dict[str, Any]:
        """Dispatch task via OpenClaw controller (Orpheus IPC).

        The controller lives in the separately installed, TOML-gated
        beagle-openclaw plugin — never in the beagle distribution itself.
        """
        # Import here to avoid circular dependency
        try:
            from beagle_openclaw.bridge import OpenClawController
            from beagle_openclaw.bridge import TaskType as OCTaskType
        except ImportError as err:
            raise RuntimeError(
                "OpenClaw controller requested but the beagle-openclaw plugin is "
                "not installed (or not importable). Install the plugin and enable "
                "it via its config.toml. " + f"(underlying error: {err})"
            ) from err

        controller = OpenClawController()

        task_type = OCTaskType.WORKFLOW
        if self.config.agent_type == "skill":
            task_type = OCTaskType.SKILL
        elif self.config.agent_type == "delegate":
            task_type = OCTaskType.DELEGATE

        spec = {
            "query": prompt,
            "model": self.config.model.model,
            "provider": self.config.model.provider,
            "context_files": self.config.context_files,
            "metaprompt": self.config.metaprompt.name if self.config.metaprompt else None,
        }

        constraints = {
            "timeout_seconds": self.config.timeout_seconds,
            "budget_usd": self.config.budget_usd,
        }

        # create_task() persists the task; start_task() dispatches it to
        # Skylon. (There is no create_and_start method on the controller.)
        created = controller.create_task(
            task_type=str(task_type),
            spec=spec,
            constraints=constraints,
        )
        raw_task_id = created.get("task_id")
        task_id = str(raw_task_id) if raw_task_id is not None else ""
        controller.start_task(task_id)

        # Wait for completion
        while True:
            status = controller.get_task_status(task_id)
            if status.get("status") in ("completed", "failed", "cancelled"):
                return status
            await asyncio.sleep(2)

    # --- Callbacks ---

    def on_complete(self, callback: Callable[[dict], Awaitable[None]]) -> AgentHarness:
        """Set completion callback."""
        self._on_complete = callback
        return self

    def on_error(self, callback: Callable[[Exception], Awaitable[None]]) -> AgentHarness:
        """Set error callback."""
        self._on_error = callback
        return self

    # --- Status ---

    def get_status(self) -> dict[str, Any]:
        """Get current harness status."""
        return {
            "agent_id": self.agent_id,
            "state": self.state.value,
            "task_id": self._task_id,
            "uptime": time.monotonic() - self._start_time if self._start_time else 0,
            "output_lines": len(self._output),
            "checkpoint_count": len(self._checkpoints),
            "metrics": self._metrics,
        }
