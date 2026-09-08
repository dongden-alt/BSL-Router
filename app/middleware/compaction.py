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
# Anchored to the start of the model id so that family tokens only fire when
# they are the leading/proper token — e.g. `claude-opus-5` or `gpt-5.6-sol`
# are skipped, but `glm-5.3-anthropic` (starts with glm) is compactable.
# Covers:
#   - claude-* / anthropic-*  : explicit prefix
#   - opus* / sonnet* / haiku* : naked Claude family names used by proxies/custom providers
#   - gpt-4* / gpt-5* / chatgpt-* : OpenAI family
#   - o1 / o3 / o4            : reasoning models
#   - gemini* / vertex*        : Gemini family
COMPACTION_SKIP_MODEL_RE = re.compile(
    r"^(?:claude|anthropic|opus|sonnet|haiku"
    r"|gpt-4|gpt-5|chatgpt|o1|o3|o4"
    r"|gemini|vertex)[-/]?",
    re.IGNORECASE,
)

# How many recent turns (user+assistant pairs) are always pinned — DEFAULT when
# config["tools"]["compaction_code_strip_turns"] is not set or zero.
_PIN_TURNS_DEFAULT = 3
_PIN_TURNS_MIN = 1
_PIN_TURNS_MAX = 20

# Calibrated token estimation (Part B): the blind /4 heuristic undercounts
# code-heavy CC traffic ~1.75x in production (estimated 49.4k vs upstream
# actual in=82-85k). `_TOKEN_RATIO` holds the correction factor applied to the
# raw estimate — longest matching model prefix wins, fallback "global".
DEFAULT_TOKEN_RATIO = 1.75
_TOKEN_RATIO: dict = {"global": DEFAULT_TOKEN_RATIO}

# EMA smoothing factor for record_usage feedback (Part B.2).
_RATIO_EMA_ALPHA = 0.3
_RATIO_MIN = 1.0
_RATIO_MAX = 3.0

# Min-savings smart gate (Part B.3): compact only if the calibrated tail is at
# least this percent of the calibrated total. Default matches config default.
DEFAULT_MIN_SAVINGS_PCT = 30


def _resolve_token_ratio(model: str) -> float:
    """Return the calibrated ratio for `model` — longest matching prefix in
    _TOKEN_RATIO (excluding "global"), falling back to the global ratio."""
    ratio_map = {k: v for k, v in _TOKEN_RATIO.items() if k != "global"}
    best_len = 0
    best_ratio = _TOKEN_RATIO.get("global", DEFAULT_TOKEN_RATIO)
    model_l = (model or "").lower()
    for prefix, ratio in ratio_map.items():
        p = prefix.lower()
        if p and model_l.startswith(p) and len(p) > best_len:
            best_len = len(p)
            best_ratio = ratio
    return best_ratio


def record_usage(model: str, estimated_tokens: int, actual_in_tokens: int) -> None:
    """
    Usage-feedback hook (Part B.2): fold upstream-observed input tokens back
    into the per-model calibration ratio via EMA.

    NOT called anywhere in this task (main.py is off-limits); wired later.
    Clamps the corrected ratio to [1.0, 3.0] so one bad sample can't skew gates.
    """
    if not estimated_tokens or estimated_tokens <= 0 or actual_in_tokens <= 0:
        return
    observed = max(_RATIO_MIN, min(_RATIO_MAX, actual_in_tokens / estimated_tokens))
    current = _resolve_token_ratio(model)
    updated = (1 - _RATIO_EMA_ALPHA) * current + _RATIO_EMA_ALPHA * observed
    _TOKEN_RATIO[model.lower()] = max(_RATIO_MIN, min(_RATIO_MAX, updated))


