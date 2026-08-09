"""
Kiro protocol adapter: OpenAI ChatCompletion ↔ AWS CodeWhisperer protocol.

Kiro's /generateAssistantResponse endpoint speaks the CodeWhisperer (Q Developer)
protocol, NOT OpenAI chat format. 9Router MITM works because VS Code already sends
native CodeWhisperer format — 9Router converts it back to OpenAI internally.
BSL does the REVERSE: receives OpenAI from clients, must convert TO CodeWhisperer.

Format reference: 9Router MITM server.js gd()/cd()/dd() functions.
"""
import json
import re
from typing import Any

# Kiro expects Content-Type: application/x-amz-json-1.0 for the generateAssistantResponse endpoint
KIRO_CONTENT_TYPE = "application/x-amz-json-1.0"


def openai_to_kiro(upstream_payload: dict) -> dict[str, Any]:
    """Convert OpenAI ChatCompletion request → Kiro CodeWhisperer protocol body."""
    model = upstream_payload.get("model", "claude-3-5-sonnet-20241022")
    messages = upstream_payload.get("messages", [])
    stream = upstream_payload.get("stream", False)
    tools = upstream_payload.get("tools", None) or upstream_payload.get("functions", None)
    history_msgs = []
    current_msg_content = ""
    current_msg_tools = _convert_tools_v2(tools) if tools else []

    # Split messages: all but last → history, last → currentMessage
    for i, msg in enumerate(messages):
        role = msg.get("role", "")
        content = msg.get("content", "")
        if isinstance(content, list):
            content = _flatten_content_v2(content)

        if i == len(messages) - 1:
            current_msg_content = content or ""
        else:
            entry = _to_kiro_history_entry(role, content, msg)
            if entry:
                history_msgs.append(entry)

    body = {
        "conversationState": {
            "history": history_msgs,
            "currentMessage": {
                "userInputMessage": {
                    "content": current_msg_content,
                    "userInputMessageContext": {
                        "tools": current_msg_tools
                    }
                }
            }
        },
        "modelId": model,
    }

    if stream:
        body["stream"] = True

    # map max_tokens
    mt = upstream_payload.get("max_tokens") or upstream_payload.get("maxTokens")
    if mt:
        body["maxResponseTokens"] = int(mt)

    return body


def _to_kiro_history_entry(role: str, content: str, msg: dict) -> dict | None:
    """Convert a single OpenAI message to Kiro conversationState.history entry."""
    if not content and role != "assistant":
        return None

    if role == "user":
        return {
            "userInputMessage": {
                "content": content or "",
                "userInputMessageContext": {"tools": []}
            }
        }
    elif role == "assistant":
        entry: dict[str, Any] = {
            "assistantResponseMessage": {
                "content": content or ""
            }
        }
        tool_calls = msg.get("tool_calls")
        if tool_calls:
            entry["assistantResponseMessage"]["toolUses"] = [
                {
                    "toolUseId": tc.get("id", f"call_{i}"),
                    "name": tc.get("function", {}).get("name", ""),
                    "input": tc.get("function", {}).get("arguments", "{}"),
                }
                for i, tc in enumerate(tool_calls)
            ]
        return entry
    elif role == "tool":
        tc_id = msg.get("tool_call_id", "unknown")
        return {
            "userInputMessage": {
                "content": f"[Tool result {tc_id}]: {content}" if content else f"[Tool result {tc_id}]",
                "userInputMessageContext": {"tools": []}
            }
        }
    return None


def _flatten_content_v2(content: list) -> str:
    """Flatten content parts to text-only (Kiro doesn't support multimodal)."""
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


