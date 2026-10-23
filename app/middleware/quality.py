"""
Middleware.quality — P3 Quality Gates: Anti-Stop Loop + Truncation Retry

Ports two 9Router strategies as stateless, fail-open transforms:

S3 — Anti-Stop Loop (finish_reason="length"):
    When the upstream truncated the response at max_tokens, BSL sends ONE
    continuation request: original messages + partial assistant output +
    a "CONTINUE from where you stopped" instruction. The two outputs are
    concatenated. Stateless — no session tracking, max 1 continuation.

S6 — Quality Gate (suspiciously short / no terminal punctuation):
    When a 200 response looks truncated (short + missing terminal punctuation
    + not a tool-call + not code), BSL retries ONCE with elevated max_tokens.
    If the retry is longer, it replaces the original.

Safety contract:
    1. Both strategies are opt-in via config: tools.anti_stop_loop / tools.quality_gate
    2. Max 1 retry each — no infinite loops.
    3. Stateless — no cross-request session state.
    4. Fail-open: any exception returns the original response unchanged.
    5. Only triggers on HTTP 200 with finish_reason="length" (S3) or short-content
       heuristic (S6). Never triggers on errors, tool_calls, or empty responses.
    6. Skips streaming responses (requires buffered content).
"""

import json
import re
from typing import Any, Dict, Optional, Tuple

# ── S3: Anti-Stop Loop ───────────────────────────────────────────────────────

# Marker appended to the continuation prompt so the model knows to resume.
_CONTINUE_MARKER = "[BSL_CONTINUE]"

# Extracts the assistant text content from an OpenAI-format response.
def _extract_assistant_text(openai_json: Dict[str, Any]) -> str:
    """Pull the text content from the first choice's message."""
    try:
        choices = openai_json.get("choices") or []
        if not choices:
            return ""
        msg = choices[0].get("message") or {}
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            # Multi-part content — concatenate text parts
            parts = []
            for p in content:
                if isinstance(p, dict):
                    t = p.get("text", "")
                    if t:
                        parts.append(t)
                elif isinstance(p, str):
                    parts.append(p)
            return "".join(parts)
        return ""
    except Exception:
        return ""


def _extract_finish_reason(openai_json: Dict[str, Any]) -> str:
    """Get finish_reason from first choice."""
    try:
        choices = openai_json.get("choices") or []
        if choices:
            return choices[0].get("finish_reason") or ""
    except Exception:
        pass
    return ""


def _extract_usage(openai_json: Dict[str, Any]) -> Dict[str, int]:
    """Extract usage metrics."""
    try:
        u = openai_json.get("usage") or {}
        return {
            "prompt_tokens": u.get("prompt_tokens", u.get("input_tokens", 0)) or 0,
            "completion_tokens": u.get("completion_tokens", u.get("output_tokens", 0)) or 0,
            "total_tokens": u.get("total_tokens", 0) or 0,
        }
    except Exception:
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def is_length_truncated(openai_json: Dict[str, Any]) -> bool:
    """S3 trigger: finish_reason is 'length' (max_tokens truncation)."""
    return _extract_finish_reason(openai_json) == "length"


def build_continuation_payload(
    original_payload: Dict[str, Any],
    partial_text: str,
    stream: bool = False,
) -> Optional[Dict[str, Any]]:
    """Build a continuation request for the Anti-Stop Loop.

    Appends the partial assistant output + a CONTINUE instruction to the
    original messages, so the model resumes from where it stopped.

    `stream=False` (default) keeps the legacy buffered behavior (S3 needs
    the full continuation to concatenate). `stream=True` keeps the response
    SSE-native so interactive clients (Antigravity IDE) never lose TTFT.

    Returns a NEW payload dict, or None if the partial text is empty.
    """
    if not partial_text or not partial_text.strip():
        return None

    new_payload = dict(original_payload)
    continue_prompt = (
        f"{_CONTINUE_MARKER} Continue your previous response exactly from "
        f"where it was cut off. Do NOT repeat what you already wrote. "
        f"Do NOT add preamble. Resume directly from the last word."
    )
    if "input" in original_payload and "messages" not in original_payload:
        inp = list(original_payload.get("input") or [])
        inp.append({
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": partial_text}],
        })
        inp.append({
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": continue_prompt}],
        })
        new_payload["input"] = inp
        new_payload["stream"] = True if original_payload.get("stream") else stream
        return new_payload

    messages = list(original_payload.get("messages") or [])

    # Append the partial assistant output so the model sees what it already wrote
    messages.append({
        "role": "assistant",
        "content": partial_text,
    })

    # Append the continue instruction
    messages.append({
        "role": "user",
        "content": continue_prompt,
    })

    new_payload["messages"] = messages
    new_payload["stream"] = stream
    return new_payload


