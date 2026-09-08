"""
BSL Router Agent Compatibility Layer — Reasoning / Thinking Policy Engine

Phase 3: No more global thinking patch. Each provider/model family gets its
own reasoning policy that determines how thinking blocks are handled in
requests and replayed across turns.

Policy enum:
  drop                         — strip all thinking/reasoning fields
  passback_unsigned            — pass thinking back without signature validation
  passback_signed_only         — require valid Anthropic signature (first-party only)
  normalize_to_reasoning_content — convert to DeepSeek-style reasoning_content
  openai_responses_reasoning_items — use OpenAI Responses reasoning item format
  provider_native              — let the provider handle it natively
"""
from typing import Dict, Any, Optional, List
from dataclasses import dataclass
import json


@dataclass
class ReasoningPolicy:
    """Policy for how thinking/reasoning is handled for a model family."""
    name: str
    request_fields: List[str]       # Fields to inject in outbound request
    replay_policy: str              # How to handle thinking in multi-turn replay
    drop_unsigned: bool             # Whether to drop unsigned thinking blocks
    inject_thinking_config: bool    # Whether to inject thinking config if absent


# ─────────────────────────────────────────────────────────────────────
# Family-level policies
# ─────────────────────────────────────────────────────────────────────

FAMILY_POLICIES: Dict[str, ReasoningPolicy] = {
    "anthropic": ReasoningPolicy(
        name="anthropic",
        request_fields=["thinking"],
        replay_policy="passback_signed_only",
        drop_unsigned=True,
        inject_thinking_config=False,
    ),
    "glm-5.1": ReasoningPolicy(
        name="glm-5.1",
        request_fields=["thinking"],
        replay_policy="provider_native",
        drop_unsigned=False,
        inject_thinking_config=True,
    ),
    "glm-5.2": ReasoningPolicy(
        name="glm-5.2",
        request_fields=["thinking", "reasoning_effort"],
        replay_policy="provider_native",
        drop_unsigned=False,
        inject_thinking_config=True,
    ),
    "deepseek": ReasoningPolicy(
        name="deepseek",
        request_fields=["reasoning", "reasoning_content"],
        replay_policy="normalize_to_reasoning_content",
        drop_unsigned=False,
        inject_thinking_config=False,
    ),
    # Kimi K2 family:
    #   K2.7 Code / K2 Thinking — always-on reasoning, no parameter needed.
    #   K2.5 / K2.6           — toggleable via enable_thinking boolean.
    # All variants return reasoning_content and REQUIRE it to be passed back
    # in multi-turn history.  We use passback_unsigned (not provider_native)
    # because Kimi is NOT first-party Anthropic and does not produce valid
    # Anthropic thinking signatures — but it still demands the reasoning
    # trace in the conversation history on subsequent turns.
    "kimi": ReasoningPolicy(
        name="kimi",
        request_fields=["enable_thinking"],  # only injected for K2.5/K2.6
        replay_policy="passback_unsigned",
        drop_unsigned=False,
        inject_thinking_config=True,
    ),
    "gemini": ReasoningPolicy(
        name="gemini",
        request_fields=["thinking_config"],
        replay_policy="provider_native",
        drop_unsigned=False,
        inject_thinking_config=True,
    ),
    "openai": ReasoningPolicy(
        name="openai",
        request_fields=["reasoning_effort"],
        replay_policy="openai_responses_reasoning_items",
        drop_unsigned=False,
        inject_thinking_config=False,
    ),
    "default": ReasoningPolicy(
        name="default",
        request_fields=[],
        replay_policy="drop",
        drop_unsigned=True,
        inject_thinking_config=False,
    ),
}

# ─────────────────────────────────────────────────────────────────────
# Thinking config value maps per model
# ─────────────────────────────────────────────────────────────────────

