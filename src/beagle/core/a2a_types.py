"""Shared A2A (Agent-to-Agent) type definitions.

Provides the canonical base types used by both the core task/RPC layer
(``core.a2a_protocol``) and the enterprise transport layer
(``enterprise.a2a_protocol``).  Centralizing these types eliminates the
dual-definition problem where ``MessageType`` and agent descriptor
dataclasses diverged between v12.3 (enterprise) and v13.4 (core).

Import hierarchy::

    core.a2a_types          ← shared enums, dataclasses, constants
        ↑               ↑
    core.a2a_protocol   enterprise.a2a_protocol
    (Task/RPC, OIDC,    (Connection/Transport,
     RBAC, Gateway)      AF_VSOCK, Streaming)
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum, StrEnum
from typing import Any

# ── Protocol constants ───────────────────────────────────────────────────────

A2A_PROTOCOL_VERSION = "1.0.0"


# ── Enums ────────────────────────────────────────────────────────────────────


class CoreMessageType(StrEnum):
    """Core A2A message types (task/RPC layer)."""

    REQUEST = "request"
    RESPONSE = "response"
    STREAMING_RESPONSE = "streaming_response"
    TASK_UPDATE = "task_update"
    ERROR = "error"


class TransportMessageType(Enum):
    """Extended message types for the transport/connection layer.

    Superset of CoreMessageType — includes handshake, streaming,
    coordination, and capability discovery messages used by the
    enterprise ``A2AProtocol`` connection manager.
    """

    # Handshake
    HELLO = "hello"
    HELLO_ACK = "hello_ack"
    GOODBYE = "goodbye"

    # Core request-response (mirrors CoreMessageType for transport compat)
    REQUEST = "request"
    RESPONSE = "response"
    ERROR = "error"

    # Streaming
    STREAM_START = "stream_start"
    STREAM_DATA = "stream_data"
    STREAM_END = "stream_end"

    # Events
    EVENT = "event"
    HEARTBEAT = "heartbeat"

    # Coordination
    LOCK_REQUEST = "lock_request"
    LOCK_GRANTED = "lock_granted"
    LOCK_RELEASE = "lock_release"

    # Capability Discovery
    CAPABILITY_QUERY = "capability_query"
    CAPABILITY_RESPONSE = "capability_response"


class MessagePriority(Enum):
    """Priority levels for A2A messages."""

    LOW = 0
    NORMAL = 1
    HIGH = 2
    CRITICAL = 3


class TaskStatus(StrEnum):
    """Task status codes."""

    PENDING = "pending"
    WORKING = "working"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


# ── Dataclasses ──────────────────────────────────────────────────────────────


@dataclass
class AgentDescriptor:
    """Unified agent descriptor — the canonical representation.

    Merges fields from the core ``AgentCard`` (name/version/skills/auth)
    and the enterprise ``AgentEndpoint`` (agent_type/protocol_version/url).
    Both modules re-export aliases that map to this class.
    """

    # Identity
    agent_id: str = ""
    name: str = ""
    version: str = A2A_PROTOCOL_VERSION
    description: str = ""

    # Capabilities
    agent_type: str = "worker"  # "orchestrator", "worker", "specialist"
    capabilities: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)

    # Network
    endpoint: str = ""
    endpoint_url: str | None = None  # Enterprise transport endpoint
    protocol_version: str = A2A_PROTOCOL_VERSION

    # Auth & metadata
    authentication: dict[str, str] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentDescriptor:
        """Create from dict, accepting both core and enterprise field names."""
        return cls(**{k: v for k, v in data.items() if k in cls.__annotations__})


@dataclass
class A2AMessage:
    """A2A protocol message (JSON-RPC 2.0 style)."""

    jsonrpc: str = "2.0"
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    method: str | None = None
    params: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary, excluding None values."""
        result = {"jsonrpc": self.jsonrpc, "id": self.id}
        if self.method:
            result["method"] = self.method
        if self.params is not None:
            result["params"] = self.params  # type: ignore[assignment]
        if self.result is not None:
            result["result"] = self.result  # type: ignore[assignment]
        if self.error is not None:
            result["error"] = self.error  # type: ignore[assignment]
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> A2AMessage:
        """Create from dictionary."""
        return cls(
            jsonrpc=data.get("jsonrpc", "2.0"),
            id=data.get("id", str(uuid.uuid4())),
            method=data.get("method"),
            params=data.get("params"),
            result=data.get("result"),
            error=data.get("error"),
        )


