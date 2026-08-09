"""
Qwen family contract.

Qwen reasoning is a TWO-AXIS contract, and BOTH axes are version-dependent.
Official source: https://docs.qwencloud.com/developer-guides/text-generation/thinking

  Axis 1 — `enable_thinking` (bool). Qwen splits models into two modes:
      * Hybrid        — thinking toggles per request. This is every Qwen
                        generation up to and including 3.7. Qwen3.7-Max is
                        off/enable ONLY; no effort enum is published for it.
      * Thinking-only — always thinks, CANNOT be disabled (the `*-thinking`
                        SKUs). Sending enable_thinking=false there is a lie
                        the model cannot honor, so we omit the field instead.

  Axis 2 — `reasoning_effort` (enum). Published PER MODEL, not per vendor.
           qwen3.8-max accepts exactly {low, medium, xhigh}; default xhigh.
           There is NO `high` and NO `max`. Older generations publish no enum
           at all, so sending one is out-of-contract.

Operator-facing ladder (mirrored in app/static/app.js getThinkingSpec):

    qwen3.8-max             off / enable / low / medium / xhigh
    qwen3.7-max and older   off / enable

Anything outside a model's published enum is CLAMPED, never passed through —
see _V38_ALIASES for the rationale on direction.

Two defects this module previously shipped, both now covered by
app/tests/test_qwen_thinking_levels.py:

  1. `_EFFORT_WORDS = (low, medium, high, max)` contained two words Qwen
     rejects and OMITTED `xhigh` — so the one valid ceiling value (which is
     also the model's own default) was coerced away to the invalid `max`.
  2. A `require_effort` rule in `sanitize` injected an effort even when the
     operator selected `off`, so thinking could never be disabled on a
     hybrid model.

Qwen also REJECTS the GLM/DeepSeek-style `thinking` / `output_config` /
`reasoning` containers with "Request body format invalid", and rejects the
same sampling parameters as Kimi K3. Both strips are unconditional (they must
run even when thinking is off), so they live in `sanitize`.

Note: Qwen ids also match the generic GLM/Chinese-model pattern, which is
why this contract outranks it — previously guaranteed only by elif order.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional

from app.compat.families._base import Contract, Provenance, ProvenanceRecord, ThinkingContext

SOURCE = "families/qwen.py"

# ── Axis 2: published effort enums ───────────────────────────────────────
# qwen3.8-max, per official docs: "options: low, medium, xhigh; default xhigh".
_V38_EFFORTS = ("low", "medium", "xhigh")

# Words outside the published enum, mapped explicitly rather than by rank
# arithmetic. `high`/`max` round UP to the ceiling instead of down: xhigh is
# the model's OWN DEFAULT, so clamping `high` down to `medium` would silently
# deliver LESS reasoning than an unconfigured request — the opposite of what
# the operator asked for.
_V38_ALIASES = {
    "minimal": "low",
    "high": "xhigh",
    "max": "xhigh",
}

# ── Axis 1: thinking on/off vocabulary ───────────────────────────────────
# "thinking on, but no specific depth requested" — the model applies its own
# default effort. `adaptive` lands here because Qwen has no wire `adaptive`.
_ENABLE_WORDS = ("enable", "enabled", "adaptive", "on", "true")

# Operator explicitly turned thinking OFF. Distinct from 'auto'/'' which mean
# "operator expressed no preference" — for those we send nothing at all and
# let the provider default stand.
_DISABLE_WORDS = ("off", "none", "false", "disable", "disabled")
_UNSET_WORDS = ("", "auto")

# ── Payload hygiene (unconditional) ──────────────────────────────────────
_FORBIDDEN_SAMPLING = ("temperature", "top_p", "presence_penalty", "frequency_penalty", "n")
# Reasoning containers Qwen rejects outright.
_FORBIDDEN_OBJECTS = ("thinking", "output_config", "reasoning")

# ── Version / mode detection ─────────────────────────────────────────────
# Matches qwen3.8-max, qwen3.8-max-preview, qwen-3.8-max, Qwen3.8-Max, ...
_V38_RE = re.compile(r"qwen-?3\.8", re.IGNORECASE)
# Thinking-only SKUs: reasoning cannot be disabled.
_ALWAYS_ON_RE = re.compile(r"thinking", re.IGNORECASE)


def _is_v38(f_val: str) -> bool:
    """True for the Qwen3.8 generation, which publishes a real effort enum."""
    return bool(_V38_RE.search(f_val or ""))


def _is_always_on(f_val: str) -> bool:
    """True for `*-thinking` SKUs, where thinking cannot be turned off."""
    return bool(_ALWAYS_ON_RE.search(f_val or ""))


def _clamp_v38_effort(word: str) -> Optional[str]:
    """Resolve a requested tier onto qwen3.8-max's published enum.

    Returns None when no specific depth was requested (or the word is not a
    depth at all), in which case the effort field is omitted and the model
    applies its own default of `xhigh`.
    """
    w = (word or "").strip().lower()
    if w in _V38_EFFORTS:
        return w
    return _V38_ALIASES.get(w)


def _apply(
    payload: Dict[str, Any],
    ctx: ThinkingContext,
    prov: Provenance,
    contract: Contract,
) -> Dict[str, Any]:
    """Resolve both axes.

    Runs for EVERY request (contract is `always_applies`) because honoring an
    explicit `off` requires acting when no effort is selected — the registry's
    default gate skips `apply` for off/auto.
    """
    effort = str(ctx.effort or "").strip().lower()

    # Never fabricate a preference the operator did not express.
    if effort in _UNSET_WORDS:
        return payload

    # Always start from a clean slate: Qwen rejects these containers outright.
    values: Dict[str, Any] = {k: None for k in _FORBIDDEN_OBJECTS}

    # ── Explicit OFF ─────────────────────────────────────────────────────
    if effort in _DISABLE_WORDS:
        values["reasoning_effort"] = None
        if _is_always_on(ctx.f_val):
            # Thinking-only SKU: the flag cannot be honored, so omit it
            # rather than send a value the model will ignore.
            values["enable_thinking"] = None
            return prov.apply(payload, contract, "thinking_always_on", values)
        values["enable_thinking"] = False
        return prov.apply(payload, contract, "thinking_off", values)

    # ── Thinking ON ──────────────────────────────────────────────────────
    values["enable_thinking"] = True

    if not _is_v38(ctx.f_val):
        # Hybrid generations (3.7 and earlier) publish no effort enum. The
        # boolean is the ONLY reasoning axis; sending an effort is
        # out-of-contract, so strip any that came in from the client.
        values["reasoning_effort"] = None
        return prov.apply(payload, contract, "enable_thinking_bool", values)

    depth = None if effort in _ENABLE_WORDS else _clamp_v38_effort(effort)
    if depth is None:
        # `enable`/`adaptive`, or an unrecognized word: thinking on at the
        # model's own default depth (xhigh). Drop any stale/invalid effort.
        values["reasoning_effort"] = None
        return prov.apply(payload, contract, "enable_default_effort", values)

    values["reasoning_effort"] = depth
    rule = "reasoning_effort" if depth == effort else "reasoning_effort_clamped"
    return prov.apply(payload, contract, rule, values)


def _sanitize(
    payload: Dict[str, Any],
    ctx: ThinkingContext,
    prov: Provenance,
    contract: Contract,
) -> Dict[str, Any]:
    """Unconditional hygiene — runs even when thinking is off/unset.

    This is the ONLY code path that runs for an unset (`auto`) request, so it
    also has to police a `reasoning_effort` the CLIENT injected: an operator
    who never configured thinking can still receive a 400 if e.g. Claude Code
    forwards its own `reasoning_effort: high`.
    """
    removals: Dict[str, Any] = {k: None for k in _FORBIDDEN_SAMPLING}
    for key in _FORBIDDEN_OBJECTS:
        removals[key] = None
    payload = prov.apply(payload, contract, "strip_unsupported", removals)

    # Police a client-supplied effort against the model's published enum.
    # NOTE: deliberately no `require_effort` rule here. Injecting an effort
    # when the operator chose `off` was the bug that made thinking
    # undisableable on hybrid models.
    incoming = payload.get("reasoning_effort")
    if isinstance(incoming, str):
        word = incoming.strip().lower()
        if not _is_v38(ctx.f_val):
            payload = prov.apply(
                payload, contract, "strip_effort_unsupported", {"reasoning_effort": None}
            )
        elif word not in _V38_EFFORTS:
            clamped = _clamp_v38_effort(word)
            payload = prov.apply(
                payload,
                contract,
                "clamp_client_effort",
                {"reasoning_effort": clamped},  # None removes it → model default
            )

    # Qwen3.8-Max sometimes generates prose like "Tool X does not exist"
    # instead of emitting structured tool_calls.  Inject a brief reminder
    # into the first system message when tools are present.
    tools = payload.get("tools")
    messages = payload.get("messages")
    if tools and isinstance(messages, list) and messages:
        reminder = (
            "\n\n[BSL] All tools listed in the tools schema are available and "
            "functional. To use a tool, emit a structured tool_calls JSON. "
            "Do NOT claim tools are unavailable or do not exist."
        )
        first = messages[0]
        if isinstance(first, dict) and first.get("role") == "system":
            content = first.get("content", "")
            if isinstance(content, str) and "[BSL]" not in content:
                first["content"] = content + reminder
                payload["messages"] = messages
                prov.records.append(
                    ProvenanceRecord(
                        contract_id=contract.id,
                        source=contract.source,
                        rule="tool_availability_reminder",
                        fields=["messages[0].content"],
                    )
                )

    return payload


CONTRACTS = [
    Contract(
        id="qwen",
        source=SOURCE,
        priority=50,
        # `qwen(?!coder)` — a negative lookahead, NOT a separate `exclude`.
        # WHY: f_val is "provider/model". The `qwencoder` PROVIDER contains the
        # substring `qwen`, so a bare `qwen` pattern matched every model it
        # served (claude, gpt-5.6, ...). The previous fix bolted on
        # `exclude=r"(?:^|/)qwencoder"`, but that disqualifies the WHOLE match —
        # so `qwencoder/qwen3.8-max` (a REAL Qwen model, enabled in config) was
        # ALSO excluded and silently shipped temperature/top_p to a Qwen
        # endpoint with no reasoning contract. The lookahead only rejects the
        # `qwencoder` token itself: it fails at `^qwencoder` (next chars
        # "coder") but still matches the model at `/qwen3.8-max`. This is the
        # pattern the divergence tests always DOCUMENTED as intended.
        pattern=r"qwen(?!coder)",
        apply=_apply,
        sanitize=_sanitize,
        # Required to honor an explicit `off`: the registry's default gate
        # skips `apply` for off/auto, which is why `off` previously fell
        # through to sanitize's effort injection and meant "max".
        always_applies=True,
    ),
]