# GLM-5.1 thinking UI options: off / enabled / adaptive
# GLM-5.2 thinking UI options: off / low / medium / max
THINKING_CONFIG_MAP: Dict[str, Dict[str, Any]] = {
    "glm-5.1": {
        "off": None,  # Don't inject
        "enabled": {"type": "enabled"},
        "adaptive": {"type": "adaptive"},
    },
    "glm-5.2": {
        "off": None,
        "low": {"type": "enabled", "reasoning_effort": "low"},
        "medium": {"type": "enabled", "reasoning_effort": "medium"},
        "max": {"type": "enabled", "reasoning_effort": "max"},
    },
    "deepseek": {
        "off": None,
        "enabled": {"reasoning": True},
        "adaptive": {"reasoning": True},  # DeepSeek doesn't have adaptive, treat as enabled
    },
    # Kimi K2 family thinking config:
    #   K2.7 Code      — always-on, inject enable_thinking=True as a no-op hint.
    #   K2.5 / K2.6    — toggleable, enable_thinking boolean controls reasoning.
    # There are NO reasoning-effort levels (low/medium/high) for Kimi.
    # The 'enable' and 'adaptive' BSL Router vocabularies both map to
    # enable_thinking=True; 'max'/'high'/'xhigh' also map to True since
    # Kimi does not support graduated effort — thinking is binary.
    "kimi": {
        "off": None,                                     # Don't inject (K2.7 ignores)
        "enabled": {"enable_thinking": True},            # K2.5/K2.6 toggle on
        "adaptive": {"enable_thinking": True},           # alias — Kimi is binary
        "auto": {"enable_thinking": True},               # alias — maps to on
        "max": {"enable_thinking": True},                # alias — Kimi has no effort levels
        "high": {"enable_thinking": True},               # alias
        "xhigh": {"enable_thinking": True},              # alias
    },
}


def detect_family(model_id: str, provider_name: str) -> str:
    """Detect model family from model ID and provider name."""
    model_lower = model_id.lower()

    if "claude" in model_lower or provider_name in ("anthropic", "claude"):
        return "anthropic"
    if "glm-5.2" in model_lower or "glm4" in model_lower:
        return "glm-5.2"
    if "glm-5.1" in model_lower or "glm" in model_lower:
        return "glm-5.1"
    # Kimi / Moonshot — check before generic OpenAI catch-all.
    # Matches: kimi-k2.7-code, kimi-k2.6, kimi-k2.5, kimi-2.6-thinking,
    #          moonshotai/kimi-k2.6, free/kimi-k2.6, etc.
    if "kimi" in model_lower or "k2." in model_lower or "moonshot" in provider_name.lower():
        return "kimi"
    if "deepseek" in model_lower:
        return "deepseek"
    if "gemini" in model_lower or provider_name == "gemini":
        return "gemini"
    if model_lower.startswith(("gpt-", "o1", "o3", "o4")) or provider_name == "openai":
        return "openai"

    return "default"


def get_policy(model_id: str, provider_name: str) -> ReasoningPolicy:
    """Get the reasoning policy for a model/provider combination."""
    family = detect_family(model_id, provider_name)
    return FAMILY_POLICIES.get(family, FAMILY_POLICIES["default"])


def get_thinking_config(model_id: str, thinking_setting: str) -> Optional[Dict[str, Any]]:
    """
    Resolve a thinking setting (e.g. 'high', 'adaptive', 'off') to the
    provider-specific config object.

    Returns None if thinking should not be injected.
    """
    family = detect_family(model_id, "")
    config_map = THINKING_CONFIG_MAP.get(family, {})

    if thinking_setting.lower() not in config_map:
        # Unknown setting — don't inject
        return None

    return config_map[thinking_setting.lower()]


def apply_thinking_to_anthropic_payload(
    payload: Dict[str, Any],
    model_id: str,
    provider_name: str,
    thinking_setting: str = "off",
) -> Dict[str, Any]:
    """
    Inject thinking config into an Anthropic-format outbound payload.

    For GLM models, this sets the `thinking` field on the request.
    For first-party Anthropic, this only injects if the client already
    sent thinking config (we don't fabricate thinking for Anthropic).
    """
    policy = get_policy(model_id, provider_name)

    if not policy.inject_thinking_config:
        # Don't inject — let the client's thinking config pass through if present
        return payload

    config = get_thinking_config(model_id, thinking_setting)
    if config is None:
        # Thinking is off — don't inject
        return payload

    # Inject thinking config — config IS the thinking object (e.g. {"type": "enabled"})
    if "type" in config:
        payload["thinking"] = {"type": config["type"]}
    if "reasoning_effort" in config:
        payload["reasoning_effort"] = config["reasoning_effort"]
    if "reasoning" in config:
        payload["reasoning"] = config["reasoning"]
    if "enable_thinking" in config:
        payload["enable_thinking"] = config["enable_thinking"]

    return payload


