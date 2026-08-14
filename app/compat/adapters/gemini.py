"""
BSL Router — Antigravity (Google Cloud Code / Gemini private API) adapter.

Phase 5B-1. Ports 9Router's Antigravity MITM conversion into BSL as a set of
PURE functions (no FastAPI / httpx imports) so they are unit-testable offline.

Wire shape mirrors the existing Claude Code pattern:
    route `v1internal:generateContent` / `:streamGenerateContent`
      → unwrap Cloud Code wrapper + convert Gemini→OpenAI (this module)
      → `_process_chat_completion(openai_body, client_wants_gemini=True)`
      → egress converts OpenAI→Gemini on the way out (this module)

Authoritative reference: `.brain/harvest/antigravity_conversion_spec.md`
(spec §1–§8 + PORTING NOTES A–G). Skipped per §G: tool-name cloaking,
OAuth refresh, the sqlite `__nineRouterFinalThinkingPatch` DB lookup — BSL uses
its own provider keys and its own per-model thinking config.
"""
from __future__ import annotations

import json
import re
import time
import uuid as _uuid
from typing import Any, Dict, Optional

# ──────────────────────────────────────────────────────────────────────────────
# Constants — spec §2 / §6
# ──────────────────────────────────────────────────────────────────────────────

# Cap on generationConfig.maxOutputTokens for inbound antigravity requests (§8.4).
_MAX_OUTPUT_TOKENS_CAP = 16384

# thinkingConfig.thinkingBudget → reasoning_effort thresholds (§3, module 58028 `Vp`).
def _budget_to_effort(budget: Any) -> str:
    try:
        b = int(budget)
    except (TypeError, ValueError):
        return "high"
    if b <= 2048:
        return "low"
    if b <= 16384:
        return "medium"
    return "high"

# Model key mapping — spec §6. Exact synonym map first, then regex cascade.
MODEL_SYNONYMS: Dict[str, str] = {
    "gemini-default": "gemini-3.5-flash-low",
    "gemini-3.5-flash-high": "gemini-3-flash-agent",
    "gemini-3.5-flash-medium": "gemini-3.5-flash-low",
    "gemini-3.5-flash-extra-low": "gemini-3.5-flash-extra-low",
    "gemini-3.1-pro-high": "gemini-pro-agent",
    "gemini-3-pro-high": "gemini-pro-agent",
    "gemini-3-pro-low": "gemini-3.1-pro-low",
}

# Each entry: (compiled case-insensitive regex, alias). Order matters — first hit wins.
MODEL_PATTERNS = [
    (re.compile(r"flash.*extra.*low|extra.*low.*flash|flash.*low|low.*flash", re.I), "gemini-3.5-flash-extra-low"),
    (re.compile(r"flash.*medium|medium.*flash", re.I), "gemini-3.5-flash-low"),
    (re.compile(r"flash.*agent|agent.*flash|flash", re.I), "gemini-3.5-flash-high"),
    (re.compile(r"pro.*low|low.*pro", re.I), "gemini-3.1-pro-low"),
    (re.compile(r"gemini.*pro|pro.*gemini", re.I), "gemini-3.1-pro-high"),
    (re.compile(r"opus", re.I), "claude-opus-4-6-thinking"),
    (re.compile(r"sonnet|claude", re.I), "claude-sonnet-4-6"),
    (re.compile(r"gpt.*oss|oss", re.I), "gpt-oss-120b-medium"),
]

# Tab-completion models bypass mapping entirely (§6 / §8.11).
_TAB_RE = re.compile(r"^tab[_-]", re.I)

# OpenAI finish_reason → Gemini finishReason (§4a `bC`→`a_`).
# `tool_calls` maps to STOP (not FUNCTION_CALL): the Antigravity IDE
# treats FUNCTION_CALL as a terminal signal and stops processing, whereas
# STOP lets it consume the accumulated functionCall parts correctly.
_FINISH_MAP = {
    "stop": "STOP",
    "length": "MAX_TOKENS",
    "tool_calls": "STOP",
    "content_filter": "SAFETY",
    "function_call": "FUNCTION_CALL",
}

# Terminal SSE sentinel (§5). Antigravity SSE is plain `data:` lines, NO `event:` prefix.
SSE_DONE = b"data: [DONE]\n\n"


