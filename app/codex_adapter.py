"""
Codex Responses-API adapter: OpenAI ChatCompletion ↔ Codex Responses API.

Codex's `https://chatgpt.com/backend-api/codex` only serves the Responses API
(`POST /responses`). It rejects /chat/completions with a 403 HTML page.

Per live probe (2026-08-22):
  - /responses requires `store: false` and `stream: true` (always).
  - `max_output_tokens`, `temperature`, `top_p`, etc. are unsupported.
  - reasoning.effort supported values: none / low / medium / high / xhigh.
  - SSE events: response.created, response.output_text.delta, response.completed.

Format reference: OpenAI Responses API (response object shape).
"""
import json
import re
from typing import Any


# ── Effort normalization ─────────────────────────────────────────────────────

_EFFORT_MAP: dict[str, str] = {
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "max": "xhigh",
    "xhigh": "xhigh",
    "none": "none",
}

# Keys inspected for an existing effort signal on the upstream payload.
_EFFORT_KEYS = ("reasoning_effort", "reasoning", "thinking", "x_reasoning")


def _resolve_effort(upstream_payload: dict) -> str:
    """Resolve reasoning.effort from the payload's existing effort signal.

    BSL's thinking pipeline writes effort onto the payload before the
    provider-format transform (see the kiro transform point in main.py).
    We inspect the conventional keys in order and normalize to the Codex
    vocabulary: minimal→low, max→xhigh, default medium.
    """
    for key in _EFFORT_KEYS:
        val = upstream_payload.get(key)
        if val is None:
            continue
        if isinstance(val, dict):
            # `reasoning` / `thinking` are nested: look for an `effort` sub-key.
            sub = val.get("effort") or val.get("level")
            if sub:
                val = sub
            else:
                continue
        s = str(val).strip().lower()
        if s in ("", "auto", "off"):
            continue
        # Budget-style values (e.g. "32k") are not effort words — skip.
        if re.fullmatch(r"\d+\s*k?", s):
            continue
        mapped = _EFFORT_MAP.get(s)
        if mapped:
            return mapped
        # Unknown word: pass through if it's already a valid Codex level.
        if s in _EFFORT_MAP.values():
            return s
    return "medium"


# ── Request Conversion ───────────────────────────────────────────────────────

def openai_to_responses(chat_body: dict) -> dict[str, Any]:
    """Convert OpenAI ChatCompletion request → Codex Responses API body.

    Codex upstream MUST always receive `stream: true` and `store: false`
    regardless of what the client sent — the caller (main.py) buffers the
    SSE for non-stream clients.
    """
    messages = chat_body.get("messages", [])
    input_parts = []
    instructions = None

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if isinstance(content, list):
            content = _flatten_content(content)

        if role == "system":
            # Codex uses top-level `instructions` for the system prompt.
            instructions = content or ""
        else:
            input_parts.append({"role": role, "content": content or ""})

    body: dict[str, Any] = {
        "input": input_parts,
        "model": chat_body.get("model", "gpt-5.5"),
        "store": False,
        "stream": True,
    }

    if instructions is not None:
        body["instructions"] = instructions

    # Reasoning effort
    effort = _resolve_effort(chat_body)
    body["reasoning"] = {"effort": effort}

    # Strip unsupported parameters from the incoming chat_body (Codex rejects
    # them with 400). These are never written to the constructed `body` above
    # except `store`/`stream` which Codex requires — those are intentionally
    # kept.
    for key in (
        "max_tokens",
        "max_output_tokens",
        "temperature",
        "top_p",
        "frequency_penalty",
        "presence_penalty",
        "stop",
        "logprobs",
        "n",
        "response_format",
        "tools",
        "functions",
        "stream_options",
        "messages",
        "reasoning_effort",
        "thinking",
        "output_config",
    ):
        chat_body.pop(key, None)

    return body


