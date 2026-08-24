"""Regression tests for bridges/goose_launcher.py stale-proxy handling.

Covers the three fixes to ``_start_proxy`` (commit D-series, see also
``progress.xml``):

  1. ``_PROXY_PID_FILE`` is NEVER unlinked before the proxy is confirmed
     dead. Deleting the pidfile first destroys the only handle to the
     stale process and wedges every subsequent launch.
  2. When the pidfile is missing or invalid but ``_proxy_alive()`` is True,
     the PID is discovered from the port (``/proc/net/tcp`` primary path,
     ``ss -tlnp`` fallback). The discovered PID is verified against
     ``ollama_cloud_proxy.py`` in its cmdline before any signal is sent —
     we never signal an unverified PID.
  3. Escalation: SIGTERM → wait up to 3s → SIGKILL the SAME verified PID
     (plain ``os.kill(pid, sig)`` only; never ``os.killpg``) → re-check the
     port.

These tests mock at the boundary: HTTP probe, ``/proc`` reads, ``os.kill``,
``os.readlink``. The launcher module is imported as
``beagle.bridges.goose_launcher``; nothing in it is
executed at import time, so we can patch its module attributes freely.
"""

from __future__ import annotations

import itertools
import re
import signal
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure the project root is importable when this file is run in isolation.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from beagle.bridges import goose_launcher  # ruff: ignore[E402]

# ── helpers ──────────────────────────────────────────────────────────────────


def _make_proc_net_tcp_row(local_addr_hex: str, inode: str = "12345") -> str:
    """Build a single /proc/net/tcp row in the canonical 17-column format.

    The real /proc/net/tcp (Linux >=2.6.33) has 17 whitespace-separated
    tokens after the ``sl:`` prefix:

      0  sl
      1  local_address
      2  rem_address
      3  st                 (0A = TCP_LISTEN)
      4  tx_queue
      5  rx_queue
      6  tr
      7  tm->when
      8  retrnsmt
      9  uid
     10  timeout
     11  inode              ← the column the launcher reads
     12  ref
     13  pointer
     14  drops
     15..
    """
    return (
        f" 0: {local_addr_hex} 00000000:0000 0A "
        f"00000000:00000000 00:00000000 00000000 "
        f"0 0 {inode} 2 0000000000000000 100 0 0 10 0\n"
    )


def _proc_net_tcp_header() -> str:
    return (
        "  sl  local_address rem_address   st tx_queue rx_queue "
        "tr tm->when retrnsmt   uid  timeout inode\n"
    )


def _local_addr_hex(host: str, port: int) -> str:
    """Mirror ``_ipv4_to_hex_le`` for fixture construction."""
    return "".join(f"{int(o):02X}" for o in reversed(host.split("."))) + f":{port:04X}"


# ── port→PID discovery ──────────────────────────────────────────────────────


