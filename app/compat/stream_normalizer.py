"""
BSL Router Agent Compatibility Layer — Stream Normalizer

Phase 4: Stream dialect must match the CLIENT, not the provider.

If Claude Code (Anthropic SSE client) sends a request that gets routed to
an OpenAI-compatible provider (DeepSeek, OpenAI), the response stream comes
back in OpenAI SSE format. The stream normalizer converts it to Anthropic SSE
before sending to Claude Code.

Conversely, if an OpenAI client sends to an Anthropic-compatible provider,
the Anthropic SSE response gets converted to OpenAI SSE.

Key rules:
- No mock prefill injection for agent clients
- Preserve tool-call deltas
- Preserve block index ordering
- Convert usage/stop reasons correctly
"""
from typing import AsyncIterator, Optional, Dict, Any, List
import json
import time
import codecs
import logging

logger = logging.getLogger(__name__)

# BUG A streaming rescue helpers (delimiter-scoped buffering for text-form
# tool calls emitted by GLM/Opus/Sonnet). See app/middleware/glm_tools.py.
from app.middleware.glm_tools import (
    find_earliest_opener,
    find_block_closer,
    find_hold_tail,
    parse_streamed_tool_block,
)



class StreamConversionError(ValueError):
    """Raised when a provider stream cannot be safely converted."""