def strip_thinking_from_messages(messages: List[Dict[str, Any]], policy: ReasoningPolicy) -> List[Dict[str, Any]]:
    """
    Strip or preserve thinking blocks in message history based on policy.

    For 'drop' and 'passback_signed_only' (with unsigned blocks), removes
    thinking content blocks from assistant messages.
    For 'provider_native' and 'passback_unsigned', preserves them.
    For 'normalize_to_reasoning_content', converts thinking to reasoning_content.
    """
    if policy.replay_policy in ("provider_native", "passback_unsigned", "openai_responses_reasoning_items"):
        return messages

    cleaned = []
    for msg in messages:
        if not isinstance(msg, dict):
            cleaned.append(msg)
            continue

        content = msg.get("content")
        if not isinstance(content, list):
            cleaned.append(msg)
            continue

        new_content = []
        for block in content:
            if not isinstance(block, dict):
                new_content.append(block)
                continue

            block_type = block.get("type", "")

            if block_type == "thinking":
                if policy.replay_policy == "drop":
                    continue  # Strip
                elif policy.replay_policy == "passback_signed_only":
                    # Only keep if it has a valid signature
                    if block.get("signature"):
                        new_content.append(block)
                    # else drop unsigned
                elif policy.replay_policy == "normalize_to_reasoning_content":
                    # Convert to reasoning_content field (DeepSeek style)
                    # The thinking text becomes reasoning_content on the message
                    thinking_text = block.get("thinking", "") or block.get("text", "")
                    if thinking_text and "reasoning_content" not in msg:
                        msg["reasoning_content"] = thinking_text
                    continue  # Don't keep the thinking block
                else:
                    new_content.append(block)
            else:
                new_content.append(block)

        msg = {**msg, "content": new_content}
        cleaned.append(msg)

    return cleaned


# ─────────────────────────────────────────────────────────────────────
# Degenerate-output guard (Lane 1: gpt-family / vsllm-r)
# ─────────────────────────────────────────────────────────────────────
# Evidence (2026-09-06, vsllm-r production logs): the vsllm.cc gpt upstream
# returns HTTP 200 but a degenerate stream — 42k-63k input tokens, only
# 9-112 output tokens of reasoning rubble — at thinking effort:xhigh
# (gpt:graded_effort). Small test payloads do NOT reproduce it, so the
# trigger is the interaction of LARGE input + TOP-tier effort. Two defenses:
#
#   1. PREVENTION (effort-cap): when estimated input tokens reach
#      DEGENERATE_INPUT_TOKEN_FLOOR and the resolved effort is xhigh/max,
#      cap it to DEGENERATE_EFFORT_CAP ("high") before the payload leaves.
#   2. DETECTION (telemetry): when a finished 200 stream shows in>=floor
#      and out<=ceiling, flag it as degenerate for observability — the
#      rubble already streamed to the client, so no retry is possible.
#
# Thresholds sit OUTSIDE the observed band (floor below 42k, ceiling above
# 112) so the guard trips on the failure class, not the exact incidents.

DEGENERATE_INPUT_TOKEN_FLOOR = 40000
DEGENERATE_OUTPUT_TOKEN_CEILING = 200
DEGENERATE_EFFORT_CAP = "high"

# Per-model scope (2026-09-07 probe, 12 uncapped max calls): sol PASS 4/4 at
# every size, astra EMPTY 4/4 on an upstream 524/502/429 storm an effort cap
# cannot fix, terra RUBBLE 4/4 (and capped-high terra PASS @100K+). So the
# cap defaults to gpt-5.6-terra ONLY; tools.degenerate_output_guard.models
# widens (or with [] disables) the set and model_overrides flips per-model
# membership + tuning.
DEGENERATE_GUARD_MODELS_DEFAULT = ("gpt-5.6-terra",)

# Graduated effort ladder (2026-09-07): the flat cap above generalizes to a
# config-driven ladder of (input_floor, effort_cap) tiers walked DESC by
# floor — see cap_effort_for_input_size. Default keeps today's behavior:
# a single tier capping xhigh/max to "high" at/above the observed floor.
EFFORT_LADDER_DEFAULT = ((DEGENERATE_INPUT_TOKEN_FLOOR, DEGENERATE_EFFORT_CAP),)

