"""Run introspection commands (history, stats, findings, diff, replay).

Extracted from cli.py in the v1.0.0 F2 split. Registered flat on the root
app via ``app.add_typer(runs_app)`` (no name), so the CLI surface is
byte-identical to the pre-split monolith.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time as _time
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from ...tracking import RunDiffer, TrackingDatabase
from ..cli_formatters import format_stats_panel

console = Console()

runs_app = typer.Typer()


@runs_app.command("stats")
def show_stats(
    days: int = typer.Option(7, "--days", "-d", help="Stats for the last N days"),
    json_output: bool = typer.Option(
        False, "--json", help="Emit machine-readable JSON instead of the rich panel"
    ),
) -> None:
    """Show aggregate execution statistics."""
    db = TrackingDatabase.get_instance()
    stats = db.get_stats(since_days=days)

    total_runs = stats["total_runs"]
    total_cost = stats["total_cost_usd"]
    avg_cost = (total_cost / total_runs) if total_runs > 0 else 0.0

    payload = {
        "window_days": days,
        "total_workflows": 0,  # Could be added to DB
        "total_runs": total_runs,
        "successful_runs": 0,  # Add to stats query
        "failed_runs": 0,
        "total_cost_usd": total_cost,
        "avg_cost_per_run_usd": avg_cost,
    }

    if json_output:
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
        return

    panel = format_stats_panel(
        {
            "total_workflows": payload["total_workflows"],
            "total_runs": payload["total_runs"],
            "successful_runs": payload["successful_runs"],
            "failed_runs": payload["failed_runs"],
            "total_cost": payload["total_cost_usd"],
            "avg_cost": payload["avg_cost_per_run_usd"],
        }
    )
    console.print(panel)


# ============================================================================
# Agent Commands
# ============================================================================


@runs_app.command("history")
def run_history(
    limit: int = typer.Option(20, "--limit", "-n", help="Number of runs to show"),
) -> None:
    """Show history of recent workflow runs."""
    db = TrackingDatabase.get_instance()
    runs = db.get_workflow_runs(limit=limit)

    if not runs:
        console.print("[yellow]No run history found.[/yellow]")
        return

    table = Table(title=f"Recent Workflow Runs (Last {limit})")
    table.add_column("Run ID", style="cyan", no_wrap=True)
    table.add_column("Workflow", style="magenta")
    table.add_column("Status", justify="center")
    table.add_column("Cost", justify="right", style="green")
    table.add_column("Tokens", justify="right")
    table.add_column("Duration", justify="right")
    table.add_column("Started At", style="dim")

    for run in runs:
        status = "[green]PASS[/green]" if run.success else "[red]FAIL[/red]"
        duration = f"{run.total_duration_seconds:.1f}s" if run.total_duration_seconds else "-"
        started = _time.ctime(run.started_at)

        table.add_row(
            run.id[:8],
            run.workflow_name,
            status,
            f"${run.total_cost_usd:.4f}",
            f"{run.total_tokens:,}",
            duration,
            started,
        )

    console.print(table)


@runs_app.command("findings")
def show_findings(
    run_id: str = typer.Argument(..., help="Run ID to show findings for"),
    severity: str | None = typer.Option(
        None, "--severity", "-s", help="Filter by severity (comma-separated)"
    ),
) -> None:
    """Show findings for a specific run."""
    db = TrackingDatabase.get_instance()
    findings = db.get_findings_for_run(run_id)

    if not findings:
        # Try with prefix if not found
        runs = db.get_workflow_runs(limit=100)
        matches = [r for r in runs if r.id.startswith(run_id)]
        if len(matches) == 1:
            findings = db.get_findings_for_run(matches[0].id)
        else:
            console.print(f"[yellow]No findings found for run {run_id}.[/yellow]")
            return

    table = Table(title=f"Findings for Run {run_id[:8]}")
    table.add_column("Sev", justify="center")
    table.add_column("Category", style="dim")
    table.add_column("Title", style="bold")
    table.add_column("File:Line", style="cyan")

    sev_colors = {
        "critical": "bold red",
        "high": "red",
        "medium": "yellow",
        "low": "blue",
        "info": "dim",
    }

    for f in findings:
        if severity and f.severity not in severity.split(","):
            continue

        sev_style = sev_colors.get(f.severity, "white")
        location = f"{f.file_path}:{f.line_number}" if f.file_path else "-"

        table.add_row(
            f"[{sev_style}]{f.severity[:4].upper()}[/{sev_style}]",
            f.category,
            f.title,
            location,
        )

    console.print(table)


@runs_app.command("diff")
def diff_runs(
    run_id_a: str = typer.Argument(..., help="Baseline run ID"),
    run_id_b: str = typer.Argument(..., help="Current run ID"),
) -> None:
    """Compare two workflow runs."""
    db = TrackingDatabase.get_instance()
    differ = RunDiffer(db)

    # Resolve short IDs
    runs = db.get_workflow_runs(limit=100)

    def resolve(sid):
        matches = [r.id for r in runs if r.id.startswith(sid)]
        return matches[0] if len(matches) == 1 else sid

    id_a = resolve(run_id_a)
    id_b = resolve(run_id_b)

    diff = differ.compare(id_a, id_b)

    console.print(f"\n[bold]Comparing {id_a[:8]} ➔ {id_b[:8]}[/bold]\n")

    if diff.new_findings:
        console.print(f"[red]New Findings ({len(diff.new_findings)}):[/red]")
        for f in diff.new_findings:
            console.print(f"  + [{f.severity}] {f.title}")

    if diff.resolved_findings:
        console.print(f"\n[green]Resolved Findings ({len(diff.resolved_findings)}):[/green]")
        for f in diff.resolved_findings:
            console.print(f"  - [{f.severity}] {f.title}")

    if not diff.new_findings and not diff.resolved_findings:
        console.print("[yellow]No findings changed between runs.[/yellow]")


@runs_app.command("replay")
def replay_cmd(
    manifest: str = typer.Argument(..., help="Path to replay manifest JSON file"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Describe replay without executing"),
) -> None:
    """Replay a previous workflow execution deterministically.

    Loads a replay manifest saved during a previous run and
    re-executes the workflow with the same inputs and model settings.

    Use --dry-run to inspect what would be replayed without execution.
    """

    from rich.table import Table

    from ...reproducibility.manifest import ReplayManifest

    manifest_path = Path(manifest)
    if not manifest_path.exists():
        console.print(f"[red]Manifest not found: {manifest_path}[/red]")
        raise typer.Exit(1)

    try:
        loaded_manifest = ReplayManifest.load(manifest_path)
    except (ValueError, OSError, json.JSONDecodeError) as e:  # Narrowed from bare Exception
        console.print(f"[red]Failed to load manifest: {e}[/red]")
        raise typer.Exit(1) from e

    if dry_run:
        table = Table(title=f"Replay Manifest: {manifest_path.name}")
        table.add_column("Field", style="cyan")
        table.add_column("Value", style="magenta")
        table.add_row("Workflow ID", loaded_manifest.workflow_id)
        table.add_row(
            "Query",
            loaded_manifest.query[:100] + "..."
            if len(loaded_manifest.query) > 100
            else loaded_manifest.query,
        )
        table.add_row("Mode", loaded_manifest.mode)
        table.add_row("Seed", loaded_manifest.seed or "(auto)")
        table.add_row("Nodes", str(len(loaded_manifest.node_inputs)))
        table.add_row("Started", str(loaded_manifest.started_at))
        table.add_row("Completed", str(loaded_manifest.completed_at))
        console.print(table)

        if loaded_manifest.node_inputs:
            node_table = Table(title="Node Inputs")
            node_table.add_column("Node", style="cyan")
            node_table.add_column("Model", style="green")
            node_table.add_column("Attempt", style="yellow")
            for ni in loaded_manifest.node_inputs:
                node_table.add_row(ni.node_name, ni.model, str(ni.attempt))
            console.print(node_table)
        return

    # Execute replay
    from ...reproducibility.replay import ReplayEngine

    engine = ReplayEngine(loaded_manifest)
    console.print(f"[bold green]Replaying workflow {loaded_manifest.workflow_id}...[/bold green]")
    result = asyncio.run(engine.replay())
    console.print("[green]Replay complete.[/green]")
    if result:
        console.print(result)
