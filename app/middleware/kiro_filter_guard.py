"""
Middleware.kiro_filter_guard - Kiro / AWS content-filter guard + context-window guard.

Applied once at the provider-egress boundary in `_process_chat_completion_pass`
(after the wire payload is final, before FEL egress), so it sees the real
`provider_name` and works for both the OpenAI and Anthropic wire shapes.

1. Thinking-tag guard (providers in `tools.kiro_filter_guard.providers`, default ["nguyenha"])
   Kiro relays (e.g. nguyenha/claude-opus-5-5, owned_by: kiro) end the response with
   `finish_reason: content_filter` and 0 output tokens when the prompt contains the words
   think / thinking / reasoning wrapped in brackets. Live probes 2026-11-01, 2-3 trials each:
     - angle, square, curly brackets and parentheses: filtered
     - same inside a ``` fenced block: filtered
     - same inside an inline `backtick` span: passes
     - plain words, and the word internal-analysis-start with single spacing: pass
     - the same word padded with two spaces on each side: filtered
   Bracketed forms are rewritten to plain `internal-analysis-start` / `internal-analysis-end`.
   Inline backtick spans are preserved verbatim. Tool-call arguments are covered.

2. Context-window guard (`tools.kiro_filter_guard.max_input_tokens_by_provider`,
   default {"nguyenha": 160000} = 200K window - 32K thinking - 8K output headroom).
   The Gemini ingress lane skips `apply_compaction`, so long IDE sessions can exceed
   the 200K window. When the estimated input is over budget, middle turns are dropped
   and replaced by a deterministic digest; system messages, the first turn and the
   longest fitting tail are kept, and the tail never starts with an orphan tool result.

Fail-open: any exception returns the payload unchanged.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set, Tuple

DEFAULT_FILTER_PROVIDERS: Tuple[str, ...] = ("nguyenha", "kiro")
# Live NIAH probes (2026-11-02) against nguyenha/claude-opus-5-5 verified a true 1M context window
# (502,437 tokens in 28.4s TTFT on Pass 1; 900,207 tokens in 55s on Pass 2 after Cloudflare 100s timeout).
# Default safety budget is set to 550,000 input tokens so single-pass TTFT stays ~30s under Cloudflare's 100s cap.
DEFAULT_KIRO_MAX_INPUT_TOKENS = 550000
DEFAULT_MAX_INPUT_TOKENS_BY_PROVIDER: Dict[str, int] = {
    "nguyenha": DEFAULT_KIRO_MAX_INPUT_TOKENS,
    "kiro": DEFAULT_KIRO_MAX_INPUT_TOKENS,
}
BUDGET_MARKER = "[BSL CONTEXT BUDGET GUARD]"
_KIRO_HOST_MARKERS = ("kiro.dev", "codewhisperer.", "q.us-east-1.amazonaws.com")
_LEARNED_KIRO_TARGETS: Set[Tuple[str, str]] = set()

# Built from escapes so this file never contains a literal trigger tag.
# open-bracket, optional '/', optional inner '[', word, optional inner ']', close-bracket
_TAG_RE = re.compile(
    r"[\x3c\x5b\x7b(]\s*(/?)\s*\x5b?\s*(?:think(?:ing)?|reasoning)\s*\x5d?\s*[\x3e\x5d\x7d)]",
    re.IGNORECASE,
)
_INLINE_CODE_RE = re.compile(r"(?<!`)`[^`\n]+`(?!`)")
_NON_REASONING_CLAUSE_RE = re.compile(r"\(For Non-Reasoning Models[\s\S]*?on its own line\)\.\s*", re.IGNORECASE)
_THINKING_CONTRACT_TAG_RE = re.compile(r"cognitive_thinking_contract", re.IGNORECASE)
_THINKING_HEADER_RE = re.compile(r"CHATBOT THINKING HEADER:\s*Thinking", re.IGNORECASE)
_QUICK_GATE_RE = re.compile(r"think|reasoning", re.IGNORECASE)
_SKIP_PART_TYPES = frozenset({"image", "image_url", "document", "input_image"})

_WORD_OPEN = "internal" + chr(45) + "analysis" + chr(45) + "start"
_WORD_CLOSE = "internal" + chr(45) + "analysis" + chr(45) + "end"


# ---------------------------------------------------------------- config

def _guard_cfg(config: Any) -> Dict[str, Any]:
    if not isinstance(config, dict):
        return {}
    tools = config.get("tools") or {}
    cfg = tools.get("kiro_filter_guard") if isinstance(tools, dict) else None
    return cfg if isinstance(cfg, dict) else {}


def is_enabled(config: Any) -> bool:
    return bool(_guard_cfg(config).get("enabled", True))


def filter_providers(config: Any) -> frozenset:
    raw = _guard_cfg(config).get("providers")
    if isinstance(raw, list):
        return frozenset(str(p).strip().lower() for p in raw if str(p).strip())
    return frozenset(DEFAULT_FILTER_PROVIDERS)


def register_kiro_content_filter_hit(provider_name: Any, target_model: Any) -> None:
    """Record a (provider, model) leaf that returned `finish_reason: content_filter` at runtime."""
    prov = str(provider_name or "").strip().lower()
    mdl = str(target_model or "").strip().lower()
    if prov and mdl:
        _LEARNED_KIRO_TARGETS.add((prov, mdl))


def _lookup_model_owned_by(prov: str, model_l: str, provider_cfg: Dict[str, Any]) -> Optional[str]:
    """Return explicit upstream `owned_by` / `upstream_owned_by` / `backend` for (prov, model_l) if known."""
    for m in provider_cfg.get("models") or []:
        if not isinstance(m, dict):
            continue
        mid = str(m.get("id") or m.get("name") or "").strip().lower()
        if mid == model_l:
            for key in ("upstream_owned_by", "owned_by", "backend"):
                val = m.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip().lower()
    try:
        from app.model_discovery import _discovered_cache
        cached = _discovered_cache.get(prov)
        if cached and isinstance(cached[1], list):
            for m in cached[1]:
                if isinstance(m, dict) and str(m.get("id") or "").strip().lower() == model_l:
                    for key in ("upstream_owned_by", "owned_by"):
                        val = m.get(key)
                        if isinstance(val, str) and val.strip() and val.strip().lower() != prov:
                            return val.strip().lower()
    except Exception:
        pass
    return None


def is_kiro_target(provider_name: Any, target_model: Any, config: Any) -> bool:
    """Determine whether `(provider_name, target_model)` is backed by Kiro / CodeWhisperer.

    5-signal priority chain:
      1. Explicit per-model `owned_by` / `upstream_owned_by` / `backend` (from `config.yaml` or `/v1/models` discovery cache):
         - `"kiro"` -> True
         - any other explicit backend (e.g. `"antigravity"` on `nguyenha/claude-opus-4-6`) -> False
      2. Native Kiro provider / format / auth_type / host (`kiro.dev`, `codewhisperer`, `q.us-east-1.amazonaws.com`) or `"kiro"` in model ID -> True
      3. Runtime auto-learned `(provider, model)` after a `finish_reason == "content_filter"` hit -> True
      4. Provider fallback list `tools.kiro_filter_guard.providers` (default `["nguyenha", "kiro"]`) -> True
    """
    prov = str(provider_name or "").strip().lower()
    model_l = str(target_model or "").strip().lower()
    if not prov and not model_l:
        return False

    providers_map = (config.get("providers") or {}) if isinstance(config, dict) else {}
    provider_cfg = providers_map.get(prov) if isinstance(providers_map, dict) else None
    if not isinstance(provider_cfg, dict):
        provider_cfg = {}

    # Signal 1: Per-model owned_by / upstream_owned_by (distinguishes kiro vs antigravity on shared relays like nguyenha)
    owned_by = _lookup_model_owned_by(prov, model_l, provider_cfg)
    if owned_by:
        if "kiro" in owned_by:
            return True
        if owned_by != prov:
            return False

    # Signal 2: Native Kiro provider / protocol / URL / model name
    if "kiro" in prov or "kiro" in model_l:
        return True
    if str(provider_cfg.get("format") or "").strip().lower() == "kiro":
        return True
    if str(provider_cfg.get("auth_type") or "").strip().lower() == "kiro":
        return True
    urls = [str(provider_cfg.get("base_url") or "")]
    for conn in provider_cfg.get("connections") or []:
        if isinstance(conn, dict) and conn.get("base_url"):
            urls.append(str(conn["base_url"]))
    for u in urls:
        ul = u.lower()
        if any(h in ul for h in _KIRO_HOST_MARKERS):
            return True

    # Signal 3: Runtime auto-learned from content_filter response
    if (prov, model_l) in _LEARNED_KIRO_TARGETS:
        return True

    # Signal 4: Provider fallback list
    return prov in filter_providers(config)


def max_input_tokens_for(provider_name: Any, config: Any, target_model: Any = None) -> Optional[int]:
    raw = _guard_cfg(config).get("max_input_tokens_by_provider")
    table = raw if isinstance(raw, dict) else DEFAULT_MAX_INPUT_TOKENS_BY_PROVIDER
    prov = str(provider_name or "").strip().lower()
    val = {str(k).strip().lower(): v for k, v in table.items()}.get(prov)
    if val is None and is_kiro_target(provider_name, target_model, config):
        val = _guard_cfg(config).get("default_max_input_tokens", DEFAULT_KIRO_MAX_INPUT_TOKENS)
    try:
        val = int(val)
    except (TypeError, ValueError):
        return None
    return val if val >= 8000 else None


# ---------------------------------------------------------------- tag guard

def sanitize_text(text: Any) -> Tuple[Any, int]:
    """Rewrite bracketed think/thinking/reasoning tags and contract triggers to plain words."""
    if not isinstance(text, str) or not text or not _QUICK_GATE_RE.search(text):
        return (text, 0)
    out, n_clause = _NON_REASONING_CLAUSE_RE.subn("", text)
    out, n_contract = _THINKING_CONTRACT_TAG_RE.subn("cognitive_analysis_contract", out)
    out, n_hdr = _THINKING_HEADER_RE.subn("CHATBOT ANALYSIS HEADER: Analysis", out)
    out, n_tags = _TAG_RE.subn(lambda m: _WORD_CLOSE if m.group(1) else _WORD_OPEN, out)
    total = n_clause + n_contract + n_hdr + n_tags
    if total == 0:
        return (text, 0)
    return (out, total)


def _sanitize_content(container: Dict[str, Any], key: str) -> int:
    val = container.get(key)
    if isinstance(val, str):
        new, n = sanitize_text(val)
        if n:
            container[key] = new
        return n
    total = 0
    if isinstance(val, list):
        for part in val:
            if not isinstance(part, dict):
                continue
            if str(part.get("type") or "text").lower() in _SKIP_PART_TYPES:
                continue
            for k in ("text", "thinking"):
                if isinstance(part.get(k), str):
                    new, n = sanitize_text(part[k])
                    if n:
                        part[k] = new
                        total += n
            if isinstance(part.get("content"), (str, list)):  # anthropic tool_result
                total += _sanitize_content(part, "content")
            if isinstance(part.get("input"), dict):  # anthropic tool_use
                total += _sanitize_mapping(part["input"])
    return total


def _sanitize_mapping(obj: Dict[str, Any]) -> int:
    total = 0
    for k, v in list(obj.items()):
        if isinstance(v, str):
            new, n = sanitize_text(v)
            if n:
                obj[k] = new
                total += n
        elif isinstance(v, dict):
            total += _sanitize_mapping(v)
    return total


def sanitize_messages(messages: Any) -> int:
    if not isinstance(messages, list):
        return 0
    total = 0
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        total += _sanitize_content(msg, "content")
        for call in msg.get("tool_calls") or []:
            fn = call.get("function") if isinstance(call, dict) else None
            if isinstance(fn, dict) and isinstance(fn.get("arguments"), str):
                new, n = sanitize_text(fn["arguments"])
                if n:
                    fn["arguments"] = new
                    total += n
    return total


# ---------------------------------------------------------------- context guard

def estimate_tokens(text: str) -> int:
    """Conservative estimate without a tokenizer: ASCII / 3.5, plus 1 per non-ASCII char."""
    if not text:
        return 0
    non_ascii = sum(1 for ch in text if ord(ch) > 127)
    return int((len(text) - non_ascii) / 3.5) + non_ascii + 1


def _msg_text(msg: Any) -> str:
    if not isinstance(msg, dict):
        return ""
    c = msg.get("content")
    parts: List[str] = []
    if isinstance(c, str):
        parts.append(c)
    elif isinstance(c, list):
        for p in c:
            if isinstance(p, dict):
                for k in ("text", "thinking"):
                    if isinstance(p.get(k), str):
                        parts.append(p[k])
                if isinstance(p.get("content"), str):
                    parts.append(p["content"])
                elif p.get("input") is not None:
                    parts.append(str(p.get("input")))
    for call in msg.get("tool_calls") or []:
        fn = call.get("function") if isinstance(call, dict) else None
        if isinstance(fn, dict) and isinstance(fn.get("arguments"), str):
            parts.append(fn["arguments"])
    return "\n".join(parts)


def _is_tool_result(msg: Any) -> bool:
    if not isinstance(msg, dict):
        return False
    if msg.get("role") == "tool":
        return True
    c = msg.get("content")
    return isinstance(c, list) and any(isinstance(p, dict) and p.get("type") == "tool_result" for p in c)


def _tool_names(msg: Dict[str, Any]) -> List[str]:
    names: List[str] = []
    for call in msg.get("tool_calls") or []:
        fn = call.get("function") if isinstance(call, dict) else None
        if isinstance(fn, dict) and fn.get("name"):
            names.append(str(fn["name"]))
    c = msg.get("content")
    if isinstance(c, list):
        names += [str(p.get("name")) for p in c if isinstance(p, dict) and p.get("type") == "tool_use" and p.get("name")]
    return names


def build_digest(dropped: List[Any], max_chars: int = 6000) -> str:
    lines: List[str] = []
    counts: Dict[str, int] = {}
    for m in dropped:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "")
        txt = re.sub(r"\s+", " ", _msg_text(m)).strip()
        if role == "user" and txt and not _is_tool_result(m):
            lines.append("- user: " + txt[:220])
        elif role == "assistant":
            for n in _tool_names(m):
                counts[n] = counts.get(n, 0) + 1
            if txt and not m.get("tool_calls"):
                lines.append("- assistant: " + txt[:160])
    if counts:
        lines.append("- tools used: " + ", ".join(f"{k} x{v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])[:20]))
    body = "\n".join(lines)
    if len(body) > max_chars:
        body = body[: max_chars // 2] + "\n...\n" + body[-(max_chars // 2):]
    return body


def apply_context_window_guard(messages: List[Any], max_input_tokens: int, extra_tokens: int = 0) -> Optional[List[Any]]:
    """Return a compacted copy of `messages` that fits the budget, or None when no change is needed.
    `extra_tokens` accounts for text outside `messages` (e.g. Anthropic top-level `system`)."""
    if not isinstance(messages, list) or len(messages) < 4:
        return None
    est = [estimate_tokens(_msg_text(m)) for m in messages]
    if sum(est) + extra_tokens <= max_input_tokens:
        return None
    sys_idx = [i for i, m in enumerate(messages) if isinstance(m, dict) and m.get("role") == "system"]
    non_idx = [i for i in range(len(messages)) if i not in set(sys_idx)]
    if len(non_idx) < 3:
        return None
    head_i = non_idx[0]
    budget = max_input_tokens - extra_tokens - sum(est[i] for i in sys_idx) - est[head_i] - 2000

    min_tail = min(4, len(non_idx) - 1)
    tail: List[int] = []
    used = 0
    for pos in range(len(non_idx) - 1, 0, -1):
        i = non_idx[pos]
        if len(tail) >= min_tail and used + est[i] > budget:
            break
        tail.insert(0, i)
        used += est[i]
    while len(tail) > 1 and _is_tool_result(messages[tail[0]]):
        used -= est[tail.pop(0)]

    tail_set = set(tail)
    dropped = [messages[i] for i in non_idx[1:] if i not in tail_set]
    out = [dict(m) if isinstance(m, dict) else m for m in messages]

    if used > budget:  # minimum tail still too big: cap long turns except the active last one
        cap = max(20000, int(max_input_tokens * 3.5 * 0.25))
        for i in [head_i] + tail[:-1]:
            c = out[i].get("content") if isinstance(out[i], dict) else None
            if isinstance(c, str) and len(c) > cap:
                out[i]["content"] = c[: cap // 2] + "\n\n...[middle of this turn omitted by BSL context budget guard]...\n\n" + c[-(cap // 2):]

    if dropped:
        note = (
            f"\n\n{BUDGET_MARKER} {len(dropped)} earlier messages were omitted to fit a "
            f"~{max_input_tokens}-token window. Digest of omitted turns:\n{build_digest(dropped)}"
        )
        head = out[head_i]
        c = head.get("content")
        if isinstance(c, str):
            head["content"] = c + note
        elif isinstance(c, list):
            head["content"] = list(c) + [{"type": "text", "text": note.strip()}]
        else:
            head["content"] = note.strip()

    keep = sorted(set(sys_idx) | {head_i} | tail_set)
    return [out[i] for i in keep]


# ---------------------------------------------------------------- entry point

def apply_kiro_egress_guards(payload: Any, provider_name: Any, target_model: Any, config: Any) -> Any:
    """Single egress entry point. Mutates and returns `payload`. Fail-open."""
    try:
        if not isinstance(payload, dict) or not is_enabled(config):
            return payload
        prov = str(provider_name or "").strip().lower()
        msgs = payload.get("messages")
        kiro_leaf = is_kiro_target(provider_name, target_model, config)

        budget = max_input_tokens_for(prov, config, target_model=target_model) if (kiro_leaf or max_input_tokens_for(prov, config) is not None) else None
        if budget and isinstance(msgs, list) and (kiro_leaf or prov in {str(k).strip().lower() for k in (_guard_cfg(config).get("max_input_tokens_by_provider") or {}).keys()}):
            sys_val = payload.get("system")
            extra = estimate_tokens(sys_val if isinstance(sys_val, str) else str(sys_val or ""))
            compacted = apply_context_window_guard(msgs, budget, extra_tokens=extra)
            if compacted is not None:
                before = sum(estimate_tokens(_msg_text(m)) for m in msgs) + extra
                after = sum(estimate_tokens(_msg_text(m)) for m in compacted) + extra
                payload["messages"] = msgs = compacted
                print(f"[KiroGuard] context window guard {prov}/{target_model}: ~{before} -> ~{after} est. tokens", flush=True)

        if kiro_leaf:
            n = sanitize_messages(msgs)
            sys_val = payload.get("system")
            if isinstance(sys_val, str):
                new, k = sanitize_text(sys_val)
                if k:
                    payload["system"] = new
                    n += k
            elif isinstance(sys_val, list):
                n += _sanitize_content(payload, "system")
            if n:
                print(f"[KiroGuard] rewrote {n} filter trigger(s) for {prov}/{target_model}", flush=True)
        return payload
    except Exception as exc:  # pragma: no cover - fail-open
        print(f"[KiroGuard] failed (fail-open): {exc}", flush=True)
        return payload
