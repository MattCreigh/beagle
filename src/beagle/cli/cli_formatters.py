"""Output formatting utilities for CLI.

Provides Rich-based formatters for tables, panels, and console output.
"""

from __future__ import annotations

import time
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()


def format_workflow_table(workflows: list[dict[str, Any]], verbose: bool = False) -> Table:
    """Create a formatted table of workflows."""
    if verbose:
        table = Table(title="Available Workflows (Verbose)", show_lines=True)
        table.add_column("Path", style="cyan", no_wrap=False)
        table.add_column("Name", style="green")
        table.add_column("Description", style="dim")
        table.add_column("Nodes", justify="right")
        table.add_column("Mode", style="yellow")

        for wf in workflows:
            desc = wf.get("description", "")
            desc_display = desc[:60] + "..." if len(desc) > 60 else desc
            table.add_row(
                str(wf.get("path", "")),
                wf.get("name", "N/A"),
                desc_display,
                str(wf.get("node_count", "N/A")),
                wf.get("mode", "N/A"),
            )
    else:
        table = Table(title="Available Workflows")
        table.add_column("Name", style="cyan")
        table.add_column("Description", style="dim")
        table.add_column("Mode", style="yellow")

        for wf in workflows:
            desc = wf.get("description", "")
            desc_display = desc[:50] + "..." if len(desc) > 50 else desc
            table.add_row(
                wf.get("name", "N/A"),
                desc_display,
                wf.get("mode", "N/A"),
            )

    return table


def format_checkpoint_table(checkpoints: list[dict[str, Any]]) -> Table:
    """Create a formatted table of checkpoints."""
    table = Table(title="Checkpoints")
    table.add_column("Workflow ID", style="cyan")
    table.add_column("Query")
    table.add_column("Nodes", justify="right")
    table.add_column("Age")

    now = time.time()
    for cp in checkpoints:
        age_seconds = now - cp.get("timestamp", now)
        if age_seconds < 3600:
            age = f"{age_seconds / 60:.0f} min ago"
        elif age_seconds < 86400:
            age = f"{age_seconds / 3600:.1f} hours ago"
        else:
            age = f"{age_seconds / 86400:.1f} days ago"

        query = cp.get("query", "")
        query_display = query[:40] + "..." if len(query) > 40 else query
        table.add_row(
            cp.get("workflow_id", "N/A"),
            query_display,
            str(cp.get("node_count", "N/A")),
            age,
        )

    return table


def format_history_table(runs: list[dict[str, Any]]) -> Table:
    """Create a formatted table of workflow run history."""
    table = Table(title="Workflow Run History")
    table.add_column("Workflow", style="cyan")
    table.add_column("Query")
    table.add_column("Status", style="green")
    table.add_column("Cost", justify="right")
    table.add_column("Timestamp")

    for run in runs:
        query = run.get("query", "")
        query_display = query[:40] + "..." if len(query) > 40 else query
        table.add_row(
            run.get("workflow", "N/A"),
            query_display,
            run.get("status", "unknown"),
            f"${run.get('cost', 0):.4f}",
            run.get("timestamp", "N/A"),
        )

    return table


def format_stats_panel(stats: dict[str, Any]) -> Panel:
    """Create a formatted panel for statistics."""
    content_lines = [
        f"[bold]Total Workflows:[/bold] {stats.get('total_workflows', 0)}",
        f"[bold]Total Runs:[/bold] {stats.get('total_runs', 0)}",
        f"[bold]Successful Runs:[/bold] {stats.get('successful_runs', 0)}",
        f"[bold]Failed Runs:[/bold] {stats.get('failed_runs', 0)}",
        f"[bold]Total Cost:[/bold] ${stats.get('total_cost', 0):.4f}",
        f"[bold]Avg Cost/Run:[/bold] ${stats.get('avg_cost', 0):.4f}",
    ]
    return Panel("\n".join(content_lines), title="Workflow Statistics", border_style="blue")


def format_agent_panel(agent_info: dict[str, Any]) -> Panel:
    """Create a formatted panel for agent information."""
    content_lines = [
        f"[bold]Name:[/bold] {agent_info.get('name', 'N/A')}",
        f"[bold]Status:[/bold] {agent_info.get('status', 'N/A')}",
        f"[bold]Model:[/bold] {agent_info.get('model', 'N/A')}",
        f"[bold]Current Task:[/bold] {agent_info.get('current_task', 'N/A')}",
        f"[bold]Cost:[/bold] ${agent_info.get('cost', 0):.4f}",
        f"[bold]Tokens:[/bold] {agent_info.get('total_tokens', 0):,}",
    ]
    return Panel("\n".join(content_lines), title="Agent Information", border_style="green")


def print_workflow_header(
    workflow: str,
    query: str,
    budget: float,
    mode: str,
    steering: str | None = None,
    approve_all: bool = False,
) -> None:
    """Print formatted workflow execution header."""
    mode_colors = {"audit": "yellow", "develop": "green", "research": "cyan"}
    color = mode_colors.get(mode, "white")

    console.print(f"[bold blue]Starting workflow:[/bold blue] {workflow}")
    console.print(f"[dim]Query:[/dim] {query[:100]}...")
    console.print(f"[dim]Budget:[/dim] ${budget:.2f}")  # Fixed: console.print instead of print
    console.print(f"[dim]Mode:[/dim] [{color}]{mode}[/{color}]")

    if steering:
        console.print(f"[dim]Steering:[/dim] {steering}")
    if approve_all:
        console.print("[yellow]Approval: --approve-all enabled[/yellow]")


def print_cost_estimate(
    phase_costs: dict[str, float], total_cost: float, model: str, total_tokens: int
) -> None:
    """Print formatted cost estimate."""
    console.print("\n[bold]Cost Estimate[/bold]\n")

    for phase, cost in phase_costs.items():
        console.print(f"  {phase}: ~${cost:.4f}")

    console.print(f"\n[bold]Total Estimate:[/bold] ~${total_cost:.4f}")
    console.print(f"[dim]Model: {model}[/dim]")
    console.print(f"[dim](~{total_tokens:,} tokens)[/dim]")  # Fixed: added opening parenthesis
    console.print("\n[yellow]Note: Actual costs may vary based on prompt/response length[/yellow]")


def print_success(message: str) -> None:
    """Print a success message."""
    console.print(f"[bold green]+[/bold green] {message}")  # Fixed: broken markup


def print_error(message: str) -> None:
    """Print an error message."""
    console.print(f"[bold red]X[/bold red] {message}")


def print_warning(message: str) -> None:
    """Print a warning message."""
    console.print(f"[yellow]![/yellow] {message}")


def print_info(message: str) -> None:
    """Print an info message."""
    console.print(f"[dim]{message}[/dim]")
