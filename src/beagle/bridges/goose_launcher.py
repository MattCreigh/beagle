#!/usr/bin/env python3
"""
Goose launcher — starts the Ollama Cloud reasoning proxy, then execs the real goose binary.

PURE PYTHON — no shell wrapper. This script is symlinked as ~/.local/bin/goose.

Architecture:
  ~/.local/bin/goose  →  goose_launcher.py  (this file — shebang, standalone)
  ~/.local/bin/goose.real  →  real goose binary (goose 1.29.1)
  ../beagle/bridges/ollama_cloud_proxy.py  →  proxy (killed+restarted each launch)

Lifecycle:
  1. Kill any stale proxy from prior session (code may have changed)
  2. Start fresh proxy as detached child
  3. Poll /v1/models until ready (10s timeout)
  4. os.execv() → goose.real (replaces this process; proxy child survives)

On goose exit (SIGTERM/SIGINT/atexit): proxy is terminated.
"""

from __future__ import annotations

import contextlib
import os
import re
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from beagle.config.paths import resolve_executable
from beagle.security.validation import validate_http_url
from beagle.utils.atomic import atomic_write_text

# ── path resolution ─────────────────────────────────────────────────────────

_THIS_FILE = Path(__file__).resolve()
_REPO_ROOT = (
    _THIS_FILE.parents[2]
    if _THIS_FILE.parts[-3:] == ("beagle", "bridges", "goose_launcher.py")
    else None
)

# goose.real is always at ~/.local/bin/goose.real
_GOOSE_REAL = Path.home() / ".local/bin" / "goose.real"

# Proxy script: prefer same-dir (package bridges/), fallback to repo bridges/
_PROXY_SCRIPT = _THIS_FILE.parent / "ollama_cloud_proxy.py"
if not _PROXY_SCRIPT.is_file() and _REPO_ROOT is not None:
    _PROXY_SCRIPT = _REPO_ROOT / "bridges" / "ollama_cloud_proxy.py"

# ── constants ────────────────────────────────────────────────────────────────

_PROXY_PORT = 9999
_PROXY_HOST = "127.0.0.1"


def _runtime_dir() -> Path:
    """Return a private runtime directory (0700) for proxy state."""
    if "XDG_RUNTIME_DIR" in os.environ:
        base = Path(os.environ["XDG_RUNTIME_DIR"]) / "beagle"
    else:
        base = Path.home() / ".beagle" / "runtime"
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    return base


_PROXY_PID_FILE = _runtime_dir() / "ollama_cloud_proxy.pid"
_PROXY_SCRIPT_BASENAME = "ollama_cloud_proxy.py"  # cmdline marker for PID verification
_READY_TIMEOUT = 10.0
_POLL_INTERVAL = 0.5
_SIGTERM_DEADLINE_S = 3.0  # wait window before SIGKILL escalation
_SIGTERM_POLL_S = 0.1  # poll cadence while waiting for graceful exit

_proxy_proc: subprocess.Popen | None = None


# ── proxy health ─────────────────────────────────────────────────────────────


def _proxy_alive() -> bool:
    try:
        req = urllib.request.Request(
            validate_http_url(f"http://{_PROXY_HOST}:{_PROXY_PORT}/v1/models"),
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=3):  # nosec B310 - scheme checked by security.validation.validate_http_url before this call
            return True
    except Exception:  # ruff: ignore[BLE001]  # broad catch intentional — liveness probe: any failure means "not up"
        return False


# ── PID discovery & verification ────────────────────────────────────────────


