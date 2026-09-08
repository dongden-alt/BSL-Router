"""
normalizer_v2 — dialect-agnostic request normalization core.

Canonical intermediate representation (CanonicalRequest) that any ingress
dialect converts TO and any egress dialect converts FROM. Built so a
single request body can be repaired, inspected, or re-targeted without
chaining pairwise converters.

Safety contract (mirrors app/middleware/anthropic_tools.py):
  1. Pure transformation; no I/O, no retries, no threads.
  2. Never raise on MESSAGE-LEVEL content: a malformed message degrades
     to an ``unknown`` part carrying the original verbatim. Only structurally
     impossible calls (unknown dialect, non-dict body → handled, not raised)
     are treated as errors.
  3. Unknown content shapes are never dropped — they ride along in
     extras["passthrough"] or as ``unknown`` parts.
"""

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

DIALECT_OPENAI_CHAT = "openai-chat"
DIALECT_RESPONSES = "responses"
DIALECT_GEMINI = "gemini"


# ── Canonical representation ────────────────────────────────────────────────

@dataclass
class CanonicalRequest:
    """Dialect-neutral request. All fields defaulted so a bare instance is
    a valid (empty) request."""
    model: str = ""
    system: List[Dict[str, Any]] = field(default_factory=list)
    messages: List[Dict[str, Any]] = field(default_factory=list)  # {"role": str, "parts": list[dict]}
    tools: List[Dict[str, Any]] = field(default_factory=list)
    tool_choice: Any = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    top_k: Optional[float] = None
    max_tokens: Optional[float] = None
    stop: Optional[List[str]] = None
    response_format: Any = None
    reasoning: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    cache_breakpoints: List[Dict[str, Any]] = field(default_factory=list)
    extras: Dict[str, Any] = field(default_factory=dict)


# ── Ingress (dialect → canonical) ───────────────────────────────────────────

def _split_data_url(url: str) -> tuple:
    """data:<media_type>;base64,<payload> → (data, media_type). Non-data URLs
    return (None, None) — caller keeps url as-is."""
    if not isinstance(url, str) or not url.startswith("data:"):
        return None, None
    header, _, payload = url.partition(",")
    media_type = header[5:].split(";")[0] or None
    return payload or None, media_type


def _ingress_content_parts(content: Any) -> List[Dict[str, Any]]:
    """OpenAI content (str | list[part-dicts]) → canonical part list."""
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if not isinstance(content, list):
        return [{"type": "unknown", "raw": content}]
    parts: List[Dict[str, Any]] = []
    for item in content:
        if not isinstance(item, dict):
            parts.append({"type": "unknown", "raw": item})
            continue
        itype = item.get("type")
        if itype == "text":
            parts.append({"type": "text", "text": item.get("text", "")})
        elif itype == "image_url":
            url = (item.get("image_url") or {}).get("url")
            if isinstance(url, str) and url.startswith("data:"):
                data, media_type = _split_data_url(url)
                parts.append({"type": "image", "url": None, "media_type": media_type, "data": data})
            else:
                parts.append({"type": "image", "url": url, "media_type": None, "data": None})
        elif itype == "input_audio":
            parts.append({
                "type": "audio",
                "data": item.get("input_audio", {}).get("data"),
                "media_type": item.get("input_audio", {}).get("format"),
            })
        elif itype == "file":
            finfo = item.get("file") or {}
            parts.append({
                "type": "document",
                "url": finfo.get("file_data") if isinstance(finfo.get("file_data"), str) and finfo.get("file_data", "").startswith("http") else None,
                "data": finfo.get("file_data") if isinstance(finfo.get("file_data"), str) and not finfo.get("file_data", "").startswith("http") else None,
                "media_type": finfo.get("mime_type"),
                "filename": finfo.get("filename"),
            })
        else:
            parts.append({"type": "unknown", "raw": item})
    return parts


_KNOWN_OPENAI_KEYS = frozenset({
    "model", "messages", "tools", "tool_choice", "temperature", "top_p",
    "max_tokens", "stop", "response_format",
})


