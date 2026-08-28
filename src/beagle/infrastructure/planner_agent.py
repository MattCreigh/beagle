#!/usr/bin/env python3
"""Planner Agent - Research planning phase of Beagle workflow.

This agent:
1. Listens for tasks from orchestrator via Orpheus ring
2. Executes research-planner skill via goose
3. Sends research plan to executor via Orpheus ring
4. Publishes heartbeat status

All operational insights are logged to RAG automatically.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time

try:  # proprietary transport — provided by the separately licensed beagle-orpheus wheel
    from beagle_orpheus.compat import (
        A2AMessage,
        MessageType,
        get_ipc,
    )
except ImportError:
    from beagle.infrastructure._orpheus_optional import A2AMessage, MessageType, get_ipc

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("Beagle.infrastructure.planner_agent")

# Configuration
WORKFLOW_ID = os.environ.get("WORKFLOW_ID", "default")
AGENT_TYPE = os.environ.get("AGENT_TYPE", "planner")
ORPHEUS_INSTANCE = os.environ.get("ORPHEUS_INSTANCE", "beagle-default")


class PlannerAgent:
    """Agent responsible for research planning phase."""

    def __init__(self):
        self.agent_name = f"beagle-{AGENT_TYPE}"
        self.ipc = get_ipc(self.agent_name, WORKFLOW_ID)
        self.running = False
        self.task_count = 0
        self.start_time = time.monotonic()

        # Log initialization to RAG
        self.log_to_rag(
            "agent_init",
            {
                "agent_type": AGENT_TYPE,
                "workflow_id": WORKFLOW_ID,
                "orpheus_instance": ORPHEUS_INSTANCE,
                "orpheus_available": self.ipc.is_available,
            },
        )

    async def run(self):
        """Main agent loop - listens for tasks and processes them."""
        self.running = True
        logger.info(f"[PlannerAgent] Starting {self.agent_name}...")

        # Send heartbeat to register agent
        await self.send_heartbeat()

        # Subscribe to message from orchestrator
        self.ipc.subscribe(["orchestrator"], self._handle_message)

        # Main processing loop
        while self.running:
            # Wait for task from orchestrator
            message = await self.ipc.receive_async(
                from_agent="orchestrator",
                timeout_ms=5000,  # 5s poll
            )

            if message:
                await self._handle_orchestrator_task(message)
            else:
                # No task, send heartbeat
                await self.send_heartbeat()

            # Brief sleep to prevent CPU spinning
            await asyncio.sleep(0.1)

        logger.info("[PlannerAgent] Shutting down...")

    async def _handle_orchestrator_task(self, message: A2AMessage) -> None:
        """Handle incoming task from orchestrator."""
        logger.info(f"[PlannerAgent] Received task from orchestrator: {message.task_id}")

        self.task_count += 1

        try:
            # Extract query from payload
            query = message.payload.get("query", "")

            if not query:
                logger.warning(f"[PlannerAgent] Empty query in task {message.task_id}")
                await self._send_error(message.task_id, "Empty query")
                return

            # Execute research planning via goose
            start_time = time.monotonic()
            plan = await self._execute_planning(query)
            execution_time = time.monotonic() - start_time

            logger.info(
                f"[PlannerAgent] Planning complete in {execution_time:.2f}s, "
                f"plan size: {len(plan)} chars"
            )

            # Send plan to executor
            result_msg = A2AMessage(
                msg_type=MessageType.RESULT,
                sender=self.agent_name,
                recipient="beagle-executor",
                task_id=message.task_id,
                payload={
                    "query": query,
                    "research_plan": plan,
                    "metadata": {
                        "agent_type": AGENT_TYPE,
                        "task_num": self.task_count,
                        "execution_time": execution_time,
                    },
                },
                correlation_id=message.correlation_id,
            )

            success = self.ipc.send(result_msg, to_agent="executor")

            if success:
                logger.info(f"[PlannerAgent] Sent plan to executor for task {message.task_id}")

                # Log successful completion to RAG
                self.log_to_rag(
                    "task_complete",
                    {
                        "task_id": message.task_id,
                        "query_length": len(query),
                        "plan_length": len(plan),
                        "execution_time": execution_time,
                        "orpheus_used": self.ipc.is_available,
                    },
                )
            else:
                logger.error("[PlannerAgent] Failed to send plan to executor")
                await self._send_error(message.task_id, "IPC send failed")

        except Exception as e:  # broad catch intentional
            logger.error(
                f"[PlannerAgent] Error processing task {message.task_id}: {e}",
                exc_info=True,
            )
            await self._send_error(message.task_id, str(e))

    async def _execute_planning(self, query: str) -> str:
        """Execute research planning via goose subprocess.

        This could be refactored to use the existing nodes.planning_node
        for consistency with other workflows.
        """
        # For now, return a simple plan
        # In production, this would call the actual goose research-planner skill
        plan = f"""objective: Research and analyze the query: {query[:200]}...