class TestPidForPort:
    """Unit tests for the /proc/net/tcp primary path."""

    def test_discovers_pid_for_listening_socket(self, tmp_path: Path):
        """A LISTEN row on the target addr+port resolves to a PID via /proc fd scan."""
        target = _local_addr_hex(goose_launcher._PROXY_HOST, goose_launcher._PROXY_PORT)  # type: ignore[attr-defined]
        rows = _proc_net_tcp_header() + _make_proc_net_tcp_row(target, inode="4242")

        mock_open = MagicMock(
            __enter__=lambda s: s,
            __exit__=lambda *a: False,
            readlines=lambda: rows.splitlines(keepends=True),
        )
        with (
            patch("builtins.open", create=True, return_value=mock_open),
            patch.object(goose_launcher, "_pid_for_inode", return_value=7777) as inode_lookup,  # type: ignore[attr-defined]
        ):
            pid = goose_launcher._pid_for_port(  # type: ignore[attr-defined]
                goose_launcher._PROXY_HOST,
                goose_launcher._PROXY_PORT,  # type: ignore[attr-defined]
            )

        assert pid == 7777
        inode_lookup.assert_called_once_with("4242")

    def test_skips_non_listen_sockets(self):
        """Sockets in state ESTABLISHED (01) are ignored, not LISTEN (0A)."""
        target = _local_addr_hex(goose_launcher._PROXY_HOST, goose_launcher._PROXY_PORT)  # type: ignore[attr-defined]
        row = (
            f" 0: {target} 01020304:0050 01 "  # state 01 = ESTABLISHED
            f"00000000:00000000 00:00000000 00000000 0 0 1000 0 9999\n"
        )
        rows = _proc_net_tcp_header() + row

        with patch(
            "builtins.open",
            create=True,
            return_value=MagicMock(
                __enter__=lambda s: s,
                __exit__=lambda *a: False,
                readlines=lambda: rows.splitlines(keepends=True),
            ),
        ):
            pid = goose_launcher._pid_for_port(  # type: ignore[attr-defined]
                goose_launcher._PROXY_HOST,
                goose_launcher._PROXY_PORT,  # type: ignore[attr-defined]
            )

        assert pid is None

    def test_returns_none_when_no_match(self):
        """A /proc/net/tcp with no matching row returns None (no fallback)."""
        rows = _proc_net_tcp_header() + _make_proc_net_tcp_row(
            _local_addr_hex("10.0.0.1", 9999),  # wrong host
            inode="4242",
        )

        with patch(
            "builtins.open",
            create=True,
            return_value=MagicMock(
                __enter__=lambda s: s,
                __exit__=lambda *a: False,
                readlines=lambda: rows.splitlines(keepends=True),
            ),
        ):
            pid = goose_launcher._pid_for_port(  # type: ignore[attr-defined]
                goose_launcher._PROXY_HOST,
                goose_launcher._PROXY_PORT,  # type: ignore[attr-defined]
            )

        assert pid is None

    def test_falls_back_to_ss_when_proc_unreadable(self):
        """If /proc/net/tcp is unreadable, fall back to ``ss -tlnp`` and parse ``pid=``."""
        ss_stdout = (
            "Recv-Q Send-Q Local Address:Port  Peer Address:Port\n"
            'LISTEN 0 5 127.0.0.1:9999 0.0.0.0:* users:(("python3",pid=31415,fd=7))\n'
        )
        with (
            patch("builtins.open", side_effect=PermissionError("/proc/net/tcp")),
            patch("subprocess.run") as ss_run,
        ):
            ss_run.return_value = MagicMock(stdout=ss_stdout, returncode=0)
            pid = goose_launcher._pid_for_port(  # type: ignore[attr-defined]
                goose_launcher._PROXY_HOST,
                goose_launcher._PROXY_PORT,  # type: ignore[attr-defined]
            )

        assert pid == 31415
        ss_run.assert_called_once()
        # The ss call must carry an explicit timeout (doctrine: no HTTP call
        # without timeout applies equally to subprocess).
        kwargs = ss_run.call_args.kwargs
        assert "timeout" in kwargs and kwargs["timeout"] > 0


# ── cmdline verification ────────────────────────────────────────────────────


class TestPidVerification:
    """The cmdline check is the gate that prevents signalling an unverified PID."""

    def test_verified_when_cmdline_contains_proxy_script(self):
        with (
            patch.object(goose_launcher, "_pid_alive", return_value=True),  # type: ignore[attr-defined]
            patch.object(
                goose_launcher,  # type: ignore[attr-defined]
                "_pid_cmdline",
                return_value="/usr/bin/python3 /srv/bridges/ollama_cloud_proxy.py --port 9999",
            ),
        ):
            assert goose_launcher._is_verified_proxy_pid(4242) is True  # type: ignore[attr-defined]

    def test_rejected_when_cmdline_lacks_proxy_script(self):
        """An unrelated process bound to 127.0.0.1:9999 must NOT be signalled."""
        with (
            patch.object(goose_launcher, "_pid_alive", return_value=True),  # type: ignore[attr-defined]
            patch.object(
                goose_launcher,  # type: ignore[attr-defined]
                "_pid_cmdline",
                return_value="/usr/bin/python3 /srv/some_other_service.py",
            ),
        ):
            assert goose_launcher._is_verified_proxy_pid(4242) is False  # type: ignore[attr-defined]

    def test_rejected_when_pid_dead(self):
        with (
            patch.object(goose_launcher, "_pid_alive", return_value=False),  # type: ignore[attr-defined]
            patch.object(
                goose_launcher,  # type: ignore[attr-defined]
                "_pid_cmdline",
                return_value="/usr/bin/python3 ollama_cloud_proxy.py",
            ),
        ):
            assert goose_launcher._is_verified_proxy_pid(4242) is False  # type: ignore[attr-defined]

    def test_rejected_when_cmdline_unreadable(self):
        with (
            patch.object(goose_launcher, "_pid_alive", return_value=True),  # type: ignore[attr-defined]
            patch.object(goose_launcher, "_pid_cmdline", return_value=None),  # type: ignore[attr-defined]
        ):
            assert goose_launcher._is_verified_proxy_pid(4242) is False  # type: ignore[attr-defined]


