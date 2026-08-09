"""
Middleware.compaction — Context Budget Guard

Context-aware, safe tail compaction for providers that have small context windows
and no meaningful native prompt caching. Designed to preserve thinking budget
on models like DeepSeek, GLM, MiniMax, Mistral, Kimi, Grok, and Qwen.

SKIP LIST (hardcoded) — providers whose native caching > compaction:
  - anthropic / claude-* : explicit cache_control, byte-exact prefix cache
  - openai / gpt-* / codex : org-level auto context cache on 128k+ models
  - gemini-* / vertex : implicit prefix cache, 1M-2M windows
  - claude (OAuth) : same as anthropic

Safety contract:
  1. System prompt is NEVER compacted (pinned forever)
  2. Latest PIN_TURNS user+assistant turns are NEVER compacted
  3. Tool-call integrity: any message containing an unresolved tool_use is pinned
  4. High-water / low-water watermarks: only trigger once per N tokens saved
  5. Summary cache: hashed so compaction model is not re-called for unchanged tail
  6. Fail-open: any error → return original messages unchanged
"""

import re
import json
import hashlib
import time
import httpx
from typing import List, Tuple, Optional
from app.models import ChatCompletionRequest, Message
from app.utils.url_normalization import build_custom_text_upstream_url

# ─── Hardcoded skip: these provider IDs are never compacted ──────────────────
COMPACTION_SKIP_PROVIDERS = frozenset({
    # Anthropic family
    "anthropic", "claude", "kilocode", "claude_code",
    # OpenAI family
    "openai", "codex", "openai_codex", "azure", "opencode-go",
    # Gemini / Vertex family
    "gemini", "vertex", "vertex-partner", "vertex_ai", "vertex_partner",
})

# Regex matches on model ID string for additional safety.
# Covers:
#   - claude-* / anthropic-*  : explicit prefix
#   - opus* / sonnet* / haiku* : naked Claude family names used by proxies/custom providers
#   - gpt-4* / gpt-5* / o1* / o3* / o4* / chatgpt-* : OpenAI family incl. reasoning aliases
#   - gemini* / vertex*        : Gemini family
COMPACTION_SKIP_MODEL_RE = re.compile(
    r"(claude|anthropic"
    r"|opus[-\d]|sonnet[-\d]|haiku[-\d]"
    r"|gpt-4|gpt-5|chatgpt|\bo1[-\s]|\bo1$|\bo3[-\s]|\bo3$|\bo4[-\s]|\bo4$"
    r"|gemini|vertex)",
    re.IGNORECASE
)

# How many recent turns (user+assistant pairs) are always pinned — DEFAULT when
# config["tools"]["compaction_code_strip_turns"] is not set or zero.
_PIN_TURNS_DEFAULT = 3
_PIN_TURNS_MIN = 1
_PIN_TURNS_MAX = 20

# In-memory summary cache: hash -> {"summary": str, "ts": float}
_summary_cache: dict = {}
CACHE_MAX_ENTRIES = 500
CACHE_TTL_SECONDS = 7 * 24 * 3600  # 7 days


def _clamp_pin_turns(tools_cfg: dict) -> int:
    """Read compaction_code_strip_turns from config, clamp to [1, 20], default 3."""
    raw = int(tools_cfg.get("compaction_code_strip_turns", 0) or 0)
    if raw < _PIN_TURNS_MIN:
        return _PIN_TURNS_DEFAULT
    return min(raw, _PIN_TURNS_MAX)