def _convert_tools_v2(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Convert OpenAI tools → Kiro toolSpecification list."""
    if not tools:
        return []
    result = []
    for t in tools:
        func = t.get("function", {}) if isinstance(t, dict) else {}
        result.append({
            "toolSpecification": {
                "name": func.get("name", ""),
                "description": func.get("description", ""),
                "inputSchema": {
                    "json": func.get("parameters", {})
                }
            }
        })
    return result


# ── Response Conversion ──────────────────────────────────────────────────────

# Regex to extract event name and data from Kiro SSE lines
_SSE_EVENT_RE = re.compile(r"^event:\s*(\S+)")
_SSE_DATA_RE = re.compile(r"^data:\s*(.+)")

# Map Kiro SSE event type → OpenAI SSE event
_KIRO_TO_OPENAI_EVENT = {
    "codeWhispererResponseEvent": None,  # handled inline (content chunk)
    "codeWhispererMetadataEvent": None,  # handled inline (stop+usage)
    "error": None,                        # error
}


def kiro_event_to_openai_chunk(event_name: str, data: dict) -> dict | None:
    """Convert a single Kiro SSE event to an OpenAI chat.completion.chunk."""
    if event_name == "codeWhispererResponseEvent":
        content = data.get("content", "")
        return {
            "id": f"chatcmpl-kiro-{id(data)}",
            "object": "chat.completion.chunk",
            "created": int(__import__("time").time()),
            "model": data.get("modelId", "kiro"),
            "choices": [{
                "index": 0,
                "delta": {"content": content} if content else {},
                "finish_reason": None,
            }]
        }

    elif event_name == "codeWhispererMetadataEvent":
        usage = data.get("usage", {})
        return {
            "id": f"chatcmpl-kiro-{id(data)}",
            "object": "chat.completion.chunk",
            "created": int(__import__("time").time()),
            "model": data.get("modelId", "kiro"),
            "choices": [{
                "index": 0,
                "delta": {},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": usage.get("inputTokens", 0),
                "completion_tokens": usage.get("outputTokens", 0),
                "total_tokens": usage.get("inputTokens", 0) + usage.get("outputTokens", 0),
            }
        }

    return None


def kiro_stream_to_openai_events(kiro_chunk: str) -> list[str]:
    """Convert a raw Kiro SSE chunk to OpenAI SSE lines."""
    events = []
    current_event = None
    current_data_lines = []

    for line in kiro_chunk.split("\n"):
        event_m = _SSE_EVENT_RE.match(line)
        data_m = _SSE_DATA_RE.match(line)

        if event_m:
            # flush previous
            if current_event and current_data_lines:
                oai = _emit_openai_event(current_event, current_data_lines)
                if oai:
                    events.append(oai)
            current_event = event_m.group(1)
            current_data_lines = []
        elif data_m:
            current_data_lines.append(data_m.group(1))

    # flush last
    if current_event and current_data_lines:
        oai = _emit_openai_event(current_event, current_data_lines)
        if oai:
            events.append(oai)

    return events


def _emit_openai_event(event_name: str, data_lines: list[str]) -> str | None:
    """Convert Kiro SSE event to OpenAI SSE, return SSE text or None."""
    try:
        data = json.loads("".join(data_lines))
    except (json.JSONDecodeError, ValueError):
        return None

    if event_name == "codeWhispererResponseEvent":
        content = data.get("content", "")
        oai = {
            "id": f"chatcmpl-kiro-{id(data)}",
            "object": "chat.completion.chunk",
            "created": int(__import__("time").time()),
            "model": data.get("modelId", "kiro"),
            "choices": [{
                "index": 0,
                "delta": {"content": content} if content else {},
                "finish_reason": None,
            }]
        }
        return f"data: {json.dumps(oai, separators=(',', ':'))}\n\n"

    elif event_name == "codeWhispererMetadataEvent":
        usage = data.get("usage", {})
        oai = {
            "id": f"chatcmpl-kiro-{id(data)}",
            "object": "chat.completion.chunk",
            "created": int(__import__("time").time()),
            "model": data.get("modelId", "kiro"),
            "choices": [{
                "index": 0,
                "delta": {},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": usage.get("inputTokens", 0),
                "completion_tokens": usage.get("outputTokens", 0),
                "total_tokens": usage.get("inputTokens", 0) + usage.get("outputTokens", 0),
            }
        }
        return f"data: {json.dumps(oai, separators=(',', ':'))}\n\n"

    return None


def kiro_nonstream_to_openai(data: dict) -> dict:
    """Convert Kiro non-streaming response to OpenAI response format."""
    content = data.get("response", {}).get("content", "")
    model = data.get("modelId", "kiro")
    return {
        "id": f"chatcmpl-kiro-{id(data)}",
        "object": "chat.completion",
        "created": int(__import__("time").time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": content,
            },
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": data.get("response", {}).get("usage", {}).get("inputTokens", 0),
            "completion_tokens": data.get("response", {}).get("usage", {}).get("outputTokens", 0),
            "total_tokens": (
                data.get("response", {}).get("usage", {}).get("inputTokens", 0)
                + data.get("response", {}).get("usage", {}).get("outputTokens", 0)
            ),
        }
    }


async def kiro_raw_to_openai_sse(raw_iter):
    """Async generator: convert Kiro raw SSE bytes to OpenAI SSE bytes.

    Kiro SSE -> OpenAI SSE event-by-event. Handles arbitrary byte boundaries
    from httpx aiter_raw (may split mid-line).
    """
    buf = b""
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
                    oai = kiro_event_to_openai_chunk(current_event, dj)
                    if oai:
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
        oai = kiro_event_to_openai_chunk(current_event, dj)
        if oai:
            yield f"data: {json.dumps(oai, separators=(',', ':'))}\n\n".encode("utf-8")
