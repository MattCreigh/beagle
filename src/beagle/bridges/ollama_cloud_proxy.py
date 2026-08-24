#!/usr/bin/env python3
"""
Ollama Cloud → Goose reasoning-model proxy.

Streams the upstream OpenAI-compatible SSE straight through to goose's streaming
client, token-by-token. minimax-m3:cloud (the configured model) emits the answer
directly in delta.content while streaming — verified against the live API — so the
old "strip stream → buffer → re-wrap as one SSE chunk" approach (which caused a
~1-minute wait-then-burst and capped every turn at the max_tokens floor, truncating
long output mid-section) is gone.

Per-chunk transforms applied while relaying:
  * carry delta.tool_calls with a per-call `index` (goose needs it to accumulate
    streaming tool calls — without it the agentic loop stalls), and
  * map a delta.reasoning / reasoning_content field into delta.content as a
    fallback for fallback-chain models that stream into the reasoning channel,
    so goose never renders a blank turn (no-op for minimax-m3).

Non-streaming clients still get the buffered path with the reasoning→content patch
and the total `_json_to_sse` / `_sse_error` hardening.

Env vars:
  OPENAI_API_KEY                 upstream bearer token
  OLLAMA_PROXY_PORT             listen port (default 9999)
  OLLAMA_PROXY_MAX_TOKENS       per-response output budget floor (default 8192)
  OLLAMA_PROXY_UPSTREAM_TIMEOUT upstream read timeout, seconds (default 300)
  OLLAMA_PROXY_ALLOW_ORIGIN     explicit CORS origin (default: none)
  OLLAMA_PROXY_ALLOW_PUBLIC     permit non-loopback bind (default: refuse)

Usage:
  python3 bridges/ollama_cloud_proxy.py --port 9999 &
  goose configure  # set OPENAI_HOST=http://127.0.0.1:9999
"""

from __future__ import annotations

import argparse
import contextlib
import http.client
import ipaddress
import json
import os
import ssl
import sys
import time
import traceback
from http.server import (  # D9 fix (v13.21.10): ThreadingHTTPServer prevents head-of-line blocking for streaming endpoints.
    BaseHTTPRequestHandler,
    ThreadingHTTPServer,
)
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from beagle.config.models import MODEL_CONFIGS, get_max_output_tokens
from beagle.security.validation import validate_http_url


def _env_int(name: str, default: int) -> int:
    """Read a positive int from the environment, else return the default."""
    raw = os.environ.get(name, "").strip()
    if raw:
        try:
            val = int(raw)
            if val > 0:
                return val
        except ValueError:
            _log(f"{name}={raw!r} is not an integer; using the default of {default}")
    return default


# Per-response output budget floor. Goose sends no max_tokens, so this becomes the
# effective ceiling; 4096 truncated long output mid-section. Env-tunable.
DEFAULT_MAX_TOKENS = _env_int("OLLAMA_PROXY_MAX_TOKENS", 8192)
# D16 (Fable 5 DD 2026-06-11): the floor was previously hard-coded to
# DEFAULT_MAX_TOKENS (8192) and silently overrode any caller request below
# that. Now env-tunable via OLLAMA_PROXY_MIN_MAX_TOKENS; set to 0 to disable
# the floor entirely. This lets cost-sensitive callers request small
# completions without paying for the goose-generation default.
MIN_MAX_TOKENS = _env_int("OLLAMA_PROXY_MIN_MAX_TOKENS", 8192)
# Upstream read timeout. Long-form non-streaming turns and slow streams need >120s;
# justified per the HTTP-timeout doctrine (long-form generation). Env-tunable.
# NOTE: a proper circuit breaker around this call is a follow-up (bare http.server).
DEFAULT_UPSTREAM_TIMEOUT = _env_int("OLLAMA_PROXY_UPSTREAM_TIMEOUT", 300)
DEFAULT_PORT = 9999
DEFAULT_BIND = "127.0.0.1"
# Cap on inbound request body size (bytes). 64 MiB is well above the
# largest plausible chat-completion request (model context + system
# prompt + tool calls) and well below the proxy's per-process RSS.
# Operators tune via OLLAMA_PROXY_MAX_BODY_BYTES.
MAX_REQUEST_BODY_BYTES = _env_int("OLLAMA_PROXY_MAX_BODY_BYTES", 64 * 1024 * 1024)
# S01 remediation: any non-loopback bind is rejected at startup so the proxy
# cannot be exposed to the network without an explicit opt-in (--allow-public).
# D5 (Fable 5 DD 2026-06-11): the previous blocklist ({0.0.0.0, ::, ""}) let
# LAN IPs (192.168.x.x) sail through. Replaced with an is_loopback allowlist
# using ipaddress. The wildcard-bind catch is now also enforced for any
# non-loopback, non-explicit-public bind.