def _flatten_content(content: list) -> str:
    """Flatten content parts to text-only."""
    texts = []
    for part in content:
        if isinstance(part, dict):
            t = part.get("type", "")
            if t == "text":
                texts.append(part.get("text", ""))
            elif t == "image_url":
                texts.append("[Image]")
            else:
                texts.append(str(part))
        else:
            texts.append(str(part))
    return "\n".join(texts)


# ── Response Conversion ──────────────────────────────────────────────────────

def responses_json_to_openai(resp: dict, model: str) -> dict[str, Any]:
    """Assemble an OpenAI chat.completion from a completed Responses object.

    `resp` is the full response object found inside `response.completed`.
    """
    text_parts = []
    # Walk output items for text content.
    for item in resp.get("output", []):
        if item.get("type") == "message":
            for c in item.get("content", []):
                if c.get("type") == "output_text":
                    text_parts.append(c.get("text", ""))

    usage = resp.get("usage", {})
    status = resp.get("status", "")

    return {
        "id": resp.get("id", f"resp-codex-{id(resp)}"),
        "object": "chat.completion",
        "created": int(__import__("time").time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "".join(text_parts),
            },
            "finish_reason": "stop" if status == "completed" else "stop",
        }],
        "usage": {
            "prompt_tokens": usage.get("input_tokens", 0),
            "completion_tokens": usage.get("output_tokens", 0),
            "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
        },
    }


# ── SSE Conversion ───────────────────────────────────────────────────────────

_SSE_EVENT_RE = re.compile(r"^event:\s*(\S+)")
_SSE_DATA_RE = re.compile(r"^data:\s*(.+)")


async def responses_sse_to_openai_sse(raw_iter):
    """Async generator: convert Codex raw SSE bytes → OpenAI chat.completion.chunk SSE.

    Handles arbitrary byte boundaries from httpx aiter_raw (may split mid-line).
    First content delta is preceded by a role-only chunk {delta:{role:assistant}}.
    """
    buf = b""
    current_event = None
    current_data_parts = []
    first_delta = True

    async for chunk in raw_iter:
        if not chunk:
            continue
        buf += chunk
        while b"\n" in buf:
            idx = buf.find(b"\n")
            line = buf[:idx]
            buf = buf[idx + 1:]
            line = line.rstrip(b"\r")
            line_str = line.decode("utf-8", errors="replace")

            if line_str.startswith("event: "):
                current_event = line_str[7:]
            elif line_str.startswith("data: "):
                current_data_parts.append(line_str[6:])
            elif line_str == "":
                if current_event and current_data_parts:
                    d = "".join(current_data_parts)
                    try:
                        dj = json.loads(d)
                    except json.JSONDecodeError:
                        dj = {}
                    oai = _codex_event_to_openai_chunk(current_event, dj)
                    if oai is not None:
                        if first_delta:
                            # Inject role chunk before first content.
                            yield f"data: {json.dumps(_role_chunk(oai), separators=(',', ':'))}\n\n".encode("utf-8")
                            first_delta = False
                        yield f"data: {json.dumps(oai, separators=(',', ':'))}\n\n".encode("utf-8")
                current_event = None
                current_data_parts = []

    # flush remaining
    if current_event and current_data_parts:
        d = "".join(current_data_parts)
        try:
            dj = json.loads(d)
        except json.JSONDecodeError:
            dj = {}
        oai = _codex_event_to_openai_chunk(current_event, dj)
        if oai is not None:
            if first_delta:
                yield f"data: {json.dumps(_role_chunk(oai), separators=(',', ':'))}\n\n".encode("utf-8")
            yield f"data: {json.dumps(oai, separators=(',', ':'))}\n\n".encode("utf-8")


def _role_chunk(content_chunk: dict) -> dict:
    """Wrap a content chunk so the first delta carries the assistant role."""
    role_d = dict(content_chunk)
    role_d["delta"] = {"role": "assistant"}
    role_d["choices"] = [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]
    return role_d