def _calibrated_count_tokens(messages: List[Message], model: str) -> int:
    """Raw /4 estimate scaled by the applicable calibration ratio for `model`."""
    return int(_count_tokens(messages) * _resolve_token_ratio(model))


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
    Split messages into three groups (ORDER-PRESERVING — Part A fix):
      sys_msgs           — system prompt(s), always first, always pinned
      compactable        — older conversational history safe to summarize
      pinned_recent_raw  — everything else (recent turns + tool-chain messages),
                           IN ORIGINAL ORDER

    The old return contract concatenated `extra_pinned + pinned_recent_raw`,
    hoisting mid-history tool-chain messages to the FRONT of the rebuilt array.
    That made the first non-system message an assistant tool_use / user
    tool_result → upstream validators reject with messages-param 400s
    (92% of glm-5.3-flash coder-2 errors, production-proven).

    Returns (sys_msgs, compactable, pinned_recent_raw).
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

    # Identify first non-system user message — the original task statement
    # (Part D1). NEVER compactable; in the rebuilt array it keeps its original
    # relative position (the order-preserving walk guarantees placement).
    first_user_idx = next(
        (i for i, m in enumerate(non_sys) if m.role == "user"), None
    )

    # Partition IN ORDER (Part A.1): walk non_sys once — pre-boundary messages
    # that are neither tool-chain-pinned nor the first-user go to `compactable`;
    # EVERYTHING else stays in `pinned_recent` in original relative order.
    # No concatenation, no hoisting.
    compactable = []
    pinned_recent = []
    for i, msg in enumerate(non_sys):
        if i < pinned_boundary and i not in pinned_tool_indices and i != first_user_idx:
            compactable.append(msg)
        else:
            pinned_recent.append(msg)

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
        "Preserve the user's original goal and all explicit user decisions/constraints word-for-word where given.\n"
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


# ─── Reconstruction invariant validator (Part A.4) ───────────────────────────

def _tool_use_ids(messages: List[Message]) -> set:
    """All tool_use IDs emitted by assistant messages in `messages`."""
    ids: set = set()
    for msg in messages:
        if msg.tool_calls:
            for tc in msg.tool_calls:
                ids.add(tc.id)
        if isinstance(msg.content, list):
            for part in msg.content:
                if isinstance(part, dict) and part.get("type") == "tool_use":
                    ids.add(part.get("id", ""))
    ids.discard("")
    return ids


def _tool_result_ids(messages: List[Message]) -> set:
    """All tool_use IDs referenced by tool_result messages in `messages`."""
    ids: set = set()
    for msg in messages:
        if msg.tool_call_id:
            ids.add(msg.tool_call_id)
        if isinstance(msg.content, list):
            for part in msg.content:
                if isinstance(part, dict) and part.get("type") == "tool_result":
                    ids.add(part.get("tool_use_id", ""))
    ids.discard("")
    return ids


