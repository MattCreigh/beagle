"""Service Level Objective commands."""

from __future__ import annotations

import json
import time as _time

import typer
from rich.console import Console
from rich.table import Table

console = Console()
slo_app = typer.Typer(help="Service Level Objective commands")


@slo_app.command("status")
def slo_status(
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """Show current SLO compliance status."""
    from ...slo import SLOTracker

    tracker = SLOTracker()
    report = tracker.generate_report()

    if json_output:
        console.print_json(json.dumps(report.to_dict(), indent=2, default=str))
        return

    console.print("\n[bold cyan]SLO Compliance Status[/bold cyan]\n")
    ts = _time.strftime("%Y-%m-%d %H:%M:%S", _time.localtime(report.generated_at))
    console.print(f"Window: last {report.window_days} days  |  Generated: {ts}")
    status_emoji = "✅" if report.overall_status == "healthy" else "⚠️"
    console.print(f"Overall: [bold]{status_emoji} {report.overall_status.upper()}[/bold]\n")

    table = Table(
        title="Service Level Objectives",
        header_style="bold magenta",
        show_header=True,
    )
    table.add_column("SLI", style="cyan", min_width=22)
    table.add_column("Target", justify="right", min_width=8)
    table.add_column("Compliance", justify="right", min_width=10)
    table.add_column("Budget", justify="right", min_width=8)
    table.add_column("Status", justify="center", min_width=10)
    table.add_column("Action", min_width=12)

    from ...slo.indicators import get_sli
    from ...slo.objectives import get_slo

    for sli_type, data in report.measurements.items():
        slo_obj = get_slo(sli_type)
        sli_def = get_sli(sli_type)
        if not slo_obj or not sli_def:
            continue
        budget = report.budgets.get(sli_type)
        status_str = budget.status.value if budget else "unknown"
        action_str = budget.velocity_action.value if budget else "unknown"

        status_color = {
            "healthy": "green",
            "caution": "yellow",
            "burning": "red",
            "exhausted": "white on red",
        }.get(status_str, "dim")

        table.add_row(
            sli_def.name,
            slo_obj.target_display,
            f"{data['compliance_percent']:.1f}%",
            f"{budget.budget_percent:.1f}%" if budget else "N/A",
            f"[{status_color}]{status_str.upper()}[/{status_color}]",
            action_str,
        )

    console.print(table)

    if report.worst_sli:
        worst = report.budgets.get(report.worst_sli)
        if worst:
            er = worst.error_rate * 100
            br = worst.burn_rate
            console.print(
                f"\n[yellow]Worst: {report.worst_sli} ({er:.1f}% error, {br:.1f}x burn)[/yellow]"
            )


@slo_app.command("report")
def slo_report(
    sli_filter: str | None = typer.Option(None, "--sli", help="Filter by SLI name"),
    limit: int = typer.Option(50, "--limit", help="Recent events limit"),
) -> None:
    """Show detailed SLO report with event history."""
    from ...slo import SLOTracker
    from ...slo.indicators import SLIType

    tracker = SLOTracker()
    report = tracker.generate_report()

    if sli_filter:
        slis = [sli_filter]
        from ...slo.objectives import get_slo as _get_slo

        if not _get_slo(sli_filter):
            console.print(f"[red]Unknown SLI: {sli_filter}[/red]")
            raise typer.Exit(1)
    else:
        slis = [s.value for s in SLIType]

    for sli in slis:
        data = report.measurements.get(sli)
        if not data:
            continue
        budget = report.budgets.get(sli)

        console.print(f"\n[bold cyan]{sli}[/bold cyan]")
        console.print(f"  Events: {data['total_events']} total, {data['bad_events']} bad")
        console.print(f"  Compliance: {data['compliance_percent']:.2f}%")
        if budget:
            console.print(f"  Budget: {budget.budget_percent:.2f}% remaining")
            console.print(f"  Burn rate: {budget.burn_rate:.2f}x expected")
            if budget.days_until_burn is not None:
                console.print(f"  Days until burn: {budget.days_until_burn:.1f}")

        history = tracker.query_history(sli, limit=min(limit, 20))
        if history:
            console.print(f"  Recent ({len(history)}):", style="dim")
            for h in history[:5]:
                ts = _time.strftime("%m-%d %H:%M", _time.localtime(h["timestamp"]))
                bad_mark = "❌" if h["bad"] else "✅"
                console.print(
                    f"    {bad_mark} {ts} value={h['value']:.2f}",
                    style="dim",
                )

    console.print()