def _is_loopback_bind(bind: str) -> bool:
    """True if `bind` is a loopback address (127.0.0.0/8, ::1) or 'localhost'."""
    if bind in ("localhost", "127.0.0.1", "::1"):
        return True
    try:
        return ipaddress.ip_address(bind).is_loopback
    except ValueError:
        return False


# ── helpers ──────────────────────────────────────────────────────────────────


def _normalize_model_name(model: str) -> str:
    """Strip date-tag suffixes from production model names.

    Ollama Cloud model names sometimes carry a date tag, e.g.
    ``deepseek-v4-flash:0731-cloud``. Map those to the canonical
    MODEL_CONFIGS key while keeping size tags such as
    ``gemma4:31b-cloud`` intact.
    """
    import re

    if ":" not in model:
        return model
    parts = model.split(":")
    if len(parts) == 3 and parts[2] in {"cloud", "local"}:
        return f"{parts[0]}:{parts[2]}"
    if len(parts) == 2 and re.fullmatch(r"\d+[\-:]?(cloud|local)", parts[1]):
        return f"{parts[0]}:cloud"
    return model


def _log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", file=sys.stderr, flush=True)


def _get_api_key() -> str:
    return os.environ.get("OPENAI_API_KEY", "")


def _patch_content(choice: dict) -> bool:
    """If content empty but reasoning present, copy best sentence into content."""
    msg = choice.get("message", {})

    # Never fabricate content over a real tool call: the tool_calls ARE the
    # response. Injecting reasoning text here both leaks deliberation to the
    # user and masks the action — goose shows the narration and stalls.
    if msg.get("tool_calls") or msg.get("function_call"):
        return False

    content = msg.get("content", "")
    reasoning = msg.get("reasoning", "")

    if content and content.strip():
        return False
    if not reasoning or not reasoning.strip():
        return False

    lines = [ln.strip() for ln in reasoning.split("\n") if ln.strip()]
    if not lines:
        return False

    # Reject pure deliberation prefixes — only unambiguous thought-chain markers
    deliberation_prefixes = (
        "let me",
        "i should",
        "i will",
        "i'm going",
        "i'll",
        "we should",
        "we'll",
        "we are",
        "to answer",
        "first,",
        "next,",
        "then,",
        "finally,",
        "here is",
        "this is",
    )

    answer_lines = [
        ln for ln in lines if not any(ln.lower().startswith(p) for p in deliberation_prefixes)
    ]

    fallback = answer_lines[-1] if answer_lines else lines[-1]

    # Truncate long reasoning dumps to last sentence or hard-truncate
    if len(fallback) > 500:
        parts = fallback.rsplit(". ", 1)
        if len(parts) == 2:
            fallback = parts[-1].rstrip(".") + "."
        elif ". " in fallback:
            # Multiple sentences but rsplit failed — take last sentence
            fallback = fallback.split(". ")[-1].rstrip(".") + "."
        else:
            # Single giant sentence — hard-truncate to last 500 chars
            fallback = "…" + fallback[-497:]

    msg["content"] = fallback
    _log(f"  patched empty content ← reasoning[{len(fallback)} ch]: {fallback[:80]}...")
    return True


# Per-model maximum output-token budget. Ollama Cloud rejects a request whose
# max_tokens exceeds the model's declared output ceiling (e.g. nemotron-3-ultra
# caps at 65536). goose sends the context window (128000) as max_tokens, which
# the proxy previously passed through untouched — the floor only raised small
# values, never lowered oversized ones. This table caps max_tokens at the
# model's real output budget so a request can never be rejected upstream.
# Keys are the bare model names goose sends (no :cloud suffix). Unknown models
# fall back to the context-window default (no cap) so a new model is never
# silently truncated.


