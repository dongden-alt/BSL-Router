from typing import Dict, Any, Optional
from app.models import ChatCompletionRequest
from datetime import datetime
import hashlib
import json
from collections import OrderedDict


# OpenAI has no verified explicit-breakpoint contract available in this project.
# Key-bound routing is limited to large static system/developer prefixes instead.
_OPENAI_GPT56_CACHE_MIN_PREFIX_CHARS = 1024
_OPENAI_GPT56_ROUTING_SUFFIXES = (
    "-antigravity-ultra",
    "-antigravity",
    "-pro20x-openai-compact",
    "-pro20x",
    "-openai-compact",
    "-xhigh",
    "-high",
    "-thinking",
    "-anthropic",
)


def _emit_tracker(obs, provider: str, model: str, strategy: str, hint: str):
    """Emit a caching tracker diagnostic entry to console_logs. Fail-open."""
    try:
        entry = {
            "timestamp": datetime.now().isoformat(),
            "event": "cache_tracker",
            "provider": provider,
            "model": model,
            "strategy": strategy,
            "cache_hint": hint,
        }
        obs.console_logs.append(entry)
        # Respect the same 10000-entry cap as observability.log_request
        if len(obs.console_logs) > 10000:
            obs.console_logs.pop(0)
        # Also persist to disk via observability's _persist_entry
        obs._persist_entry(obs._CONSOLE_LOG_PATH, entry)
    except Exception:
        pass  # Tracker must never break the proxy pipeline


def _canonical_gpt56_family(model_id: Any) -> Optional[str]:
    """Return a distinct GPT-5.6 family after removing routing-only suffixes."""
    if not isinstance(model_id, str):
        return None
    model = model_id.rsplit("/", 1)[-1].lower().strip()
    changed = True
    while changed:
        changed = False
        for suffix in _OPENAI_GPT56_ROUTING_SUFFIXES:
            if model.endswith(suffix):
                model = model[:-len(suffix)]
                changed = True
                break
    if model in {"gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"}:
        return model
    return None


def _stable_prefix_content(payload: Dict[str, Any]) -> Optional[str]:
    """Serialize only static system/developer content; never include user turns."""
    if not isinstance(payload, dict):
        return None

    parts = []
    if "system" in payload:
        system = payload.get("system")
        if system is not None:
            try:
                parts.append(json.dumps(system, sort_keys=True, ensure_ascii=False, separators=(",", ":")))
            except (TypeError, ValueError):
                return None

    messages = payload.get("messages", [])
    if not isinstance(messages, list):
        return None
    for message in messages:
        if not isinstance(message, dict):
            return None
        if message.get("role") not in {"system", "developer"}:
            break
        if "content" not in message:
            return None
        try:
            parts.append(json.dumps(
                {"role": message["role"], "content": message["content"]},
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            ))
        except (TypeError, ValueError):
            return None
    return "\n".join(parts)


def _apply_openai_gpt56_cache_key(
    payload: Dict[str, Any],
    target_model: str,
    tools_config: Dict[str, Any],
) -> str:
    """Generate a deterministic, privacy-safe GPT-5.6 cache-routing key.

    Returns a tracker-safe status only; neither the key nor prompt content is
    logged. Caller-supplied cache key and retention are intentionally untouched.
    """
    if not isinstance(payload, dict):
        return "malformed"

    family = _canonical_gpt56_family(target_model)
    if not family:
        return "implicit"
    if payload.get("prompt_cache_key"):
        existing = payload["prompt_cache_key"]
        if isinstance(existing, str) and existing.startswith("bsl-cache-"):
            return "bsl-generated"
        return "preserved"
    if not tools_config.get("caching_openai_key_bound", True):
        return "disabled"

    prefix = _stable_prefix_content(payload)
    if prefix is None:
        return "malformed"
    if len(prefix) < _OPENAI_GPT56_CACHE_MIN_PREFIX_CHARS:
        return "too-short"

    digest = hashlib.sha256(
        f"bsl-router-openai-cache-v1\0{family}\0{prefix}".encode("utf-8")
    ).hexdigest()
    payload["prompt_cache_key"] = f"bsl-cache-{family}-{digest}"
    if tools_config.get("caching_openai_retention_24h") and not payload.get("prompt_cache_retention"):
        payload["prompt_cache_retention"] = "24h"
    return "generated"


