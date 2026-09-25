"""OpenAI → Google Cloud Code (Antigravity) upstream adapter.

THE VISION/COMPACTION 404s (2026-08-23). The antigravity provider is
type=oauth, format=openai, so combo entries dialed
``{base}/chat/completions`` on daily-cloudcode-pa.googleapis.com. That host
serves ONLY ``v1internal:generateContent`` RPC verbs — ``/chat/completions``
returns an HTML 404 page (observed live 2026-08-23 22:45: ``antigravity/
gemini-pro-agent`` and ``antigravity/gemini-3.6-flash-high`` both 404'd and
survived only via lucky combo fallback to tokenharbor). Vision and IDE
compaction calls died on every antigravity entry.

Fix — forge the EXACT envelope the Antigravity IDE sends. Contract verified
live (2026-08-23, HTTP 200 streams) against
https://daily-cloudcode-pa.googleapis.com:

  POST /v1internal:streamGenerateContent?alt=sse   (stream; alt=sse REQUIRED)
  Headers:
    Authorization: Bearer <ya29 token>          (ensure_fresh_token, unchanged)
    User-Agent: antigravity/ide/2.1.1 windows/amd64
      ^ REQUIRED. The generic ``google-api-nodejs-client/9.15.1`` stealth UA
        gets 403 SUBSCRIPTION_REQUIRED (#3501) from cloudaicompanion — Google
        ties the license to the client identity header.
  Body (Cloud Code envelope):
    {model, project, requestId, userAgent: "antigravity",
     requestType: "agent",
     request: {sessionId, contents, systemInstruction?, tools?,
               toolConfig?, generationConfig?}}

Model ids pass through VERBATIM: ``gemini-pro-agent`` and
``gemini-3.6-flash-high`` (the config model ids) return 200, while public
names (gemini-3-pro-preview, gemini-3-pro, ...) return 404 NOT_FOUND.

Response direction: Google streams Gemini ``data: {"candidates": [...]}``
SSE frames (identical to what the IDE consumes). This module converts them
to OpenAI ``chat.completion.chunk`` frames so BSL's existing OpenAI-canonical
pipeline (combo fallback rails, BUG L/N gates, zero-token watchdogs,
anthropic/gemini egress converters) operates unchanged.
"""

import hashlib
import json
import threading
import time
import uuid
from collections import OrderedDict
from typing import Any, Dict, List, Optional

# ── Verified upstream contract constants ────────────────────────────────────

ANTIGRAVITY_UPSTREAM_UA = "antigravity/ide/2.1.1 windows/amd64"
ANTIGRAVITY_DEFAULT_PROJECT = "ungoogly-rite-fd7ql"
ANTIGRAVITY_STREAM_URL_PATH = "/v1internal:streamGenerateContent?alt=sse"

# reasoning_effort (OpenAI vocab) → thinkingConfig.thinkingBudget (Gemini).
# Inverse of app.compat.adapters.gemini._budget_to_effort thresholds.
_EFFORT_TO_BUDGET = {
    "low": 2048,
    "medium": 8192,
    "high": 24576,
}

# Gemini finishReason → OpenAI finish_reason.
# STOP-with-functionCalls is upgraded to "tool_calls" by the translator
# (OpenAI contract); the IDE egress maps it back via _FINISH_MAP.
_FINISH_INVERSE = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "content_filter",
    "RECITATION": "content_filter",
    "PROHIBITED": "content_filter",
    "FUNCTION_CALL": "tool_calls",
    "OTHER": "stop",
}


# ── Router-side thought_signature cache (defense in depth, Step 3, 2026-09-21) ──
# Clients are NOT contractually obliged to round-trip the unknown
# thought_signature carrier, so the router caches every signature it stamps on
# an outbound OpenAI tool_call and re-injects it on the next-turn echo even when
# the client dropped it. Bounded LRU (512 entries, 900s TTL), pure in-memory,
# Lock-guarded. AGENTS.md §2: NO disk I/O, NO per-entry logging, nothing on the
# event loop -- a synchronous writer here once produced a 29.8 GB log and killed
# the IDE. Observability is two integer counters, read directly by tests.
_SIGNATURE_CACHE_MAX = 512
_SIGNATURE_CACHE_TTL = 3600.0
_SIGNATURE_CACHE: "OrderedDict[Any, Any]" = OrderedDict()
_SIGNATURE_CACHE_LOCK = threading.Lock()
SIGNATURE_CACHE_HITS = 0
SIGNATURE_CACHE_MISSES = 0
SIGNATURE_CACHE_FALLBACK_HITS = 0