def _pid_alive(pid: int) -> bool:
    """Return True iff ``pid`` is a live process we can signal.

    Uses ``os.kill(pid, 0)`` (signal 0 = existence/permission probe, no actual
    signal delivered). Raises ``ProcessLookupError`` if the PID is dead;
    ``PermissionError`` if it exists but is owned by another user.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not ours — still treat as live
    return True


def _pid_cmdline(pid: int) -> str | None:
    """Read ``/proc/<pid>/cmdline`` and return the joined command line.

    Returns ``None`` if the proc entry is missing, unreadable, or the PID is
    not a process we own (permission denied). NUL separators are replaced
    with spaces so the result is grep-friendly.
    """
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
        return None
    if not raw:
        return None
    # NUL-separated argv; drop trailing NUL and decode defensively.
    return raw.rstrip(b"\x00").replace(b"\x00", b" ").decode("utf-8", errors="replace")


def _is_verified_proxy_pid(pid: int) -> bool:
    """Return True iff ``pid`` is live AND its cmdline names the proxy script.

    This is the gate we enforce before ever calling ``os.kill`` on a PID that
    came from anywhere other than our own pidfile (port discovery can race with
    an unrelated process bound to 127.0.0.1:9999, e.g. a previous launcher
    instance that just started a non-proxy process).
    """
    if not _pid_alive(pid):
        return False
    cmdline = _pid_cmdline(pid)
    if cmdline is None:
        return False
    return _PROXY_SCRIPT_BASENAME in cmdline


def _pid_for_port(host: str, port: int) -> int | None:
    """Discover the PID listening on ``host:port``.

    Primary path: parse ``/proc/net/tcp`` (always available on Linux, no
    shell-out, no extra dependencies). Local addresses in /proc/net/tcp are
    little-endian hex; port 9999 = ``0x270F`` and 127.0.0.1 = ``0x0100007F``.

    Fallback path: shell out to ``ss -tlnp`` and parse its output. ``ss`` is
    in iproute2 on every modern Linux distro; the shell-out is bounded
    (3s timeout) and only used if /proc/net/tcp is unreadable.

    Returns the first PID that owns a LISTEN socket on the target address,
    or ``None`` if none can be found.
    """
    target_addr = f"{_ipv4_to_hex_le(host)}:{port:04X}"

    # ── primary: /proc/net/tcp ────────────────────────────────────────────
    try:
        with open("/proc/net/tcp", encoding="ascii", errors="replace") as fh:
            lines = fh.readlines()
    except (FileNotFoundError, PermissionError, OSError):
        lines = None

    if lines is not None and len(lines) >= 2:
        # Header: "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt
        #   uid  timeout inode"
        # We need columns: local_address (col 1), st (col 3), uid (col 7), inode (col 9).
        # PID is not in /proc/net/tcp directly — we must look it up by inode
        # under /proc/*/fd/* socket:[<inode>].
        for line in lines[1:]:
            parts = line.split()
            if len(parts) < 10:
                continue
            local_addr, st = parts[1], parts[3]
            inode = parts[9]
            if local_addr != target_addr:
                continue
            if st != "0A":  # 0A = TCP_LISTEN
                continue
            pid = _pid_for_inode(inode)
            if pid > 0:
                return pid
        return None

    # ── fallback: ss -tlnp ────────────────────────────────────────────────
    try:
        out = subprocess.run(
            # A missing `ss` raises FileNotFoundError from resolve_executable,
            # which the handler below already treats as "no PID found".
            [resolve_executable("ss"), "-tlnp", f"sport = :{port}"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        ).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None

    # ss line example:
    #   LISTEN 0 5 127.0.0.1:9999 0.0.0.0:* users:(("python3",pid=1234,fd=7))
    pid_match = re.search(r"pid=(\d+)", out)
    if pid_match:
        pid = int(pid_match.group(1))
        return pid if pid > 0 else None
    return None


def _pid_for_inode(inode: str) -> int:
    """Resolve a /proc/net/tcp inode to a PID by scanning /proc/[0-9]+/fd/*.

    Returns 0 if no PID owns a socket with the given inode.
    """
    try:
        pids = [int(p.name) for p in Path("/proc").iterdir() if p.name.isdigit()]
    except (FileNotFoundError, PermissionError, OSError):
        return 0
    needle = f"socket:[{inode}]"
    # A live /proc tree changes under the scan: descriptors close and processes
    # exit between listing and reading. Individually those are expected, so they
    # are counted rather than reported one by one. All of them failing means the
    # scan learned nothing, which is worth saying once.
    unreadable_pids = 0
    for pid in pids:
        try:
            fd_dir = Path(f"/proc/{pid}/fd")
            for fd in fd_dir.iterdir():
                # A descriptor that closes mid-scan, or one owned by another
                # user, simply is not the socket being looked for. suppress()
                # states that directly instead of an except clause whose only
                # job is to resume the loop.
                with contextlib.suppress(FileNotFoundError, PermissionError, OSError):
                    if os.readlink(fd) == needle:
                        return pid
        except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
            unreadable_pids += 1

    if pids and unreadable_pids == len(pids):
        print(
            f"goose-launcher: could not read the fd table of any of {len(pids)} "
            "processes; PID discovery by inode is not usable here",
            file=sys.stderr,
        )
    return 0


def _ipv4_to_hex_le(host: str) -> str:
    """Convert an IPv4 address to /proc/net/tcp little-endian hex form.

    ``127.0.0.1`` -> ``0100007F`` (bytes reversed: 01, 00, 00, 7F).
    """
    octets = host.split(".")
    if len(octets) != 4:
        raise ValueError(f"not an IPv4 address: {host!r}")
    return "".join(f"{int(o):02X}" for o in reversed(octets))


# ── proxy lifecycle ──────────────────────────────────────────────────────────


def _kill_proxy() -> None:
    global _proxy_proc
    if _proxy_proc is None:
        return
    proc, _proxy_proc = _proxy_proc, None
    try:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
    except ProcessLookupError:
        # The proxy already exited; the pidfile removal below is the whole
        # remaining cleanup, so there is nothing to report.
        print("goose-launcher: proxy already gone; clearing the stale pidfile", file=sys.stderr)
    _PROXY_PID_FILE.unlink(missing_ok=True)


def _read_pidfile() -> int | None:
    """Return the PID recorded in the pidfile, or ``None`` if missing/invalid."""
    if not _PROXY_PID_FILE.is_file():
        return None
    try:
        text = _PROXY_PID_FILE.read_text().strip()
    except OSError:
        return None
    if not text:
        return None
    try:
        pid = int(text)
    except ValueError:
        return None
    return pid if pid > 0 else None


def _resolve_stale_proxy_pid() -> int | None:
    """Return a verified PID for the stale proxy, or ``None`` if we cannot.

    Tries (in order):
      1. The pidfile (if present and valid).
      2. Port-based discovery (if proxy is reachable but pidfile is
         missing/corrupted). Discovered PIDs MUST pass cmdline verification
         before they are returned — we never hand out an unverified PID.

    Returns ``None`` when no verified PID can be established. The caller
    treats ``None`` as "we know the proxy is up but cannot identify it
    safely" and exits with an error rather than signalling blindly.
    """
    pidfile_pid = _read_pidfile()
    if pidfile_pid is not None and _is_verified_proxy_pid(pidfile_pid):
        return pidfile_pid

    # Pidfile missing or untrustworthy; fall back to port discovery.
    discovered = _pid_for_port(_PROXY_HOST, _PROXY_PORT)
    if discovered is not None and _is_verified_proxy_pid(discovered):
        return discovered

    return None


def _terminate_verified_pid(pid: int) -> None:
    """SIGTERM the verified PID, wait up to 3s, then SIGKILL the SAME pid.

    Plain ``os.kill(pid, ...)`` only — NEVER ``os.killpg`` or process-group
    signalling. The verified PID was confirmed to be ``ollama_cloud_proxy.py``
    via cmdline check, so a single-PID signal is the correct scope.

    Re-checks ``_pid_alive`` (signal 0) after each step; the port-liveness
    probe is checked again by the caller to confirm the socket is gone.
    """
    if not _pid_alive(pid):
        return

    # 1. Graceful: SIGTERM
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError:
        print(
            f"goose-launcher: PID {pid} not owned by us; refusing to SIGTERM",
            file=sys.stderr,
        )
        return

    deadline = time.monotonic() + _SIGTERM_DEADLINE_S
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return
        time.sleep(_SIGTERM_POLL_S)

    # 2. Force: SIGKILL the SAME verified pid (not killpg)
    if _pid_alive(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        except PermissionError:
            print(
                f"goose-launcher: PID {pid} not owned by us; refusing to SIGKILL",
                file=sys.stderr,
            )
            return

        # Brief wait for the kernel to reap the process.
        reap_deadline = time.monotonic() + 1.0
        while time.monotonic() < reap_deadline:
            if not _pid_alive(pid):
                return
            time.sleep(_SIGTERM_POLL_S)


def _start_proxy() -> None:
    global _proxy_proc

    # 1. Kill any stale proxy (code may have changed since last session)
    if _proxy_alive():
        old_pid = _resolve_stale_proxy_pid()
        if old_pid is None:
            # We KNOW the proxy is up (we just probed /v1/models), but we
            # cannot safely identify it — refuse to launch rather than
            # signalling an unverified PID. The port stays bound, the
            # operator can inspect 127.0.0.1:9999 manually.
            print(
                "goose-launcher: stale proxy detected on "
                f"{_PROXY_HOST}:{_PROXY_PORT} but no verified PID could be "
                "established (pidfile missing/invalid and port-discovery "
                f"cmdline check did not match {_PROXY_SCRIPT_BASENAME}); "
                "refusing to signal an unverified process",
                file=sys.stderr,
            )
            sys.exit(1)

        _terminate_verified_pid(old_pid)

        # Final re-check of the port: cmdline-liveness (signal 0) and HTTP
        # liveness can drift briefly while the kernel reaps the socket.
        # We MUST confirm the proxy is dead via _proxy_alive() BEFORE
        # unlinking the pidfile — deleting the pidfile before confirmation
        # destroys the only handle to the stale process and wedges every
        # subsequent launch.
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if not _proxy_alive():
                break
            time.sleep(0.3)
        if _proxy_alive():
            print("goose-launcher: stale proxy refused to die", file=sys.stderr)
            sys.exit(1)

        # Proxy is confirmed dead: safe to clear the pidfile.
        _PROXY_PID_FILE.unlink(missing_ok=True)

    # 2. Start fresh proxy
    _PROXY_PID_FILE.unlink(missing_ok=True)
    _proxy_proc = subprocess.Popen(
        [sys.executable, str(_PROXY_SCRIPT), "--port", str(_PROXY_PORT)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,  # survives os.execv() — proxy outlives goose sessions
    )
    # Atomic write: a stale-proxy reader must never parse a partial PID,
    # which could misclassify liveness or spawn against a wrong PID.
    atomic_write_text(_PROXY_PID_FILE, str(_proxy_proc.pid), mode=0o644)
    # No atexit/signal cleanup — os.execv() replaces this process entirely.
    # The proxy is killed on next launch by the stale-proxy logic above.

    # 3. Poll until ready
    deadline = time.monotonic() + _READY_TIMEOUT
    while time.monotonic() < deadline:
        if _proxy_alive():
            return
        time.sleep(_POLL_INTERVAL)

    print(f"goose-launcher: proxy failed to start within {_READY_TIMEOUT}s", file=sys.stderr)
    _kill_proxy()
    sys.exit(1)


# ── entry point ──────────────────────────────────────────────────────────────


def main() -> None:
    if not _GOOSE_REAL.is_file():
        print(f"goose-launcher: goose.real not found at {_GOOSE_REAL}", file=sys.stderr)
        sys.exit(1)
    if not _PROXY_SCRIPT.is_file():
        print(f"goose-launcher: proxy script not found at {_PROXY_SCRIPT}", file=sys.stderr)
        sys.exit(1)

    _start_proxy()
    # nosec B606 - execv without a shell is the point: this launcher replaces
    # itself with the real goose binary, whose absolute path is a module
    # constant, and no argument is interpreted by a shell.
    os.execv(str(_GOOSE_REAL), [str(_GOOSE_REAL), *sys.argv[1:]])  # nosec B606


if __name__ == "__main__":
    main()