phases:
  - name: discovery
    goal: Find relevant files and documentation
    searches:
      - pattern: "**/*.py"
        grep: "orchestrator"
        reason: "Find orchestrator-related code"
    deliverable: List of orchestrator files

  - name: execution
    goal: Execute search and gather evidence
    deliverable: Raw execution context

risks:
  - Large codebase may require selective searching
  - Some files may be inaccessible

success_criteria:
  - All orchestrator files identified
  - Key patterns documented
"""
        return plan

    async def send_heartbeat(self) -> None:
        """Send heartbeat status to monitoring system."""
        heartbeat = A2AMessage(
            msg_type=MessageType.HEARTBEAT,
            sender=self.agent_name,
            recipient="monitor",
            task_id="heartbeat",
            payload={
                "agent_type": AGENT_TYPE,
                "status": "running",
                "tasks_processed": self.task_count,
                "uptime": time.monotonic() - self.start_time,
                "orpheus_available": self.ipc.is_available,
            },
        )

        self.ipc.send(heartbeat, to_agent="monitor")

    async def _send_error(self, task_id: str, error_msg: str) -> None:
        """Send error status back to orchestrator."""
        error_msg_obj = A2AMessage(
            msg_type=MessageType.RESULT,
            sender=self.agent_name,
            recipient="beagle-orchestrator",
            task_id=task_id,
            payload={
                "error": error_msg,
                "agent_type": AGENT_TYPE,
            },
        )

        self.ipc.send(error_msg_obj, to_agent="orchestrator")

    def _handle_message(self, message: A2AMessage) -> None:
        """Handle incoming message (callback for subscription)."""
        logger.debug(f"[PlannerAgent] Received message: {message.msg_type} from {message.sender}")

    async def shutdown(self):
        """Graceful shutdown."""
        self.running = False

        # Send final heartbeat
        heartbeat = A2AMessage(
            msg_type=MessageType.HEARTBEAT,
            sender=self.agent_name,
            recipient="monitor",
            task_id="shutdown",
            payload={
                "agent_type": AGENT_TYPE,
                "status": "shutting_down",
                "tasks_processed": self.task_count,
                "uptime": time.monotonic() - self.start_time,
            },
        )

        self.ipc.send(heartbeat, to_agent="monitor")

        # Log shutdown to RAG
        self.log_to_rag(
            "agent_shutdown",
            {
                "agent_type": AGENT_TYPE,
                "tasks_processed": self.task_count,
                "uptime": time.monotonic() - self.start_time,
            },
        )

    def log_to_rag(self, event_type: str, data: dict) -> None:
        """Log events to RAG."""
        # v1.2.0 (RG-6, BGL-009): resolve from the canonical data root.
        from beagle.config.paths import get_data_root

        rag_log_dir = get_data_root() / "rag_logs"
        rag_log_dir.mkdir(parents=True, exist_ok=True)

        log_entry = {
            "timestamp": time.time(),
            "event_type": event_type,
            "source": f"agent_{AGENT_TYPE}",
            "agent_type": AGENT_TYPE,
            "data": data,
        }

        log_file = rag_log_dir / f"agent_{AGENT_TYPE}_{int(time.time())}.jsonl"
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(log_entry) + "\n")

        logger.debug(f"[RAG] Logged {event_type}")


async def main():
    """Main entry point."""
    agent = PlannerAgent()

    try:
        await agent.run()
    except KeyboardInterrupt:
        logger.info("[PlannerAgent] Received shutdown signal")
    finally:
        await agent.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