def build_continuation_stream_payload(
    original_payload: Dict[str, Any],
    partial_text: str,
) -> Optional[Dict[str, Any]]:
    """Streaming-native continuation: same shape as S3 but keeps stream=True.

    Used by StreamTruncationDetector consumers so the continuation is spliced
    as SSE frames instead of buffered (no TTFT sacrifice in IDE sessions).
    """
    return build_continuation_payload(original_payload, partial_text, stream=True)


def merge_continuation_response(
    original_json: Dict[str, Any],
    continuation_json: Dict[str, Any],
) -> Dict[str, Any]:
    """Merge the continuation response into the original.

    Concatenates assistant text content. Updates usage (additive).
    Preserves the continuation's finish_reason (it's the latest).
    """
    try:
        orig_text = _extract_assistant_text(original_json)
        cont_text = _extract_assistant_text(continuation_json)

        merged = dict(original_json)
        merged_choices = list(merged.get("choices") or [])

        if merged_choices:
            choice = dict(merged_choices[0])
            msg = dict(choice.get("message") or {})
            msg["content"] = orig_text + cont_text
            choice["message"] = msg

            # Update finish_reason from continuation
            cont_finish = _extract_finish_reason(continuation_json)
            if cont_finish:
                choice["finish_reason"] = cont_finish

            merged_choices[0] = choice
            merged["choices"] = merged_choices

        # Merge usage additively
        orig_usage = _extract_usage(original_json)
        cont_usage = _extract_usage(continuation_json)
        merged_usage = {
            "prompt_tokens": orig_usage["prompt_tokens"] + cont_usage["prompt_tokens"],
            "completion_tokens": orig_usage["completion_tokens"] + cont_usage["completion_tokens"],
            "total_tokens": orig_usage["total_tokens"] + cont_usage["total_tokens"],
        }
        # Preserve original key naming convention
        if "usage" in original_json and isinstance(original_json["usage"], dict):
            u = dict(original_json["usage"])
            if "prompt_tokens" in u or "input_tokens" in u:
                _key = "prompt_tokens" if "prompt_tokens" in u else "input_tokens"
                u[_key] = merged_usage["prompt_tokens"]
            if "completion_tokens" in u or "output_tokens" in u:
                _key = "completion_tokens" if "completion_tokens" in u else "output_tokens"
                u[_key] = merged_usage["completion_tokens"]
            if "total_tokens" in u:
                u["total_tokens"] = merged_usage["total_tokens"]
            merged["usage"] = u
        else:
            merged["usage"] = merged_usage

        return merged
    except Exception as e:
        print(f"[AntiStop] merge failed (fail-open): {e}", flush=True)
        return original_json


# ── S6: Quality Gate (truncation heuristic) ──────────────────────────────────

# Minimum completion tokens to even consider truncation. Below this, the model
# likely had a reason to stop short (e.g. simple "ok" reply).
_MIN_TOKENS_FOR_GATE = 50

# Terminal punctuation that signals a complete response.
_TERMINAL_PUNCT = re.compile(r"[.!?;。！？؛]\s*$")

# Markdown/code block closers that signal completeness.
_BLOCK_CLOSER = re.compile(r"```\s*$")


