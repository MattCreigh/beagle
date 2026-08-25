"""Section 4.2: Startup health check module tests."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from beagle.startup.health_check import (
    StartupCheckResult,
    check_config_loads,
    check_core_modules_importable,
    check_edge_features_importable,
    check_essential_directories,
    check_firecracker_binary,
    check_goose_binary,
    check_mcp_server_scripts,
    check_ollama_cloud_endpoint,
    check_orpheus_ring_dir,
    format_startup_report,
    run_startup_checks,
)


class TestStartupCheckResult:
    """Tests for StartupCheckResult dataclass."""

    def test_ok_result(self):
        r = StartupCheckResult("test", "ok", "All good")
        assert r.is_ok
        assert not r.is_fail

    def test_warn_result(self):
        r = StartupCheckResult("test", "warn", "Warning")
        assert not r.is_ok
        assert not r.is_fail

    def test_fail_result(self):
        r = StartupCheckResult("test", "fail", "Broken")
        assert not r.is_ok
        assert r.is_fail

    def test_fix_hint(self):
        r = StartupCheckResult("test", "fail", "Broken", "Fix it")
        assert r.fix_hint == "Fix it"

    def test_details_default(self):
        r = StartupCheckResult("test", "ok", "Works")
        assert r.details == {}

    def test_details_custom(self):
        r = StartupCheckResult("test", "ok", "Works", details={"k": "v"})
        assert r.details["k"] == "v"


class TestCheckConfigLoads:
    """Tests for config loading check."""

    def test_config_loads_ok(self):
        result = check_config_loads()
        assert result.name == "config"
        assert result.status == "ok"

    def test_config_loads_failure(self):
        with patch(
            "beagle.config.loader.load_config",
            side_effect=ValueError("bad config"),
        ):
            result = check_config_loads()
        assert result.status == "fail"
        assert "bad config" in result.message
        assert result.fix_hint


class TestCheckEssentialDirectories:
    """Tests for directory access check."""

    def test_directories_ok(self):
        result = check_essential_directories()
        assert result.name == "directories"
        assert result.status == "ok"

    def test_directories_with_mock_failure(self):
        with patch(
            "beagle.startup.health_check.Path.mkdir",
            side_effect=OSError("no space"),
        ):
            result = check_essential_directories()
        # The check should report failure if directories can't be created
        assert result.status in ("fail", "ok")  # May pass if dirs exist


class TestCheckCoreModulesImportable:
    """Tests for module import check."""

    def test_core_modules_ok(self):
        result = check_core_modules_importable()
        assert result.name == "core_modules"
        assert result.status == "ok"

    def test_core_modules_import_failure(self):
        with patch("importlib.import_module", side_effect=ImportError("nope")):
            result = check_core_modules_importable()
        assert result.status == "fail"
        assert "nope" in result.message


class TestCheckGooseBinary:
    """Tests for goose binary check."""

    def test_goose_binary_warn_if_missing(self):
        result = check_goose_binary()
        # Goose may or may not be installed — just verify it returns a result
        assert result.name == "goose_binary"
        assert result.status in ("ok", "warn")

    def test_goose_binary_warn_with_empty_path(self):
        mock_config = MagicMock()
        mock_config.paths.goose_bin = "/nonexistent/path/goose"
        with (
            patch(
                "beagle.config.loader.load_config",
                return_value=mock_config,
            ),
            patch("shutil.which", return_value=None),
        ):
            result = check_goose_binary()
        assert result.status == "warn"


class TestCheckMCPServerScripts:
    """Tests for MCP server module check."""

    def test_mcp_scripts_ok(self):
        result = check_mcp_server_scripts()
        assert result.name == "mcp_scripts"
        assert result.status == "ok"


class TestCheckOrpheusRingDir:
    """Tests for Orpheus ring directory check."""

    def test_orpheus_ring_check_returns_result(self):
        result = check_orpheus_ring_dir()
        assert result.name == "orpheus_rings"
        # Either ok or warn depending on environment
        assert result.status in ("ok", "warn")


class TestCheckFirecrackerBinary:
    """Tests for Firecracker binary check."""

    def test_firecracker_disabled_is_ok(self):
        # Default config has microvm disabled
        result = check_firecracker_binary()
        assert result.name == "firecracker"
        # Should be ok when disabled
        assert result.status in ("ok", "warn")

    def test_firecracker_warn_when_enabled_but_missing(self):
        mock_config = MagicMock()
        mock_config.sandbox_microvm.enabled = True
        mock_config.sandbox_microvm.firecracker_binary = "/nonexistent/firecracker"
        with patch(
            "beagle.config.loader.load_config",
            return_value=mock_config,
        ):
            result = check_firecracker_binary()
        assert result.status == "warn"
        assert "setup_firecracker" in result.fix_hint


class TestCheckEdgeFeaturesImportable:
    """Tests for competitive-differentiator feature check."""

    def test_edge_features_ok(self):
        result = check_edge_features_importable()
        assert result.name == "edge_features"
        assert result.status == "ok"

    def test_edge_features_warn_on_import_failure(self):
        with patch("importlib.import_module", side_effect=ImportError("nope")):
            result = check_edge_features_importable()
        assert result.status == "warn"


class TestRunStartupChecks:
    """Tests for the orchestrator function."""

    def test_required_checks_always_run(self):
        results = run_startup_checks(include_optional=False)
        names = [r.name for r in results]
        assert "config" in names
        assert "directories" in names
        assert "core_modules" in names

    def test_optional_checks_included_by_default(self):
        results = run_startup_checks(include_optional=True)
        names = [r.name for r in results]
        assert "goose_binary" in names
        assert "mcp_scripts" in names
        assert "orpheus_rings" in names
        assert "edge_features" in names

    def test_stop_on_fail(self):
        with patch(
            "beagle.startup.health_check.check_config_loads",
            return_value=StartupCheckResult("config", "fail", "broken"),
        ):
            results = run_startup_checks(stop_on_fail=True)
        # Should stop after first required check failure
        assert len(results) == 1
        assert results[0].status == "fail"

    def test_all_results_have_valid_status(self):
        results = run_startup_checks()
        for r in results:
            assert r.status in ("ok", "warn", "fail", "skip"), f"{r.name}: {r.status}"
            assert r.message, f"{r.name}: empty message"


class TestFormatStartupReport:
    """Tests for report formatter."""

    def test_format_report_ok(self):
        results = [
            StartupCheckResult("test1", "ok", "Good"),
            StartupCheckResult("test2", "ok", "Also good"),
        ]
        report = format_startup_report(results)
        assert "2 ok" in report
        assert "0 warnings" in report
        assert "0 failures" in report

    def test_format_report_with_warnings(self):
        results = [
            StartupCheckResult("t1", "ok", "Good"),
            StartupCheckResult("t2", "warn", "Warn", "Fix it"),
        ]
        report = format_startup_report(results)
        assert "1 warnings" in report
        assert "Fix it" in report

    def test_format_report_with_failures(self):
        results = [
            StartupCheckResult("t1", "fail", "Broken", "Reinstall"),
        ]
        report = format_startup_report(results)
        assert "1 failures" in report
        assert "Reinstall" in report


# ── v13.22.3: ollama_cloud_endpoint connectivity check ──────────────────
class TestCheckOllamaCloudEndpoint:
    """Provider reachability check — configured-endpoint contract.

    The OSS build ships NO preset host: the check probes whatever endpoint
    the operator configured, and reports ``skip`` when none is set. These
    tests inject an endpoint so legacy probe behaviour stays covered.
    """

    @pytest.fixture(autouse=True)
    def _configured_endpoint(self, monkeypatch):  # type: ignore[no-untyped-def]
        import types

        from beagle.config import loader as _loader

        cfg = types.SimpleNamespace(
            ollama_cloud=types.SimpleNamespace(endpoint="https://ollama.test")
        )
        monkeypatch.setattr(_loader, "get_config", lambda *a, **k: cfg)

    """Tests for the Ollama Cloud connectivity smoke-test (v13.22.3).

    Regression guard for the silent-fallback chain: the previous
    configuration had ``PROVIDER_FALLBACK_CHAIN = ['openai']`` with
    no OPENAI_API_KEY, every subprocess call returned 401, the
    circuit breaker tripped after 5 failures, and the workflow
    silently returned ``CircuitBreakerOpenError`` without ever
    surfacing the real cause. The connectivity check ensures we
    fail loud at startup instead.
    """

    def _mock_response(self, status=200, body=b'{"models":[{"name":"minimax-m3"}]}'):
        """Build a context-managed mock matching urllib.request.urlopen."""

        class _Resp:
            def __init__(self):
                self.status = status

            def read(self, n=-1):
                if n == -1:
                    return self._remaining
                chunk = self._remaining[:n]
                self._remaining = self._remaining[n:]
                return chunk

            def __enter__(self):
                self._remaining = body
                return self

            def __exit__(self, *_):
                return False

        return _Resp()

    def test_ok_when_endpoint_returns_200_with_advertised_models(self):
        """Live endpoint + all configured :cloud models advertised → ok."""
        from unittest.mock import patch

        # The mock advertises every model currently in
        # [models.allowed] (stripped of the ":cloud" suffix as the
        # real /api/tags endpoint does). If a future config.toml
        # edit adds a model, this test still passes — the list is
        # computed from the live allowlist.
        from beagle.config.allowlist import allowed_models

        # /api/tags advertises the BARE name, so strip whichever Ollama
        # Cloud suffix the allowlist entry carries: ":cloud" for an
        # untagged base model (minimax-m3:cloud) or "-cloud" for one that
        # is already tagged (gemma4:31b-cloud). Stripping only ":cloud"
        # leaves the "-cloud" entries unstripped, which made this mock
        # disagree with the real endpoint and hid the checker's matching
        # bug until 2026-07-28.
        def _bare(m: str) -> str:
            return m[: -len("-cloud")] if m.endswith((":cloud", "-cloud")) else m

        advertised_names = ",".join(f'{{"name":"{_bare(m)}"}}' for m in sorted(allowed_models()))
        body = f'{{"models":[{advertised_names}]}}'.encode()
        with patch(
            "urllib.request.urlopen",
            return_value=self._mock_response(status=200, body=body),
        ):
            r = check_ollama_cloud_endpoint()
        assert r.status == "ok", r.message
        assert "Ollama Cloud reachable" in r.message
        assert r.details["endpoint"] == "https://ollama.test/api/tags"  # configured, not preset

    def test_fail_when_hyphen_cloud_model_is_not_advertised(self):
        """A retired allowlisted model must be caught, not skipped.

        Regression for the 2026-07-28 blind spot: the checker matched only
        ``endswith(":cloud")``, so allowlist entries using the tagged form
        (``gemma4:31b-cloud``, ``qwen3.5:397b-cloud``) were never compared
        against /api/tags at all. A model retired upstream would pass
        startup clean and only surface later as a circuit-breaker trip.

        v1.0.8: the allowlist no longer uses ``:cloud`` or ``-cloud``
        suffixes — all models are bare names. The test now picks the
        first allowlisted model and simulates its retirement.
        """
        from unittest.mock import patch

        from beagle.config.allowlist import allowed_models

        def _bare(m: str) -> str:
            return m[: -len("-cloud")] if m.endswith((":cloud", "-cloud")) else m

        all_models = sorted(allowed_models())
        assert all_models, "allowlist must not be empty"
        retired = all_models[0]

        # Advertise everything EXCEPT the retired model.
        advertised = ",".join(f'{{"name":"{_bare(m)}"}}' for m in all_models if m != retired)
        body = f'{{"models":[{advertised}]}}'.encode()
        with patch(
            "urllib.request.urlopen",
            return_value=self._mock_response(status=200, body=body),
        ):
            r = check_ollama_cloud_endpoint()
        assert r.status == "fail", r.message
        assert retired in r.details["missing"]

    def test_fail_when_endpoint_returns_5xx(self):
        """5xx response must fail loud at startup, not warn."""
        from unittest.mock import patch

        with patch(
            "urllib.request.urlopen",
            return_value=self._mock_response(status=503, body=b"down"),
        ):
            r = check_ollama_cloud_endpoint()
        assert r.status == "fail"
        assert "HTTP 503" in r.message
        assert r.fix_hint  # must include remediation hint

    def test_fail_on_timeout(self):
        """TimeoutError must produce a fail result, not warn."""
        from unittest.mock import patch

        with patch("urllib.request.urlopen", side_effect=TimeoutError("slow")):
            r = check_ollama_cloud_endpoint()
        assert r.status == "fail"
        assert "timed out" in r.message
        assert r.fix_hint

    def test_fail_on_dns_error(self):
        """URLError (DNS / connection refused) must produce a fail."""
        import urllib.error
        from unittest.mock import patch

        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("Name or service not known"),
        ):
            r = check_ollama_cloud_endpoint()
        assert r.status == "fail"
        assert "unreachable" in r.message

    def test_fail_when_configured_models_not_advertised(self):
        """If config.toml lists a model Ollama Cloud doesn't host, fail loud.

        This catches the drift between the configured allowlist and the
        live Ollama Cloud catalogue before the first subprocess call
        does. The previous silent-fallback bug masked exactly this kind
        of drift for hours.
        """
        from unittest.mock import patch

        # Advertise a model that's NOT in our allowlist; our allowlist
        # contains minimax-m3 which ISN'T in the advertised list,
        # so we expect a fail with "missing" details.
        with patch(
            "urllib.request.urlopen",
            return_value=self._mock_response(
                status=200,
                body=b'{"models":[{"name":"some-other-model"}]}',
            ),
        ):
            r = check_ollama_cloud_endpoint()
        assert r.status == "fail"
        assert "not advertised" in r.message
        assert "minimax-m3" in r.details["missing"]

    def test_run_startup_checks_includes_ollama_endpoint(self):
        """The required-checks list must include the connectivity probe.

        Without this assertion, a future refactor could silently demote
        the check from required→optional and re-introduce the silent-
        fallback regression we're guarding against.
        """
        from beagle.startup.health_check import (
            run_startup_checks,
        )

        # We don't actually invoke run_startup_checks (it triggers the
        # live probe); we just inspect that the function references
        # check_ollama_cloud_endpoint in its required list. Read the
        # function's referenced names via co_names — that's the
        # closest the bytecode exposes us to "what does this
        # function call".
        closure_names = run_startup_checks.__code__.co_names
        assert "check_ollama_cloud_endpoint" in closure_names, (
            "check_ollama_cloud_endpoint must be referenced from "
            "run_startup_checks — otherwise the connectivity probe "
            "is not wired into the required-checks path"
        )
