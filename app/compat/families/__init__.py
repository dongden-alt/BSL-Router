"""
BSL Router — Family Contract Registry (single reasoning writer).

This package replaces the 220-line regex cascade formerly at
app/main.py:4283-4535 (Engine B) and the parallel, always-overwritten
path in app/compat/reasoning_policy.py (Engine A).

ONE resolver, `resolve_thinking`, owns every field in
THINKING_PAYLOAD_KEYS. It:

  1. Finds all contracts whose detector matches (each contract owns its
     own regex + priority; no elif ordering).
  2. Runs the highest-priority contract's `apply` to write thinking
     fields — but only when the operator selected a real thinking level
     (or the contract is `always_applies`, e.g. GPT-5 metadata).
  3. Runs `sanitize` for EVERY matching contract that defines one, even
     when thinking is off. This preserves the legacy behavior where the
     Kimi-K3 and Qwen sampling-parameter strips were unconditional and
     independent of which apply branch fired.
  4. Records provenance for every write, so the request log names the
     contract + rule + fields that produced the payload.

Adding a new model = add a Contract row to the relevant family module,
or add a new family module (auto-discovered below). No cascade edits.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from app.compat.families._base import (
    Contract,
    Provenance,
    ThinkingContext,
    THINKING_PAYLOAD_KEYS,
)
from app.compat.families import (
    openai as _openai,
    openrouter as _openrouter,
    gemini as _gemini,
    anthropic as _anthropic,
    grok as _grok,
    deepseek as _deepseek,
    kimi as _kimi,
    qwen as _qwen,
    glm as _glm,
    minimax as _minimax,
)

# Registry assembly. Order here is irrelevant — resolution is by the
# explicit `priority` on each Contract — but we list families in the same
# sequence as the legacy cascade for readability.
_FAMILY_MODULES = [
    _openai,
    _openrouter,
    _gemini,
    _anthropic,
    _grok,
    _deepseek,
    _kimi,
    _qwen,
    _glm,
    _minimax,
]

CONTRACTS: List[Contract] = []
for _mod in _FAMILY_MODULES:
    CONTRACTS.extend(_mod.CONTRACTS)

# Sanity: contract ids must be unique so logs are unambiguous.
_seen: Dict[str, str] = {}
for _c in CONTRACTS:
    if _c.id in _seen:
        raise ValueError(
            f"Duplicate contract id '{_c.id}' in {_c.source} and {_seen[_c.id]}"
        )
    _seen[_c.id] = _c.source


def _matching(ctx: ThinkingContext) -> List[Contract]:
    return [c for c in CONTRACTS if c.matches(ctx)]


def resolve_thinking(
    upstream_payload: Dict[str, Any],
    f_val: str,
    thinking_suffix: str,
    reasoning_mode: Optional[str] = None,
    reasoning_context: Optional[str] = None,
    wire_format: str = "openai",
) -> Tuple[Dict[str, Any], Provenance]:
    """Single entry point for reasoning-field resolution.

    `wire_format` is the upstream transport ("openai" | "anthropic" |
    "gemini" | "openai-responses"). Contracts whose payload shape depends
    on the transport read it from the context; the rest ignore it.

    Returns (payload, provenance). Payload is mutated in place and also
    returned for call-site parity with the legacy cascade.
    """
    ctx = ThinkingContext(
        f_val=(f_val or "").lower(),
        effort=str(thinking_suffix or "auto").lower(),
        reasoning_mode=reasoning_mode,
        reasoning_context=reasoning_context,
        wire_format=str(wire_format or "openai").lower(),
    )
    prov = Provenance()

    matches = _matching(ctx)
    if not matches:
        return upstream_payload, prov

    matches.sort(key=lambda c: c.priority, reverse=True)
    winner = matches[0]

    # Gate mirrors legacy: apply runs when a real effort is selected, or
    # when the winning contract always applies (GPT-5 metadata path).
    if ctx.effort_is_explicit or winner.always_applies:
        if winner.apply is not None:
            upstream_payload = winner.apply(upstream_payload, ctx, prov, winner)

    # Sanitize is unconditional and per-contract, independent of the apply
    # winner — faithful to the legacy Kimi-K3 / Qwen strip blocks.
    for c in matches:
        if c.sanitize is not None:
            upstream_payload = c.sanitize(upstream_payload, ctx, prov, c)

    return upstream_payload, prov


def matches_contract(f_val: str, contract_id: str) -> bool:
    """True if `f_val` resolves to the given contract id.

    Exists so transport-level concerns elsewhere (e.g. Qwen's 65535
    max_tokens hard cap) can ask the registry instead of re-declaring a
    family regex locally — the duplication that let detection drift
    between modules before this refactor.
    """
    ctx = ThinkingContext(f_val=(f_val or "").lower())
    return any(c.id == contract_id and c.matches(ctx) for c in CONTRACTS)


__all__ = [
    "resolve_thinking",
    "matches_contract",
    "CONTRACTS",
    "THINKING_PAYLOAD_KEYS",
]