def looks_truncated(openai_json: Dict[str, Any]) -> bool:
    """S6 heuristic: does this 200 response look truncated?

    Criteria (ALL must be true):
      1. finish_reason is NOT 'stop' (i.e. 'length' or None or unknown)
      2. Content is non-empty
      3. Does NOT end with terminal punctuation
      4. Does NOT end with a code block closer
      5. Has NO tool_calls
      6. Estimated output tokens >= _MIN_TOKENS_FOR_GATE
    """
    try:
        choices = openai_json.get("choices") or []
        if not choices:
            return False

        choice = choices[0]
        if not isinstance(choice, dict):
            return False

        # Must not have tool_calls (those have their own finish flow)
        msg = choice.get("message") or {}
        if isinstance(msg.get("tool_calls"), list) and msg["tool_calls"]:
            return False

        # Must not be 'stop' (stop = model finished naturally)
        finish = choice.get("finish_reason")
        if finish == "stop":
            return False

        content = msg.get("content")
        if not isinstance(content, str) or not content.strip():
            return False

        # Must have enough tokens to look like real output
        usage = _extract_usage(openai_json)
        if usage["completion_tokens"] < _MIN_TOKENS_FOR_GATE:
            return False

        text = content.rstrip()

        # Ends with terminal punctuation → complete
        if _TERMINAL_PUNCT.search(text):
            return False

        # Ends with code block closer → complete
        if _BLOCK_CLOSER.search(text):
            return False

        # All checks passed — looks truncated
        return True
    except Exception:
        return False


def should_retry_with_higher_budget(
    openai_json: Dict[str, Any],
    original_max_tokens: int,
) -> Tuple[bool, int]:
    """S6 decision: should we retry? Returns (should_retry, new_max_tokens).

    Elevates max_tokens by 2x (capped at 65535) for the retry.
    65535 (not 65536) because Qwen API hard-caps max_tokens at 65535; a
    retry with 65536 → 400 "max_tokens exceeds limit" which defeats the
    purpose of the quality gate retry.
    """
    if not looks_truncated(openai_json):
        return False, original_max_tokens

    new_mt = min((original_max_tokens or 32768) * 2, 65535)
    if new_mt <= (original_max_tokens or 0):
        return False, original_max_tokens

    return True, new_mt


# ── S3-Streaming: StreamTruncationDetector ────────────────────────────
# Buffered S3 can't run on SSE paths (it would kill TTFT). This detector
# taps the raw upstream byte stream, accumulates text deltas, and flags
# truncation the moment a terminal finish_reason="length" / "max_tokens" /
# "MAX_TOKENS" frame passes through — WITHOUT holding back any chunk.
# The caller splices ONE continuation stream before emitting its own [DONE].