def terminal_error_frame(code: int, message: str, model: str = "") -> Dict[str, Any]:
    """The SOLE client-visible Gemini terminal frame for an error path.

    CONTRACT (2026-08-07 freeze fix). A Gemini error exit must emit exactly ONE
    parser-valid terminal frame: this response envelope, optionally followed by
    SSE_DONE. It must NOT be preceded by a top-level `{"error": {...}}` frame.

    Why no preceding error frame — the freeze evidence:
      The previous contract emitted three frames in order:
        1. `{"error": {"code", "message", "status"}}`   (top-level, no candidates)
        2. `{"response": {"candidates": [{finishReason: STOP}]}}`  (this helper)
        3. `data: [DONE]`                                (OpenAI sentinel)
      All-leaves-429 reproduction (2026-08-07) confirmed the IDE froze despite
      frame 2 carrying `finishReason`. The Antigravity Gemini parser treats a
      top-level `error` object as a terminal/error transition: once it consumes
      frame 1 it stops processing subsequent `candidates` frames, so the
      `finishReason` it actually needs to end the stream is never acted on and
      the IDE waits on a closed connection. The `[DONE]` sentinel is discarded
      by the Gemini wire protocol regardless. The fix is to emit frame 2 ALONE
      (plus the harmless `[DONE]` tail) — never frame 1.

    This frame carries what the parser actually terminates on — a candidate with
    a `finishReason` — and puts the error text in `parts` so the failure is
    VISIBLE in the transcript rather than silent.

    Uses finishReason STOP rather than a non-standard value on purpose: unknown
    enum values risk being dropped by the same strict parser, which would
    reintroduce the freeze. A visible error message plus a clean stop is the
    safer contract.
    """
    label = (message or "upstream error").strip()
    return {
        "response": {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [{"text": f"\n\n[BSL Router] upstream error {code}: {label}"}],
                    },
                    "finishReason": "STOP",
                    "index": 0,
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 0,
                "candidatesTokenCount": 0,
                "totalTokenCount": 0,
            },
            "modelVersion": model or "bsl-routed",
            "responseId": f"bslerr_{int(time.time() * 1000)}",
        }
    }

# ──────────────────────────────────────────────────────────────────────────────
# Antigravity IDE metadata injection (spec §8.16)
# ──────────────────────────────────────────────────────────────────────────────

def _inject_tool_metadata(args: Any, tool_name: str) -> Dict[str, Any]:
    """Inject required IDE metadata fields into functionCall args.

    The Antigravity IDE validates tool calls against a schema that requires
    `toolSummary` and `toolAction` fields. Many models (DeepSeek, Qwen, GLM, etc.)
    don't generate these metadata fields because they're not part of the actual
    tool parameters — they're IDE-internal validation metadata. This causes the
    IDE to reject valid tool calls with "required field toolSummary is missing".

    This function injects defaults when missing, ensuring ALL models can produce
    IDE-compatible tool calls.

    Args:
        args: The parsed functionCall args dict (may be empty or missing keys)
        tool_name: The tool name for generating default metadata values

    Returns:
        The args dict with toolSummary and toolAction populated if missing
    """
    if not isinstance(args, dict):
        args = {}
    args.setdefault("toolSummary", f"Executing {tool_name}")
    args.setdefault("toolAction", f"Running {tool_name}")
    return args



# ──────────────────────────────────────────────────────────────────────────────
# SSE wire helpers — spec §5
# ──────────────────────────────────────────────────────────────────────────────

def sse_data(obj: Dict[str, Any]) -> bytes:
    """Encode a Gemini chunk object as a `data: <json>\n\n` SSE frame (no event: prefix)."""
    return b"data: " + json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n\n"


def build_response_headers(stream: bool) -> Dict[str, str]:
    """Response headers for the antigravity egress (spec PORTING NOTES B.7)."""
    base = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "Access-Control-Allow-Origin": "*",
    }
    if stream:
        base["Content-Type"] = "text/event-stream"
    else:
        base["Content-Type"] = "application/json"
    return base


# ──────────────────────────────────────────────────────────────────────────────
# Ingress: unwrap + detect + model normalize + Gemini→OpenAI (spec §2, §3, §6)
# ──────────────────────────────────────────────────────────────────────────────