def _ingress_openai_chat(body: Dict[str, Any]) -> CanonicalRequest:
    can = CanonicalRequest()
    can.model = body.get("model") or ""
    can.tools = body.get("tools") or []
    can.tool_choice = body.get("tool_choice")
    can.temperature = body.get("temperature")
    can.top_p = body.get("top_p")
    can.max_tokens = body.get("max_tokens")
    can.stop = body.get("stop")
    can.response_format = body.get("response_format")

    for msg in body.get("messages") or []:
        if not isinstance(msg, dict):
            can.messages.append({"role": "assistant", "parts": [{"type": "unknown", "raw": msg}]})
            continue
        role = msg.get("role") or "user"
        parts: List[Dict[str, Any]] = []
        if role == "system" or role == "developer":
            parts.extend(_ingress_content_parts(msg.get("content")))
            can.system.extend(parts)
            continue
        if role == "tool":
            parts.append({
                "type": "tool_result",
                "tool_use_id": msg.get("tool_call_id"),
                "content": _ingress_content_parts(msg.get("content")),
                "is_error": None,
            })
            can.messages.append({"role": "user", "parts": parts})
            continue
        parts.extend(_ingress_content_parts(msg.get("content")))
        for tc in msg.get("tool_calls") or []:
            if not isinstance(tc, dict):
                parts.append({"type": "unknown", "raw": tc})
                continue
            fn = tc.get("function") or {}
            raw_args = fn.get("arguments")
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) and raw_args.strip() else {}
            except Exception:
                args = {}
            parts.append({
                "type": "tool_use",
                "id": tc.get("id"),
                "name": fn.get("name"),
                "arguments": args,
                "raw_arguments": raw_args if isinstance(raw_args, str) else None,
            })
        can.messages.append({"role": role, "parts": parts})

    unknown = {k: v for k, v in body.items() if k not in _KNOWN_OPENAI_KEYS}
    if unknown:
        can.extras.setdefault(DIALECT_OPENAI_CHAT, {}).update(unknown)
    return can


def _responses_reasoning_text(item: Dict[str, Any]) -> str:
    """Joined text of a Responses reasoning item's summary/content lists."""
    bits: List[str] = []
    for key in ("summary", "content"):
        val = item.get(key)
        if isinstance(val, str):
            bits.append(val)
        elif isinstance(val, list):
            for s in val:
                if isinstance(s, str):
                    bits.append(s)
                elif isinstance(s, dict) and isinstance(s.get("text"), str):
                    bits.append(s["text"])
    return "".join(bits)


def _ingress_responses_content(content: Any) -> List[Dict[str, Any]]:
    """Responses content (str | list[input_text/input_image/input_file/output_text]) → canonical parts."""
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if not isinstance(content, list):
        return [{"type": "unknown", "raw": content}]
    parts: List[Dict[str, Any]] = []
    for item in content:
        if not isinstance(item, dict):
            parts.append({"type": "unknown", "raw": item})
            continue
        itype = item.get("type")
        if itype in ("input_text", "output_text"):
            text = item.get("text")
            parts.append({"type": "text", "text": text if isinstance(text, str) else ""})
        elif itype == "input_image":
            url = item.get("image_url")
            if isinstance(url, dict):
                url = url.get("url")
            if isinstance(url, str) and url.startswith("data:"):
                data, media_type = _split_data_url(url)
                parts.append({"type": "image", "url": None, "media_type": media_type, "data": data})
            else:
                parts.append({"type": "image", "url": url, "media_type": None, "data": None})
        elif itype == "input_file":
            fdata = item.get("file_data")
            fname = item.get("filename")
            fmime = item.get("mime_type")
            if isinstance(fdata, str) and fdata.startswith("data:"):
                data, media_type = _split_data_url(fdata)
                parts.append({"type": "document", "url": None, "data": data,
                              "media_type": fmime or media_type, "filename": fname})
            elif isinstance(fdata, str) and fdata.startswith("http"):
                parts.append({"type": "document", "url": fdata, "data": None,
                              "media_type": fmime, "filename": fname})
            else:
                parts.append({"type": "document", "url": None, "data": fdata,
                              "media_type": fmime, "filename": fname, "file_id": item.get("file_id")})
        else:
            parts.append({"type": "unknown", "raw": item})
    return parts


_KNOWN_RESPONSES_KEYS = frozenset({
    "model", "instructions", "input", "tools", "tool_choice", "temperature",
    "top_p", "max_output_tokens", "reasoning", "text",
})


