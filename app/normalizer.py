from typing import List, Dict, Any
import json
import time
from app.models import ChatCompletionRequest

class UniversalNormalizer:
    @staticmethod
    def normalize_to_openai(request_body: Dict[str, Any]) -> ChatCompletionRequest:
        """
        Takes an incoming raw JSON payload (which could be in Anthropic or Gemini format)
        and forces it into the strict OpenAI Pydantic schema for internal proxy handling.
        """
        # Use model_validate (Pydantic v2) instead of **kwargs unpacking.
        # This prevents crashes on unknown/extra fields sent by various clients
        # (e.g. OpenWebUI adds 'user', Cursor adds 'metadata', etc.)
        return ChatCompletionRequest.model_validate(request_body)

    # ------------------------------------------------------------------
    # Anthropic → OpenAI ingress conversion
    # ------------------------------------------------------------------
    @staticmethod
    def normalize_to_openai_from_anthropic(anthropic_body: Dict[str, Any]) -> Dict[str, Any]:
        """
        Translates an incoming Anthropic /v1/messages payload into an OpenAI
        chat-completions dict so it can be processed by the standard OpenAI route.

        Phase 1 Agent Compatibility Layer — preserves tools, tool_choice,
        tool_use/tool_result content blocks, and scalar fields that were
        previously silently dropped (the root cause of Claude Code tool/MCP
        failures with non-Claude models).

        Phase 7B: Cache Control Preservation — extracts cache_control breakpoints
        from system + content blocks so they can be re-injected when routing to
        Anthropic-compatible upstreams (GLM-anthropic, etc). Without this, the
        round-trip through OpenAI intermediate format strips all cache_control
        tags, reducing cache hit rates to ~0.3% (system-prompt-only implicit cache).
        """
        openai_body: Dict[str, Any] = {
            "model": anthropic_body.get("model", "default-model"),
            "messages": [],
            "stream": anthropic_body.get("stream", False)
        }

        # ── Phase 7B: Extract cache_control breakpoints BEFORE normalization ──
        # We stash them in a side-channel field so normalize_to_anthropic can
        # re-inject them for Anthropic-compatible upstreams.
        _cache_breakpoints: List[Dict[str, Any]] = []

        # System prompt cache_control
        sys_val = anthropic_body.get("system")
        if isinstance(sys_val, list):
            for block in sys_val:
                if isinstance(block, dict) and block.get("cache_control"):
                    _cache_breakpoints.append({
                        "location": "system",
                        "text_preview": (block.get("text", "")[:100]) if block.get("text") else "",
                        "cache_control": block["cache_control"]
                    })

        # Message content block cache_control
        for idx, m in enumerate(anthropic_body.get("messages", [])):
            content = m.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("cache_control"):
                        _cache_breakpoints.append({
                            "location": "message",
                            "message_index": idx,
                            "role": m.get("role", "user"),
                            "text_preview": (block.get("text", "")[:100]) if block.get("text") else "",
                            "cache_control": block["cache_control"]
                        })

        if _cache_breakpoints:
            openai_body["_bsl_cache_breakpoints"] = _cache_breakpoints

        # ── System prompt ──────────────────────────────────────────
        if "system" in anthropic_body:
            sys_val = anthropic_body["system"]
            if isinstance(sys_val, str):
                openai_body["messages"].append({"role": "system", "content": sys_val})
            elif isinstance(sys_val, list):
                text = "".join([
                    c.get("text", "")
                    for c in sys_val
                    if isinstance(c, dict) and c.get("type") == "text"
                ])
                if text:
                    openai_body["messages"].append({"role": "system", "content": text})

        # Build a map of tool_use_id -> name from assistant messages containing tool_use blocks.
        # This is needed because some OpenAI-compatible endpoints (and wrappers like LiteLLM)
        # require the "name" parameter in tool-role messages (which correspond to Anthropic's tool_result).
        tool_name_map = {}
        for m in anthropic_body.get("messages", []):
            content = m.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        t_id = block.get("id")
                        t_name = block.get("name")
                        if t_id and t_name:
                            tool_name_map[t_id] = t_name

        # ── Messages — convert Anthropic content blocks to OpenAI ──
        for m in anthropic_body.get("messages", []):
            role = m.get("role", "user")
            content = m.get("content")

            # --- Assistant with content blocks (text + tool_use) ---
            if role == "assistant" and isinstance(content, list):
                text_parts: List[str] = []
                tool_calls: List[Dict[str, Any]] = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text":
                        text_parts.append(block.get("text", ""))
                    elif btype == "tool_use":
                        tool_calls.append({
                            "id": block.get("id", ""),
                            "type": "function",
                            "function": {
                                "name": block.get("name", ""),
                                "arguments": json.dumps(block.get("input", {}))
                            }
                        })
                    # thinking blocks are intentionally skipped here —
                    # reasoning state is handled by the Thinking Policy Engine,
                    # not by the message converter.
                msg: Dict[str, Any] = {"role": "assistant"}
                msg["content"] = "\n".join(text_parts) if text_parts else None
                if tool_calls:
                    msg["tool_calls"] = tool_calls
                openai_body["messages"].append(msg)

            # --- User/tool messages with content blocks ---
            elif isinstance(content, list):
                text_parts_u: List[str] = []
                tool_results: List[Dict[str, Any]] = []
                image_parts_u: List[Dict[str, Any]] = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text":
                        text_parts_u.append(block.get("text", ""))
                    elif btype == "tool_result":
                        tool_results.append(block)
                    elif btype == "image":
                        # Anthropic image block → OpenAI image_url data URI
                        src = block.get("source", {})
                        src_type = src.get("type", "")
                        if src_type == "base64":
                            mime = src.get("media_type", "image/png")
                            data = src.get("data", "")
                            image_parts_u.append({
                                "type": "image_url",
                                "image_url": {"url": f"data:{mime};base64,{data}"}
                            })
                        elif src_type == "url":
                            image_parts_u.append({
                                "type": "image_url",
                                "image_url": {"url": src.get("url", "")}
                            })

                # Emit tool-role messages first (preserves call ordering)
                for tr in tool_results:
                    tr_content = tr.get("content", "")
                    if isinstance(tr_content, list):
                        tr_content = "\n".join(
                            b.get("text", "")
                            for b in tr_content
                            if isinstance(b, dict) and b.get("type") == "text"
                        )
                    tool_use_id = tr.get("tool_use_id", "")
                    openai_body["messages"].append({
                        "role": "tool",
                        "tool_call_id": tool_use_id,
                        "content": str(tr_content) if tr_content else "",
                        "name": tool_name_map.get(tool_use_id, "tool")
                    })

                # Emit user message — as content list if images present, else plain text
                if image_parts_u:
                    content_list: List[Dict[str, Any]] = []
                    if text_parts_u:
                        content_list.append({"type": "text", "text": "\n".join(text_parts_u)})
                    content_list.extend(image_parts_u)
                    openai_body["messages"].append({"role": role, "content": content_list})
                elif text_parts_u:
                    openai_body["messages"].append({
                        "role": role,
                        "content": "\n".join(text_parts_u)
                    })

            # --- Plain string content — pass through ---
            else:
                openai_body["messages"].append({"role": role, "content": content})

        # ── Tools — convert Anthropic → OpenAI format ──────────────
        if "tools" in anthropic_body:
            openai_tools: List[Dict[str, Any]] = []
            for tool in anthropic_body["tools"]:
                if not isinstance(tool, dict):
                    continue
                if "name" in tool and "function" not in tool:
                    # Anthropic format: {name, description, input_schema}
                    openai_tools.append({
                        "type": "function",
                        "function": {
                            "name": tool.get("name", ""),
                            "description": tool.get("description", ""),
                            "parameters": tool.get("input_schema", {"type": "object", "properties": {}})
                        }
                    })
                elif "function" in tool:
                    # Already OpenAI format
                    openai_tools.append(tool)
            if openai_tools:
                openai_body["tools"] = openai_tools

        # ── Tool choice — convert Anthropic → OpenAI ──────────────
        if "tool_choice" in anthropic_body:
            tc = anthropic_body["tool_choice"]
            if isinstance(tc, dict):
                tc_type = tc.get("type", "auto")
                if tc_type == "auto":
                    openai_body["tool_choice"] = "auto"
                elif tc_type == "any":
                    openai_body["tool_choice"] = "required"
                elif tc_type == "tool":
                    openai_body["tool_choice"] = {
                        "type": "function",
                        "function": {"name": tc.get("name", "")}
                    }
                else:
                    openai_body["tool_choice"] = tc_type
            else:
                openai_body["tool_choice"] = tc

        # ── Scalar fields ──────────────────────────────────────────
        if "max_tokens" in anthropic_body:
            openai_body["max_tokens"] = anthropic_body["max_tokens"]
        if "temperature" in anthropic_body:
            openai_body["temperature"] = anthropic_body["temperature"]
        if "top_p" in anthropic_body:
            openai_body["top_p"] = anthropic_body["top_p"]

        return openai_body

    # ------------------------------------------------------------------
    # Phase 7B: Cache Control Re-injection for Anthropic-compatible upstreams
    # ------------------------------------------------------------------
    @staticmethod
    def reinject_cache_control(anthropic_payload: Dict[str, Any], cache_breakpoints: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Re-injects cache_control breakpoints into an Anthropic-format payload
        after the round-trip through OpenAI intermediate format.

        Anthropic limits cache_control to 4 breakpoints. We prioritize:
        1. System prompt (highest cache value — static across turns)
        2. Last user message (growing conversation history)
        """
        if not cache_breakpoints:
            return anthropic_payload

        injected = 0
        MAX_CACHE_BREAKPOINTS = 4  # Anthropic hard limit

        # 1. System prompt — convert to list format with cache_control
        sys_breakpoints = [bp for bp in cache_breakpoints if bp.get("location") == "system"]
        if sys_breakpoints and injected < MAX_CACHE_BREAKPOINTS:
            sys_val = anthropic_payload.get("system")
            if isinstance(sys_val, str) and sys_val:
                anthropic_payload["system"] = [{
                    "type": "text",
                    "text": sys_val,
                    "cache_control": sys_breakpoints[0].get("cache_control", {"type": "ephemeral"})
                }]
                injected += 1
            elif isinstance(sys_val, list) and sys_val:
                last_block = sys_val[-1]
                if isinstance(last_block, dict) and "cache_control" not in last_block:
                    last_block["cache_control"] = sys_breakpoints[0].get("cache_control", {"type": "ephemeral"})
                    injected += 1

        # 2. Message content blocks
        msg_breakpoints = [bp for bp in cache_breakpoints if bp.get("location") == "message"]
        messages = anthropic_payload.get("messages", [])
        for bp in reversed(msg_breakpoints):
            if injected >= MAX_CACHE_BREAKPOINTS:
                break
            msg_idx = bp.get("message_index", -1)
            if msg_idx < 0 or msg_idx >= len(messages):
                continue
            msg = messages[msg_idx]
            content = msg.get("content")
            if isinstance(content, list) and content:
                for block in reversed(content):
                    if isinstance(block, dict) and block.get("type") == "text" and "cache_control" not in block:
                        block["cache_control"] = bp.get("cache_control", {"type": "ephemeral"})
                        injected += 1
                        break
            elif isinstance(content, str) and content:
                msg["content"] = [{
                    "type": "text",
                    "text": content,
                    "cache_control": bp.get("cache_control", {"type": "ephemeral"})
                }]
                injected += 1

        return anthropic_payload

    # ------------------------------------------------------------------
    # OpenAI internal → Anthropic outbound conversion
    # ------------------------------------------------------------------
    @staticmethod
    def normalize_to_anthropic(request: ChatCompletionRequest) -> Dict[str, Any]:
        """
        Translates the internal OpenAI Pydantic model into an Anthropic
        /v1/messages payload.

        Phase 1 Agent Compatibility Layer — now includes tools, tool_choice,
        and proper conversion of OpenAI tool-role messages back into Anthropic
        tool_result content blocks.
        """
        anthropic_messages: List[Dict[str, Any]] = []
        system_prompt = ""

        for msg in request.messages:
            if msg.role == "system":
                # Anthropic pulls system prompt out of the messages array
                content = msg.content
                if isinstance(content, list):
                    content = "".join([
                        c.get("text", "")
                        for c in content
                        if isinstance(c, dict) and c.get("type") == "text"
                    ])
                system_prompt += (content or "") + "\n"

            elif msg.role == "tool":
                # Convert OpenAI tool-role message → Anthropic user tool_result
                tr_content = msg.content if isinstance(msg.content, str) else str(msg.content or "")
                anthropic_messages.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": msg.tool_call_id or "",
                        "content": tr_content
                    }]
                })

            else:
                # Serialize content safely — msg.content may be Pydantic objects
                content = msg.content
                if isinstance(content, list):
                    content = [
                        p.model_dump(exclude_none=True) if hasattr(p, "model_dump") else p
                        for p in content
                    ]

                # Convert OpenAI content list → Anthropic content blocks
                # OpenAI: [{"type":"image_url","image_url":{"url":"data:mime;base64,..."}}]
                # Anthropic: [{"type":"image","source":{"type":"base64","media_type":"...","data":"..."}}]
                if isinstance(content, list):
                    anthropic_content: List[Dict[str, Any]] = []
                    for part in content:
                        if not isinstance(part, dict):
                            continue
                        ptype = part.get("type", "")
                        if ptype == "text":
                            anthropic_content.append({"type": "text", "text": part.get("text", "")})
                        elif ptype == "image_url":
                            url = (part.get("image_url") or {}).get("url", "")
                            if url.startswith("data:"):
                                # data:mime/type;base64,<data>
                                try:
                                    meta, data = url.split(",", 1)
                                    mime = meta.split(":", 1)[1].split(";")[0]
                                except (ValueError, IndexError):
                                    mime, data = "image/png", ""
                                anthropic_content.append({
                                    "type": "image",
                                    "source": {"type": "base64", "media_type": mime, "data": data}
                                })
                            elif url:
                                # Plain URL — use url source type
                                anthropic_content.append({
                                    "type": "image",
                                    "source": {"type": "url", "url": url}
                                })
                        else:
                            # Pass through any already-valid Anthropic block (e.g. tool_result)
                            anthropic_content.append(part)
                    content = anthropic_content

                anthropic_msg: Dict[str, Any] = {"role": msg.role, "content": content}

                # Handle tool_calls → Anthropic tool_use blocks
                if msg.tool_calls:
                    anthropic_msg["content"] = []
                    # Include any preceding text content
                    if isinstance(content, str) and content:
                        anthropic_msg["content"].append({"type": "text", "text": content})
                    elif isinstance(content, list):
                        # Prepend any text/image blocks that were already converted
                        anthropic_msg["content"].extend(
                            p for p in content if isinstance(p, dict) and p.get("type") in ("text", "image")
                        )
                    for tool in msg.tool_calls:
                        anthropic_msg["content"].append({
                            "type": "tool_use",
                            "id": tool.id,
                            "name": tool.function.name,
                            "input": json.loads(tool.function.arguments) if isinstance(tool.function.arguments, str) else tool.function.arguments
                        })
                anthropic_messages.append(anthropic_msg)

        payload: Dict[str, Any] = {
            "model": request.model,
            "messages": anthropic_messages,
            "stream": request.stream
        }
        if system_prompt.strip():
            payload["system"] = system_prompt.strip()

        if request.max_tokens:
            payload["max_tokens"] = request.max_tokens
        else:
            payload["max_tokens"] = 4096  # Anthropic requires max_tokens

        # ── Tools — convert OpenAI → Anthropic format ──────────────
        if request.tools:
            anthropic_tools: List[Dict[str, Any]] = []
            for tool in request.tools:
                if not isinstance(tool, dict):
                    continue
                if "function" in tool:
                    fn = tool["function"]
                    anthropic_tools.append({
                        "name": fn.get("name", ""),
                        "description": fn.get("description", ""),
                        "input_schema": fn.get("parameters", {"type": "object", "properties": {}})
                    })
                elif "name" in tool:
                    # Already Anthropic format
                    anthropic_tools.append(tool)
            if anthropic_tools:
                payload["tools"] = anthropic_tools

        # ── Tool choice — convert OpenAI → Anthropic ───────────────
        if request.tool_choice:
            tc = request.tool_choice
            if isinstance(tc, str):
                if tc == "auto":
                    payload["tool_choice"] = {"type": "auto"}
                elif tc == "required":
                    payload["tool_choice"] = {"type": "any"}
                elif tc == "none":
                    pass  # Don't send — Anthropic has no "none"
            elif isinstance(tc, dict) and tc.get("type") == "function":
                payload["tool_choice"] = {
                    "type": "tool",
                    "name": tc.get("function", {}).get("name", "")
                }

        return payload

    # ------------------------------------------------------------------
    # OpenAI → Anthropic egress conversion (non-streaming response)
    # ------------------------------------------------------------------
    @staticmethod
    def openai_response_to_anthropic(openai_resp: Dict[str, Any], model: str = "") -> Dict[str, Any]:
        """
        Convert a non-streaming OpenAI chat/completions response into an
        Anthropic Messages response. Used when a client hits /v1/messages
        (Anthropic protocol) but the resolved upstream provider is OpenAI-format.
        """
        import uuid as _uuid

        choices = openai_resp.get("choices", [])
        choice = choices[0] if choices else {}
        message = choice.get("message", {}) if isinstance(choice, dict) else {}

        content_blocks: List[Dict[str, Any]] = []

        # Text content
        text = message.get("content")
        if text:
            content_blocks.append({"type": "text", "text": text})

        # Tool calls → Anthropic tool_use blocks
        tool_calls = message.get("tool_calls") or []
        legacy_call = message.get("function_call")
        if not tool_calls and isinstance(legacy_call, dict) and legacy_call.get("name"):
            tool_calls = [{
                "id": f"call_{_uuid.uuid4().hex[:24]}",
                "type": "function",
                "function": legacy_call,
            }]
        for tc in tool_calls:
            fn = tc.get("function", {})
            raw_args = fn.get("arguments", "{}")
            try:
                parsed_args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
            except (json.JSONDecodeError, TypeError):
                parsed_args = {}
            content_blocks.append({
                "type": "tool_use",
                "id": tc.get("id") or f"toolu_{_uuid.uuid4().hex[:24]}",
                "name": fn.get("name", ""),
                "input": parsed_args,
            })

        if not content_blocks:
            content_blocks.append({"type": "text", "text": ""})

        # Map finish_reason → Anthropic stop_reason
        finish = choice.get("finish_reason") if isinstance(choice, dict) else None
        if not finish and (message.get("tool_calls") or message.get("function_call")):
            finish = "tool_calls"
        stop_reason = {
            "stop": "end_turn",
            "length": "max_tokens",
            "tool_calls": "tool_use",
            "function_call": "tool_use",
            "content_filter": "end_turn",
        }.get(finish, "end_turn")

        usage = openai_resp.get("usage", {}) or {}
        in_tokens = usage.get("prompt_tokens", 0) or usage.get("input_tokens", 0)
        out_tokens = usage.get("completion_tokens", 0) or usage.get("output_tokens", 0)

        return {
            "id": openai_resp.get("id") or f"msg_{_uuid.uuid4().hex[:24]}",
            "type": "message",
            "role": "assistant",
            "model": model or openai_resp.get("model", ""),
            "content": content_blocks,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {
                "input_tokens": in_tokens,
                "output_tokens": out_tokens,
            },
        }

    @staticmethod
    def anthropic_response_to_openai(anthropic_resp: Dict[str, Any], model: str = "") -> Dict[str, Any]:
        """
        Convert a non-streaming Anthropic Messages response into an OpenAI
        chat/completions response. Used when a client hits /v1/chat/completions
        (OpenAI protocol) but the resolved upstream provider is
        Anthropic-compatible (GLM, Kimi, MiniMax, etc.).
        """
        import uuid as _uuid

        content_blocks = anthropic_resp.get("content", [])
        text_parts: List[str] = []
        tool_calls: List[Dict[str, Any]] = []

        for block in content_blocks:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type", "")
            if block_type == "text":
                text_parts.append(block.get("text", ""))
            elif block_type == "tool_use":
                raw_input = block.get("input", {})
                try:
                    args_str = json.dumps(raw_input) if not isinstance(raw_input, str) else raw_input
                except (TypeError, ValueError):
                    args_str = "{}"
                tool_calls.append({
                    "id": block.get("id", f"toolu_{_uuid.uuid4().hex[:24]}"),
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": args_str,
                    },
                })

        # Map Anthropic stop_reason → OpenAI finish_reason
        stop_reason = anthropic_resp.get("stop_reason", "end_turn")
        finish_reason = {
            "end_turn": "stop",
            "max_tokens": "length",
            "tool_use": "tool_calls",
            "stop_sequence": "stop",
        }.get(stop_reason, "stop")

        message: Dict[str, Any] = {
            "role": "assistant",
            "content": "\n".join(text_parts) if text_parts else "",
        }
        if tool_calls:
            message["tool_calls"] = tool_calls

        usage = anthropic_resp.get("usage", {}) or {}
        in_tokens = usage.get("input_tokens", 0) or usage.get("prompt_tokens", 0)
        out_tokens = usage.get("output_tokens", 0) or usage.get("completion_tokens", 0)

        return {
            "id": anthropic_resp.get("id") or f"chatcmpl-bsl-{_uuid.uuid4().hex[:24]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model or anthropic_resp.get("model", ""),
            "choices": [{
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
            }],
            "usage": {
                "prompt_tokens": in_tokens,
                "completion_tokens": out_tokens,
                "total_tokens": in_tokens + out_tokens,
            },
        }
