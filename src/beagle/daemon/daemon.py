"""Main daemon loop for Beagle (KAIROS-lite).

Proactively monitors codebase and triggers background workflows.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from pathlib import Path

from ..core.graph import run_workflow
from ..events import (
    DaemonChangeDetected,
    DaemonIdleStart,
    DaemonStarted,
    DaemonStopped,
    DaemonTriggered,
    get_event_bus,
)
from .scheduler import DaemonScheduler
from .triggers import Trigger, TriggerMatcher
from .watcher import ChangeSet, Watcher

logger = logging.getLogger("Beagle.daemon")

# Module-level singleton tracking for the active daemon instance.
_active_daemon: BeagleDaemon | None = None


def get_active_daemon() -> BeagleDaemon | None:
    """Return the currently active daemon instance, or None if none is running."""
    return _active_daemon


class BeagleDaemon:
    """Background agent for autonomous workspace monitoring."""

    def __init__(self, workspace_root: Path | str):
        global _active_daemon
        self.workspace_root = Path(workspace_root)
        self.watcher = Watcher(self.workspace_root)
        self.triggers = TriggerMatcher()
        self.scheduler = DaemonScheduler()

        self._stop_event = asyncio.Event()
        self._idle_counter = 0
        self._deferred_queue = deque()  # type: ignore[var-annotated]
        self.daily_cost = 0.0
        _active_daemon = self

    async def run(self) -> None:
        """Main daemon execution loop."""
        logger.info("[Daemon] Starting Beagle daemon...")
        get_event_bus().publish(
            DaemonStarted(
                workflow_id="daemon",
                tick_interval=self.scheduler.tick_interval,
                max_daily_cost=self.scheduler.max_daily_cost_usd,
            )
        )

        while not self._stop_event.is_set():
            try:
                # 1. Check for changes
                changes = self.watcher.check()

                if changes.has_changes:
                    self._idle_counter = 0
                    get_event_bus().publish(
                        DaemonChangeDetected(
                            workflow_id="daemon",
                            changed_files=len(changes.changed_files + changes.new_files),
                            affected_modules=list(changes.affected_modules()),
                        )
                    )

                    # 2. Match changes to triggers
                    matched = self.triggers.match(changes)

                    for trigger in matched:
                        # 3. Check daily cost cap
                        if self.scheduler.is_over_budget():
                            logger.warning("[Daemon] Daily budget reached. Deferring trigger.")
                            continue

                        # (Simple version: run immediately for now)
                        await self._run_triggered_workflow(trigger, changes)
                else:
                    self._idle_counter += self.scheduler.tick_interval

                    # 4. Idle period — run autoDream (Placeholder)
                    if self._idle_counter >= self.scheduler.idle_threshold:
                        get_event_bus().publish(
                            DaemonIdleStart(workflow_id="daemon", idle_seconds=self._idle_counter)
                        )
                        # await self._run_autodream()
                        self._idle_counter = 0

            except Exception as e:  # broad catch intentional
                logger.error(f"[Daemon] Tick error: {e}", exc_info=True)

            await asyncio.sleep(self.scheduler.tick_interval)

    async def _run_triggered_workflow(self, trigger: Trigger, changes: ChangeSet) -> None:
        """Execute a workflow in response to a trigger."""
        logger.info(f"[Daemon] Triggered: {trigger.name} -> {trigger.workflow}")
        get_event_bus().publish(
            DaemonTriggered(
                workflow_id="daemon",
                trigger_name=trigger.name,
                workflow=trigger.workflow,
            )
        )

        try:
            # We run the actual graph workflow
            result = await run_workflow(
                query=(
                    f"Auto-triggered by {trigger.name} "
                    f"due to changes in {', '.join(changes.changed_files[:3])}"
                ),
                workflow_name=trigger.workflow,
                budget=trigger.budget,
            )
            cost = result.get("total_cost", 0.0)
            self.scheduler.increment_cost(cost)

        except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
            logger.error(f"[Daemon] Workflow execution failed: {e}")

    def stop(self) -> None:
        """Signal daemon to stop."""
        global _active_daemon
        self._stop_event.set()
        _active_daemon = None
        get_event_bus().publish(
            DaemonStopped(workflow_id="daemon", total_cost_usd=self.scheduler.daily_cost)
        )