def unwrap_request(body: Dict[str, Any]) -> Dict[str, Any]:
    """Unwrap the Cloud Code envelope. Returns the inner `request` object, or
    `body` itself when there is no wrapper (bare Gemini request)."""
    inner = body.get("request") if isinstance(body, dict) else None
    if isinstance(inner, dict):
        return inner
    return body if isinstance(body, dict) else {}


def is_antigravity(body: Dict[str, Any]) -> bool:
    """Detect an antigravity (Cloud Code) request: userAgent=="antigravity" with
    contents present, falling back to a bare contents array (§1)."""
    if not isinstance(body, dict):
        return False
    inner = body.get("request")
    target = inner if isinstance(inner, dict) else body
    if body.get("userAgent") == "antigravity" and target.get("contents"):
        return True
    # Fall back to bare contents (no wrapper but Gemini-shaped body).
    if isinstance(inner, dict) and inner.get("contents"):
        return False
    return bool(body.get("contents")) and body.get("userAgent") == "antigravity"


def normalize_model(antigravity_id: str) -> str:
    """Map a Google-Cloud-Code model id to a BSL canonical id (§6).

    Synonym map → regex cascade → passthrough. `tab[_-]*` ids bypass mapping.
    BSL's own routing/aliases resolve the returned id further.
    """
    if not antigravity_id:
        return antigravity_id
    if _TAB_RE.match(antigravity_id):
        return antigravity_id
    if antigravity_id in MODEL_SYNONYMS:
        return MODEL_SYNONYMS[antigravity_id]
    for pattern, alias in MODEL_PATTERNS:
        if pattern.search(antigravity_id):
            return alias
    return antigravity_id


def _join_system_text(system_instruction: Any) -> str:
    """Join the text parts of a Gemini systemInstruction (may be dict or str)."""
    if isinstance(system_instruction, str):
        return system_instruction
    if isinstance(system_instruction, dict):
        parts = system_instruction.get("parts") or []
        return "".join(
            p.get("text", "") for p in parts if isinstance(p, dict)
        )
    return ""


def _sanitize_parameters(params: Any) -> Dict[str, Any]:
    """Sanitize a functionDeclaration `parameters` schema for OpenAI tools:
    lowercase schema `type` strings recursively, drop `enumDescriptions`, and
    synthesize a reason-explanation schema when none is present (§3, §8.7)."""
    if not isinstance(params, dict) or not params:
        return {
            "type": "object",
            "properties": {
                "reason": {"type": "string", "description": "Brief explanation"}
            },
            "required": ["reason"],
        }

    def _sanitize_schema(value: Any) -> Any:
        if isinstance(value, dict):
            sanitized = {}
            for key, child in value.items():
                if key == "enumDescriptions":
                    continue
                if key == "type" and isinstance(child, str):
                    sanitized[key] = child.lower()
                elif key == "type" and isinstance(child, list):
                    sanitized[key] = [
                        item.lower() if isinstance(item, str) else _sanitize_schema(item)
                        for item in child
                    ]
                else:
                    sanitized[key] = _sanitize_schema(child)
            return sanitized
        if isinstance(value, list):
            return [_sanitize_schema(item) for item in value]
        return value

    return _sanitize_schema(params)