class StreamTruncationDetector:
    """Stateful SSE tap that detects max_tokens truncation mid-stream.

    Feed raw upstream bytes (any dialect: OpenAI, Anthropic, or Gemini
    SSE); `truncated` is set when the terminal frame carries a length-style
    stop reason, and `partial_text` accumulates every text delta seen so
    far (usable as the assistant prefix for a continuation request).

    Fail-open: any parse error is swallowed; a broken feed simply never
    sets `truncated`, so the caller falls through to the normal stream.

    Two independent flags control the tap (decoupled 2026-09-29):

    - ``enabled`` gates the truncation-detection + continuation-splice
      logic. When False, ``truncated`` is never set and no splice may
      ever fire -- the Qwen anti-stop behavior is bit-identical to a
      detector that saw no length frame.
    - ``accumulate`` gates the ``partial_text`` text-accumulation path.
      When True the tap records every content/reasoning/text/thinking
      delta even when ``enabled`` is False. This lets the usage-backfill
      helper see real delivered text on NON-Qwen providers (mintrouter/*),
      where the splice gate is intentionally off. Accumulation appends to a
      fragment LIST (no extra JSON parsing beyond ``_observe``) -- see
      ``_append_text`` for why this must never be a growing string.

    Safety cap: ``partial_text`` is bounded by ``PARTIAL_TEXT_CAP_BYTES``
    (2 MB) so a 100k-token runaway stream cannot balloon memory. The
    accumulator stops buffering at the cap but ``partial_len`` keeps counting
    total delivered chars so the len//4 usage estimate stays representative.
    """

    # Upper bound on the buffered text. 2 MB comfortably exceeds the largest
    # plausible single-request output while keeping a runaway stream from
    # growing memory without limit. ASCII marker only.
    PARTIAL_TEXT_CAP_BYTES = 2 * 1024 * 1024

    def __init__(self, f_val: str = "", enabled: bool = True, accumulate: bool = None):
        self.f_val = f_val
        self.enabled = enabled
        # `accumulate` defaults to "inherit from enabled" so every historical
        # construction (bare, or enabled=False) keeps its exact prior behavior;
        # only an explicit accumulate=True decouples text tapping from a closed
        # splice gate (non-Qwen streams pass accumulate=is_stream from main.py).
        self.accumulate = bool(enabled) if accumulate is None else bool(accumulate)
        # Delivered text is held as a LIST OF FRAGMENTS, not a growing string.
        # `_joined` caches the concatenation and is invalidated on every append,
        # so reads at stream end cost one O(n) join total. `partial_text` is a
        # property over these; nothing outside this class may assume a plain
        # attribute (the setter below keeps direct assignment working).
        self._parts: list = []
        self._parts_chars = 0
        self._joined = ""
        # Total delivered chars (uncapped) so the usage estimate stays
        # representative even after the buffer hit the byte cap.
        self.partial_len = 0
        self.tool_call_chars = 0
        self.truncated = False
        self._buf = ""

    def feed(self, chunk: bytes) -> None:
        """Feed one raw upstream chunk (bytes)."""
        # Accumulation is decoupled from the splice gate: run the text
        # tap whenever `accumulate` is True, even when `enabled` is False.
        # The detection/splice logic below stays gated on `enabled` only.
        if not (self.enabled or self.accumulate):
            return
        try:
            self._buf += chunk.decode("utf-8", errors="replace")
            lines = self._buf.split("\n")
            self._buf = lines.pop()
            for line in lines:
                stripped = line.strip()
                if not stripped.startswith("data: "):
                    continue
                payload = stripped[6:].strip()
                if payload == "[DONE]":
                    continue
                try:
                    data = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                self._observe(data)
        except Exception:
            pass  # fail-open: never break the stream for a detector bug

    @property
    def partial_text(self) -> str:
        """Full accumulated text -- a lazy, cached join of the fragment list.

        Every consumer keeps working unchanged: the Qwen continuation splice
        (main.py build_continuation_stream_payload) needs the COMPLETE text,
        the splice gate needs its truthiness, and the usage backfill needs its
        length. The join is cached and only redone after a new fragment is
        appended, so the several reads at stream end cost one O(n) join total.
        """
        if self._joined is None:
            self._joined = "".join(self._parts)
        return self._joined

    @partial_text.setter
    def partial_text(self, value: str) -> None:
        """Back-compat: a direct assignment replaces the accumulated fragments."""
        _v = value or ""
        self._parts = [_v] if _v else []
        self._parts_chars = len(_v)
        self._joined = _v

    def _append_text(self, text: str) -> None:
        """Record one text delta in O(1) amortized time and bounded memory.

        DO NOT "optimize" this back into ``self.partial_text += text``.
        CPython's in-place unicode resize only engages when the left operand is
        a refcount-1 LOCAL; an ATTRIBUTE target reallocates and copies the whole
        buffer on every delta, making accumulation quadratic. Measured on this
        very class (.brain/scratch/bench_real_detector.py, Py3.11.9): a 2M-char
        stream cost 34.08 s at 16-char deltas and 8.28 s at 64-char deltas of
        SYNCHRONOUS event-loop time -- the loop-stall class that took the IDE
        down before (AGENTS.md S2). The fragment list below is O(1) per delta.

        Buffering stops at PARTIAL_TEXT_CAP_BYTES (same observable cap as
        before: ``partial_text`` never exceeds it), while ``partial_len`` keeps
        the exact uncapped total so the len//4 usage estimate stays truthful.
        """
        if not text:
            return
        self.partial_len += len(text)
        if self._parts_chars >= self.PARTIAL_TEXT_CAP_BYTES:
            return  # buffer full -- keep counting partial_len only
        _room = self.PARTIAL_TEXT_CAP_BYTES - self._parts_chars
        _frag = text if len(text) <= _room else text[:_room]
        self._parts.append(_frag)
        self._parts_chars += len(_frag)
        self._joined = None  # invalidate the cached join

    def _observe(self, data: Dict[str, Any]) -> None:
        # OpenAI chat.completion.chunk: delta.content + finish_reason
        choices = data.get("choices") or []
        if choices and isinstance(choices[0], dict):
            choice = choices[0]
            delta = choice.get("delta") or {}
            content = delta.get("content")
            if isinstance(content, str) and self.accumulate:
                self._append_text(content)
            # REASONING continuation (2026-09-27): thinking-heavy streams (GLM,
            # o-series, reasoning Claude) put their partial output in
            # delta.reasoning_content, leaving `content` empty until the final
            # answer. On a mid-thinking transport death partial_text was "" ->
            # should_splice_continucontinuation's `not partial_text.strip()` gate
            # refused the splice -> terminal 502 even though the model had
            # produced continuable output. Accumulate the reasoning delta too so
            # the SAME provider/model can resume. This prefix is only ever sent
            # back to the SAME model, never spliced into a live client parser.
            reasoning = delta.get("reasoning_content")
            if isinstance(reasoning, str) and self.accumulate:
                self._append_text(reasoning)
            elif isinstance(delta.get("reasoning"), str) and self.accumulate:
                self._append_text(delta.get("reasoning"))
            if self.accumulate and isinstance(delta.get("tool_calls"), list):
                for _tc in delta["tool_calls"]:
                    if isinstance(_tc, dict):
                        _fn = _tc.get("function")
                        if isinstance(_fn, dict):
                            if isinstance(_fn.get("name"), str):
                                self.tool_call_chars += len(_fn["name"])
                            if isinstance(_fn.get("arguments"), str):
                                self.tool_call_chars += len(_fn["arguments"])
            # `enabled` is the truncation/splice gate: with it off, a length
            # frame must NEVER set truncated (Qwen anti-stop stays scoped).
            if self.enabled and choice.get("finish_reason") == "length":
                self.truncated = True
            return

        # Anthropic Messages SSE
        if data.get("type") == "content_block_start":
            if self.accumulate:
                _cb = data.get("content_block")
                if isinstance(_cb, dict) and _cb.get("type") == "tool_use":
                    self.tool_call_chars += len(str(_cb.get("name") or ""))
            return
        if data.get("type") == "content_block_delta":
            delta = data.get("delta") or {}
            text = delta.get("text")
            if isinstance(text, str) and self.accumulate:
                self._append_text(text)
            # Anthropic extended-thinking deltas carry their text in
            # delta.thinking, not delta.text -- same reasoning-stream gap.
            thinking = delta.get("thinking")
            if isinstance(thinking, str) and self.accumulate:
                self._append_text(thinking)
            if isinstance(delta.get("partial_json"), str) and self.accumulate:
                self.tool_call_chars += len(delta["partial_json"])
            return
        # Anthropic also emits a standalone thinking_delta event type.
        if data.get("type") == "thinking_delta":
            delta = data.get("delta") or {}
            thinking = delta.get("thinking")
            if isinstance(thinking, str) and self.accumulate:
                self._append_text(thinking)
            return
        if data.get("type") == "message_delta":
            delta = data.get("delta") or {}
            if self.enabled and delta.get("stop_reason") == "max_tokens":
                self.truncated = True
            return

        # OpenAI Responses SSE (Codex)
        ev_type = data.get("type")
        if ev_type in (
            "response.output_text.delta",
            "response.reasoning_summary_text.delta",
            "response.reasoning_text.delta",
        ):
            delta_str = data.get("delta")
            if isinstance(delta_str, str) and self.accumulate:
                self._append_text(delta_str)
            return
        if ev_type == "response.output_item.done":
            if self.accumulate:
                item = data.get("item")
                if isinstance(item, dict) and item.get("type") == "function_call":
                    name = item.get("name")
                    args = item.get("arguments")
                    if isinstance(name, str):
                        self.tool_call_chars += len(name)
                    if isinstance(args, str):
                        self.tool_call_chars += len(args)
                    elif args is not None:
                        try:
                            self.tool_call_chars += len(json.dumps(args, ensure_ascii=False))
                        except Exception:
                            pass
            return
        if ev_type == "response.incomplete":
            if self.enabled:
                resp_obj = data.get("response") if isinstance(data.get("response"), dict) else data
                inc = resp_obj.get("incomplete_details") if isinstance(resp_obj, dict) else None
                if isinstance(inc, dict) and inc.get("reason") in ("max_output_tokens", "length"):
                    self.truncated = True
            return

        # Gemini native chunk: candidates[].content.parts + finishReason
        candidates = data.get("candidates") or []
        if candidates and isinstance(candidates[0], dict):
            candidate = candidates[0]
            if self.enabled and candidate.get("finishReason") == "MAX_TOKENS":
                self.truncated = True
            parts = (candidate.get("content") or {}).get("parts") or []
            for part in parts:
                if isinstance(part, dict):
                    text = part.get("text")
                    if isinstance(text, str) and self.accumulate:
                        # Gemini thought parts carry the SAME text but with
                        # thought:true -- both are continuable model output, so
                        # accumulate regardless of the thought flag.
                        self._append_text(text)
                    if self.accumulate and isinstance(part.get("functionCall"), dict):
                        try:
                            self.tool_call_chars += len(json.dumps(part["functionCall"], ensure_ascii=False))
                        except Exception:
                            pass


