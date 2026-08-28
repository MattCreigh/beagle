"""Steering input sources for Beagle mid-workflow guidance.

Supports multiple input mechanisms:
- File-based (`.beagle/steer.md`)
- Environment variable
- TUI input channel
- API/webhook
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .types import SteeringDirective

logger = logging.getLogger("Beagle.steering.sources")


class SteeringSource(ABC):
    """Abstract base for steering input sources."""

    @abstractmethod
    def read(self) -> SteeringDirective | None:
        """Read steering directive from source. Returns None if no guidance."""
        pass

    @abstractmethod
    def acknowledge(self) -> None:
        """Mark guidance as consumed so it's not re-read."""
        pass


@dataclass
class FileSteeringSource(SteeringSource):
    """File-based steering via `.beagle/steer.md` in workspace root.

    File format:
    ```markdown
    # Steering Guidance

    ## Priority
    Focus on database query performance.

    ## Skip Nodes
    - ui-testing
    - legacy-reports

    ## Budget Override
    Increase budget to 15.00 for this run.

    ## Stop After
    synthesis
    ```
    """

    path: Path = field(default_factory=lambda: Path(".beagle/steer.md"))
    workflow_id: str = "default"

    def read(self) -> SteeringDirective | None:
        """Check for steering file and parse if exists."""
        if not self.path.exists():
            return None

        try:
            content = self.path.read_text(encoding="utf-8")
            return self._parse_markdown(content)
        except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
            logger.warning(f"Failed to read steering file {self.path}: {e}")
            return None

    def acknowledge(self) -> None:
        """Rename applied steering file to avoid re-reading."""
        if not self.path.exists():
            return

        timestamp = int(time.time())
        applied_path = self.path.with_name(f"{self.path.name}.applied.{timestamp}")
        try:
            self.path.rename(applied_path)
            logger.info(f"Steering applied and archived to {applied_path.name}")
        except OSError as e:
            logger.warning(f"Failed to rename steering file: {e}")

    def _parse_markdown(self, content: str) -> SteeringDirective:
        """Parse the markdown steering protocol."""
        directive = SteeringDirective(workflow_id=self.workflow_id, has_guidance=True)

        lines = content.split("\n")
        current_section = ""
        current_body: list[str] = []

        for line in lines:
            stripped = line.strip()

            # Check for section headers (## or ###)
            if stripped.startswith("#"):
                # Process previous section
                if current_section and current_body:
                    self._apply_section(directive, current_section, "\n".join(current_body))

                # Start new section
                current_section = stripped.lower().lstrip("#").strip()
                current_body = []
            else:
                current_body.append(line)

        # Process last section
        if current_section and current_body:
            self._apply_section(directive, current_section, "\n".join(current_body))

        return directive

    def _apply_section(self, directive: SteeringDirective, section: str, body: str) -> None:
        """Apply parsed section to directive."""
        body = body.strip()

        if "priority" in section:
            directive.priority_guidance = body
        elif "skip nodes" in section:
            # Parse list items
            nodes = []
            for line in body.split("\n"):
                line = line.strip()
                if line.startswith("-"):
                    node = line.lstrip("-").strip()
                    if node:
                        nodes.append(node)
                elif "," in line:
                    # Handle comma-separated on same line
                    nodes.extend([n.strip() for n in line.split(",") if n.strip()])
            directive.skip_nodes = nodes
        elif "budget override" in section:
            import re

            match = re.search(r"(\d+\.?\d*)", body)
            if match:
                with contextlib.suppress(ValueError):
                    directive.budget_override_usd = float(match.group(1))
        elif "stop after" in section:
            directive.stop_after_node = body.strip()


@dataclass
class EnvSteeringSource(SteeringSource):
    """Environment variable steering via BEAGLE_STEER_* vars.

    Environment variables:
    - BEAGLE_STEER_PRIORITY: Priority guidance text
    - BEAGLE_STEER_SKIP: Comma-separated node names to skip
    - BEAGLE_STEER_BUDGET: Budget override amount
    - BEAGLE_STEER_STOP: Node name to stop after
    """

    prefix: str = "BEAGLE_STEER_"

    def read(self) -> SteeringDirective | None:
        """Read steering from environment variables."""
        priority = os.environ.get(f"{self.prefix}PRIORITY", "")
        skip_str = os.environ.get(f"{self.prefix}SKIP", "")
        budget_str = os.environ.get(f"{self.prefix}BUDGET", "")
        stop_after = os.environ.get(f"{self.prefix}STOP", "")

        # No guidance if nothing set
        if not any([priority, skip_str, budget_str, stop_after]):
            return None

        directive = SteeringDirective(workflow_id="env", has_guidance=True, source="env")

        if priority:
            directive.priority_guidance = priority

        if skip_str:
            directive.skip_nodes = [n.strip() for n in skip_str.split(",") if n.strip()]

        if budget_str:
            with contextlib.suppress(ValueError):
                directive.budget_override_usd = float(budget_str)

        if stop_after:
            directive.stop_after_node = stop_after.strip()

        return directive

    def acknowledge(self) -> None:
        """Clear environment-based steering after use."""
        for var in [
            "BEAGLE_STEER_PRIORITY",
            "BEAGLE_STEER_SKIP",
            "BEAGLE_STEER_BUDGET",
            "BEAGLE_STEER_STOP",
        ]:
            os.environ.pop(var, None)


