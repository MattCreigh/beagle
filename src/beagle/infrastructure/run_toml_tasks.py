#!/usr/bin/env python3
"""
OpenClaw TOML Batch Runner
==========================
Runs all TOML-defined tasks from the tasks directory.

Usage:
    python3 run_toml_tasks.py                    # Run all pending tasks
    python3 run_toml_tasks.py --task host_optimization  # Run specific task
    python3 run_toml_tasks.py --list             # List available tasks
    python3 run_toml_tasks.py --dry-run          # Show what would run
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from beagle.config.paths import get_data_root as _get_data_root
from beagle.metaprompts.task_loader import (
    TASKS_DIR,
    list_available_tasks,
    load_task_spec,
)
from beagle.utils.env_manager import build_goose_env

# Runtime state (task DB, launcher logs, RAG index) anchors to data_root,
# never the package dir: under a wheel install the package lives in
# site-packages and is replaced wholesale by the next install.
_AI_STATE_DIR = _get_data_root() / "ai"
_AI_STATE_DIR.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger(__name__)

# Setup paths
_THIS_DIR = Path(__file__).resolve().parent
_WORKSPACE = _THIS_DIR.parent
_METAPROMPTS_DIR = _WORKSPACE / "metaprompts"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [Beagle.infrastructure.run_toml_tasks] %(levelname)s: %(message)s",
)
log = logging.getLogger("Beagle.infrastructure.run_toml_tasks")

# TOML loader + task store import as package modules (no sys.path hack).
TOML_AVAILABLE = True

# Task store
try:
    from beagle.infrastructure.task_store import TaskStore, get_task_store

    STORE_AVAILABLE = True
except ImportError as e:
    log.error(f"Task store not available: {e}")
    STORE_AVAILABLE = False

# Paths
_DB_PATH = _AI_STATE_DIR / "openclaw_tasks.db"
# v1.2.0 (RG-6, BGL-009): default to the interpreter running this process
# instead of a hardcoded host venv path.
_VENV_PYTHON = Path(os.environ.get("BEAGLE_VENV_PYTHON", sys.executable))
_CLI_PATH = _WORKSPACE / "cli.py"


def get_pending_toml_tasks() -> list[dict]:
    """Get all TOML task files that haven't been run yet."""
    if not TASKS_DIR.exists():
        log.warning(f"Tasks directory not found: {TASKS_DIR}")
        return []

    pending = []
    for toml_file in TASKS_DIR.glob("*.toml"):
        # Extract task name from filename
        task_name = toml_file.stem
        pending.append(
            {
                "name": task_name,
                "path": str(toml_file),
                "toml_file": toml_file,
            }
        )

    log.info(f"Found {len(pending)} TOML task definitions")
    return pending


