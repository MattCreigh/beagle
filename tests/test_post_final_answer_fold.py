"""Regression tests for v13.21.13 post-final-answer fold enforcement.

These tests pin down the root-cause fix for the bug observed in
Projects/Skylon_Ecosystem/skylon (2026-06-12): the deepseek session recorded
<Post-Final-Answer Fold> as its next step in progress.xml, then never
executed it. The conversation ended with the model narrating the fold
("compaction just happened (check_and_fold_context returned compact_now),
so the fold is essentially done. Let me just emit the final_answer.")
rather than actually invoking the tool.

The fix is ``enforce_post_final_answer_fold()`` in
``context/post_compaction_rehydration.py`` and the corresponding MCP tool
``enforce_post_final_answer_fold()`` in
``infrastructure/tools/_impl.py``. The function:

1. ALWAYS writes the rehydration sidecar (bypasses the 0.58 threshold)
2. ALWAYS returns action="compact_now" so the caller can confirm
3. NEVER depends on a model making the tool call
4. NEVER raises — failures are captured into the result dict and logged

These tests prove the sidecar is written without a model call, the
threshold is bypassed, and the MCP tool wrapper exposes the same path.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from beagle.context.post_compaction_rehydration import (
    BEAGLE_SYSTEM_IDENTITY,
    enforce_post_final_answer_fold,
)
from beagle.infrastructure.tools._impl import (
    enforce_post_final_answer_fold as mcp_enforce_post_final_answer_fold,
)

# ── Core function: enforce_post_final_answer_fold ────────────────────────────


class TestEnforcePostFinalAnswerFoldCore:
    """The runtime-side hook must fire unconditionally."""

    def test_writes_sidecar_above_pre_compact(self, tmp_path: Path) -> None:
        """Above the 0.58 threshold: sidecar must be written."""
        with patch(
            "beagle.context.post_compaction_rehydration.Path.home",
            return_value=tmp_path,
        ):
            result = enforce_post_final_answer_fold(
                workflow_id="test_wf_above",
                query="demo query above threshold",
                completed_nodes=["node_a", "node_b"],
                percentage=0.72,
            )
        assert result["action"] == "compact_now"
        assert result["sidecar_chars"] > 0
        sidecar = tmp_path / ".beagle" / "post_compaction_rehydration.txt"
        assert sidecar.exists()
        # Compare char counts (not bytes — em-dashes etc. are multi-byte
        # in UTF-8, so len(text) and stat().st_size differ for non-ASCII
        # content like the rehydration prompt's '—' and '→' glyphs).
        body = sidecar.read_text()
        assert len(body) == result["sidecar_chars"]
        assert BEAGLE_SYSTEM_IDENTITY.strip() in body
        assert "demo query above threshold" in body

    def test_writes_sidecar_below_pre_compact(self, tmp_path: Path) -> None:
        """The fix: even at 30% context the sidecar MUST be written.

        This is the exact scenario that broke in the Skylon session —
        the conversation ended below the 0.58 threshold so
        check_and_fold_context would have returned action=continue, but
        the doctrine says post_final_answer_fold is required regardless.
        """
        with patch(
            "beagle.context.post_compaction_rehydration.Path.home",
            return_value=tmp_path,
        ):
            result = enforce_post_final_answer_fold(
                workflow_id="test_wf_below",
                query="demo query below threshold",
                completed_nodes=["node_a"],
                percentage=0.30,
            )
        assert result["action"] == "compact_now", (
            "enforce_post_final_answer_fold must return compact_now "
            "regardless of percentage — the threshold is exactly what "
            "broke the Skylon session"
        )
        sidecar = tmp_path / ".beagle" / "post_compaction_rehydration.txt"
        assert sidecar.exists(), (
            "Sidecar must be written even at 30% context — this is the "
            "core regression from the Skylon bug"
        )
        assert result["sidecar_chars"] > 0

    def test_writes_sidecar_at_zero_percent(self, tmp_path: Path) -> None:
        """Zero reported usage — still must write sidecar."""
        with patch(
            "beagle.context.post_compaction_rehydration.Path.home",
            return_value=tmp_path,
        ):
            result = enforce_post_final_answer_fold(
                workflow_id="test_wf_zero",
                query="zero context",
                completed_nodes=[],
                percentage=0.0,
            )
        assert result["action"] == "compact_now"
        assert (tmp_path / ".beagle" / "post_compaction_rehydration.txt").exists()

    def test_sidecar_contains_resume_directive(self, tmp_path: Path) -> None:
        """The sidecar must contain the resume directive for the next session."""
        with patch(
            "beagle.context.post_compaction_rehydration.Path.home",
            return_value=tmp_path,
        ):
            enforce_post_final_answer_fold(
                workflow_id="test_wf_resume",
                query="important task",
                completed_nodes=["step1", "step2"],
            )
        body = (tmp_path / ".beagle" / "post_compaction_rehydration.txt").read_text()
        assert "<resume_directive>" in body
        assert "NEVER halt and ask for instructions" in body
        assert "test_wf_resume" in body

    def test_no_completed_nodes_still_works(self, tmp_path: Path) -> None:
        """completed_nodes=None and [] must both be accepted."""
        with patch(
            "beagle.context.post_compaction_rehydration.Path.home",
            return_value=tmp_path,
        ):
            r1 = enforce_post_final_answer_fold(
                workflow_id="t1",
                query="q",
                completed_nodes=None,
            )
            r2 = enforce_post_final_answer_fold(
                workflow_id="t2",
                query="q",
                completed_nodes=[],
            )
        assert r1["action"] == "compact_now"
        assert r2["action"] == "compact_now"

    def test_returns_workflow_id_and_metadata(self, tmp_path: Path) -> None:
        """The result dict must include workflow_id and trigger metadata."""
        with patch(
            "beagle.context.post_compaction_rehydration.Path.home",
            return_value=tmp_path,
        ):
            result = enforce_post_final_answer_fold(
                workflow_id="metadata_test",
                query="q",
                completed_nodes=["a"],
                percentage=0.55,
            )
        assert result["workflow_id"] == "metadata_test"
        assert result["trigger"] == "post_final_answer_fold"
        assert result["percentage_reported"] == 0.55
        assert result["sidecar_path"].endswith("post_compaction_rehydration.txt")

    def test_does_not_raise_on_disk_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A sidecar-write failure must NOT raise — it must be captured.

        The doctrine says: "fold is best-effort but sidecar is durable; the
        result dict must report failure cleanly so finalize() can log it."
        """
        # Force a real disk failure: place a regular file where the sidecar
        # directory should be. mkdir(parents=True, exist_ok=True) will
        # succeed, but write_text() will fail with IsADirectoryError or
        # FileExistsError because the target IS a directory we cannot
        # write to.
        broken = tmp_path / "beagle_blocked"
        broken.mkdir()
        # Create a file with the same name as the sidecar — this will
        # make write_text fail because the parent dir's permissions
        # block it, OR (cleaner) make the HOME a path whose parent
        # contains a regular file with the sidecar name.
        blocker = tmp_path / "beagle_dir_blocker"
        blocker.write_text("blocking")  # regular file
        # Point home to a path UNDER this regular file
        # Simpler: use a path that is read-only at the FS level
        # Real portable approach: patch the sidecar_path constant directly
        # to a path inside /proc/cmdline (Linux, not writable) — but that's
        # too environment-specific. Use a file-as-directory pattern: a
        # path where a non-directory exists in the middle of the tree.

        # Create a regular file where mkdir would need to go
        conflict_dir = tmp_path / "conflict_root"
        conflict_dir.mkdir()
        file_in_way = conflict_dir / ".beagle"
        file_in_way.write_text("I am a file, not a directory")
        # Now if home is conflict_dir/something, mkdir ~/.beagle will fail
        # because ~/.beagle would need to be created under conflict_dir,
        # but conflict_dir/.beagle already exists as a file.
        # Actually we need: home() returns a path where a non-leaf
        # component is a regular file. Simpler: set home to conflict_dir/.beagle
        # so that home() is a file, and sidecar_path = home/.beagle/... would
        # require mkdir under a file, which fails.

        def _home_is_file() -> Path:
            return file_in_way

        with patch(
            "beagle.context.post_compaction_rehydration.Path.home",
            _home_is_file,
        ):
            result = enforce_post_final_answer_fold(
                workflow_id="t_disk_fail",
                query="q",
                completed_nodes=[],
            )
        # If we get here without an exception, the contract holds. The
        # status may be 'ok' (mkdir raised but we caught it before
        # status assignment) or 'sidecar_write_failed'. The contract is
        # that we never raise and the dict is always populated.
        assert "action" in result
        assert "status" in result
        assert result["action"] == "compact_now"
        # The fold_id may be empty (fold best-effort) or set
        assert "fold_id" in result


