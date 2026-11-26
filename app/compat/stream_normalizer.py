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
from typing import AsyncIterator, Dict, Any, List, Optional
from app.compat.adapters.gemini import bound_tool_call_id, _mint_tool_call_id
import asyncio
import json
import time
import codecs
import logging

logger = logging.getLogger(__name__)


def _as_obj(value):
    """Dict-or-empty helper for JSON nulls (`"delta": null` must not crash)."""
    return value if isinstance(value, dict) else {}


# BUG A streaming rescue helpers (delimiter-scoped buffering for text-form
# tool calls emitted by GLM/Opus/Sonnet). See app/middleware/glm_tools.py.
from app.middleware.glm_tools import (
    StreamTextToolRescue,
    find_earliest_opener,
    find_block_closer,
    find_hold_tail,
    parse_streamed_tool_block,
    strip_orphan_ifm_close,
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

    def __init__(
        self,
        source_dialect: str,
        target_dialect: str,
        model_name: str = "bsl-routed",
        tools_in_request: bool = False,
        post_block_ping_idle_s: float = 0.0,
        tool_choice_required: bool = False,
    ):
        self.source = source_dialect
        self.target = target_dialect
        self.model_name = model_name
        # BUG A gate: the text-form tool-call rescue only activates when the
        # request declared tools (Split Pump contract: tool-less chats and
        # structured-call streams stay pure passthrough).
        self.tools_in_request = bool(tools_in_request)
        self.tool_choice_required = bool(tool_choice_required)
        # Kiro/Anthropic-SSE ping-tail guard (default 0.0 = disabled): when > 0,
        # if a deliverable block (text/tool_use) has already emitted
        # content_block_stop and upstream emits ONLY `event: ping` / `: ping` /
        # silence for at least the tool-aware threshold with zero new block
        # starts/deltas, finalize cleanly instead of hanging 60-360s.
        self.post_block_ping_idle_s = max(0.0, float(post_block_ping_idle_s or 0.0))

    def _effective_post_block_idle_s(self, saw_tool_use: bool) -> float:
        base = float(getattr(self, "post_block_ping_idle_s", 0.0) or 0.0)
        if base <= 0.0:
            return 0.0
        # Never cut after text-only when tool_choice forces a tool call
        if getattr(self, "tool_choice_required", False) and not saw_tool_use:
            return 0.0
        # When tools are declared and only preamble text closed so far, Kiro/Bedrock
        # can spend 20-55s buffering a large tool_use input JSON before content_block_start
        if getattr(self, "tools_in_request", False) and not saw_tool_use:
            return max(base, 60.0)
        # After at least one tool_use closed, allow 30s for parallel tool_use #2+
        if saw_tool_use:
            return max(base, 30.0)
        return base

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
        cached_tokens = 0
        cache_write_tokens = 0
        stop_reason = "end_turn"
        reasoning_buffer = ""
        thinking_emitted = False  # a thinking block was flushed mid-stream

        buffer = ""
        decoder = codecs.getincrementaldecoder("utf-8")()

        # ── BUG A: text-form tool-call rescue state ──────────────────────
        # Active only when the request declared tools; otherwise the stream
        # stays a pure forwarder. Buffering is decided by DELIMITERS (plus
        # markdown code-fence protection), never by timers.
        rescue_active = bool(self.tools_in_request)
        rescue_sm = StreamTextToolRescue(max_hold=96, max_block=131072) if rescue_active else None

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

        def _flush_thinking():
            """Yield a complete thinking block for pending reasoning_content.

            Interleaved reasoning (C1 fix, 2026-09-06): reasoning_content deltas
            are buffered and flushed as a thinking block the moment non-reasoning
            content (text) arrives, or at end-of-stream when the stream also
            carried text/tools. A reasoning-ONLY stream never reaches this — it
            keeps the legacy text-block flush below byte-identical.
            """
            nonlocal reasoning_buffer, current_tool_index, thinking_emitted
            if not reasoning_buffer:
                return
            block_index = current_tool_index
            current_tool_index += 1
            yield self._encode_anthropic_event("content_block_start", {
                "type": "content_block_start",
                "index": block_index,
                "content_block": {"type": "thinking", "thinking": ""},
            })
            yield self._encode_anthropic_event("content_block_delta", {
                "type": "content_block_delta",
                "index": block_index,
                "delta": {"type": "thinking_delta", "thinking": reasoning_buffer},
            })
            yield self._encode_anthropic_event("content_block_stop", {
                "type": "content_block_stop",
                "index": block_index,
            })
            reasoning_buffer = ""
            thinking_emitted = True

        def _register_rescued(calls: List[Dict[str, Any]]) -> None:
            """Add rescued text-form tool calls to tool_blocks.

            Rescued calls have no OpenAI stream index; they get high keys so
            they can never collide with structured indices (small integers).
            """
            nonlocal current_tool_index
            for call in calls:
                fn = _as_obj(call.get("function") if isinstance(call, dict) else None)
                args = fn.get("arguments") or ""
                tool_blocks[1000 + len(tool_blocks)] = {
                    "index": current_tool_index,
                    "id": bound_tool_call_id(call.get("id") or _mint_tool_call_id(fn.get('name') or 'function', len(tool_blocks))),
                    "name": fn.get("name", ""),
                    "args_buffer": args,
                    "arg_fragments": [args] if args else [],
                    # SIG-CARRY (Gemini 3.1-Pro 400 fix, 2026-09-21): rescued
                    # calls lost their upstream signature through the text
                    # rescue path. The rescue parser drops non-JSON fields, so
                    # a signature surviving here is best-effort only -- but if
                    # one IS present we must not drop it.
                    "thought_signature": (
                        call.get("thought_signature")
                        if isinstance(call.get("thought_signature"), str) and call.get("thought_signature")
                        else ""
                    ),
                }
                current_tool_index += 1

        def _drain_rescue(delta_str: str):
            """Feed delta_str into StreamTextToolRescue (fenced-code-safe)."""
            if rescue_sm is None:
                return
            safe_txt, rescued_calls = rescue_sm.push(delta_str)
            if safe_txt:
                for _ev in _emit_text(safe_txt):
                    yield _ev
            if rescued_calls:
                _register_rescued(rescued_calls)

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
            # SIG-EMIT (Gemini 3.1-Pro 400 fix, 2026-09-21): re-emit the
            # carried signature as a TOP-LEVEL field on the tool_use
            # content_block_start event. Anthropic's protocol places the
            # signature sibling to "input" inside content_block, but the
            # model-side signature contract must survive the stream round
            # trip: clients that understand it echo it back on the next
            # turn's tool_result; without it Gemini 3.1-Pro 400s on the
            # next-turn echo of an unsigned functionCall part.
            _cb: Dict[str, Any] = {
                "type": "tool_use",
                "id": tc_data["id"],
                "name": tc_data["name"],
                "input": {},
            }
            _sig = tc_data.get("thought_signature")
            if isinstance(_sig, str) and _sig:
                _cb["thought_signature"] = _sig
            yield self._encode_anthropic_event("content_block_start", {
                "type": "content_block_start",
                "index": tc_data["index"],
                "content_block": _cb,
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
                    input_tokens = u.get("prompt_tokens") or u.get("input_tokens") or input_tokens
                    output_tokens = u.get("completion_tokens") or u.get("output_tokens") or output_tokens
                    _ptd = u.get("prompt_tokens_details") if isinstance(u.get("prompt_tokens_details"), dict) else {}
                    cached_tokens = int(_ptd.get("cached_tokens") or u.get("cache_read_input_tokens") or cached_tokens or 0)
                    cache_write_tokens = int(_ptd.get("cache_write_tokens") or u.get("cache_creation_input_tokens") or cache_write_tokens or 0)

                choices = data.get("choices") or []
                if not choices:
                    if isinstance(data.get("error"), (dict, str)) and data.get("error"):
                        _err_v = data["error"]
                        _err_msg = _err_v.get("message", str(_err_v)) if isinstance(_err_v, dict) else str(_err_v)
                        yield self._encode_anthropic_event("error", {
                            "type": "error",
                            "error": {"type": "upstream_error", "message": str(_err_msg)[:1000]},
                        })
                        yield self._encode_anthropic_event("message_stop", {"type": "message_stop"})
                        return
                    continue

                choice = choices[0]
                delta = _as_obj(choice.get("delta"))
                finish = choice.get("finish_reason")

                # Text content delta
                text_content = delta.get("content")
                if text_content:
                    if reasoning_buffer:
                        # Interleaved reasoning (C1): flush pending reasoning as
                        # a thinking block before any non-reasoning content.
                        for _tev in _flush_thinking():
                            yield _tev
                    if rescue_active:
                        for _rev in _drain_rescue(text_content):
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

                # Reasoning content delta (max-thinking models: effort max; supports OpenRouter/MiMo `reasoning`)
                reasoning_content = delta.get("reasoning_content") or delta.get("reasoning")
                if reasoning_content:
                    reasoning_buffer += reasoning_content

                # Tool call delta (modern tool_calls or legacy function_call)
                tool_calls = delta.get("tool_calls") or []
                legacy_call = delta.get("function_call")
                if not tool_calls and isinstance(legacy_call, dict) and (
                    legacy_call.get("name") or "arguments" in legacy_call
                ):
                    # SIG-CARRY legacy shim (Gemini 3.1-Pro 400 fix,
                    # 2026-09-21): forward the legacy function_call's
                    # signature onto the synthesized tool_calls entry so
                    # legacy dialects are not silently downgraded to
                    # unsigned calls.
                    tool_calls = [{
                        "index": 0,
                        "id": "",
                        "function": legacy_call,
                        "thought_signature": legacy_call.get("thought_signature"),
                    }]
                for tc in tool_calls:
                    tc_index = tc.get("index", 0)
                    tc_id = tc.get("id", "")
                    function = _as_obj(tc.get("function"))
                    tc_name = function.get("name", "")
                    tc_args = function.get("arguments", "")
                    # SIG-CARRY (Gemini 3.1-Pro 400 fix, 2026-09-21): OpenAI
                    # tool_call deltas may carry the BSL-internal snake_case
                    # thought_signature carrier (minted by the antigravity
                    # upstream translator). Persist it into tool_blocks so
                    # _tool_events can re-emit it on the tool_use block;
                    # dropping it here is why Antigravity IDE received
                    # unsigned tool_use blocks and Gemini 3.1-Pro 400'd the
                    # next-turn echo. Some dialects nest the carrier inside
                    # the function object, so read both levels.
                    tc_sig = tc.get("thought_signature")
                    if not (isinstance(tc_sig, str) and tc_sig):
                        tc_sig = function.get("thought_signature")
                    if not (isinstance(tc_sig, str) and tc_sig):
                        tc_sig = ""

                    if tc_index not in tool_blocks:
                        block_index = current_tool_index
                        current_tool_index += 1
                        tool_blocks[tc_index] = {
                            "index": block_index,
                            "id": bound_tool_call_id(tc_id or _mint_tool_call_id(tc_name or 'function', len(tool_blocks))),
                            "name": tc_name,
                            "args_buffer": "",
                            "arg_fragments": [],
                            "thought_signature": tc_sig,
                        }
                    else:
                        if tc_id:
                            tool_blocks[tc_index]["id"] = tc_id
                        if tc_name:
                            tool_blocks[tc_index]["name"] = tc_name
                        if tc_sig:
                            tool_blocks[tc_index]["thought_signature"] = tc_sig

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
                    elif finish in ("content_filter", "refusal", "safety"):
                        stop_reason = "refusal"
                    elif finish == "stop":
                        stop_reason = "end_turn"

        # Flush a final line if the stream ended without a trailing newline.
        tail = decoder.decode(b"", final=True)
        if tail:
            buffer += tail

        # BUG A -- flush any rescue hold. An unclosed block at EOF cannot be
        # rescued; emit it as text (fail-open) rather than swallow it.
        if rescue_sm is not None:
            _eof_txt, _eof_calls = rescue_sm.flush_eof()
            if _eof_txt:
                for _rev in _emit_text(_eof_txt):
                    yield _rev
            if _eof_calls:
                _register_rescued(_eof_calls)

        if buffer.strip():
            line = buffer.strip()
            if line.startswith("data: ") and line[6:].strip() != "[DONE]":
                try:
                    data = json.loads(line[6:].strip())
                except json.JSONDecodeError:
                    data = None
                if data and data.get("usage"):
                    u = data["usage"]
                    input_tokens = u.get("prompt_tokens") or u.get("input_tokens") or input_tokens
                    output_tokens = u.get("completion_tokens") or u.get("output_tokens") or output_tokens
                    _ptd = u.get("prompt_tokens_details") if isinstance(u.get("prompt_tokens_details"), dict) else {}
                    cached_tokens = int(_ptd.get("cached_tokens") or u.get("cache_read_input_tokens") or cached_tokens or 0)
                    cache_write_tokens = int(_ptd.get("cache_write_tokens") or u.get("cache_creation_input_tokens") or cache_write_tokens or 0)

        # BUG A -- rescued text-form calls arrive under finish_reason "stop".
        # Anthropic clients only execute tools when stop_reason is "tool_use",
        # so promote it whenever tool blocks exist. (Mirrors the finish_reason
        # promotion the buffered normalize_glm_tool_calls path performs.)
        if tool_blocks and stop_reason == "end_turn":
            stop_reason = "tool_use"

        # Interleaved reasoning (C1): reasoning still pending at end-of-stream
        # belongs to a stream that also carried text/tools — surface it as a
        # thinking block. Only a reasoning-ONLY stream falls through to the
        # legacy text-block flush below (behavior preserved byte-identical,
        # including its double-stop quirk).
        if reasoning_buffer and (text_block_started or thinking_emitted or tool_blocks):
            for _tev in _flush_thinking():
                yield _tev

        # Max-thinking streams can end with zero content blocks (only
        # reasoning_content deltas). Emit the reasoning as the text block so
        # Anthropic clients never see an empty content list.
        if not tool_blocks and not text_block_started and reasoning_buffer:
            yield self._encode_anthropic_event("content_block_start", {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            })
            yield self._encode_anthropic_event("content_block_delta", {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": reasoning_buffer},
            })
            yield self._encode_anthropic_event("content_block_stop", {
                "type": "content_block_stop",
                "index": 0,
            })
            text_block_started = True

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
        _delta_usage: Dict[str, Any] = {
            "input_tokens": max(0, int(input_tokens or 0) - int(cached_tokens or 0) - int(cache_write_tokens or 0)),
            "output_tokens": output_tokens,
        }
        if cached_tokens > 0:
            _delta_usage["cache_read_input_tokens"] = cached_tokens
        if cache_write_tokens > 0:
            _delta_usage["cache_creation_input_tokens"] = cache_write_tokens
        yield self._encode_anthropic_event("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": _delta_usage,
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
        stream_id = f"chatcmpl-bsl-{created}"
        buffer = ""
        # C5 fix (2026-09-06): incremental UTF-8 decoder — a network split
        # mid-codepoint must be held across chunks, not raise (mirrors the
        # decoder already used in convert_openai_to_anthropic).
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        current_tool_id = None
        current_tool_name = None
        current_tool_index = -1
        finished_emitted = False
        input_tokens = 0
        cache_read_tokens = 0
        cache_write_tokens = 0
        prompt_tokens_inclusive = 0
        output_tokens = 0
        streamed_output_chars = 0
        block_types: Dict[int, str] = {}
        deliverable_block_closed_at = None
        ping_tail_terminated = False
        error_seen = False

        def _build_usage_dict() -> Optional[Dict[str, Any]]:
            _final_prompt = (
                prompt_tokens_inclusive
                if prompt_tokens_inclusive > 0
                else (input_tokens + cache_read_tokens + cache_write_tokens)
            )
            _eff_out = output_tokens
            if _eff_out <= 0 and streamed_output_chars > 0:
                _eff_out = max(1, streamed_output_chars // 4)
            if not (_final_prompt or _eff_out or cache_read_tokens or cache_write_tokens):
                return None
            _u_out: Dict[str, Any] = {
                "prompt_tokens": _final_prompt,
                "completion_tokens": _eff_out,
                "total_tokens": _final_prompt + _eff_out,
            }
            if cache_read_tokens > 0 or cache_write_tokens > 0:
                _u_out["prompt_tokens_details"] = {
                    **({"cached_tokens": cache_read_tokens} if cache_read_tokens > 0 else {}),
                    **({"cache_write_tokens": cache_write_tokens} if cache_write_tokens > 0 else {}),
                }
            return _u_out

        def _check_post_block_idle_expired() -> bool:
            if deliverable_block_closed_at is None:
                return False
            _cap = self._effective_post_block_idle_s(saw_tool_use=(current_tool_index >= 0))
            if _cap <= 0.0:
                return False
            if (time.monotonic() - deliverable_block_closed_at) >= _cap:
                print(
                    f"[StreamNormalizer] {self.model_name}: post-content_block_stop "
                    f"ping/idle tail exceeded {_cap:.0f}s — finalizing cleanly",
                    flush=True,
                )
                return True
            return False

        _aiter = anthropic_stream.__aiter__()
        _nt: Optional[asyncio.Task] = None
        try:
            while True:
                _wait_cap = 0.0
                if deliverable_block_closed_at is not None:
                    _eff_cap = self._effective_post_block_idle_s(saw_tool_use=(current_tool_index >= 0))
                    if _eff_cap > 0.0:
                        _rem = _eff_cap - (time.monotonic() - deliverable_block_closed_at)
                        if _rem <= 0.0:
                            if _check_post_block_idle_expired():
                                ping_tail_terminated = True
                                break
                        _wait_cap = max(0.02, _rem)
                try:
                    if _wait_cap > 0.0:
                        _nt = asyncio.ensure_future(_aiter.__anext__())
                        _done, _pend = await asyncio.wait({_nt}, timeout=_wait_cap)
                        if _pend:
                            _nt.cancel()
                            try:
                                await _nt
                            except BaseException:
                                pass
                            _nt = None
                            if _check_post_block_idle_expired():
                                ping_tail_terminated = True
                                break
                            continue
                        try:
                            chunk = _nt.result()
                        finally:
                            _nt = None
                    else:
                        chunk = await _aiter.__anext__()
                except StopAsyncIteration:
                    _nt = None
                    break

                try:
                    text_chunk = decoder.decode(chunk)
                    buffer += text_chunk
                    lines = buffer.split("\n")
                    buffer = lines.pop()

                    for line in lines:
                        if line.startswith(":"):
                            if _check_post_block_idle_expired():
                                ping_tail_terminated = True
                                break
                            continue
                        if line.startswith("data: "):
                            data_str = line[6:].strip()
                            if data_str == "[DONE]":
                                continue
                            try:
                                data = json.loads(data_str)
                            except json.JSONDecodeError:
                                continue

                            event_type = data.get("type", "")

                            if event_type == "ping":
                                if _check_post_block_idle_expired():
                                    ping_tail_terminated = True
                                    break
                                continue

                            if event_type in ("error", "response.failed"):
                                deliverable_block_closed_at = None
                                error_seen = True
                                _err_obj = data.get("error")
                                if not isinstance(_err_obj, dict):
                                    _err_obj = {
                                        "message": str(_err_obj or data.get("message") or "upstream stream error"),
                                        "type": "upstream_error",
                                    }
                                yield self._encode_openai_chunk({"error": _err_obj})
                                finished_emitted = True
                                continue

                            if event_type == "message_start":
                                _msg = _as_obj(data.get("message"))
                                if _msg.get("id"):
                                    stream_id = str(_msg["id"])
                                _start_usage = _msg.get("usage") or {}
                                if _start_usage:
                                    if _start_usage.get("prompt_tokens"):
                                        prompt_tokens_inclusive = int(_start_usage["prompt_tokens"])
                                    if _start_usage.get("input_tokens") is not None:
                                        input_tokens = int(_start_usage.get("input_tokens") or 0)
                                    if _start_usage.get("cache_read_input_tokens"):
                                        cache_read_tokens = int(_start_usage["cache_read_input_tokens"])
                                    if _start_usage.get("cache_creation_input_tokens"):
                                        cache_write_tokens = int(_start_usage["cache_creation_input_tokens"])

                            elif event_type == "content_block_delta":
                                deliverable_block_closed_at = None
                                delta = _as_obj(data.get("delta"))
                                delta_type = delta.get("type", "")

                                if delta_type == "text_delta":
                                    _txt_d = delta.get("text", "")
                                    if _txt_d:
                                        streamed_output_chars += len(_txt_d)
                                    yield self._encode_openai_chunk({
                                        "id": stream_id,
                                        "object": "chat.completion.chunk",
                                        "created": created,
                                        "model": self.model_name,
                                        "choices": [{
                                            "index": 0,
                                            "delta": {"content": _txt_d},
                                            "finish_reason": None,
                                        }],
                                    })

                                elif delta_type == "thinking_delta":
                                    # Anthropic extended-thinking block → OpenAI reasoning_content
                                    # This keeps Gemini thought parts alive during long thinking phases
                                    # so the IDE sees SSE frames instead of silence.
                                    thinking_text = delta.get("thinking", "")
                                    if thinking_text:
                                        streamed_output_chars += len(thinking_text)
                                        yield self._encode_openai_chunk({
                                            "id": stream_id,
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
                                        streamed_output_chars += len(partial)
                                        yield self._encode_openai_chunk({
                                            "id": stream_id,
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
                                deliverable_block_closed_at = None
                                block = _as_obj(data.get("content_block"))
                                _blk_idx = data.get("index")
                                if isinstance(_blk_idx, int):
                                    block_types[_blk_idx] = str(block.get("type") or "")
                                if block.get("type") == "tool_use":
                                    current_tool_index += 1
                                    current_tool_name = block.get("name", "")
                                    current_tool_id = bound_tool_call_id(block.get("id") or _mint_tool_call_id(current_tool_name or 'function', current_tool_index))
                                    # Preserve the provider ID: clients echo it in tool_result.
                                    # Synthesize only when the upstream omitted one.
                                    # Emit start chunk (arguments: "" init slot in accumulator)
                                    yield self._encode_openai_chunk({
                                        "id": stream_id,
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
                                        streamed_output_chars += len(args_str)
                                        yield self._encode_openai_chunk({
                                            "id": stream_id,
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

                            elif event_type == "content_block_stop":
                                _stop_idx = data.get("index")
                                _stop_type = block_types.get(_stop_idx, "") if isinstance(_stop_idx, int) else ""
                                if _stop_type in ("text", "tool_use"):
                                    deliverable_block_closed_at = time.monotonic()

                            elif event_type == "message_delta":
                                deliverable_block_closed_at = None
                                msg_delta = _as_obj(data.get("delta"))
                                stop_reason = msg_delta.get("stop_reason", "end_turn")
                                finish = "stop"
                                if stop_reason == "tool_use":
                                    finish = "tool_calls"
                                elif stop_reason == "max_tokens":
                                    finish = "length"
                                elif stop_reason in ("refusal", "content_filter"):
                                    finish = "content_filter"

                                # Aggregate usage from message_start + message_delta
                                _usage = data.get("usage") or {}
                                if _usage:
                                    if _usage.get("prompt_tokens"):
                                        prompt_tokens_inclusive = int(_usage["prompt_tokens"])
                                    if _usage.get("input_tokens"):
                                        input_tokens = int(_usage["input_tokens"])
                                    if _usage.get("cache_read_input_tokens"):
                                        cache_read_tokens = int(_usage["cache_read_input_tokens"])
                                    if _usage.get("cache_creation_input_tokens"):
                                        cache_write_tokens = int(_usage["cache_creation_input_tokens"])
                                    if _usage.get("output_tokens") is not None:
                                        output_tokens = int(_usage.get("output_tokens") or output_tokens)
                                    elif _usage.get("completion_tokens") is not None:
                                        output_tokens = int(_usage.get("completion_tokens") or output_tokens)
                                _finish_payload: Dict[str, Any] = {
                                    "id": stream_id,
                                    "object": "chat.completion.chunk",
                                    "created": created,
                                    "model": self.model_name,
                                    "choices": [{
                                        "index": 0,
                                        "delta": {},
                                        "finish_reason": finish,
                                    }],
                                }
                                _u_dict = _build_usage_dict()
                                if _u_dict:
                                    _finish_payload["usage"] = _u_dict
                                yield self._encode_openai_chunk(_finish_payload)
                                finished_emitted = True

                except Exception as exc:
                    logger.warning("SSE chunk parse/emit failed (stream_normalizer): %s", exc, exc_info=True)
                if ping_tail_terminated:
                    break
        finally:
            if _nt is not None and not _nt.done():
                _nt.cancel()
                try:
                    await _nt
                except BaseException:
                    pass
                _nt = None
            if ping_tail_terminated:
                _ac = getattr(anthropic_stream, "aclose", None)
                if callable(_ac):
                    try:
                        _ct = asyncio.ensure_future(_ac())
                        _d, _p = await asyncio.wait({_ct}, timeout=1.5)
                        if _p:
                            _ct.cancel()
                            _ct.add_done_callback(lambda t: None if t.cancelled() else t.exception())
                    except BaseException:
                        pass

        if not finished_emitted and not error_seen:
            # Fallback for providers (like vsllm-claude/grok-4.5 or Kiro ping-tail)
            # that drop the message_delta terminal — preserve message_start usage +
            # estimated completion_tokens so downstream meters & logs stay accurate.
            finish = "tool_calls" if current_tool_index >= 0 else "stop"
            _fallback_finish: Dict[str, Any] = {
                "id": stream_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": self.model_name,
                "choices": [{
                    "index": 0,
                    "delta": {},
                    "finish_reason": finish,
                }],
            }
            _u_dict = _build_usage_dict()
            if _u_dict:
                _fallback_finish["usage"] = _u_dict
            yield self._encode_openai_chunk(_fallback_finish)

        # Emit DONE
        yield b"data: [DONE]\n\n"

    async def convert_gemini_to_openai(
        self,
        gemini_stream: AsyncIterator[bytes],
    ) -> AsyncIterator[bytes]:
        """
        Convert a native Gemini streamGenerateContent SSE stream into
        OpenAI chat/completions SSE.

        Mapping (C1 fix, 2026-09-06):
          - part with thought:true + text -> delta.reasoning_content
          - plain text part               -> delta.content
          - functionCall part             -> delta.tool_calls entry
          - candidate finishReason        -> terminal finish_reason chunk
            (MAX_TOKENS -> "length", everything else -> "stop")

        Unknown part shapes are skipped (fail-open); a frame that fails to
        JSON-decode is skipped — a bad frame never crashes the stream.
        """
        created = int(time.time())
        buffer = ""
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        tool_call_counter = 0
        finish_emitted = False

        async for chunk in gemini_stream:
            buffer += decoder.decode(chunk)
            lines = buffer.split("\n")
            buffer = lines.pop()

            for line in lines:
                if not line.startswith("data: "):
                    continue
                data_str = line[6:].strip()
                if not data_str or data_str == "[DONE]":
                    continue
                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                if not isinstance(data, dict):
                    continue

                candidates = data.get("candidates") or []
                cand = candidates[0] if candidates and isinstance(candidates[0], dict) else {}
                content = _as_obj(cand.get("content"))
                for part in (content.get("parts") or []):
                    if not isinstance(part, dict):
                        continue
                    text = part.get("text")
                    if text and part.get("thought"):
                        yield self._encode_openai_chunk({
                            "id": f"chatcmpl-bsl-{created}",
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": self.model_name,
                            "choices": [{
                                "index": 0,
                                "delta": {"reasoning_content": text},
                                "finish_reason": None,
                            }],
                        })
                    elif text:
                        yield self._encode_openai_chunk({
                            "id": f"chatcmpl-bsl-{created}",
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": self.model_name,
                            "choices": [{
                                "index": 0,
                                "delta": {"content": text},
                                "finish_reason": None,
                            }],
                        })
                    elif "functionCall" in part:
                        fc = _as_obj(part.get("functionCall"))
                        name = fc.get("name", "")
                        args = json.dumps(fc.get("args") or {}, ensure_ascii=False)
                        # SIG-CARRY (Gemini 3.1-Pro 400 fix, 2026-09-21):
                        # stamp the thoughtSignature onto the emitted OpenAI
                        # tool_call. Google nests it EITHER as a sibling of
                        # functionCall on the part OR inside functionCall
                        # itself -- read both levels, and only add the key
                        # when a non-empty string is present (never emit an
                        # empty carrier on the wire).
                        _oai_tc: Dict[str, Any] = {
                            "index": tool_call_counter,
                            "id": f"call_{tool_call_counter}",
                            "type": "function",
                            "function": {"name": name, "arguments": args},
                        }
                        _sig = part.get("thoughtSignature")
                        if not (isinstance(_sig, str) and _sig):
                            _sig = fc.get("thoughtSignature")
                        if isinstance(_sig, str) and _sig:
                            _oai_tc["thought_signature"] = _sig
                            # Store in shared cache so the envelope converter can
                            # re-inject on next-turn echo even when the client drops
                            # the carrier (Anthropic client path, 2026-09-25).
                            try:
                                from app.compat.adapters.antigravity_upstream import (
                                    _signature_cache_key,
                                    _signature_cache_store,
                                )
                                _signature_cache_store(
                                    _signature_cache_key(
                                        self.model_name,
                                        _oai_tc["id"],
                                        name,
                                        fc.get("args") or {},
                                    ),
                                    _sig,
                                )
                            except Exception:
                                pass  # best-effort; never break the stream
                        yield self._encode_openai_chunk({
                            "id": f"chatcmpl-bsl-{created}",
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": self.model_name,
                            "choices": [{
                                "index": 0,
                                "delta": {"tool_calls": [_oai_tc]},
                                "finish_reason": None,
                            }],
                        })
                        tool_call_counter += 1

                finish_reason = cand.get("finishReason")
                if finish_reason and not finish_emitted:
                    finish_emitted = True
                    finish = "length" if finish_reason == "MAX_TOKENS" else "stop"
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
