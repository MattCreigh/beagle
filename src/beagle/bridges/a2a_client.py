"""A2A Client Bridge — Outbound A2A calls from Beagle workflows.

Phase 5 of the LangChain Ecosystem Compatibility Plan.
Allows Beagle workflows to call remote A2A agents (in other frameworks
like CrewAI, AutoGen, or other Beagle instances) as workflow nodes.

Used when YAML phase specifies executor="a2a_remote":
  - name: "call_remote_researcher"
    executor: "a2a_remote"
    agent_url: "https://crewai.example.com:8420/a2a"
    agent_name: "researcher"
    input_mapping:
      query: "{{state.query}}"
    output_key: "remote_research"
    timeout: 120
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from typing import Any

import httpx

from .config import get_a2a_config

logger = logging.getLogger("Beagle.bridges.a2a_client")


class A2AClientBridge:
    """Client for calling remote A2A agents from Beagle workflows.

    Handles signing, timeout, retry, and concurrency for
    outbound A2A calls.

    Usage:
        client = A2AClientBridge()
        result = await client.call_remote_agent(
            agent_url="https://remote:8420/a2a",
            agent_name="researcher",
            task_input={"query": "Analyze auth module"},
        )
    """

    def __init__(self) -> None:
        self.config = get_a2a_config()
        self._semaphore = asyncio.Semaphore(self.config.max_concurrent_tasks)
        self._discovery_cache: dict[str, tuple[float, list]] = {}
        # v13.22.4: cache for the loaded Ed25519 SigningKey so we
        # don't re-read + re-parse the key file on every request.
        self._signing_key: Any | None = None
        # v13.22.4: discovery-cache LRU + size cap to prevent
        # unbounded growth across many distinct peer URLs.
        self._discovery_cache_max = 256
        # Module-level reusable HTTP client (Phase 6 edge-inference
        # optimisation). Avoids per-call TCP+TLS handshake + connection
        # pool re-creation; with three concurrent LLM ensemble members
        # this saves ~200-400 ms of overhead per call. Lazily
        # initialised so the bridge is cheap to construct even when
        # no HTTP call is ever made.
        self._http: httpx.AsyncClient | None = None

    async def _get_http(self) -> httpx.AsyncClient:
        """Return the shared ``httpx.AsyncClient``, creating it on first use.

        Uses connection-pool defaults sized for the edge-inference
        workload (max 10 keepalive connections, max 20 total). Tune
        via ``httpx.Limits`` if the ensemble grows.
        """
        if self._http is None or self._http.is_closed:
            from ..core.transports import active

            self._http = active().async_client(
                timeout=httpx.Timeout(30.0, connect=5.0),
                limits=httpx.Limits(
                    max_keepalive_connections=10,
                    max_connections=20,
                    keepalive_expiry=30.0,
                ),
                http2=False,  # HTTP/2 adds a round-trip; HTTP/1.1 is faster on a single hop
            )
        return self._http

    async def aclose(self) -> None:
        """Close the shared HTTP client. Call at process shutdown."""
        if self._http is not None and not self._http.is_closed:
            await self._http.aclose()
            self._http = None

    def _get_signing_key(self) -> Any:
        """Load the Ed25519 signing key for outbound requests.

        SECURITY (DevSecOps): Ed25519-only — no HMAC fallback.
        If nacl is unavailable, raises RuntimeError (fail-closed).

        v13.22.4: cache the loaded SigningKey on the instance. The
        previous implementation re-read + re-parsed the key file on
        every request (and TWICE per request: once in _sign_request,
        once for peer_public_key). Under ensemble load this is a
        measurable latency tax and a needless disk-touch per call.
        """
        # Cache hit: skip disk I/O and key parse.
        if self._signing_key is not None:
            return self._signing_key

        try:
            from pathlib import Path

            import nacl.signing

            key_path = Path(self.config.key_path).expanduser() / "signing.key"
            if key_path.exists():
                seed = key_path.read_bytes()
                self._signing_key = nacl.signing.SigningKey(seed)
                return self._signing_key
        except ImportError:
            raise RuntimeError(
                "PyNaCl is REQUIRED for A2A signing (Ed25519). "
                "HMAC fallback has been removed for security — "
                "cryptographic downgrade attacks are prevented. "
                "Install with: pip install pynacl"
            ) from None
        except Exception as exc:  # broad catch intentional
            logger.error(f"Could not load Ed25519 signing key: {exc}")
            raise RuntimeError(
                f"Ed25519 signing key initialization failed: {exc}. "
                "A2A signing will not proceed without valid Ed25519 keys."
            ) from exc
        # No key file found — fail-closed to prevent unsigned requests
        try:
            import nacl.signing
        except ImportError:
            raise RuntimeError(
                "PyNaCl is REQUIRED for A2A signing (Ed25519). Install with: pip install pynacl"
            ) from None
        raise RuntimeError(
            f"Ed25519 signing key not found at {key_path}. "
            'Generate with: python3 -c "from nacl.signing import SigningKey; '
            "open(key_path, 'wb').write(SigningKey.generate().encode())\""
        )

    def _sign_request(self, payload: bytes) -> str:
        """Sign an outbound A2A request using Ed25519.

        SECURITY (DevSecOps): HMAC fallback removed. If Ed25519 signing
        fails, raises RuntimeError rather than silently degrading to a
        weaker cryptographic scheme.
        """
        key = self._get_signing_key()
        if key is None:
            raise RuntimeError(
                "No Ed25519 signing key available — cannot sign A2A request. "
                "Generate a key pair with: python -m beagle.bridges.a2a_client --gen-keys"
            )
        try:
            import nacl.signing

            if isinstance(key, nacl.signing.SigningKey):
                signed = key.sign(payload)
                return signed.signature.hex()
        except Exception as exc:  # broad catch intentional
            raise RuntimeError(f"Ed25519 signing failed: {exc}") from exc
        raise RuntimeError("Ed25519 signing failed — unexpected key type")

    async def discover_remote_agents(self, agent_url: str) -> list[dict[str, Any]]:
        """Discover available agents at a remote A2A endpoint.

        Results are cached for discovery_cache_ttl_seconds.

        Args:
            agent_url: Base URL of the remote A2A server.

        Returns:
            List of AgentCard dicts.

        """
        # Check cache
        now = time.time()
        if agent_url in self._discovery_cache:
            ts, cards = self._discovery_cache[agent_url]
            if now - ts < self.config.discovery_cache_ttl_seconds:
                return cards

        discover_url = f"{agent_url.rstrip('/')}/discover"

        client = await self._get_http()
        try:
            resp = await client.post(discover_url, timeout=httpx.Timeout(30.0, connect=5.0))
            resp.raise_for_status()
            cards = resp.json()

            self._discovery_cache[agent_url] = (now, cards)
            # v13.22.4: evict oldest entries when the cache grows past
            # _discovery_cache_max. Without this, a long-running process
            # calling many distinct URLs leaks memory unboundedly.
            if len(self._discovery_cache) > self._discovery_cache_max:
                # Sort by timestamp; drop the oldest quarter.
                by_ts = sorted(
                    self._discovery_cache.items(),
                    key=lambda kv: kv[1][0],
                )
                drop_n = max(1, self._discovery_cache_max // 4)
                for old_key, _ in by_ts[:drop_n]:
                    self._discovery_cache.pop(old_key, None)
            logger.info(f"[A2A Client] Discovered {len(cards)} agents at {agent_url}")
            return cards

        except Exception as exc:  # ruff: ignore[BLE001]  # broad catch intentional
            logger.error(f"[A2A Client] Discovery failed for {agent_url}: {exc}")
            return []

    async def call_remote_agent(
        self,
        agent_url: str,
        agent_name: str,
        task_input: dict[str, Any],
        task_id: str | None = None,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        """Execute a task on a remote A2A agent.

        Args:
            agent_url: Base URL of the remote A2A server.
            agent_name: Name of the remote agent to call.
            task_input: Task input dict (must include 'query' key).
            task_id: Optional task ID (auto-generated if None).
            timeout: Timeout in seconds.

        Returns:
            A2ATaskResult as a dict.

        Raises:
            TimeoutError: If the remote agent doesn't respond in time.
            httpx.HTTPError: On network errors.

        """
        cfg = get_a2a_config()
        if task_id is None:
            task_id = str(uuid.uuid4())
        if timeout is None:
            timeout = cfg.max_task_timeout_seconds

        execute_url = f"{agent_url.rstrip('/')}/execute"

        # Build request body
        body: dict[str, Any] = {
            "task_id": task_id,
            "agent_name": agent_name,
            "input": task_input,
        }

        # Sign the input payload
        input_payload = json.dumps(task_input, sort_keys=True).encode()
        body["signature"] = self._sign_request(input_payload)

        # Include public key for verification by the remote agent
        signing_key = self._get_signing_key()
        try:
            if signing_key is not None:
                import nacl.signing

                if isinstance(signing_key, nacl.signing.SigningKey):
                    body["peer_public_key"] = signing_key.verify_key.encode().hex()
        except (ImportError, AttributeError, TypeError, ValueError) as exc:
            logger.warning(
                "Cannot attach the peer public key to the A2A request (%s); the remote "
                "agent receives an unsigned-identity call and may reject it.",
                exc,
            )

        logger.info(f"[A2A Client] Calling remote agent '{agent_name}' at {agent_url}")

        # Execute with concurrency cap
        async with self._semaphore:
            client = await self._get_http()
            try:
                resp = await client.post(
                    execute_url, json=body, timeout=httpx.Timeout(timeout, connect=10.0)
                )
                resp.raise_for_status()
                result = resp.json()

                logger.info(
                    f"[A2A Client] Remote agent '{agent_name}' completed: {result.get('status')}"
                )
                return result

            except httpx.TimeoutException:
                logger.error(f"[A2A Client] Remote agent '{agent_name}' timed out after {timeout}s")
                return {
                    "task_id": task_id,
                    "status": "failed",
                    "error": f"Timeout after {timeout}s",
                    "agent_name": agent_name,
                }
            except httpx.HTTPStatusError as exc:
                logger.error(f"[A2A Client] Remote agent returned {exc.response.status_code}")
                return {
                    "task_id": task_id,
                    "status": "failed",
                    "error": f"HTTP {exc.response.status_code}: {exc.response.text[:200]}",
                    "agent_name": agent_name,
                }
            except (httpx.RequestError, ConnectionError, TimeoutError) as exc:
                # v13.22.4: widen the catch to include httpx.RequestError
                # (parent of ConnectError, ReadError, PoolTimeout, etc).
                # The previous (ConnectionError, TimeoutError) tuple only
                # matched the BUILTIN exceptions; an httpx ConnectError
                # or TLS error propagated uncaught out of the workflow
                # node, bypassing the graceful 'status: failed' contract.
                logger.error(f"[A2A Client] Remote call failed: {exc}")
                return {
                    "task_id": task_id,
                    "status": "failed",
                    "error": str(exc),
                    "agent_name": agent_name,
                }

    async def execute_as_node(
        self,
        state: dict[str, Any],
        phase_spec: dict[str, Any],
        output_key: str,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        """Execute a remote A2A agent as an Beagle workflow node.

        Called when workflow_loader detects executor="a2a_remote".

        Args:
            state: Current Beagle workflow state dict.
            phase_spec: Phase spec from YAML workflow.
            output_key: Key to store result in state.
            timeout: Timeout in seconds.

        Returns:
            State update dict following Beagle conventions.

        """
        import re

        agent_url = phase_spec.get("agent_url", "")
        agent_name = phase_spec.get("agent_name", phase_spec.get("name", "remote"))
        input_mapping = phase_spec.get("input_mapping", {})

        if not agent_url:
            # Try remote_agents config
            cfg = get_a2a_config()
            agent_url = cfg.remote_agents.get(agent_name, "")

        if not agent_url:
            err_msg = f"{agent_name}: No agent_url configured for A2A remote call"
            logger.error(err_msg)
            return {"errors": [err_msg], "completed_nodes": [f"{agent_name}(no_url)"]}

        # Resolve input mapping from state
        task_input: dict[str, Any] = {}
        for key, template in input_mapping.items():
            if isinstance(template, str):
                match = re.match(r"^\{\{state\.([a-zA-Z0-9_.]+)\}\}$", template.strip())
                if match:
                    path = match.group(1).split(".")
                    value: Any = state
                    for p in path:
                        if isinstance(value, dict):
                            value = value.get(p, "")
                        else:
                            value = ""
                            break
                    task_input[key] = value
                else:
                    task_input[key] = template
            else:
                task_input[key] = template

        # Ensure 'query' is present
        if "query" not in task_input:
            task_input["query"] = state.get("query", "")

        result = await self.call_remote_agent(
            agent_url=agent_url,
            agent_name=agent_name,
            task_input=task_input,
            timeout=timeout,
        )

        result_str = json.dumps(result, default=str, indent=2)

        updates: dict[str, Any] = {
            "completed_nodes": [agent_name],
            "metadata": {**state.get("metadata", {}), output_key: result_str},
        }

        try:
            from beagle.utils.field_mapping import map_output_to_state

            target_key = map_output_to_state(output_key, skill_name=agent_name)
            if target_key:
                updates[target_key] = result_str
        except (ImportError, AttributeError, TypeError, KeyError, ValueError) as exc:
            logger.warning(
                "Cannot map A2A output key %r onto the workflow state (%s); the remote "
                "agent's result is dropped from the state update.",
                output_key,
                exc,
            )

        if result.get("status") == "failed":
            updates["errors"] = [result.get("error", "Remote A2A call failed")]

        return updates


# ── Global singleton ──────────────────────────────────────────────────────────

_client: A2AClientBridge | None = None
_client_lock = threading.Lock()


def get_a2a_client() -> A2AClientBridge:
    """Get the global A2A client bridge singleton.

    v13.22.4: double-checked locking. The previous unsynchronised
    check-then-create let two concurrent first-callers create two
    clients; one would leak its connection pool. A2AClientBridge is
    cheap to construct but its shared httpx pool is not.
    """
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = A2AClientBridge()
    return _client