def gemini_request_to_openai(req: Dict[str, Any], model: str) -> Dict[str, Any]:
    """Translate a Gemini (Cloud Code) request into an OpenAI ChatCompletion dict.

    Implements spec §3 table + §8 edge cases. The returned dict is consumed by
    `_process_chat_completion`; BSL's own thinking patch re-applies per-provider
    thinking downstream, so the `reasoning_effort` hint here is advisory.
    """
    req = req if isinstance(req, dict) else {}
    openai_body: Dict[str, Any] = {
        "model": model,
        "messages": [],
        "stream": True,  # antigravity streams by default (§8.12); route overrides
    }

    # ── systemInstruction → system message ──────────────────────────────
    sys_text = _join_system_text(req.get("systemInstruction"))
    if sys_text:
        openai_body["messages"].append({"role": "system", "content": sys_text})

    # ── generationConfig → scalars ──────────────────────────────────────
    gc = req.get("generationConfig") or {}
    if isinstance(gc, dict):
        if "maxOutputTokens" in gc:
            try:
                openai_body["max_tokens"] = min(int(gc["maxOutputTokens"]), _MAX_OUTPUT_TOKENS_CAP)
            except (TypeError, ValueError):
                pass
        if "temperature" in gc and gc["temperature"] is not None:
            openai_body["temperature"] = gc["temperature"]
        if "topP" in gc and gc["topP"] is not None:
            openai_body["top_p"] = gc["topP"]
        if "topK" in gc and gc["topK"] is not None:
            openai_body["top_k"] = gc["topK"]  # non-standard but preserved (§3)
        tc = gc.get("thinkingConfig")
        if isinstance(tc, dict) and "thinkingBudget" in tc:
            openai_body["reasoning_effort"] = _budget_to_effort(tc["thinkingBudget"])

    # ── tools → OpenAI tools + VALIDATED toolConfig ─────────────────────
    openai_tools: list = []
    for tool in (req.get("tools") or []):
        if not isinstance(tool, dict):
            continue
        for decl in (tool.get("functionDeclarations") or []):
            if not isinstance(decl, dict):
                continue
            _tool_name = decl.get("name")
            if not isinstance(_tool_name, str) or not _tool_name.strip():
                continue
            openai_tools.append({
                "type": "function",
                "function": {
                    "name": _tool_name.strip(),
                    "description": decl.get("description", ""),
                    "parameters": _sanitize_parameters(decl.get("parameters")),
                },
            })
    if openai_tools:
        openai_body["tools"] = openai_tools
        # §8.6: VALIDATED mode only asserted when tools are present.
        tc = req.get("toolConfig")
        mode = None
        if isinstance(tc, dict):
            fcc = tc.get("functionCallingConfig")
            if isinstance(fcc, dict):
                mode = fcc.get("mode")
        openai_body["tool_choice"] = "auto"  # OpenAI-side equivalent; mode VALIDATED recorded below
        # Carry the validated-mode intent so callers/tests can assert it.
        openai_body["x_gemini_tool_mode"] = mode or "VALIDATED"
    # safetySettings are intentionally stripped (§8.5).

    # ── Tool-call id uniqueness (root-cause fix for pix4k 502 / bare-origin 400) ──
    # The Gemini wire carries NO per-call id: functionCall/functionResponse pair
    # by position+name. Minting ids as f"call_{name}" collapses every invocation
    # of the same tool onto ONE id, so a long agent session emits e.g. 30 tool_use
    # blocks all id="call_run_command". Anthropic REQUIRES globally-unique
    # tool_use.id; the duplicate ids make the /v1/messages body malformed, which
    # Cloudflare-fronted pix4k returns as a 502 HTML page and bare origins (Grok/
    # Qwen) return as a 400. Mint a unique id per call and positionally pair each
    # functionResponse to it via this FIFO queue.
    _tool_call_seq = 0
    _pending_calls: list = []  # FIFO of (name, call_id) awaiting a functionResponse

    # ── contents → messages ─────────────────────────────────────────────
    for content in (req.get("contents") or []):
        if not isinstance(content, dict):
            continue
        role = content.get("role", "user")
        # §8.1: a content carrying functionResponse is re-roled to user.
        parts = content.get("parts") or []
        has_fn_response = any(
            isinstance(p, dict) and "functionResponse" in p for p in parts
        )
        if has_fn_response:
            openai_role = "user"
        elif role == "model":
            openai_role = "assistant"
        else:
            openai_role = role  # "user" → user, else passthrough

        msg: Dict[str, Any] = {"role": openai_role}
        text_parts: list = []
        image_parts: list = []
        tool_calls: list = []
        tool_results: list = []
        reasoning_parts: list = []

        for part in parts:
            if not isinstance(part, dict):
                continue

            if "functionCall" in part:
                fc = part["functionCall"] or {}
                name = fc.get("name") if isinstance(fc, dict) else None
                if not isinstance(name, str) or not name.strip():
                    continue
                name = name.strip()
                args = fc.get("args") or fc.get("arguments") or {}
                # Unique id per invocation (see FIFO note above). The monotonic
                # suffix guarantees uniqueness across the whole conversation while
                # keeping the readable tool name for debugging.
                _tool_call_seq += 1
                _call_id = f"call_{name}_{_tool_call_seq}"
                tool_calls.append({
                    "id": _call_id,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(args),
                    },
                })
                _pending_calls.append((name, _call_id))
                # thoughtSignature only kept alongside fn/text (§8.3) — no-op here.
                continue

            # functionResponse → OpenAI tool-role message (§3)
            if "functionResponse" in part:
                fr = part["functionResponse"] or {}
                name = fr.get("name", "")
                resp = fr.get("response")
                result = resp.get("result", resp) if isinstance(resp, dict) else resp
                # Pair by NAME first (oldest pending call with the same tool name),
                # so parallel calls answered out of order still map correctly.
                # Fall back to global FIFO, then to a fresh unique id if nothing is
                # pending (e.g. history truncated before the originating call).
                _matched_id = None
                for _qi, (_qname, _qid) in enumerate(_pending_calls):
                    if _qname == name:
                        _matched_id = _qid
                        _pending_calls.pop(_qi)
                        break
                if _matched_id is None and _pending_calls:
                    _matched_id = _pending_calls.pop(0)[1]
                if _matched_id is None:
                    _tool_call_seq += 1
                    _matched_id = f"call_{name}_{_tool_call_seq}"
                tool_results.append({
                    "role": "tool",
                    "tool_call_id": _matched_id,
                    "content": json.dumps(result),
                })
                continue

            # inlineData → image_url data URI (§3)
            if "inlineData" in part:
                idata = part["inlineData"] or {}
                mime = idata.get("mimeType") or "image/png"
                data = idata.get("data", "")
                image_parts.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{data}"},
                })
                continue

            # thought:true (+ text) → reasoning_content channel (§3)
            if part.get("thought") is True:
                # §8.2: pure-thought parts (no text/fn) are dropped on request.
                if part.get("text"):
                    reasoning_parts.append(part.get("text"))
                # thoughtSignature dropped unless with fn/text (handled above/below).
                continue

            # plain text / thoughtSignature-with-text (§8.3: keep text)
            if "text" in part and part["text"] is not None:
                text_parts.append(part["text"])
                continue

        # §Ordering: tool-result messages MUST follow the assistant message that
        # issued the call.  The Gemini wire puts functionCall in model-role and
        # functionResponse in the NEXT user-role content block, so tool_results
        # only ever accumulates on a pure-functionResponse block (has_fn_response=True).
        # In that case we skip the trailing empty message and only emit the tool
        # messages — which naturally come after the previously-appended assistant turn.
        if has_fn_response:
            for tr in tool_results:
                openai_body["messages"].append(tr)
            # Nothing else left for this block.
            continue

        # Assemble the main message for this content block.
        if openai_role == "tool":
            # A bare tool-role content (rare) — fold text into content.
            msg["content"] = "\n".join(text_parts) if text_parts else ""
            openai_body["messages"].append(msg)
            continue

        if image_parts:
            content_list = []
            if text_parts:
                content_list.append({"type": "text", "text": "\n".join(text_parts)})
            content_list.extend(image_parts)
            msg["content"] = content_list
        else:
            # GAP-4 FIX: use "" not None — Anthropic-format providers reject
            # content:null on assistant messages (1210 parameter error).
            msg["content"] = "\n".join(text_parts) if text_parts else ""

        if reasoning_parts:
            msg["reasoning_content"] = "\n".join(reasoning_parts)
        if tool_calls:
            msg["tool_calls"] = tool_calls
            # An assistant turn that is purely a tool call may have no text;
            # content is already "" from above.
        openai_body["messages"].append(msg)

    return openai_body


