"""
Meta Muse Spark family contract / Hợp đồng gia đình Meta Muse Spark.

Muse Spark is Meta's multimodal reasoning model (1M context). Wire shape
diverges by version — detection is from `f_val`:

  * contains ``1.1``  → Muse Spark 1.1 wire
  * contains ``1.2``  → Muse Spark 1.2 wire
  * unversioned bare ``muse-spark`` → **1.2** (latest default; document
    this so a future 1.3 bump can re-point the fallback deliberately)

Muse Spark 1.1 (Meta Model API)
-------------------------------
Uses ``thinking`` + ``output_config.effort``. The model always reasons
natively; disabling is UNSUPPORTED.

  * Base: ``thinking: {type: adaptive}`` (model default depth).
  * Explicit depth ``low`` / ``medium`` / ``high`` / ``xhigh`` ALSO sets
    ``output_config: {effort: <word>}``.
  * ``enable`` / ``adaptive`` / ``auto`` / unset → adaptive only, no
    ``output_config`` override.
  * ``off`` / ``none`` / ``minimal`` / ``disable`` → coerce to lowest:
    adaptive + ``output_config.effort = low`` (rule ``off_coerced_to_low``).
  * Unknown explicit words (``max``, ``ultra``, garbage) → ``xhigh``
    (highest published for 1.1).
  * Token-budget compat: if the INCOMING payload already has
    ``thinking.budget_tokens`` (int >= 1024), preserve
    ``thinking: {type: enabled, budget_tokens: n}`` and still apply
    ``output_config.effort`` when a depth was requested.
  * ``display`` (summarized / omitted / etc.) must PASS THROUGH untouched.

Muse Spark 1.2 (Meta Model API, OpenAI-compatible wire)
-------------------------------------------------------
Uses top-level ``reasoning_effort`` (snake_case on chat-completions;
camelCase ``reasoningEffort`` / nested ``reasoning.effort`` in Meta docs
are Responses-API/SDK naming — BSL speaks OpenAI-compatible wire).

  * Vocabulary: ``minimal`` / ``low`` / ``medium`` / ``high`` / ``xhigh``
    (DEFAULT) / ``ultra``. ``ultra`` is a valid client-side multi-agent
    scaling word and passes through when explicitly selected.
  * Unset / ``auto`` → OMIT ``reasoning_effort`` entirely (model default
    is xhigh; do not fabricate).
  * ``none`` / ``off`` / ``disable`` → ``minimal`` (HTTP 400 avoidance;
    rule ``off_coerced_to_minimal``). ``none`` is NOT supported upstream.
  * ``max`` → ``xhigh``. Unknown garbage explicit words → ``xhigh``.

Contract identity (id/priority/pattern/always_applies) is shared across
both wires — only the apply branch diverges.

Muse Spark là model reasoning đa phương thức của Meta (cửa sổ 1M token).
Hình dạng wire phụ thuộc phiên bản (1.1 vs 1.2); bare ``muse-spark``
mặc định theo wire 1.2 (bản mới nhất).
"""
from __future__ import annotations

from typing import Any, Dict

from app.compat.families._base import (
    Contract,
    Provenance,
    ThinkingContext,
)

SOURCE = "families/muse.py"

# 1.1 published depth words for output_config.effort.
_MUSE11_DEPTHS = frozenset({"low", "medium", "high", "xhigh"})
# 1.2 full vocabulary (includes minimal + ultra; xhigh is model default).
_MUSE12_EFFORTS = frozenset({"minimal", "low", "medium", "high", "xhigh", "ultra"})
# Client intents that mean "turn thinking off" — unsupported on Muse; coerce.
_OFF_WORDS = frozenset({"none", "off", "disable"})
# 1.1 enable-like words that keep model-default depth (no output_config).
_MUSE11_ENABLE_LIKE = frozenset({"enable", "adaptive", "auto", ""})


def _wire_version(f_val: str) -> str:
    """Return ``1.1`` or ``1.2`` from f_val.

    Unversioned bare muse-spark defaults to 1.2 (latest). A future 1.3 must
    update this deliberately rather than inherit an accidental 1.1 match.
    """
    fv = (f_val or "").lower()
    if "1.1" in fv:
        return "1.1"
    # contains 1.2, or unversioned → 1.2
    return "1.2"