def _model_max_output(model: str) -> int | None:
    """Return the declared max output budget for a model, or None if unknown.

    Values are sourced from beagle.config.models (the single source of truth)
    instead of a local table. The helper also accepts date-tagged variants such
    as ``deepseek-v4-flash:0731-cloud`` because ``get_max_output_tokens``
    normalizes those to the canonical MODEL_CONFIGS key.
    """
    # A model is "known" if it appears in MODEL_CONFIGS either exactly or after
    # stripping a date tag. Unknown models return the default 8000, which the
    # proxy treats as "no cap" so a new model is never silently truncated.
    if model in MODEL_CONFIGS:
        return get_max_output_tokens(model)
    normalized = _normalize_model_name(model)
    if normalized in MODEL_CONFIGS:
        return get_max_output_tokens(normalized)
    return None


def _ensure_max_tokens(body: dict) -> bool:
    """Ensure max_tokens is within [MIN_MAX_TOKENS, model max output].

    Returns True if changed.

    D16 (Fable 5 DD 2026-06-11): the previous version used DEFAULT_MAX_TOKENS
    (the proxy's "good for goose" budget) as the floor, silently overriding
    callers that intentionally requested smaller completions. The floor is
    now a separate constant MIN_MAX_TOKENS, env-tunable via
    OLLAMA_PROXY_MIN_MAX_TOKENS. Set to 0 to disable the floor entirely.

    v1.0.10 (F1): added the per-model CAP. goose sends the context window
    (128000) as max_tokens; Ollama Cloud rejects any request whose max_tokens
    exceeds the model's declared output ceiling (nemotron-3-ultra → 65536).
    The floor alone never lowered oversized values, so every nemotron-3-ultra
    call failed with a 400. The cap clamps max_tokens to the model's real
    output budget, and is env-tunable via OLLAMA_PROXY_MAX_OUTPUT_TOKENS
    (comma-separated ``model=value`` pairs) for operators who need to
    override a single model without editing the table.

    WP-3 M9: ``body.setdefault('max_tokens', 0)`` ensures the log line can
    always read body['max_tokens'] even when the floor path did not set it.
    MIN_MAX_TOKENS is positive by default, so the floor branch is reachable;
    the setdefault is a defensive guard, not dead code.
    """
    model = body.get("model", "")
    cap = _model_max_output(model)
    if cap is None:
        # Unknown model — apply the floor only (no cap) so a new model is
        # never silently truncated.
        cap = 0

    max_t = body.get("max_tokens", 0)
    max_ct = body.get("max_completion_tokens", 0)
    changed = False

    # Floor: never let a caller request a tiny completion that truncates
    # long-form output mid-section.
    body.setdefault("max_tokens", 0)
    if MIN_MAX_TOKENS > 0:
        if max_ct and max_ct < MIN_MAX_TOKENS:
            body["max_completion_tokens"] = MIN_MAX_TOKENS
            changed = True
        if not max_t or max_t < MIN_MAX_TOKENS:
            body["max_tokens"] = MIN_MAX_TOKENS
            changed = True

    # Cap: never let a caller request an output budget the model cannot
    # produce — Ollama Cloud rejects it with a 400.
    if cap > 0:
        if max_ct and max_ct > cap:
            body["max_completion_tokens"] = cap
            changed = True
        if max_t and max_t > cap:
            body["max_tokens"] = cap
            changed = True

    if changed:
        _log(
            f"  bounded tokens: max_tokens {max_t}→{body['max_tokens']}, "
            f"max_completion_tokens {max_ct}→{body.get('max_completion_tokens', 'n/a')} "
            f"(model={model}, cap={cap or 'none'})"
        )
    return changed


# ── handler ──────────────────────────────────────────────────────────────────


