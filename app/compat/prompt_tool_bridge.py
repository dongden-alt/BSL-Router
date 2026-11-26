"""Prompt-based Tool Bridge for OpenAI-compatible channels that reject native `tools`.

Some reseller channels (e.g. `xingchi` for `gpt-6.1-sol` / `gpt-6-sol`) only
serve `/v1/chat/completions` and reject requests containing a native `tools`
field with HTTP 400 (`requires Responses for tool calls; this account only
supports Chat Completions`).

When enabled for a model on such a channel, this bridge:
  1. Converts declared `tools` + `tool_choice` into an explicit system instruction
     asking the model to emit tool calls inside `<tool_call>...</tool_call>`
     (or ```json_tool_call ... ```) blocks, flattens prior `tool_calls` /
     `role: "tool"` history into plain assistant/user text, and strips `tools`,
     `tool_choice`, and `parallel_tool_calls` from the outgoing payload.
  2. Parses tool call blocks back into standard OpenAI `tool_calls` on both
     non-streaming JSON responses and streaming SSE byte generators.
"""
from __future__ import annotations

import codecs
import json
import re
import time
import uuid
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple


XINGCHI_PROMPT_TOOL_MODELS = frozenset({
    "gpt-6.1-sol",
    "gpt-6-sol",
    "gpt-5.6-sol",
    "gpt-6.1-terra",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-6-luna",
})

_BRIDGE_MARKER = "[BSL Prompt Tool Bridge]"

_OPENERS = ("<tool_call>", "```json_tool_call")

_TOOL_BLOCK_RE = re.compile(
    r"<tool_call>\s*(.*?)\s*</tool_call>|```json_tool_call\s*\r?\n?(.*?)\r?\n?```",
    re.DOTALL | re.IGNORECASE,
)


def needs_prompt_tool_bridge(
    provider_name: str, target_model: str, payload: Optional[dict]
) -> bool:
    """Return True when the upstream channel/model rejects native `tools` on chat/completions."""
    if not isinstance(payload, dict):
        return False
    prov = (provider_name or "").strip().lower()
    model = (target_model or "").strip().lower()
    if prov != "xingchi" or model not in XINGCHI_PROMPT_TOOL_MODELS:
        return False
    tools = payload.get("tools")
    return isinstance(tools, list) and len(tools) > 0


def _flatten_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if item.get("type") == "text" and isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif isinstance(item.get("content"), str):
                    parts.append(item["content"])
        return "\n".join(p for p in parts if p)
    if content is None:
        return ""
    return str(content)