def _args_digest(args: Any) -> str:
    """Canonical sha256 digest of tool-call args, shared by the primary key and
    the name/id-agnostic fallbacks so store-side and lookup-side always agree."""
    if isinstance(args, dict):
        canonical = json.dumps(args, sort_keys=True, ensure_ascii=False)
    elif isinstance(args, str):
        try:
            canonical = json.dumps(json.loads(args), sort_keys=True, ensure_ascii=False)
        except (json.JSONDecodeError, ValueError):
            canonical = args
    else:
        canonical = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _signature_cache_key(model: str, call_id: str, name: str, args: Any) -> tuple:
    """Composite cache key -- NEVER id-only.

    ``_frame_to_chunk`` mints ids as ``f"call_{name}_{counter}"`` from a
    PER-INSTANCE counter, so ``call_read_1`` recurs across turns and an id-only
    key collides. Adding model + name + a digest of the canonicalized arguments
    makes the key unique per logical call. Args are canonicalized identically on
    both the store and lookup sides (sort_keys) so a dict at store time and the
    JSON string echoed at lookup time hash the same.
    """
    return (model, call_id, name, _args_digest(args))


def _signature_cache_store(key: tuple, signature: str) -> None:
    """Insert/refresh a signature, evicting the oldest beyond the 512 cap."""
    if not (isinstance(signature, str) and signature):
        return
    now = time.time()
    with _SIGNATURE_CACHE_LOCK:
        _SIGNATURE_CACHE[key] = (signature, now)
        _SIGNATURE_CACHE.move_to_end(key)
        while len(_SIGNATURE_CACHE) > _SIGNATURE_CACHE_MAX:
            _SIGNATURE_CACHE.popitem(last=False)


def _signature_cache_lookup(key: tuple) -> Optional[str]:
    """Return a fresh cached signature for ``key``, or None on a silent miss.

    Increments SIGNATURE_CACHE_HITS / SIGNATURE_CACHE_MISSES. An expired entry
    is dropped and counted as a miss. NEVER logs (AGENTS.md §2).
    """
    global SIGNATURE_CACHE_HITS, SIGNATURE_CACHE_MISSES
    now = time.time()
    with _SIGNATURE_CACHE_LOCK:
        entry = _SIGNATURE_CACHE.get(key)
        if entry is None:
            SIGNATURE_CACHE_MISSES += 1
            return None
        signature, stored_at = entry
        if now - stored_at > _SIGNATURE_CACHE_TTL:
            _SIGNATURE_CACHE.pop(key, None)
            SIGNATURE_CACHE_MISSES += 1
            return None
        _SIGNATURE_CACHE.move_to_end(key)
        SIGNATURE_CACHE_HITS += 1
        return signature


_UNSIGNED_WARNED = False


def _signature_cache_lookup_fallback(model: str, args: Any) -> Optional[str]:
    """Name/id-agnostic last-resort signature lookup.

    The primary key embeds the volatile per-turn ``call_{name}_{counter}`` id and
    the tool ``name`` -- both of which break when the client substitutes a
    placeholder name (e.g. ``default_api:invalid``) or the id churns across turns.
    Matching purely on ``(model, args_digest)`` survives both. Only returns a
    signature when exactly ONE distinct live signature matches the digest for the
    model (unambiguous); an ambiguous or empty match returns None so we never
    attach the wrong signature. Increments SIGNATURE_CACHE_FALLBACK_HITS on use.
    """
    global SIGNATURE_CACHE_FALLBACK_HITS
    digest = _args_digest(args)
    now = time.time()
    with _SIGNATURE_CACHE_LOCK:
        matches = set()
        for (m, _cid, _n, d), (sig, stored_at) in _SIGNATURE_CACHE.items():
            if m != model or d != digest:
                continue
            if now - stored_at > _SIGNATURE_CACHE_TTL:
                continue
            matches.add(sig)
        if len(matches) == 1:
            SIGNATURE_CACHE_FALLBACK_HITS += 1
            return next(iter(matches))
    return None