# ──────────────────────────────────────────────────────────────────────────────
# Egress: OpenAI → Gemini (spec §4)
# ──────────────────────────────────────────────────────────────────────────────

def _usage_metadata(usage: Dict[str, Any]) -> Dict[str, Any]:
    """Map OpenAI usage → Gemini usageMetadata (§4a, §8.14)."""
    usage = usage or {}
    p = usage.get("prompt_tokens", 0) or usage.get("input_tokens", 0)
    q = usage.get("completion_tokens", 0) or usage.get("output_tokens", 0)
    meta: Dict[str, Any] = {
        "promptTokenCount": p,
        "candidatesTokenCount": q,
        "totalTokenCount": (p or 0) + (q or 0),
    }
    # §8.14: forward reasoning/cached counts when present.
    details = usage.get("completion_tokens_details") or {}
    if isinstance(details, dict) and details.get("reasoning_tokens"):
        meta["thoughtsTokenCount"] = details["reasoning_tokens"]
    pdetails = usage.get("prompt_tokens_details") or {}
    if isinstance(pdetails, dict) and pdetails.get("cached_tokens"):
        meta["cachedContentTokenCount"] = pdetails["cached_tokens"]
    return meta


def _new_state() -> Dict[str, Any]:
    """Fresh per-stream translation state for openai_chunk_to_gemini."""
    return {
        "toolCallAccum": {},   # index → {"id","name","args"}
        "responseId": None,
        "modelVersion": None,
        "usage": None,
    }


