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
from app.compat.adapters.gemini import bound_tool_call_id


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
            call_id = msg.get("tool_call_id")
            if call_id:
                input_parts.append({
                    "type": "function_call_output",
                    "call_id": bound_tool_call_id(call_id),
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
                        "call_id": bound_tool_call_id(tc.get("id") or tc.get("call_id") or f"call_{idx}"),
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

    # Prompt caching affinity for Codex /responses: preserve explicit client key
    # or derive a deterministic session key from (model, instructions, first user/dev anchor)
    # so multi-turn agentic coding sessions with store=False still hit OpenAI's prompt cache.
    explicit_cache_key = chat_body.get("prompt_cache_key")
    if isinstance(explicit_cache_key, str) and explicit_cache_key.strip():
        body["prompt_cache_key"] = explicit_cache_key.strip()
    else:
        first_anchor = ""
        for part in input_parts:
            if isinstance(part, dict) and part.get("role") in ("user", "developer") and part.get("content"):
                first_anchor = str(part.get("content"))[:2048]
                break
        if instructions or first_anchor:
            import hashlib
            digest = hashlib.sha256(
                f"{body['model']}|{instructions or ''}|{first_anchor}".encode("utf-8", errors="ignore")
            ).hexdigest()[:24]
            body["prompt_cache_key"] = f"bsl-codex-{digest}"

    # Reasoning effort
    effort = _resolve_effort(chat_body)
    body["reasoning"] = {"effort": effort}

    # Tool-declaration forwarding: a bare first-turn declaration has NO
    # tool history by definition, and Codex must see the declarations to
    # ever emit a function_call — so tools forward on EVERY request that
    # carries them, history or not (bug 2026-10-13: the old has_tool_history
    # gate made first-turn tool use impossible, and turn 1 is always
    # history-free). The BSL_CODEX_FORWARD_TOOLS kill-switch remains the
    # single off-switch.
    if _codex_forward_tools_enabled():
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
        "prompt_cache_key",
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
            from app.compat.adapters.gemini import coerce_json_schema_numerics
            t["parameters"] = coerce_json_schema_numerics(src["parameters"])
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

def classify_codex_error_status(err_obj: Any, msg: str = "", default_status: int = 503) -> int:
    """Classify a Codex SSE `response.failed` / `error` event into an HTTP status code.

    Prevents deterministic 400 validation/schema errors (e.g. `Invalid schema for function ...`)
    or 401 auth errors from being misclassified as transient 503/502 server errors that
    trigger `[CodexOverloadRetry]` 4x on the same account and trip the circuit breaker.
    """
    code_str = ""
    if isinstance(err_obj, dict):
        code_str = f"{err_obj.get('code') or ''} {err_obj.get('type') or ''}".lower()
    elif err_obj is not None:
        code_str = str(err_obj).lower()
    msg_low = (msg or "").lower()
    combined = f"{code_str} {msg_low}"
    if any(m in combined for m in ("rate", "quota", "limit_reached", "usage_limit", "too_many", "credit")):
        return 429
    if any(m in combined for m in (
        "invalid schema", "invalid_schema", "invalid_request", "invalid_function",
        "not of type", "unsupported", "model_not_found", "context_length",
        "string_above_max_length", "missing required", "bad_request",
    )):
        return 400
    if any(m in combined for m in ("invalid_api_key", "unauthorized", "authentication", "token_expired", "auth")):
        return 401
    if "limit" in msg_low or "token" in code_str:
        return 429 if "limit" in msg_low else 401
    return default_status


def responses_json_to_openai(resp: dict, model: str) -> dict[str, Any]:
    """Assemble an OpenAI chat.completion from a completed Responses object.

    `resp` is the full response object found inside `response.completed`.
    """
    text_parts = []
    reasoning_parts = []
    tool_calls_out: list[dict] = []
    for item in resp.get("output", []):
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message":
            for c in item.get("content", []):
                if isinstance(c, dict) and c.get("type") in ("output_text", "text"):
                    t = c.get("text", "")
                    if t:
                        text_parts.append(t)
        elif item.get("type") == "reasoning":
            for s in item.get("summary") or []:
                if isinstance(s, dict) and s.get("text"):
                    reasoning_parts.append(str(s.get("text")))
            for c in item.get("content") or []:
                if isinstance(c, dict) and c.get("text"):
                    reasoning_parts.append(str(c.get("text")))
        elif item.get("type") == "function_call" and item.get("name"):
            args = item.get("arguments", "")
            if not isinstance(args, str):
                args = json.dumps(args) if args is not None else "{}"
            tc: dict[str, Any] = {
                "type": "function",
                "function": {"name": item["name"], "arguments": args},
            }
            call_id = item.get("call_id") or item.get("id")
            if call_id:
                tc["id"] = call_id
            tool_calls_out.append(tc)

    usage = resp.get("usage", {}) or {}
    out_details = usage.get("output_tokens_details") or {}
    reasoning_tokens = int(out_details.get("reasoning_tokens", 0) or 0)

    message: dict[str, Any] = {
        "role": "assistant",
        "content": "".join(text_parts),
    }
    if reasoning_parts:
        message["reasoning_content"] = "\n".join(reasoning_parts)
    if tool_calls_out:
        message["tool_calls"] = tool_calls_out

    finish_reason = "tool_calls" if tool_calls_out else "stop"
    if resp.get("status") == "incomplete":
        finish_reason = "length"
        if not message["content"] and not reasoning_parts and reasoning_tokens > 0:
            message["reasoning_content"] = f"[reasoning_tokens={reasoning_tokens}]"

    in_details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
    cached_tokens = int(in_details.get("cached_tokens", 0) or 0) if isinstance(in_details, dict) else 0
    out_usage: dict[str, Any] = {
        "prompt_tokens": usage.get("input_tokens", 0),
        "completion_tokens": usage.get("output_tokens", 0),
        "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
    }
    if cached_tokens > 0:
        out_usage["prompt_tokens_details"] = {"cached_tokens": cached_tokens}
    if reasoning_tokens > 0:
        out_usage["completion_tokens_details"] = {"reasoning_tokens": reasoning_tokens}

    result: dict[str, Any] = {
        "id": resp.get("id", f"resp-codex-{id(resp)}"),
        "object": "chat.completion",
        "created": int(__import__("time").time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": finish_reason,
        }],
        "usage": out_usage,
    }
    if resp.get("error"):
        result["error"] = resp.get("error")
    return result


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
    # Per-stream tool-call index ledger (PARALLEL-BATCH INDEX FIX
    # 2026-10-15): call_id -> stable OpenAI tool_call index, plus the next
    # free index. Owned here and threaded into every conversion call so
    # parallel done-events never share index 0 (which collapsed N calls
    # into one downstream accumulator slot and concatenated their args).
    tc_state: dict = {"call_index": {}, "next_index": 0}

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

            if line_str.startswith("event:"):
                current_event = line_str[6:].strip()
            elif line_str.startswith("data:"):
                d_part = line_str[5:]
                if d_part.startswith(" "):
                    d_part = d_part[1:]
                current_data_parts.append(d_part)
            elif line_str == "":
                if current_data_parts:
                    d = "".join(current_data_parts)
                    try:
                        dj = json.loads(d)
                    except json.JSONDecodeError:
                        dj = {}
                    ev_name = (current_event or (dj.get("type") if isinstance(dj, dict) else "") or "").strip()
                    converted = _codex_event_to_openai_chunk(ev_name, dj, tc_state) if ev_name else None
                    for oai in (converted if isinstance(converted, list) else [converted]):
                        if oai is not None:
                            if first_delta:
                                yield f"data: {json.dumps(_role_chunk(oai), separators=(',', ':'))}\n\n".encode("utf-8")
                                first_delta = False
                            yield f"data: {json.dumps(oai, separators=(',', ':'))}\n\n".encode("utf-8")
                current_event = None
                current_data_parts = []

    # flush remaining
    if current_data_parts:
        d = "".join(current_data_parts)
        try:
            dj = json.loads(d)
        except json.JSONDecodeError:
            dj = {}
        ev_name = (current_event or (dj.get("type") if isinstance(dj, dict) else "") or "").strip()
        converted = _codex_event_to_openai_chunk(ev_name, dj, tc_state) if ev_name else None
        for oai in (converted if isinstance(converted, list) else [converted]):
            if oai is not None:
                if first_delta:
                    yield f"data: {json.dumps(_role_chunk(oai), separators=(',', ':'))}\n\n".encode("utf-8")
                    first_delta = False
                yield f"data: {json.dumps(oai, separators=(',', ':'))}\n\n".encode("utf-8")


def _role_chunk(content_chunk: dict) -> dict:
    """Wrap a content chunk so the first delta carries the assistant role."""
    role_d = dict(content_chunk)
    role_d["delta"] = {"role": "assistant"}
    role_d["choices"] = [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]
    return role_d


def _error_chunks(rid: str, ts: int, response: dict, message: str) -> list[dict]:
    """Build a visible error text delta followed by an ``error`` finish."""
    model = response.get("model", "codex") if isinstance(response, dict) else "codex"
    base = {
        "id": rid,
        "object": "chat.completion.chunk",
        "created": ts,
        "model": model,
    }
    return [
        {**base, "choices": [{
            "index": 0,
            "delta": {"content": f"[BSL] upstream error: {message}"},
            "finish_reason": None,
        }]},
        {**base, "choices": [{
            "index": 0,
            "delta": {},
            "finish_reason": "error",
        }]},
    ]


def _codex_event_to_openai_chunk(event_name: str, data: dict, tc_state: dict | None = None) -> dict | None:
    """Convert a Codex Responses SSE event to an OpenAI chunk dict, or None to ignore.

    Failed/incomplete events use ``error`` as the terminal finish reason.

    ``tc_state`` is the per-stream tool-call index ledger owned by
    responses_sse_to_openai_sse (``{"call_index": {call_id: i}, "next_index": n}``).
    When omitted (direct/unit calls) a fresh ledger is used, so the first
    call still gets index 0 — the pinned single-call contract is unchanged.
    """
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

    elif event_name in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta"):
        delta_text = data.get("delta", "") if isinstance(data, dict) else ""
        if not isinstance(delta_text, str) or not delta_text:
            return None
        return {
            "id": rid,
            "object": "chat.completion.chunk",
            "created": ts,
            "model": data.get("model", "codex"),
            "choices": [{
                "index": 0,
                "delta": {"reasoning_content": delta_text},
                "finish_reason": None,
            }],
        }

    elif event_name == "error":
        err_obj = data.get("error") if isinstance(data, dict) and isinstance(data.get("error"), dict) else data
        message = (
            (err_obj.get("message") or err_obj.get("code") or "error")
            if isinstance(err_obj, dict)
            else str(err_obj or "error")
        )
        return _error_chunks(rid, ts, data if isinstance(data, dict) else {}, str(message))

    elif event_name in ("response.failed", "response.incomplete"):
        resp_obj = data.get("response", data)
        if not isinstance(resp_obj, dict):
            resp_obj = {}
        details = resp_obj.get("error" if event_name == "response.failed" else "incomplete_details")
        message = details.get("message" if event_name == "response.failed" else "reason") if isinstance(details, dict) else None
        return _error_chunks(rid, ts, resp_obj, message or event_name)

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
            in_details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
            cached_tokens = int(in_details.get("cached_tokens", 0) or 0) if isinstance(in_details, dict) else 0
            chunk["usage"] = {
                "prompt_tokens": usage.get("input_tokens", 0),
                "completion_tokens": usage.get("output_tokens", 0),
                "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
                **({"prompt_tokens_details": {"cached_tokens": cached_tokens}} if cached_tokens > 0 else {}),
            }
        return chunk

    elif event_name == "response.output_item.done":
        # Codex function calls are emitted from response.output_item.done with
        # the complete item; response.completed supplies the terminal frame.
        item = data.get("item")
        if isinstance(item, dict) and item.get("type") == "function_call" and item.get("name"):
            args = item.get("arguments", "")
            if not isinstance(args, str):
                args = json.dumps(args) if args is not None else "{}"
            # PARALLEL-BATCH INDEX FIX (2026-10-15): every done-event used
            # to carry index 0, so N parallel calls collapsed into ONE
            # Gemini accumulator slot and their arguments concatenated --
            # the "Extra data" drop loop on client=gemini -> provider=codex
            # (gpt-5.6-sol) traffic. Allocate a per-stream, per-call_id
            # index instead; the first call still gets 0, preserving the
            # pinned single-call chunk shape.
            call_id = item.get("call_id") or item.get("id")
            if tc_state is None:
                tc_state = {}
            _idx_map = tc_state.setdefault("call_index", {})
            if call_id and call_id in _idx_map:
                tc_index = _idx_map[call_id]
            else:
                tc_index = tc_state.get("next_index", 0)
                tc_state["next_index"] = tc_index + 1
                if call_id:
                    _idx_map[call_id] = tc_index
            tool_call: dict[str, Any] = {
                "index": tc_index,
                "type": "function",
                "function": {"name": item["name"], "arguments": args},
            }
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
        # DROPPED (2026-10-13): per the Responses spec, response.output_item.done
        # carries the COMPLETE function_call item including full `arguments`.
        # Forwarding these incremental fragments as well meant the Gemini
        # accumulator received fragments + the full payload and concatenated
        # both into one corrupt double-args buffer. The done-event alone is
        # the complete, authoritative emission.
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
    last_response_obj = None
    delta_text_parts = []
    done_text_parts = []
    reasoning_text_parts = []
    function_call_items: list[dict] = []
    stream_error = None
    buf = b""
    current_event = None
    current_data_parts = []

    def _consume(event_name: str, data_str: str):
        nonlocal completed, last_response_obj, stream_error
        try:
            dj = json.loads(data_str) if data_str else {}
        except json.JSONDecodeError:
            dj = {}
        ev = (event_name or (dj.get("type") if isinstance(dj, dict) else "") or "").strip()
        if isinstance(dj, dict) and isinstance(dj.get("response"), dict):
            last_response_obj = dj["response"]
        if ev == "response.output_text.delta":
            d = dj.get("delta") if isinstance(dj, dict) else None
            if isinstance(d, str) and d:
                delta_text_parts.append(d)
        elif ev == "response.output_text.done":
            t = dj.get("text") if isinstance(dj, dict) else None
            if isinstance(t, str) and t:
                done_text_parts.append(t)
        elif ev == "response.content_part.done":
            part = dj.get("part") if isinstance(dj, dict) else None
            if isinstance(part, dict) and isinstance(part.get("text"), str) and part.get("text"):
                done_text_parts.append(part["text"])
        elif ev in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta"):
            d = dj.get("delta") if isinstance(dj, dict) else None
            if isinstance(d, str) and d:
                reasoning_text_parts.append(d)
        elif ev in ("response.reasoning_summary_text.done", "response.reasoning_text.done"):
            t = dj.get("text") if isinstance(dj, dict) else None
            if isinstance(t, str) and t and not reasoning_text_parts:
                reasoning_text_parts.append(t)
        elif ev == "response.output_item.done":
            it = dj.get("item") if isinstance(dj, dict) else None
            if isinstance(it, dict):
                if it.get("type") == "function_call" and it.get("name"):
                    function_call_items.append(dict(it))
                elif it.get("type") == "message":
                    for c in it.get("content") or []:
                        if isinstance(c, dict) and isinstance(c.get("text"), str) and c.get("text"):
                            done_text_parts.append(c["text"])
                elif it.get("type") == "reasoning":
                    for s in it.get("summary") or []:
                        if isinstance(s, dict) and isinstance(s.get("text"), str) and s.get("text"):
                            reasoning_text_parts.append(s["text"])
        elif ev in ("response.completed", "response.done"):
            completed = (dj.get("response") if isinstance(dj, dict) else None) or (dj if isinstance(dj, dict) else {})
        elif ev in ("response.failed", "response.incomplete"):
            resp_obj = (dj.get("response") if isinstance(dj, dict) else None) or (dj if isinstance(dj, dict) else {})
            if isinstance(resp_obj, dict):
                completed = dict(resp_obj)
                if ev == "response.failed" and not completed.get("status"):
                    completed["status"] = "failed"
                elif ev == "response.incomplete" and not completed.get("status"):
                    completed["status"] = "incomplete"
        elif ev == "error":
            err_obj = (dj.get("error") if isinstance(dj, dict) else None) or dj
            stream_error = err_obj

    def _process_line(line_str: str):
        nonlocal current_event, current_data_parts
        if line_str.startswith("event:"):
            current_event = line_str[6:].strip()
        elif line_str.startswith("data:"):
            d_part = line_str[5:]
            if d_part.startswith(" "):
                d_part = d_part[1:]
            current_data_parts.append(d_part)
        elif line_str == "":
            if current_data_parts:
                _consume(current_event or "", "".join(current_data_parts))
            current_event = None
            current_data_parts = []

    async for chunk in raw_iter:
        if not chunk:
            continue
        buf += chunk
        while b"\n" in buf:
            idx = buf.find(b"\n")
            line = buf[:idx]
            buf = buf[idx + 1:]
            line = line.rstrip(b"\r")
            _process_line(line.decode("utf-8", errors="replace"))

    if buf:
        _process_line(buf.rstrip(b"\r").decode("utf-8", errors="replace"))
    # flush
    if current_data_parts:
        _consume(current_event or "", "".join(current_data_parts))

    if completed is None:
        if isinstance(last_response_obj, dict):
            completed = dict(last_response_obj)
        else:
            completed = {"id": "codex-empty", "output": [], "usage": {}, "status": "completed"}
    if stream_error and isinstance(completed, dict) and not completed.get("error"):
        completed["error"] = stream_error
        completed["status"] = "failed"
    if not delta_text_parts and done_text_parts:
        delta_text_parts = [done_text_parts[-1]]
    if isinstance(completed, dict) and reasoning_text_parts:
        output = completed.get("output")
        if not isinstance(output, list):
            output = []
        if not any(isinstance(o, dict) and o.get("type") == "reasoning" for o in output):
            output.append({
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": "".join(reasoning_text_parts)}],
            })
            completed["output"] = output
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
    if isinstance(completed, dict) and function_call_items:
        # Graft accumulated function_call items onto the completed response's
        # output so responses_json_to_openai (which only walks completed
        # output) emits them as message.tool_calls on the non-stream lane.
        output = completed.get("output")
        if not isinstance(output, list):
            output = []
        existing = {
            (o.get("call_id") or o.get("id"))
            for o in output
            if isinstance(o, dict) and o.get("type") == "function_call"
        }
        for fc in function_call_items:
            key = fc.get("call_id") or fc.get("id")
            if key is None or key not in existing:
                output.append(fc)
        completed["output"] = output
    return completed