def _warn_unsigned_once(model: str, name: str) -> None:
    """Emit ONE throttled warning (no PII, no args) when a functionCall would go
    out unsigned -- that part is what Google 400s on. Diagnostic only; never raises."""
    global _UNSIGNED_WARNED
    if _UNSIGNED_WARNED:
        return
    _UNSIGNED_WARNED = True
    try:
        print(
            "[antigravity] thought_signature cache miss: emitting unsigned "
            "functionCall (name=%r, model=%s) -- upstream may 400" % (name, model)
        )
    except Exception:
        pass


# Gemini ``Schema`` proto fields a functionDeclaration ``parameters`` blob may
# carry. Everything else — JSON-Schema 2020-12 markers (``$schema``, ``$defs``,
# ``$ref``, ``$comment``, …), OpenAI-only keywords (``additionalProperties``,
# ``examples``, ``default``, ``title``, ``strict`` …) — is rejected by Gemini's
# proto parser with 400 "Unknown name … : Cannot find field" (antigravity lane,
# 2026-09-05). Conservative by design: dropping a field that turns out to be
# valid only loses minor validation fidelity, while keeping an invalid one
# 400s the entire request. The upstream reports unknowns in random map order,
# so enumeration is impossible — an allowlist is the only deterministic fix.
_GEMINI_SCHEMA_KEEP = frozenset({
    "type", "format", "description", "nullable",
    "items", "properties", "required", "enum",
    "minItems", "maxItems", "minimum", "maximum",
    "anyOf", "propertyOrdering",
})


def _normalize_tool_parameters(params: Any) -> Dict[str, Any]:
    """Rewrite a JSON-Schema ``parameters`` blob for Gemini functionDeclarations.

    Layer 2 of the Gemini 3.1-Pro 400 fix (2026-09-05): the IDE ships tools as
    JSON-Schema 2020-12, whose markers the Gemini ``Schema`` proto cannot
    parse. Rebuilds the dict key-by-key (input is never mutated) keeping only
    ``_GEMINI_SCHEMA_KEEP`` fields; renames ``oneOf``→``anyOf`` (Gemini's only
    union) and flattens draft-07 ``type`` arrays (``["string","null"]``) into a
    scalar type plus ``nullable``. Fail-open: non-dict/empty input yields a
    trivial object schema.
    """
    if not isinstance(params, dict) or not params:
        return {"type": "object", "properties": {}}

    def _walk(value: Any) -> Any:
        if isinstance(value, dict):
            out: Dict[str, Any] = {}
            for key, child in value.items():
                if key == "oneOf" and isinstance(child, list):
                    key = "anyOf"
                if key == "properties" and isinstance(child, dict):
                    # Map of ARBITRARY property names -> subschema: the names
                    # are user-defined, not proto fields — never filtered.
                    out["properties"] = {
                        name: _walk(sub)
                        for name, sub in child.items()
                        if isinstance(sub, (dict, list))
                    }
                    continue
                if key == "type" and isinstance(child, list):
                    # Draft-07 union types ("type": ["string","null"]) have no
                    # proto equivalent: first non-null entry wins, "null"
                    # promotes to nullable.
                    non_null = [
                        t for t in child
                        if isinstance(t, str) and t.lower() != "null"
                    ]
                    if non_null:
                        out["type"] = non_null[0].lower()
                    if any(
                        isinstance(t, str) and t.lower() == "null" for t in child
                    ):
                        out["nullable"] = True
                    continue
                if key not in _GEMINI_SCHEMA_KEEP:
                    continue
                if key == "type" and isinstance(child, str):
                    out[key] = child.lower()
                    continue
                out[key] = _walk(child)
            return out
        if isinstance(value, list):
            return [_walk(item) for item in value]
        return value

    return _walk(params)


def _image_url_to_part(url: str) -> Optional[Dict[str, Any]]:
    """OpenAI image_url → Gemini inlineData (data URI) / fileData (remote URI)."""
    if not isinstance(url, str) or not url:
        return None
    if url.startswith("data:"):
        header, _, b64 = url.partition(",")
        mime = header[5:].split(";", 1)[0] if header.startswith("data:") else ""
        return {"inlineData": {"mimeType": mime or "image/png", "data": b64}}
    if url.startswith(("http://", "https://", "gs://")):
        ext_mime = "image/png"
        lower = url.split("?", 1)[0].lower()
        for ext, mime in (
            (".jpg", "image/jpeg"), (".jpeg", "image/jpeg"), (".png", "image/png"),
            (".gif", "image/gif"), (".webp", "image/webp"), (".pdf", "application/pdf"),
        ):
            if lower.endswith(ext):
                ext_mime = mime
                break
        return {"fileData": {"mimeType": ext_mime, "fileUri": url}}
    return None