def _format_tool_schemas(tools: List[Any], tool_choice: Any = None) -> str:
    specs: List[str] = []
    for raw in tools:
        if not isinstance(raw, dict):
            continue
        fn = raw.get("function") if isinstance(raw.get("function"), dict) else raw
        name = str(fn.get("name") or "").strip()
        if not name:
            continue
        desc = str(fn.get("description") or "").strip()
        params = fn.get("parameters") or fn.get("input_schema") or {"type": "object", "properties": {}}
        try:
            params_s = json.dumps(params, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            params_s = "{}"
        line = f"- {name}: {desc}\n  parameters: {params_s}" if desc else f"- {name}\n  parameters: {params_s}"
        specs.append(line)

    choice_note = ""
    if tool_choice == "required" or (
        isinstance(tool_choice, dict) and tool_choice.get("type") == "any"
    ):
        choice_note = "\nYou MUST call at least one tool in this turn."
    elif isinstance(tool_choice, dict):
        fn_obj = tool_choice.get("function") or {}
        forced_name = fn_obj.get("name") or tool_choice.get("name")
        if forced_name:
            choice_note = f'\nYou MUST call tool "{forced_name}" in this turn.'

    return (
        f"{_BRIDGE_MARKER}\n"
        "You have access to the following tools. Never claim you lack tools or cannot access files/commands; "
        "call the declared tools instead.\n"
        "When calling one or more tools, output each tool call wrapped in exact tags using valid JSON:\n"
        "<tool_call>\n"
        '{"name": "<tool_name>", "arguments": {<json_arguments>}}\n'
        "</tool_call>\n"
        "Emit nothing after the final </tool_call> tag and wait for the tool result in the next user turn."
        f"{choice_note}\n\n"
        "Declared tools:\n" + "\n".join(specs)
    )


def _format_prior_tool_call(tc: Any) -> str:
    if not isinstance(tc, dict):
        return ""
    fn = tc.get("function") if isinstance(tc.get("function"), dict) else tc
    name = str(fn.get("name") or "").strip()
    args_raw = fn.get("arguments", {})
    if isinstance(args_raw, str):
        try:
            args_val = json.loads(args_raw)
        except Exception:
            args_val = args_raw
    else:
        args_val = args_raw if args_raw is not None else {}
    try:
        payload_s = json.dumps({"name": name, "arguments": args_val}, ensure_ascii=False)
    except Exception:
        payload_s = json.dumps({"name": name, "arguments": {}}, ensure_ascii=False)
    return f"<tool_call>\n{payload_s}\n</tool_call>"


def apply_prompt_tool_bridge(payload: dict) -> dict:
    """Return a copy of `payload` with `tools` encoded as prompt instructions and stripped."""
    out = dict(payload)
    tools = out.pop("tools", None) or []
    tool_choice = out.pop("tool_choice", None)
    out.pop("parallel_tool_calls", None)

    directive = _format_tool_schemas(tools, tool_choice)
    raw_msgs = out.get("messages") or []
    new_msgs: List[Dict[str, Any]] = []
    injected_directive = False

    for msg in raw_msgs:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "user").lower()
        if role in ("system", "developer"):
            text = _flatten_content(msg.get("content"))
            if not injected_directive:
                text = f"{text}\n\n{directive}".strip() if _BRIDGE_MARKER not in text else text
                injected_directive = True
            new_msgs.append({"role": role, "content": text})
        elif role == "assistant":
            text = _flatten_content(msg.get("content"))
            tc_list = msg.get("tool_calls") or []
            if isinstance(tc_list, list) and tc_list:
                rendered = "\n".join(filter(None, (_format_prior_tool_call(tc) for tc in tc_list)))
                text = f"{text}\n{rendered}".strip() if text else rendered
            if not text:
                text = "..."
            new_msgs.append({"role": role, "content": text})
        elif role == "tool":
            call_id = msg.get("tool_call_id") or msg.get("name") or "tool"
            res_text = _flatten_content(msg.get("content"))
            chunk_text = f"[Tool result ({call_id})]: {res_text}"
            if (
                new_msgs
                and new_msgs[-1].get("role") == "user"
                and isinstance(new_msgs[-1].get("content"), str)
            ):
                new_msgs[-1]["content"] = f"{new_msgs[-1]['content']}\n\n{chunk_text}".strip()
            else:
                new_msgs.append({"role": "user", "content": chunk_text})
        else:
            # Preserve multimodal user messages or plain text strings verbatim
            new_msgs.append({k: v for k, v in msg.items() if k not in ("tool_calls", "tool_call_id")})

    if not injected_directive:
        new_msgs.insert(0, {"role": "system", "content": directive})

    out["messages"] = new_msgs
    return out


def _coerce_tool_call_dicts(obj: Any) -> List[Dict[str, Any]]:
    items = obj if isinstance(obj, list) else [obj]
    results: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        fn = item.get("function") if isinstance(item.get("function"), dict) else item
        name = str(fn.get("name") or item.get("tool") or "").strip()
        if not name:
            continue
        args = fn.get("arguments")
        if args is None:
            args = fn.get("parameters", item.get("input", {}))
        if isinstance(args, str):
            args_str = args
        else:
            try:
                args_str = json.dumps(args if args is not None else {}, ensure_ascii=False)
            except Exception:
                args_str = "{}"
        results.append({
            "id": f"call_xc_{uuid.uuid4().hex[:12]}",
            "type": "function",
            "function": {"name": name, "arguments": args_str},
        })
    return results


def _parse_json_objects_from_block(raw_inner: str) -> List[Dict[str, Any]]:
    text = (raw_inner or "").strip()
    if not text:
        return []
    # Fast path: single JSON object or array
    try:
        return _coerce_tool_call_dicts(json.loads(text))
    except Exception:
        pass
    # Multi-object stream inside a single fence
    decoder = json.JSONDecoder()
    idx = 0
    found: List[Dict[str, Any]] = []
    n = len(text)
    while idx < n:
        ch = text[idx]
        if ch in "{[":
            try:
                obj, end_idx = decoder.raw_decode(text, idx)
                found.extend(_coerce_tool_call_dicts(obj))
                idx = end_idx
                continue
            except Exception:
                pass
        idx += 1
    return found


def extract_tool_calls_from_text(text: str) -> Tuple[str, List[Dict[str, Any]]]:
    """Extract `<tool_call>` / ```json_tool_call blocks from `text`, returning (clean_text, tool_calls)."""
    if not text:
        return text, []
    calls: List[Dict[str, Any]] = []
    kept_parts: List[str] = []
    last = 0
    for m in _TOOL_BLOCK_RE.finditer(text):
        inner = m.group(1) if m.group(1) is not None else m.group(2)
        parsed = _parse_json_objects_from_block(inner or "")
        if parsed:
            kept_parts.append(text[last:m.start()])
            calls.extend(parsed)
            last = m.end()
    if not calls:
        return text, []
    kept_parts.append(text[last:])
    clean = "".join(kept_parts).strip()
    return clean, calls


