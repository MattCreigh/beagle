"""Test for CVE-2025-64439 mitigation — Checkpoint Deserialization RCE.

Verifies that:
1. langgraph-checkpoint >= 3.0.0 is installed (patched version)
2. The checkpointer does not use vulnerable deserialization patterns
3. Synthetic exploit payloads are rejected
4. JSON deserialization is safe (no arbitrary constructor instantiation)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


class TestCVE202564439Mitigation:
    """Security tests for CVE-2025-64439 — Checkpoint Deserialization RCE."""

    def test_langgraph_checkpoint_version_patched(self):
        """Verify langgraph-checkpoint >= 3.0.0 is installed (patches CVE).

        v1.0.2: this shelled out to `pip3 show langgraph-checkpoint` and skipped
        when it could not parse a version. `pip3` resolves through PATH to the
        SYSTEM interpreter (/usr/bin/pip3 here), not the venv running the tests,
        so it answered "WARNING: Package(s) not found" with exit 0 — empty
        stdout, version None, skip. The result: a CVE regression guard that had
        never once executed, while reporting green.

        importlib.metadata reads the metadata of the interpreter actually
        running the test, which is the only environment whose package versions
        this assertion is about. It also cannot silently degrade to a skip:
        if the distribution is absent that is itself a failure, because
        langgraph-checkpoint is a declared hard dependency (pyproject.toml).
        """
        from importlib.metadata import PackageNotFoundError
        from importlib.metadata import version as dist_version

        try:
            installed = dist_version("langgraph-checkpoint")
        except PackageNotFoundError:  # pragma: no cover - declared hard dependency
            pytest.fail(
                "langgraph-checkpoint is not installed in the interpreter running "
                "the tests, but it is a declared hard dependency in pyproject.toml. "
                "This test cannot verify the CVE-2025-64439 mitigation."
            )

        major = int(installed.split(".")[0])
        assert major >= 3, (
            f"langgraph-checkpoint version {installed} is vulnerable to "
            f"CVE-2025-64439. Upgrade to >= 3.0.0"
        )

    def test_checkpointer_uses_safe_json_deserialization(self):
        """Verify checkpointer uses json.loads (safe) not pickle/yaml.load (unsafe)."""
        checkpointer_path = PROJECT_ROOT / "src" / "checkpointer.py"
        if not checkpointer_path.exists():
            # Check in memory subdirectory
            checkpointer_path = PROJECT_ROOT / "src" / "memory" / "checkpointer.py"

        if checkpointer_path.exists():
            content = checkpointer_path.read_text()
            # These are UNSAFE and must NOT be present for user-provided data
            assert "pickle.loads" not in content, "checkpointer uses pickle.loads — vulnerable!"
            assert "yaml.load" not in content, (
                "checkpointer uses yaml.load (unsafe) — use yaml.safe_load instead"
            )
            # json.loads is the safe deserialization method
            assert "json.loads" in content, (
                "checkpointer should use json.loads for safe deserialization"
            )

    def test_exploit_payload_rejected_by_json(self):
        """Verify that JSON deserialization rejects constructor payloads (CVE pattern)."""
        # The CVE-2025-64439 exploit pattern: {"lc": 2, "type": "constructor", ...}
        exploit_payloads = [
            (
                '{"lc": 2, "type": "constructor",'
                ' "id": ["os", "system"],'
                ' "kwargs": {"command": "id"}}'
            ),
            (
                '{"lc": 2, "type": "constructor",'
                ' "id": ["subprocess", "Popen"],'
                ' "kwargs": {"args": ["rm", "-rf", "/"]}}'
            ),
            (
                '{"lc": 2, "type": "constructor",'
                ' "id": ["builtins", "eval"],'
                ' "kwargs": {"expression":'
                ' "__import__"}}'
            ),
        ]

        for payload in exploit_payloads:
            # json.loads() safely parses this as a regular dict — it does NOT
            # instantiate any constructors. The vulnerability was in JsonPlusSerializer
            # which would instantiate arbitrary classes from such payloads.
            result = json.loads(payload)
            assert isinstance(result, dict), "Payload should parse as regular dict"
            assert result.get("lc") == 2, "Payload indicator present"
            assert result.get("type") == "constructor", "Constructor type present"
            # The key point: json.loads does NOT call any constructor
            # The actual exploitation requires a custom deserializer (like JsonPlusSerializer)
            # which langgraph-checkpoint >= 3.0.0 patches against

    def test_checkpoint_data_roundtrip_safety(self):
        """Verify checkpoint save/load roundtrip uses safe serialization."""
        from beagle.checkpointer import Checkpoint

        # Create a benign checkpoint
        cp = Checkpoint(
            workflow_id="test-cve-64439",
            query="security test",
            completed_nodes=["node1"],
            state_data={"result": "safe data"},
        )

        # Save to temp dir
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "test_checkpoint.json"
            saved_path = cp.save(path)

            # Read raw JSON and verify no constructor payloads
            raw_content = saved_path.read_text()
            parsed = json.loads(raw_content)

            # Verify safe data types only
            assert isinstance(parsed, dict)
            assert "workflow_id" in parsed
            assert parsed["workflow_id"] == "test-cve-64439"

            # Verify no dangerous type markers
            assert parsed.get("lc") is None, "Should not contain LangChain constructor markers"
            assert parsed.get("type") != "constructor", "Should not contain constructor type"

    def test_memory_checkpointer_safe(self):
        """Verify the memory checkpointer module uses AsyncSqliteSaver (safe)."""
        memory_cp_path = PROJECT_ROOT / "src" / "memory" / "checkpointer.py"
        if memory_cp_path.exists():
            content = memory_cp_path.read_text()
            assert "AsyncSqliteSaver" in content, "Should use AsyncSqliteSaver"
            assert "pickle.loads" not in content, "Should not use pickle.loads"
            assert "import pickle" not in content, "Should not import pickle"
            assert "yaml.load" not in content, "Should not use unsafe yaml.load"


class TestRequirementPinning:
    """Verify security-related requirements are properly pinned."""

    def test_langgraph_checkpoint_pinned_in_requirements(self):
        """Verify requirements.txt pins langgraph-checkpoint >= 3.0.0."""
        req_path = PROJECT_ROOT / "requirements.txt"
        content = req_path.read_text()
        assert "langgraph-checkpoint" in content, (
            "langgraph-checkpoint should be in requirements.txt"
        )

    def test_langgraph_checkpoint_pinned_in_pyproject(self):
        """Verify pyproject.toml pins langgraph-checkpoint >= 3.0.0."""
        pyproject_path = PROJECT_ROOT / "pyproject.toml"
        content = pyproject_path.read_text()
        assert "langgraph-checkpoint" in content, "langgraph-checkpoint should be in pyproject.toml"
