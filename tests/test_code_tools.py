"""Tests for Beagle Structured Code Tools (Phase 8.3)."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from beagle.infrastructure.mcp_utility_server import (
    code_context,
    code_search,
    file_discovery,
)


@pytest.mark.asyncio
async def test_code_search_basic():
    """Test code_search returns structured results."""
    # We'll mock subprocess.run to avoid dependency on actual rg installed in environment
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = json.dumps(
            {
                "type": "match",
                "data": {
                    "path": {"text": "test.py"},
                    "line_number": 10,
                    "lines": {"text": "def test_func():\n"},
                    "context_before": [{"text": "# before"}],
                    "context_after": [{"text": "# after"}],
                },
            }
        )

        result_json = await code_search("test_func", path=".")
        result = json.loads(result_json)

        assert result["status"] == "ok"
        assert len(result["matches"]) == 1
        assert result["matches"][0]["file"] == "test.py"
        assert result["matches"][0]["line"] == 10


@pytest.mark.asyncio
async def test_file_discovery_basic():
    """Test file_discovery returns list of files."""
    # v1.0.2: `fd` being absent is signalled by shutil.which() returning None,
    # not by subprocess.run raising FileNotFoundError — file_discovery resolves
    # the binary to a full path before it ever spawns one (S607). The old mock
    # fed a FileNotFoundError to subprocess.run, which the probe no longer
    # reaches, so the error escaped into the search call and the tool returned
    # status="error". Patch the probe cache too, so the branch under test is
    # selected deterministically regardless of test order.
    with (
        patch("beagle.infrastructure.mcp_utility_server._HAS_FD", None),
        patch("shutil.which", return_value=None),
        patch("subprocess.run") as mock_run,
    ):
        mock_run.return_value = MagicMock(stdout="file1.py\nfile2.py\n")

        result_json = await file_discovery(path=".")
        result = json.loads(result_json)

        assert result["status"] == "ok"
        assert result["total_found"] == 2


@pytest.mark.asyncio
async def test_code_context_overview():
    """Test code_context overview parsing."""
    # Create a real temp file for AST parsing
    content = """
class MyClass:
    def method(self):
        pass

def top_func():
    pass
"""
    with (
        patch(
            "beagle.infrastructure.mcp_utility_server._PROJECT_ROOT",
            Path("/tmp"),
        ),
        patch("pathlib.Path.exists", return_value=True),
        patch("pathlib.Path.is_file", return_value=True),
        patch("pathlib.Path.read_text", return_value=content),
    ):
        result_json = await code_context("dummy.py", query_type="overview")
        result = json.loads(result_json)

        assert result["status"] == "ok"
        defs = result["definitions"]
        assert any(d["name"] == "MyClass" and d["type"] == "class" for d in defs)
        assert any(d["name"] == "top_func" and d["type"] == "function" for d in defs)


@pytest.mark.asyncio
async def test_path_validation():
    """Test that path traversal is blocked."""
    result_json = await code_search("pattern", path="../../../etc/passwd")
    result = json.loads(result_json)
    assert result["status"] == "error"
    assert "Path traversal" in result["message"]


# ── RG-5 (BGL-005, BGL-006): search failure visibility ──────────────────────


def _mock_rg(returncode: int, stdout: str = "", stderr: str = "") -> MagicMock:
    """Build a subprocess.run mock with the given return code and streams."""
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


@pytest.mark.asyncio
async def test_code_search_invalid_pattern_returns_error():
    """An invalid pattern (ripgrep exit 2) must return status='error' with stderr."""
    with patch("subprocess.run", return_value=_mock_rg(2, stderr="regex parse error")):
        result_json = await code_search("(", path=".")
    result = json.loads(result_json)
    assert result["status"] == "error"
    assert "regex parse error" in result["message"]


@pytest.mark.asyncio
async def test_code_search_lookbehind_returns_error():
    """A Python-valid but ripgrep-invalid pattern must return status='error'."""
    with patch("subprocess.run", return_value=_mock_rg(2, stderr="look-behind not supported")):
        result_json = await code_search("(?<=def )beagle", path=".")
    result = json.loads(result_json)
    assert result["status"] == "error"
    assert "look-behind not supported" in result["message"]


@pytest.mark.asyncio
async def test_code_search_no_match_is_success():
    """Exit code 1 (no match) must stay a success with 0 matches."""
    with patch("subprocess.run", return_value=_mock_rg(1)):
        result_json = await code_search("zzz_no_match_zzz", path=".")
    result = json.loads(result_json)
    assert result["status"] == "ok"
    assert result["total_matches"] == 0
    assert result["matches"] == []