def parse_prompt_tool_calls_in_completion(
    openai_json: Dict[str, Any],
) -> Tuple[Dict[str, Any], bool]:
    """Post-process an OpenAI completion dict, lifting prompt-bridged tool calls."""
    try:
        choices = openai_json.get("choices")
        if not isinstance(choices, list) or not choices:
            return openai_json, False
        modified = False
        for ch in choices:
            if not isinstance(ch, dict):
                continue
            msg = ch.get("message")
            if not isinstance(msg, dict):
                continue
            if msg.get("tool_calls"):
                continue
            content = msg.get("content")
            if not isinstance(content, str) or not content:
                continue
            clean, calls = extract_tool_calls_from_text(content)
            if not calls:
                continue
            msg["tool_calls"] = calls
            msg["content"] = clean or None
            ch["finish_reason"] = "tool_calls"
            modified = True
        return openai_json, modified
    except Exception:
        return openai_json, False


def _partial_opener_hold_idx(text: str) -> int:
    """Return index where a complete or partial opener begins, or len(text) if safe to flush."""
    best = len(text)
    low = text.lower()
    for opener in _OPENERS:
        pos = low.find(opener)
        if pos != -1 and pos < best:
            best = pos
    if best < len(text):
        return best
    max_pfx = max(len(o) for o in _OPENERS) - 1
    tail_start = max(0, len(text) - max_pfx)
    for idx in range(tail_start, len(text)):
        suf = low[idx:]
        if any(o.startswith(suf) for o in _OPENERS):
            return idx
    return len(text)


def _find_block_close(text: str, start_idx: int) -> Optional[Tuple[int, int, str]]:
    """If the opener at `start_idx` has its closing marker, return (inner_start, close_end, close_tag)."""
    sub_low = text[start_idx:].lower()
    if sub_low.startswith("<tool_call>"):
        close_pos = sub_low.find("</tool_call>", len("<tool_call>"))
        if close_pos == -1:
            return None
        return (
            start_idx + len("<tool_call>"),
            start_idx + close_pos + len("</tool_call>"),
            "</tool_call>",
        )
    if sub_low.startswith("```json_tool_call"):
        close_pos = text.find("```", start_idx + len("```json_tool_call"))
        if close_pos == -1:
            return None
        return (
            start_idx + len("```json_tool_call"),
            close_pos + 3,
            "```",
        )
    return None


def _encode_sse_chunk(
    meta: Dict[str, Any],
    delta: Dict[str, Any],
    finish_reason: Optional[str] = None,
    usage: Optional[Dict[str, Any]] = None,
) -> bytes:
    payload: Dict[str, Any] = {
        "id": meta.get("id") or "chatcmpl-ptb",
        "object": "chat.completion.chunk",
        "created": meta.get("created") or int(time.time()),
        "model": meta.get("model") or "",
        "choices": [{
            "index": 0,
            "delta": delta,
            "finish_reason": finish_reason,
        }],
    }
    if usage is not None:
        payload["usage"] = usage
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


