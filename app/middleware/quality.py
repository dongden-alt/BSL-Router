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
    messages = list(original_payload.get("messages") or [])

    # Append the partial assistant output so the model sees what it already wrote
    messages.append({
        "role": "assistant",
        "content": partial_text,
    })

    # Append the continue instruction
    messages.append({
        "role": "user",
        "content": (
            f"{_CONTINUE_MARKER} Continue your previous response exactly from "
            f"where it was cut off. Do NOT repeat what you already wrote. "
            f"Do NOT add preamble. Resume directly from the last word."
        ),
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
    """

    def __init__(self, f_val: str = "", enabled: bool = True):
        self.f_val = f_val
        self.enabled = enabled
        self.partial_text = ""
        self.truncated = False
        self._buf = ""

    def feed(self, chunk: bytes) -> None:
        """Feed one raw upstream chunk (bytes)."""
        if not self.enabled:
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

    def _observe(self, data: Dict[str, Any]) -> None:
        # OpenAI chat.completion.chunk: delta.content + finish_reason
        choices = data.get("choices") or []
        if choices and isinstance(choices[0], dict):
            choice = choices[0]
            delta = choice.get("delta") or {}
            content = delta.get("content")
            if isinstance(content, str):
                self.partial_text += content
            if choice.get("finish_reason") == "length":
                self.truncated = True
            return

        # Anthropic Messages SSE
        if data.get("type") == "content_block_delta":
            delta = data.get("delta") or {}
            text = delta.get("text")
            if isinstance(text, str):
                self.partial_text += text
            return
        if data.get("type") == "message_delta":
            delta = data.get("delta") or {}
            if delta.get("stop_reason") == "max_tokens":
                self.truncated = True
            return

        # Gemini native chunk: candidates[].content.parts + finishReason
        candidates = data.get("candidates") or []
        if candidates and isinstance(candidates[0], dict):
            candidate = candidates[0]
            if candidate.get("finishReason") == "MAX_TOKENS":
                self.truncated = True
            parts = (candidate.get("content") or {}).get("parts") or []
            for part in parts:
                if isinstance(part, dict):
                    text = part.get("text")
                    if isinstance(text, str):
                        self.partial_text += text
