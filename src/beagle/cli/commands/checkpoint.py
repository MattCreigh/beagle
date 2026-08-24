"""Checkpoint management commands."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import typer

logger = logging.getLogger(__name__)

try:
    from rich.console import Console
except ImportError:  # pragma: no cover

    class Console:  # type: ignore[misc,no-redef]
        def print(self, *args, **kwargs):
            logger.info(*args)


console = Console(stderr=True)

checkpoint_app = typer.Typer(help="Checkpoint management commands")


def _list_snapshots(snapshots_dir: Path) -> tuple[int, float, str | None]:
    """Count snapshots, total size, and latest path."""
    if not snapshots_dir.exists():
        return 0, 0.0, None
    snapshots = sorted(snapshots_dir.glob("*.snap"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not snapshots:
        return 0, 0.0, None
    total = sum(s.stat().st_size for s in snapshots)
    return len(snapshots), total, str(snapshots[0])


@checkpoint_app.command("list")
def checkpoint_list() -> None:
    """List available checkpoints and snapshots."""
    try:
        from ...lifecycle.checkpoint import list_checkpoints

        checkpoints = list_checkpoints()
    except Exception as exc:  # broad catch intentional
        console.print(f"[bold red]Error listing checkpoints:[/bold red] {exc}")
        raise typer.Exit(1) from exc

    if not checkpoints:
        console.print("[yellow]No checkpoints found[/yellow]")
        return

    from rich.table import Table

    table = Table(title="Checkpoints")
    table.add_column("ID", style="cyan")
    table.add_column("Workflow", style="green")
    table.add_column("Timestamp", style="magenta")
    table.add_column("Status", style="yellow")

    for cp in checkpoints[:20]:
        table.add_row(
            str(cp.timestamp)[:8],
            cp.active_workflow_id or "unknown",
            str(cp.timestamp),
            cp.restart_reason or "unknown",
        )
    console.print(table)
    console.print(f"\nTotal: {len(checkpoints)} checkpoints")


@checkpoint_app.command()
def resume(
    checkpoint_id: str = typer.Argument(..., help="Checkpoint ID to resume from"),
    skip_errors: bool = typer.Option(False, "--skip-errors", help="Skip corrupted checkpoints"),
) -> None:
    """Resume a workflow from a checkpoint."""
    try:
        from ...lifecycle.restore import restore_from_checkpoint

        result = asyncio.run(restore_from_checkpoint(checkpoint_id, skip_errors=skip_errors))
        if result:
            console.print(f"[bold green]Resumed from checkpoint {checkpoint_id[:8]}[/bold green]")
        else:
            console.print("[bold red]Resume failed[/bold red]")
            raise typer.Exit(1)
    except Exception as exc:  # broad catch intentional
        console.print(f"[bold red]Error resuming checkpoint:[/bold red] {exc}")
        raise typer.Exit(1) from exc


@checkpoint_app.command()
def cleanup(
    keep_recent: int = typer.Option(
        10, "--keep-recent", "-k", help="Number of checkpoints to keep"
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be deleted"),
) -> None:
    """Clean up old checkpoints, keeping the N most recent."""
    try:
        from ...lifecycle.checkpoint import delete_checkpoint, list_checkpoints

        checkpoints = list_checkpoints()
        if len(checkpoints) <= keep_recent:
            console.print(f"[green]Nothing to clean ({len(checkpoints)} ≤ {keep_recent})[/green]")
            return

        to_remove = checkpoints[keep_recent:]
        if dry_run:
            console.print(f"[yellow]Would delete {len(to_remove)} checkpoints:[/yellow]")
            for cp in to_remove:
                console.print(f"  - {str(cp.timestamp)[:8]}")
            return

        deleted = 0
        failed = 0
        for cp in to_remove:
            try:
                delete_checkpoint(str(cp.timestamp))
                deleted += 1
            except (OSError, RuntimeError, ValueError, KeyError) as exc:
                failed += 1
                console.print(f"[yellow]Could not delete checkpoint {cp.timestamp}: {exc}[/yellow]")
        console.print(f"[green]Deleted {deleted} checkpoints[/green]")
        if failed:
            console.print(
                f"[yellow]{failed} checkpoint(s) could not be deleted and remain on disk[/yellow]"
            )
    except Exception as exc:  # broad catch intentional
        console.print(f"[bold red]Error cleaning checkpoints:[/bold red] {exc}")
        raise typer.Exit(1) from exc