def _name_from_call_id(call_id: str) -> str:
    """Recover a tool name from an ingress-minted ``call_{name}_{seq}`` id."""
    if not isinstance(call_id, str) or not call_id.startswith("call_"):
        return ""
    body = call_id[len("call_"):]
    return body.rsplit("_", 1)[0] if "_" in body else body


def _tool_result_payload(content: Any) -> Any:
    """OpenAI tool-message content → Gemini functionResponse.response.result."""
    if isinstance(content, (dict, list)):
        return content
    if isinstance(content, str) and content:
        try:
            parsed = json.loads(content)
            if isinstance(parsed, (dict, list)):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass
        return {"content": content}
    return {"content": ""}


def openai_to_cloudcode_envelope(
    openai_body: Dict[str, Any], model: str, project: Optional[str] = None
) -> Dict[str, Any]:
    """Translate an OpenAI-canonical chat payload into the Cloud Code envelope.

    Inverse of ``gemini_request_to_openai`` (ingress). BSL-internal keys
    (``_bsl_original_model``, ``x_gemini_tool_mode``, ...) are dropped by
    construction: only known OpenAI fields are read.
    """
    body = openai_body if isinstance(openai_body, dict) else {}
    contents: List[Dict[str, Any]] = []
    sys_texts: List[str] = []
    call_names: Dict[str, str] = {}
    seq = 0

    for msg in (body.get("messages") or []):
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")

        if role == "system":
            text = msg.get("content") if isinstance(msg.get("content"), str) else None
            if text:
                sys_texts.append(text)
            continue

        parts: List[Dict[str, Any]] = []
        content = msg.get("content")
        if isinstance(content, str):
            if content:
                parts.append({"text": content})
        elif isinstance(content, list):
            for c in content:
                if not isinstance(c, dict):
                    continue
                ctype = c.get("type")
                if ctype == "text" and c.get("text"):
                    parts.append({"text": c["text"]})
                elif ctype == "image_url":
                    iu = c.get("image_url")
                    url = iu.get("url", "") if isinstance(iu, dict) else str(iu or "")
                    part = _image_url_to_part(url)
                    if part:
                        parts.append(part)

        for tc in (msg.get("tool_calls") or []):
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            name = fn.get("name") if isinstance(fn, dict) else None
            if not isinstance(name, str) or not name.strip():
                continue
            name = name.strip()
            raw_args = fn.get("arguments") if isinstance(fn, dict) else None
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) and raw_args else {}
            except (json.JSONDecodeError, TypeError):
                args = {}
            seq += 1
            call_id = tc.get("id") or f"call_{name}_{seq}"
            call_names[call_id] = name
            _fc_part: Dict[str, Any] = {"functionCall": {"name": name, "args": args}}
            # Re-emit the carried signature as a camelCase SIBLING of
            # functionCall (Gemini 3.1-Pro 400 fix, 2026-09-04): Gemini
            # validates that echoed calls keep their original thoughtSignature —
            # an unsigned part is rejected with INVALID_ARGUMENT.
            _tsig = tc.get("thought_signature")
            if not (isinstance(_tsig, str) and _tsig):
                # Step 3d (defense in depth, 2026-09-21): the client is not
                # contractually obliged to round-trip the unknown
                # thought_signature carrier, so fall back to the router-side
                # cache keyed by (model, call_id, name, args-digest). The id IS
                # a standard OpenAI field the client echoes, so it anchors the
                # lookup. A miss is silent and non-fatal.
                _tsig = _signature_cache_lookup(
                    _signature_cache_key(model, call_id, name, raw_args)
                )
                if not (isinstance(_tsig, str) and _tsig):
                    # Step 4 (2026-09-25): primary key embeds the volatile per-turn
                    # call_id and the tool name (which clients sometimes replace
                    # with a placeholder like ``default_api:invalid``), so a miss
                    # is common on long agent loops and previously emitted an
                    # UNSIGNED part -> Google 400. Fall back to a name/id-agnostic
                    # (model, args_digest) match before giving up.
                    _tsig = _signature_cache_lookup_fallback(model, raw_args)
            if isinstance(_tsig, str) and _tsig:
                _fc_part["thoughtSignature"] = _tsig
            else:
                # Still unsigned after all fallbacks: this is the exact part that
                # triggers Google's 400. Emit one throttled diagnostic (never raise).
                _warn_unsigned_once(model, name)
            parts.append(_fc_part)

        if role == "tool":
            call_id = msg.get("tool_call_id") or ""
            tool_name = (
                call_names.get(call_id)
                or _name_from_call_id(call_id)
                or "tool"
            )
            fr_part: Dict[str, Any] = {
                "functionResponse": {
                    "name": tool_name,
                    "response": {"result": _tool_result_payload(msg.get("content"))},
                }
            }
            # Re-emit the carried signature as a camelCase sibling on the
            # functionResponse part (Gemini 3.1-Pro 400 fix): symmetric with
            # the functionCall sibling above, preserving whatever the IDE echoed.
            _tsig = msg.get("thought_signature")
            if isinstance(_tsig, str) and _tsig:
                fr_part["thoughtSignature"] = _tsig
            # Gemini groups consecutive functionResponses in ONE user content.
            prev = contents[-1] if contents else None
            if isinstance(prev, dict) and prev.get("role") == "user" and prev.get("__fr__"):
                prev["parts"].append(fr_part)
            else:
                contents.append({
                    "role": "user",
                    "parts": [fr_part],
                    "__fr__": True,
                })
            continue

        if not parts:
            parts = [{"text": ""}]
        contents.append({
            "role": "model" if role == "assistant" else "user",
            "parts": parts,
        })

    for c in contents:
        c.pop("__fr__", None)

    request_obj: Dict[str, Any] = {
        "sessionId": f"bsl-{uuid.uuid4().hex[:12]}",
        "contents": contents,
    }
    if sys_texts:
        request_obj["systemInstruction"] = {"parts": [{"text": "\n".join(sys_texts)}]}

    decls: List[Dict[str, Any]] = []
    for tool in (body.get("tools") or []):
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function")
        if isinstance(fn, dict):
            src = [fn]
        elif tool.get("type") == "function" and isinstance(tool.get("function"), dict):
            src = [tool["function"]]
        else:
            src = []
        for f in src:
            name = f.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            decls.append({
                "name": name.strip(),
                "description": f.get("description", "") or "",
                "parameters": _normalize_tool_parameters(f.get("parameters")),
            })
    if decls:
        request_obj["tools"] = [{"functionDeclarations": decls}]
        request_obj["toolConfig"] = {"functionCallingConfig": {"mode": "AUTO"}}

    gc: Dict[str, Any] = {}
    try:
        if body.get("max_tokens"):
            gc["maxOutputTokens"] = int(body["max_tokens"])
    except (TypeError, ValueError):
        pass
    for src_key, dst_key in (("temperature", "temperature"), ("top_p", "topP"), ("top_k", "topK")):
        val = body.get(src_key)
        if val is not None:
            gc[dst_key] = val
    effort = body.get("reasoning_effort")
    if not effort and isinstance(body.get("thinking"), dict):
        effort = body["thinking"].get("effort") or body["thinking"].get("type")
    if isinstance(effort, str) and effort.lower() in _EFFORT_TO_BUDGET:
        gc["thinkingConfig"] = {"thinkingBudget": _EFFORT_TO_BUDGET[effort.lower()]}
    if gc:
        request_obj["generationConfig"] = gc

    return {
        "model": model,
        "project": (project if isinstance(project, str) and project else ANTIGRAVITY_DEFAULT_PROJECT),
        "requestId": f"bsl-{uuid.uuid4().hex[:16]}",
        "userAgent": "antigravity",
        "requestType": "agent",
        "request": request_obj,
    }