def openai_chunk_to_gemini(chunk: Dict[str, Any], state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Translate one OpenAI `chat.completion.chunk` into a Gemini chunk (§4a).

    Returns `{"response": <gemini_obj>}` or `None` for empty mid-stream deltas
    (translator emits nothing when there are no parts and no finish). The caller
    wraps `response` into a `data:` SSE frame via `sse_data`.

    Maintains per-stream `state` for tool-call accumulation and the
    responseId/modelVersion/usage carried until the finish chunk.
    """
    if not isinstance(chunk, dict):
        return None

    # Lazily initialize per-stream state so callers may pass a bare `{}`.
    if "toolCallAccum" not in state:
        state["toolCallAccum"] = {}
        state.setdefault("responseId", None)
        state.setdefault("modelVersion", None)
        state.setdefault("usage", None)

    # Track stable ids for the whole response.
    if not state.get("responseId"):
        state["responseId"] = chunk.get("id") or f"resp_{int(time.time() * 1000)}"
    if not state.get("modelVersion"):
        state["modelVersion"] = chunk.get("model")

    usage = chunk.get("usage")
    if usage:
        state["usage"] = usage

    choices = chunk.get("choices") or []
    if not choices:
        # A usage-only chunk (no choices) — emit usageMetadata if we have it.
        if state.get("usage"):
            obj = _base_gemini_response(state, parts=[{"text": ""}], finish_reason=None)
            obj["usageMetadata"] = _usage_metadata(state["usage"])
            state["usage"] = None
            return {"response": obj}
        return None

    choice = choices[0] if isinstance(choices[0], dict) else {}
    delta = choice.get("delta") or {}
    finish = choice.get("finish_reason")

    parts: list = []

    # reasoning_content → {thought:true, text} (§4a)
    # Keep thought:true for Antigravity too — IDE expects the thought part shape.
    rc = delta.get("reasoning_content")
    if rc:
        parts.append({"thought": True, "text": rc})

    # content → {text} (§4a)
    content = delta.get("content")
    if content:
        parts.append({"text": content})

    # tool_calls accumulate by index across chunks (§4a)
    for tc in (delta.get("tool_calls") or []):
        if not isinstance(tc, dict):
            continue
        idx = tc.get("index", 0)
        fn = tc.get("function") or {}
        slot = state["toolCallAccum"].get(idx)
        if slot is None:
            slot = {"id": tc.get("id", ""), "name": fn.get("name", ""), "args": ""}
            state["toolCallAccum"][idx] = slot
        else:
            if tc.get("id"):
                slot["id"] = tc["id"]
            if fn.get("name"):
                slot["name"] = fn["name"]
        if fn.get("arguments"):
            slot["args"] += fn["arguments"]

    # On finish: flush accumulated tool calls to functionCall parts.
    truncated_tool_call = False
    if finish:
        for idx in sorted(state["toolCallAccum"].keys()):
            slot = state["toolCallAccum"][idx]
            name = slot["name"] or "tool"
            raw_args = slot["args"]
            try:
                args = json.loads(raw_args) if raw_args else {}
            except (json.JSONDecodeError, TypeError) as e:
                # L3 TRUNCATION GUARD — root cause of Opus/Sonnet TOOL_CALL_INCOMPLETE.
                # A thinking model can exhaust the output-token budget mid tool call,
                # cutting the argument JSON so it will not parse. The previous behavior
                # silently substituted args={}, which the Antigravity IDE receives as a
                # malformed call and reports as TOOL_CALL_INCOMPLETE. Instead of masking
                # the failure, DROP the truncated call and force a MAX_TOKENS finishReason
                # (below) so the IDE sees an honest truncation it can retry/continue from.
                print(
                    f"[BSL ROUTER] Tool args TRUNCATED for {name}: {e}. "
                    f"Dropping malformed call, signaling MAX_TOKENS. "
                    f"Raw args ({len(raw_args)} chars): {raw_args[:200]!r}",
                    flush=True,
                )
                truncated_tool_call = True
                continue
            # §8.16: inject Antigravity IDE metadata when model omits it.
            # The IDE validates tool calls against a schema requiring toolSummary
            # and toolAction. Many models (DeepSeek, Qwen, GLM) don't generate
            # these metadata fields — inject defaults to prevent IDE rejection.
            args = _inject_tool_metadata(args, name)
            parts.append({"functionCall": {"name": name, "args": args}})
        # §8.8: a finish with no produced parts must still keep parts non-empty.
        if not parts:
            parts.append({"text": ""})

    # §4a: emit nothing for empty mid-stream deltas (no parts, no finish).
    if not parts and not finish:
        return None

    finish_reason = _FINISH_MAP.get(finish, "STOP") if finish else None
    # A truncated tool call must surface as MAX_TOKENS regardless of the upstream
    # finish_reason (which may falsely claim tool_calls/stop), so the IDE does not
    # try to execute an argument-less call and instead treats it as a token-limit cutoff.
    if truncated_tool_call:
        finish_reason = "MAX_TOKENS"
    obj = _base_gemini_response(state, parts=parts, finish_reason=finish_reason)

    # Usage rides on whichever chunk carries it — usually the finish chunk.
    if state.get("usage"):
        obj["usageMetadata"] = _usage_metadata(state["usage"])
        state["usage"] = None

    return {"response": obj}


def gemini_frame_has_content(frame: Dict[str, Any]) -> bool:
    """True only if a Gemini frame carries something the CLIENT CAN RENDER.

    BUG L (2026-08-04). This exists to answer one specific question correctly:
    "if we now fail over to another provider, would the user see a corrupted
    transcript?" Only rendered content can be corrupted, so only rendered
    content may disable fallback.

    NOT content (returns False):
      * usage-only chunks - `openai_chunk_to_gemini` emits `parts=[{"text": ""}]`
        purely to carry `usageMetadata` (see the `not choices` branch above).
      * a finish chunk whose tool calls were all dropped - §8.8 forces
        `parts=[{"text": ""}]` to keep `parts` non-empty.
      * empty-string text or thought deltas.

    IS content (returns True): non-empty text, non-empty thought text,
    functionCall, inlineData/fileData.

    A `finishReason` alone is deliberately NOT content: it ends the stream but
    renders nothing, and treating it as content would re-block fallback on
    exactly the zero-output finishes we need to fail over from.
    """
    if not isinstance(frame, dict):
        return False
    inner = frame.get("response") if "response" in frame else frame
    if not isinstance(inner, dict):
        return False
    for cand in inner.get("candidates") or []:
        if not isinstance(cand, dict):
            continue
        content = cand.get("content")
        if not isinstance(content, dict):
            continue
        for part in content.get("parts") or []:
            if not isinstance(part, dict):
                continue
            # Any non-empty text counts, whether or not it is a thought: the
            # IDE renders thoughts in the reasoning pane, so splicing a second
            # provider after one would still be visible corruption.
            if isinstance(part.get("text"), str) and part["text"] != "":
                return True
            if part.get("functionCall") or part.get("inlineData") or part.get("fileData"):
                return True
    return False


def gemini_frame_is_thought_only(frame: Dict[str, Any]) -> bool:
    """True only if EVERY part of the frame is a thought (reasoning) part.

    BUG N (2026-08-13). Pairs with `gemini_frame_has_content` at the Gemini
    egress pre-render buffer (main.py). A thought-only frame renders in the
    IDE's reasoning pane but nothing into the transcript body. While the
    stream has produced ONLY such frames (plus scaffolding), abandoning the
    leaf and failing over leaves the client with an incomplete reasoning pane
    and no body text — recoverable. The moment a visible-text or tool frame
    exists, the transcript is committed and fallback is forbidden.

    Classification is conservative in the SAFE direction: anything that is not
    provably a thought part (text, functionCall, inlineData, usage metadata,
    finish frames, malformed shapes) returns False so the buffer flushes and
    commits. A misclassified thought frame merely commits one fallback earlier
    than optimal; a misclassified visible frame would permit the transcript
    corruption this module exists to prevent.

    Frames with no content at all (usage-only, finish-with-empty-parts) return
    False: they are not thoughts, and the buffer only ever holds thought frames
    — they pass straight through unbuffered and uncommitted.
    """
    if not isinstance(frame, dict):
        return False
    inner = frame.get("response") if "response" in frame else frame
    if not isinstance(inner, dict):
        return False
    found = False
    for cand in inner.get("candidates") or []:
        if not isinstance(cand, dict):
            return False
        content = cand.get("content")
        if not isinstance(content, dict):
            return False
        for part in content.get("parts") or []:
            if not isinstance(part, dict):
                return False
            found = True
            if part.get("thought") is not True:
                return False
            if not isinstance(part.get("text"), str) or part["text"] == "":
                return False
            if part.get("functionCall") or part.get("inlineData") or part.get("fileData"):
                return False
    return found


def _base_gemini_response(state: Dict[str, Any], parts: list, finish_reason: Optional[str]) -> Dict[str, Any]:
    """Build the inner Gemini chunk object (§4a). Caller wraps as {"response": obj}."""
    candidate: Dict[str, Any] = {
        "content": {"role": "model", "parts": parts},
    }
    if finish_reason:
        candidate["finishReason"] = finish_reason
    obj: Dict[str, Any] = {
        "candidates": [candidate],
        "modelVersion": state.get("modelVersion"),
        "responseId": state.get("responseId"),
    }
    return obj


def openai_response_to_gemini(openai_resp: Dict[str, Any], model: str) -> Dict[str, Any]:
    """Render a non-streaming OpenAI completion as a single Gemini response (§4b).

    Returns the wrapped `{"response": {candidates, usageMetadata, modelVersion, responseId}}`.
    """
    openai_resp = openai_resp or {}
    choices = openai_resp.get("choices") or []
    choice = choices[0] if choices and isinstance(choices[0], dict) else {}
    message = choice.get("message") or {} if isinstance(choice, dict) else {}

    parts: list = []
    text = message.get("content")
    if text:
        parts.append({"text": text})

    # Tool calls → functionCall parts (§4b note).
    for tc in (message.get("tool_calls") or []):
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        fn_name = fn.get("name") if isinstance(fn, dict) else None
        # Guard: skip nameless calls — a functionCall with an empty name is
        # rejected by Gemini-side validators (mirror of L1/L2 guards above).
        if not isinstance(fn_name, str) or not fn_name.strip():
            continue
        fn_name = fn_name.strip()
        raw_args = fn.get("arguments", "{}")
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
        except (json.JSONDecodeError, TypeError):
            args = {}
        # §8.16: inject Antigravity IDE metadata when model omits it.
        args = _inject_tool_metadata(args, fn_name)
        parts.append({"functionCall": {"name": fn_name, "args": args}})

    if not parts:
        parts.append({"text": ""})

    finish = choice.get("finish_reason") if isinstance(choice, dict) else None
    finish_reason = _FINISH_MAP.get(finish, "STOP") if finish else "STOP"

    candidate = {
        "content": {"role": "model", "parts": parts},
        "finishReason": finish_reason,
        "index": 0,
    }

    obj: Dict[str, Any] = {
        "candidates": [candidate],
        "usageMetadata": _usage_metadata(openai_resp.get("usage") or {}),
        "modelVersion": model or openai_resp.get("model", ""),
        "responseId": openai_resp.get("id") or f"resp_{int(time.time() * 1000)}",
    }
    return {"response": obj}


def _fresh_response_id() -> str:
    return f"resp_{_uuid.uuid4().hex[:16]}"
