"""Regression test suite for Phase 4 silent-failure cluster (audit B-7, B-8, B-9, B-10, B-11).

Asserts that operations succeed cleanly and fail loudly when appropriate,
rather than swallowing exceptions or raising unexpected errors.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from beagle.cli.cli import app
from beagle.core.autonomous_orchestrator import DAGOrchestrator
from beagle.infrastructure.health_check import main as health_check_main
from beagle.infrastructure.hotswap_ingest import stage_ingest
from beagle.validation.runner import ToolResult, ValidationResult


# (a) B-8: health monitor startup must be awaited and running
@pytest.mark.asyncio
async def test_b8_health_monitor_awaited():
    orch = DAGOrchestrator(workflow_id="test_wf_b8")
    orch.state.query = "test query"

    mock_monitor = MagicMock()
    mock_monitor.start = AsyncMock()

    with (
        patch("beagle.health.get_health_monitor", return_value=mock_monitor),
        patch(
            "beagle.lifecycle.restore.restore_from_checkpoint",
            new_callable=AsyncMock,
            return_value=False,
        ),
    ):
        await orch._run_startup()
        mock_monitor.start.assert_awaited_once()


# (b) B-9: post-workflow validation findings reach state.errors
@pytest.mark.asyncio
async def test_b9_post_workflow_validation_reaches_state_errors():
    orch = DAGOrchestrator(workflow_id="test_wf_b9")
    orch.state.query = "test query"

    mock_tool_res = ToolResult(
        tool="pytest",
        exit_code=1,
        passed=False,
        stdout="",
        stderr="1 test failed",
        duration_seconds=0.1,
        summary="1 failed",
        findings_count=1,
    )
    stub_val_result = ValidationResult(
        timestamp=1000.0,
        workflow_id="test_wf_b9",
        tool_results=(mock_tool_res,),
        total_findings=1,
        all_passed=False,
        duration_seconds=0.1,
        files_checked=("test_file.py",),
    )

    with (
        patch("beagle.config.config.get_config") as mock_cfg,
        patch(
            "beagle.validation.feedback.run_validation",
            new_callable=AsyncMock,
            return_value=stub_val_result,
        ),
    ):
        mock_val_cfg = MagicMock()
        mock_val_cfg.run_after_workflow = True
        mock_cfg.return_value.validation = mock_val_cfg

        await orch.post_workflow_cleanup()

        assert any("pytest: 1 failed" in err for err in orch.state.errors)


# (c) B-9: ValidationBlock.execute regression coverage was retired together
# with the dead beagle.blocks package on 2026-07-29 (see
# audits/blocks_engine_upgrade_2026-07-29.md). The live B-9 path — findings
# reaching state.errors — remains covered by test (b) above.


# (d) B-10: `beagle run --dry-run` prints Estimated cost line and exits 0
def test_b10_cli_dry_run_prints_estimated_cost():
    runner = CliRunner()
    res = runner.invoke(app, ["run", "default", "build something", "--dry-run"])

    assert res.exit_code == 0
    assert "Estimated cost:" in res.stdout
    assert "Estimated tokens:" in res.stdout


# (e) B-11: health_check main() with one failing check returns 1 without raising TypeError
def test_b11_health_check_failing_check_returns_1():
    with (
        patch(
            "beagle.infrastructure.health_check.argparse.ArgumentParser.parse_args"
        ) as mock_parse,
        patch(
            "beagle.infrastructure.health_check.check_goose_binary",
            return_value=(False, "Goose binary missing"),
        ),
    ):
        mock_args = MagicMock()
        mock_args.agent = "test_agent"
        mock_args.verbose = False
        mock_parse.return_value = mock_args

        # Must exit 1 and NOT raise TypeError: warning() got an unexpected keyword argument 'file'
        exit_code = health_check_main()
        assert exit_code == 1


# (f) B-7: stage_ingest with one unreadable file returns status ok plus a warning
def test_b7_stage_ingest_unreadable_file_returns_ok_with_warning(tmp_path: Path, monkeypatch):
    # Isolate the delta-engine state so a successful stage_ingest does not
    # write the repo's real ~/.beagle/rag_state.json (which would wipe the
    # incremental cache and force a full re-index on the next real trigger).
    # delta_engine honours $BEAGLE_DATA_ROOT (via get_data_root), so point it
    # at tmp_path; reload the module so the module-level _STATE_DIR follows.
    import importlib

    import beagle.infrastructure.delta_engine as de

    monkeypatch.setenv("BEAGLE_DATA_ROOT", str(tmp_path))
    importlib.reload(de)
    # The rest of this test imports de._STATE_FILE via the reloaded module.

    codebase = tmp_path / "codebase"
    codebase.mkdir()
    readable_file = codebase / "good.py"
    readable_file.write_text("def ok(): pass\n")

    unreadable_file = codebase / "bad.py"
    unreadable_file.write_text("def bad(): pass\n")

    orig_read_text = Path.read_text

    def _mock_read_text(self, *args, **kwargs):
        if str(self).endswith("bad.py"):
            raise OSError("Permission denied")
        return orig_read_text(self, *args, **kwargs)

    staging_dir_path = tmp_path / "staging"

    mock_embedder = MagicMock()
    mock_embedder.encode.return_value = [[0.1, 0.2, 0.3, 0.4]]

    with (
        patch.object(Path, "read_text", _mock_read_text),
        patch(
            "beagle.infrastructure.cast_ingestion._resolve_embedder",
            return_value=mock_embedder,
        ),
    ):
        res = stage_ingest(str(codebase), staging_dir=str(staging_dir_path))

        assert res["status"] == "ok"
        assert len(res["warnings"]) >= 1
        assert any("bad.py" in w for w in res["warnings"])