class PromptCachingAdapter:
    @staticmethod
    def apply_provider_caching(
        payload: Dict[str, Any],
        provider_name: str,
        target_model: str,
        tools_config: Dict[str, Any] = None,
        obs: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """
        Dynamically applies prompt caching strategies based on the specific model family.
        - Anthropic: Explicit cache_control tags (max 4). Gated by tools.caching_anthropic_explicit.
        - OpenAI GPT-5.6: Deterministic key-bound routing for large static prefixes only.
        - OpenAI / DeepSeek / GLM-5 / Minimax M3 / Qwen3.x: Implicit prefix matching.
        - Kimi K2.6+: Implicit prefix matching with optional `prompt_cache_key`. Gated by tools.caching_kimi_key_bound.

        tools_config: the config["tools"] dict. When None or key missing, defaults True
        (preserves backward-compatible always-on behavior for callers that don't pass config).

        obs: the observability module. When provided AND caching_tracker_enabled is True,
        emits per-request cache-strategy diagnostics to console_logs.
        """
        if not isinstance(payload, dict):
            return payload

        t = tools_config or {}
        tracker_enabled = t.get("caching_tracker_enabled", False)

        strategy = "none"
        cache_hint = ""

        if provider_name == "anthropic":
            # Gate: Anthropic explicit cache_control injection
            if not t.get("caching_anthropic_explicit", True):
                strategy = "anthropic-disabled"
                if tracker_enabled and obs:
                    _emit_tracker(obs, provider_name, target_model, strategy, "")
                return payload
            # Anthropic explicitly requires cache_control breakpoints
            strategy = "anthropic-explicit-ephemeral"
            system_content = payload.get("system")
            if system_content:
                if isinstance(system_content, str):
                    payload["system"] = [
                        {
                            "type": "text",
                            "text": system_content,
                            "cache_control": {"type": "ephemeral"}
                        }
                    ]
                    cache_hint = "system-prompt-cached"
        elif provider_name == "kimi" or "moonshot" in provider_name:
            # Gate: Kimi prompt_cache_key injection
            if not t.get("caching_kimi_key_bound", True):
                strategy = "kimi-disabled"
                if tracker_enabled and obs:
                    _emit_tracker(obs, provider_name, target_model, strategy, "")
                return payload
            strategy = "kimi-prefix-key-bound"
            # Moonshot Kimi uses Longest Prefix Matching.
            # To optimize cache routing, Kimi accepts a `prompt_cache_key` scheduling hint.
            # We hash the system prompt to guarantee consistent cluster routing.
            system_content = payload.get("system") or ""
            if isinstance(system_content, list):
                 system_content = str(system_content)
            if len(system_content) > 1024:
                cache_key = hashlib.md5(system_content.encode()).hexdigest()
                payload["prompt_cache_key"] = cache_key
                cache_hint = f"key={cache_key[:8]}"
        elif provider_name == "openai" and _canonical_gpt56_family(target_model):
            strategy = "openai-gpt-5.6-key-bound"
            cache_hint = _apply_openai_gpt56_cache_key(payload, target_model, t)
        elif provider_name in ["openai", "deepseek", "glm", "zhipu", "minimax", "qwen", "dashscope", "gemini"]:
            strategy = "implicit-prefix"
            cache_hint = "static-first-sorted"

        if tracker_enabled and obs:
            _emit_tracker(obs, provider_name, target_model, strategy, cache_hint)

        return payload

    @staticmethod
    def apply_static_first_sort(
        request: ChatCompletionRequest,
        tools_config: Dict[str, Any] = None,
    ) -> ChatCompletionRequest:
        """
        Reorders messages to ensure all system instructions and large reference blocks
        are pushed to the absolute top (Static-First) to maximize KV cache hits.
        Gated by tools.caching_static_sort (default True).

        GPT-5.6 OpenAI-compatible requests receive a key-bound routing hint here
        because this middleware is the universal pre-egress call path. The helper
        only hashes system/developer prefix content and fails open on invalid data.
        """
        t = tools_config or {}
        if t.get("caching_static_sort", True):
            static_msgs = [m for m in request.messages if m.role in ("system", "developer")]
            other_msgs = [m for m in request.messages if m.role not in ("system", "developer")]
            request.messages = static_msgs + other_msgs

        try:
            payload = request.model_dump(exclude_none=True)
            if _canonical_gpt56_family(request.model):
                _apply_openai_gpt56_cache_key(payload, request.model, t)
                if "prompt_cache_key" in payload:
                    request.prompt_cache_key = payload["prompt_cache_key"]
                if "prompt_cache_retention" in payload:
                    request.prompt_cache_retention = payload["prompt_cache_retention"]
        except Exception:
            pass  # Cache routing must never break normal dispatch
        return request


# ─────────────────────────────────────────────────────────────────────────────
# D2: Cache-aware routing telemetry + tiebreak (tools.cache_aware_routing,
# default OFF — when off, callers execute only the single flag read).
#
# Per (provider, connection) EWMA of the cache-read share of input tokens,
# sampled from upstream usage on the response path. In-memory only (LRU-capped
# at 64 entries, evict least-recently-updated; no persistence). Consumed ONLY
# as a FINAL tiebreak among connections the resolver already treats as
# interchangeable — never overrides authorization/health/round-robin order.
# Every entry point fails open: telemetry can never break routing.
# ─────────────────────────────────────────────────────────────────────────────

_CACHE_WARMTH_CAPACITY = 64
_CACHE_WARMTH_ALPHA = 0.3


class CacheWarmthTracker:
    """LRU-capped EWMA of cache_read/input ratio per (provider, conn_index).

    EWMA formula: new = alpha * sample + (1 - alpha) * prev, where the first
    sample initializes directly. The sample is the cache-READ share of the
    INCLUSIVE input total: OpenAI prompt_tokens is already inclusive, while
    Anthropic input_tokens is exclusive so fresh + cache_read + cache_creation
    are folded (mirrors main._extract_usage_tokens normalization).
    """

    def __init__(self, capacity: int = _CACHE_WARMTH_CAPACITY, alpha: float = _CACHE_WARMTH_ALPHA):
        self.capacity = max(1, int(capacity))
        self.alpha = float(alpha)
        self._state: "OrderedDict[tuple, float]" = OrderedDict()

    def update(self, provider: Any, conn_index: Any, usage: Any) -> float:
        """Fold one usage sample; returns the post-update warmth (0.0 on no data)."""
        if not isinstance(usage, dict):
            return 0.0
        cache_read = usage.get("cache_read_input_tokens", 0) or 0
        details = usage.get("prompt_tokens_details")
        cached = details.get("cached_tokens", 0) or 0 if isinstance(details, dict) else 0
        cached = cached or cache_read
        prompt = usage.get("prompt_tokens")
        if prompt:
            # OpenAI shape: prompt_tokens is already INCLUSIVE of cache.
            total = prompt
        else:
            # Anthropic shape: fold fresh + read + creation (EXCLUSIVE input).
            fresh = usage.get("input_tokens", 0) or 0
            create = usage.get("cache_creation_input_tokens", 0) or 0
            total = fresh + cache_read + create
        if not isinstance(total, (int, float)) or total <= 0:
            return 0.0
        sample = max(0.0, min(1.0, cached / total))
        key = (str(provider), conn_index)
        prev = self._state.get(key)
        value = sample if prev is None else (self.alpha * sample + (1.0 - self.alpha) * prev)
        self._state[key] = value
        self._state.move_to_end(key)
        while len(self._state) > self.capacity:
            self._state.popitem(last=False)
        return value

    def warmth(self, provider: Any, conn_index: Any) -> float:
        """Current warmth for a key; 0.0 when never sampled (cold = no signal)."""
        return self._state.get((str(provider), conn_index), 0.0)


_CACHE_WARMTH_TRACKER = CacheWarmthTracker()


def record_cache_warmth(provider: Any, conn_index: Any, usage: Any, tools_config: Any = None) -> None:
    """Response-path hook: fold one usage sample into the tracker.

    Gate-first: when tools.cache_aware_routing is off (default) this is a
    single dict read. Fail-open: any error is swallowed — telemetry must never
    break the proxy response path.
    """
    try:
        if not (tools_config or {}).get("cache_aware_routing", False):
            return
        if conn_index is None:
            return
        _CACHE_WARMTH_TRACKER.update(provider, conn_index, usage)
    except Exception:
        pass  # fail-open


def get_connection_warmth(provider: Any, conn_index: Any) -> float:
    """Read current warmth for (provider, conn_index). Fail-open, 0.0 default."""
    try:
        return _CACHE_WARMTH_TRACKER.warmth(provider, conn_index)
    except Exception:
        return 0.0  # fail-open


def reset_cache_warmth_tracker() -> None:
    """Test hook: clear all tracked warmth (module-global state)."""
    _CACHE_WARMTH_TRACKER._state.clear()


def pick_warmest_connection(
    provider_config: Any,
    provider_name: str,
    model_id: str,
    breaker=None,
    exclude_indexes=None,
    tools_config: Any = None,
) -> Optional[tuple]:
    """Cache-warmth FINAL tiebreak among equal-tier candidate connections.

    Runs alongside resolve_active_connection and returns a replacement
    (conn, index) ONLY when ALL of the following hold, else None (keep the
    resolver's pick — zero behavior change):
      - tools.cache_aware_routing is ON (default False),
      - the provider is NOT round_robin (rotation owns primary selection),
      - 2+ connections are otherwise interchangeable: enabled + authorized
        via model connection_indexes + not excluded by request-scoped key
        failover + breaker-healthy,
      - the warmest candidate has warmth > 0 (cold tracker = no signal) and
        is NOT already the deterministic lowest-index pick the resolver made.

    Eligibility mirrors app/utils/model_resolver._pick_connection exactly.
    Warmth NEVER overrides cost/health/latency ordering: the breaker filter
    and connection_indexes authorization are applied first, and ties in
    warmth fall back to the existing lowest-index rule. Fail-open: any error
    returns None (keep resolver pick).
    """
    try:
        t = tools_config or {}
        if not t.get("cache_aware_routing", False):
            return None
        if not isinstance(provider_config, dict) or provider_config.get("round_robin"):
            return None
        connections = provider_config.get("connections") or []
        eligible = [
            {"index": i, "conn": c}
            for i, c in enumerate(connections)
            if isinstance(c, dict) and c.get("enabled", True)
        ]
        if not eligible:
            return None
        # connection_indexes authorization (mirrors _pick_connection rules).
        indexes = None
        for m in provider_config.get("models", []) or []:
            if isinstance(m, dict) and m.get("id") == model_id:
                raw = m.get("connection_indexes")
                if isinstance(raw, list) and raw and all(isinstance(x, int) for x in raw):
                    indexes = set(raw)
                break
        if indexes is not None:
            eligible = [e for e in eligible if e["index"] in indexes]
        if not eligible:
            return None
        # Request-scoped key failover exclusions.
        if exclude_indexes:
            eligible = [e for e in eligible if e["index"] not in exclude_indexes]
        if not eligible:
            return None
        # Health gate: drop OPEN connections when the breaker is enabled
        # (fail-open on breaker errors, mirroring the resolver).
        if breaker is not None:
            try:
                if getattr(breaker, "enabled", False):
                    eligible = breaker.filter_healthy_connections(provider_name, model_id, eligible)
            except Exception:
                pass  # fail-open: the breaker is an optimization, never a gate
        if len(eligible) <= 1:
            return None
        # FINAL tiebreak key: warmth first, then the existing lowest-index rule.
        warmest = max(eligible, key=lambda e: (get_connection_warmth(provider_name, e["index"]), -e["index"]))
        if get_connection_warmth(provider_name, warmest["index"]) <= 0.0:
            return None
        if warmest["index"] == eligible[0]["index"]:
            return None  # resolver's deterministic lowest-index pick already wins
        conn = dict(warmest["conn"])
        conn.setdefault("format", provider_config.get("format"))
        conn.setdefault("type", provider_config.get("type"))
        return conn, warmest["index"]
    except Exception:
        return None  # fail-open: keep the resolver's pick


def propagate_prompt_cache_key(upstream_payload: Any, request_obj: Any) -> Any:
    """Carry a caller-supplied prompt_cache_key onto an upstream payload.

    UniversalNormalizer.normalize_to_anthropic builds the /v1/messages egress
    payload from scratch and drops extra fields; this restores the caller's
    key at the anthropic egress build. Passthrough only — never generates,
    overwrites, or removes. Fail-open.
    """
    try:
        if not isinstance(upstream_payload, dict) or upstream_payload.get("prompt_cache_key"):
            return upstream_payload
        if isinstance(request_obj, dict):
            value = request_obj.get("prompt_cache_key")
        else:
            value = getattr(request_obj, "prompt_cache_key", None)
        if isinstance(value, str) and value:
            upstream_payload["prompt_cache_key"] = value
    except Exception:
        pass  # fail-open
    return upstream_payload