def _merge_output_config(payload: Dict[str, Any], effort: str) -> Dict[str, Any]:
    """Return output_config with effort set, preserving any sibling keys."""
    oc = payload.get("output_config")
    if not isinstance(oc, dict):
        oc = {}
    else:
        oc = dict(oc)
    oc["effort"] = effort
    return oc


def _thinking_base(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Build the 1.1 thinking object, preserving budget_tokens compat.

    If the incoming payload already carries thinking.budget_tokens
    (int >= 1024), emit {type: enabled, budget_tokens: n}; otherwise
    {type: adaptive}. Any client-supplied display key is left on the
    payload itself (passthrough) — we never touch top-level `display`.
    """
    incoming = payload.get("thinking")
    if isinstance(incoming, dict):
        bt = incoming.get("budget_tokens")
        if isinstance(bt, int) and bt >= 1024:
            return {"type": "enabled", "budget_tokens": bt}
    return {"type": "adaptive"}


def _apply_11(
    payload: Dict[str, Any],
    ctx: ThinkingContext,
    prov: Provenance,
    contract: Contract,
) -> Dict[str, Any]:
    """Muse Spark 1.1: thinking + optional output_config.effort."""
    effort = str(ctx.effort or "").lower()
    thinking = _thinking_base(payload)

    # OFF / none / minimal / disable → lowest published depth (disabling
    # is unsupported; coerce rather than 400 or silent no-op).
    if effort in _OFF_WORDS or effort == "minimal":
        return prov.apply(
            payload,
            contract,
            "off_coerced_to_low",
            {
                "thinking": thinking,
                "output_config": _merge_output_config(payload, "low"),
            },
        )

    # Explicit published depths → adaptive (or budget-enabled) + effort.
    if effort in _MUSE11_DEPTHS:
        return prov.apply(
            payload,
            contract,
            "thinking_adaptive_effort",
            {
                "thinking": thinking,
                "output_config": _merge_output_config(payload, effort),
            },
        )

    # enable / adaptive / auto / unset → model default depth, no override.
    if effort in _MUSE11_ENABLE_LIKE or not ctx.effort_is_explicit:
        return prov.apply(
            payload,
            contract,
            "thinking_adaptive",
            {"thinking": thinking},
        )

    # max / ultra / garbage → highest published for 1.1.
    return prov.apply(
        payload,
        contract,
        "thinking_adaptive_effort",
        {
            "thinking": thinking,
            "output_config": _merge_output_config(payload, "xhigh"),
        },
    )


def _apply_12(
    payload: Dict[str, Any],
    ctx: ThinkingContext,
    prov: Provenance,
    contract: Contract,
) -> Dict[str, Any]:
    """Muse Spark 1.2: top-level reasoning_effort (OpenAI-compatible wire)."""
    effort = str(ctx.effort or "").lower()

    # none / off / disable → minimal (none is a 400 upstream).
    # Checked BEFORE the OFF_VALUES "unset" gate: none/off live in OFF_VALUES
    # so effort_is_explicit is False, but they must still coerce — not omit.
    if effort in _OFF_WORDS:
        return prov.apply(
            payload,
            contract,
            "off_coerced_to_minimal",
            {"reasoning_effort": "minimal"},
        )

    # Unset / auto / "" → omit entirely (model default is xhigh; do not fabricate).
    if not ctx.effort_is_explicit:
        return payload

    # Published vocabulary incl. ultra passes through exactly.
    if effort in _MUSE12_EFFORTS:
        return prov.apply(
            payload,
            contract,
            "reasoning_effort",
            {"reasoning_effort": effort},
        )

    # max or unknown garbage → xhigh (highest common published level).
    return prov.apply(
        payload,
        contract,
        "reasoning_effort_clamped",
        {"reasoning_effort": "xhigh"},
    )


def _apply(
    payload: Dict[str, Any],
    ctx: ThinkingContext,
    prov: Provenance,
    contract: Contract,
) -> Dict[str, Any]:
    if _wire_version(ctx.f_val) == "1.1":
        return _apply_11(payload, ctx, prov, contract)
    return _apply_12(payload, ctx, prov, contract)


CONTRACTS = [
    Contract(
        id="muse-spark",
        source=SOURCE,
        priority=48,
        # Match both hyphen and underscore variants.
        pattern=r"muse[\-_\s]?spark",
        apply=_apply,
        # Always run: 1.1 must emit thinking even when effort is unset;
        # 1.2 must still coerce none/off away from a client-injected 400.
        always_applies=True,
    ),
]