# Efforts known to trigger the rubble signature. "high" itself has not been
# observed to degenerate, so it passes through untouched.
_DEGENERATE_CAP_EFFORTS = ("xhigh", "max")


def cap_effort_for_input_size(
    effort: Any, input_tokens: Any, floor: int = None, ladder=None
) -> tuple:
    """Cap a top-tier reasoning effort when the prompt is degenerate-sized.

    `ladder` is a sequence of (input_floor, effort_cap) tiers walked sorted
    DESC by floor: the first tier whose floor the input reaches wins and
    caps xhigh/max to that tier's cap. None -> EFFORT_LADDER_DEFAULT; an
    empty sequence is an explicit opt-out (never caps); a non-list/non-tuple
    value falls back to the default ladder with one console warn. `floor`
    is the legacy single-threshold override, superseded by `ladder` (kept
    only so older call sites keep their keyword). Returns (effort, capped).
    Pure: no payload mutation, no config I/O — the caller owns writing the
    result (single-writer discipline: this runs AFTER resolve_thinking, so
    it caps the already-resolved value; the malformed-ladder fallback emits
    a single console warn, nothing else).
    """
    try:
        n_in = int(input_tokens or 0)
    except (TypeError, ValueError):
        n_in = 0
    e = str(effort or "").strip().lower()
    if ladder is None:
        _ladder = EFFORT_LADDER_DEFAULT
    elif isinstance(ladder, (list, tuple)):
        _ladder = ladder
    else:
        print(
            "[DegenerateGuard] effort_ladder must be a list/tuple of "
            "(floor, cap) tiers; falling back to the default ladder",
            flush=True,
        )
        _ladder = EFFORT_LADDER_DEFAULT
    if e not in _DEGENERATE_CAP_EFFORTS or not _ladder:
        return e, False
    # Defensive tier sanitize: malformed entries are skipped, never raised —
    # the guard is fail-open and config paths bypassing degenerate_guard_config
    # must not crash routing. Valid tier shape: (int floor >= 0, non-empty cap).
    tiers = []
    for t in _ladder:
        try:
            t_floor, t_cap = t
            t_floor = int(t_floor)
        except (TypeError, ValueError):
            continue
        t_cap = str(t_cap or "").strip().lower()
        if t_floor < 0 or not t_cap:
            continue
        tiers.append((t_floor, t_cap))
    if not tiers:
        return e, False
    for tier_floor, tier_cap in sorted(tiers, key=lambda t: t[0], reverse=True):
        if n_in >= tier_floor:
            return tier_cap, True
    return e, False


def is_degenerate_output(in_tokens: Any, out_tokens: Any) -> bool:
    """True when a completed response matches the rubble signature.

    in>=DEGENERATE_INPUT_TOKEN_FLOOR and out<=DEGENERATE_OUTPUT_TOKEN_CEILING
    (the observed band is 9-112 output on 42k-63k input). out==0 is also
    degenerate here — main.py already has its own zero-token combo fallback
    for the pre-emission case, and this predicate additionally catches the
    post-emission zero-usage stream for telemetry.
    """
    try:
        n_in = int(in_tokens or 0)
        n_out = int(out_tokens or 0)
    except (TypeError, ValueError):
        return False
    return (
        n_in >= DEGENERATE_INPUT_TOKEN_FLOOR
        and n_out <= DEGENERATE_OUTPUT_TOKEN_CEILING
    )


def _validated_ladder(ladder_raw) -> tuple:
    """Validate an effort_ladder config value -> (tiers, dropped_count).

    Shared by the global effort_ladder and the per-model override ladders:
    valid shape is a list of {floor: int >= 0, cap: non-empty str} dicts;
    anything else counts as dropped and the caller owns the (single) warn.
    """
    tiers = []
    dropped = 0
    for entry in ladder_raw or ():
        t_floor = t_cap = None
        if isinstance(entry, dict):
            try:
                t_floor = int(entry.get("floor"))
                t_cap = str(entry.get("cap") or "").strip().lower()
            except (TypeError, ValueError):
                pass
        if t_floor is None or t_floor < 0 or not t_cap:
            dropped += 1
            continue
        tiers.append((t_floor, t_cap))
    return tuple(tiers), dropped