def _approximate_tokens(text: str) -> int:
    """Rough 4-chars-per-token estimate. Fast, good enough for threshold logic."""
    return max(1, len(text) // 4)


def _msg_text(msg: Message) -> str:
    """Extract plain text from a message for token estimation and hashing."""
    if isinstance(msg.content, str):
        return msg.content or ""
    if isinstance(msg.content, list):
        parts = []
        for p in msg.content:
            if isinstance(p, dict):
                parts.append(p.get("text") or p.get("content") or "")
            else:
                parts.append(getattr(p, "text", "") or "")
        return " ".join(filter(None, parts))
    return ""


def _count_tokens(messages: List[Message]) -> int:
    return sum(_approximate_tokens(_msg_text(m)) for m in messages)


def _build_tool_call_graph(messages: List[Message]) -> set:
    """
    Return the set of message indices that must not be compacted
    because they participate in an unresolved tool-call chain.

    Rules:
    - Any message with tool_calls whose IDs appear in a later tool_call_id
      is pinned.
    - Any tool message (role=tool / role=function / content parts with type=tool_result)
      is pinned if its parent tool_use is still in the window.
    """
    # Collect all tool_use IDs emitted
    emitted: dict = {}  # tool_use_id -> message_index
    for i, msg in enumerate(messages):
        if msg.tool_calls:
            for tc in msg.tool_calls:
                emitted[tc.id] = i
        # Anthropic style: content blocks with type=tool_use
        if isinstance(msg.content, list):
            for part in msg.content:
                if isinstance(part, dict) and part.get("type") == "tool_use":
                    emitted[part.get("id", "")] = i

    # Collect all tool_result references
    referenced: set = set()
    for msg in messages:
        if msg.tool_call_id:
            referenced.add(msg.tool_call_id)
        if isinstance(msg.content, list):
            for part in msg.content:
                if isinstance(part, dict) and part.get("type") == "tool_result":
                    referenced.add(part.get("tool_use_id", ""))

    # Pin all message indices involved in matched tool pairs
    pinned_indices: set = set()
    for tid, idx in emitted.items():
        if tid in referenced:
            pinned_indices.add(idx)
    # Also pin all tool result messages
    for i, msg in enumerate(messages):
        if msg.tool_call_id and msg.tool_call_id in emitted:
            pinned_indices.add(i)
        if isinstance(msg.content, list):
            for part in msg.content:
                if isinstance(part, dict) and part.get("type") == "tool_result":
                    pinned_indices.add(i)

    return pinned_indices


def _identify_compactable_tail(
    messages: List[Message],
    pin_turns: int
) -> Tuple[List[Message], List[Message], List[Message]]:
    """
    Split messages into three groups:
      sys_msgs      — system prompt(s), always first, always pinned
      compactable   — older conversational history safe to summarize
      pinned_recent — last `pin_turns` user+assistant pairs + all tool-chain messages

    Returns (sys_msgs, compactable, pinned_recent)
    """
    sys_msgs = [m for m in messages if m.role == "system"]
    non_sys = [m for m in messages if m.role != "system"]

    if not non_sys:
        return sys_msgs, [], []

    # Identify the boundary for pinned recent turns
    # Count user+assistant pairs from the end
    pinned_boundary = len(non_sys)
    pair_count = 0
    for i in range(len(non_sys) - 1, -1, -1):
        if non_sys[i].role in ("user", "assistant"):
            pair_count += 1
        if pair_count >= pin_turns * 2:
            pinned_boundary = i
            break
    else:
        pinned_boundary = 0  # All messages are recent

    # Build tool-call dependency graph on full non_sys list
    pinned_tool_indices = _build_tool_call_graph(non_sys)

    # Identify compactable: messages before boundary AND not in tool graph
    compactable_candidates = non_sys[:pinned_boundary]
    pinned_recent_raw = non_sys[pinned_boundary:]

    # Further remove any tool-chain messages from compactable_candidates
    compactable = []
    extra_pinned = []
    for i, msg in enumerate(compactable_candidates):
        if i in pinned_tool_indices:
            extra_pinned.append(msg)
        else:
            compactable.append(msg)

    pinned_recent = extra_pinned + pinned_recent_raw
    return sys_msgs, compactable, pinned_recent


def _cache_key(messages: List[Message]) -> str:
    raw = json.dumps(
        [{"role": m.role, "content": _msg_text(m)} for m in messages],
        sort_keys=True
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def _evict_cache():
    now = time.time()
    expired = [k for k, v in _summary_cache.items() if now - v["ts"] > CACHE_TTL_SECONDS]
    for k in expired:
        del _summary_cache[k]
    # Hard LRU cap
    if len(_summary_cache) > CACHE_MAX_ENTRIES:
        oldest = sorted(_summary_cache.items(), key=lambda x: x[1]["ts"])
        for k, _ in oldest[:len(_summary_cache) - CACHE_MAX_ENTRIES]:
            del _summary_cache[k]


async def _call_compaction_model(
    messages: List[Message],
    http_client: httpx.AsyncClient,
    conn: dict,
    model: str
) -> str:
    """Call the assigned compaction model to produce a dense state-map summary."""
    history_text = ""
    for msg in messages:
        role = msg.role.upper()
        text = _msg_text(msg)
        if text:
            history_text += f"[{role}]: {text}\n"

    prompt = (
        "You are an expert context compressor for a coding AI assistant session. "
        "Summarize the following conversation history into a dense, factual state map. "
        "You MUST preserve verbatim:\n"
        "  - All file paths and line numbers\n"
        "  - All function names, class names, variable names, error messages\n"
        "  - Any explicit user decisions or constraints\n"
        "  - The current objective and what has been completed\n"
        "Omit: greetings, repetitive explanations, resolved dead-ends, chit-chat.\n\n"
        f"--- CONVERSATION START ---\n{history_text}\n--- CONVERSATION END ---\n\n"
        "Output ONLY the compact state map. No preamble."
    )

    # Wire format now travels with the connection (model_resolver injects the
    # provider-level `format`). This replaces a base_url keyword sniff that
    # could never fire: the conn dict had no `format` key, and no configured
    # base_url contains those keywords — every provider is a reverse-proxy on
    # a neutral domain. The result was that anthropic-format providers were
    # silently dialed with an OpenAI body.
    fmt = str(conn.get("format") or "openai").lower()
    is_anthropic_fmt = fmt == "anthropic"

    # URL construction is delegated to the same builder main.py uses, so the
    # scouts cannot drift from the primary routing path.
    try:
        endpoint = build_custom_text_upstream_url(
            conn.get("base_url", ""),
            "anthropic" if is_anthropic_fmt else "openai",
        )
    except ValueError as exc:
        # Unusable base_url — honour the fail-open contract (safety rule 6).
        raise RuntimeError(f"compaction base_url unusable: {exc}") from exc

    if is_anthropic_fmt:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 2048,
        }
        headers = {
            "x-api-key": conn.get("api_key", ""),
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        resp = await http_client.post(endpoint, json=payload, headers=headers, timeout=300.0)
        resp.raise_for_status()
        data = resp.json()
        content_blocks = data.get("content", [])
        return " ".join(b.get("text", "") for b in content_blocks if b.get("type") == "text")
    else:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 2048,
            "stream": False,
        }
        headers = {
            "Authorization": f"Bearer {conn.get('api_key', '')}",
            "Content-Type": "application/json",
        }
        resp = await http_client.post(endpoint, json=payload, headers=headers, timeout=300.0)
        resp.raise_for_status()
        data = resp.json()
        return data.get("choices", [{}])[0].get("message", {}).get("content", "")


def _resolve_compaction_conn(config: dict, compaction_model: str) -> Tuple[Optional[dict], str]:
    """
    Given a model ID, legacy alias, or Combo alias, find an active connection.
    Delegates to the shared Combo-aware resolve_model_conn.
    Returns (conn_dict_or_None, resolved_model_id).
    """
    from app.utils.model_resolver import resolve_model_conn
    return resolve_model_conn(config, compaction_model)


async def apply_compaction(
    request: ChatCompletionRequest,
    http_client: httpx.AsyncClient,
    config: dict,
    provider_name: str = ""
) -> ChatCompletionRequest:
    """
    Main entry point. Evaluates whether compaction should run for this request.
    Fail-open: any error returns original request unchanged.
    """
    t = config.get("tools", {})

    # Gate 1: feature disabled
    if not t.get("compaction_enabled", False):
        return request

    # Gate 2: skip hardcoded provider families — never compact their contexts
    if provider_name.lower() in COMPACTION_SKIP_PROVIDERS:
        return request
    if COMPACTION_SKIP_MODEL_RE.search(request.model):
        return request

    # Gate 3: read thresholds — high-water triggers compaction, target is low-water
    high_water = int(t.get("compaction_threshold", 48000))
    low_water = max(16000, int(high_water * 0.667))  # ~2/3 of high-water

    # Gate 4: count total input tokens
    total_tokens = _count_tokens(request.messages)
    if total_tokens <= high_water:
        return request  # Under threshold, nothing to do

    # Gate 5: split into sys / compactable tail / pinned recent
    try:
        # Resolve pin turns from config: compaction_code_strip_turns (clamped 1-20)
        pin_turns = _clamp_pin_turns(t)
        sys_msgs, compactable, pinned_recent = _identify_compactable_tail(
            request.messages, pin_turns
        )
    except Exception as e:
        print(f"[Compaction] Safety analysis failed: {e} — skipping")
        return request

    if not compactable:
        # Nothing safe to compact (all messages are pinned tool chains or recent)
        print("[Compaction] No safe compactable messages found — skipping")
        return request

    # Gate 6: count compactable tail tokens.
    tail_tokens = _count_tokens(compactable)

    # Gate 6a: aggressive tail-trim threshold — if total tokens exceed the configured
    # compaction_tail_trim_threshold, drop compactable messages entirely instead of calling
    # the compaction model (saves the model call cost + latency at the expense of older context).
    # IMPORTANT: this must run BEFORE the projected-token skip gate; otherwise the
    # trim path is unreachable when summarization is predicted to be insufficient.
    tail_trim_threshold = int(t.get("compaction_tail_trim_threshold", 0) or 0)
    if tail_trim_threshold > 0 and total_tokens > tail_trim_threshold:
        saved = tail_tokens
        request.messages = sys_msgs + pinned_recent
        print(
            f"[Compaction] TAIL-TRIM — total={total_tokens:,} > threshold={tail_trim_threshold:,}: "
            f"dropped {len(compactable)} old messages (~{saved:,} tokens) without summarization"
        )
        return request

    # Gate 6b: would summarizing the tail actually bring us below low_water?
    projected_tokens = total_tokens - tail_tokens + 200  # 200 = estimated summary size
    if projected_tokens > high_water:
        # Compaction won't help enough — abort
        print(f"[Compaction] Tail too small to reach low-water ({projected_tokens} > {high_water}) — skipping")
        return request

    # Gate 7: cache check — avoid re-calling compaction model for same tail
    cache_k = _cache_key(compactable)
    _evict_cache()
    if cache_k in _summary_cache:
        summary = _summary_cache[cache_k]["summary"]
        print("[Compaction] Cache hit — reusing existing summary")
    else:
        # Resolve compaction model connection
        compaction_model = t.get("compaction_model", "")
        if not compaction_model:
            print("[Compaction] No compaction_model configured — skipping")
            return request

        conn, resolved_model = _resolve_compaction_conn(config, compaction_model)
        if not conn:
            print(f"[Compaction] No active connection found for model '{compaction_model}' — skipping")
            return request

        try:
            summary = await _call_compaction_model(compactable, http_client, conn, resolved_model)
        except Exception as e:
            print(f"[Compaction] Model call failed: {e} — fail-open, using original messages")
            return request

        if not summary.strip():
            print("[Compaction] Empty summary returned — skipping")
            return request

        # Store in cache
        _summary_cache[cache_k] = {"summary": summary, "ts": time.time()}

    # Reconstruct: merge state map INTO the last system message (avoid dual-system rejection)
    saved_tokens = tail_tokens - _approximate_tokens(summary)
    state_block = (
        f"\n\n--- CONTEXT BUDGET GUARD: Compacted {len(compactable)} older turns "
        f"({tail_tokens:,} tokens → ~{_approximate_tokens(summary):,} tokens saved) ---\n"
        f"{summary}\n"
        f"--- END COMPACTED CONTEXT ---"
    )

    if sys_msgs:
        # Append to the last system message in-place — preserves single-system-message contract
        last_sys = sys_msgs[-1]
        if isinstance(last_sys.content, str):
            last_sys.content = (last_sys.content or "") + state_block
        elif isinstance(last_sys.content, list):
            # Anthropic content-block style: append a new text block
            last_sys.content.append({"type": "text", "text": state_block})
        request.messages = sys_msgs + pinned_recent
    else:
        # No system message exists — inject one at position 0
        state_msg = Message(role="system", content=state_block.strip())
        request.messages = [state_msg] + pinned_recent

    print(
        f"[Compaction] SUCCESS — {total_tokens:,} → ~{_count_tokens(request.messages):,} tokens "
        f"(saved ~{saved_tokens:,} tokens)"
    )
    return request