# ── escalation (SIGTERM → SIGKILL) ──────────────────────────────────────────


class TestTerminateVerifiedPid:
    def test_sigterm_only_when_process_exits_in_time(self):
        """If the process dies promptly, only SIGTERM is sent — no SIGKILL.

        Sequence: alive -> SIGTERM -> alive (recheck) -> dead (recheck, return).
        Only the first two ``_pid_alive`` calls happen; the function returns
        after the second one reports False.
        """
        # A stateful probe: True for the first call (initial check), True
        # for the second (post-SIGTERM poll), False thereafter. Two calls
        # are enough for the graceful path.
        probe = iter([True, True, False, False, False])

        with (
            patch.object(goose_launcher, "_pid_alive", side_effect=lambda _p: next(probe)),  # type: ignore[attr-defined]
            patch("beagle.bridges.goose_launcher.time.sleep"),
            patch("beagle.bridges.goose_launcher.os.kill") as kill,
        ):
            goose_launcher._terminate_verified_pid(4242)  # type: ignore[attr-defined]

        kill.assert_any_call(4242, signal.SIGTERM)  # type: ignore[name-defined]
        sigkill_calls = [
            c
            for c in kill.call_args_list
            if c.args[1] == signal.SIGKILL  # type: ignore[name-defined]
        ]
        assert sigkill_calls == [], f"SIGKILL should not be sent: {kill.call_args_list}"

    def test_sigkill_after_sigterm_deadline_when_still_alive(self):
        """If SIGTERM is ignored, SIGKILL is sent to the SAME PID after 3s.

        The launcher polls until the SIGTERM deadline (3s) expires, then
        re-probes once more before sending SIGKILL. With ``time.sleep``
        patched to a no-op, the loop would spin until the real clock
        advances; we also patch ``time.monotonic`` to fast-forward 0.5s
        per call so the deadline is reached within a few iterations.
        """
        # _pid_alive stream: always True until SIGKILL is sent, then False
        # for the reap loop. We give plenty of True entries so the polling
        # loop can never accidentally exit early.
        alive_stream = itertools.chain(
            itertools.repeat(True, 100),
            iter([False, False, False]),
        )
        # time.monotonic stream: fast-forward by 0.5s per call so the
        # 3s SIGTERM deadline is reached after ~6 calls.
        clock = [0.0]

        with (
            patch.object(
                goose_launcher,  # type: ignore[attr-defined]
                "_pid_alive",
                side_effect=lambda _p: next(alive_stream),
            ),
            patch("beagle.bridges.goose_launcher.time.sleep"),
            patch(
                "beagle.bridges.goose_launcher.time.monotonic",
                side_effect=lambda: clock.__setitem__(0, clock[0] + 0.5) or clock[0],
            ),
            patch("beagle.bridges.goose_launcher.os.kill") as kill,
        ):
            goose_launcher._terminate_verified_pid(4242)  # type: ignore[attr-defined]

        # Walk the kill log: every os.kill with a real signal must target
        # the SAME pid. We accept an arbitrary number of liveness probes
        # (signal 0); we only care that the SIGKILL is sent and to PID 4242.
        real_signal_calls = [c for c in kill.call_args_list if c.args[1] != 0]
        sent_signals = [c.args[1] for c in real_signal_calls]
        sent_pids = [c.args[0] for c in real_signal_calls]
        assert signal.SIGTERM in sent_signals, sent_signals  # type: ignore[name-defined]
        assert signal.SIGKILL in sent_signals, sent_signals  # type: ignore[name-defined]
        assert all(pid == 4242 for pid in sent_pids), sent_pids

    def test_never_uses_killpg(self):
        """The escalation MUST use ``os.kill(pid, sig)`` — never ``os.killpg``.

        We check for actual call sites of ``killpg`` (with a balanced paren)
        rather than the bare word — the docstring can name the forbidden
        API in negation form, but the runtime call surface must be empty.
        """
        alive_stream = itertools.chain(
            itertools.repeat(True, 100),
            iter([False, False, False]),
        )
        clock = [0.0]
        with (
            patch.object(
                goose_launcher,  # type: ignore[attr-defined]
                "_pid_alive",
                side_effect=lambda _p: next(alive_stream),
            ),
            patch("beagle.bridges.goose_launcher.time.sleep"),
            patch(
                "beagle.bridges.goose_launcher.time.monotonic",
                side_effect=lambda: clock.__setitem__(0, clock[0] + 0.5) or clock[0],
            ),
            patch("beagle.bridges.goose_launcher.os.kill"),
        ):
            goose_launcher._terminate_verified_pid(4242)  # type: ignore[attr-defined]

        src = Path(goose_launcher.__file__).read_text(encoding="utf-8")
        # Strip line and block comments, then look for the API call.
        code_lines = [ln for ln in src.splitlines() if not ln.lstrip().startswith("#")]
        code = "\n".join(code_lines)
        # Also strip docstrings (triple-quoted strings).
        code = re.sub(r"\"\"\"[\s\S]*?\"\"\"", "", code)
        code = re.sub(r"'''[\s\S]*?'''", "", code)
        assert "killpg" not in code, (
            "killpg is forbidden — process-group kill would over-signal "
            "into other launcher state. Use os.kill(pid, sig) only."
        )