async def wrap_sse_with_prompt_tool_bridge(
    byte_stream: AsyncGenerator[bytes, None],
    model: str = "",
) -> AsyncGenerator[bytes, None]:
    """Wrap an OpenAI SSE byte stream, converting `<tool_call>` text deltas into `delta.tool_calls`."""
    meta: Dict[str, Any] = {"model": model}
    text_buf = ""
    tc_index = 0
    emitted_tools = False
    done_seen = False
    finish_seen: Optional[str] = None
    finish_emitted = False
    last_usage: Optional[Dict[str, Any]] = None

    def _drain_buffer(final: bool = False) -> List[bytes]:
        nonlocal text_buf, tc_index, emitted_tools
        out_frames: List[bytes] = []
        while text_buf:
            low = text_buf.lower()
            open_pos = -1
            for op in _OPENERS:
                p = low.find(op)
                if p != -1 and (open_pos == -1 or p < open_pos):
                    open_pos = p
            if open_pos == -1:
                if final:
                    if text_buf:
                        out_frames.append(_encode_sse_chunk(meta, {"content": text_buf}))
                        text_buf = ""
                    break
                safe_idx = _partial_opener_hold_idx(text_buf)
                if safe_idx > 0:
                    out_frames.append(_encode_sse_chunk(meta, {"content": text_buf[:safe_idx]}))
                    text_buf = text_buf[safe_idx:]
                break
            if open_pos > 0:
                prose = text_buf[:open_pos]
                if prose.strip() or not emitted_tools:
                    out_frames.append(_encode_sse_chunk(meta, {"content": prose}))
                text_buf = text_buf[open_pos:]
            closed = _find_block_close(text_buf, 0)
            if closed is None:
                if final:
                    # Handle truncated tag missing closing tag at EOF if JSON inside parses cleanly
                    inner_fallback = re.sub(r"^(?:<tool_call>|```json_tool_call)", "", text_buf, flags=re.I)
                    parsed = _parse_json_objects_from_block(inner_fallback)
                    if parsed:
                        for call in parsed:
                            call_chunk = {
                                "index": tc_index,
                                "id": call["id"],
                                "type": "function",
                                "function": call["function"],
                            }
                            out_frames.append(_encode_sse_chunk(meta, {"tool_calls": [call_chunk]}))
                            tc_index += 1
                            emitted_tools = True
                        text_buf = ""
                    else:
                        out_frames.append(_encode_sse_chunk(meta, {"content": text_buf}))
                        text_buf = ""
                break
            inner_start, close_end, close_tag = closed
            inner = text_buf[inner_start : close_end - len(close_tag)]
            parsed = _parse_json_objects_from_block(inner)
            if parsed:
                for call in parsed:
                    call_chunk = {
                        "index": tc_index,
                        "id": call["id"],
                        "type": "function",
                        "function": call["function"],
                    }
                    out_frames.append(_encode_sse_chunk(meta, {"tool_calls": [call_chunk]}))
                    tc_index += 1
                    emitted_tools = True
                text_buf = text_buf[close_end:].lstrip("\r\n")
            else:
                out_frames.append(_encode_sse_chunk(meta, {"content": text_buf[:close_end]}))
                text_buf = text_buf[close_end:]
        return out_frames

    utf8_dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
    line_buf = ""

    async for raw in byte_stream:
        chunk_bytes = raw if isinstance(raw, (bytes, bytearray)) else str(raw).encode("utf-8")
        line_buf += utf8_dec.decode(chunk_bytes, final=False)
        if "\n" not in line_buf:
            continue
        lines = line_buf.split("\n")
        line_buf = lines.pop()

        for raw_line in lines:
            stripped = raw_line.strip()
            if not stripped:
                continue
            if stripped.startswith(":"):
                yield (stripped + "\n\n").encode("utf-8")
                continue
            if not stripped.startswith("data:"):
                yield (stripped + "\n").encode("utf-8")
                continue
            data_str = stripped[5:].strip()
            if data_str == "[DONE]":
                done_seen = True
                for fr in _drain_buffer(final=True):
                    yield fr
                if not finish_emitted and (finish_seen is not None or emitted_tools):
                    fr_val = "tool_calls" if emitted_tools else (finish_seen or "stop")
                    yield _encode_sse_chunk(meta, {}, finish_reason=fr_val, usage=last_usage)
                    finish_seen = None
                    finish_emitted = True
                yield b"data: [DONE]\n\n"
                continue
            try:
                obj = json.loads(data_str)
            except Exception:
                yield (stripped + "\n\n").encode("utf-8")
                continue
            if not isinstance(obj, dict) or "choices" not in obj:
                if isinstance(obj, dict) and obj.get("usage"):
                    last_usage = obj["usage"]
                yield (stripped + "\n\n").encode("utf-8")
                continue

            if obj.get("id") and "id" not in meta:
                meta["id"] = obj["id"]
            if obj.get("created") and "created" not in meta:
                meta["created"] = obj["created"]
            if obj.get("model"):
                meta["model"] = obj["model"]
            if obj.get("usage"):
                last_usage = obj["usage"]

            choices = obj.get("choices") or []
            ch0 = choices[0] if choices and isinstance(choices[0], dict) else {}
            delta = ch0.get("delta") or {}
            fr = ch0.get("finish_reason")

            content_piece = delta.get("content")
            other_delta = {k: v for k, v in delta.items() if k != "content"}
            if other_delta:
                yield _encode_sse_chunk(meta, other_delta)

            if isinstance(content_piece, str) and content_piece:
                text_buf += content_piece
                for fr_b in _drain_buffer(final=False):
                    yield fr_b

            if fr is not None:
                finish_seen = "tool_calls" if emitted_tools else fr
                for fr_b in _drain_buffer(final=True):
                    yield fr_b
                if not finish_emitted:
                    final_fr = "tool_calls" if emitted_tools else fr
                    yield _encode_sse_chunk(meta, {}, finish_reason=final_fr, usage=last_usage)
                    finish_emitted = True
                finish_seen = None

    # EOF cleanup
    tail_str = line_buf + utf8_dec.decode(b"", final=True)
    if tail_str.strip().startswith("data:") and "[DONE]" in tail_str:
        done_seen = True
    if text_buf:
        for fr_b in _drain_buffer(final=True):
            yield fr_b
    if not finish_emitted and (finish_seen is not None or (not done_seen and emitted_tools)):
        yield _encode_sse_chunk(
            meta,
            {},
            finish_reason="tool_calls" if emitted_tools else (finish_seen or "stop"),
            usage=last_usage,
        )
        finish_emitted = True
    if not done_seen:
        yield b"data: [DONE]\n\n"