class StreamNormalizer:
    """
    Converts SSE streams between OpenAI and Anthropic dialects.

    Usage:
        normalizer = StreamNormalizer(target_dialect="anthropic_sse")
        async for chunk in normalizer.convert(openai_stream):
            yield chunk
    """

    def __init__(self, source_dialect: str, target_dialect: str, model_name: str = "bsl-routed", tools_in_request: bool = False):
        self.source = source_dialect
        self.target = target_dialect
        self.model_name = model_name
        # BUG A gate: the text-form tool-call rescue only activates when the
        # request declared tools (Split Pump contract: tool-less chats and
        # structured-call streams stay pure passthrough).
        self.tools_in_request = tools_in_request

    def needs_conversion(self) -> bool:
        """Check if source and target dialects differ."""
        return self.source != self.target

    # ──────────────────────────────────────────────────────────────
    # OpenAI SSE → Anthropic SSE conversion
    # ──────────────────────────────────────────────────────────────

    async def convert_openai_to_anthropic(
        self,
        openai_stream: AsyncIterator[bytes],
    ) -> AsyncIterator[bytes]:
        """
        Convert an OpenAI chat/completions SSE stream into Anthropic Messages SSE.

        Emits the required Anthropic event sequence:
        1. message_start
        2. content_block_start (index 0, text)
        3. content_block_delta (text deltas)
        4. [content_block_stop + content_block_start for tool_use blocks]
        5. content_block_stop
        6. message_delta (stop reason)
        7. message_stop
        """
        # Emit message_start
        yield self._encode_anthropic_event("message_start", {
            "type": "message_start",
            "message": {
                "id": f"msg_bsl_{int(time.time() * 1000)}",
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": self.model_name,
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        })

        # State tracking
        text_block_started = False
        tool_blocks: Dict[int, Dict[str, Any]] = {}  # tool_index -> block state
        current_tool_index = 1  # Text is index 0
        input_tokens = 0
        output_tokens = 0
        stop_reason = "end_turn"

        buffer = ""
        decoder = codecs.getincrementaldecoder("utf-8")()

        # ── BUG A: text-form tool-call rescue state ──────────────────────
        # Active only when the request declared tools; otherwise the stream
        # stays a pure forwarder. Buffering is decided by DELIMITERS, never
        # by timers -- the same 9router-forwarder principle as antifreeze.py.
        rescue_active = bool(self.tools_in_request)
        rescue_hold = ""           # text not yet proven safe to emit
        rescue_in_block = False    # a complete opener arrived; awaiting closer
        rescue_unicode = False     # block uses unicode delimiters
        rescue_body_start = 0      # body start index inside rescue_hold
        _RESCUE_MAX_HOLD = 60      # max chars of text deferred for a decision
        _RESCUE_MAX_BLOCK = 131072  # memory cap for one buffered block

        def _emit_text(text: str):
            """Yield Anthropic text events for proven-safe text."""
            nonlocal text_block_started
            if not text:
                return
            if not text_block_started:
                yield self._encode_anthropic_event("content_block_start", {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                })
                text_block_started = True
            yield self._encode_anthropic_event("content_block_delta", {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": text},
            })

        def _register_rescued(calls: List[Dict[str, Any]]) -> None:
            """Add rescued text-form tool calls to tool_blocks.

            Rescued calls have no OpenAI stream index; they get high keys so
            they can never collide with structured indices (small integers).
            """
            nonlocal current_tool_index
            for call in calls:
                fn = call.get("function", {}) if isinstance(call, dict) else {}
                args = fn.get("arguments") or ""
                tool_blocks[1000 + len(tool_blocks)] = {
                    "index": current_tool_index,
                    "id": call.get("id") or f"call_{fn.get('name') or 'function'}_{len(tool_blocks)}",
                    "name": fn.get("name", ""),
                    "args_buffer": args,
                    "arg_fragments": [args] if args else [],
                }
                current_tool_index += 1

        def _drain_rescue():
            """Consume rescue_hold after a text delta was appended.

            Emits text deltas for proven-safe text and registers rescued tool
            calls in tool_blocks. Decisions are delimiter-driven only:
              - text before an opener            -> emit immediately
              - a tail that may become an opener -> hold (bounded)
              - opener seen, closer pending      -> hold the block body
              - complete block                   -> parse, emit structured
              - unparseable / unclosed at EOF    -> fail-open as text
            """
            nonlocal rescue_in_block, rescue_unicode, rescue_hold, rescue_body_start
            while rescue_hold:
                if not rescue_in_block:
                    found = find_earliest_opener(rescue_hold)
                    if found is None:
                        keep_from = find_hold_tail(rescue_hold)
                        # A pathological run of '<' would otherwise be held
                        # forever; force-release beyond the bounded deferral.
                        if len(rescue_hold) - keep_from > _RESCUE_MAX_HOLD:
                            keep_from = len(rescue_hold) - _RESCUE_MAX_HOLD
                        for _ev in _emit_text(rescue_hold[:keep_from]):
                            yield _ev
                        rescue_hold = rescue_hold[keep_from:]
                        return
                    start, end, uni = found
                    for _ev in _emit_text(rescue_hold[:start]):
                        yield _ev
                    rescue_in_block = True
                    rescue_unicode = uni
                    rescue_body_start = end - start
                    rescue_hold = rescue_hold[start:]
                    continue
                closer = find_block_closer(rescue_hold, rescue_unicode, pos=rescue_body_start)
                if closer is None:
                    if len(rescue_hold) > _RESCUE_MAX_BLOCK:
                        # Memory cap: fail-open, emit the whole span as text.
                        for _ev in _emit_text(rescue_hold):
                            yield _ev
                        rescue_hold = ""
                        rescue_in_block = False
                    return
                cs, ce = closer
                body = rescue_hold[rescue_body_start:cs]
                calls = parse_streamed_tool_block(body, rescue_unicode)
                if calls:
                    _register_rescued(calls)
                    print(
                        f"[StreamToolRescue] extracted {len(calls)} tool_call(s) "
                        f"from streaming text",
                        flush=True,
                    )
                else:
                    # Unparseable block: fail-open, emit it raw (today's
                    # behavior) -- never swallow bytes.
                    for _ev in _emit_text(rescue_hold[:ce]):
                        yield _ev
                rescue_hold = rescue_hold[ce:]
                rescue_in_block = False
                continue

        def _repair_tool_input(tc_index: int, tc_data: Dict[str, Any]) -> None:
            """Make a tool call's arguments emittable. NEVER raises.

            BUG B2 FIXED HERE (2026-08-04). This function used to raise
            StreamConversionError on empty/malformed arguments. Nothing in
            production code caught it (grep: only tests did), so the exception
            propagated out mid-stream and the turn died with no message_stop.

            The user-visible consequence was the difference between
            "the model fixes its own mistake" and "the model is stuck":

              valid JSON, missing required fields -> forwarded -> client
                  rejects -> error re-enters the conversation -> model RETRIES.
              malformed/empty arguments           -> stream KILLED -> model is
                  never told what went wrong -> it CANNOT retry.

            Both are the same class of model mistake; only our handling differed.
            So we now do what the recoverable path already did: forward it and
            let the client validate. The client owns the tool schema -- we do
            not, and cannot check `required` fields anyway.

            This is the same principle as the streaming fixes in antifreeze.py:
            do not terminate a stream based on our own judgment of the bytes.
            """
            raw_args = tc_data["args_buffer"]
            ident = tc_data.get("id") or f"index {tc_index}"
            name = tc_data.get("name") or "<unknown>"

            # `arguments == ""` is VALID: a zero-parameter tool has no args to
            # send. The old code rejected these outright, which is why calls to
            # simple tools could fail for no reason. Anthropic requires an
            # object, so the correct rendering is `{}`, not an error.
            if raw_args == "":
                tc_data["args_buffer"] = "{}"
                tc_data["arg_fragments"] = ["{}"]
                return

            try:
                parsed = json.loads(raw_args)
            except json.JSONDecodeError as exc:
                # Truncated or corrupt JSON (model stopped mid-emit). Emitting
                # the raw fragments would hand the client unparseable
                # input_json_delta content. Instead pass it through as a valid
                # object whose contents are self-evidently wrong: the client
                # reports a tool error, that error reaches the model, and the
                # model can correct itself on the next turn.
                print(
                    f"[ToolRepair] {ident} ({name}): malformed JSON at position "
                    f"{exc.pos} -- forwarding for client validation instead of "
                    f"killing the stream.",
                    flush=True,
                )
                _salvaged = json.dumps({"_bsl_malformed_arguments": raw_args})
                tc_data["args_buffer"] = _salvaged
                tc_data["arg_fragments"] = [_salvaged]
                return

            if not isinstance(parsed, dict):
                # A bare scalar/array is not a valid tool input object.
                print(
                    f"[ToolRepair] {ident} ({name}): arguments were "
                    f"{type(parsed).__name__}, expected object -- wrapping.",
                    flush=True,
                )
                _wrapped = json.dumps({"_bsl_unexpected_arguments": parsed})
                tc_data["args_buffer"] = _wrapped
                tc_data["arg_fragments"] = [_wrapped]
                return

            # Well-formed object. Missing REQUIRED fields (e.g. toolSummary) are
            # deliberately NOT checked here: the client owns the schema and its
            # rejection is what teaches the model to fix the call.


        def _tool_events(tc_data: Dict[str, Any]):
            yield self._encode_anthropic_event("content_block_start", {
                "type": "content_block_start",
                "index": tc_data["index"],
                "content_block": {
                    "type": "tool_use",
                    "id": tc_data["id"],
                    "name": tc_data["name"],
                    "input": {},
                },
            })
            for fragment in tc_data["arg_fragments"]:
                yield self._encode_anthropic_event("content_block_delta", {
                    "type": "content_block_delta",
                    "index": tc_data["index"],
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": fragment,
                    },
                })

        async for chunk in openai_stream:
            text_chunk = decoder.decode(chunk)
            buffer += text_chunk
            lines = buffer.split("\n")
            buffer = lines.pop()

            for line in lines:
                if not line.startswith("data: "):
                    continue
                data_str = line[6:].strip()
                if data_str == "[DONE]":
                    continue

                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                # Extract usage
                if "usage" in data and data["usage"]:
                    u = data["usage"]
                    input_tokens = u.get("prompt_tokens", input_tokens)
                    output_tokens = u.get("completion_tokens", output_tokens)

                choices = data.get("choices") or []
                if not choices:
                    continue

                choice = choices[0]
                delta = choice.get("delta", {})
                finish = choice.get("finish_reason")

                # Text content delta
                text_content = delta.get("content")
                if text_content:
                    if rescue_active:
                        # BUG A rescue: the text may contain tool-call markup.
                        # Feed the delimiter-scoped state machine; proven-safe
                        # text emits immediately, block spans hold until the
                        # closing delimiter arrives.
                        rescue_hold += text_content
                        for _rev in _drain_rescue():
                            yield _rev
                    else:
                        if not text_block_started:
                            yield self._encode_anthropic_event("content_block_start", {
                                "type": "content_block_start",
                                "index": 0,
                                "content_block": {"type": "text", "text": ""},
                            })
                            text_block_started = True

                        yield self._encode_anthropic_event("content_block_delta", {
                            "type": "content_block_delta",
                            "index": 0,
                            "delta": {"type": "text_delta", "text": text_content},
                        })

                # Tool call delta (modern tool_calls or legacy function_call)
                tool_calls = delta.get("tool_calls") or []
                legacy_call = delta.get("function_call")
                if not tool_calls and isinstance(legacy_call, dict) and (
                    legacy_call.get("name") or "arguments" in legacy_call
                ):
                    tool_calls = [{
                        "index": 0,
                        "id": "",
                        "function": legacy_call,
                    }]
                for tc in tool_calls:
                    tc_index = tc.get("index", 0)
                    tc_id = tc.get("id", "")
                    function = tc.get("function", {})
                    tc_name = function.get("name", "")
                    tc_args = function.get("arguments", "")

                    if tc_index not in tool_blocks:
                        block_index = current_tool_index
                        current_tool_index += 1
                        tool_blocks[tc_index] = {
                            "index": block_index,
                            "id": tc_id or f"call_{tc_name or 'function'}_{len(tool_blocks)}",
                            "name": tc_name,
                            "args_buffer": "",
                            "arg_fragments": [],
                        }
                    else:
                        if tc_id:
                            tool_blocks[tc_index]["id"] = tc_id
                        if tc_name:
                            tool_blocks[tc_index]["name"] = tc_name

                    # Accumulate arguments without emitting malformed fragments.
                    tool_blocks[tc_index]["args_buffer"] += tc_args
                    if tc_args:
                        tool_blocks[tc_index]["arg_fragments"].append(tc_args)

                # Map finish reason
                if finish:
                    if finish in ("tool_calls", "function_call"):
                        stop_reason = "tool_use"
                    elif finish == "length":
                        stop_reason = "max_tokens"
                    elif finish == "stop":
                        stop_reason = "end_turn"

        # Flush a final line if the stream ended without a trailing newline.
        tail = decoder.decode(b"", final=True)
        if tail:
            buffer += tail

        # BUG A -- flush any rescue hold. An unclosed block at EOF cannot be
        # rescued; emit it as text (fail-open) rather than swallow it.
        if rescue_active and rescue_hold:
            for _rev in _emit_text(rescue_hold):
                yield _rev
            rescue_hold = ""
            rescue_in_block = False

        if buffer.strip():
            line = buffer.strip()
            if line.startswith("data: ") and line[6:].strip() != "[DONE]":
                try:
                    data = json.loads(line[6:].strip())
                except json.JSONDecodeError:
                    data = None
                if data and data.get("usage"):
                    u = data["usage"]
                    input_tokens = u.get("prompt_tokens", input_tokens)
                    output_tokens = u.get("completion_tokens", output_tokens)

        # BUG A -- rescued text-form calls arrive under finish_reason "stop".
        # Anthropic clients only execute tools when stop_reason is "tool_use",
        # so promote it whenever tool blocks exist. (Mirrors the finish_reason
        # promotion the buffered normalize_glm_tool_calls path performs.)
        if tool_blocks and stop_reason == "end_turn":
            stop_reason = "tool_use"

        if tool_blocks:
            # Repair (never reject) each tool call before emitting. A bad call
            # must still reach the client so the client's rejection can teach
            # the model to fix it -- see _repair_tool_input.
            for tc_index, tc_data in tool_blocks.items():
                _repair_tool_input(tc_index, tc_data)

            if text_block_started:
                yield self._encode_anthropic_event("content_block_stop", {
                    "type": "content_block_stop",
                    "index": 0,
                })
                text_block_started = False

            for tc_data in tool_blocks.values():
                for event in _tool_events(tc_data):
                    yield event
                yield self._encode_anthropic_event("content_block_stop", {
                    "type": "content_block_stop",
                    "index": tc_data["index"],
                })

        # Close text block if still open (only if no tool blocks were present)
        elif text_block_started:
            yield self._encode_anthropic_event("content_block_stop", {
                "type": "content_block_stop",
                "index": 0,
            })

        # Emit message_delta with stop reason and usage
        yield self._encode_anthropic_event("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": output_tokens},
        })

        # Emit message_stop
        yield self._encode_anthropic_event("message_stop", {
            "type": "message_stop",
        })

    # ──────────────────────────────────────────────────────────────
    # Anthropic SSE → OpenAI SSE conversion
    # ──────────────────────────────────────────────────────────────

    async def convert_anthropic_to_openai(
        self,
        anthropic_stream: AsyncIterator[bytes],
    ) -> AsyncIterator[bytes]:
        """
        Convert an Anthropic Messages SSE stream into OpenAI chat/completions SSE.

        Useful when an OpenAI client (Cursor, Cline) sends to an
        Anthropic-compatible provider (GLM, Kimi) via BSL.
        """
        created = int(time.time())
        buffer = ""
        current_tool_id = None
        current_tool_name = None
        current_tool_index = -1
        finished_emitted = False
        tool_args_buffer = ""
        _tool_id_counter: Dict[str, int] = {}  # per-tool-name counter for unique IDs
        input_tokens = 0
        output_tokens = 0

        async for chunk in anthropic_stream:
            try:
                text_chunk = chunk.decode("utf-8")
                buffer += text_chunk
                lines = buffer.split("\n")
                buffer = lines.pop()

                for line in lines:
                    if line.startswith("data: "):
                        data_str = line[6:].strip()
                        if data_str == "[DONE]":
                            continue
                        try:
                            data = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue

                        event_type = data.get("type", "")

                        if event_type == "message_start":
                            _msg = data.get("message", {})
                            _start_usage = _msg.get("usage") or {}
                            if _start_usage:
                                input_tokens = _start_usage.get("input_tokens", input_tokens)

                        elif event_type == "content_block_delta":
                            delta = data.get("delta", {})
                            delta_type = delta.get("type", "")

                            if delta_type == "text_delta":
                                yield self._encode_openai_chunk({
                                    "id": f"chatcmpl-bsl-{created}",
                                    "object": "chat.completion.chunk",
                                    "created": created,
                                    "model": self.model_name,
                                    "choices": [{
                                        "index": 0,
                                        "delta": {"content": delta.get("text", "")},
                                        "finish_reason": None,
                                    }],
                                })

                            elif delta_type == "thinking_delta":
                                # Anthropic extended-thinking block → OpenAI reasoning_content
                                # This keeps Gemini thought parts alive during long thinking phases
                                # so the IDE sees SSE frames instead of silence.
                                thinking_text = delta.get("thinking", "")
                                if thinking_text:
                                    yield self._encode_openai_chunk({
                                        "id": f"chatcmpl-bsl-{created}",
                                        "object": "chat.completion.chunk",
                                        "created": created,
                                        "model": self.model_name,
                                        "choices": [{
                                            "index": 0,
                                            "delta": {"reasoning_content": thinking_text},
                                            "finish_reason": None,
                                        }],
                                    })

                            elif delta_type == "input_json_delta":
                                # Tool call argument delta
                                partial = delta.get("partial_json", "")
                                if partial and current_tool_id:
                                    yield self._encode_openai_chunk({
                                        "id": f"chatcmpl-bsl-{created}",
                                        "object": "chat.completion.chunk",
                                        "created": created,
                                        "model": self.model_name,
                                        "choices": [{
                                            "index": 0,
                                            "delta": {
                                                "tool_calls": [{
                                                    "index": current_tool_index,
                                                    "function": {"arguments": partial},
                                                }],
                                            },
                                            "finish_reason": None,
                                        }],
                                    })

                        elif event_type == "content_block_start":
                            block = data.get("content_block", {})
                            if block.get("type") == "tool_use":
                                current_tool_index += 1
                                current_tool_name = block.get("name", "")
                                current_tool_id = block.get("id") or f"call_{current_tool_name or 'function'}_{current_tool_index}"
                                # Preserve the provider ID: clients echo it in tool_result.
                                # Synthesize only when the upstream omitted one.
                                # Emit start chunk (arguments: "" init slot in accumulator)
                                yield self._encode_openai_chunk({
                                    "id": f"chatcmpl-bsl-{created}",
                                    "object": "chat.completion.chunk",
                                    "created": created,
                                    "model": self.model_name,
                                    "choices": [{
                                        "index": 0,
                                        "delta": {
                                            "tool_calls": [{
                                                "index": current_tool_index,
                                                "id": current_tool_id,
                                                "type": "function",
                                                "function": {"name": current_tool_name, "arguments": ""},
                                            }],
                                        },
                                        "finish_reason": None,
                                    }],
                                })
                                # Some providers (xAI/Grok) put the full tool
                                # input directly in content_block_start instead
                                # of streaming it via input_json_delta.  Capture
                                # it here so the args aren't silently lost.
                                existing_input = block.get("input")
                                if existing_input:
                                    args_str = json.dumps(existing_input, ensure_ascii=False)
                                    yield self._encode_openai_chunk({
                                        "id": f"chatcmpl-bsl-{created}",
                                        "object": "chat.completion.chunk",
                                        "created": created,
                                        "model": self.model_name,
                                        "choices": [{
                                            "index": 0,
                                            "delta": {
                                                "tool_calls": [{
                                                    "index": current_tool_index,
                                                    "function": {"arguments": args_str},
                                                }],
                                            },
                                            "finish_reason": None,
                                        }],
                                    })

                        elif event_type == "message_delta":
                            msg_delta = data.get("delta", {})
                            stop_reason = msg_delta.get("stop_reason", "end_turn")
                            finish = "stop"
                            if stop_reason == "tool_use":
                                finish = "tool_calls"
                            elif stop_reason == "max_tokens":
                                finish = "length"

                            # Aggregate usage from message_start + message_delta
                            _usage = data.get("usage") or {}
                            if _usage:
                                input_tokens = _usage.get("input_tokens", input_tokens)
                                output_tokens = _usage.get("output_tokens", output_tokens)
                            _finish_payload: Dict[str, Any] = {
                                "id": f"chatcmpl-bsl-{created}",
                                "object": "chat.completion.chunk",
                                "created": created,
                                "model": self.model_name,
                                "choices": [{
                                    "index": 0,
                                    "delta": {},
                                    "finish_reason": finish,
                                }],
                            }
                            if input_tokens or output_tokens:
                                _finish_payload["usage"] = {
                                    "prompt_tokens": input_tokens,
                                    "completion_tokens": output_tokens,
                                }
                            yield self._encode_openai_chunk(_finish_payload)
                            finished_emitted = True

            except Exception as exc:
                logger.warning("SSE chunk parse/emit failed (stream_normalizer): %s", exc, exc_info=True)

        if not finished_emitted:
            # Fallback for providers (like vsllm-claude/grok-4.5) that drop the message_delta terminal
            finish = "tool_calls" if current_tool_index >= 0 else "stop"
            yield self._encode_openai_chunk({
                "id": f"chatcmpl-bsl-{created}",
                "object": "chat.completion.chunk",
                "created": created,
                "model": self.model_name,
                "choices": [{
                    "index": 0,
                    "delta": {},
                    "finish_reason": finish,
                }],
            })

        # Emit DONE
        yield b"data: [DONE]\n\n"

    # ──────────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────────

    @staticmethod
    def _encode_anthropic_event(event_type: str, data: Dict[str, Any]) -> bytes:
        """Encode an Anthropic SSE event."""
        return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")

    @staticmethod
    def _encode_openai_chunk(data: Dict[str, Any]) -> bytes:
        """Encode an OpenAI SSE chunk."""
        return f"data: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")
