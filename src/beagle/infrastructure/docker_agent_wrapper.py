#!/usr/bin/env python3
"""Docker Agent Wrapper - Wraps existing node.py functions for containerized execution.

This wrapper:
1. Adapts the existing LangGraph nodes to work as standalone containers
2. Uses Orpheus IPC for inter-agent communication
3. Automatically captures all learnings to RAG
4. Provides graceful shutdown handling

Key insight: Instead of reimplementing the goose subprocess logic,
we can reuse nodes.py functions and just adapt the I/O layer.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("Beagle.infrastructure.docker_agent_wrapper")

# Configuration
AGENT_TYPE = os.environ.get("AGENT_TYPE", "orchestrator")
WORKFLOW_ID = os.environ.get("WORKFLOW_ID", "default")
WORKSPACE_ROOT = os.environ.get("WORKSPACE_ROOT", "/app")

# Mapping from agent type to node function
_AGENT_NODE_MAP = {
    "planner": ("nodes", "planning_node"),
    "executor": ("nodes", "execution_node"),
    "verifier": ("nodes", "verification_node"),
    "synthesizer": ("nodes", "synthesis_node"),
    "orchestrator": None,  # Orchestrator coordinates agents, not a single node
}

# Input/Output ring mappings (from_agent → to_agent)
_RING_IO_MAP = {
    "planner": {
        "input": "orchestrator",
        "output": "executor",
        "state_key": "research_plan",
    },
    "executor": {
        "input": "planner",
        "output": "verifier",
        "state_key": "raw_execution_context",
    },
    "verifier": {
        "input": "executor",
        "output": "synthesizer",
        "state_key": "verified_facts",
    },
    "synthesizer": {
        "input": "verifier",
        "output": "orchestrator",
        "state_key": "final_report",
    },
}


class DockerAgentWrapper:
    """Wrapper that bridges LangGraph nodes and Docker/Orpheus container environment."""

    def __init__(self):
        self.agent_name = f"beagle-{AGENT_TYPE}"
        self.node_func = None
        self.running = False
        self.tasks_processed = 0
        self.start_time = time.monotonic()
        self.state = {}

        # Load node function dynamically
        self._load_node_function()

        # Initialize RAG logging
        self._log_init()

    def _load_node_function(self) -> None:
        """Load the appropriate node function for this agent type."""
        if AGENT_TYPE not in _AGENT_NODE_MAP:
            logger.error(f"[Wrapper] Unknown agent type: {AGENT_TYPE}")
            raise ValueError(f"Unknown agent type: {AGENT_TYPE}")

        # Orchestrator doesn't have a single node function - it coordinates agents
        if _AGENT_NODE_MAP[AGENT_TYPE] is None:
            logger.info("[Wrapper] Orchestrator mode: will coordinate multiple agents")
            self.node_func = None
            return

        module_path, func_name = _AGENT_NODE_MAP[AGENT_TYPE]  # type: ignore[misc]

        try:
            # Import module (nodes.py is in the root)
            module = __import__(module_path)

            # Get function
            self.node_func = getattr(module, func_name)
            logger.info(f"[Wrapper] Loaded node function: {module_path}.{func_name}")

            # Log to RAG
            self.log_to_rag(
                "node_loaded",
                {
                    "agent_type": AGENT_TYPE,
                    "module": module_path,
                    "function": func_name,
                },
            )

        except Exception as e:  # broad catch intentional
            logger.error(f"[Wrapper] Failed to load node function: {e}")
            raise

    def _log_init(self) -> None:
        """Log initialization details to RAG."""
        env_info = {
            "agent_type": AGENT_TYPE,
            "workflow_id": WORKFLOW_ID,
            "workspace_root": WORKSPACE_ROOT,
            "node_function": _AGENT_NODE_MAP.get(AGENT_TYPE),
        }

        # Sanitize sensitive env vars
        safe_env = {
            k: v
            for k, v in os.environ.items()
            if not any(
                sensitive in k.upper()
                for sensitive in ["KEY", "SECRET", "TOKEN", "PASSWORD", "API"]
            )
        }

        self.log_to_rag(
            "agent_init",
            {
                "env": env_info,
                "env_count": len(safe_env),
                "cwd": str(Path.cwd()),
                "python_path": sys.path[:3],  # First 3 entries only
            },
        )

        logger.info(f"[Wrapper] Initialized {self.agent_name} in {WORKSPACE_ROOT}")

    async def run_single_task(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """Execute the node function with given input data.

        This is the main entry point for containerized execution.
        Docker can invoke this with a JSON payload via stdin or environment.

        Args:
            input_data: Dictionary containing node input (query, state, etc.)

        Returns:
            Dictionary with node output and metadata.

        """
        logger.info(f"[Wrapper] Executing task for {AGENT_TYPE}")

        try:
            # Build initial state from input
            state = self._build_initial_state(input_data)

            # Log input
            self.log_to_rag(
                "task_start",
                {
                    "agent_type": AGENT_TYPE,
                    "query_length": len(state.get("query", "")),
                    "input_keys": list(state.keys()),
                },
            )

            # Execute node function
            start_time = time.monotonic()
            result_state = await self.node_func(state)  # type: ignore[misc]
            execution_time = time.monotonic() - start_time

            self.tasks_processed += 1

            # Extract output
            io_config = _RING_IO_MAP.get(AGENT_TYPE, {})
            output_key = io_config.get("state_key", AGENT_TYPE)
            output_value = result_state.get(output_key, result_state.get("output", ""))

            result = {
                "output": output_value,
                "agent_type": AGENT_TYPE,
                "execution_time": execution_time,
                "task_num": self.tasks_processed,
                "timestamp": time.time(),
                "metadata": {
                    "input_keys": list(state.keys()),
                    "output_keys": list(result_state.keys()),
                    "output_length": len(output_value),
                },
            }

            # Log success
            logger.info(
                f"[Wrapper] Task completed in {execution_time:.2f}s, "
                f"output: {len(output_value)} chars"
            )

            self.log_to_rag(
                "task_complete",
                {
                    "agent_type": AGENT_TYPE,
                    "execution_time": execution_time,
                    "output_length": len(output_value),
                    "task_num": self.tasks_processed,
                },
            )

            return result

        except Exception as e:  # broad catch intentional
            logger.error(f"[Wrapper] Task execution failed: {e}", exc_info=True)

            self.log_to_rag(
                "task_error",
                {
                    "agent_type": AGENT_TYPE,
                    "error": str(e),
                    "error_type": type(e).__name__,
                },
            )

            return {
                "error": str(e),
                "agent_type": AGENT_TYPE,
                "success": False,
            }

    def _build_initial_state(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """Build initial state dictionary for node function.

        This creates a minimal state that node.py functions expect.
        """
        # Import state creation function
        from beagle.core.state import create_initial_state

        query = input_data.get("query", "")

        # Use the proper state creation function
        state = create_initial_state(
            query=query,
            workflow_id=WORKFLOW_ID,
            steering_prompt=input_data.get("steering", ""),
            workflow_mode=input_data.get("mode", "research"),
        )

        # Add any additional state from input_data
        state.update(
            {
                k: v
                for k, v in input_data.items()
                if k not in ["query", "steering", "mode"] and not k.startswith("_")
            }
        )

        return state

    def log_to_rag(self, event_type: str, data: dict) -> None:
        """Log events to RAG."""
        # The default used to be the fixed path /tmp/beagle_rag: predictable and
        # world-writable, so a local user could pre-create it or plant a symlink
        # and capture (or corrupt) the RAG event log. The per-user runtime
        # directory is private; BEAGLE_KNOWLEDGE_DIR still overrides it.
        from beagle.config.paths import get_runtime_dir

        _default_knowledge_dir = str(get_runtime_dir() / "beagle_rag")
        rag_log_dir = (
            Path(os.environ.get("BEAGLE_KNOWLEDGE_DIR", _default_knowledge_dir)) / "rag_logs"
        )
        rag_log_dir.mkdir(parents=True, exist_ok=True)

        log_entry = {
            "timestamp": time.time(),
            "event_type": event_type,
            "source": f"docker_agent_wrapper_{AGENT_TYPE}",
            "agent_type": AGENT_TYPE,
            "workflow_id": WORKFLOW_ID,
            "data": data,
        }

        log_file = rag_log_dir / f"wrapper_{AGENT_TYPE}_{int(time.time())}.jsonl"
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(log_entry) + "\n")


def main():
    """Main entry point for Docker container execution.

    Reads input from stdin (JSON) or environment variables,
    executes the node function, and writes output to stdout (JSON).
    """
    logger.info(f"[Wrapper] Starting Docker agent wrapper for {AGENT_TYPE}")

    wrapper = DockerAgentWrapper()

    # Read input from stdin or environment
    input_source = os.environ.get("DOCKER_AGENT_INPUT", "stdin")

    if input_source == "stdin":
        # Read JSON from stdin
        try:
            input_data = json.loads(sys.stdin.read())
            logger.info(f"[Wrapper] Read input from stdin: {list(input_data.keys())}")
        except json.JSONDecodeError as e:
            logger.error(f"[Wrapper] Failed to parse stdin input: {e}")
            input_data = {}
    else:
        # Read from environment variable
        try:
            env_input = os.environ.get("DOCKER_AGENT_PAYLOAD", "{}")
            input_data = json.loads(env_input)
            logger.info(f"[Wrapper] Read input from environment: {list(input_data.keys())}")
        except json.JSONDecodeError as e:
            logger.error(f"[Wrapper] Failed to parse env input: {e}")
            input_data = {}

    # Execute task
    if input_data:
        result = asyncio.run(wrapper.run_single_task(input_data))

        # Write output to stdout as JSON
        output_json = json.dumps(result, indent=2)
        sys.stdout.write(output_json)
        sys.stdout.write("\n")

        logger.info(f"[Wrapper] Output written to stdout: {len(output_json)} chars")

        # Exit code based on success
        sys.exit(0 if result.get("success", True) else 1)
    else:
        logger.error("[Wrapper] No input provided")
        sys.exit(1)


if __name__ == "__main__":
    main()