@dataclass
class TUIChannelSource(SteeringSource):
    """TUI input channel steering via async queue.

    The TUI can push steering commands directly to the running workflow.
    """

    queue: Any | None = field(default=None, repr=False)
    channel_name: str = "steering"

    def read(self) -> SteeringDirective | None:
        """Check queue for steering input without blocking."""
        if self.queue is None:
            return None

        try:
            # Non-blocking check
            while not self.queue.empty():
                item = self.queue.get_nowait()
                if isinstance(item, dict) and item.get("type") == "steering":
                    return self._parse_tui_input(item)
        except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
            logger.debug(f"TUI steering queue check failed: {e}")

        return None

    def acknowledge(self) -> None:
        """TUI steering is consumed immediately on read."""
        pass

    def _parse_tui_input(self, item: dict[str, Any]) -> SteeringDirective:
        """Parse TUI steering input format."""
        directive = SteeringDirective(workflow_id="tui", has_guidance=True, source="tui")

        if "priority" in item:
            directive.priority_guidance = str(item["priority"])
        if "skip" in item:
            nodes = item["skip"]
            if isinstance(nodes, str):
                nodes = [n.strip() for n in nodes.split(",")]
            directive.skip_nodes = nodes
        if "budget" in item:
            with contextlib.suppress(ValueError, TypeError):
                directive.budget_override_usd = float(item["budget"])
        if "stop_after" in item:
            directive.stop_after_node = str(item["stop_after"])

        return directive


@dataclass
class APISource(SteeringSource):
    """API/webhook steering via HTTP callback or shared state.

    Expects state in a JSON file or Redis-like store.
    """

    state_path: Path = field(default_factory=lambda: Path(".beagle/steer_api.json"))

    def read(self) -> SteeringDirective | None:
        """Read steering from API state file."""
        if not self.state_path.exists():
            return None

        try:
            content = self.state_path.read_text(encoding="utf-8")
            data = json.loads(content)

            # Check if this is fresh steering data
            if not data.get("active", False):
                return None

            return self._parse_api_json(data)
        except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
            logger.warning(f"Failed to read API steering from {self.state_path}: {e}")
            return None

    def acknowledge(self) -> None:
        """Mark API steering as consumed."""
        if not self.state_path.exists():
            return

        try:
            content = self.state_path.read_text(encoding="utf-8")
            data = json.loads(content)
            data["active"] = False
            data["applied_at"] = time.time()
            self.state_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
            logger.warning(f"Failed to acknowledge API steering: {e}")

    def _parse_api_json(self, data: dict[str, Any]) -> SteeringDirective:
        """Parse API JSON format."""
        directive = SteeringDirective(
            workflow_id=data.get("workflow_id", "api"), has_guidance=True, source="api"
        )

        if "priority" in data:
            directive.priority_guidance = str(data["priority"])
        if "skip_nodes" in data:
            directive.skip_nodes = data["skip_nodes"]
        if "budget_override_usd" in data:
            directive.budget_override_usd = data["budget_override_usd"]
        if "stop_after_node" in data:
            directive.stop_after_node = data["stop_after_node"]

        return directive


class SteeringSourceManager:
    """Manages multiple steering sources with priority ordering.

    Sources are checked in order until one provides guidance:
    1. API (webhook/callback) - highest priority
    2. TUI channel
    3. File (.beagle/steer.md)
    4. Environment variables
    """

    def __init__(self, workspace_root: Path, workflow_id: str = "default"):
        """Initialize the steering source manager.

        Args:
            workspace_root: Project root directory containing ``.beagle/`` config.
            workflow_id: Unique identifier for the current workflow run.

        """
        self.workspace_root = workspace_root
        self.workflow_id = workflow_id

        # Initialize sources in priority order
        self.sources: list[SteeringSource] = [
            APISource(state_path=workspace_root / ".beagle" / "steer_api.json"),
            TUIChannelSource(),
            FileSteeringSource(
                path=workspace_root / ".beagle" / "steer.md", workflow_id=workflow_id
            ),
            EnvSteeringSource(),
        ]

    def check(self) -> SteeringDirective | None:
        """Check all sources in priority order, return first guidance found."""
        for source in self.sources:
            try:
                directive = source.read()
                if directive and directive.has_guidance:
                    logger.info(
                        f"Steering received from {source.__class__.__name__} "
                        f"({source.__class__.__bases__[0].__name__})"
                    )
                    return directive
            except (ImportError, OSError) as e:
                logger.warning(f"Error reading from {source.__class__.__name__}: {e}")

        return None

    def acknowledge(self, directive: SteeringDirective) -> None:
        """Acknowledge guidance on the source it came from."""
        source_map = {
            "api": APISource,
            "tui": TUIChannelSource,
            "file": FileSteeringSource,
            "env": EnvSteeringSource,
        }

        source_class = source_map.get(directive.source)
        if source_class:
            for source in self.sources:
                if isinstance(source, source_class):
                    try:  # type: ignore[unreachable]  # dynamic dispatch over concrete source types
                        source.acknowledge()
                    except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
                        logger.warning(f"Failed to acknowledge on {source_class.__name__}: {e}")