# ── MCP tool wrapper ─────────────────────────────────────────────────────────


class TestEnforcePostFinalAnswerFoldMCPTool:
    """The MCP tool surface must expose the same path for the runtime."""

    @pytest.mark.asyncio
    async def test_mcp_tool_returns_json_with_compact_now(self, tmp_path: Path) -> None:
        with patch(
            "beagle.context.post_compaction_rehydration.Path.home",
            return_value=tmp_path,
        ):
            payload = await mcp_enforce_post_final_answer_fold(
                workflow_id="mcp_test",
                query="test from mcp wrapper",
                completed_nodes=["n1", "n2"],
                percentage=0.40,
            )
        parsed = json.loads(payload)
        assert parsed["action"] == "compact_now"
        assert parsed["workflow_id"] == "mcp_test"
        assert parsed["trigger"] == "post_final_answer_fold"
        assert parsed["sidecar_chars"] > 0
        assert (tmp_path / ".beagle" / "post_compaction_rehydration.txt").exists()

    @pytest.mark.asyncio
    async def test_mcp_tool_uses_default_workflow_id(self, tmp_path: Path) -> None:
        """The default workflow_id is 'cli_session' — matches the bootstrap
        resume_point convention used in the Skylon session."""
        with patch(
            "beagle.context.post_compaction_rehydration.Path.home",
            return_value=tmp_path,
        ):
            payload = await mcp_enforce_post_final_answer_fold()
        parsed = json.loads(payload)
        assert parsed["workflow_id"] == "cli_session"
        assert parsed["action"] == "compact_now"

    @pytest.mark.asyncio
    async def test_mcp_tool_does_not_propagate_disk_error(self, tmp_path: Path) -> None:
        """A disk error inside the MCP wrapper must not raise — the JSON
        envelope is the contract, and downstream parsers expect a valid
        JSON response, never an exception."""
        # Use a file as the home directory — mkdir(parents=True) will fail.
        file_in_way = tmp_path / "beagle_blocker"
        file_in_way.write_text("blocking")

        with patch(
            "beagle.context.post_compaction_rehydration.Path.home",
            return_value=file_in_way,
        ):
            # Should not raise
            payload = await mcp_enforce_post_final_answer_fold(
                workflow_id="disk_fail",
                query="q",
            )
        parsed = json.loads(payload)
        # The contract: never raise, return a valid JSON with the
        # standard envelope. Status may be 'ok' (mkdir raised but the
        # OSError handler caught it gracefully inside the function) or
        # 'sidecar_write_failed' (caught at the write_text step). Either
        # is acceptable as long as the envelope is valid.
        assert "status" in parsed
        assert "action" in parsed
        assert parsed["action"] == "compact_now"
        assert "fold_id" in parsed
        # We may or may not have sidecar_chars — both are valid outcomes
        # depending on where in the pipeline the failure occurred.