def _ingress_responses(body: Dict[str, Any]) -> CanonicalRequest:
    can = CanonicalRequest()
    can.model = body.get("model") or ""

    instructions = body.get("instructions")
    if isinstance(instructions, str) and instructions:
        can.system.append({"type": "text", "text": instructions})

    inp = body.get("input")
    if isinstance(inp, str):
        if inp:
            can.messages.append({"role": "user", "parts": [{"type": "text", "text": inp}]})
    elif isinstance(inp, list):
        for item in inp:
            if not isinstance(item, dict):
                can.messages.append({"role": "assistant", "parts": [{"type": "unknown", "raw": item}]})
                continue
            itype = item.get("type")
            if itype == "message":
                role = item.get("role") or "user"
                parts = _ingress_responses_content(item.get("content"))
                if role in ("system", "developer"):
                    can.system.extend(parts)
                else:
                    can.messages.append({"role": role, "parts": parts})
            elif itype == "function_call":
                raw_args = item.get("arguments")
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) and raw_args.strip() else {}
                except Exception:
                    args = {}
                can.messages.append({"role": "assistant", "parts": [{
                    "type": "tool_use",
                    "id": item.get("call_id"),
                    "name": item.get("name"),
                    "arguments": args,
                    "raw_arguments": raw_args if isinstance(raw_args, str) else None,
                }]})
            elif itype == "function_call_output":
                output = item.get("output")
                content = _ingress_responses_content(output) if isinstance(output, list) else output
                can.messages.append({"role": "user", "parts": [{
                    "type": "tool_result",
                    "tool_use_id": item.get("call_id"),
                    "content": content,
                    "is_error": None,
                }]})
            elif itype == "reasoning":
                can.messages.append({"role": "assistant", "parts": [{
                    "type": "thinking",
                    "thinking": _responses_reasoning_text(item),
                    "signature": None,
                }]})
                # preserve the raw item (incl. encrypted_content) verbatim
                can.extras.setdefault(DIALECT_RESPONSES, {}).setdefault("reasoning_items", []).append(item)
            else:
                can.messages.append({"role": "assistant", "parts": [{"type": "unknown", "raw": item}]})

    for tool in body.get("tools") or []:
        if isinstance(tool, dict) and tool.get("type") == "function":
            fn: Dict[str, Any] = {"name": tool.get("name")}
            if tool.get("description") is not None:
                fn["description"] = tool.get("description")
            if tool.get("parameters") is not None:
                fn["parameters"] = tool.get("parameters")
            can.tools.append({"type": "function", "function": fn})
            if "strict" in tool:
                can.extras.setdefault(DIALECT_RESPONSES, {}).setdefault("tool_strict", []).append(
                    {"name": tool.get("name"), "strict": tool.get("strict")})
        else:
            can.extras.setdefault(DIALECT_RESPONSES, {}).setdefault("tools_unmapped", []).append(tool)

    can.tool_choice = body.get("tool_choice")
    can.temperature = body.get("temperature")
    can.top_p = body.get("top_p")
    can.max_tokens = body.get("max_output_tokens")

    reasoning = body.get("reasoning")
    if isinstance(reasoning, dict):
        can.reasoning = {
            "enabled": bool(reasoning.get("effort")),
            "effort": reasoning.get("effort"),
            "summary": reasoning.get("summary"),
        }
    text_cfg = body.get("text")
    if isinstance(text_cfg, dict) and "format" in text_cfg:
        can.response_format = text_cfg.get("format")

    unknown = {k: v for k, v in body.items() if k not in _KNOWN_RESPONSES_KEYS}
    if unknown:
        can.extras.setdefault(DIALECT_RESPONSES, {}).update(unknown)
    return can


_KNOWN_GEMINI_KEYS = frozenset({
    "model", "systemInstruction", "contents", "tools", "toolConfig",
    "generationConfig",
})
_KNOWN_GEMINI_GEN_KEYS = frozenset({
    "temperature", "topP", "topK", "maxOutputTokens", "stopSequences",
    "responseMimeType", "responseSchema", "thinkingConfig",
})


def _gemini_inline_part(idata: Dict[str, Any]) -> Dict[str, Any]:
    """inlineData payload → canonical image/document part (image/* → image, else document)."""
    mime = idata.get("mimeType")
    base = {
        "url": None,
        "data": idata.get("data"),
        "media_type": mime,
        "filename": idata.get("fileName") if isinstance(idata.get("fileName"), str) else None,
    }
    if isinstance(mime, str) and mime.startswith("image/"):
        return {"type": "image", **base}
    return {"type": "document", **base}


