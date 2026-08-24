#!/usr/bin/env python3
"""Beagle Gateway - Makes 'goose run' use the DAG workflow by default.

This wrapper intercepts goose run commands and routes them through
the Beagle (Beagle) workflow system.

Usage:
    goose run "query"              → Uses Beagle workflow
    goose run --vanilla "query"    → Uses vanilla goose CLI
    goose -i                        → Interactive mode (unchanged)
    goose --help                    → Help (unchanged)
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import subprocess
import sys
from pathlib import Path

from beagle.runtime.goose_cli import GooseCliRuntime

logger = logging.getLogger(__name__)


def get_beagle_root() -> Path:
    """Get the Beagle root directory (workspace root)."""
    from .utils.env_manager import get_workspace_root

    return get_workspace_root()


def get_goose_binary() -> Path:
    """Get the vanilla goose binary path."""
    return Path(GooseCliRuntime().binary_path)


# ---------------------------------------------------------------------------
# Subprocess command validation
# ---------------------------------------------------------------------------
# Characters that should never appear in command arguments passed to
# subprocesses.  These guard against shell-injection-style attacks
# when arguments are concatenated into command lists.
_SHELL_META = frozenset(";&|`$()\n\r")


def _validate_cmd_arg(arg: str, label: str = "argument") -> None:
    """Validate a single subprocess command argument against shell metacharacters.

    This prevents injection when arguments originate from user input
    (CLI flags, config values) but are passed as list elements to
    subprocess.Popen / subprocess.run.

    Args:
        arg: The argument to validate.
        label: Human-readable label for error messages.

    Raises:
        ValueError: If the argument contains shell metacharacters.

    """
    if not isinstance(arg, str):
        raise TypeError(f"Command {label} must be a string, got {type(arg).__name__}")

    found = _SHELL_META.intersection(arg)
    if found:
        raise ValueError(
            f"Command {label} contains shell metacharacters ({''.join(sorted(found))}): "
            f"{arg[:80]!r}"
        )

    # Path arguments should be absolute and exist
    if arg.startswith("/") or arg.startswith("~"):
        # This is likely a path — validate it resolves
        resolved = Path(arg).expanduser()
        if not resolved.exists():
            import logging as _logging

            _logging.getLogger("Beagle.gateway").warning(
                "Command %s references non-existent path: %s", label, resolved
            )


def _validate_cmd(cmd: list[str]) -> list[str]:
    """Validate all arguments in a subprocess command list.

    Args:
        cmd: Command list (e.g., ["/usr/bin/python3", "script.py", "--flag", "value"])

    Returns:
        The validated command list (same object, for chaining).

    Raises:
        ValueError: If any argument contains shell metacharacters.
        TypeError: If the command is not a list of strings.

    """
    if not isinstance(cmd, list):
        raise TypeError(f"Command must be a list, got {type(cmd).__name__}")
    if not cmd:
        raise ValueError(
            "Command list is empty — provide at least one argument (the executable name)"
        )
    for i, arg in enumerate(cmd):
        _validate_cmd_arg(str(arg), label=f"arg[{i}]")
    return cmd


def route_to_beagle_workflow(args: argparse.Namespace) -> int:
    """Route command to Beagle workflow system.

    Args:
        args: Parsed command-line arguments

    Returns:
        Exit code from the workflow

    """
    beagle_root = get_beagle_root()
    cli_path = beagle_root / "cli.py"

    # Determine workflow based on query content
    query = " ".join(args.query) if args.query else ""
    workflow = determine_workflow(query)

    # Allow explicit workflow override
    if args.workflow:
        workflow = args.workflow

    # Build the Beagle CLI command
    beagle_cmd = [
        sys.executable,
        str(cli_path),
        "run",
        workflow,
        query,
    ]

    # Add optional flags
    if args.budget:
        beagle_cmd.extend(["--budget", str(args.budget)])
    if args.mode:
        beagle_cmd.extend(["--mode", str(args.mode)])
    if args.steering:
        beagle_cmd.extend(["--steering", args.steering])
    if args.approve_all:
        beagle_cmd.append("--approve-all")

    logger.info(f"[Beagle Gateway] Routing to workflow: {workflow}")
    logger.info(f"[Beagle Gateway] Query: {query[:100]}{'...' if len(query) > 100 else ''}")
    logger.info(f"[Beagle Gateway] Executing in: {beagle_root}")

    # Run with working directory set and proper environment
    process = None
    try:
        # Validate command arguments before spawning subprocess
        _validate_cmd(beagle_cmd)
        # Start the subprocess
        process = subprocess.Popen(
            beagle_cmd,
            cwd=str(beagle_root),
            env=os.environ.copy(),
        )

        # Wait for process to complete
        return process.wait()
    except KeyboardInterrupt:
        if process and process.poll() is None:
            logger.info("\n[Beagle Gateway] Interrupted by user, terminating workflow...")
            process.terminate()
            try:
                # Give it 2 seconds to terminate gracefully
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                # Force kill if it doesn't terminate
                process.kill()
                process.wait()
            logger.info("[Beagle Gateway] Workflow terminated")
        return 130
    except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
        if process and process.poll() is None:
            process.terminate()
            process.wait()
        logger.info(f"[Beagle Gateway] Error running workflow: {e}")
        return 1


def determine_workflow(query: str) -> str:
    """Determine the appropriate workflow based on query content.

    Args:
        query: The user's query

    Returns:
        Workflow name to use

    """
    query_lower = query.lower()

    # Domain 1: Incident Management (highest priority)
    if any(
        kw in query_lower
        for kw in [
            "traceback",
            "error",
            "exception",
            "stack trace",
            "failed",
            "crashed",
            "broken",
            "doesn't work",
            "incident",
            "downtime",
            "outage",
            "emergency",
            "fix bug",
            "debug",
            "troubleshoot",
        ]
    ):
        return "incident"

    # Domain 3: Operations & Infrastructure - check BEFORE development
    if any(
        kw in query_lower
        for kw in [
            "deploy",
            "ci/cd",
            "pipeline",
            "infrastructure",
            "docker",
            "kubernetes",
            "k8s",
            "container",
            "terraform",
            "helm",
            "release",
        ]
    ):
        return "devops"

    # db-migration - check BEFORE development
    if any(
        kw in query_lower
        for kw in [
            "migration",
            "schema",
            "alembic",
            "drizzle",
            "seeding",
            "database migration",
            "add table",
            "create table",
            "alter table",
            "drop table",
            "add column",
            "remove column",
            "rename column",
        ]
    ):
        return "db-migration"

    # Domain 4: Audit & Security - check BEFORE research
    # Only route to security if it's about vulnerabilities/hardening, not explaining auth
    if any(
        kw in query_lower
        for kw in [
            "vulnerability",
            "threat",
            "hardening",
            "penetration",
            "sql injection",
            "xss",
            "csrf",
            "sast",
            "exploit",
            "security compliance",
            "owasp",
            "sanitize",
            "escape input",
        ]
    ):
        return "security"

    if any(
        kw in query_lower
        for kw in [
            "audit",
            "codebase audit",
            "security audit",
            "analyze codebase",
            "survey code",
            "evaluate security",
        ]
    ):
        return "audit"

    # Self-improvement - check BEFORE research
    if any(
        kw in query_lower
        for kw in [
            "optimize",
            "optimize code",
            "optimize performance",
            "improve codebase",
            "improve performance",
            "cleanup",
            "technical debt",
            "refactor for performance",
            "performance optimization",
            "speed up",
            "make faster",
        ]
    ):
        return "self-improvement"

    # Deep-planning - check BEFORE research
    if any(
        kw in query_lower
        for kw in [
            "plan",
            "design architecture",
            "design system",
            "architecture design",
            "complex design",
            "multi-step",
            "strategy",
            "roadmap",
            "technical strategy",
        ]
    ):
        return "deep-planning"

    # Domain 1: Research & Investigation
    # "investigate" only routes to incident if it's about problems
    if "investigate" in query_lower and any(
        kw in query_lower
        for kw in [
            "memory leak",
            "performance issue",
            "slow",
            "hang",
            "freeze",
            "error",
            "fail",
            "crash",
            "exception",
            "bug",
        ]
    ):
        return "incident"

    if any(
        kw in query_lower
        for kw in [
            "explain how",
            "how does",
            "how does authentication work",
            "how does authorization work",
            "what is authentication",
            "what is authorization",
            "research",
            "investigate",
            "find where",
            "search for",
            "explore",
            "compare",
            "how to implement",
            "show me how",
        ]
    ):
        return "research"

    # Domain 2: Development
    if any(
        kw in query_lower
        for kw in [
            "implement",
            "add feature",
            "build",
            "develop",
            "write code",
            "fix bug",
            "patch",
            "refactor code",
            "create api",
            "create endpoint",
            "create function",
            "create class",
            "add endpoint",
            "update function",
        ]
    ):
        return "develop"

    # Default to deep planning for complex queries
    if len(query.split()) > 20:
        return "deep-planning"

    # Default to research workflow
    return "research"


def route_to_vanilla_goose(args: argparse.Namespace) -> int:
    """Route to vanilla goose CLI.

    Args:
        args: Parsed command-line arguments

    Returns:
        Exit code from goose

    """
    goose_bin = get_goose_binary()

    cmd = [str(goose_bin), "run"] + (args.query or [])
    _validate_cmd(cmd)

    logger.info(f"[Beagle Gateway] Bypassing to vanilla goose: {' '.join(cmd)}")

    process = None
    try:
        process = subprocess.Popen(cmd)
        return process.wait()
    except KeyboardInterrupt:
        if process and process.poll() is None:
            logger.info("\n[Beagle Gateway] Interrupted by user, terminating...")
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        return 130


def main() -> int:
    """Main entry point for the Beagle gateway.

    This is only called for 'goose run' commands (the bash router
    at ~/.local/bin/goose handles all other subcommands directly).
    """

    # Set up signal handlers for graceful shutdown
    def signal_handler(_sig, _frame):
        logger.info("\n[Beagle Gateway] Interrupted by user")
        sys.exit(130)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    args = sys.argv[1:]

    # Check for --vanilla flag: bypass Beagle, use vanilla goose run
    vanilla_mode = "--vanilla" in args
    args = [a for a in args if a != "--vanilla"]

    # Parse arguments (first arg should be 'run' from the bash router)
    parser = argparse.ArgumentParser(
        prog="goose",
        description="Beagle Gateway - Goose with DAG workflow support",
        add_help=False,
    )

    parser.add_argument("command", nargs="?", default=None)
    parser.add_argument("query", nargs=argparse.REMAINDER, default=[])
    parser.add_argument("--budget", "-b", type=float, default=None)
    parser.add_argument("--mode", "-m", choices=["audit", "develop", "research"])
    parser.add_argument("--steering", "-s", type=str, default=None)
    parser.add_argument("--workflow", "-w", type=str, default=None)
    parser.add_argument("--approve-all", "--auto-approve", action="store_true", default=False)

    parsed, unknown = parser.parse_known_args(args)

    # Check for --help or -h: show CLI help (workflow list shown by bash router)
    if "--help" in args or "-h" in args:
        # Show Beagle CLI help (workflow list already shown by bash router)
        help_cmd = [sys.executable, str(get_beagle_root() / "cli.py"), "--help"]
        _validate_cmd(help_cmd)
        result = subprocess.run(
            help_cmd,
            env=os.environ.copy(),
            timeout=30,
        )
        return result.returncode

    # Route to vanilla goose run if --vanilla was passed
    if vanilla_mode:
        goose_bin = get_goose_binary()
        cmd = [str(goose_bin), "run"] + (parsed.query or []) + unknown
        _validate_cmd(cmd)
        logger.info(f"[Beagle Gateway] Bypassing to vanilla goose: {' '.join(cmd)}")

        process = None
        try:
            process = subprocess.Popen(cmd)
            return process.wait()
        except KeyboardInterrupt:
            if process and process.poll() is None:
                logger.info("\n[Beagle Gateway] Interrupted by user, terminating...")
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            return 130

    # Handle "list" command directly
    if parsed.command == "list":
        beagle_root = get_beagle_root()
        list_cmd = [sys.executable, str(beagle_root / "cli.py"), "list", *sys.argv[3:]]
        _validate_cmd(list_cmd)
        result = subprocess.run(
            list_cmd,
            cwd=str(beagle_root),
            env=os.environ.copy(),
            timeout=30,
        )
        return result.returncode

    # Handle "info" command directly
    if parsed.command == "info":
        beagle_root = get_beagle_root()
        info_cmd = [sys.executable, str(beagle_root / "cli.py"), "info", *sys.argv[3:]]
        _validate_cmd(info_cmd)
        result = subprocess.run(
            info_cmd,
            cwd=str(beagle_root),
            env=os.environ.copy(),
            timeout=30,
        )
        return result.returncode

    # Route to Beagle workflow
    if parsed.command == "run":
        return route_to_beagle_workflow(parsed)

    # Fallback: pass through to vanilla goose (shouldn't normally reach here)
    process = None
    try:
        fallback_cmd = [str(get_goose_binary()), *sys.argv[1:]]
        _validate_cmd(fallback_cmd)
        process = subprocess.Popen(fallback_cmd)
        return process.wait()
    except KeyboardInterrupt:
        if process and process.poll() is None:
            logger.info("\n[Beagle Gateway] Interrupted by user, terminating...")
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        return 130


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit as e:
        sys.exit(e.code)
    except KeyboardInterrupt:
        logger.info("\n[Beagle Gateway] Interrupted by user")
        sys.exit(130)
    except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
        logger.info(f"[Beagle Gateway] Unexpected error: {e}")
        sys.exit(1)
