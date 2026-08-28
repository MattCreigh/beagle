# Copyright (c) 2026 Matt Creigh. Released under the MIT License.
# SPDX-License-Identifier: MIT
"""Tests for the app's signal framing contract via beacon_lib.codec.

LANTERN P4 cutover: beagle.beacon.intents is gone; the envelope lives in
beacon_lib.codec. These tests pin the app-visible semantics (D-05, I-2).

See plans/beagle-beacon-coordination.xml WP-3, decision D-05, invariant I-2.
"""

from __future__ import annotations

import json
import uuid

import pytest

# orpheus / beacon_lib are the optional proprietary ring transport; the
# signal-framing tests exercise it directly and must skip when absent.
pytest.importorskip("beacon_lib.codec")
from beacon_lib.codec import SIGNAL_OPS, decode_signal, encode_signal  # noqa: E402


class TestEncodeDecodeRoundTrip:
    def test_round_trips_every_ring_op(self) -> None:
        agent_id = str(uuid.uuid4())
        for op in SIGNAL_OPS:
            encoded = encode_signal(op, agent_id, {"k": "v"}, slot_size=4096)
            decoded_op, decoded_agent, decoded_args = decode_signal(encoded)
            assert decoded_op == op
            assert decoded_agent == agent_id
            assert decoded_args == {"k": "v"}

    def test_envelope_shape_matches_the_contract(self) -> None:
        agent_id = str(uuid.uuid4())
        encoded = encode_signal("heartbeat", agent_id, {"phase": "writing"}, slot_size=4096)
        envelope = json.loads(encoded.decode("utf-8"))
        assert set(envelope.keys()) == {"v", "op", "agent_id", "ts", "args"}
        assert envelope["v"] == 1
        assert envelope["op"] == "heartbeat"
        assert envelope["agent_id"] == agent_id
        assert envelope["args"] == {"phase": "writing"}


class TestOpAllowlist:
    def test_lock_file_is_rejected(self) -> None:
        """I-2: a lock ACQUIRE is synchronous-only, never a ring op."""
        with pytest.raises(ValueError, match="lock_file"):
            encode_signal("lock_file", str(uuid.uuid4()), {}, slot_size=4096)

    def test_arbitrary_unknown_op_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="not a signal operation"):
            encode_signal("delete_everything", str(uuid.uuid4()), {}, slot_size=4096)

    def test_every_ring_op_name_matches_the_recipe_contract(self) -> None:
        assert (
            frozenset(
                {
                    "heartbeat",
                    "event",
                    "commit",
                    "checkpoint",
                    "plan_status",
                    "unlock_file",
                    "touch_file",
                }
            )
            == SIGNAL_OPS
        )


class TestSizeCap:
    def test_oversized_payload_raises_and_names_the_socket_fallback(self) -> None:
        huge_args = {"blob": "x" * 5000}
        with pytest.raises(ValueError, match="synchronous RPC") as exc_info:
            encode_signal("event", str(uuid.uuid4()), huge_args, slot_size=4096)
        assert "rpc" in str(exc_info.value).lower()  # lib names the synchronous-RPC fallback

    def test_payload_at_exactly_the_slot_size_boundary(self) -> None:
        agent_id = str(uuid.uuid4())
        # Find the largest 'x'*N payload that still fits, then confirm N+1 doesn't.
        slot_size = 200
        low, high = 0, slot_size
        while low < high:
            mid = (low + high + 1) // 2
            try:
                encode_signal("event", agent_id, {"b": "x" * mid}, slot_size=slot_size)
                low = mid
            except ValueError:
                high = mid - 1
        # low bytes fits; low+1 must not.
        encode_signal("event", agent_id, {"b": "x" * low}, slot_size=slot_size)
        with pytest.raises(ValueError, match="exceeds"):
            encode_signal("event", agent_id, {"b": "x" * (low + 1)}, slot_size=slot_size)

    def test_never_truncates_silently(self) -> None:
        """An oversized payload must raise, never return a shortened envelope."""
        with pytest.raises(ValueError):
            result = encode_signal("event", str(uuid.uuid4()), {"blob": "x" * 5000}, slot_size=4096)
            # If encode_signal ever returns instead of raising, this must fail
            # loudly rather than silently accept a truncated envelope.
            pytest.fail(f"expected ValueError, got {len(result)}-byte result instead")


class TestMalformedSlots:
    def test_non_utf8_bytes_raise(self) -> None:
        with pytest.raises(ValueError, match="UTF-8"):
            decode_signal(b"\xff\xfe\x00\x01")

    def test_non_json_bytes_raise(self) -> None:
        with pytest.raises(ValueError, match="JSON"):
            decode_signal(b"not json at all")

    def test_json_array_instead_of_object_raises(self) -> None:
        with pytest.raises(ValueError, match="object"):
            decode_signal(b'["a", "b"]')

    def test_wrong_envelope_version_raises(self) -> None:
        bad = json.dumps({"v": 99, "op": "heartbeat", "agent_id": "x", "ts": "x", "args": {}})
        with pytest.raises(ValueError, match="version"):
            decode_signal(bad.encode("utf-8"))

    def test_missing_field_raises(self) -> None:
        bad = json.dumps({"v": 1, "op": "heartbeat", "agent_id": "x"})  # no ts, no args
        with pytest.raises(ValueError, match="missing"):
            decode_signal(bad.encode("utf-8"))

    def test_decoded_op_outside_allowlist_raises(self) -> None:
        bad = json.dumps({"v": 1, "op": "lock_file", "agent_id": "x", "ts": "x", "args": {}})
        with pytest.raises(ValueError, match="not a signal operation"):
            decode_signal(bad.encode("utf-8"))

    def test_args_not_an_object_raises(self) -> None:
        bad = json.dumps({"v": 1, "op": "heartbeat", "agent_id": "x", "ts": "x", "args": "nope"})
        with pytest.raises(ValueError, match="object"):
            decode_signal(bad.encode("utf-8"))