# ── Threshold independence ────────────────────────────────────────────────────


class TestThresholdIndependence:
    """The whole point: the 0.58 threshold must NOT gate this path.

    ``check_and_fold_context`` returns action=continue below 0.58. That is
    correct for the per-turn pattern (don't fold if you have headroom). The
    post-final-answer fold is different — it is the session-end SIDE-EFFECT
    that must always run so the next session can rehydrate.
    """

    @pytest.mark.parametrize("pct", [0.0, 0.10, 0.30, 0.55, 0.58, 0.70, 0.85, 1.0])
    def test_every_percentage_writes_sidecar(self, tmp_path: Path, pct: float) -> None:
        with patch(
            "beagle.context.post_compaction_rehydration.Path.home",
            return_value=tmp_path,
        ):
            result = enforce_post_final_answer_fold(
                workflow_id=f"pct_{pct}",
                query="q",
                completed_nodes=["x"],
                percentage=pct,
            )
        assert result["action"] == "compact_now", (
            f"action must be compact_now at every percentage, got "
            f"pct={pct} action={result['action']}"
        )
        assert (tmp_path / ".beagle" / "post_compaction_rehydration.txt").exists()


# ── Integration: sidecar is read by beagle_session_bootstrap ───────────────────


class TestSidecarIntegrationWithBootstrap:
    """The sidecar that ``enforce_post_final_answer_fold`` writes must be
    the same one that ``beagle_session_bootstrap`` reads."""

    def test_written_sidecar_is_readable_by_bootstrap(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Make Path.home() point to tmp_path for both writers and readers
        monkeypatch.setattr(
            "beagle.context.post_compaction_rehydration.Path.home",
            lambda: tmp_path,
        )

        enforce_post_final_answer_fold(
            workflow_id="bootstrap_integration",
            query="rehydration test",
            completed_nodes=["n1"],
        )

        # Now read it back the same way bootstrap does
        from beagle.infrastructure.tools._impl import (
            post_compaction_rehydrate,
        )

        payload = asyncio.run(post_compaction_rehydrate())
        parsed = json.loads(payload)
        assert parsed["found"] is True
        assert parsed["rehydration_prompt"]
        assert "bootstrap_integration" in parsed["rehydration_prompt"]
        assert "rehydration test" in parsed["rehydration_prompt"]
        assert "<resume_directive>" in parsed["rehydration_prompt"]


# ── Regression pin: the Skylon bug ───────────────────────────────────────────


class TestSkylonRegression:
    """Pin the exact failure mode observed in the Skylon deepseek session:

    1. progress.xml said <next_step>Post-final-answer fold</next_step>
    2. Model narrated a fold that never happened
    3. Session ended without writing a sidecar
    4. Next session had nothing to rehydrate from

    These tests assert the runtime is no longer model-cooperative — the
    fold fires even when no model call has been made."""

    def test_fold_fires_without_model_tool_call(self, tmp_path: Path) -> None:
        """Calling enforce_post_final_answer_fold directly is enough.
        No model tool call, no LLM round-trip, no narration — just the
        function. The sidecar is written and the action is compact_now.
        """
        with patch(
            "beagle.context.post_compaction_rehydration.Path.home",
            return_value=tmp_path,
        ):
            result = enforce_post_final_answer_fold(
                workflow_id="skylon_repro",
                query="S2 IPC fault-injection tests (Skylon session 2026-06-12)",
                completed_nodes=[
                    "S1 verify",
                    "S2 design",
                    "S2 implement test_ipc_fault_injection.py",
                    "S2 run pytest",
                    "S2 ruff",
                    "S2 build wheel",
                    "S2 commit adc5085",
                    "S2 push to main",
                ],
                percentage=0.85,  # 109k/128k from the Skylon info-msg
            )
        # All Skylon-session progress must be in the sidecar
        sidecar = (tmp_path / ".beagle" / "post_compaction_rehydration.txt").read_text()
        assert "S2 IPC fault-injection tests" in sidecar
        assert "adc5085" in sidecar
        assert result["action"] == "compact_now"
        assert result["status"] == "ok"

    def test_finalize_phase_uses_same_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The autonomous_orchestrator._run_finalize hook must call
        enforce_post_final_answer_fold (not on_post_compaction or
        check_and_fold_context). This pins the API the orchestrator uses
        so a future refactor cannot silently break the runtime contract.
        """
        # Check that the finalize function calls enforce_post_final_answer_fold
        import inspect

        from beagle.core import autonomous_orchestrator as ao

        source = inspect.getsource(ao.DAGOrchestrator._run_finalize)
        assert "enforce_post_final_answer_fold" in source, (
            "_run_finalize must call enforce_post_final_answer_fold for "
            "runtime-side fold enforcement"
        )
        assert "Post-Final-Answer Fold" in source or "post_final_answer_fold" in source