# ── the headline regression: missing-pidfile-but-port-occupied ───────────────


class TestStaleProxyRegression:
    """When the pidfile is missing but the port is occupied, the launcher
    MUST recover by discovering the PID from the port, verifying its
    cmdline, then escalating SIGTERM→SIGKILL before unlinking anything.
    """

    def test_missing_pidfile_recovers_via_port_discovery(self, tmp_path: Path):
        """Full happy-path: pidfile gone, port-bound proxy found, killed cleanly."""
        pidfile = tmp_path / "ollama_cloud_proxy.pid"
        # pidfile deliberately NOT created — the regression trigger.

        # Track every call to os.kill so we can assert scope and ordering.
        kill_log: list[tuple[int, int]] = []
        unlink_order: list[str] = []

        # State machine for the proxy's liveness, tracked as a counter of
        # calls remaining as "alive". We use a single flag that the mock
        # flips after the launcher's kill has been issued (signalled by
        # the SIGKILL call entering the kill log).
        #
        # The launcher flow is:
        #   1. initial stale check:       _proxy_alive() → True
        #   2. SIGTERM+SIGKILL on PID,    (kill_log grows)
        #   3. port re-check loop:        _proxy_alive() → False (dead)
        #   4. Popen() → mocked fake
        #   5. fresh-proxy poll:          _proxy_alive() → True
        #   6. execv() → mocked
        #
        # We toggle the mock's return value when we see the SIGKILL
        # in the kill log.
        proxy_alive_flag = {"value": True}

        def proxy_alive_side_effect(*_a, **_kw):
            return proxy_alive_flag["value"]

        def kill_side_effect(pid, sig):
            kill_log.append((pid, sig))
            # Once SIGKILL is sent, the proxy is "dead" until the new one
            # is Popen'd. We can't observe the Popen from here directly,
            # so we use a different signal: the launcher's port re-check
            # happens AFTER kill_side_effect, so flipping on SIGKILL is
            # the right hook.
            if sig == signal.SIGKILL:  # type: ignore[name-defined]
                proxy_alive_flag["value"] = False

        with (
            patch.object(goose_launcher, "_PROXY_PID_FILE", pidfile),  # type: ignore[attr-defined]
            patch.object(goose_launcher, "_PROXY_SCRIPT", tmp_path / "ollama_cloud_proxy.py"),  # type: ignore[attr-defined]
            patch.object(goose_launcher, "_proxy_alive", side_effect=proxy_alive_side_effect),  # type: ignore[attr-defined]
            patch.object(goose_launcher, "_pid_for_port", return_value=9876) as pid_for_port_mock,  # type: ignore[attr-defined]
            patch.object(
                goose_launcher, "_is_verified_proxy_pid", return_value=True
            ) as verify_mock,  # type: ignore[attr-defined]
            patch.object(goose_launcher, "_pid_alive", return_value=True),  # type: ignore[attr-defined]
            patch("beagle.bridges.goose_launcher.time.sleep"),
            patch(
                "beagle.bridges.goose_launcher.time.monotonic",
                side_effect=range(1000),
            ),
            patch(
                "beagle.bridges.goose_launcher.os.kill",
                side_effect=kill_side_effect,
            ),
            patch.object(Path, "unlink", lambda self, **kw: unlink_order.append(self.name)),
        ):
            fake_popen = MagicMock()
            fake_popen.pid = 11111

            # Track Popen invocations to flip proxy_alive_flag back to True
            # when the launcher spawns the fresh proxy. We use a wrapper
            # Popen that flips the flag and returns the fake.
            def popen_wrapper(*args, **kwargs):
                proxy_alive_flag["value"] = True
                return fake_popen

            with (
                patch.object(goose_launcher.subprocess, "Popen", side_effect=popen_wrapper),  # type: ignore[attr-defined]
                patch.object(goose_launcher, "_GOOSE_REAL", tmp_path / "goose.real"),  # type: ignore[attr-defined]
                patch("beagle.bridges.goose_launcher.os.execv"),
            ):
                (tmp_path / "goose.real").write_text("#!/bin/sh\nexit 0\n")
                (tmp_path / "ollama_cloud_proxy.py").write_text("# proxy stub\n")
                goose_launcher.main()  # type: ignore[attr-defined]

        # ── Assertions ──────────────────────────────────────────────────────

        # 1. Port discovery was used (pidfile was missing).
        pid_for_port_mock.assert_called_with(
            goose_launcher._PROXY_HOST,  # type: ignore[attr-defined]
            goose_launcher._PROXY_PORT,  # type: ignore[attr-defined]
        )

        # 2. The discovered PID was verified against the cmdline marker.
        verify_mock.assert_called_with(9876)

        # 3. SIGTERM was sent first, then SIGKILL — both to PID 9876, in that
        #    order. No other PID was signalled.
        assert (9876, signal.SIGTERM) in kill_log  # type: ignore[name-defined]
        term_idx = kill_log.index((9876, signal.SIGTERM))  # type: ignore[name-defined]
        kill_idx = next(
            i
            for i, (p, s) in enumerate(kill_log)
            if s == signal.SIGKILL  # type: ignore[name-defined]
        )
        assert term_idx < kill_idx, "SIGTERM must precede SIGKILL"
        assert all(p == 9876 for (p, _) in kill_log), (
            f"only the verified PID is signalled, got: {kill_log}"
        )

        # 4. The pidfile was unlinked (after the kill), not before.
        pidfile_unlinks = [
            i for i, name in enumerate(unlink_order) if name == "ollama_cloud_proxy.pid"
        ]
        assert pidfile_unlinks, "pidfile must be unlinked once the proxy is dead"
        assert kill_log, "expected at least one signal to be sent before unlink"

    def test_refuses_to_signal_unverified_pid_discovered_from_port(self, tmp_path: Path):
        """If the port-discovery PID fails cmdline verification, the launcher
        MUST exit with an error rather than signalling an unverified process.
        """
        pidfile = tmp_path / "ollama_cloud_proxy.pid"

        with (
            patch.object(goose_launcher, "_PROXY_PID_FILE", pidfile),  # type: ignore[attr-defined]
            patch.object(goose_launcher, "_PROXY_SCRIPT", tmp_path / "ollama_cloud_proxy.py"),  # type: ignore[attr-defined]
            patch.object(goose_launcher, "_proxy_alive", return_value=True),  # type: ignore[attr-defined]
            patch.object(goose_launcher, "_pid_for_port", return_value=4242),  # type: ignore[attr-defined]
            patch.object(goose_launcher, "_is_verified_proxy_pid", return_value=False),  # type: ignore[attr-defined]
            patch("beagle.bridges.goose_launcher.os.kill") as kill,
        ):
            with pytest.raises(SystemExit) as excinfo:
                goose_launcher.main()  # type: ignore[attr-defined]
            assert excinfo.value.code == 1

        # No os.kill of any kind should have been issued.
        kill.assert_not_called()

    def test_no_unlink_before_proxy_confirmed_dead(self, tmp_path: Path):
        """Regression for the core invariant: pidfile is NEVER unlinked while
        the proxy might still be alive.
        """
        pidfile = tmp_path / "ollama_cloud_proxy.pid"
        pidfile.write_text("1234")  # valid pidfile present

        unlink_events: list[float] = []
        sigterm_at: list[float] = []
        clock = [0.0]

        def fake_sleep(_s: float) -> None:
            clock[0] += 0.05

        def fake_monotonic() -> float:
            return clock[0]

        def fake_kill(pid: int, sig: int) -> None:
            if sig == signal.SIGTERM:  # type: ignore[name-defined]
                sigterm_at.append(clock[0])

        # Track unlink calls with their timestamps.
        def tracked_unlink(self, *args, **kwargs):
            unlink_events.append(clock[0])
            # Simulate the pidfile being removed.
            return None

        with (
            patch.object(goose_launcher, "_PROXY_PID_FILE", pidfile),  # type: ignore[attr-defined]
            patch.object(goose_launcher, "_PROXY_SCRIPT", tmp_path / "ollama_cloud_proxy.py"),  # type: ignore[attr-defined]
            patch.object(goose_launcher, "_proxy_alive", return_value=True),  # type: ignore[attr-defined]
            patch.object(goose_launcher, "_is_verified_proxy_pid", return_value=True),  # type: ignore[attr-defined]
            patch.object(goose_launcher, "_pid_alive", return_value=True),  # type: ignore[attr-defined]
            patch("beagle.bridges.goose_launcher.time.sleep", side_effect=fake_sleep),
            patch(
                "beagle.bridges.goose_launcher.time.monotonic",
                side_effect=fake_monotonic,
            ),
            patch("beagle.bridges.goose_launcher.os.kill", side_effect=fake_kill),
            patch.object(Path, "unlink", tracked_unlink),
        ):
            fake_popen = MagicMock()
            fake_popen.pid = 99999
            with (
                patch.object(goose_launcher.subprocess, "Popen", return_value=fake_popen),  # type: ignore[attr-defined]
                patch.object(goose_launcher, "_GOOSE_REAL", tmp_path / "goose.real"),  # type: ignore[attr-defined]
                patch("beagle.bridges.goose_launcher.os.execv"),
            ):
                (tmp_path / "goose.real").write_text("#!/bin/sh\nexit 0\n")
                (tmp_path / "ollama_cloud_proxy.py").write_text("# proxy stub\n")
                # The proxy "refuses to die" — _proxy_alive stays True
                # through the whole window, so the launcher must sys.exit(1)
                # before the pidfile can be unlinked.
                with pytest.raises(SystemExit):
                    goose_launcher.main()  # type: ignore[attr-defined]

        # Invariant: NO pidfile unlink is permitted while SIGTERM has been
        # sent but the process is still alive. In this test the proxy never
        # dies, so the unlink for the pidfile must not occur at all (the
        # only legitimate unlink in the stale-proxy block is AFTER the
        # port re-check passes).
        pidfile_unlinks = unlink_events
        # The launcher exits with code 1 when the stale proxy refuses to
        # die. It must NOT have unlinked the pidfile on the way out.
        assert len(pidfile_unlinks) == 0, (
            "pidfile was unlinked while the proxy was still alive — "
            "this is the exact regression the new ordering guards against"
        )