@dataclass
class MessageEnvelope:
    """Rich message envelope for the transport layer.

    Extends A2AMessage with sender/recipient routing, TTL, retry,
    and priority — used by ``A2AProtocol`` (enterprise) for stateful
    connection-based communication.
    """

    # Identity
    message_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    correlation_id: str | None = None
    conversation_id: str | None = None

    # Routing
    sender: AgentDescriptor | None = None
    recipient: AgentDescriptor | None = None

    # Message
    message_type: TransportMessageType = TransportMessageType.REQUEST
    priority: MessagePriority = MessagePriority.NORMAL
    payload: dict[str, Any] = field(default_factory=dict)

    # Metadata
    timestamp: float = field(default_factory=time.time)
    ttl_seconds: int = 300
    retry_count: int = 0
    max_retries: int = 3

    def to_json(self) -> str:
        return json.dumps(
            {
                "message_id": self.message_id,
                "correlation_id": self.correlation_id,
                "conversation_id": self.conversation_id,
                "sender": self.sender.to_dict() if self.sender else None,
                "recipient": self.recipient.to_dict() if self.recipient else None,
                "message_type": self.message_type.value,
                "priority": self.priority.value,
                "payload": self.payload,
                "timestamp": self.timestamp,
                "ttl_seconds": self.ttl_seconds,
                "retry_count": self.retry_count,
                "max_retries": self.max_retries,
            }
        )

    @classmethod
    def from_json(cls, json_str: str) -> MessageEnvelope:
        data = json.loads(json_str)
        return cls(
            message_id=data["message_id"],
            correlation_id=data.get("correlation_id"),
            conversation_id=data.get("conversation_id"),
            sender=(AgentDescriptor.from_dict(data["sender"]) if data.get("sender") else None),
            recipient=(
                AgentDescriptor.from_dict(data["recipient"]) if data.get("recipient") else None
            ),
            message_type=TransportMessageType(data["message_type"]),
            priority=MessagePriority(data["priority"]),
            payload=data.get("payload", {}),
            timestamp=data["timestamp"],
            ttl_seconds=data.get("ttl_seconds", 300),
            retry_count=data.get("retry_count", 0),
            max_retries=data.get("max_retries", 3),
        )

    def create_response(self, payload: dict, error: bool = False) -> MessageEnvelope:
        """Create a response envelope for this message."""
        return MessageEnvelope(
            correlation_id=self.message_id,
            conversation_id=self.conversation_id,
            sender=self.recipient,
            recipient=self.sender,
            message_type=(TransportMessageType.ERROR if error else TransportMessageType.RESPONSE),
            priority=self.priority,
            payload=payload,
            ttl_seconds=self.ttl_seconds,
        )

    def is_expired(self) -> bool:
        """Check if message has exceeded TTL."""
        return time.time() > self.timestamp + self.ttl_seconds


@dataclass
class Task:
    """Represents an A2A task with lifecycle tracking."""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    status: TaskStatus = TaskStatus.PENDING
    agent_id: str | None = None
    input_data: dict[str, Any] | None = None
    output_data: dict[str, Any] | None = None
    error: str | None = None
    created_at: str = field(
        default_factory=lambda: (
            __import__("datetime").datetime.now(__import__("datetime").UTC).isoformat()
        )
    )
    updated_at: str = field(
        default_factory=lambda: (
            __import__("datetime").datetime.now(__import__("datetime").UTC).isoformat()
        )
    )
    metadata: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value if isinstance(self.status, TaskStatus) else self.status
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Task:
        """Build a Task from a dict produced by ``to_dict`` (or a server payload)."""
        status = data.get("status", TaskStatus.PENDING.value)
        try:
            status = TaskStatus(status) if isinstance(status, str) else status
        except ValueError:
            status = TaskStatus.PENDING
        return cls(
            id=data.get("id", str(uuid.uuid4())),
            status=status,
            agent_id=data.get("agent_id") or data.get("agentId"),
            input_data=data.get("input_data") or data.get("input"),
            output_data=data.get("output_data") or data.get("output"),
            error=data.get("error"),
            created_at=data.get("created_at") or data.get("createdAt", ""),
            updated_at=data.get("updated_at") or data.get("updatedAt", ""),
            metadata=data.get("metadata"),
        )


@dataclass
class A2AConnection:
    """Active A2A connection (transport layer)."""

    endpoint: AgentDescriptor
    connected_at: float = field(default_factory=time.time)
    last_heartbeat: float = field(default_factory=time.time)
    message_count: int = 0
    bytes_sent: int = 0
    bytes_received: int = 0

    def is_healthy(self, timeout_seconds: int = 30) -> bool:
        """Check if connection is healthy."""
        return (
            # wall-clock-ok: compares against a persisted timestamp
            time.time() - self.last_heartbeat < timeout_seconds
        )


# ── Backward-compatibility aliases ───────────────────────────────────────────
# These allow existing code to import the old names without breaking.

# core/a2a_protocol.py used these names:
AgentCard = AgentDescriptor  # core alias
MessageType = CoreMessageType  # core alias

# enterprise/a2a_protocol.py used these names:
AgentEndpoint = AgentDescriptor  # enterprise alias