def _ingress_gemini(body: Dict[str, Any]) -> CanonicalRequest:
    can = CanonicalRequest()
    can.model = body.get("model") or ""
    gem_extra = can.extras.setdefault(DIALECT_GEMINI, {})

    sysinstr = body.get("systemInstruction")
    if isinstance(sysinstr, str) and sysinstr:
        can.system.append({"type": "text", "text": sysinstr})
    elif isinstance(sysinstr, dict):
        for p in sysinstr.get("parts") or []:
            if isinstance(p, dict) and isinstance(p.get("text"), str):
                can.system.append({"type": "text", "text": p["text"]})
            else:
                can.system.append({"type": "unknown", "raw": p})

    for content in body.get("contents") or []:
        if not isinstance(content, dict):
            can.messages.append({"role": "assistant", "parts": [{"type": "unknown", "raw": content}]})
            continue
        role = content.get("role") or "user"
        if role == "model":
            role = "assistant"
        parts: List[Dict[str, Any]] = []
        for p in content.get("parts") or []:
            if not isinstance(p, dict):
                parts.append({"type": "unknown", "raw": p})
                continue
            if set(p.keys()) == {"thoughtSignature"}:
                # attach to the previous part; no previous → degrade to unknown
                if parts:
                    parts[-1].setdefault("extras", {})["thought_signature"] = p.get("thoughtSignature")
                else:
                    parts.append({"type": "unknown", "raw": p})
                continue
            if "functionCall" in p:
                fc = p.get("functionCall") or {}
                args = fc.get("args")
                parts.append({
                    "type": "tool_use",
                    "id": "gemini_fc:" + str(fc.get("name")),
                    "name": fc.get("name"),
                    "arguments": args if isinstance(args, dict) else {},
                    "raw_arguments": None,
                })
            elif "functionResponse" in p:
                fr = p.get("functionResponse") or {}
                parts.append({
                    "type": "tool_result",
                    "tool_use_id": "gemini_fc:" + str(fr.get("name")),
                    "content": fr.get("response"),
                    "is_error": None,
                })
            elif p.get("thought") is True:
                text = p.get("text")
                parts.append({"type": "thinking", "thinking": text if isinstance(text, str) else "", "signature": None})
            elif "inlineData" in p:
                parts.append(_gemini_inline_part(p.get("inlineData") or {}))
            elif "fileData" in p:
                fd = p.get("fileData") or {}
                mime = fd.get("mimeType")
                base = {
                    "url": fd.get("fileUri"),
                    "data": None,
                    "media_type": mime,
                    "filename": fd.get("fileName") if isinstance(fd.get("fileName"), str) else None,
                }
                if isinstance(mime, str) and mime.startswith("image/"):
                    parts.append({"type": "image", **base})
                else:
                    parts.append({"type": "document", **base})
            elif isinstance(p.get("text"), str):
                parts.append({"type": "text", "text": p["text"]})
            else:
                parts.append({"type": "unknown", "raw": p})
        can.messages.append({"role": role, "parts": parts})

    for tool in body.get("tools") or []:
        if isinstance(tool, dict) and isinstance(tool.get("functionDeclarations"), list):
            for fd in tool["functionDeclarations"]:
                if not isinstance(fd, dict):
                    continue
                fn: Dict[str, Any] = {"name": fd.get("name")}
                if fd.get("description") is not None:
                    fn["description"] = fd.get("description")
                if fd.get("parameters") is not None:
                    fn["parameters"] = fd.get("parameters")
                can.tools.append({"type": "function", "function": fn})
        else:
            gem_extra.setdefault("tools_unmapped", []).append(tool)

    tool_cfg = body.get("toolConfig")
    if isinstance(tool_cfg, dict):
        fcc = tool_cfg.get("functionCallingConfig")
        if isinstance(fcc, dict):
            mode = fcc.get("mode")
            allowed = fcc.get("allowedFunctionNames")
            if mode == "AUTO":
                can.tool_choice = "auto"
            elif mode == "NONE":
                can.tool_choice = "none"
            elif mode == "ANY":
                if isinstance(allowed, list) and allowed:
                    can.tool_choice = {"type": "function", "function": {"name": allowed[0]}}
                else:
                    can.tool_choice = "required"
            else:
                can.tool_choice = mode

    gen = body.get("generationConfig")
    if isinstance(gen, dict):
        if gen.get("temperature") is not None:
            can.temperature = gen.get("temperature")
        if gen.get("topP") is not None:
            can.top_p = gen.get("topP")
        if gen.get("topK") is not None:
            can.top_k = gen.get("topK")
        if gen.get("maxOutputTokens") is not None:
            can.max_tokens = gen.get("maxOutputTokens")
        if gen.get("stopSequences") is not None:
            can.stop = gen.get("stopSequences")
        if gen.get("responseMimeType") is not None or gen.get("responseSchema") is not None:
            can.response_format = {"mime_type": gen.get("responseMimeType"), "schema": gen.get("responseSchema")}
        tcfg = gen.get("thinkingConfig")
        if isinstance(tcfg, dict):
            budget = tcfg.get("thinkingBudget")
            if isinstance(budget, (int, float)) and budget > 0:
                can.reasoning = {"enabled": True, "budget_tokens": budget}
            if tcfg.get("includeThoughts") is True:
                gem_extra["include_thoughts"] = True
            tc_rest = {k: v for k, v in tcfg.items() if k not in ("thinkingBudget", "includeThoughts")}
            if tc_rest:
                gem_extra.setdefault("generation_config", {}).setdefault("thinkingConfig", {}).update(tc_rest)
        gen_rest = {k: v for k, v in gen.items() if k not in _KNOWN_GEMINI_GEN_KEYS}
        if gen_rest:
            gem_extra.setdefault("generation_config", {}).update(gen_rest)

    unknown = {k: v for k, v in body.items() if k not in _KNOWN_GEMINI_KEYS}
    if unknown:
        gem_extra.update(unknown)
    return can


_INGRESS: Dict[str, Callable[[Dict[str, Any]], CanonicalRequest]] = {
    DIALECT_OPENAI_CHAT: _ingress_openai_chat,
    DIALECT_RESPONSES: _ingress_responses,
    DIALECT_GEMINI: _ingress_gemini,
}