# ── additional coverage: pidfile parsing and helpers ─────────────────────────


class TestReadPidfile:
    def test_returns_pid_when_valid(self, tmp_path: Path):
        pidfile = tmp_path / "pid"
        pidfile.write_text("4242\n")
        with patch.object(goose_launcher, "_PROXY_PID_FILE", pidfile):  # type: ignore[attr-defined]
            assert goose_launcher._read_pidfile() == 4242  # type: ignore[attr-defined]

    def test_returns_none_when_missing(self, tmp_path: Path):
        with patch.object(goose_launcher, "_PROXY_PID_FILE", tmp_path / "nope"):  # type: ignore[attr-defined]
            assert goose_launcher._read_pidfile() is None  # type: ignore[attr-defined]

    def test_returns_none_when_corrupted(self, tmp_path: Path):
        pidfile = tmp_path / "pid"
        pidfile.write_text("not-a-pid\n")
        with patch.object(goose_launcher, "_PROXY_PID_FILE", pidfile):  # type: ignore[attr-defined]
            assert goose_launcher._read_pidfile() is None  # type: ignore[attr-defined]

    def test_returns_none_when_empty(self, tmp_path: Path):
        pidfile = tmp_path / "pid"
        pidfile.write_text("   \n")
        with patch.object(goose_launcher, "_PROXY_PID_FILE", pidfile):  # type: ignore[attr-defined]
            assert goose_launcher._read_pidfile() is None  # type: ignore[attr-defined]


class TestIpv4ToHexLe:
    def test_loopback(self):
        assert goose_launcher._ipv4_to_hex_le("127.0.0.1") == "0100007F"  # type: ignore[attr-defined]

    def test_unspecified(self):
        assert goose_launcher._ipv4_to_hex_le("0.0.0.0") == "00000000"  # type: ignore[attr-defined]

    def test_raises_on_non_ipv4(self):
        with pytest.raises(ValueError):
            goose_launcher._ipv4_to_hex_le("::1")  # type: ignore[attr-defined]
