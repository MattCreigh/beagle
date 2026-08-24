"""SP-5: tests for steering/types + bridges/autogen/messages.

beagle-spotless-phase2, work package SP-5. The steering directive dataclass
and AutoGen message converters had no direct tests.
"""

from __future__ import annotations

from beagle.bridges.autogen.messages import (
    autogen_message_to_beagle_event,
    beagle_event_to_autogen_message,
)
from beagle.steering.types import SteeringDirective

# ── SteeringDirective ───────────────────────────────────────────────────────


def test_steering_directive_defaults() -> None:
    """A directive defaults to no guidance and source='file'."""
    d = SteeringDirective(workflow_id="wf")
    assert d.has_guidance is False
    assert d.priority_guidance == ""
    assert d.skip_nodes == []
    assert d.budget_override_usd is None
    assert d.source == "file"


def test_steering_directive_full() -> None:
    """All directive fields are settable."""
    d = SteeringDirective(
        workflow_id="wf",
        has_guidance=True,
        priority_guidance="be thorough",
        skip_nodes=["node2"],
        budget_override_usd=25.0,
        stop_after_node="node1",
        source="api",
    )
    assert d.has_guidance is True
    assert d.priority_guidance == "be thorough"
    assert d.budget_override_usd == 25.0
    assert d.stop_after_node == "node1"
    assert d.source == "api"


# ── Message converters ──────────────────────────────────────────────────────


def test_beagle_event_to_autogen_message() -> None:
    """A Beagle event becomes an AutoGen message dict."""
    ev = {"role": "user", "content": "hello", "agent_name": "user-agent"}
    msg = beagle_event_to_autogen_message(ev)
    assert msg["role"] == "user"
    assert msg["content"] == "hello"
    assert msg["source"] == "user-agent"


def test_beagle_event_to_autogen_defaults() -> None:
    """Missing event keys default to assistant role and system source."""
    msg = beagle_event_to_autogen_message({})
    assert msg["role"] == "assistant"
    assert msg["content"] == ""
    assert msg["source"] == "system"


def test_autogen_message_to_beagle_event() -> None:
    """An AutoGen message becomes a Beagle event dict."""
    msg = {"role": "user", "content": "hi", "source": "assistant-1"}
    ev = autogen_message_to_beagle_event(msg)
    assert ev["event_type"] == "agent_message"
    assert ev["role"] == "user"
    assert ev["content"] == "hi"
    assert ev["agent_name"] == "assistant-1"


def test_autogen_message_to_beagle_defaults() -> None:
    """Missing message keys default to user role and unknown agent."""
    ev = autogen_message_to_beagle_event({})
    assert ev["role"] == "user"
    assert ev["content"] == ""
    assert ev["agent_name"] == "unknown"