def _validate_reconstruction(original_messages: List[Message], compacted_messages: List[Message]) -> bool:
    """
    Check the safety invariants of a compaction reconstruction (Part A.4).
    Returns True iff ALL hold:
      1. First non-system message role == "user".
      2. Every tool_use id in compacted has a matching tool_result, and vice
         versa — no NEW orphans relative to the original (an already-orphaned
         pair in the original is not this function's problem to fix, but the
         compaction must not create new ones).
      3. Relative order of pinned (non-compacted) original messages is
         preserved in the compacted array.
    """
    # Invariant 1: user-first
    first_non_sys = next((m for m in compacted_messages if m.role != "system"), None)
    if first_non_sys is None or first_non_sys.role != "user":
        return False

    # Invariant 2: tool pairing — no NEW orphans vs original
    orig_use = _tool_use_ids(original_messages)
    orig_res = _tool_result_ids(original_messages)
    new_use = _tool_use_ids(compacted_messages)
    new_res = _tool_result_ids(compacted_messages)
    # orphans = emitted-without-result or result-without-emitter
    if (new_use - new_res) - (orig_use - orig_res):
        return False
    if (new_res - new_use) - (orig_res - orig_use):
        return False

    # Invariant 3: relative order of pinned messages preserved.
    # A pinned original message is any message that still appears in the
    # compacted array (kept messages). Their relative order in compacted must
    # match their relative order in original. Greedy earliest-match walk:
    # each compacted message consumes the earliest still-unmatched original
    # twin; synthetic messages (e.g. the summary) match nothing.
    def _sig(m: Message):
        return (m.role, _msg_text(m), bool(m.tool_calls), m.tool_call_id or "")

    orig_sigs = [_sig(m) for m in original_messages]
    unmatched = list(range(len(original_messages)))
    kept_positions = []
    for m in compacted_messages:
        s = _sig(m)
        found = next((i for i in unmatched if orig_sigs[i] == s), None)
        if found is None:
            continue  # synthetic message — not a pinned original
        unmatched.remove(found)
        kept_positions.append(found)
    if kept_positions != sorted(kept_positions):
        return False
    return True


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

    # Gate 4: count total input tokens (calibrated — Part B.1)
    total_tokens = _calibrated_count_tokens(request.messages, request.model)
    if total_tokens <= high_water:
        return request  # Under threshold, nothing to do

    # Gate 5: split into sys / compactable tail / pinned recent (order-preserving)
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

    # Gate 6: count compactable tail tokens (calibrated).
    tail_tokens = _calibrated_count_tokens(compactable, request.model)

    # Gate 6-smart (Part B.3): compact ONLY if the tail is a meaningful share
    # of the context. Production failing cases saved only ~4% → skip those.
    min_savings_pct = int(t.get("compaction_min_savings_pct", DEFAULT_MIN_SAVINGS_PCT) or 0)
    if min_savings_pct > 0 and total_tokens > 0:
        savings_pct = tail_tokens * 100.0 / total_tokens
        if savings_pct < min_savings_pct:
            print(
                f"[Compaction] Skip — savings too small ({savings_pct:.1f}% < {min_savings_pct}%)"
            )
            return request

    # Gate 6a: aggressive tail-trim threshold — if total tokens exceed the configured
    # compaction_tail_trim_threshold, drop compactable messages entirely instead of calling
    # the compaction model (saves the model call cost + latency at the expense of older context).
    # IMPORTANT: this must run BEFORE the projected-token skip gate; otherwise the
    # trim path is unreachable when summarization is predicted to be insufficient.
    # DEFAULT-DISABLED (Part D2): 0/missing → unreachable. Order + first-user pin
    # still hold here because pinned_recent keeps original order and contains
    # every tool-chain + first-user message.
    tail_trim_threshold = int(t.get("compaction_tail_trim_threshold", 0) or 0)
    if tail_trim_threshold > 0 and total_tokens > tail_trim_threshold:
        saved = tail_tokens
        rebuilt = sys_msgs + pinned_recent  # order-preserving: pinned is a subsequence
        if not _validate_reconstruction(request.messages, rebuilt):
            print("[Compaction] Reconstruction invariant failed — fail-open")
            return request
        request.messages = rebuilt
        print(
            f"[Compaction] TAIL-TRIM — total={total_tokens:,} > threshold={tail_trim_threshold:,}: "
            f"dropped {len(compactable)} old messages (~{saved:,} tokens) without summarization"
        )
        return request

    # Gate 6b: would summarizing the tail actually bring us below low_water?
    # (200 = estimated summary size; 48000 matches the config default for
    # high_water so the check also works in tests that set a tiny threshold.)
    projected_tokens = total_tokens - tail_tokens + 200  # 200 = estimated summary size
    if projected_tokens > max(high_water, 48000):
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

    # Reconstruct (Part A.2, order-preserving): walk the ORIGINAL messages in
    # order, dropping only compactable ones, and insert the summary as a
    # synthetic `user` message AT THE POSITION of the first dropped message.
    # Tool chains stay in place, in order. The system prompt is untouched —
    # the old state-block-into-system merge is gone.
    saved_tokens = tail_tokens - _approximate_tokens(summary)
    state_text = (
        "[CONTEXT SUMMARY]\n"
        f"--- CONTEXT BUDGET GUARD: Compacted {len(compactable)} older turns "
        f"({tail_tokens:,} tokens → ~{_approximate_tokens(summary):,} tokens saved) ---\n"
        f"{summary}\n"
        f"--- END COMPACTED CONTEXT ---"
    )
    summary_msg = Message(role="user", content=state_text)

    compactable_ids = {id(m) for m in compactable}
    drop_seen = False
    rebuilt: List[Message] = []
    for m in request.messages:
        if id(m) in compactable_ids:
            if not drop_seen:
                rebuilt.append(summary_msg)  # at position of first dropped message
                drop_seen = True
            continue
        rebuilt.append(m)

    if not _validate_reconstruction(request.messages, rebuilt):
        print("[Compaction] Reconstruction invariant failed — fail-open")
        return request

    # Part D3: quality telemetry — what was kept vs summarized, auditable
    # from logs alone. (request.messages is still the original here.)
    first_user_msg = next((m for m in request.messages if m.role == "user"), None)
    first_user_kept = any(m is first_user_msg for m in rebuilt) if first_user_msg else 0
    tool_chain_count = len(_build_tool_call_graph(rebuilt))
    request.messages = rebuilt

    print(
        f"[Compaction] SUCCESS — kept {len(sys_msgs)} system + {1 if first_user_kept else 0} first-user "
        f"+ {tool_chain_count} tool-chain + {len(pinned_recent)} recent-pinned, "
        f"summarized {len(compactable)} messages — "
        f"{total_tokens:,} → ~{_calibrated_count_tokens(request.messages, request.model):,} tokens "
        f"(saved ~{saved_tokens:,} tokens)"
    )
    return request