# ── Transport-death continuation trigger ────────────────────────────────
# Extends the SAME AntiStop continuation splice that handles finish_reason="length"
# truncation to ALSO cover the one remaining force-stop: a mid-stream transport
# death AFTER partial content was already delivered (stats["out"] > 0).
#
# The transport-death case never sets `truncated` (no terminal length frame
# arrives — the socket just dies), but the detector has still accumulated
# `partial_text` from the deltas that did flow. That partial text is the model's
# OWN output, so a continuation request to the SAME provider/model is a coherent
# resume — no second provider is spliced into the live parser (the BUG J
# transcript-integrity invariant holds).
#
# The flag is written by app/main.py::_midstream_transport_fallback on its
# out>0 fall-through (out==0 still takes the existing combo failover). This name
# is the canonical key so every splice site and every test reads one symbol.

TRANSPORT_DIED_PARTIAL_FLAG = "transport_died_partial"


def should_splice_continuation(
    *,
    truncated: bool,
    transport_died_partial: bool,
    partial_text: str,
    infinite_retry_enabled: bool,
    used: bool,
) -> bool:
    """Single source of truth for the AntiStop continuation-splice gate.

    Fires when EITHER:
      * finish_reason="length" truncation was observed (truncated=True), OR
      * a mid-stream transport death left partial output
        (transport_died_partial=True) AND never-stop combo retry is opted in.

    Both branches additionally require:
      * the splice has not already been used this request (one-shot), AND
      * non-empty/whitespace partial_text (nothing to continue -> terminal).

    The transport-death branch is gated on infinite_retry_enabled so that
    settings.combo_infinite_retry=false preserves today's terminal-502 contract
    exactly. The length-truncation branch is NOT gated (unchanged behaviour).

    Pure and stateless: the caller owns _cont_state["used"] / stats; this only
    answers the yes/no so the caller can reuse its EXISTING splice block.
    """
    if used:
        return False
    if not (partial_text or "").strip():
        return False
    if truncated:
        return True
    # Transport-death branch: only when never-stop retry is opted in.
    return bool(transport_died_partial and infinite_retry_enabled)
