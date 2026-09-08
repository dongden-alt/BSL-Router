"""N4 — Responses API thread store (previous_response_id emulation, DEFAULT OFF).

The OpenAI Responses API is stateful: clients send ``previous_response_id``
and expect the server to prepend that response's output items to the new
request. BSL's ``/v1/responses`` is stateless — today the id is silently
dropped (verified: zero references in the codebase), breaking multi-turn
Responses clients. This module adds OPTIONAL server-side threading.

Config gate (mirrors the ``tools.normalizer_v2`` / ``tools.fel`` gates):

    (config or {}).get("tools", {}).get("responses_thread_store", {}) →
        {"enabled": false}

OFF (default) → the module is inert: the endpoint never calls into it, no
storage is allocated beyond the empty singleton, no bytes are written. No
disk writers exist at all — the store is pure in-memory.

Store: LRU + TTL map of response_id → output items (the list of Responses
API items the response produced: message items with output_text, function_call
items; ``encrypted_content`` on any reasoning item would ride along verbatim
because items are stored untouched). Cap 256 threads, LRU evict; TTL 24h with
lazy expiry on access plus a periodic sweep on write (cheap monotonic
timestamp check). All mutation runs under ONE asyncio.Lock, rebound when the
running loop changes (asyncio primitives cannot outlive their loop — pytest
creates one loop per test). Fail-open everywhere: any error degrades to the
unthreaded behavior, never a failed request.

Chain semantics (matches the real API): a response created with
``previous_response_id=A`` stores its own items plus a link to A. Stitching
for ``previous_response_id=B`` walks B → A → … (cycle-guarded, depth-capped)
so the FULL conversation history is prepended, oldest first. A broken link
(evicted/expired middle response) degrades to the collected suffix.

Response-side capture:
  * Non-streaming: the chat-completions JSONResponse from
    ``_process_chat_completion`` is parsed and re-emitted as a Responses-shaped
    response object (id ``resp_<hex>``, ``object: "response"``, ``output``:
    [...]) so the client can reference it; the output items are stored under
    that id. Anything non-2xx, non-JSON, or unparseable passes through
    untouched (fail-open).
  * Streaming: chat SSE chunks are passed through BYTE-FOR-BYTE (zero
    client-visible change); an accumulator tees the deltas and stores the
    assembled output items under the chat completion id the client actually
    sees in the stream (``chatcmpl-...``), so that id is a valid
    ``previous_response_id`` on the next turn.

Import-order safe (no app.main import — that would be circular). Stitched
message items deliberately use plain-string / output_text content because
``ResponsesConverter.responses_to_chat`` (the live converter on this endpoint)
extracts exactly those shapes.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections import OrderedDict
from typing import Any, List, Optional, Tuple

from starlette.responses import JSONResponse, Response, StreamingResponse

MAX_THREADS = 256
TTL_SECONDS = 24 * 3600.0
SWEEP_INTERVAL_SECONDS = 60.0
MAX_CHAIN_DEPTH = 64


# ── Store ────────────────────────────────────────────────────────────────────

class ResponsesThreadStore:
    """In-memory LRU+TTL map: response_id → (monotonic_ts, items, prev_id).

    One asyncio.Lock guards all mutation. The lock is rebound when the running
    loop changes; without a running loop the (synchronous, event-loop-atomic)
    internals run directly so pure unit tests never need a loop.
    """

    def __init__(self, max_threads: int = MAX_THREADS, ttl_seconds: float = TTL_SECONDS):
        self.max_threads = max(1, int(max_threads))
        self.ttl_seconds = max(0.0, float(ttl_seconds))
        self._entries: "OrderedDict[str, Tuple[float, List[dict], Optional[str]]]" = OrderedDict()
        self._lock: Optional[asyncio.Lock] = None
        self._lock_loop = None
        self._last_sweep = time.monotonic()

    # -- lock plumbing (loop-fresh, mirrors normalizer_shadow's per-loop task) --
    def _get_lock(self) -> Optional[asyncio.Lock]:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None
        if self._lock is None or self._lock_loop is not loop:
            self._lock = asyncio.Lock()
            self._lock_loop = loop
        return self._lock

    # -- internals (sync; the event loop serializes them while the lock is held) --
    def _sweep(self, now: float) -> None:
        """Drop expired entries. Full scan over ≤256 timestamps — cheap."""
        if self.ttl_seconds <= 0:
            self._entries.clear()
        else:
            dead = [k for k, (ts, _items, _prev) in self._entries.items()
                    if (now - ts) > self.ttl_seconds]
            for k in dead:
                self._entries.pop(k, None)
        self._last_sweep = now

    def _put_sync(self, response_id: str, output_items: List[dict],
                  previous_response_id: Optional[str]) -> None:
        if not isinstance(response_id, str) or not response_id:
            return
        if not isinstance(output_items, list) or not output_items:
            return
        now = time.monotonic()
        if (now - self._last_sweep) >= SWEEP_INTERVAL_SECONDS or len(self._entries) >= self.max_threads:
            self._sweep(now)
        self._entries.pop(response_id, None)  # re-put refreshes + moves to MRU
        while len(self._entries) >= self.max_threads:
            self._entries.popitem(last=False)  # LRU evict
        prev = previous_response_id if isinstance(previous_response_id, str) and previous_response_id else None
        self._entries[response_id] = (now, list(output_items), prev)
        self._entries.move_to_end(response_id)

    def _get_sync(self, response_id: str) -> Optional[Tuple[List[dict], Optional[str]]]:
        if not isinstance(response_id, str) or not response_id:
            return None
        entry = self._entries.get(response_id)
        if entry is None:
            return None
        ts, items, prev = entry
        if self.ttl_seconds <= 0 or (time.monotonic() - ts) > self.ttl_seconds:
            self._entries.pop(response_id, None)  # lazy expiry on access
            return None
        self._entries.move_to_end(response_id)  # LRU touch on hit
        return list(items), prev  # copy: callers may mutate their list freely

    # -- public API --
    async def put(self, response_id: str, output_items: List[dict],
                  *, previous_response_id: Optional[str] = None) -> None:
        lock = self._get_lock()
        if lock is not None:
            async with lock:
                self._put_sync(response_id, output_items, previous_response_id)
        else:
            self._put_sync(response_id, output_items, previous_response_id)

    async def get(self, response_id: str) -> Optional[List[dict]]:
        """Stored output items for one id (None if unknown/expired)."""
        found = await self.lookup(response_id)
        return found[0] if found is not None else None

    async def lookup(self, response_id: str) -> Optional[Tuple[List[dict], Optional[str]]]:
        """(items, previous_response_id) for one id; LRU touch + lazy TTL."""
        lock = self._get_lock()
        if lock is not None:
            async with lock:
                return self._get_sync(response_id)
        return self._get_sync(response_id)

    async def collect_chain(self, response_id: str) -> List[dict]:
        """Walk the previous_response_id chain, oldest → newest, flattened.

        Cycle-guarded and depth-capped; a missing/expired link yields the
        collected suffix (fail-open). Unknown id → [].
        """
        chunks: List[List[dict]] = []
        seen = set()
        cur = response_id
        depth = 0
        while isinstance(cur, str) and cur and cur not in seen and depth < MAX_CHAIN_DEPTH:
            seen.add(cur)
            depth += 1
            found = await self.lookup(cur)
            if found is None:
                break
            items, cur = found
            chunks.append(items)
        flat: List[dict] = []
        for chunk in reversed(chunks):  # collected newest-first → chronological
            flat.extend(chunk)
        return flat

    def __len__(self) -> int:
        return len(self._entries)


_STORE = ResponsesThreadStore()


def get_store() -> ResponsesThreadStore:
    return _STORE


def reset_store(max_threads: int = MAX_THREADS, ttl_seconds: float = TTL_SECONDS) -> ResponsesThreadStore:
    """Swap in a fresh store (test isolation)."""
    global _STORE
    _STORE = ResponsesThreadStore(max_threads, ttl_seconds)
    return _STORE


# ── Gate ─────────────────────────────────────────────────────────────────────

def thread_store_enabled(config: Any) -> bool:
    """tools.responses_thread_store.enabled, default FALSE. Never raises."""
    try:
        return bool(
            (((config or {}).get("tools") or {}).get("responses_thread_store") or {}).get("enabled", False)
        )
    except Exception:
        return False


# ── Stitch-in (request path) ────────────────────────────────────────────────

async def stitch_previous_response(body: Any, *, store: Optional[ResponsesThreadStore] = None) -> Any:
    """Prepend the stored chain's output items to ``body["input"]``.

    Per Responses API semantics ``previous_response_id`` means the parent
    response's output items come before the new input items. Unknown/expired
    id → body returned unchanged (fail-open; BSL never 404s on it). The input
    body is never mutated — a shallow copy carries the new ``input`` list.
    """
    if not isinstance(body, dict):
        return body
    prev_id = body.get("previous_response_id")
    if not isinstance(prev_id, str) or not prev_id.strip():
        return body
    st = store if store is not None else _STORE
    try:
        items = await st.collect_chain(prev_id.strip())
    except Exception:
        return body
    if not items:
        return body
    inp = body.get("input")
    if isinstance(inp, str):
        # Plain string content keeps ResponsesConverter.responses_to_chat
        # lossless (its list branch only extracts output_text parts).
        new_input = list(items) + [{"type": "message", "role": "user", "content": inp}]
    elif isinstance(inp, list):
        new_input = list(items) + list(inp)
    else:
        new_input = list(items)
    stitched = dict(body)
    stitched["input"] = new_input
    return stitched


# ── Response-object builders (pure) ─────────────────────────────────────────

def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _output_items_from_chat(chat_payload: Any) -> List[dict]:
    """chat.completion payload → Responses output items (message + function_call).

    Reasoning items are NOT synthesized: chat completions never carry
    encrypted_content, so there is nothing to preserve on this path.
    """
    items: List[dict] = []
    if not isinstance(chat_payload, dict):
        return items
    choices = chat_payload.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    content = message.get("content")
    text = ""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = "".join(
            p.get("text", "") for p in content
            if isinstance(p, dict) and p.get("type") in ("text", "output_text")
        )
    if text:
        items.append({
            "type": "message",
            "id": _new_id("msg"),
            "role": "assistant",
            "content": [{"type": "output_text", "text": text}],
        })
    for tc in message.get("tool_calls") or []:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
        name = str(fn.get("name") or "").strip()
        # Nameless tool calls get rejected upstream (same 400 class as the
        # responses_api guard) and are worthless as history — skip.
        if not name:
            continue
        args = fn.get("arguments")
        if isinstance(args, str) and args:
            args_out = args
        elif args is None:
            args_out = "{}"
        else:
            try:
                args_out = json.dumps(args, default=str)
            except Exception:
                args_out = "{}"
        items.append({
            "type": "function_call",
            "id": _new_id("fc"),
            "call_id": tc.get("id") or _new_id("call"),
            "name": name,
            "arguments": args_out,
        })
    return items


def build_response_object(resp_id: str, chat_payload: Any, *,
                          previous_response_id: Optional[str] = None) -> dict:
    """chat.completion payload → the object the Responses API would return."""
    payload = chat_payload if isinstance(chat_payload, dict) else {}
    usage = payload.get("usage")
    usage_out = None
    if isinstance(usage, dict):
        ptd = usage.get("prompt_tokens_details") if isinstance(usage.get("prompt_tokens_details"), dict) else {}
        ctd = usage.get("completion_tokens_details") if isinstance(usage.get("completion_tokens_details"), dict) else {}
        usage_out = {
            "input_tokens": usage.get("prompt_tokens"),
            "input_tokens_details": {"cached_tokens": ptd.get("cached_tokens")},
            "output_tokens": usage.get("completion_tokens"),
            "output_tokens_details": {"reasoning_tokens": ctd.get("reasoning_tokens")},
            "total_tokens": usage.get("total_tokens"),
        }
    return {
        "id": resp_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "model": payload.get("model") or "",
        "output": _output_items_from_chat(payload),
        "parallel_tool_calls": True,
        "previous_response_id": previous_response_id if isinstance(previous_response_id, str) else None,
        "usage": usage_out,
    }


# ── Capture-out (response path) ─────────────────────────────────────────────

def _passthrough_headers(response: Response) -> Optional[dict]:
    """Original headers minus content-length (the replacement body differs)."""
    try:
        return {k: v for k, v in response.headers.items() if str(k).lower() != "content-length"}
    except Exception:
        return None


class _StreamAccumulator:
    """Best-effort chat-SSE tee: parses complete ``data:`` lines from byte
    chunks (buffering partial lines) and accumulates the assistant text and
    streamed tool-call deltas. Never raises on malformed input."""

    def __init__(self) -> None:
        self.text: List[str] = []
        self.tools: "OrderedDict[int, dict]" = OrderedDict()
        self.chat_id: Optional[str] = None
        self.model: Optional[str] = None
        self._buf = bytearray()

    def feed(self, chunk: Any) -> None:
        if isinstance(chunk, (bytes, bytearray, memoryview)):
            self._buf += bytes(chunk)
        elif isinstance(chunk, str):
            self._buf += chunk.encode("utf-8", errors="replace")
        else:
            return
        while True:
            nl = self._buf.find(b"\n")
            if nl < 0:
                break
            line = bytes(self._buf[:nl]).strip(b"\r").decode("utf-8", errors="replace")
            del self._buf[:nl + 1]
            self._line(line)

    def _line(self, line: str) -> None:
        if not line.startswith("data:"):
            return
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            return
        try:
            evt = json.loads(payload)
        except Exception:
            return
        if not isinstance(evt, dict):
            return
        mid = evt.get("id")
        if isinstance(mid, str) and mid and not self.chat_id:
            self.chat_id = mid
        model = evt.get("model")
        if isinstance(model, str) and model and not self.model:
            self.model = model
        choices = evt.get("choices")
        if not isinstance(choices, list):
            return
        for ch in choices:
            if not isinstance(ch, dict):
                continue
            delta = ch.get("delta") if isinstance(ch.get("delta"), dict) else {}
            content = delta.get("content")
            if isinstance(content, str) and content:
                self.text.append(content)
            elif isinstance(content, list):
                for p in content:
                    if isinstance(p, dict) and isinstance(p.get("text"), str):
                        self.text.append(p["text"])
            for tc in delta.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                idx = tc.get("index") if isinstance(tc.get("index"), int) else len(self.tools)
                slot = self.tools.setdefault(idx, {"id": None, "name": None, "args": []})
                if isinstance(tc.get("id"), str) and tc["id"]:
                    slot["id"] = tc["id"]
                fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                fname = fn.get("name")
                if isinstance(fname, str) and fname:
                    slot["name"] = (slot["name"] or "") + fname
                fargs = fn.get("arguments")
                if isinstance(fargs, str):
                    slot["args"].append(fargs)

    def finish(self) -> Tuple[str, List[dict]]:
        """(store_key, output items). The key is the chat completion id the
        client saw in the SSE chunks (a generated resp_ id is the fallback)."""
        items: List[dict] = []
        text = "".join(self.text)
        if text:
            items.append({
                "type": "message",
                "id": _new_id("msg"),
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            })
        for _idx, slot in self.tools.items():
            name = (slot["name"] or "").strip()
            if not name:
                continue
            items.append({
                "type": "function_call",
                "id": _new_id("fc"),
                "call_id": slot["id"] or _new_id("call"),
                "name": name,
                "arguments": "".join(slot["args"]) or "{}",
            })
        return self.chat_id or _new_id("resp"), items


def _wrap_stream_capture(response: StreamingResponse,
                         store: ResponsesThreadStore,
                         previous_response_id: Optional[str]) -> Response:
    iterator = response.body_iterator
    if not hasattr(iterator, "__aiter__"):
        return response  # sync iterator: cannot tee safely — pass through

    async def _tee():
        acc = _StreamAccumulator()
        async for chunk in iterator:
            yield chunk  # verbatim — the client contract is untouched
            try:
                acc.feed(chunk)
            except Exception:
                pass  # capture must never break the stream
        # Natural completion only: a disconnected client (GeneratorExit) or an
        # upstream error propagates past this point and stores nothing.
        try:
            key, items = acc.finish()
            if items:
                await store.put(key, items, previous_response_id=previous_response_id)
        except Exception:
            pass

    try:
        return StreamingResponse(
            _tee(),
            status_code=response.status_code,
            headers={k: v for k, v in response.headers.items()},
            media_type=response.media_type,
            background=response.background,
        )
    except Exception:
        return response


async def capture_response(response: Any, *, previous_response_id: Optional[str] = None,
                           store: Optional[ResponsesThreadStore] = None) -> Any:
    """Store the response's output items so future turns can thread onto it.

    Non-streaming 2xx JSON chat responses are re-emitted as Responses-shaped
    response objects (id ``resp_<hex>``) with their items stored under that id.
    Streaming responses are teed (verbatim passthrough) and stored under the
    chat completion id visible in the SSE chunks. Everything else — errors,
    non-JSON bodies, unparseable payloads — passes through untouched.
    """
    if response is None:
        return response
    st = store if store is not None else _STORE
    if isinstance(response, StreamingResponse):
        return _wrap_stream_capture(response, st, previous_response_id)
    if isinstance(response, Response) and 200 <= response.status_code < 300:
        try:
            if "json" not in str(response.headers.get("content-type", "")).lower():
                return response
            payload = json.loads(response.body)
        except Exception:
            return response
        if not isinstance(payload, dict) or not isinstance(payload.get("choices"), list):
            return response
        resp_id = _new_id("resp")
        obj = build_response_object(resp_id, payload, previous_response_id=previous_response_id)
        try:
            if obj["output"]:
                await st.put(resp_id, obj["output"], previous_response_id=previous_response_id)
        except Exception:
            pass  # storage failure must not lose the response
        try:
            return JSONResponse(obj, status_code=response.status_code,
                                headers=_passthrough_headers(response))
        except Exception:
            return response
    return response