# ── Egress (canonical → dialect) ────────────────────────────────────────────

def _egress_text_join(parts: List[Dict[str, Any]]) -> str:
    return "".join(p.get("text", "") for p in parts if p.get("type") == "text")


def _egress_openai_chat(canonical: CanonicalRequest, ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {"model": canonical.model or ""}
    system_str = _egress_text_join(canonical.system)
    if system_str:
        out["system"] = system_str
    out_messages: List[Dict[str, Any]] = []
    for msg in canonical.messages:
        role = msg.get("role") or "user"
        parts = msg.get("parts") or []
        if role == "user":
            tool_results = [p for p in parts if p.get("type") == "tool_result"]
            plain = [p for p in parts if p.get("type") != "tool_result"]
            for tr in tool_results:
                content = tr.get("content")
                if isinstance(content, list):
                    content_str = _egress_text_join(content)
                else:
                    content_str = content if isinstance(content, str) else ""
                out_messages.append({
                    "role": "tool",
                    "tool_call_id": tr.get("tool_use_id"),
                    "content": content_str,
                })
            if plain:
                out_messages.append({"role": "user", "content": [_egress_part_openai_chat(p) for p in plain]})
        elif role == "assistant":
            tool_calls = []
            content_parts = []
            for p in parts:
                if p.get("type") == "tool_use":
                    raw = p.get("raw_arguments")
                    args_str = raw if raw is not None else json.dumps(p.get("arguments") or {})
                    tool_calls.append({
                        "id": p.get("id"),
                        "type": "function",
                        "function": {"name": p.get("name"), "arguments": args_str},
                    })
                elif p.get("type") == "text":
                    content_parts.append({"type": "text", "text": p.get("text", "")})
            m: Dict[str, Any] = {"role": "assistant"}
            if content_parts:
                m["content"] = _egress_text_join(content_parts)
            else:
                m["content"] = None
            if tool_calls:
                m["tool_calls"] = tool_calls
            out_messages.append(m)
        else:
            out_messages.append({"role": role, "content": [_egress_part_openai_chat(p) for p in parts]})
    out["messages"] = out_messages
    if canonical.tools:
        out["tools"] = canonical.tools
    if canonical.tool_choice is not None:
        out["tool_choice"] = canonical.tool_choice
    if canonical.temperature is not None:
        out["temperature"] = canonical.temperature
    if canonical.top_p is not None:
        out["top_p"] = canonical.top_p
    if canonical.max_tokens is not None:
        out["max_tokens"] = canonical.max_tokens
    if canonical.stop is not None:
        out["stop"] = canonical.stop
    if canonical.response_format is not None:
        out["response_format"] = canonical.response_format
    # Reasoning stays canonical-only (openai-chat has no field for it).
    extra = canonical.extras.get(DIALECT_OPENAI_CHAT)
    if isinstance(extra, dict):
        out.update(extra)
    return out


def _egress_part_openai_chat(part: Dict[str, Any]) -> Dict[str, Any]:
    ptype = part.get("type")
    if ptype == "text":
        return {"type": "text", "text": part.get("text", "")}
    if ptype == "image":
        url = part.get("url")
        data = part.get("data")
        media_type = part.get("media_type")
        if url is None and data is not None:
            url = f"data:{media_type or 'application/octet-stream'};base64,{data}"
        return {"type": "image_url", "image_url": {"url": url}}
    if ptype == "audio":
        return {"type": "input_audio", "input_audio": {"data": part.get("data"), "format": part.get("media_type")}}
    if ptype == "document":
        src = part.get("url") or part.get("data")
        return {"type": "file", "file": {"file_data": src, "mime_type": part.get("media_type"), "filename": part.get("filename")}}
    return {"type": "text", "text": json.dumps(part.get("raw", part), default=str)}


def _result_output_str(content: Any) -> str:
    """tool_result content → Responses function_call_output string."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return _egress_text_join(content)
    if content is None:
        return ""
    try:
        return json.dumps(content, default=str)
    except Exception:
        return str(content)


def _egress_part_responses(part: Dict[str, Any]) -> Dict[str, Any]:
    ptype = part.get("type")
    if ptype == "text":
        return {"type": "input_text", "text": part.get("text", "")}
    if ptype == "image":
        url = part.get("url")
        data = part.get("data")
        if url is None and data is not None:
            url = f"data:{part.get('media_type') or 'application/octet-stream'};base64,{data}"
        return {"type": "input_image", "image_url": url}
    if ptype == "document":
        d: Dict[str, Any] = {}
        if part.get("data") is not None:
            d["file_data"] = part.get("url") or (
                f"data:{part.get('media_type') or 'application/octet-stream'};base64,{part['data']}")
        elif part.get("url") is not None:
            d["file_data"] = part.get("url")
        if part.get("file_id") is not None:
            d["file_id"] = part.get("file_id")
        if part.get("filename") is not None:
            d["filename"] = part.get("filename")
        if part.get("media_type") is not None:
            d["mime_type"] = part.get("media_type")
        return {"type": "input_file", **d}
    if ptype == "audio":
        return {"type": "input_audio", "input_audio": {"data": part.get("data"), "format": part.get("media_type")}}
    return {"type": "input_text", "text": json.dumps(part.get("raw", part), default=str)}


def _responses_tool_choice_out(tc: Any) -> Any:
    """Canonical tool_choice → Responses tool_choice (openai-chat form mapped)."""
    if isinstance(tc, dict):
        fn = tc.get("function")
        if isinstance(fn, dict) and fn.get("name") is not None:
            return {"type": "function", "name": fn.get("name")}
        if tc.get("type") == "function" and tc.get("name") is not None:
            return {"type": "function", "name": tc.get("name")}
    return tc


def _egress_responses(canonical: CanonicalRequest, ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {"model": canonical.model or ""}
    instructions = _egress_text_join(canonical.system)
    if instructions:
        out["instructions"] = instructions

    extras_raw = canonical.extras.get(DIALECT_RESPONSES)
    extras_resp = extras_raw if isinstance(extras_raw, dict) else {}
    raw_reasoning = extras_resp.get("reasoning_items")
    raw_reasoning = raw_reasoning if isinstance(raw_reasoning, list) else []
    reasoning_idx = 0

    input_items: List[Any] = []
    for msg in canonical.messages:
        role = msg.get("role") or "user"
        buffered: List[Dict[str, Any]] = []

        def _flush() -> None:
            if buffered:
                input_items.append({"type": "message", "role": role,
                                    "content": [_egress_part_responses(p) for p in buffered]})
                buffered.clear()

        for part in msg.get("parts") or []:
            if not isinstance(part, dict):
                _flush()
                input_items.append(part)
                continue
            ptype = part.get("type")
            if ptype in ("text", "image", "document", "audio"):
                buffered.append(part)
                continue
            _flush()
            if ptype == "tool_use":
                raw = part.get("raw_arguments")
                args_str = raw if raw is not None else json.dumps(part.get("arguments") or {})
                input_items.append({"type": "function_call", "call_id": part.get("id"),
                                    "name": part.get("name"), "arguments": args_str})
            elif ptype == "tool_result":
                input_items.append({"type": "function_call_output",
                                    "call_id": part.get("tool_use_id"),
                                    "output": _result_output_str(part.get("content"))})
            elif ptype == "thinking":
                if reasoning_idx < len(raw_reasoning):
                    input_items.append(raw_reasoning[reasoning_idx])
                    reasoning_idx += 1
                else:
                    input_items.append({"type": "reasoning",
                                        "summary": [{"type": "summary_text", "text": part.get("thinking", "")}]})
            else:  # unknown — verbatim
                input_items.append(part.get("raw", part))
        _flush()
    out["input"] = input_items

    strict_map: Dict[Any, Any] = {}
    for entry in extras_resp.get("tool_strict") or []:
        if isinstance(entry, dict) and entry.get("name") is not None:
            strict_map[entry["name"]] = entry.get("strict")
    tools_out: List[Any] = []
    unmapped = list(extras_resp.get("tools_unmapped") or [])
    for tool in canonical.tools:
        if isinstance(tool, dict) and isinstance(tool.get("function"), dict):
            fn = tool["function"]
            t: Dict[str, Any] = {"type": "function", "name": fn.get("name")}
            if fn.get("description") is not None:
                t["description"] = fn.get("description")
            if fn.get("parameters") is not None:
                t["parameters"] = fn.get("parameters")
            if fn.get("name") in strict_map:
                t["strict"] = strict_map[fn.get("name")]
            tools_out.append(t)
        else:
            unmapped.append(tool)
    if tools_out or unmapped:
        out["tools"] = tools_out + unmapped

    if canonical.tool_choice is not None:
        out["tool_choice"] = _responses_tool_choice_out(canonical.tool_choice)
    if canonical.temperature is not None:
        out["temperature"] = canonical.temperature
    if canonical.top_p is not None:
        out["top_p"] = canonical.top_p
    if canonical.max_tokens is not None:
        out["max_output_tokens"] = canonical.max_tokens
    if canonical.response_format is not None:
        out["text"] = {"format": canonical.response_format}
    if canonical.reasoning.get("enabled") and canonical.reasoning.get("effort"):
        out["reasoning"] = {"effort": canonical.reasoning.get("effort")}

    # consumed internally; everything else merges back verbatim (last).
    consumed = {"reasoning_items", "tool_strict", "tools_unmapped", "stop_sequences"}
    merge = {k: v for k, v in extras_resp.items() if k not in consumed}
    if canonical.stop is not None:
        # documented gap: Responses has no stop field — carry as stop_sequences
        merge.setdefault("stop_sequences", canonical.stop)
    if merge:
        out.update(merge)
    return out


def _gemini_result_name(tool_use_id: Any, canonical: CanonicalRequest) -> Any:
    """tool_use_id → gemini function name (strip prefix; fallback: matching tool_use)."""
    if isinstance(tool_use_id, str) and tool_use_id.startswith("gemini_fc:"):
        return tool_use_id[len("gemini_fc:"):]
    for msg in canonical.messages:
        if not isinstance(msg, dict):
            continue
        for p in msg.get("parts") or []:
            if isinstance(p, dict) and p.get("type") == "tool_use" and p.get("id") == tool_use_id:
                return p.get("name")
    return tool_use_id


def _gemini_result_payload(content: Any) -> Any:
    """tool_result content → functionResponse response (dict passthrough, else {"result": text})."""
    if isinstance(content, dict):
        return content
    if isinstance(content, str):
        return {"result": content}
    if isinstance(content, list):
        return {"result": _egress_text_join(content)}
    if content is None:
        return {"result": ""}
    return {"result": str(content)}


def _gemini_tool_config(tc: Any) -> Optional[Dict[str, Any]]:
    """Canonical tool_choice → functionCallingConfig (None → omit)."""
    if tc == "auto":
        return {"mode": "AUTO"}
    if tc == "none":
        return {"mode": "NONE"}
    if tc == "required":
        return {"mode": "ANY"}
    if isinstance(tc, dict):
        name = None
        fn = tc.get("function")
        if isinstance(fn, dict):
            name = fn.get("name")
        elif tc.get("type") == "function":
            name = tc.get("name")
        if name is not None:
            return {"mode": "ANY", "allowedFunctionNames": [name]}
    return None


def _egress_gemini(canonical: CanonicalRequest, ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {"model": canonical.model or ""}

    if canonical.system:
        sys_parts: List[Any] = []
        for p in canonical.system:
            if isinstance(p, dict) and p.get("type") == "text":
                sys_parts.append({"text": p.get("text", "")})
            else:
                sys_parts.append(p.get("raw", p) if isinstance(p, dict) else p)
        out["systemInstruction"] = {"parts": sys_parts}

    contents: List[Dict[str, Any]] = []
    for msg in canonical.messages:
        if not isinstance(msg, dict):
            contents.append({"role": "user", "parts": [msg]})
            continue
        role = msg.get("role") or "user"
        if role == "assistant":
            role = "model"
        gparts: List[Any] = []
        for part in msg.get("parts") or []:
            if not isinstance(part, dict):
                gparts.append(part)
                continue
            ptype = part.get("type")
            if ptype == "text":
                gparts.append({"text": part.get("text", "")})
            elif ptype == "image":
                if part.get("data") is not None:
                    gparts.append({"inlineData": {"mimeType": part.get("media_type") or "image/png",
                                                  "data": part.get("data")}})
                elif part.get("url") is not None:
                    gparts.append({"fileData": {"fileUri": part.get("url"), "mimeType": part.get("media_type")}})
                else:
                    gparts.append({"text": json.dumps(part, default=str)})
            elif ptype == "document":
                if part.get("data") is not None:
                    gparts.append({"inlineData": {"mimeType": part.get("media_type") or "application/octet-stream",
                                                  "data": part.get("data")}})
                elif part.get("url") is not None:
                    gparts.append({"fileData": {"fileUri": part.get("url"), "mimeType": part.get("media_type")}})
                else:
                    gparts.append({"text": json.dumps(part, default=str)})
            elif ptype == "tool_use":
                gparts.append({"functionCall": {"name": part.get("name"), "args": part.get("arguments") or {}}})
            elif ptype == "tool_result":
                gparts.append({"functionResponse": {
                    "name": _gemini_result_name(part.get("tool_use_id"), canonical),
                    "response": _gemini_result_payload(part.get("content")),
                }})
            elif ptype == "thinking":
                gparts.append({"text": part.get("thinking", ""), "thought": True})
            else:  # unknown — verbatim
                gparts.append(part.get("raw", part))
            sig = (part.get("extras") or {}).get("thought_signature")
            if sig is not None:
                gparts.append({"thoughtSignature": sig})
        contents.append({"role": role, "parts": gparts})
    out["contents"] = contents

    extras_raw = canonical.extras.get(DIALECT_GEMINI)
    extras_gem = extras_raw if isinstance(extras_raw, dict) else {}
    unmapped = list(extras_gem.get("tools_unmapped") or [])
    decls: List[Dict[str, Any]] = []
    for tool in canonical.tools:
        if isinstance(tool, dict) and isinstance(tool.get("function"), dict):
            fn = tool["function"]
            decl: Dict[str, Any] = {"name": fn.get("name")}
            if fn.get("description") is not None:
                decl["description"] = fn.get("description")
            if fn.get("parameters") is not None:
                decl["parameters"] = fn.get("parameters")
            decls.append(decl)
        else:
            unmapped.append(tool)
    tools_out: List[Any] = []
    if decls:
        tools_out.append({"functionDeclarations": decls})
    tools_out.extend(unmapped)
    if tools_out:
        out["tools"] = tools_out

    if canonical.tool_choice is not None:
        fcc = _gemini_tool_config(canonical.tool_choice)
        if fcc is not None:
            out["toolConfig"] = {"functionCallingConfig": fcc}

    gen: Dict[str, Any] = {}
    if canonical.temperature is not None:
        gen["temperature"] = canonical.temperature
    if canonical.top_p is not None:
        gen["topP"] = canonical.top_p
    if canonical.top_k is not None:
        gen["topK"] = canonical.top_k
    if canonical.max_tokens is not None:
        gen["maxOutputTokens"] = canonical.max_tokens
    if canonical.stop is not None:
        gen["stopSequences"] = canonical.stop
    rf = canonical.response_format
    if isinstance(rf, dict) and (rf.get("mime_type") is not None or rf.get("schema") is not None):
        if rf.get("mime_type") is not None:
            gen["responseMimeType"] = rf.get("mime_type")
        if rf.get("schema") is not None:
            gen["responseSchema"] = rf.get("schema")
    thinking: Dict[str, Any] = {}
    budget = canonical.reasoning.get("budget_tokens")
    if budget:
        thinking["thinkingBudget"] = budget
    if extras_gem.get("include_thoughts") is True:
        thinking["includeThoughts"] = True
    gen_extra = extras_gem.get("generation_config")
    if isinstance(gen_extra, dict):
        tc_extra = gen_extra.get("thinkingConfig")
        if isinstance(tc_extra, dict):
            thinking.update(tc_extra)
        for k, v in gen_extra.items():
            if k != "thinkingConfig":
                gen.setdefault(k, v)
    if thinking:
        gen["thinkingConfig"] = thinking
    if gen:
        out["generationConfig"] = gen

    # consumed internally; everything else merges back verbatim (last).
    consumed = {"tools_unmapped", "include_thoughts", "generation_config"}
    merge = {k: v for k, v in extras_gem.items() if k not in consumed}
    if merge:
        out.update(merge)
    return out


_EGRESS: Dict[str, Callable[..., Dict[str, Any]]] = {
    DIALECT_OPENAI_CHAT: _egress_openai_chat,
    DIALECT_RESPONSES: _egress_responses,
    DIALECT_GEMINI: _egress_gemini,
}


# ── Public API ──────────────────────────────────────────────────────────────

def to_canonical(dialect: str, body: Any) -> CanonicalRequest:
    """dialect body → CanonicalRequest. Unknown dialect raises ValueError;
    a non-dict body yields an empty canonical with the body in
    extras["passthrough"] (never raises on shape)."""
    converter = _INGRESS.get(dialect)
    if converter is None:
        raise ValueError(f"unknown dialect: {dialect!r}")
    if not isinstance(body, dict):
        can = CanonicalRequest()
        can.extras.setdefault("passthrough", []).append(body)
        return can
    try:
        return converter(body)
    except Exception:
        can = CanonicalRequest()
        can.extras.setdefault("passthrough", []).append(body)
        return can


def from_canonical(dialect: str, canonical: Any, ctx: Optional[Dict[str, Any]] = None) -> Any:
    """CanonicalRequest → dialect body. Non-CanonicalRequest input is wrapped
    as passthrough unchanged. Unknown dialect raises ValueError."""
    converter = _EGRESS.get(dialect)
    if converter is None:
        raise ValueError(f"unknown dialect: {dialect!r}")
    if not isinstance(canonical, CanonicalRequest):
        return canonical
    return converter(canonical, ctx)


def scan_tool_pairing(canonical_messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Report tool_use parts that have no matching tool_result anywhere AFTER
    them. Pure; never raises."""
    try:
        seen_result_ids: set = set()
        for msg in canonical_messages or []:
            for part in (msg.get("parts") or []) if isinstance(msg, dict) else []:
                if isinstance(part, dict) and part.get("type") == "tool_result":
                    tid = part.get("tool_use_id")
                    if tid is not None:
                        seen_result_ids.add(tid)
        unanswered: List[Dict[str, Any]] = []
        for msg in canonical_messages or []:
            if not isinstance(msg, dict):
                continue
            for part in msg.get("parts") or []:
                if isinstance(part, dict) and part.get("type") == "tool_use":
                    tid = part.get("id")
                    if tid is not None and tid not in seen_result_ids:
                        unanswered.append({
                            "tool_use_id": tid,
                            "name": part.get("name"),
                            "index": len(unanswered),
                        })
        return unanswered
    except Exception:
        return []