def strip_thought_signature_keys(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Remove inline ``thought_signature`` carriers from an OpenAI payload.

    Hygiene for non-antigravity lanes (Gemini 3.1-Pro 400 fix, 2026-09-04):
    the inline keys are BSL-internal carriers between the Gemini ingress/egress
    adapters and this module's envelope builder. Providers that validate
    tool-call shape strictly must never see them. Fail-open by design — an
    unexpected payload shape is returned untouched rather than raising, so
    hygiene can never break routing.
    """
    try:
        messages = payload.get("messages")
        if not isinstance(messages, list):
            return payload
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            msg.pop("thought_signature", None)
            for tc in msg.get("tool_calls") or []:
                if isinstance(tc, dict):
                    tc.pop("thought_signature", None)
            # Anthropic wire shape: after normalize_to_anthropic re-emit, the
            # carrier lives on tool_use content blocks. Strip it there too so
            # anthropic-format non-antigravity lanes get the same hygiene.
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        block.pop("thought_signature", None)
                        block.pop("thoughtSignature", None)
        return payload
    except Exception:
        return payload


def _usage_to_openai(usage: Dict[str, Any]) -> Dict[str, Any]:
    """Gemini usageMetadata → OpenAI usage."""
    usage = usage if isinstance(usage, dict) else {}
    return {
        "prompt_tokens": usage.get("promptTokenCount") or 0,
        "completion_tokens": usage.get("candidatesTokenCount") or 0,
        "total_tokens": usage.get("totalTokenCount") or 0,
        "prompt_tokens_details": {"cached_tokens": usage.get("cachedContentTokenCount") or 0},
        "completion_tokens_details": {"reasoning_tokens": usage.get("thoughtsTokenCount") or 0},
    }


class CloudCodeSSETranslator:
    """Incremental Gemini-SSE → OpenAI-SSE translator with aggregation state.

    Feed raw upstream text chunks via :meth:`feed` (returns converted OpenAI
    SSE byte frames); call :meth:`close` at stream end to emit ``[DONE]``.
    :meth:`final_openai_response` aggregates everything seen into one
    non-streaming OpenAI completion (for buffered/non-stream client paths).
    """

    def __init__(self, model: str):
        self.model = model
        self._buf = ""
        self._done_emitted = False
        self._id = f"chatcmpl-ag-{uuid.uuid4().hex[:16]}"
        self._created = int(time.time())
        self._tc_count = 0
        self.agg: Dict[str, Any] = {
            "content": [],
            "reasoning": [],
            "tool_calls": [],
            "finish": None,
            "usage": None,
        }

    # ── incremental feed ────────────────────────────────────────────────

    def feed(self, text: str) -> List[bytes]:
        outs: List[bytes] = []
        self._buf += text.replace("\r\n", "\n")
        while "\n\n" in self._buf:
            event, self._buf = self._buf.split("\n\n", 1)
            data_payload = "".join(
                line[len("data:"):].strip()
                for line in event.split("\n")
                if line.startswith("data:")
            )
            if not data_payload:
                continue
            outs.extend(self._handle_payload(data_payload))
        return outs

    def close(self) -> List[bytes]:
        if self._done_emitted:
            return []
        self._done_emitted = True
        return [b"data: [DONE]\n\n"]

    def _handle_payload(self, payload: str) -> List[bytes]:
        if payload.strip() == "[DONE]":
            self._done_emitted = True
            return [b"data: [DONE]\n\n"]
        try:
            frame = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            return []
        if not isinstance(frame, dict):
            return []
        if isinstance(frame.get("response"), dict):
            frame = frame["response"]
        chunk = self._frame_to_chunk(frame)
        if chunk is None:
            return []
        return [f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8")]

    def _frame_to_chunk(self, frame: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        cands = frame.get("candidates") or []
        cand = cands[0] if cands and isinstance(cands[0], dict) else {}
        parts = ((cand.get("content") or {}).get("parts")) or []

        delta: Dict[str, Any] = {}
        texts: List[str] = []
        thoughts: List[str] = []
        tool_calls: List[Dict[str, Any]] = []
        for part in parts:
            if not isinstance(part, dict):
                continue
            if part.get("thought") is True and part.get("text"):
                thoughts.append(part["text"])
            elif part.get("text"):
                texts.append(part["text"])
            elif isinstance(part.get("functionCall"), dict):
                fc = part["functionCall"]
                name = fc.get("name") or "tool"
                self._tc_count += 1
                _openai_tc: Dict[str, Any] = {
                    "index": self._tc_count - 1,
                    "id": f"call_{name}_{self._tc_count}",
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(fc.get("args") or {}, ensure_ascii=False),
                    },
                }
                # PRESERVE thoughtSignature (Gemini 3.1-Pro 400 ROOT-CAUSE fix,
                # 2026-09-04): Gemini streams the signature as a sibling of
                # functionCall on the same part. Carrying it inline on the
                # OpenAI tool_call lets the IDE frame converters re-emit it, so
                # the next-turn echo is signed and Gemini 3.1-Pro stops
                # returning INVALID_ARGUMENT on unsigned history parts.
                # Step 2 (2026-09-21): read BOTH nesting levels. Wire telemetry
                # (mitm_egress_frames.jsonl) shows functionCall parts sometimes
                # carry the signature NESTED inside functionCall rather than as
                # a part sibling, so a sibling-only read silently missed those
                # turns. Sibling first, then nested fallback.
                _sig = part.get("thoughtSignature")
                if not (isinstance(_sig, str) and _sig):
                    _sig = fc.get("thoughtSignature")
                if isinstance(_sig, str) and _sig:
                    _openai_tc["thought_signature"] = _sig
                    # Step 3c (2026-09-21): cache the signature so the router can
                    # re-inject it on the next-turn echo even if the client drops
                    # the carrier. Keyed by the minted id + name + args digest.
                    _signature_cache_store(
                        _signature_cache_key(
                            self.model, _openai_tc["id"], name, fc.get("args") or {}
                        ),
                        _sig,
                    )
                tool_calls.append(_openai_tc)

        if thoughts:
            delta["reasoning_content"] = "".join(thoughts)
            self.agg["reasoning"].extend(thoughts)
        if texts:
            delta["content"] = "".join(texts)
            self.agg["content"].extend(texts)
        if tool_calls:
            delta["tool_calls"] = tool_calls
            self.agg["tool_calls"].extend(tool_calls)

        finish_reason = None
        fr = cand.get("finishReason")
        if isinstance(fr, str) and fr:
            if fr == "STOP" and self.agg["tool_calls"]:
                finish_reason = "tool_calls"
            else:
                finish_reason = _FINISH_INVERSE.get(fr, "stop")
            self.agg["finish"] = finish_reason

        usage_oai = None
        if isinstance(frame.get("usageMetadata"), dict):
            usage_oai = _usage_to_openai(frame["usageMetadata"])
            self.agg["usage"] = usage_oai

        if not delta and finish_reason is None and usage_oai is None:
            return None

        choice: Dict[str, Any] = {"index": 0, "delta": delta}
        if finish_reason is not None:
            choice["finish_reason"] = finish_reason
        chunk: Dict[str, Any] = {
            "id": self._id,
            "object": "chat.completion.chunk",
            "created": self._created,
            "model": self.model,
            "choices": [choice],
        }
        if usage_oai is not None:
            chunk["usage"] = usage_oai
        return chunk

    # ── aggregation (non-stream clients) ────────────────────────────────

    def final_openai_response(self) -> Dict[str, Any]:
        message: Dict[str, Any] = {"role": "assistant"}
        if self.agg["reasoning"]:
            message["reasoning_content"] = "".join(self.agg["reasoning"])
        if self.agg["content"]:
            message["content"] = "".join(self.agg["content"])
        elif self.agg["tool_calls"]:
            message["content"] = None
        else:
            message["content"] = ""
        if self.agg["tool_calls"]:
            message["tool_calls"] = self.agg["tool_calls"]
        finish = self.agg["finish"] or ("tool_calls" if self.agg["tool_calls"] else "stop")
        return {
            "id": self._id,
            "object": "chat.completion",
            "created": self._created,
            "model": self.model,
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": self.agg["usage"] or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }


# ── Client-boundary wrapper ─────────────────────────────────────────────────
#
# Wraps the antigravity egress httpx.AsyncClient so every hardened send site
# in main.py (probe, OAuth 401-retry, thinking-degradation retry, combo
# fallback, buffered aggregation, zero-token watchdogs) operates unchanged on
# OpenAI-canonical payloads while the wire speaks Cloud Code.

import codecs

try:  # httpx is a hard dependency of BSL; guard keeps this module importable
    # for unit tests of the pure translation functions alone.
    import httpx as _httpx
except ImportError:  # pragma: no cover
    _httpx = None


class _TranslatedStreamResponse:
    """Stream response whose byte iterators yield translated OpenAI SSE.

    Non-200 responses are never wrapped (callers see the raw Google error).
    All unknown attributes delegate to the inner response.
    """

    def __init__(self, inner: Any, model: str):
        self._inner = inner
        self._tr = CloudCodeSSETranslator(model)
        self._dec = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    @property
    def status_code(self) -> int:
        return self._inner.status_code

    @property
    def headers(self) -> Any:
        return self._inner.headers

    async def aiter_raw(self):
        async for chunk in self._inner.aiter_raw():
            for out in self._tr.feed(self._dec.decode(chunk)):
                yield out
        tail = self._dec.decode(b"", final=True)
        if tail:
            for out in self._tr.feed(tail):
                yield out
        for out in self._tr.close():
            yield out

    async def aiter_bytes(self):
        async for out in self.aiter_raw():
            yield out

    async def aread(self) -> bytes:
        data = b""
        async for out in self.aiter_raw():
            data += out
        return data

    async def aread_translator(self) -> "CloudCodeSSETranslator":
        """Consume the stream, returning the aggregating translator."""
        async for _ in self.aiter_raw():
            pass
        return self._tr

    async def aclose(self) -> None:
        await self._inner.aclose()


class AntigravityUpstreamClient:
    """httpx-client facade: OpenAI in/out, Cloud Code on the wire.

    ``build_request`` rewrites the OpenAI payload to the verified Cloud Code
    envelope and points the request at ``v1internal:streamGenerateContent``.
    ``send`` transparently translates the Gemini SSE response back to OpenAI
    chunks (stream) or one aggregated OpenAI completion (non-stream).
    """

    def __init__(self, inner: Any, model: str):
        self._inner = inner
        self._model = model

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    # ── request direction ───────────────────────────────────────────────

    def build_request(self, method: str, url: Any, headers: Any = None, content: Any = None, **kw: Any):
        payload: Dict[str, Any] = {}
        if isinstance(content, (bytes, bytearray)):
            try:
                payload = json.loads(content.decode("utf-8", errors="replace"))
            except (json.JSONDecodeError, ValueError):
                payload = {}
        elif isinstance(content, str):
            try:
                payload = json.loads(content)
            except (json.JSONDecodeError, ValueError):
                payload = {}
        elif isinstance(content, dict):
            payload = content

        model = None
        if isinstance(payload, dict):
            m = payload.get("model")
            if isinstance(m, str) and m.strip():
                model = m.strip()
        if not model or "/" in model:
            model = self._model

        envelope = openai_to_cloudcode_envelope(payload, model)

        # Rewrite {base}/chat/completions -> {base}/v1internal:streamGenerateContent?alt=sse
        base = _httpx.URL(str(url)) if _httpx else None
        if base is not None:
            # httpx 0.27 URL.netloc is BYTES — decode before interpolation or
            # the literal b'...' poisons the host (getaddrinfo 11001).
            netloc = base.netloc.decode("ascii") if isinstance(base.netloc, bytes) else str(base.netloc)
            rpc = f"{base.scheme}://{netloc}{ANTIGRAVITY_STREAM_URL_PATH}"
        else:  # pragma: no cover — httpx missing, tests only
            rpc = str(url)

        hdrs = {k: v for k, v in (headers or {}).items() if k.lower() != "user-agent"}
        hdrs["User-Agent"] = ANTIGRAVITY_UPSTREAM_UA
        hdrs.setdefault("Content-Type", "application/json")

        return self._inner.build_request(
            method,
            str(rpc),
            headers=hdrs,
            content=json.dumps(envelope, sort_keys=True).encode("utf-8"),
            **kw,
        )

    # ── response direction ──────────────────────────────────────────────

    async def send(self, request: Any, *, stream: bool = False, **kw: Any):
        if stream:
            resp = await self._inner.send(request, stream=True, **kw)
            if resp.status_code != 200:
                return resp
            return _TranslatedStreamResponse(resp, self._model_of(request))

        # Non-stream: still use the SSE verb (the only live-verified one),
        # then aggregate through the translator into one OpenAI completion.
        resp = await self._inner.send(request, stream=True, **kw)
        if resp.status_code != 200:
            try:
                await resp.aread()
            except Exception:
                pass
            return resp
        wrapper = _TranslatedStreamResponse(resp, self._model_of(request))
        tr = await wrapper.aread_translator()
        final = tr.final_openai_response()
        body = json.dumps(final, ensure_ascii=False).encode("utf-8")
        out_headers = {k: v for k, v in resp.headers.items() if k.lower() != "content-length"}
        out_headers["Content-Type"] = "application/json"
        try:
            await resp.aclose()
        except Exception:
            pass
        return _httpx.Response(200, headers=out_headers, content=body, request=request)

    def _model_of(self, request: Any) -> str:
        try:
            env = json.loads(request.content.decode("utf-8", errors="replace"))
            m = env.get("model")
            if isinstance(m, str) and m.strip():
                return m.strip()
        except Exception:
            pass
        return self._model


def wrap_antigravity_upstream_client(inner: Any, model: str) -> AntigravityUpstreamClient:
    """Public entry: wrap the egress client for one antigravity target model."""
    return AntigravityUpstreamClient(inner, model)
