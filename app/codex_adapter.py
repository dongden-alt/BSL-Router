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
import os
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

    Chat tool history is converted to Responses sibling items
    (function_call / function_call_output): Codex only accepts the message
    roles user|assistant|system|developer inside `input[]` (bug 2026-10-09:
    role:"tool" shipped raw hit input[6] with a 400 invalid_value).
    """
    messages = chat_body.get("messages", [])
    input_parts = []
    system_texts: list[str] = []
    has_tool_history = False

    # BUG 2026-10-09: every non-system role used to be shipped verbatim, so a
    # chat tool result (role:"tool") reached Codex as input[6] with an illegal
    # role -> 400 "Invalid value: 'tool'". Only user|assistant|system|developer
    # are legal input[] message roles; every other role is converted below or
    # coerced to user. Item shapes mirror normalizer_v2._egress_responses:
    # function_call / function_call_output are TOP-LEVEL siblings in input[],
    # never nested inside a message item.
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "")
        content = msg.get("content", "")
        if isinstance(content, list):
            content = _flatten_content(content)

        if role == "system":
            # Codex uses top-level `instructions` for the system prompt.
            # Multiple system messages concatenate in order (previously the
            # last one silently won and earlier ones were dropped).
            system_texts.append(content or "")
        elif role in ("tool", "function"):
            # Tool result — legacy role:"function" behaves identically.
            has_tool_history = True
            call_id = msg.get("tool_call_id")
            if call_id:
                input_parts.append({
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": content or "",
                })
            else:
                # A function_call_output without a call_id is itself a 400
                # (missing required field) — degrade to a user message so the
                # tool text still reaches the model on a valid payload.
                input_parts.append({
                    "role": "user",
                    "content": "[tool result]\n" + (content or ""),
                })
        elif role == "assistant":
            tool_calls = msg.get("tool_calls") or []
            if tool_calls:
                has_tool_history = True
                # Accompanying assistant text precedes the function_call
                # items (chat-history order).
                if content:
                    input_parts.append({"role": "assistant", "content": content or ""})
                for idx, tc in enumerate(tool_calls):
                    if not isinstance(tc, dict):
                        continue
                    fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                    name = fn.get("name") or tc.get("name")
                    if not name:
                        # A nameless function_call item is another 400 — skip
                        # the call rather than emit one.
                        continue
                    args = fn.get("arguments", tc.get("arguments"))
                    if not isinstance(args, str):
                        # Responses requires `arguments` to be a string.
                        args = json.dumps(args) if args is not None else "{}"
                    input_parts.append({
                        "type": "function_call",
                        "call_id": tc.get("id") or tc.get("call_id") or f"call_{idx}",
                        "name": name,
                        "arguments": args,
                    })
            else:
                input_parts.append({"role": "assistant", "content": content or ""})
        else:
            # user / developer pass through; anything unknown or empty
            # coerces to user so an unvalidated role never ships raw.
            out_role = role if role in ("user", "developer") else "user"
            input_parts.append({"role": out_role, "content": content or ""})

    instructions = "\n\n".join(system_texts) if system_texts else None

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

    # Tool-declaration forwarding (bug 2026-10-09, the input[6] role:"tool"
    # 400): a function_call history item whose function was never declared is
    # itself a likely 400, so requests that carry tool history get their
    # tools/functions converted and forwarded on the constructed body. The
    # BSL_CODEX_FORWARD_TOOLS kill-switch exists because the 2026-08-22 live
    # probe recorded in the module docstring claimed tools were unsupported —
    # that probe predates tool-history support. Forwarding is scoped to
    # requests that actually carry tool history; a bare declaration with no
    # tool traffic keeps the legacy strip behaviour (pinned by
    # test_codex_kiro_oauth.test_strips_unsupported_params).
    if has_tool_history and _codex_forward_tools_enabled():
        tools_out = _convert_chat_tools(chat_body)
        if tools_out:
            body["tools"] = tools_out

    # Strip unsupported parameters from the incoming chat_body (Codex rejects
    # them with 400). These are never written to the constructed `body` above
    # except `store`/`stream` which Codex requires — those are intentionally
    # kept — and `tools`, which the forwarding block above may re-add in the
    # Responses shape.
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


def _codex_forward_tools_enabled() -> bool:
    """BSL_CODEX_FORWARD_TOOLS kill-switch — DEFAULT ENABLED.

    Setting the env var to `0`/`false`/`no` (case-insensitive) disables tool
    forwarding and restores the pre-2026-10-09 strip-everything lane. Read
    per call so tests can monkeypatch os.environ.
    """
    raw = os.environ.get("BSL_CODEX_FORWARD_TOOLS")
    if raw is None:
        return True
    return str(raw).strip().lower() not in ("0", "false", "no")


def _convert_chat_tools(chat_body: dict) -> list[dict[str, Any]]:
    """OpenAI chat `tools` / legacy `functions` → Responses function tools.

    Shape mirrors normalizer_v2._egress_responses: flat
    {"type":"function","name",...} with `description`/`parameters` omitted
    (not null) when absent.
    """
    tools_out: list[dict[str, Any]] = []

    def _add(src: dict) -> None:
        if not src.get("name"):
            return
        t: dict[str, Any] = {"type": "function", "name": src["name"]}
        if src.get("description") is not None:
            t["description"] = src["description"]
        if src.get("parameters") is not None:
            t["parameters"] = src["parameters"]
        tools_out.append(t)

    for tool in chat_body.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function")
        _add(fn if isinstance(fn, dict) else tool)
    for fn in chat_body.get("functions") or []:
        if isinstance(fn, dict):
            _add(fn)
    return tools_out


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
        # A completed response carrying function_call output items ends a
        # tool round-trip: report finish_reason "tool_calls" so chat clients
        # run their tool loop (bug 2026-10-09, return direction). Text-only
        # responses keep the legacy "stop".
        output_items = resp_obj.get("output")
        has_function_call = isinstance(output_items, list) and any(
            isinstance(o, dict) and o.get("type") == "function_call"
            for o in output_items
        )
        chunk = {
            "id": rid,
            "object": "chat.completion.chunk",
            "created": ts,
            "model": resp_obj.get("model", "codex"),
            "choices": [{
                "index": 0,
                "delta": {},
                "finish_reason": "tool_calls" if has_function_call else "stop",
            }],
        }
        if usage:
            chunk["usage"] = {
                "prompt_tokens": usage.get("input_tokens", 0),
                "completion_tokens": usage.get("output_tokens", 0),
                "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
            }
        return chunk

    elif event_name == "response.output_item.done":
        # Codex function call finished — surface it as an OpenAI tool_calls
        # delta (bug 2026-10-09, return direction). The terminal
        # finish_reason "tool_calls" rides the response.completed chunk.
        item = data.get("item")
        if isinstance(item, dict) and item.get("type") == "function_call" and item.get("name"):
            args = item.get("arguments", "")
            if not isinstance(args, str):
                args = json.dumps(args) if args is not None else "{}"
            tool_call: dict[str, Any] = {
                "index": 0,
                "type": "function",
                "function": {"name": item["name"], "arguments": args},
            }
            call_id = item.get("call_id") or item.get("id")
            if call_id:
                tool_call["id"] = call_id
            return {
                "id": rid,
                "object": "chat.completion.chunk",
                "created": ts,
                "model": data.get("model", "codex"),
                "choices": [{
                    "index": 0,
                    "delta": {"tool_calls": [tool_call]},
                    "finish_reason": None,
                }],
            }
        return None

    elif event_name == "response.function_call_arguments.delta":
        # Incremental arguments fragment passthrough — arrives between
        # output_item.added (ignored) and output_item.done.
        frag = data.get("delta")
        if isinstance(frag, str) and frag:
            return {
                "id": rid,
                "object": "chat.completion.chunk",
                "created": ts,
                "model": data.get("model", "codex"),
                "choices": [{
                    "index": 0,
                    "delta": {"tool_calls": [{
                        "index": 0,
                        "type": "function",
                        "function": {"arguments": frag},
                    }]},
                    "finish_reason": None,
                }],
            }
        return None

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
