"""Webhook and callback integration for Goose workflows.

Provides webhook delivery for workflow events with retry logic,
HMAC signatures, and support for common platforms.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

logger = logging.getLogger("Beagle.webhooks")


class WebhookEvent(StrEnum):
    """Webhook event types."""

    WORKFLOW_STARTED = "workflow.started"
    WORKFLOW_COMPLETED = "workflow.completed"
    WORKFLOW_FAILED = "workflow.failed"
    NODE_COMPLETED = "node.completed"
    NODE_FAILED = "node.failed"
    BUDGET_WARNING = "budget.warning"
    BUDGET_EXCEEDED = "budget.exceeded"
    APPROVAL_REQUIRED = "approval.required"


@dataclass
class WebhookConfig:
    """Configuration for a webhook endpoint."""

    url: str
    secret: str = ""
    events: list[str] = field(default_factory=list)  # Empty = all events
    timeout_seconds: int = 10
    max_retries: int = 3
    retry_delay: float = 1.0
    enabled: bool = True

    def __repr__(self) -> str:
        """Mask secret in repr to prevent credential leaks in logs."""
        masked = self.secret[:4] + "****" if len(self.secret) > 4 else "****"
        return (
            f"WebhookConfig(url={self.url!r}, secret={masked!r}, "
            f"events={self.events!r}, enabled={self.enabled})"
        )


@dataclass
class WebhookDelivery:
    """Represents a webhook delivery attempt."""

    webhook_id: str
    event: str
    payload: dict[str, Any]
    attempt: int = 0
    success: bool = False
    response_code: int | None = None
    error: str = ""
    timestamp: float = field(default_factory=time.time)


def compute_signature(payload: str, secret: str) -> str:
    """Compute HMAC-SHA256 signature.

    Args:
        payload: JSON payload string
        secret: Webhook secret

    Returns:
        Hex-encoded signature with prefix

    """
    if not secret or not str(secret).strip() or len(str(secret).strip()) < 8:
        raise ValueError(
            "Webhook cryptographic secret is missing or insufficiently complex. "
            "Signature generation aborted."
        )
    signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"sha256={signature}"


def verify_signature(payload: str, secret: str, received_signature: str) -> bool:
    """Verify HMAC-SHA256 signature using constant-time comparison.

    Args:
        payload: JSON payload string
        secret: Webhook secret
        received_signature: Signature to verify

    Returns:
        True if signature is valid

    """
    try:
        expected = compute_signature(payload, secret)
    except ValueError:
        return False
    if not expected or not received_signature:
        return False
    return hmac.compare_digest(expected, received_signature)


class WebhookManager:
    """Manages webhook registrations and deliveries."""

    MAX_DELIVERY_HISTORY = 10000

    def __init__(self):
        """Initialize webhook manager."""
        self._webhooks: dict[str, WebhookConfig] = {}
        self._deliveries: list[WebhookDelivery] = []

    def register(
        self,
        webhook_id: str,
        config: WebhookConfig,
    ) -> None:
        """Register a webhook.

        Args:
            webhook_id: Unique identifier for the webhook
            config: Webhook configuration

        """
        self._webhooks[webhook_id] = config
        logger.info(f"[WEBHOOK] Registered: {webhook_id} -> {config.url}")

    def unregister(self, webhook_id: str) -> bool:
        """Unregister a webhook.

        Args:
            webhook_id: Webhook identifier

        Returns:
            True if removed, False if not found

        """
        if webhook_id in self._webhooks:
            del self._webhooks[webhook_id]
            logger.info(f"[WEBHOOK] Unregistered: {webhook_id}")
            return True
        return False

    def should_deliver(
        self,
        config: WebhookConfig,
        event: str,
    ) -> bool:
        """Check if event should be delivered to webhook.

        Args:
            config: Webhook configuration
            event: Event type

        Returns:
            True if should deliver

        """
        if not config.enabled:
            return False
        if not config.events:  # Empty = all events
            return True
        return event in config.events

    async def deliver(
        self,
        event: str,
        payload: dict[str, Any],
    ) -> list[WebhookDelivery]:
        """Deliver event to all matching webhooks.

        Args:
            event: Event type
            payload: Event payload

        Returns:
            List of delivery results

        """
        deliveries = []

        # Snapshot dict to avoid RuntimeError if register/unregister called concurrently
        for webhook_id, config in list(self._webhooks.items()):
            if not self.should_deliver(config, event):
                continue

            delivery = await self._deliver_to_webhook(webhook_id, config, event, payload)
            deliveries.append(delivery)
            self._deliveries.append(delivery)

        # Cap delivery history to prevent unbounded memory growth
        if len(self._deliveries) > self.MAX_DELIVERY_HISTORY:
            self._deliveries = self._deliveries[-self.MAX_DELIVERY_HISTORY :]

        return deliveries

    async def _deliver_to_webhook(
        self,
        webhook_id: str,
        config: WebhookConfig,
        event: str,
        payload: dict[str, Any],
    ) -> WebhookDelivery:
        """Deliver to a single webhook with retries.

        Args:
            webhook_id: Webhook identifier
            config: Webhook configuration
            event: Event type
            payload: Event payload

        Returns:
            Delivery result

        """
        # Build the full payload
        full_payload = {
            "event": event,
            "timestamp": time.time(),
            "data": payload,
        }
        json_payload = json.dumps(full_payload, default=str)

        delivery = WebhookDelivery(
            webhook_id=webhook_id,
            event=event,
            payload=full_payload,
        )

        # Check for aiohttp availability before retry loop
        try:
            import aiohttp
        except ImportError:
            logger.error(
                f"[WEBHOOK] aiohttp not installed — cannot deliver to {webhook_id}. "
                "Install with: pip install aiohttp"
            )
            delivery.success = False
            delivery.error = "aiohttp not installed"
            return delivery

        for attempt in range(1, config.max_retries + 1):
            delivery.attempt = attempt

            try:
                headers = {
                    "Content-Type": "application/json",
                    "User-Agent": "Beagle-Webhook/1.0",
                    "X-Webhook-Event": event,
                }

                # Add signature if secret configured
                try:
                    signature = compute_signature(json_payload, config.secret)
                    headers["X-Webhook-Signature"] = signature
                except ValueError:
                    logger.warning(
                        f"[WEBHOOK] Skipping signature for {webhook_id}: invalid/missing secret"
                    )

                async with (
                    aiohttp.ClientSession() as session,
                    session.post(
                        config.url,
                        data=json_payload,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=config.timeout_seconds),
                    ) as response,
                ):
                    delivery.response_code = response.status
                    delivery.success = 200 <= response.status < 300

                    if delivery.success:
                        logger.info(f"[WEBHOOK] Delivered {event} to {webhook_id}")
                        return delivery
                    else:
                        delivery.error = f"HTTP {response.status}"

            except TimeoutError:
                delivery.error = "Timeout"
            except ConnectionError as e:
                delivery.error = str(e)

            # Retry with exponential backoff
            if attempt < config.max_retries:
                delay = config.retry_delay * (2 ** (attempt - 1))
                logger.warning(
                    f"[WEBHOOK] Retry {attempt}/{config.max_retries} for "
                    f"{webhook_id} in {delay:.1f}s: {delivery.error}"
                )
                await asyncio.sleep(delay)

        logger.error(
            f"[WEBHOOK] Failed to deliver {event} to {webhook_id} "
            f"after {config.max_retries} attempts: {delivery.error}"
        )
        return delivery

    def get_recent_deliveries(
        self,
        limit: int = 100,
    ) -> list[WebhookDelivery]:
        """Get recent deliveries.

        Args:
            limit: Maximum number to return

        Returns:
            Recent deliveries, newest first

        """
        return sorted(
            self._deliveries[-limit:],
            key=lambda d: d.timestamp,
            reverse=True,
        )

    def stats(self) -> dict[str, Any]:
        """Get webhook statistics.

        Returns:
            Statistics dict

        """
        total = len(self._deliveries)
        successful = sum(1 for d in self._deliveries if d.success)
        failed = total - successful

        return {
            "webhooks_registered": len(self._webhooks),
            "deliveries": {
                "total": total,
                "successful": successful,
                "failed": failed,
                "success_rate": round(successful / max(1, total) * 100, 1),
            },
        }


# Platform-specific webhook formatters


def format_slack_payload(
    event: str,
    data: dict[str, Any],
) -> dict[str, Any]:
    """Format payload for Slack webhook.

    Args:
        event: Event type
        data: Event data

    Returns:
        Slack-formatted payload

    """
    emoji = {
        "workflow.started": ":rocket:",
        "workflow.completed": ":white_check_mark:",
        "workflow.failed": ":x:",
        "node.completed": ":ballot_box_with_check:",
        "node.failed": ":warning:",
        "budget.warning": ":moneybag:",
        "budget.exceeded": ":no_entry:",
        "approval.required": ":question:",
    }.get(event, ":information_source:")

    text = f"{emoji} *{event}*"
    if "workflow_id" in data:
        text += f" | Workflow: `{data['workflow_id']}`"
    if "node_name" in data:
        text += f" | Node: `{data['node_name']}`"
    if "error" in data:
        text += f"\n> {data['error']}"

    return {
        "text": text,
        "attachments": [
            {
                "fields": [
                    {"title": k, "value": str(v)[:100], "short": True}
                    for k, v in data.items()
                    if k not in ("workflow_id", "node_name", "error")
                ][:10]  # Max 10 fields
            }
        ],
    }


def format_discord_payload(
    event: str,
    data: dict[str, Any],
) -> dict[str, Any]:
    """Format payload for Discord webhook.

    Args:
        event: Event type
        data: Event data

    Returns:
        Discord-formatted payload

    """
    color = {
        "workflow.started": 0x3498DB,  # Blue
        "workflow.completed": 0x2ECC71,  # Green
        "workflow.failed": 0xE74C3C,  # Red
        "node.completed": 0x9B59B6,  # Purple
        "node.failed": 0xF39C12,  # Orange
        "budget.warning": 0xF1C40F,  # Yellow
        "budget.exceeded": 0xE74C3C,  # Red
        "approval.required": 0x95A5A6,  # Gray
    }.get(event, 0x95A5A6)

    embed = {
        "title": event,
        "color": color,
        "fields": [],
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    for k, v in list(data.items())[:10]:
        embed["fields"].append(  # type: ignore[attr-defined]
            {
                "name": k,
                "value": str(v)[:100],
                "inline": True,
            }
        )

    return {"embeds": [embed]}


# Global webhook manager
_webhook_manager: WebhookManager | None = None


def get_webhook_manager() -> WebhookManager:
    """Get or create the global webhook manager.

    Returns:
        WebhookManager instance

    """
    global _webhook_manager
    if _webhook_manager is None:
        _webhook_manager = WebhookManager()
    return _webhook_manager


async def emit_event(
    event: str,
    **data: Any,
) -> list[WebhookDelivery]:
    """Emit a webhook event.

    Args:
        event: Event type
        **data: Event data

    Returns:
        List of delivery results

    """
    manager = get_webhook_manager()
    return await manager.deliver(event, data)


if __name__ == "__main__":

    async def demo():
        """Demo webhook functionality."""
        manager = WebhookManager()

        # Register test webhook
        manager.register(
            "test",
            WebhookConfig(
                url="https://httpbin.org/post",
                events=["workflow.completed"],
            ),
        )

        # Deliver event
        deliveries = await manager.deliver(
            "workflow.completed",
            {
                "workflow_id": "demo123",
                "total_tokens": 1500,
                "total_cost_usd": 0.003,
            },
        )

        logger.info(f"Deliveries: {len(deliveries)}")
        for d in deliveries:
            logger.info(f"  {d.webhook_id}: {'OK' if d.success else d.error}")

        logger.info(f"\nStats: {json.dumps(manager.stats(), indent=2)}")

    asyncio.run(demo())
