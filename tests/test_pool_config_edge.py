"""Test that the edge worker clamp is honoured (R4.3, v13.20.8).

Per audit V5/B2 (and R1.3 which fixed the psutil-missing path),
_EDGE_MAX_WORKERS = 4 must clamp the returned worker count from
_get_pool_config_workers() regardless of CPU count or memory size
on the deployed low-power edge target.

This test exercises three paths:
  1. The happy path: a mocked large machine returns min(real, 4)
  2. The psutil-missing path: an ImportError on psutil also returns <= 4
  3. The constant is 4 (not 8, not configurable) on the edge target
"""

from __future__ import annotations

import sys
from unittest.mock import patch


def test_edge_max_workers_constant_is_4() -> None:
    """The edge ceiling is 4; this is the contract for the deployment target."""
    from beagle.utils.subprocess.pool_config import _EDGE_MAX_WORKERS

    assert _EDGE_MAX_WORKERS == 4, (
        f"_EDGE_MAX_WORKERS must be 4 for the edge deployment target, "
        f"got {_EDGE_MAX_WORKERS}. This constant is a contract, not a knob."
    )


def test_pool_config_workers_clamps_to_edge_ceiling() -> None:
    """A 32-CPU 64-GB machine returns <= 4 workers (edge contract)."""
    from beagle.utils.subprocess import pool_config

    with patch.object(pool_config, "psutil", create=True) as mock_psutil:
        # 32 CPUs, 64 GB available: well above both the <2 GB and <8 GB
        # thresholds. Without the edge clamp, the function would return
        # min(int(32*1.5), 8) = 8. The contract is min(real, 4) = 4.
        mock_psutil.virtual_memory.return_value.available = 64 * 1024**3
        with patch("os.cpu_count", return_value=32):
            workers = pool_config._get_pool_config_workers()
            assert workers <= pool_config._EDGE_MAX_WORKERS, (
                f"Got {workers} workers on a 32-CPU machine; "
                f"expected <= {pool_config._EDGE_MAX_WORKERS} per the edge contract."
            )
            assert workers == 4


def test_pool_config_workers_psutil_missing_also_clamps() -> None:
    """The psutil-missing path (audit V5 / R1.3 fix) also returns <= 4."""
    from beagle.utils.subprocess import pool_config

    # Force ImportError on the psutil import inside the function.
    # The R1.3 fix moved the _EDGE_MAX_WORKERS clamp into the ImportError
    # branch; this test pins that.
    with patch.dict(sys.modules, {"psutil": None}), patch("os.cpu_count", return_value=32):
        workers = pool_config._get_pool_config_workers()
        assert workers <= pool_config._EDGE_MAX_WORKERS, (
            f"psutil-missing path returned {workers} workers; "
            f"expected <= {pool_config._EDGE_MAX_WORKERS} per the R1.3 fix."
        )
