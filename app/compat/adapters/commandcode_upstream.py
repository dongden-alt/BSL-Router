"""CommandCode upstream adapter — Vercel AI SDK Data Stream Protocol.

CommandCode's alpha endpoint (https://api.commandcode.ai/alpha/generate) speaks
the Vercel AI SDK Data Stream Protocol, NOT OpenAI's chat-completions wire
format. The request envelope wraps params in a `{threadId, memory, config,
params}` shell and messages use Anthropic content-block shape. The response is
SSE with typed frames: start → start-step → text-start → text-delta* → text-end
→ finish-step → finish → provider-metadata.

This module mirrors the Antigravity adapter pattern: a client-boundary wrapper
lets every hardened send site in main.py (probe, OAuth 401-retry, thinking-
degradation retry, combo fallback, buffered aggregation, zero-token watchdogs)
operate unchanged on OpenAI-canonical payloads while the wire speaks Vercel
AI SDK.

Live-verified 2026-09-17 (scratch/cc_upstream_probe.py):
- POST /alpha/generate → 200, content-type: text/event-stream
- Old /provider/v1 → 403 "Your Go plan doesn't include API access"
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, List, Optional

# ── Envelope builder (request direction) ─────────────────────────────────────

COMMANDCODE_BASE_URL = "https://api.commandcode.ai"
COMMANDCODE_GENERATE_PATH = "/alpha/generate"


def openai_to_commandcode_envelope(payload: Dict[str, Any], model: str) -> Dict[str, Any]:
    """Convert an OpenAI chat-completion payload into the CommandCode envelope.

    The alpha endpoint expects:
        {
          "threadId": "<uuid>",
          "memory": "",
          "config": { workingDir, date, environment, structure, isGitRepo,
                      currentBranch, mainBranch, gitStatus, recentCommits },
          "params": { model, messages, stream, max_tokens, temperature }
        }

    ``params.messages`` use Anthropic content-block shape:
        {"role": "user", "content": [{"type": "text", "text": "..."}]}

    OpenAI string content is normalized to that block shape. Messages already
    in block form (list content) pass through untouched.
    """
    params: Dict[str, Any] = {
        "model": model,
        "messages": _normalize_messages(payload.get("messages") or []),
        "stream": bool(payload.get("stream", False)),
    }
    # Pass through optional tuning params only when present.
    for key in ("max_tokens", "temperature", "top_p", "stop"):
        if key in payload and payload[key] is not None:
            params[key] = payload[key]

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return {
        "threadId": str(uuid.uuid4()),
        "memory": "",
        "config": {
            "workingDir": "",
            "date": now,
            "environment": "cli",
            "structure": "",
            "isGitRepo": False,
            "currentBranch": "",
            "mainBranch": "",
            "gitStatus": "",
            "recentCommits": "",
        },
        "params": params,
    }


def _normalize_messages(messages: List[Any]) -> List[Dict[str, Any]]:
    """Normalize OpenAI messages to Anthropic content-block shape.

    String content becomes [{"type": "text", "text": ...}].  List content
    (already blocks) passes through.  Non-dict messages are coerced to a user
    text block so the wire shape is always valid.
    """
    out: List[Dict[str, Any]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            out.append({"role": "user", "content": [{"type": "text", "text": str(msg)}]})
            continue
        role = msg.get("role", "user")
        content = msg.get("content")
        if isinstance(content, str):
            block_content = [{"type": "text", "text": content}]
        elif isinstance(content, list):
            block_content = content
        else:
            block_content = [{"type": "text", "text": str(content) if content is not None else ""}]
        entry: Dict[str, Any] = {"role": role, "content": block_content}
        out.append(entry)
    return out


# ── Vercel AI SSE translator (response direction) ────────────────────────────

# Vercel finishReason → OpenAI finish_reason
_FINISH_INVERSE: Dict[str, str] = {
    "stop": "stop",
    "end_turn": "stop",
    "length": "length",
    "max_tokens": "length",
    "content_filter": "content_filter",
    "tool_calls": "tool_calls",
}


def _usage_to_openai(usage: Dict[str, Any]) -> Dict[str, Any]:
    """Map Vercel usage fields to OpenAI usage shape."""
    prompt = usage.get("prompt_tokens") or usage.get("promptTokens") or 0
    completion = usage.get("completion_tokens") or usage.get("completionTokens") or 0
    total = usage.get("total_tokens") or usage.get("totalTokens") or (prompt + completion)
    out: Dict[str, Any] = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
    }
    # Preserve cache hit info if present (raw.prompt_cache_hit_tokens).
    raw = usage.get("raw")
    if isinstance(raw, dict):
        hit = raw.get("prompt_cache_hit_tokens")
        if isinstance(hit, int) and hit > 0:
            out["prompt_cache_hit_tokens"] = hit
    return out


class VercelAISSETranslator:
    """Translate Vercel AI SDK Data Stream frames into OpenAI chat.completion.chunk SSE.

    Feed raw decoded text chunks via ``feed()``; it buffers partial lines and
    yields OpenAI-format SSE byte strings. ``close()`` flushes any trailing
    buffer. ``final_openai_response()`` aggregates a non-stream completion.
    """

    def __init__(self, model: str):
        self.model = model
        self._id = f"chatcmpl-cc-{uuid.uuid4().hex[:24]}"
        self._created = int(time.time())
        self._buf = ""
        self.agg: Dict[str, Any] = {
            "content": [],
            "finish": None,
            "usage": None,
        }
        self._started = False

    # ── stream API ──

    def feed(self, text: str) -> List[bytes]:
        self._buf += text
        out: List[bytes] = []
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = line.rstrip("\r")
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data:
                continue
            frame = self._parse_frame(data)
            if frame is None:
                continue
            chunk = self._frame_to_chunk(frame)
            if chunk is not None:
                out.append(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8"))
        return out

    def close(self) -> List[bytes]:
        """Flush trailing buffer + emit [DONE]."""
        out: List[bytes] = []
        if self._buf.strip():
            for line in self._buf.split("\n"):
                line = line.rstrip("\r")
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data:
                    continue
                frame = self._parse_frame(data)
                if frame is not None:
                    chunk = self._frame_to_chunk(frame)
                    if chunk is not None:
                        out.append(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8"))
            self._buf = ""
        out.append(b"data: [DONE]\n\n")
        return out

    # ── frame parsing ──

    def _parse_frame(self, data: str) -> Optional[Dict[str, Any]]:
        try:
            obj = json.loads(data)
            if isinstance(obj, dict) and "type" in obj:
                return obj
        except (json.JSONDecodeError, ValueError):
            pass
        return None

    def _frame_to_chunk(self, frame: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Convert one Vercel frame to an OpenAI chunk (or None to skip)."""
        ftype = frame.get("type")

        if ftype == "start":
            # Emit the initial role chunk on first content-bearing frame.
            return None

        if ftype == "text-delta":
            text = frame.get("text", "")
            if not text:
                return None
            self.agg["content"].append(text)
            delta: Dict[str, Any] = {}
            if not self._started:
                delta["role"] = "assistant"
                self._started = True
            delta["content"] = text
            return {
                "id": self._id,
                "object": "chat.completion.chunk",
                "created": self._created,
                "model": self.model,
                "choices": [{"index": 0, "delta": delta}],
            }

        if ftype in ("finish-step", "finish"):
            fr = frame.get("finishReason") or frame.get("finish_reason") or "stop"
            finish_reason = _FINISH_INVERSE.get(fr, "stop")
            self.agg["finish"] = finish_reason
            usage_oai = None
            raw_usage = frame.get("usage") or frame.get("totalUsage")
            if isinstance(raw_usage, dict):
                usage_oai = _usage_to_openai(raw_usage)
                self.agg["usage"] = usage_oai
            chunk: Dict[str, Any] = {
                "id": self._id,
                "object": "chat.completion.chunk",
                "created": self._created,
                "model": self.model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
            }
            if usage_oai is not None:
                chunk["usage"] = usage_oai
            return chunk

        # Ignored frames: start-step, text-start, text-end, provider-metadata
        return None

    # ── aggregation (non-stream clients) ──

    def final_openai_response(self) -> Dict[str, Any]:
        message: Dict[str, Any] = {"role": "assistant"}
        if self.agg["content"]:
            message["content"] = "".join(self.agg["content"])
        else:
            message["content"] = ""
        finish = self.agg["finish"] or "stop"
        return {
            "id": self._id,
            "object": "chat.completion",
            "created": self._created,
            "model": self.model,
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": self.agg["usage"] or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }


# ── Client-boundary wrapper ──────────────────────────────────────────────────
# Mirrors AntigravityUpstreamClient: wraps the standard egress httpx.AsyncClient
# so every hardened send site in main.py operates unchanged on OpenAI-canonical
# payloads while the wire speaks Vercel AI SDK.

import codecs

try:  # httpx is a hard dependency of BSL; guard keeps this module importable
    # for unit tests of the pure translation functions alone.
    import httpx as _httpx
except ImportError:  # pragma: no cover
    _httpx = None


class _TranslatedStreamResponse:
    """Stream response whose byte iterators yield translated OpenAI SSE.

    Non-200 responses are never wrapped (callers see the raw error).
    All unknown attributes delegate to the inner response.
    """

    def __init__(self, inner: Any, model: str):
        self._inner = inner
        self._tr = VercelAISSETranslator(model)
        self._dec = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    @property
    def status_code(self) -> int:
        return self._inner.status_code

    @property
    def headers(self) -> Any:
        return self._inner.headers

    async def aiter_raw(self):
        async for chunk in self._inner.aiter_raw():
            for out in self._tr.feed(self._dec.decode(chunk)):
                yield out
        tail = self._dec.decode(b"", final=True)
        if tail:
            for out in self._tr.feed(tail):
                yield out
        for out in self._tr.close():
            yield out

    async def aiter_bytes(self):
        async for out in self.aiter_raw():
            yield out

    async def aread(self) -> bytes:
        data = b""
        async for out in self.aiter_raw():
            data += out
        return data

    async def aread_translator(self) -> "VercelAISSETranslator":
        """Consume the stream, returning the aggregating translator."""
        async for _ in self.aiter_raw():
            pass
        return self._tr

    async def aclose(self) -> None:
        await self._inner.aclose()


class CommandCodeUpstreamClient:
    """httpx-client facade: OpenAI in/out, Vercel AI SDK on the wire.

    ``build_request`` rewrites the OpenAI payload to the CommandCode envelope
    and points the request at ``/alpha/generate``. ``send`` transparently
    translates the Vercel SSE response back to OpenAI chunks (stream) or one
    aggregated OpenAI completion (non-stream).
    """

    def __init__(self, inner: Any, model: str):
        self._inner = inner
        self._model = model

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    # ── request direction ──

    def build_request(self, method: str, url: Any, headers: Any = None, content: Any = None, **kw: Any):
        payload: Dict[str, Any] = {}
        if isinstance(content, (bytes, bytearray)):
            try:
                payload = json.loads(content.decode("utf-8", errors="replace"))
            except (json.JSONDecodeError, ValueError):
                payload = {}
        elif isinstance(content, str):
            try:
                payload = json.loads(content)
            except (json.JSONDecodeError, ValueError):
                payload = {}
        elif isinstance(content, dict):
            payload = content

        model = None
        if isinstance(payload, dict):
            m = payload.get("model")
            if isinstance(m, str) and m.strip():
                model = m.strip()
        if not model:
            model = self._model

        envelope = openai_to_commandcode_envelope(payload, model)

        # Point at {base}/alpha/generate regardless of the original path.
        base = _httpx.URL(str(url)) if _httpx else None
        if base is not None:
            netloc = base.netloc.decode("ascii") if isinstance(base.netloc, bytes) else str(base.netloc)
            rpc = f"{base.scheme}://{netloc}{COMMANDCODE_GENERATE_PATH}"
        else:  # pragma: no cover — httpx missing, tests only
            rpc = str(url)

        hdrs = dict(headers or {})
        hdrs.setdefault("Content-Type", "application/json")
        hdrs["Accept"] = "text/event-stream"

        return self._inner.build_request(
            method,
            str(rpc),
            headers=hdrs,
            content=json.dumps(envelope, sort_keys=True).encode("utf-8"),
            **kw,
        )

    # ── response direction ──

    async def send(self, request: Any, *, stream: bool = False, **kw: Any):
        if stream:
            resp = await self._inner.send(request, stream=True, **kw)
            if resp.status_code != 200:
                return resp
            return _TranslatedStreamResponse(resp, self._model_of(request))

        # Non-stream: still use the SSE verb (the only live-verified one),
        # then aggregate through the translator into one OpenAI completion.
        resp = await self._inner.send(request, stream=True, **kw)
        if resp.status_code != 200:
            try:
                await resp.aread()
            except Exception:
                pass
            return resp
        wrapper = _TranslatedStreamResponse(resp, self._model_of(request))
        tr = await wrapper.aread_translator()
        final = tr.final_openai_response()
        body = json.dumps(final, ensure_ascii=False).encode("utf-8")
        out_headers = {k: v for k, v in resp.headers.items() if k.lower() != "content-length"}
        out_headers["Content-Type"] = "application/json"
        try:
            await resp.aclose()
        except Exception:
            pass
        return _httpx.Response(200, headers=out_headers, content=body, request=request)

    def _model_of(self, request: Any) -> str:
        try:
            env = json.loads(request.content.decode("utf-8", errors="replace"))
            # The envelope nests model under params.model.
            params = env.get("params")
            if isinstance(params, dict):
                m = params.get("model")
                if isinstance(m, str) and m.strip():
                    return m.strip()
            # Fallback: top-level model (shouldn't happen after envelope rewrite).
            m = env.get("model")
            if isinstance(m, str) and m.strip():
                return m.strip()
        except Exception:
            pass
        return self._model


def wrap_commandcode_upstream_client(inner: Any, model: str) -> CommandCodeUpstreamClient:
    """Public entry: wrap the egress client for one commandcode target model."""
    return CommandCodeUpstreamClient(inner, model)
