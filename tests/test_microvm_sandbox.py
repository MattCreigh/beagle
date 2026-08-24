"""Tests for MicroVM sandbox implementation.

Validates SandboxConfig, MicroVMConfig, MicroVMResult, and MicroVMSandbox
against the actual Beagle implementation, including config generation,
health check, graceful shutdown, and fallback behavior.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from beagle.core.sandbox import (
    MicroVMConfig,
    MicroVMResult,
    MicroVMSandbox,
    SandboxConfig,
)


class TestSandboxConfig:
    """Tests for SandboxConfig defaults."""

    def test_default_config(self):
        config = SandboxConfig()
        assert config.memory_limit == 512 * 1024 * 1024
        assert config.cpu_time_limit == 60
        assert config.max_files == 64
        assert config.max_processes == 8
        assert config.allow_network is False
        assert config.readonly_filesystem is False

    def test_custom_config(self):
        config = SandboxConfig(
            memory_limit=256 * 1024 * 1024,
            cpu_time_limit=30,
            allow_network=False,
            readonly_filesystem=True,
        )
        assert config.memory_limit == 256 * 1024 * 1024
        assert config.cpu_time_limit == 30


class TestMicroVMConfig:
    """Tests for MicroVMConfig."""

    def test_default_config(self):
        config = MicroVMConfig()
        assert config.mem_size_mib == 256
        assert config.vcpu_count == 1
        assert config.timeout_seconds == 60
        assert config.network_enabled is False

    def test_custom_config(self):
        config = MicroVMConfig(
            mem_size_mib=512,
            vcpu_count=2,
            timeout_seconds=120,
            network_enabled=True,
        )
        assert config.mem_size_mib == 512
        assert config.vcpu_count == 2

    def test_env_var_paths(self):
        """MicroVMConfig should use env vars for path defaults."""
        config = MicroVMConfig()
        # Default paths should be set from env or fallback
        assert config.kernel_path is not None
        assert config.rootfs_path is not None

    def test_explicit_paths(self):
        """MicroVMConfig should accept explicit paths."""
        config = MicroVMConfig(
            kernel_path="/custom/vmlinux",
            rootfs_path="/custom/rootfs.ext4",
        )
        assert config.kernel_path == "/custom/vmlinux"
        assert config.rootfs_path == "/custom/rootfs.ext4"


class TestMicroVMResult:
    """Tests for MicroVMResult dataclass."""

    def test_success(self):
        result = MicroVMResult(exit_code=0, stdout="hello\n", stderr="")
        assert result.ok is True
        assert result.exit_code == 0
        assert result.sandbox_type == "fallback"

    def test_failure(self):
        result = MicroVMResult(exit_code=1, stdout="", stderr="error")
        assert result.ok is False
        assert result.sandbox_type == "fallback"

    def test_microvm_type(self):
        result = MicroVMResult(exit_code=0, stdout="", stderr="", sandbox_type="microvm")
        assert result.sandbox_type == "microvm"
        assert result.ok is True


class TestMicroVMSandboxAvailability:
    """Tests for MicroVM availability checking."""

    def test_raises_when_binary_missing(self):
        """Missing firecracker should make available=False with clear message."""
        sandbox = MicroVMSandbox()
        # Patch shutil.which to simulate missing binary
        with patch("shutil.which", return_value=None):
            sandbox._available = None
            assert sandbox.available is False

    def test_raises_when_kernel_missing(self):
        """Missing kernel image should make available=False."""
        config = MicroVMConfig(
            kernel_path="/nonexistent/vmlinux",
            rootfs_path="/nonexistent/rootfs.ext4",
        )
        sandbox = MicroVMSandbox(config=config)
        with (
            patch("shutil.which", return_value="/usr/local/bin/firecracker"),
            patch("os.path.exists", side_effect=lambda p: p == "/dev/kvm"),
        ):
            sandbox._available = None
            assert sandbox.available is False

    def test_config_from_toml(self):
        """MicroVMConfig should accept values matching config.toml section."""
        config = MicroVMConfig(
            kernel_path="/usr/share/beagle/vmlinux",
            rootfs_path="/usr/share/beagle/rootfs.ext4",
            vcpu_count=1,
            mem_size_mib=128,
            timeout_seconds=60,
        )
        assert config.vcpu_count == 1
        assert config.mem_size_mib == 128


class TestMicroVMConfigGeneration:
    """Tests for VM config generation."""

    def test_create_vm_config_basic(self):
        """_create_vm_config should produce valid Firecracker config."""
        config = MicroVMConfig(
            kernel_path="/usr/share/beagle/vmlinux",
            rootfs_path="/usr/share/beagle/rootfs.ext4",
        )
        sandbox = MicroVMSandbox(config=config)
        vm_config = sandbox._create_vm_config("test123", ["echo", "hello"])

        assert vm_config["boot-source"]["kernel_image_path"] == "/usr/share/beagle/vmlinux"
        assert "beagle.cmd=echo hello" in vm_config["boot-source"]["boot_args"]
        assert vm_config["drives"][0]["drive_id"] == "rootfs"
        assert vm_config["drives"][0]["is_root_device"] is True
        assert vm_config["machine-config"]["vcpu_count"] == 1
        assert vm_config["machine-config"]["mem_size_mib"] == 256

    def test_create_vm_config_no_network(self):
        """_create_vm_config with network_enabled=False should have empty ifaces."""
        config = MicroVMConfig(network_enabled=False)
        sandbox = MicroVMSandbox(config=config)
        vm_config = sandbox._create_vm_config("abc", ["ls"])
        assert vm_config["network-interfaces"] == []

    def test_create_vm_config_with_network(self):
        """_create_vm_config with network_enabled=True should add iface."""
        config = MicroVMConfig(network_enabled=True)
        sandbox = MicroVMSandbox(config=config)
        vm_config = sandbox._create_vm_config("abc", ["ls"])
        assert len(vm_config["network-interfaces"]) == 1
        assert vm_config["network-interfaces"][0]["iface_id"] == "eth0"

    def test_config_json_serializable(self):
        """VM config must be JSON-serializable for Firecracker."""
        config = MicroVMConfig()
        sandbox = MicroVMSandbox(config=config)
        vm_config = sandbox._create_vm_config("test", ["echo"])
        # Should not raise
        json_str = json.dumps(vm_config)
        assert isinstance(json_str, str)
        parsed = json.loads(json_str)
        assert "boot-source" in parsed


class TestMicroVMStopAndHealthCheck:
    """Tests for VM lifecycle management."""

    @pytest.mark.asyncio
    async def test_stop_vm_terminated_process(self):
        """_stop_vm should handle already-terminated process."""
        sandbox = MicroVMSandbox()
        mock_proc = MagicMock()
        mock_proc.returncode = 0  # Already stopped
        await sandbox._stop_vm(mock_proc)
        mock_proc.terminate.assert_not_called()

    @pytest.mark.asyncio
    async def test_stop_vm_sends_sigterm(self):
        """_stop_vm should send SIGTERM then wait."""
        sandbox = MicroVMSandbox()
        mock_proc = MagicMock()
        mock_proc.returncode = None
        mock_proc.wait = AsyncMock(return_value=0)
        await sandbox._stop_vm(mock_proc)
        mock_proc.terminate.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_vm_sigkill_after_timeout(self):
        """_stop_vm should send SIGKILL if SIGTERM times out."""
        sandbox = MicroVMSandbox()
        mock_proc = MagicMock()
        mock_proc.returncode = None
        # First wait() raises TimeoutError (SIGTERM didn't work)
        # Second wait() succeeds (after SIGKILL)
        mock_proc.wait = AsyncMock(side_effect=[TimeoutError, None])
        await sandbox._stop_vm(mock_proc)
        mock_proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_health_check_returns_false_on_connection_error(self):
        """_health_check should return False when socket unavailable."""
        sandbox = MicroVMSandbox()
        result = await sandbox._health_check("/nonexistent/socket")
        assert result is False


class TestMicroVMSandboxFallack:
    """Tests for MicroVM fallback execution."""

    def _optin_sandbox(self) -> MicroVMSandbox:
        """Build a sandbox that explicitly permits the subprocess degrade."""
        sandbox = MicroVMSandbox(config=MicroVMConfig(allow_fallback=True))
        # Force fallback since firecracker is unlikely available
        sandbox._available = False
        return sandbox

    @pytest.mark.asyncio
    async def test_fallback_echo(self):
        """Fallback mode should execute commands via SandboxedExecutor when opted in."""
        sandbox = self._optin_sandbox()
        stdout, _stderr, exit_code = await sandbox.run(["echo", "hello"])
        assert exit_code == 0
        assert b"hello" in stdout

    @pytest.mark.asyncio
    async def test_fallback_failure_exit_code(self):
        """Fallback mode should capture non-zero exit codes."""
        sandbox = self._optin_sandbox()
        _stdout, _stderr, exit_code = await sandbox.run(["false"])
        assert exit_code != 0

    @pytest.mark.asyncio
    async def test_stderr_capture_in_fallback(self):
        """Fallback mode should capture stderr."""
        sandbox = self._optin_sandbox()
        _stdout, stderr, _exit_code = await sandbox.run(
            ["python3", "-c", "import sys; sys.stderr.write('err\\n')"]
        )
        assert b"err" in stderr

    @pytest.mark.asyncio
    async def test_fallback_denied_by_default(self):
        """Deny-by-default: without allow_fallback, degrade refuses to run.

        C01 (README remediation follow-up): a fail-open sandbox must not
        silently drop hardware isolation. With ``allow_fallback=False``
        (the default), an unavailable MicroVM must refuse to execute the
        payload at reduced isolation rather than degrade silently.
        """
        sandbox = MicroVMSandbox()  # default: allow_fallback=False
        sandbox._available = False
        stdout, _stderr, exit_code = await sandbox.run(["echo", "hello"])
        assert exit_code == 126, "deny-by-default must refuse to run the payload"
        assert b"REFUSING" in stdout or b"fallback disabled" in stdout
        # The payload must NOT have executed.
        assert b"hello" not in stdout


class TestSandboxContext:
    """Tests for SandboxContext resource limit capture/restore (C1 fix)."""

    @pytest.mark.skipif(
        not hasattr(__import__("resource"), "RLIMIT_NOFILE"),
        reason="POSIX-only test: resource module unavailable",
    )
    def test_sandbox_restores_rlimits_on_exit(self):
        """Every sandboxed rlimit must come back, not just the one we look at.

        This test used to check RLIMIT_NOFILE alone and passed while
        RLIMIT_NPROC leaked at (8, 8) for the life of the process — after which
        every thread and subprocess creation raised "can't start new thread"
        and the whole suite aborted with INTERNALERROR about thirty tests later.
        A restore test that checks one of five limits is a restore test that
        cannot fail for the right reason.
        """
        import resource

        from beagle.core.sandbox import SandboxConfig, SandboxContext

        limits = {
            "RLIMIT_AS": resource.RLIMIT_AS,
            "RLIMIT_CPU": resource.RLIMIT_CPU,
            "RLIMIT_STACK": resource.RLIMIT_STACK,
            "RLIMIT_NOFILE": resource.RLIMIT_NOFILE,
            "RLIMIT_NPROC": resource.RLIMIT_NPROC,
        }
        before = {name: resource.getrlimit(lt) for name, lt in limits.items()}

        with SandboxContext(SandboxConfig(max_files=16, max_processes=8)):
            inside = resource.getrlimit(resource.RLIMIT_NOFILE)
            assert inside[0] == 16, f"rlimit not lowered inside sandbox: {inside}"
            # The sandbox must lower the soft limit and leave the hard limit
            # alone; a lowered hard limit cannot be raised back by an
            # unprivileged process, so it would make the restore impossible.
            in_nproc = resource.getrlimit(resource.RLIMIT_NPROC)
            assert in_nproc[0] <= 8, f"NPROC soft not lowered: {in_nproc}"
            assert in_nproc[1] == before["RLIMIT_NPROC"][1], (
                f"sandbox lowered the NPROC hard limit to {in_nproc[1]}; "
                f"that is irreversible and breaks restore"
            )

        after = {name: resource.getrlimit(lt) for name, lt in limits.items()}
        leaked = {n: (before[n], after[n]) for n in limits if before[n] != after[n]}
        assert not leaked, f"rlimits leaked out of the sandbox: {leaked}"
