"""Relay Task B — Kùzu memory-bounds regression test.

Spins up Kùzu with a tiny ``max_db_size`` (the 8 MiB floor), attempts to
insert data beyond that limit, and asserts the system raises an explicit
memory error instead of mmap'ing the entire disk.

This locks the v13.22.3 H1 fix: Kùzu's default is an unbounded 8 TB mmap
that OOMs memory-constrained hosts. Beagle passes an explicit
``max_db_size`` (env-gated via ``BEAGLE_KUZU_MAX_DB_SIZE_MB``) so an
over-limit insert fails loudly with a buffer-manager error rather than
silently growing the on-disk DB without bound.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

kuzu = pytest.importorskip("kuzu", reason="kuzu not installed")

# Kùzu's documented minimum max_db_size.
KUZU_MIN_DB_SIZE = 8 * 1024 * 1024  # 8 MiB


def test_kuzu_raises_explicit_error_on_overflow() -> None:
    """Inserting beyond max_db_size must raise, not mmap the whole disk."""
    tmp = tempfile.mkdtemp(prefix="kuzu_bounds_")
    db_path = Path(tmp) / "db"

    db = kuzu.Database(str(db_path), max_db_size=KUZU_MIN_DB_SIZE)
    conn = kuzu.Connection(db)
    conn.execute("CREATE NODE TABLE T(id INT64, data STRING, PRIMARY KEY(id))")

    # Insert enough data to exceed the 8 MiB cap. Each row is ~2 KB, so
    # 20k rows ≈ 40 MB — well over the limit.
    with pytest.raises(RuntimeError, match=r"frame groups|Buffer manager|allocator"):
        for i in range(20000):
            conn.execute(
                "CREATE (t:T {id: $i, data: $d})",
                parameters={"i": i, "d": "x" * 2000},
            )


def test_kuzu_min_db_size_floor() -> None:
    """Kùzu rejects a max_db_size below its 8 MiB floor with a clear error."""
    tmp = tempfile.mkdtemp(prefix="kuzu_bounds_")
    db_path = Path(tmp) / "db"

    with pytest.raises(RuntimeError, match="at least 8388608"):
        kuzu.Database(str(db_path), max_db_size=1024 * 1024)  # 1 MiB < floor


def test_beagle_kuzu_max_db_size_is_env_gated() -> None:
    """Beagle's Kùzu open must read BEAGLE_KUZU_MAX_DB_SIZE_MB at call time."""
    from beagle.infrastructure import cast_ingestion as ci

    source = (Path(ci.__file__).parent / "cast_ingestion.py").read_text(encoding="utf-8")
    assert "BEAGLE_KUZU_MAX_DB_SIZE_MB" in source, (
        "cast_ingestion.py must read BEAGLE_KUZU_MAX_DB_SIZE_MB so operators "
        "can cap the Kùzu DB size on memory-constrained hosts."
    )
    assert "max_db_size=" in source, (
        "kuzu.Database calls must set max_db_size explicitly (not the 8 TB mmap default)."
    )