def check_task_completed(store: TaskStore, task_name: str) -> bool:
    """Check if a task has already completed successfully."""
    # Query by spec_json containing the task name
    # This is a simple heuristic - could be improved with task_id tracking
    try:
        cursor = store._get_conn().execute(
            """
            SELECT task_id, status, result_json
            FROM tasks
            WHERE spec_json LIKE ?
            AND status = 'completed'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (f"%{task_name}%",),
        )
        row = cursor.fetchone()
        return row is not None
    except (sqlite3.Error, OSError) as e:
        log.warning(f"Failed to check task completion: {e}")
        return False


def run_task_via_toml(toml_path: Path, model: str = "glm-5:cloud", budget: float = 3.0) -> str:
    """
    Run a task defined in a TOML file by invoking the Beagle workflow.

    This bridges TOML configs to the existing workflow execution.
    """
    if not TOML_AVAILABLE:
        raise RuntimeError("TOML loader not available - cannot run tasks")

    # Load the TOML spec
    try:
        spec = load_task_spec(toml_path)
        log.info(f"Loaded TOML task: {spec.name} (ID: {spec.task_id})")
    except Exception as e:  # broad catch intentional
        log.error(f"Failed to load TOML: {e}")
        raise

    # Build the command
    workflow = spec.workflow.name
    query = spec.query
    use_budget = spec.budget.max_cost_usd or budget
    use_model = spec.model.model or model

    # Use the original launch mechanism
    cmd = [
        str(_VENV_PYTHON),
        str(_CLI_PATH),
        "run",
        workflow,
        query,
        "--budget",
        str(use_budget),
        "--mode",
        spec.workflow.mode,
        "--approve-all",
    ]

    # Build environment
    env = build_goose_env(use_model)

    log.info(f"Spawning task: workflow={workflow}, model={use_model}, budget=${use_budget:.2f}")
    log.info(f"Query preview: {query[:100]}...")

    # Run the subprocess
    start_time = time.monotonic()
    proc = subprocess.Popen(
        cmd,
        cwd=str(_WORKSPACE),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Wait for completion with timeout
    timeout = spec.budget.timeout_seconds
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        elapsed = time.monotonic() - start_time

        result = {
            "task_id": spec.task_id,
            "name": spec.name,
            "exit_code": proc.returncode,
            "elapsed_seconds": elapsed,
            "stdout_chars": len(stdout),
            "stderr_chars": len(stderr),
        }

        if proc.returncode == 0:
            log.info(f"Task {spec.name} completed successfully in {elapsed:.1f}s")
        else:
            log.warning(f"Task {spec.name} failed with exit code {proc.returncode}")
            result["stderr"] = stderr.decode("utf-8", errors="replace")[:500]

        return json.dumps(result, indent=2)

    except subprocess.TimeoutExpired:
        proc.kill()
        log.error(f"Task {spec.name} timed out after {timeout}s")
        return json.dumps(
            {
                "task_id": spec.task_id,
                "name": spec.name,
                "error": "timeout",
                "timeout_seconds": timeout,
            }
        )


def run_all_pending_tasks(dry_run: bool = False, force: bool = False) -> list[dict]:
    """Run all TOML tasks that haven't completed yet."""
    if not TOML_AVAILABLE or not STORE_AVAILABLE:
        log.error("Required components not available")
        return []

    tasks = get_pending_toml_tasks()
    store = get_task_store(_DB_PATH)
    results = []

    for task_info in tasks:
        task_name = task_info["name"]
        toml_path = task_info["toml_file"]

        # Check if already completed
        if not force and check_task_completed(store, task_name):
            log.info(f"Task {task_name} already completed, skipping")
            results.append({"name": task_name, "status": "skipped", "reason": "already_completed"})
            continue

        if dry_run:
            log.info(f"[DRY-RUN] Would run: {task_name}")
            results.append({"name": task_name, "status": "dry_run"})
            continue

        log.info(f"\n{'=' * 60}\nRunning task: {task_name}\n{'=' * 60}")

        try:
            result_json = run_task_via_toml(toml_path)
            result = json.loads(result_json)
            result["status"] = "completed" if result.get("exit_code") == 0 else "failed"
            results.append(result)
        except Exception as e:  # broad catch intentional
            log.exception(f"Failed to run task {task_name}")
            results.append({"name": task_name, "status": "error", "error": str(e)})

    return results


def main():
    parser = argparse.ArgumentParser(description="Run OpenClaw TOML task definitions")
    parser.add_argument(
        "--task",
        "-t",
        help="Run specific task by name",
    )
    parser.add_argument(
        "--list",
        "-l",
        action="store_true",
        help="List available TOML tasks",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would run without executing",
    )
    parser.add_argument(
        "--force",
        "-f",
        action="store_true",
        help="Re-run even if task already completed",
    )
    parser.add_argument(
        "--model",
        "-m",
        default="glm-5:cloud",
        help="Model to use for task execution",
    )
    parser.add_argument(
        "--budget",
        "-b",
        type=float,
        default=3.0,
        help="Budget in USD per task",
    )

    args = parser.parse_args()

    if not TOML_AVAILABLE:
        logger.warning("ERROR: TOML task loader not available")
        logger.info("Make sure task_schema.py and task_loader.py are in the metaprompts directory")
        sys.exit(1)

    if args.list:
        logger.info("\nAvailable TOML Tasks:")
        logger.info("=" * 60)
        tasks = list_available_tasks()
        for task in tasks:
            logger.info(f"  - {task['name']:30s} [{task.get('task_type', 'workflow')}]")
            if task.get("description"):
                logger.info(f"    {task['description'][:50]}...")
        logger.info(f"\nTotal: {len(tasks)} tasks")
        return 0

    if args.task:
        # Run specific task
        toml_path = TASKS_DIR / f"{args.task}.toml"
        if not toml_path.exists():
            log.error(f"Task file not found: {toml_path}")
            return 1

        logger.info(f"Running task: {args.task}")
        try:
            result = run_task_via_toml(toml_path, args.model, args.budget)
            logger.info(result)
            return 0
        except Exception as e:  # broad catch intentional
            log.exception(f"Failed to run task: {e}")
            return 1

    # Run all pending tasks
    logger.info(f"\n{'=' * 60}")
    logger.info("OpenClaw TOML Batch Runner")
    logger.info(f"{'=' * 60}")
    logger.info(f"Tasks directory: {TASKS_DIR}")
    logger.info(f"Dry run: {args.dry_run}")
    logger.info(f"Force re-run: {args.force}")
    logger.info(f"{'=' * 60}\n")

    results = run_all_pending_tasks(dry_run=args.dry_run, force=args.force)

    logger.info("\n" + "=" * 60)
    logger.info("Batch Run Summary")
    logger.info("=" * 60)
    for r in results:
        status = r.get("status", "unknown")
        name = r.get("name", "unknown")
        elapsed = r.get("elapsed_seconds", 0)
        logger.info(f"  {name:30s} [{status}] {elapsed:.1f}s")

    completed = sum(1 for r in results if r.get("status") == "completed")
    failed = sum(1 for r in results if r.get("status") in ("error", "failed"))
    logger.info(f"\nCompleted: {completed}, Failed: {failed}, Total: {len(results)}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
