from typing import Dict, Any, Optional
from app.models import ChatCompletionRequest
from datetime import datetime
import hashlib
import json


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
