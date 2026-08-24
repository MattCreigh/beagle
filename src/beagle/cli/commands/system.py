"""System/config ops commands (agents, checkpoint, config, dream, daemon, health, doctor).

Extracted from cli.py in the v1.0.0 F2 split. Registered flat on the root
app via ``app.add_typer(system_app)`` (no name), so the CLI surface is
byte-identical to the pre-split monolith.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

import typer
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from ...config.config import get_workspace_root

console = Console()

system_app = typer.Typer()


@system_app.command()
def agents() -> None:
    """List available agents (recipes)."""
    recipes_dir = get_workspace_root() / "recipes"

    if not recipes_dir.exists():
        console.print("[yellow]Recipes directory not found[/yellow]")
        return

    recipes = sorted(recipes_dir.glob("*.xml"))

    table = Table(title="Available Agents")
    table.add_column("Agent Name", style="cyan")
    table.add_column("File")

    for recipe in recipes:
        table.add_row(recipe.stem, recipe.name)

    console.print(table)
    console.print(f"\n[dim]Total: {len(recipes)} agents[/dim]")


# ============================================================================
# Config Commands
# ============================================================================

# Helpers
# ============================================================================


@system_app.command("checkpoint")
def checkpoint(
    list_checkpoints: bool = typer.Option(False, "--list", "-l", help="List available checkpoints"),
    resume: str = typer.Option(None, "--resume", "-r", help="Resume from a checkpoint ID"),
    clear: bool = typer.Option(False, "--clear", help="Delete all checkpoints"),
) -> None:
    """Manage workflow checkpoints (v13.19.4)."""
    if list_checkpoints:
        console.print("[dim]No checkpoints available.[/dim]")
    elif resume:
        console.print(f"[yellow]Resume from checkpoint '{resume}' not yet implemented.[/yellow]")
    elif clear:
        console.print("[yellow]Checkpoint clearing not yet implemented.[/yellow]")
    else:
        console.print("[dim]Use --list, --resume <id>, or --clear.[/dim]")


@system_app.command("dream")
def dream(
    prune_only: bool = typer.Option(False, "--prune-only", help="Run only prune operation"),
    merge_only: bool = typer.Option(False, "--merge-only", help="Run only merge operation"),
    refresh_only: bool = typer.Option(False, "--refresh-only", help="Run only refresh operation"),
    report: bool = typer.Option(True, "--report", help="Show consolidation report"),
) -> None:
    """Run autoDream memory consolidation."""
    from ...memory.autodream import AutoDream

    dreamer = AutoDream(get_workspace_root())

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        progress.add_task("[cyan]Consolidating memory...", total=None)

        if prune_only:
            count = asyncio.run(dreamer.prune())
            console.print(f"[green]Pruned {count} entries.[/green]")
        elif merge_only:
            count = asyncio.run(dreamer.merge())
            console.print(f"[green]Merged {count} entries.[/green]")
        elif refresh_only:
            count = asyncio.run(dreamer.refresh())
            console.print(f"[green]Refreshed {count} entries.[/green]")
        else:
            rep = asyncio.run(dreamer.consolidate())
            if report:
                table = Table(title="autoDream Consolidation Report")
                table.add_column("Metric", style="cyan")
                table.add_column("Value", style="magenta")
                table.add_row("Pruned", str(rep.pruned_count))
                table.add_row("Merged", str(rep.merged_count))
                table.add_row("Refreshed", str(rep.refreshed_count))
                table.add_row(
                    "Index Tokens",
                    f"{rep.index_tokens_before} -> {rep.index_tokens_after}",
                )
                table.add_row("Duration", f"{rep.duration_seconds:.2f}s")
                console.print(table)


@system_app.command("daemon")
def daemon_cmd(
    action: str = typer.Argument("status", help="Action: start, stop, status"),
    detach: bool = typer.Option(False, "--detach", help="Run daemon in background (detached)"),
) -> None:
    """Manage the Beagle background daemon."""
    from ...daemon.daemon import BeagleDaemon

    if action == "start":
        if detach:
            console.print(
                "[yellow]Detached mode not implemented in this version. "
                "Running in foreground...[/yellow]"
            )

        daemon = BeagleDaemon(get_workspace_root())
        try:
            console.print("[bold green]Beagle Daemon started. Press Ctrl+C to stop.[/bold green]")
            asyncio.run(daemon.run())
        except KeyboardInterrupt:
            daemon.stop()
            console.print("\n[yellow]Daemon stopped.[/yellow]")
    elif action == "stop":
        console.print("[dim]Send SIGINT (Ctrl+C) to the running daemon process to stop it.[/dim]")
    elif action == "status":
        console.print(
            "Beagle Daemon: [bold blue]READY[/bold blue] (Use 'beagle daemon start' to launch)"
        )
    else:
        console.print(f"[red]Unknown daemon action: {action}[/red]")


@system_app.command("health")
def health_check(
    json_output: bool = typer.Option(False, "--json", help="Output results as JSON"),
    skip_optional: bool = typer.Option(False, "--required-only", help="Skip optional checks"),
) -> None:
    """Run startup health checks and report system readiness."""
    from ...startup.health_check import (
        format_startup_report,
        run_startup_checks,
    )

    results = run_startup_checks(include_optional=not skip_optional)

    if json_output:
        import json

        data = [
            {
                "name": r.name,
                "status": r.status,
                "message": r.message,
                "fix_hint": r.fix_hint,
            }
            for r in results
        ]
        console.print_json(json.dumps(data, indent=2))
    else:
        report = format_startup_report(results)
        console.print(report)

    # Exit with error code if any required check fails
    has_fail = any(r.status == "fail" for r in results)
    if has_fail:
        raise typer.Exit(1)


@system_app.command("doctor")
def doctor(
    json_output: bool = typer.Option(False, "--json", help="Output diagnostic info as JSON"),
) -> None:
    """Diagnose Beagle installation: deps, versions, config, runtime.

    Like ``beagle health`` but additionally reports:
      - The package ``__version__`` (from the SSOT)
      - Python version
      - Critical third-party packages and their versions
      - Feature-flag state from ``constants.FEATURE_FLAGS``
      - Whether the ``google-re2`` secret-scrubber dependency is present
        (mandatory; the system fails closed without it)
    """
    import json as _json
    import platform

    from beagle import __version__ as _pkg_version
    from beagle import constants as _c
    from beagle.startup.health_check import (
        format_startup_report,
        run_startup_checks,
    )

    # ── Collect diagnostic info ──────────────────────────────────────
    info: dict[str, Any] = {
        "package_version": _pkg_version,
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
    }

    # Critical third-party packages. Each entry: (import_name, display_name).
    critical_deps = [
        ("typer", "typer"),
        ("rich", "rich"),
        ("pydantic", "pydantic"),
        ("mcp", "mcp"),
        ("langgraph", "langgraph"),
        ("langchain", "langchain"),
        ("lancedb", "lancedb"),
        ("kuzu", "kuzu"),
        ("opentelemetry", "opentelemetry-api"),
    ]
    deps: dict[str, str | None] = {}
    for import_name, display_name in critical_deps:
        try:
            mod = __import__(import_name)
            ver = getattr(mod, "__version__", None) or getattr(
                getattr(mod, "version", None), "__version__", "unknown"
            )
            deps[display_name] = str(ver)
        except ImportError:
            deps[display_name] = None
    info["critical_dependencies"] = deps

    # Mandatory secret-scrubber dependency (re2 / google-re2)
    import importlib.util

    info["google_re2"] = (
        "installed"
        if importlib.util.find_spec("re2") is not None
        else "MISSING — secret scrubbing will fail closed"
    )
    info["feature_flags"] = dict(_c.FEATURE_FLAGS)
    info["supported_workflows"] = list(_c.SUPPORTED_WORKFLOWS)
    info["default_workflow"] = _c.WORKFLOW_DEFAULT

    # ── Run the standard startup checks ─────────────────────────────
    results = run_startup_checks(include_optional=True)
    info["startup_checks"] = [
        {
            "name": r.name,
            "status": r.status,
            "message": r.message,
            "fix_hint": r.fix_hint,
        }
        for r in results
    ]
    has_fail = any(r.status == "fail" for r in results)

    if json_output:
        console.print_json(_json.dumps(info, indent=2, default=str))
    else:
        # Human-readable rendering
        console.print(f"\n[bold]Beagle Doctor — v{_pkg_version}[/bold]")
        console.print(f"Python {info['python_version']} on {info['platform']}\n")
        console.print("[bold]Critical dependencies:[/bold]")
        for name, ver in deps.items():
            if ver is None:
                console.print(f"  [red]✗[/red] {name}: [red]NOT INSTALLED[/red]")
            else:
                console.print(f"  [green]✓[/green] {name}: {ver}")
        re2_status = info["google_re2"]
        if re2_status == "installed":
            console.print("  [green]✓[/green] google-re2: installed")
        else:
            console.print(f"  [red]✗[/red] google-re2: {re2_status}")
        console.print(f"\n[bold]Feature flags:[/bold] {info['feature_flags']}")
        console.print(
            "\n[bold]Supported workflows:[/bold] "
            f"{', '.join(str(w) for w in info['supported_workflows'])}"
        )
        console.print("\n[bold]Startup checks:[/bold]")
        console.print(format_startup_report(results))

    if has_fail:
        raise typer.Exit(1)


# ============================================================================
# SLO Commands
# ============================================================================

# CrewAI + AutoGen Bridge Commands
# ============================================================================