def canonical_model_id(model_id: Any) -> str:
    """Canonicalize a model id through the existing variant-ID choke point.

    The 2026-09-06 variant-ID normalization lives in
    app/compat/families/_base.py ThinkingContext.__post_init__ (digit-dash-
    digit -> digit-dot-digit on the match target) — reused here, NOT
    re-implemented, so config-side model scoping
    (tools.degenerate_output_guard.models / model_overrides) agrees with
    contract matching: gpt-5-6-terra == gpt-5.6-terra. Word dashes
    (gpt-6-astra, kimi-k3) are unaffected. The import is deferred because
    reasoning_policy loads before the families package on some entry paths
    and the helper only runs at request/config time. Fail-open: any import
    or construction problem falls back to the lowercased input so guard
    config can never break routing.
    """
    s = str(model_id or "").strip().lower()
    try:
        from app.compat.families._base import ThinkingContext

        return ThinkingContext(f_val=s).f_val
    except Exception:
        return s


def degenerate_guard_config(config: Optional[dict] = None) -> Dict[str, Any]:
    """Resolve tools.degenerate_output_guard into a normalized settings dict.

    Accepts: absent/None (defaults), bool (enabled flag only), or a dict
    {enabled, effort_cap, input_token_floor, output_token_ceiling,
    effort_ladder, models, model_overrides}. Default is ENABLED — the guard
    only fires on the observed failure class.

    `effort_ladder` is the graduated form of the flat effort_cap +
    input_token_floor pair: a list of {floor, cap} entries stored as a
    tuple of (floor, cap) tiers. Invalid entries (non-dict, floor not an
    int >= 0, cap empty) are dropped with at most ONE console warn line
    total. When effort_ladder is absent the key is omitted and callers
    fall back to a single (input_token_floor, effort_cap) tier, so legacy
    flat keys keep working unchanged.

    `models` (2026-09-07) scopes the cap to a per-model set: a list of
    model ids stored as a tuple of canonical ids (dash variants fold onto
    dotted canonicals via canonical_model_id). Absent -> the default set
    (DEGENERATE_GUARD_MODELS_DEFAULT, gpt-5.6-terra only); [] is an
    explicit opt-out (cap never applies). `model_overrides` is a dict
    {model_id: {enabled, effort_cap?, input_token_floor?, effort_ladder?}}
    keyed by canonical id; unknown models are kept but stay inert until the
    decision side consults them, and invalid shapes are dropped fail-open.
    All invalidity across the new keys emits at most ONE console warn line
    total.
    """
    cfg = {
        "enabled": True,
        "effort_cap": DEGENERATE_EFFORT_CAP,
        "input_token_floor": DEGENERATE_INPUT_TOKEN_FLOOR,
        "output_token_ceiling": DEGENERATE_OUTPUT_TOKEN_CEILING,
        "models": DEGENERATE_GUARD_MODELS_DEFAULT,
        "model_overrides": {},
    }
    raw = (config or {}).get("tools", {}).get("degenerate_output_guard")
    if raw is None:
        return cfg
    if isinstance(raw, bool):
        cfg["enabled"] = raw
        return cfg
    if isinstance(raw, dict):
        if "enabled" in raw:
            cfg["enabled"] = bool(raw.get("enabled"))
        if raw.get("effort_cap"):
            cfg["effort_cap"] = str(raw["effort_cap"]).lower()
        try:
            if raw.get("input_token_floor") is not None:
                cfg["input_token_floor"] = max(0, int(raw["input_token_floor"]))
        except (TypeError, ValueError):
            pass
        try:
            if raw.get("output_token_ceiling") is not None:
                cfg["output_token_ceiling"] = max(0, int(raw["output_token_ceiling"]))
        except (TypeError, ValueError):
            pass
        ladder_raw = raw.get("effort_ladder")
        if isinstance(ladder_raw, list):
            tiers, dropped = _validated_ladder(ladder_raw)
            if dropped:
                print(
                    f"[DegenerateGuard] effort_ladder: dropped {dropped} invalid "
                    f"entr{'y' if dropped == 1 else 'ies'} (need "
                    "{floor: int >= 0, cap: non-empty str})",
                    flush=True,
                )
            cfg["effort_ladder"] = tiers
        # ── Per-model scope: models set + overrides (2026-09-07) ──────────
        # Invalidity here is fail-open and shares ONE warn line: guard
        # config problems must never break routing.
        _scope_warns = []
        models_raw = raw.get("models")
        if models_raw is None:
            pass  # absent -> default set (gpt-5.6-terra only)
        elif isinstance(models_raw, list):
            parsed_models = []
            bad_models = 0
            for entry in models_raw:
                if not isinstance(entry, str) or not entry.strip():
                    bad_models += 1
                    continue
                parsed_models.append(canonical_model_id(entry))
            if bad_models:
                _scope_warns.append(
                    f"{bad_models} invalid models entr"
                    f"{'y' if bad_models == 1 else 'ies'}"
                )
            cfg["models"] = tuple(parsed_models)  # [] == explicit opt-out
        else:
            _scope_warns.append("models must be a list of model ids")
        ovr_raw = raw.get("model_overrides")
        if ovr_raw is not None:
            if isinstance(ovr_raw, dict):
                overrides = {}
                bad_ovr = 0
                for key, sub in ovr_raw.items():
                    if (
                        not isinstance(key, str)
                        or not isinstance(sub, dict)
                        or not key.strip()
                    ):
                        bad_ovr += 1
                        continue
                    norm: Dict[str, Any] = {}
                    if "enabled" in sub:
                        norm["enabled"] = bool(sub.get("enabled"))
                    if sub.get("effort_cap"):
                        norm["effort_cap"] = str(sub["effort_cap"]).strip().lower()
                    try:
                        if sub.get("input_token_floor") is not None:
                            norm["input_token_floor"] = max(0, int(sub["input_token_floor"]))
                    except (TypeError, ValueError):
                        bad_ovr += 1
                    if isinstance(sub.get("effort_ladder"), list):
                        o_tiers, o_dropped = _validated_ladder(sub["effort_ladder"])
                        bad_ovr += o_dropped
                        norm["effort_ladder"] = o_tiers
                    overrides[canonical_model_id(key)] = norm
                if bad_ovr:
                    _scope_warns.append(
                        f"{bad_ovr} invalid model_overrides shape"
                        f"{'s' if bad_ovr != 1 else ''}"
                    )
                cfg["model_overrides"] = overrides
            else:
                _scope_warns.append("model_overrides must be a dict")
        if _scope_warns:
            print(
                "[DegenerateGuard] model scope config dropped "
                + "; ".join(_scope_warns)
                + " (fail-open)",
                flush=True,
            )
    return cfg