class ProxyHandler(BaseHTTPRequestHandler):
    # Client-socket inactivity timeout. Must exceed the upstream timeout so a long
    # streaming relay isn't cut off mid-flight by the client connection timing out.
    timeout = DEFAULT_UPSTREAM_TIMEOUT + 60

    # Upstream set by handler_factory before instantiation
    _upstream: str = "https://ollama.com"

    def __init__(self, *args, **kwargs):
        self._api_key = _get_api_key()
        super().__init__(*args, **kwargs)

    def do_POST(self):
        self._proxy("POST")

    def do_GET(self):
        self._proxy("GET")

    def do_OPTIONS(self):
        self._proxy("OPTIONS")

    def _proxy(self, method: str) -> None:
        # WP-4 B8: SSRF guard. Only accept absolute paths with a single
        # leading slash; build the upstream URL with urljoin; verify the
        # resulting host matches the configured upstream host before any
        # Authorization header is attached.
        from urllib.parse import urljoin, urlsplit

        if not self.path.startswith("/") or self.path.startswith("//"):
            _log(f"  rejected {method} {self.path}: invalid request target")
            self.send_error(400, "Invalid request target")
            return

        # The host check below pins *where* the request goes; this pins *how*.
        # target inherits its scheme from self._upstream, so a misconfigured
        # upstream of file:// or ftp:// would turn every proxied request into a
        # local-file read or an outbound FTP fetch — urlopen dispatches on the
        # scheme, it is not an HTTP-only client.
        try:
            target = validate_http_url(urljoin(self._upstream, self.path))
        except ValueError as exc:
            _log(f"  rejected {method} {self.path}: {exc}")
            self.send_error(400, "Invalid request target")
            return
        parsed_target = urlsplit(target)
        parsed_upstream = urlsplit(self._upstream)
        if parsed_target.hostname != parsed_upstream.hostname:
            _log(
                f"  rejected {method} {self.path}: target host "
                f"{parsed_target.hostname} != upstream {parsed_upstream.hostname}"
            )
            self.send_error(400, "Invalid request target")
            return

        # Bound the body read to prevent memory-exhaustion DoS. A local
        # client (or any client permitted through the firewall) can send
        # Content-Length: 10GB; without a cap the proxy allocates that
        # much memory before noticing. v13.22.4 S2.
        try:
            content_len = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            _log(f"  rejected {method} {self.path}: malformed Content-Length")
            self.send_error(400, "Malformed Content-Length")
            return
        if content_len < 0:
            _log(f"  rejected {method} {self.path}: negative Content-Length {content_len}")
            self.send_error(400, "Negative Content-Length")
            return
        if content_len > MAX_REQUEST_BODY_BYTES:
            _log(
                f"  rejected {method} {self.path}: body {content_len} > "
                f"limit {MAX_REQUEST_BODY_BYTES}"
            )
            self.send_error(413, "Request body too large")
            return
        body = self.rfile.read(content_len) if content_len > 0 else b""

        _log(f"{method} {self.path}  ({content_len} bytes)")

        wants_stream = False
        if self.path == "/v1/chat/completions" and body:
            try:
                body, wants_stream = self._intercept_chat(body)
            except (ValueError, TypeError):
                _log(f"  intercept error: {traceback.format_exc()}")

        req_headers = {}
        if method == "POST" and body:
            req_headers["Content-Type"] = "application/json"
        if self._api_key:
            req_headers["Authorization"] = f"Bearer {self._api_key}"
        req_headers["Accept"] = "text/event-stream" if wants_stream else "application/json"

        # Streaming relay: pass the upstream SSE straight through, token-by-token.
        if wants_stream and self.path == "/v1/chat/completions":
            self._proxy_chat_streaming(target, body, req_headers)
            return

        # Buffered path: non-streaming clients, /v1/models, OPTIONS, etc.
        req = Request(
            target,
            data=body if method == "POST" else None,
            headers=req_headers,
            method=method,
        )
        try:
            with urlopen(req, timeout=DEFAULT_UPSTREAM_TIMEOUT) as upstream_resp:  # nosec B310 - scheme checked by security.validation.validate_http_url before this call
                resp_body = upstream_resp.read()
                status = upstream_resp.status

                if self.path == "/v1/chat/completions" and resp_body:
                    try:
                        resp_body = self._intercept_chat_response(resp_body)
                    except (ValueError, KeyError, IndexError, TypeError):
                        _log(f"  response intercept error: {traceback.format_exc()}")

                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                upstream_hdrs = dict(upstream_resp.headers.items())
                for hdr in (
                    "X-Request-Id",
                    "Retry-After",
                    "X-RateLimit-Limit",
                    "X-RateLimit-Remaining",
                    "X-RateLimit-Reset",
                ):
                    if hdr in upstream_hdrs:
                        self.send_header(hdr, upstream_hdrs[hdr])
                self.send_header("Content-Length", str(len(resp_body)))
                self._maybe_cors()
                self.end_headers()
                self.wfile.write(resp_body)
                _log(f"  ← {status}  ({len(resp_body)} bytes)")

        except HTTPError as e:
            _log(f"  upstream error: {e.code} {e.reason}")
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(e.read() or b"{}")
        except URLError as e:
            _log(f"  upstream unreachable: {e.reason}")
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": f"Upstream unreachable: {e.reason}"}).encode())

    def _intercept_chat(self, body: bytes) -> tuple[bytes, bool]:
        """Return (possibly-rewritten body, wants_stream).

        Unlike earlier versions we KEEP stream=true — minimax-m3:cloud streams the
        answer directly in delta.content, so we relay the upstream SSE for real
        token-by-token output. We only floor max_tokens here.
        """
        data = json.loads(body)
        wants_stream = bool(data.get("stream"))
        if _ensure_max_tokens(data):
            body = json.dumps(data).encode()
        return body, wants_stream

    def _maybe_cors(self) -> None:
        # S07: no wildcard CORS. The loopback-only proxy needs none; an operator who
        # intentionally exposes it can set OLLAMA_PROXY_ALLOW_ORIGIN to one origin.
        allow_origin = os.environ.get("OLLAMA_PROXY_ALLOW_ORIGIN", "").strip()
        if allow_origin:
            self.send_header("Access-Control-Allow-Origin", allow_origin)
            self.send_header("Vary", "Origin")

    def _send_sse_headers(self, status: int, upstream_hdrs: dict) -> None:
        """Send SSE response headers (no Content-Length → close-delimited stream)."""
        self.send_response(status)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        for hdr in (
            "X-Request-Id",
            "Retry-After",
            "X-RateLimit-Limit",
            "X-RateLimit-Remaining",
            "X-RateLimit-Reset",
        ):
            if hdr in upstream_hdrs:
                self.send_header(hdr, upstream_hdrs[hdr])
        self._maybe_cors()
        self.end_headers()

    def _safe_write(self, data: bytes) -> None:
        """Write + flush, swallowing a client disconnect (BrokenPipe etc.)."""
        try:
            self.wfile.write(data)
            self.wfile.flush()
        except OSError as exc:
            # The client hung up mid-response. Expected for a cancelled request;
            # recorded so a burst of them is visible in the log.
            _log(f"  client disconnected during write: {exc}")

    def _proxy_chat_streaming(self, target: str, body: bytes, req_headers: dict) -> None:
        """Relay the upstream SSE to goose token-by-token.

        Each upstream `data:` chunk is transformed (tool_calls index +
        reasoning→content fallback) and flushed immediately so tokens appear live.
        The stream is ALWAYS terminated with `data: [DONE]` — even on error — so
        goose never hangs on a half-open connection.
        """
        req = Request(target, data=body, headers=req_headers, method="POST")
        try:
            upstream = urlopen(req, timeout=DEFAULT_UPSTREAM_TIMEOUT)  # nosec B310 - scheme checked by security.validation.validate_http_url before this call
        except HTTPError as e:
            self._send_sse_headers(e.code, {})
            self._safe_write(self._sse_error(e.read() or b"{}"))
            _log(f"  stream upstream error: {e.code} {e.reason}")
            return
        except URLError as e:
            self._send_sse_headers(502, {})
            self._safe_write(
                self._sse_error(
                    json.dumps({"error": {"message": f"Upstream unreachable: {e.reason}"}}).encode()
                )
            )
            _log(f"  stream upstream unreachable: {e.reason}")
            return

        with upstream:
            ctype = (upstream.headers.get("Content-Type", "") or "").lower()
            if "text/event-stream" not in ctype:
                # Upstream did not stream (error JSON / non-stream model) — buffer,
                # patch reasoning→content, and emit one terminating SSE.
                raw = upstream.read()
                with contextlib.suppress(ValueError, KeyError, IndexError, TypeError):
                    raw = self._intercept_chat_response(raw)
                self._send_sse_headers(upstream.status, dict(upstream.headers.items()))
                self._safe_write(self._json_to_sse(raw))
                _log(f"  stream→buffered fallback ({len(raw)} bytes)")
                return

            self._send_sse_headers(upstream.status, dict(upstream.headers.items()))
            emitted_content = False
            n = 0
            try:
                for raw_line in upstream:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line or not line.startswith("data:"):
                        continue  # skip blanks + SSE comment/event lines
                    payload = line[len("data:") :].strip()
                    if payload == "[DONE]":
                        break
                    out, had = self._transform_stream_chunk(payload)
                    emitted_content = emitted_content or had
                    self.wfile.write(f"data: {out}\n\n".encode("utf-8", errors="replace"))
                    self.wfile.flush()
                    n += 1
            # v13.22.4 S5: IncompleteRead, ssl.SSLError, and HTTPException
            # are NOT subclasses of OSError/ValueError; an upstream half-close,
            # TLS renegotiation failure, or non-2xx-mid-stream would otherwise
            # propagate out of the handler thread unhandled.
            except (OSError, ValueError, http.client.IncompleteRead, ssl.SSLError) as exc:
                _log(f"  stream relay interrupted: {exc!r}")
            finally:
                self._safe_write(b"data: [DONE]\n\n")
            _log(f"  ← stream relayed {n} chunks (content={emitted_content})")

    @staticmethod
    def _transform_stream_chunk(payload: str) -> tuple[str, bool]:
        """Transform one upstream SSE data payload → (json_str, had_content).

        Never raises: a malformed chunk is passed through unchanged. Fast-paths the
        common minimax-m3 case (content-only delta) by returning the payload verbatim.
        """
        try:
            chunk = json.loads(payload)
            choices = chunk.get("choices")
            if not isinstance(choices, list) or not choices:
                return payload, False
            delta = choices[0].get("delta")
            if not isinstance(delta, dict):
                return payload, False

            had_content = bool(delta.get("content"))
            tcs = delta.get("tool_calls")
            needs_tc = isinstance(tcs, list) and bool(tcs)

            # Fast path: clean content-only chunk → pass through unmodified.
            if had_content and not needs_tc:
                return payload, True

            # Fallback-chain models may stream into a reasoning channel; surface it
            # as content so goose never renders a blank turn. No-op for minimax-m3.
            if not had_content:
                reasoning = delta.get("reasoning") or delta.get("reasoning_content")
                if isinstance(reasoning, str) and reasoning:
                    delta["content"] = reasoning
                    had_content = True

            # Streaming tool-call fix: each tool_call delta needs an index so goose
            # can accumulate it; without it the agentic loop stalls.
            if needs_tc and isinstance(tcs, list):
                delta["tool_calls"] = [
                    {**tc, "index": tc.get("index", i)}
                    for i, tc in enumerate(tcs)
                    if isinstance(tc, dict)
                ]
            return json.dumps(chunk), had_content
        except (ValueError, TypeError):
            return payload, False

    def _json_to_sse(self, body: bytes) -> bytes:
        """Convert a non-streaming JSON chat completion response into SSE chunks.

        Goose's OpenAI client sends stream:true and expects SSE events.
        We stripped stream:true to avoid reasoning-model content starvation
        (the Ollama Cloud SSE emits reasoning chunks, not content).  Now we
        must convert the non-streaming JSON back into valid SSE so goose can
        parse it.

        Format per SSE spec (https://html.spec.whatwg.org/multipage/server-sent-events.html):
          data: {JSON}\\n\\n
        With final [DONE] marker so goose knows streaming is complete.
        """
        # Upstream returned SSE despite our stream strip — already valid; pass through.
        head = body.lstrip()[:6]
        if head.startswith(b"data:") or head.startswith(b"event:"):
            return body

        # TOTAL FUNCTION — MUST NOT raise. This runs after we commit to an SSE
        # response but BEFORE send_response(); an exception here leaves goose
        # waiting on a half-open connection forever (the "spins then dies" hang).
        # Any unexpected body (error JSON, empty choices, non-JSON) is coerced
        # into a valid, self-terminating SSE stream via _sse_error.
        try:
            data = json.loads(body)
            choice = data["choices"][0]
            msg = choice["message"]
            if not isinstance(msg, dict):
                raise TypeError("message is not an object")

            # Build a delta-form chunk that looks like a streaming response
            delta = {}
            if "role" in msg:
                delta["role"] = msg["role"]
            if "content" in msg:
                delta["content"] = msg["content"]

            # Carry tool calls through the SSE conversion. Stripping stream=true and
            # re-wrapping the response previously dropped message.tool_calls, so goose
            # received an assistant turn with no action and ended the agentic loop
            # (the narrate-then-stop stall). Add a per-call index — the OpenAI
            # streaming schema requires delta.tool_calls[].index for accumulation.
            tool_calls = msg.get("tool_calls")
            if isinstance(tool_calls, list) and tool_calls:
                delta["tool_calls"] = [
                    {**tc, "index": tc.get("index", i)}
                    for i, tc in enumerate(tool_calls)
                    if isinstance(tc, dict)
                ]
            if msg.get("function_call"):
                delta["function_call"] = msg["function_call"]

            chunk = {
                "id": data.get("id", "proxy-sse"),
                "object": "chat.completion.chunk",
                "created": data.get("created", 0),
                "model": data.get("model", ""),
                "choices": [
                    {
                        "index": 0,
                        "delta": delta,
                        "finish_reason": choice.get("finish_reason"),
                    }
                ],
            }
            sse = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"
            return sse.encode("utf-8", errors="replace")
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            _log(f"  json_to_sse: non-standard body ({exc.__class__.__name__}); emitting error SSE")
            return self._sse_error(body)

    @staticmethod
    def _sse_error(body: bytes) -> bytes:
        """Wrap an unconvertible upstream body into one terminating SSE chunk.

        Runs when _json_to_sse cannot parse/serialize the upstream response.
        Extracts a human-readable message (OpenAI error bodies use
        {"error": {"message": ...}}) and emits it as assistant content with a
        clean [DONE] so goose renders the failure and the agentic loop ends
        instead of hanging on a half-open stream.
        """
        text = ""
        try:
            err = json.loads(body)
            if isinstance(err, dict):
                detail = err.get("error")
                if isinstance(detail, dict):
                    text = str(detail.get("message", "")).strip()
                elif isinstance(detail, str):
                    text = detail.strip()
        except (ValueError, TypeError) as exc:
            _log(
                f"  upstream error body is not the expected JSON shape ({exc}); using the raw snippet"
            )
        if not text:
            snippet = body.decode("utf-8", errors="replace")[:500]
            text = f"[ollama-cloud-proxy] upstream returned an unparseable response: {snippet}"

        chunk = {
            "id": "proxy-sse-error",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "",
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
        }
        sse = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"
        return sse.encode("utf-8", errors="replace")

    def _intercept_chat_response(self, body: bytes) -> bytes:
        data = json.loads(body)
        choices = data.get("choices", [])
        patched = sum(1 for c in choices if _patch_content(c))
        if patched:
            _log(f"  patched {patched}/{len(choices)} choices reasoning→content")
            body = json.dumps(data).encode()
        return body

    def log_message(self, _format, *_args):
        pass  # Suppress default HTTP request logging


