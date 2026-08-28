# Copyright (c) 2026 Matt Creigh. Released under the MIT License.
# SPDX-License-Identifier: MIT
"""Beacon coordination roster commands.

See plans/beagle-beacon-coordination.xml WP-8.

This CLI is a READ-ONLY observer. It connects over the socket, never holds
a coordination lease (it does not call CoordSession.attach()), and never
writes to the store — the roster layout is concept spec section 6.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import typer
from rich.console import Console
from rich.live import Live
from rich.table import Table

from beagle.beacon.backends import get_driver
from beagle.beacon.keys import resolve_paths
from beagle.beacon.records import AgentRecord
from beagle.beacon.spawn import is_live
from beagle.config.config import get_config

logger = logging.getLogger(__name__)
console = Console(stderr=True)

coord_app = typer.Typer(help="Beacon coordination roster")


def _read_roster(workdir: Path) -> list[AgentRecord] | None:
    """Read the live roster directly, read-only. None if no Beacon is live."""
    paths = resolve_paths(workdir)
    probe_timeout_s = get_config().coord.probe_timeout_s
    if not is_live(paths, connect_timeout_s=probe_timeout_s):
        return None

    coord = get_config().coord
    client = get_driver(coord.backend).connect(
        paths, connect_timeout_s=coord.probe_timeout_s, options=coord.backend_options
    )
    try:
        agent_ids = client.smembers("agent:list")
        records = []
        for agent_id in agent_ids:
            data = client.hgetall(f"agent:{agent_id}")
            if not data:
                continue  # lease expired, not yet swept
            records.append(AgentRecord.from_hash(data))
        return records
    finally:
        client.close()


def _roster_table(records: list[AgentRecord]) -> Table:
    table = Table(title="Beacon — live agents")
    table.add_column("agent_id", style="cyan", no_wrap=True)
    table.add_column("colour")
    table.add_column("model")
    table.add_column("phase")
    table.add_column("plan")
    table.add_column("work")
    table.add_column("files")
    for r in records:
        table.add_row(
            r.agent_id,
            r.colour,
            r.model,
            r.phase,
            r.current_plan,
            r.current_work,
            ", ".join(r.files) if r.files else "",
        )
    return table


@coord_app.command("status")
def coord_status() -> None:
    """Print the current Beacon roster for the working directory, once."""
    workdir = Path.cwd()
    records = _read_roster(workdir)
    if records is None:
        console.print(f"no Beacon running in {workdir}")
        return
    if not records:
        console.print(f"Beacon is live for {workdir}, no agents currently attached")
        return
    console.print(_roster_table(records))
    _print_journal_health(workdir)


def _print_journal_health(workdir: Path) -> None:
    """Render the journal durability status file, if present (audit A2).

    The write-behind journal publishes its fsync health to
    ``<base>/journal/journal_status.json`` on every flush cycle; this is
    the operator-visible surfacing the release audit demanded. A missing
    or unreadable file is reported as such — silence would itself be a
    durability signal.

    Args:
        workdir: The directory whose Beacon instance to inspect.

    """
    status_path = resolve_paths(workdir).base_dir / "journal" / "journal_status.json"
    try:
        payload = json.loads(status_path.read_text(encoding="utf-8"))
        state = payload.get("state", "unknown")
        errors = payload.get("fsync_error_count", 0)
        style = "green" if state == "ok" and errors == 0 else "red"
        console.print(
            f"journal: [{style}]{state}[/{style}] "
            f"fsyncs={payload.get('fsync_count', '?')} "
            f"errors={errors}"
        )
    except FileNotFoundError:
        console.print("journal: no status file (journal never flushed here)")
    except (json.JSONDecodeError, OSError) as exc:
        console.print(f"journal: status file unreadable ({exc})")


@coord_app.command("watch")
def coord_watch() -> None:
    """Live-refresh the Beacon roster on a configurable interval. Ctrl-C stops."""
    workdir = Path.cwd()
    poll_interval_s = get_config().coord.watch_poll_interval_s
    records = _read_roster(workdir)
    if records is None:
        console.print(f"no Beacon running in {workdir}")
        return

    try:
        with Live(_roster_table(records or []), console=console, refresh_per_second=1) as live:
            while True:
                time.sleep(poll_interval_s)
                current = _read_roster(workdir)
                if current is None:
                    live.update(Table(title=f"Beacon for {workdir} is no longer running"))
                    break
                live.update(_roster_table(current))
    except KeyboardInterrupt:
        logger.info("coord watch stopped by Ctrl-C")
        raise typer.Exit(code=0) from None