def degenerate_cap_chip_state(guard_cfg: Optional[dict], model_id: str) -> bool:
    """Derive the Providers-UI "Cap 40K→high" chip state for one model.

    model_overrides[<canonical>].enabled wins when present; otherwise the
    models list decides — so the default config shows terra ON and sol /
    astra OFF. Mirrored in app.js deriveCapChipState (the UI reads the raw
    config before a save normalizes it); keep the two in sync. Pure: no
    I/O, never raises, canonicalizes both sides.
    """
    cfg = guard_cfg if isinstance(guard_cfg, dict) else {}
    cid = canonical_model_id(model_id)
    overrides = cfg.get("model_overrides")
    if isinstance(overrides, dict):
        ovr = overrides.get(cid)
        if isinstance(ovr, dict) and "enabled" in ovr:
            return bool(ovr["enabled"])
    models = cfg.get("models")
    if models is None or not isinstance(models, (list, tuple)):
        models = DEGENERATE_GUARD_MODELS_DEFAULT
    models = {
        canonical_model_id(m) for m in models if isinstance(m, str) and m.strip()
    }
    return cid in models


def get_ui_thinking_options(model_id: str) -> List[str]:
    """
    Return the available thinking UI options for a model.

    GLM-5.1: off / enabled / adaptive
    GLM-5.2: off / low / medium / max
    DeepSeek: off / enabled
    Others: off
    """
    family = detect_family(model_id, "")

    if family == "glm-5.1":
        return ["off", "enabled", "adaptive"]
    if family == "glm-5.2":
        return ["off", "low", "medium", "max"]
    if family == "deepseek":
        return ["off", "enabled"]
    if family == "kimi":
        # K3 is a REAL reasoning_effort model: low/high/max (NO medium).
        # K2 is binary enable_thinking. detect_family returns "kimi" for
        # both, so disambiguate on the model id here.
        if "k3" in model_id.lower():
            return ["low", "high", "max"]
        return ["off", "enabled"]
    if family == "gemini":
        return ["off", "enabled"]
    if family == "openai":
        return ["off", "low", "medium", "high", "xhigh", "max"]

    return ["off"]
