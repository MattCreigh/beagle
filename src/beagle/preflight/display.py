"""Confirmation display for Beagle pre-flight check.

Renders cost and time forecasts using Rich.
"""

from __future__ import annotations

import logging

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

from .estimator import PreFlightEstimate

logger = logging.getLogger("Beagle.preflight.display")

console = Console()


def display_preflight_check(estimate: PreFlightEstimate) -> str:
    """Render the pre-flight check and prompt for confirmation.

    Returns:
        "y" for Yes, "n" for No, "a" for Adjust budget

    """
    # Create the main table
    table = Table(
        title=f"Beagle Pre-Flight Check: {estimate.workflow_name}",
        box=box.DOUBLE_EDGE,
        show_header=True,
        header_style="bold cyan",
        expand=True,
    )

    table.add_column("Node", style="dim")
    table.add_column("Model", style="magenta")
    table.add_column("Est.$", justify="right", style="green")
    table.add_column("Est. Time", justify="right", style="yellow")

    for node in estimate.nodes:
        # Format runtime
        runtime = node.estimated_runtime_seconds
        time_str = f"~{runtime:.0f}s" if runtime < 60 else f"~{runtime / 60:.1f}m"

        table.add_row(node.node_name, node.model, f"${node.estimated_cost_usd:.3f}", time_str)

    # Add Total row
    total_runtime = estimate.total_estimated_runtime_seconds
    if total_runtime < 60:
        total_time_str = f"~{total_runtime:.0f}s"
    else:
        total_time_str = f"~{total_runtime / 60:.1f}m"

    table.add_section()
    table.add_row(
        "TOTAL",
        "",
        f"[bold]${estimate.total_estimated_cost_usd:.3f}[/bold]",
        f"[bold]{total_time_str}[/bold]",
    )

    # Header Info
    header_info = (
        f"Workflow: [bold]{estimate.workflow_name}[/bold]   "
        f"Nodes: [bold]{estimate.node_count}[/bold]\n"
    )
    header_info += f"Budget:   [bold]${estimate.budget_usd:.2f}[/bold]"

    # Display everything
    console.print()
    console.print(Panel(header_info, border_style="cyan", title="Summary"))
    console.print(table)

    # Display warnings
    for warning in estimate.warnings:
        console.print(f"[yellow]⚠ {warning}[/yellow]")

    # Budget sufficiency
    if estimate.budget_sufficient:
        console.print(
            f"[green]✅ Budget sufficient (${estimate.budget_usd:.2f} "
            f"> ${estimate.total_estimated_cost_usd:.3f})[/green]"
        )
    else:
        console.print(
            f"[red]❌ Budget insufficient (${estimate.budget_usd:.2f} "
            f"< ${estimate.total_estimated_cost_usd:.3f})[/red]"
        )

    # Prompt
    choice = str(Prompt.ask("\nProceed?", choices=["y", "n", "a"], default="y")).lower()

    return choice


def log_preflight_estimate(estimate: PreFlightEstimate) -> None:
    """Non-interactive logging of the estimate (for headless mode).

    v1.0.9 (audit M5): routed through the logging module instead of
    ``console.print``. The previous Rich Console applied ANSI highlighting
    even in headless mode, so a downstream ``grep '[PREFLIGHT]'`` found
    nothing. Logging emits plain text that is greppable and colour-free.
    """
    logger.info(
        "[PREFLIGHT] Workflow %r estimate: $%.3f / %s tokens. Sufficient: %s",
        estimate.workflow_name,
        estimate.total_estimated_cost_usd,
        f"{estimate.total_estimated_tokens:,}",
        estimate.budget_sufficient,
    )
