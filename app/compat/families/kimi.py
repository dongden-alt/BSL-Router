"""
Kimi / Moonshot family — TWO incompatible contracts under one vendor.

This family is the reference example for why the registry keys on
contract generation rather than company:

  K3 -> a reasoning model. Requires a top-level `reasoning_effort` and
        REJECTS sampling parameters (temperature, top_p, presence_penalty,
        frequency_penalty, n) and the `thinking` object outright. The
        strip is unconditional — it must happen even when thinking is
        off, so it lives in `sanitize`, not `apply`.

        Per official Moonshot docs (2026-08-02):
        - Thinking is ALWAYS ON; cannot be disabled.
        - reasoning_effort supports "low", "high", "max" (default "max").
          NO "medium" — passing it causes a 400.
        - temperature=1.0, top_p=0.95, n=1, presence_penalty=0,
          frequency_penalty=0 are FIXED; omit them from requests.

  K2 -> reasoning is controlled by a top-level `enable_thinking` boolean
        in BOTH OpenAI- and Anthropic-format reseller channels — NOT an
        Anthropic thinking object. K2.7-code and *-thinking SKUs reason
        unconditionally (the boolean is a harmless hint); K2.5/K2.6 honor
        it as the on/off switch.

        Per official Moonshot docs:
        - temperature is NOT modifiable for K2.7-code/K2.6; must omit it.
        - K2.7-code thinking is always on (cannot disable).

The K2 pattern also matches K3 ids, so K2 explicitly excludes them. In
the previous cascade this was enforced only by `is_kimi` sitting eight
lines above `is_chinese_m` in an elif chain.
"""
from __future__ import annotations

from typing import Any, Dict

from app.compat.families._base import Contract, Provenance, ThinkingContext

SOURCE = "families/kimi.py"

_K3_RE = r"kimi-k3"
# K3 official docs: only "low", "high", "max" (default "max"). NO "medium".
_K3_EFFORT_WORDS = ("low", "high", "max")

# Sampling parameters K3 rejects.
_FORBIDDEN_SAMPLING = ("temperature", "top_p", "presence_penalty", "frequency_penalty", "n")

# Sampling parameters K2.7-code/K2.6 docs say are not modifiable.
_K2_FORBIDDEN_SAMPLING = ("temperature", "top_p")


def _k3_effort(ctx: ThinkingContext) -> str:
    return ctx.effort if ctx.effort in _K3_EFFORT_WORDS else "max"


def _apply_k3(
    payload: Dict[str, Any],
    ctx: ThinkingContext,
    prov: Provenance,
    contract: Contract,
) -> Dict[str, Any]:
    return prov.apply(
        payload,
        contract,
        "reasoning_effort",
        {"reasoning_effort": _k3_effort(ctx), "thinking": None},
    )


def _sanitize_k3(
    payload: Dict[str, Any],
    ctx: ThinkingContext,
    prov: Provenance,
    contract: Contract,
) -> Dict[str, Any]:
    """Unconditional — K3 rejects these regardless of thinking setting."""
    removals: Dict[str, Any] = {k: None for k in _FORBIDDEN_SAMPLING}
    removals["thinking"] = None
    payload = prov.apply(payload, contract, "strip_sampling", removals)

    existing = payload.get("reasoning_effort")
    if isinstance(existing, str) and existing.lower() in _K3_EFFORT_WORDS:
        # Valid K3 effort already present — leave it.
        return payload
    if existing is not None and not (isinstance(existing, str) and existing.lower() in ("off", "auto", "enable")):
        # Invalid word present (e.g. legacy 'medium') — coerce into K3 vocab.
        return prov.apply(
            payload,
            contract,
            "coerce_effort",
            {"reasoning_effort": _k3_effort(ctx)},
        )
    # Missing, or a non-effort word (off/auto/enable) that K3 would reject.
    removals2 = {"reasoning_effort": None} if existing is not None else {}
    if removals2:
        payload = prov.apply(payload, contract, "strip_invalid_effort", removals2)
    return prov.apply(
        payload,
        contract,
        "require_effort",
        {"reasoning_effort": _k3_effort(ctx)},
    )


def _apply_k2(
    payload: Dict[str, Any],
    ctx: ThinkingContext,
    prov: Provenance,
    contract: Contract,
) -> Dict[str, Any]:
    return prov.apply(
        payload,
        contract,
        "enable_thinking_bool",
        {"enable_thinking": True, "thinking": None},
    )


def _sanitize_k2(
    payload: Dict[str, Any],
    ctx: ThinkingContext,
    prov: Provenance,
    contract: Contract,
) -> Dict[str, Any]:
    """K2.7-code/K2.6 docs: temperature is not modifiable — strip it."""
    removals: Dict[str, Any] = {k: None for k in _K2_FORBIDDEN_SAMPLING}
    return prov.apply(payload, contract, "strip_sampling_k2", removals)


CONTRACTS = [
    Contract(
        id="kimi-k3",
        source=SOURCE,
        priority=55,
        pattern=_K3_RE,
        apply=_apply_k3,
        sanitize=_sanitize_k3,
    ),
    Contract(
        id="kimi-k2",
        source=SOURCE,
        priority=45,
        pattern=r"kimi|k2\.|moonshot",
        exclude=_K3_RE,
        apply=_apply_k2,
        sanitize=_sanitize_k2,
    ),
]
