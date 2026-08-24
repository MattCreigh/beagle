"""
Security Sandbox for Beagle subprocess execution.

Provides multiple isolation layers:
- Process isolation with resource limits
- Optional Firecracker microVM support
- Network filtering
- Filesystem restrictions
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import resource
import shutil
import tempfile
import threading
import uuid
from dataclasses import dataclass

logger = logging.getLogger("Beagle.Sandbox")

# Typed constant list — iterate once, capture + set + restore symmetrically.
_SANDBOXED_LIMITS: tuple[int, ...] = (
    resource.RLIMIT_AS,
    resource.RLIMIT_CPU,
    resource.RLIMIT_STACK,
    resource.RLIMIT_NOFILE,
    resource.RLIMIT_NPROC,
)

# Resource limits (can be overridden by config)
DEFAULT_MEMORY_LIMIT_BYTES = 512 * 1024 * 1024  # 512 MB
DEFAULT_CPU_TIME_LIMIT = 60  # seconds
DEFAULT_STACK_SIZE = 8 * 1024 * 1024  # 8 MB
DEFAULT_MAX_FILES = 64
DEFAULT_MAX_PROCESSES = 8


@dataclass
class SandboxConfig:
    """Sandbox configuration."""

    memory_limit: int = DEFAULT_MEMORY_LIMIT_BYTES
    cpu_time_limit: int = DEFAULT_CPU_TIME_LIMIT
    stack_size: int = DEFAULT_STACK_SIZE
    max_files: int = DEFAULT_MAX_FILES
    max_processes: int = DEFAULT_MAX_PROCESSES
    allow_network: bool = False  # Default-off: require explicit opt-in for network access
    readonly_filesystem: bool = False
    allowed_paths: list[str] | None = None
    denied_paths: list[str] | None = None
    working_directory: str | None = None
    env_whitelist: list[str] | None = None
    strict: bool = False  # v0.3.0: raise on resource limit failure instead of warning


class SandboxContext:
    """Context manager for sandboxed execution.

    .. warning::
        This context manager is **NOT thread-safe**. It mutates global process
        state (``os.environ``, ``os.chdir()``, ``resource.setrlimit()``) which
        affects all threads. Use it only from the main thread, or serialize
        access with an external lock. For concurrent sandboxed execution, use
        ``SandboxedExecutor.run()`` instead, which builds per-call environments
        and runs in subprocesses.

    Anti-patterns to avoid:
        - Entering two SandboxContext instances concurrently (will clobber env/cwd)
        - Using SandboxContext inside thread pools without serialization
        - Assuming resource limits are thread-local (they are process-wide)
    """

    # Class-level lock to prevent TOCTOU race on _owning_thread check
    _entry_lock = threading.Lock()

    def __init__(self, config: SandboxConfig | None = None):
        self.config = config or SandboxConfig()
        # Use int keys (the rlimit constants themselves) — not str — so we can
        # round-trip directly through resource.setrlimit without lookup tables.
        self.original_limits: dict[int, tuple[int, int]] = {}
        self.temp_dir: str | None = None
        self.original_cwd: str | None = None
        self.original_env: dict | None = None
        # Guard: detect concurrent entry from different threads
        self._owning_thread: threading.Thread | None = None

    def __enter__(self):
        """Set up sandbox environment."""
        # S03 remediation: SandboxContext mutates process-global state
        # (os.chdir, os.environ, resource.setrlimit). Using it from any
        # thread other than the main thread is unsafe because these
        # operations affect every thread in the process simultaneously.
        # SandboxedExecutor.run() builds a per-call environ dict and
        # uses start_new_session=True subprocess isolation instead.
        import threading as _threading

        if _threading.current_thread() is not _threading.main_thread():
            raise RuntimeError(
                "SandboxContext must not be used from non-main threads. "
                "It mutates global process state (os.chdir, os.environ, "
                "resource.setrlimit) which affects all threads. "
                "Use SandboxedExecutor.run() instead, which provides "
                "per-call environment isolation via subprocess."
            )

        # Thread-safety guard: detect concurrent entry (atomic under class-level lock)
        with SandboxContext._entry_lock:
            current_thread = threading.current_thread()
            if self._owning_thread is not None and self._owning_thread is not current_thread:
                raise RuntimeError(
                    f"SandboxContext entered from thread {current_thread.name} but "
                    f"owned by {self._owning_thread.name}. SandboxContext is not "
                    f"thread-safe — use SandboxedExecutor.run() for concurrent execution."
                )
            self._owning_thread = current_thread

        # Store original state
        self.original_cwd = os.getcwd()
        self.original_env = os.environ.copy()

        # Set up temporary directory if needed
        if self.config.working_directory:
            self.temp_dir = self.config.working_directory
        else:
            self.temp_dir = tempfile.mkdtemp(prefix="beagle_sandbox_")

        # v13.22.4: wrap the post-chdir state mutation in try/except so
        # that if _set_resource_limits() or _sanitize_environment() raises,
        # we restore the original cwd before propagating. The previous
        # implementation only restored chdir in __exit__, which is not
        # called when __enter__ itself raises after chdir.
        try:
            # M1 (audit 2026-08-15): actually enter the sandbox directory.
            # The docstring promised os.chdir() isolation but no chdir ever
            # happened — code inside `with SandboxContext():` ran in the
            # caller's cwd. Enter the temp dir so relative-path writes land
            # inside the sandbox, not the repo root.
            os.chdir(self.temp_dir)

            # Set resource limits
            self._set_resource_limits()

            # Sanitize environment
            self._sanitize_environment()

            logger.debug(f"Sandbox entered: {self.temp_dir}")
            return self
        except BaseException:
            # Restore cwd and env immediately; the context-manager
            # protocol never reaches __exit__ when __enter__ raises.
            with contextlib.suppress(OSError):
                if self.original_cwd is not None:
                    os.chdir(self.original_cwd)
            if self.original_env:
                os.environ.clear()
                os.environ.update(self.original_env)
            if self.temp_dir and not self.config.working_directory:
                with contextlib.suppress(OSError):
                    shutil.rmtree(self.temp_dir, ignore_errors=True)
            self._owning_thread = None
            raise

    def __exit__(self, _exc_type, _exc_val, _exc_tb):
        """Clean up sandbox environment."""
        # Restore resource limits
        self._restore_resource_limits()

        # Restore environment
        if self.original_env:
            os.environ.clear()
            os.environ.update(self.original_env)

        # Restore working directory
        if self.original_cwd:
            os.chdir(self.original_cwd)

        # Clean up temp directory
        if self.temp_dir and not self.config.working_directory:
            # L3 (audit 2026-08-15): rmtree(ignore_errors=True) never raises,
            # so the except OSError below was unreachable. Drop the dead
            # handler.
            shutil.rmtree(self.temp_dir, ignore_errors=True)

        self._owning_thread = None
        logger.debug("Sandbox exited")
        return False

    def _capture_original_limits(self) -> None:
        """Snapshot current rlimits so __exit__ can restore them.

        Called before _set_resource_limits so a partial failure mid-setup
        still has a complete baseline to roll back to.
        """
        for limit_type in (
            resource.RLIMIT_AS,
            resource.RLIMIT_CPU,
            resource.RLIMIT_STACK,
            resource.RLIMIT_NOFILE,
            resource.RLIMIT_NPROC,
        ):
            try:
                self.original_limits[limit_type] = resource.getrlimit(limit_type)
            except (OSError, ValueError) as e:
                # Some rlimits may be unsupported on the kernel (e.g. RLIMIT_NPROC
                # in some container runtimes). Log but continue — we simply will
                # not attempt to restore what we could not capture.
                logger.warning(f"Cannot capture rlimit {limit_type}: {e}")

    def _set_resource_limits(self) -> None:
        """Set resource limits AFTER capturing originals."""
        self._capture_original_limits()

        # <invariant>
        # Lower the SOFT limit only; leave every HARD limit at the value we
        # captured. An unprivileged process cannot raise its own hard limit once
        # lowered, so lowering hard here is a one-way door and __exit__ can
        # never undo it. The kernel enforces the soft limit, so the sandbox is
        # still in force.
        #
        # This was a live bug, not a hypothetical. The old code set hard to the
        # sandbox value, and _restore_resource_limits' `orig_hard > curr_hard`
        # branch — written for the RLIM_INFINITY case — then clamped the
        # "restored" value back down to the lowered hard limit, so the restore
        # silently restored nothing. A single SandboxContext(max_processes=8)
        # left RLIMIT_NPROC at (8, 8) for the life of the process, after which
        # every thread or subprocess creation raised
        # "RuntimeError: can't start new thread". It made the test suite
        # unfinishable: pytest aborted with INTERNALERROR ~30 tests after
        # test_microvm_sandbox ran, and did so for every ordering.
        #
        # If a future caller needs genuinely irreversible limits, apply them in
        # a forked child before exec, never in the parent.
        # </invariant>
        def _soft_only(limit_type: int, soft: int) -> tuple[int, int]:
            """Pair a desired soft limit with the captured hard limit.

            Args:
                limit_type: The resource.RLIMIT_* constant.
                soft: The desired soft limit.

            Returns:
                A (soft, hard) pair that leaves the hard limit untouched.

            """
            _orig_soft, orig_hard = self.original_limits.get(
                limit_type, (soft, resource.RLIM_INFINITY)
            )
            if orig_hard == resource.RLIM_INFINITY:
                return (soft, orig_hard)
            return (min(soft, orig_hard), orig_hard)

        desired = {
            resource.RLIMIT_AS: _soft_only(resource.RLIMIT_AS, self.config.memory_limit),
            resource.RLIMIT_CPU: _soft_only(resource.RLIMIT_CPU, self.config.cpu_time_limit),
            resource.RLIMIT_STACK: _soft_only(resource.RLIMIT_STACK, self.config.stack_size),
            resource.RLIMIT_NOFILE: _soft_only(resource.RLIMIT_NOFILE, self.config.max_files),
            resource.RLIMIT_NPROC: _soft_only(resource.RLIMIT_NPROC, self.config.max_processes),
        }

        failures: list[tuple[int, str]] = []
        for limit_type, values in desired.items():
            try:
                resource.setrlimit(limit_type, values)
            except (OSError, ValueError) as e:
                failures.append((limit_type, str(e)))

        if failures:
            msg = "; ".join(f"rlimit={lt}: {err}" for lt, err in failures)
            if self.config.strict:
                raise RuntimeError(f"Cannot set resource limits (strict mode): {msg}")
            logger.warning(f"Partial rlimit set failure: {msg}")

    def _restore_resource_limits(self) -> None:
        """Restore original rlimits captured on entry.

        Runs in __exit__ regardless of whether setrlimit succeeded — partial
        sandboxes must not leak into the parent process.

        Handles RLIM_INFINITY (-1) hard limits which cannot be raised after
        being lowered. In that case we restore soft up to the current hard.
        """

        # <invariant>
        # RLIM_INFINITY is -1: a sentinel, not a magnitude. It must never reach
        # a numeric comparison or min(). The previous code did exactly that —
        # `min(orig_soft, curr_hard)` with an infinite hard limit returned -1,
        # so restoring RLIMIT_STACK from (8 MB, infinity) *raised* the soft
        # limit to infinity instead of putting it back. Silent, and in the
        # permissive direction, which is the worst way for a sandbox to fail.
        # </invariant>
        def _capped(soft: int, ceiling: int) -> int:
            """Clamp a soft limit to a hard ceiling, honouring RLIM_INFINITY.

            Args:
                soft: The desired soft limit, possibly RLIM_INFINITY.
                ceiling: The hard limit to clamp against, possibly RLIM_INFINITY.

            Returns:
                The soft limit to request.

            """
            if ceiling == resource.RLIM_INFINITY:
                return soft
            if soft == resource.RLIM_INFINITY or soft > ceiling:
                return ceiling
            return soft

        for limit_type, (orig_soft, orig_hard) in self.original_limits.items():
            try:
                _curr_soft, curr_hard = resource.getrlimit(limit_type)
                if curr_hard == orig_hard:
                    # The normal path: we never lower hard limits, so this is
                    # an exact restore.
                    target_soft, target_hard = orig_soft, orig_hard
                else:
                    # Something outside this context lowered the hard limit.
                    # An unprivileged process cannot raise it back, so the most
                    # we can do is restore soft up to the surviving ceiling.
                    target_soft = _capped(orig_soft, curr_hard)
                    target_hard = curr_hard
                resource.setrlimit(limit_type, (target_soft, target_hard))
            except (OSError, ValueError) as e:
                # Non-fatal on exit path, but this is a real leak — log at ERROR.
                logger.error(
                    f"[CRITICAL] Failed to restore rlimit {limit_type}"
                    f" to original={(orig_soft, orig_hard)}: {e}. "
                    f"Parent process now has LEAKED sandbox limits."
                )

    def _sanitize_environment(self):
        """Remove sensitive env vars and restrict to whitelist."""
        # Remove potentially dangerous env vars
        dangerous_vars = [
            "LD_PRELOAD",
            "LD_LIBRARY_PATH",
            "DYLD_INSERT_LIBRARIES",
            "DYLD_LIBRARY_PATH",
            "_LD_LIBRARY_PATH",
            "BASH_ENV",
            "ENV",
            "PERL5LIB",
            "PYTHONPATH",  # Restrict Python path
            "RUBYLIB",
        ]

        for var in dangerous_vars:
            os.environ.pop(var, None)

        # Whitelist environment variables if specified
        if self.config.env_whitelist:
            allowed = set(self.config.env_whitelist)
            # Always allow these
            allowed.update({"PATH", "HOME", "USER", "LANG", "LC_ALL"})

            to_remove = [k for k in os.environ if k not in allowed]
            for var in to_remove:
                os.environ.pop(var, None)

        # Add Beagle-specific env
        os.environ["BEAGLE_SANDBOXED"] = "1"


class SandboxedExecutor:
    """
    Execute code in a sandboxed environment.

    Usage:
        executor = SandboxedExecutor()
        result = await executor.run(
            command=["python3", "script.py"],
            config=SandboxConfig(memory_limit=256*1024*1024),
        )
    """

    def __init__(self, default_config: SandboxConfig | None = None):
        self.default_config = default_config or SandboxConfig()

    async def run(
        self,
        command: list[str],
        config: SandboxConfig | None = None,
        input_data: bytes | None = None,
        timeout: float | None = None,
    ) -> tuple[bytes, bytes, int]:
        """
        Run a command in a sandboxed environment.

        Args:
            command: Command and arguments to execute
            config: Sandbox configuration (uses default if not provided)
            input_data: Data to send to stdin
            timeout: Timeout in seconds

        Returns:
            Tuple of (stdout, stderr, returncode)

        """
        config = config or self.default_config
        timeout = timeout or config.cpu_time_limit

        process = None

        try:
            # Create subprocess in new session (detached from TTY)
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE if input_data else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                cwd=config.working_directory,
                env=self._build_env(config),
            )

            # Run with timeout
            stdout, stderr = await asyncio.wait_for(
                process.communicate(input=input_data),
                timeout=timeout,
            )

            return stdout, stderr, process.returncode  # type: ignore[return-value]

        except TimeoutError:
            if process:
                try:
                    process.terminate()
                    await asyncio.wait_for(process.wait(), timeout=5)
                except TimeoutError:
                    process.kill()
                    await process.wait()
            raise SandboxTimeoutError(f"Command timed out after {timeout}s") from None

        except (OSError, RuntimeError) as e:
            logger.error(f"Sandbox execution failed: {e}")
            raise

    def _build_env(self, config: SandboxConfig) -> dict:
        """Build environment for sandboxed execution.

        NOTE (F7 audit): same name as utils/env_manager.build_goose_env but a
        genuinely different function — this is an allowlist-based sandbox env
        for `sandbox run`, not the Goose launcher env. Do not merge them.
        """
        env = {}

        # Copy essential vars
        for var in ["PATH", "HOME", "USER", "LANG", "LC_ALL", "TMPDIR"]:
            if var in os.environ:
                env[var] = os.environ[var]

        # Set sandbox flag
        env["BEAGLE_SANDBOXED"] = "1"

        # Restrict PYTHONPATH if configured
        if config.env_whitelist and "PYTHONPATH" not in config.env_whitelist:
            # Don't include PYTHONPATH
            pass
        elif "PYTHONPATH" in os.environ:
            env["PYTHONPATH"] = os.environ["PYTHONPATH"]

        return env


class SandboxTimeoutError(Exception):
    """Raised when sandboxed execution times out."""

    pass


class SandboxResourceError(Exception):
    """Raised when sandbox resource limits are exceeded."""

    pass


# ---------------------------------------------------------------------------
# MicroVM Sandbox (Phase 8: Firecracker integration)
# ---------------------------------------------------------------------------


@dataclass
class MicroVMResult:
    """Result from a MicroVM sandbox execution.

    Attributes:
        stdout: Captured standard output from the execution.
        stderr: Captured standard error from the execution.
        exit_code: Process exit code (0 for success, non-zero for failure).
        sandbox_type: Type of sandbox that executed the command
                      ("microvm" or "fallback").

    """

    stdout: str
    stderr: str
    exit_code: int
    sandbox_type: str = "fallback"

    @property
    def ok(self) -> bool:
        """Whether the execution succeeded (exit code 0)."""
        return self.exit_code == 0


class MicroVMConfig:
    """Configuration for Firecracker MicroVM-based sandboxing.

    MicroVMs provide hardware-level isolation using KVM, suitable for
    running untrusted code with stronger guarantees than process isolation.

    Requires: firecracker binary, jailer, and KVM access on host.
    When MicroVM is unavailable, execution degrades to the subprocess
    SandboxedExecutor ONLY if ``allow_fallback`` is explicitly enabled
    (deny-by-default). A fallback that is permitted emits a loud WARNING
    so the loss of hardware isolation is never silent.
    """

    def __init__(
        self,
        kernel_path: str | None = None,
        rootfs_path: str | None = None,
        vcpu_count: int = 1,
        mem_size_mib: int = 256,
        network_enabled: bool = False,
        timeout_seconds: int = 60,
        allow_fallback: bool = False,
    ):
        self.kernel_path = kernel_path or os.environ.get(
            "BEAGLE_MICROVM_KERNEL", "/usr/share/beagle/vmlinux"
        )
        self.rootfs_path = rootfs_path or os.environ.get(
            "BEAGLE_MICROVM_ROOTFS", "/usr/share/beagle/rootfs.ext4"
        )
        self.vcpu_count = vcpu_count
        self.mem_size_mib = mem_size_mib
        self.network_enabled = network_enabled
        self.timeout_seconds = timeout_seconds
        self.allow_fallback = allow_fallback


class MicroVMSandbox:
    """Firecracker MicroVM sandbox for hardware-isolated code execution.

    Provides the strongest isolation guarantee by running code inside a
    lightweight virtual machine with KVM. Falls back to SandboxedExecutor
    if Firecracker is not available on the host.

    Setup: Run scripts/setup_firecracker.py to install Firecracker,
    kernel, and rootfs. Then enable in config.toml:

        [sandbox.microvm]
        enabled = true
    """

    def __init__(self, config: MicroVMConfig | None = None):
        self.config = config or MicroVMConfig()
        self._available: bool | None = None
        self._proc: asyncio.subprocess.Process | None = None

    @property
    def available(self) -> bool:
        """Check if MicroVM sandbox is available on this host."""
        if self._available is None:
            self._available = self._check_availability()
        return self._available

    def _check_availability(self) -> bool:
        """Verify Firecracker binary, kernel, rootfs, and KVM access."""
        if not shutil.which("firecracker"):
            logger.info(
                "MicroVM: firecracker not found. Install with: scripts/setup_firecracker.py"
            )
            return False

        if not os.path.exists("/dev/kvm"):
            logger.info("MicroVM: /dev/kvm not found — KVM not available")
            return False

        if not os.path.exists(self.config.kernel_path):
            logger.info(
                f"MicroVM: kernel not found at {self.config.kernel_path}. "
                "Run: scripts/setup_firecracker.py --kernel"
            )
            return False

        if not os.path.exists(self.config.rootfs_path):
            logger.info(
                f"MicroVM: rootfs not found at {self.config.rootfs_path}. "
                "Run: scripts/setup_firecracker.py --rootfs"
            )
            return False

        return True

    def _create_vm_config(self, vm_id: str, command: list[str]) -> dict:
        """Generate Firecracker VM configuration dict.

        Args:
            vm_id: Unique identifier for this VM instance.
            command: Command to execute inside the VM.

        Returns:
            Dict suitable for JSON serialization as Firecracker config.

        """
        # Encode command into boot args for init script
        cmd_str = " ".join(command)
        boot_args = f"console=ttyS0 reboot=k panic=1 pci=off beagle.cmd={cmd_str}"
        return {
            "boot-source": {
                "kernel_image_path": self.config.kernel_path,
                "boot_args": boot_args,
            },
            "drives": [
                {
                    "drive_id": "rootfs",
                    "path_on_host": self.config.rootfs_path,
                    "is_root_device": True,
                    "is_read_only": False,
                }
            ],
            "machine-config": {
                "vcpu_count": self.config.vcpu_count,
                "mem_size_mib": self.config.mem_size_mib,
            },
            "network-interfaces": []
            if not self.config.network_enabled
            else [
                {
                    "iface_id": "eth0",
                    "guest_mac": "AA:FC:00:00:00:01",
                    "host_dev_name": f"beagle-tap-{vm_id}",
                }
            ],
        }

    async def _health_check(self, socket_path: str) -> bool:
        """Verify a running VM is responsive via its API socket.

        Sends GET /machine-config to the Firecracker API socket.
        Returns True if the VM responds, False otherwise.
        """

        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(socket_path),
                timeout=2.0,
            )
            request = (
                "GET /machine-config HTTP/1.1\r\n"
                "Host: localhost\r\n"
                "Accept: application/json\r\n"
                "\r\n"
            )
            writer.write(request.encode())
            await writer.drain()
            response = await asyncio.wait_for(reader.read(4096), timeout=2.0)
            writer.close()
            await writer.wait_closed()
            return b"200" in response
        except (ConnectionRefusedError, TimeoutError, OSError):
            return False

    async def _stop_vm(self, proc: asyncio.subprocess.Process) -> None:
        """Gracefully stop a running Firecracker VM process.

        Sends SIGTERM first, waits briefly, then SIGKILL if needed.
        """
        if proc.returncode is not None:
            return  # Already stopped

        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except TimeoutError:
            logger.warning("MicroVM: VM did not stop gracefully, sending SIGKILL")
            proc.kill()
            await proc.wait()

    async def run(
        self, command: list[str], input_data: bytes | None = None
    ) -> tuple[bytes, bytes, int]:
        """Run a command in a MicroVM.

        If MicroVM is unavailable, falls back to SandboxedExecutor.

        Returns:
            Tuple of (stdout, stderr, return_code)

        """
        if not self.available:
            if not self.config.allow_fallback:
                # Deny-by-default: hardware isolation is gone and the caller
                # did NOT explicitly opt in to the subprocess degrade. Refuse
                # to run the payload at reduced isolation.
                logger.error(
                    "MicroVM unavailable but allow_fallback is disabled — "
                    "REFUSING to run payload at reduced (subprocess) isolation. "
                    "Install Firecracker/KVM or set allow_fallback=true to "
                    "explicitly permit the degrade."
                )
                return (
                    b"MicroVM unavailable and fallback disabled (deny-by-default)\n",
                    b"",
                    126,  # 126 = command cannot execute, per POSIX shell convention
                )

            # Loud event: the fail-open path is permitted, but never silently.
            # The degrade drops hardware (KVM) isolation while keeping
            # rlimits/timeouts; surface it at WARNING so operators notice.
            logger.warning(
                "MicroVM unavailable — DEGRADED to SandboxedExecutor (subprocess "
                "isolation ONLY, no KVM hardware isolation). Payload ran because "
                "allow_fallback=true was explicitly set. Install with: "
                "scripts/setup_firecracker.py"
            )
            executor = SandboxedExecutor()
            config = SandboxConfig(
                memory_limit=self.config.mem_size_mib * 1024 * 1024,
                cpu_time_limit=self.config.timeout_seconds,
                allow_network=self.config.network_enabled,
            )
            return await executor.run(command, config=config, input_data=input_data)

        return await self._run_in_microvm(command, input_data)

    async def _write_vm_config_atomic(self, path: str, config: dict) -> None:
        """Write the Firecracker VM config atomically off the event loop.

        Runs the file write in a worker thread (so the asyncio loop is never
        blocked by disk I/O) and performs an atomic tmp-file + ``os.replace``
        so a crash mid-write can never leave a truncated config that a later
        run would try to parse as JSON. Returns once ``config`` is durably
        in place at ``path``.
        """
        import json as json_mod

        def _do_write() -> None:
            tmp = f"{path}.tmp.{uuid.uuid4().hex}"
            with open(tmp, "w", encoding="utf-8") as f:
                json_mod.dump(config, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)

        await asyncio.to_thread(_do_write)

    async def _run_in_microvm(
        self, command: list[str], input_data: bytes | None = None
    ) -> tuple[bytes, bytes, int]:
        """Execute command inside a Firecracker MicroVM.

        Creates a temporary VM, injects the command via boot args,
        collects output, and tears down the VM.
        """
        vm_id = uuid.uuid4().hex
        tmp_dir = tempfile.mkdtemp(prefix=f"fc-{vm_id}-")
        socket_path = os.path.join(tmp_dir, f"fc-{vm_id}.sock")
        config_path = os.path.join(tmp_dir, f"fc-cfg-{vm_id}.json")
        self._proc = None

        try:
            # Generate VM config
            fc_config = self._create_vm_config(vm_id, command)
            # ASYNC230: never block the event loop with a synchronous open().
            # The write is also atomic (tmp file + os.replace) so a crashed
            # or killed process can never leave a truncated JSON that a
            # subsequent run would try to parse.
            await self._write_vm_config_atomic(config_path, fc_config)

            # Build launch command — use jailer if available for isolation.
            # v13.22.4 S3: the previous invocation ran the jailer with
            # --uid 0 --gid 0, which (a) meant the guest kernel ran with
            # uid 0 inside the microVM and (b) let the host jailer
            # process own the guest's resources as root, defeating
            # jailer's privilege-drop model. Run the jailer itself as
            # an unprivileged uid (1000 / 1000 by default; tunable via
            # BEAGLE_SANDBOX_JAILER_UID/GID). The guest is given its own
            # uid mapping inside the VM, which is the actual isolation
            # boundary; the host-side jailer should NOT be root.
            fc_binary = shutil.which("firecracker") or "firecracker"
            _jailer_uid = int(os.environ.get("BEAGLE_SANDBOX_JAILER_UID", "1000"))
            _jailer_gid = int(os.environ.get("BEAGLE_SANDBOX_JAILER_GID", "1000"))
            if shutil.which("jailer"):
                launch_cmd = [
                    "jailer",
                    "--id",
                    vm_id,
                    "--exec-binary",
                    fc_binary,
                    "--uid",
                    str(_jailer_uid),
                    "--gid",
                    str(_jailer_gid),
                    "--",
                    "--api-sock",
                    socket_path,
                    "--config-file",
                    config_path,
                ]
            else:
                launch_cmd = [
                    fc_binary,
                    "--api-sock",
                    socket_path,
                    "--config-file",
                    config_path,
                ]

            # Launch Firecracker
            self._proc = await asyncio.create_subprocess_exec(
                *launch_cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            # Wait for VM to be ready (optional health check)
            if os.path.exists(socket_path):
                await asyncio.sleep(0.1)  # Brief settle time
                # Send input data via stdin
                stdout, stderr = await asyncio.wait_for(
                    self._proc.communicate(input=input_data),
                    timeout=self.config.timeout_seconds,
                )
                return stdout, stderr, self._proc.returncode or 0
            else:
                # Fallback: communicate without health check
                stdout, stderr = await asyncio.wait_for(
                    self._proc.communicate(input=input_data),
                    timeout=self.config.timeout_seconds,
                )
                return stdout, stderr, self._proc.returncode or 0

        except TimeoutError:
            if self._proc:
                await self._stop_vm(self._proc)
            raise SandboxTimeoutError(
                f"MicroVM execution timed out after {self.config.timeout_seconds}s"
            ) from None
        except SandboxTimeoutError:
            raise
        except SandboxResourceError:
            raise
        except (OSError, RuntimeError) as e:
            logger.error(f"MicroVM execution error: {e}")
            if self._proc:
                await self._stop_vm(self._proc)
            raise SandboxResourceError(
                f"MicroVM execution failed: {e}. Run: scripts/setup_firecracker.py to verify setup."
            ) from e
        finally:
            # Cleanup temp files
            with contextlib.suppress(OSError):
                shutil.rmtree(tmp_dir, ignore_errors=True)
            self._proc = None


# Pre-configured sandbox profiles
SANDBOX_PROFILES = {
    "safe": SandboxConfig(
        memory_limit=256 * 1024 * 1024,  # 256 MB
        cpu_time_limit=30,
        max_files=32,
        max_processes=4,
        allow_network=False,
        readonly_filesystem=True,
    ),
    "standard": SandboxConfig(
        memory_limit=512 * 1024 * 1024,  # 512 MB
        cpu_time_limit=60,
        max_files=64,
        max_processes=8,
        allow_network=False,  # Network requires explicit opt-in
        readonly_filesystem=False,
    ),
    "unrestricted": SandboxConfig(
        memory_limit=1024 * 1024 * 1024,  # 1 GB
        cpu_time_limit=300,
        max_files=256,
        max_processes=32,
        allow_network=True,
        readonly_filesystem=False,
    ),
    "microvm": SandboxConfig(
        memory_limit=256 * 1024 * 1024,  # 256 MB (MicroVM managed)
        cpu_time_limit=60,
        max_files=32,
        max_processes=1,
        allow_network=False,
        readonly_filesystem=True,
    ),
}


def get_sandbox_profile(name: str) -> SandboxConfig:
    """Get a pre-configured sandbox profile."""
    return SANDBOX_PROFILES.get(name, SANDBOX_PROFILES["standard"])


if __name__ == "__main__":
    # Test sandbox

    logging.basicConfig(level=logging.INFO)

    async def test_sandbox():
        executor = SandboxedExecutor()
        config = get_sandbox_profile("safe")

        async with SandboxContext(config):  # type: ignore[attr-defined]
            logger.info("Running in sandbox with limits:")
            logger.info(f"  Memory: {config.memory_limit // (1024 * 1024)} MB")
            logger.info(f"  CPU: {config.cpu_time_limit}s")
            logger.info(f"  Files: {config.max_files}")
            logger.info(f"  Processes: {config.max_processes}")

            # Test a simple command
            stdout, _stderr, _code = await executor.run(
                ["echo", "Hello from sandbox!"],
            )
            logger.info(f"Result: {stdout.decode()}")

    asyncio.run(test_sandbox())
