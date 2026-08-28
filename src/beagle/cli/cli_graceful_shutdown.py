"""CLI graceful shutdown helper for Beagle.

Provides graceful shutdown handling for asyncio workflows, properly
cancelling tasks on keyboard interrupts.
"""

import asyncio
import logging
import signal
import sys
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


class GracefulShutdown:
    """Context manager for graceful shutdown of async workflows."""

    def __init__(self) -> None:
        self.shutdown_requested = False
        self.tasks: set[asyncio.Task] = set()
        self.loop: asyncio.AbstractEventLoop | None = None

    def _signal_handler(self, signum: int, _frame: Any) -> None:
        """Handle termination signals gracefully."""
        logger.info(f"Received signal {signum}, initiating graceful shutdown...")
        self.shutdown_requested = True

        # Cancel all tasks
        if self.loop:
            for task in self.tasks:
                if not task.done():
                    task.cancel()

    async def run_with_graceful_shutdown(self, coro: Any) -> Any:
        """Run a coroutine with graceful shutdown handling."""
        self.loop = asyncio.get_running_loop()

        # Set up signal handlers
        for sig in (signal.SIGINT, signal.SIGTERM):
            self.loop.add_signal_handler(sig, self._signal_handler, sig, None)

        try:
            # Track the main task
            task = asyncio.create_task(coro)
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

            # Wait for completion or cancellation
            try:
                return await task
            except asyncio.CancelledError:
                logger.info("Workflow cancelled by user")
                sys.exit(130)
        finally:
            # Clean up signal handlers
            if self.loop:
                for sig in (signal.SIGINT, signal.SIGTERM):
                    self.loop.remove_signal_handler(sig)

    def run_async(self, coro: Any) -> Any:
        """Synchronous wrapper to run async code with graceful shutdown."""
        try:
            return asyncio.run(self.run_with_graceful_shutdown(coro))
        except KeyboardInterrupt:
            logger.info("Interrupted by user")
            sys.exit(130)


def run_workflow_gracefully(dag: Any, query: str) -> Any:
    """Run a DAG workflow with graceful shutdown support.

    Args:
        dag: The loaded workflow DAG
        query: The query to process

    Returns:
        The result from the workflow

    """

    async def _run() -> Any:
        return await dag.run(query)

    shutdown = GracefulShutdown()
    return shutdown.run_async(_run())


def run_workflow_state_gracefully(dag: Any, state: Any) -> Any:
    """Run a DAG workflow with state and graceful shutdown support.

    Args:
        dag: The loaded workflow DAG (with state already set)
        state: The agent state (for resume workflows)

    Returns:
        The result from the workflow

    """

    async def _run() -> Any:
        return await dag.run(state.query)

    shutdown = GracefulShutdown()
    return shutdown.run_async(_run())


def run_graph_workflow_gracefully(
    run_workflow_func: Callable[..., Any], *args: Any, **kwargs: Any
) -> dict[str, Any]:
    """Run a graph.py workflow function with graceful shutdown support.

    Args:
        run_workflow_func: The async run_workflow function from graph.py
        *args: Positional arguments to pass to run_workflow
        **kwargs: Keyword arguments to pass to run_workflow

    Returns:
        The final state dict from the workflow

    """

    async def _run() -> dict[str, Any]:
        return await run_workflow_func(*args, **kwargs)

    shutdown = GracefulShutdown()
    return shutdown.run_async(_run())