# ── main ─────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="Ollama Cloud reasoning-model proxy")
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help=f"Listen port (default: {DEFAULT_PORT})"
    )
    parser.add_argument(
        "--bind", default=DEFAULT_BIND, help=f"Bind address (default: {DEFAULT_BIND})"
    )
    parser.add_argument("--upstream", default="https://ollama.com", help="Upstream host")
    parser.add_argument(
        "--allow-public",
        action="store_true",
        help="Permit non-loopback bind addresses (insecure; required for LAN exposure)",
    )
    args = parser.parse_args()

    # S01 remediation: refuse non-loopback binds unless --allow-public is set.
    # Default proxy serves the local goose client only; wild binds would
    # expose the un-authenticated chat proxy to the network.
    allow_public = os.environ.get("OLLAMA_PROXY_ALLOW_PUBLIC", "").lower() in (
        "1",
        "true",
        "yes",
    )
    # D5 fix: refuse any non-loopback bind unless --allow-public is set.
    # Previous blocklist missed LAN IPs (192.168.x.x); now uses is_loopback
    # allowlist. Default proxy serves the local goose client only; wild binds
    # would expose the un-authenticated chat proxy to the network.
    allow_public = os.environ.get("OLLAMA_PROXY_ALLOW_PUBLIC", "").lower() in (
        "1",
        "true",
        "yes",
    )
    if not _is_loopback_bind(args.bind) and not (allow_public or args.allow_public):
        _log(
            f"refusing non-loopback bind {args.bind!r} — pass --allow-public to override "
            "(or set OLLAMA_PROXY_ALLOW_PUBLIC=1)"
        )
        sys.exit(2)

    # Patch the class default before starting server
    ProxyHandler._upstream = args.upstream

    def handler_factory(*a, **kw):
        return ProxyHandler(*a, **kw)

    server = ThreadingHTTPServer((args.bind, args.port), handler_factory)
    _log(f"Ollama Cloud proxy listening on {args.bind}:{args.port} → {args.upstream}")
    _log(
        f"streaming relay: ON  |  max_tokens floor: {DEFAULT_MAX_TOKENS}  |  upstream timeout: {DEFAULT_UPSTREAM_TIMEOUT}s"
    )

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _log("shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