def _codex_event_to_openai_chunk(event_name: str, data: dict) -> dict | None:
    """Convert a Codex Responses SSE event to an OpenAI chunk dict, or None to ignore."""
    ts = int(__import__("time").time())
    rid = f"resp-codex-{id(data)}"

    if event_name == "response.output_text.delta":
        delta_text = data.get("delta", "")
        if not delta_text:
            # Try nested delta (response.output_item.added shape).
            item = data.get("item")
            if isinstance(item, dict):
                content_list = item.get("content", [])
                if isinstance(content_list, list) and content_list:
                    delta_text = content_list[0].get("text", "")
        return {
            "id": rid,
            "object": "chat.completion.chunk",
            "created": ts,
            "model": data.get("model", "codex"),
            "choices": [{
                "index": 0,
                "delta": {"content": delta_text} if delta_text else {},
                "finish_reason": None,
            }],
        }

    elif event_name == "response.completed":
        # Final chunk with finish_reason and optional usage.
        resp_obj = data.get("response", data)
        usage = resp_obj.get("usage", {})
        chunk = {
            "id": rid,
            "object": "chat.completion.chunk",
            "created": ts,
            "model": resp_obj.get("model", "codex"),
            "choices": [{
                "index": 0,
                "delta": {},
                "finish_reason": "stop",
            }],
        }
        if usage:
            chunk["usage"] = {
                "prompt_tokens": usage.get("input_tokens", 0),
                "completion_tokens": usage.get("output_tokens", 0),
                "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
            }
        return chunk

    # Ignore all other event types (response.created, response.output_item.added, etc.)
    return None


async def assemble_responses_from_sse(raw_iter) -> dict:
    """Buffer all Codex SSE frames and return the `response.completed` response object.

    Used for the non-stream client path: upstream always streams (store:false
    + stream:true forced), so we accumulate and synthesize a single JSON response.

    CODEX FIX (2026-08-22): the completed object's own output[] items do not
    reliably expose message/output_text in the shape responses_json_to_openai
    walks (usage arrived but content stayed empty -> zombie_empty_response).
    The DELTA events are the proven shape — the streaming converter consumes
    them live — so we accumulate response.output_text.delta text here and
    graft it onto the completed object as a synthetic message item.
    """
    completed = None
    delta_text_parts = []
    buf = b""
    current_event = None
    current_data_parts = []

    def _consume(event_name: str, data_str: str):
        nonlocal completed
        try:
            dj = json.loads(data_str) if data_str else {}
        except json.JSONDecodeError:
            dj = {}
        if event_name == "response.output_text.delta":
            d = dj.get("delta") if isinstance(dj, dict) else None
            if isinstance(d, str) and d:
                delta_text_parts.append(d)
        elif event_name == "response.completed":
            completed = (dj.get("response") if isinstance(dj, dict) else None) or (dj if isinstance(dj, dict) else {})

    async for chunk in raw_iter:
        if not chunk:
            continue
        buf += chunk
        while b"\n" in buf:
            idx = buf.find(b"\n")
            line = buf[:idx]
            buf = buf[idx + 1:]
            line = line.rstrip(b"\r")
            line_str = line.decode("utf-8", errors="replace")

            if line_str.startswith("event: "):
                current_event = line_str[7:]
            elif line_str.startswith("data: "):
                current_data_parts.append(line_str[6:])
            elif line_str == "":
                if current_event and current_data_parts:
                    _consume(current_event, "".join(current_data_parts))
                current_event = None
                current_data_parts = []

    # flush
    if current_event and current_data_parts:
        _consume(current_event, "".join(current_data_parts))

    if completed is None:
        # Synthesize an empty completed object so callers don't crash.
        completed = {"id": "codex-empty", "output": [], "usage": {}, "status": "completed"}
    if isinstance(completed, dict) and delta_text_parts:
        text = "".join(delta_text_parts)
        output = completed.get("output")
        if not isinstance(output, list):
            output = []
        # Prepend a synthetic message item carrying the delta-derived text.
        output = [{
            "id": "msg-bsl-assembled",
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        }] + [o for o in output if isinstance(o, dict)]
        completed["output"] = output
    return completed
