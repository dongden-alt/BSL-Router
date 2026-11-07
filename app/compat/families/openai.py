"""
OpenAI family contracts.

GPT-5.x / GPT-6 (Astra) use a top-level `reasoning_effort` for /chat/completions,
with nested reasoning.mode / reasoning.context as best-effort extras for
reseller channels. effort="auto" is never emitted at either level. gpt-6-astra
carries no mode/context config keys, so those extras never fire for it, and
budget-style thinking values (e.g. "32k") coerce to a level before emission.

`always_applies=True` because explicit reasoning_mode/reasoning_context
metadata must still be sent when no effort level is selected.

Transport note (2026-08-20): on an Anthropic-compatible wire
(`wire_format=anthropic`), OpenAI-style `reasoning_effort` / `reasoning`
must NOT be emitted. Live AgentRouter probe for gpt-5.6-sol:
  - /v1/messages plain body -> content "OK"
  - /v1/messages + reasoning_effort/mode -> stalls after message_start (empty)
  - /v1/chat/completions + reasoning_effort/mode -> content "OK"
So on the anthropic wire the contract strips foreign keys and is a no-op.

Tools note (2026-09-30): a payload carrying a non-empty `tools` list gets
NO reasoning_effort/reasoning at all for gpt-6-astra — AgentRouter's
gpt-6-astra lane 400s on reasoning_effort + function tools in
/v1/chat/completions ("Function tools with reasoning_effort are not
supported ... use /v1/responses or set reasoning_effort to 'none'").
Astra has no "off" (the model thinks by default), so omitting the effort
on tool-carrying requests is safe. For AgentRouter gpt-5.x (gpt-5.6-sol),
omitting reasoning_effort still triggers the 400 because the server
defaults effort ON unless reasoning_effort="none" is explicitly set.
"""
from __future__ import annotations

import re
from typing import Any, Dict

from app.compat.adapters.gemini import bound_tool_call_id
from app.compat.families._base import (
    Contract,
    Provenance,
    ProvenanceRecord,
    ThinkingContext,
)
from app.compat.families._effort import apply_gpt5_reasoning_controls, coerce_effort

SOURCE = "families/openai.py"

# OpenAI-native reasoning keys that Anthropic-compatible gateways reject
# or silently mishandle when a GPT model is served over /v1/messages.
_OPENAI_REASONING_KEYS = (
    "reasoning_effort",
    "reasoning",
    "output_config",
    "thinking",
)

_AGENTROUTER_FAMILY_RE = re.compile(r"^agentrouter(?:[-_][a-z0-9_]+)?(?:/|$)", re.IGNORECASE)
_AGENTROUTER_GPT_SOL_OR_61_RE = re.compile(r"gpt-?(?:5(?:\.\d+)?|6\.1)\b", re.IGNORECASE)


def _has_function_tools(payload: Dict[str, Any]) -> bool:
    """Return True iff payload carries at least one OpenAI function tool definition."""
    tools = payload.get("tools")
    if isinstance(tools, list) and tools:
        for t in tools:
            if isinstance(t, dict) and (
                t.get("type") == "function" or isinstance(t.get("function"), dict)
            ):
                return True
    functions = payload.get("functions")
    if isinstance(functions, list) and any(isinstance(f, dict) for f in functions):
        return True
    return False


def _is_agentrouter_gpt_sol_or_61(f_val: str, model: str = "") -> bool:
    """True for AgentRouter-family lanes serving GPT-5.x or GPT-6.1 (excludes gpt-6-astra)."""
    f_low = str(f_val or "").strip().lower()
    if not _AGENTROUTER_FAMILY_RE.match(f_low):
        return False
    combined = f"{f_low} {str(model or '').strip().lower()}"
    if "astra" in combined:
        return False
    return bool(_AGENTROUTER_GPT_SOL_OR_61_RE.search(combined))


def _apply_gpt5(
    payload: Dict[str, Any],
    ctx: ThinkingContext,
    prov: Provenance,
    contract: Contract,
) -> Dict[str, Any]:
    # Codex Responses-wire sanitize (2026-10-19): when codex_adapter has
    # already converted the egress body to the Responses shape ({input,
    # model, store, stream, reasoning:{effort}}), the generic OpenAI
    # controls below would pollute it — chatgpt.com codex/responses 400s
    # on top-level `reasoning_effort` ("Unsupported parameter") and on
    # `reasoning.mode="pro"` ("reasoning.mode is not supported with this
    # model"). Live probes: context="all_turns" + mode="standard" -> 200;
    # mode="pro" -> 400. The adapter already wrote reasoning.effort, so
    # the ONLY job here is to remove the forbidden keys and return —
    # skip every OpenAI control below. effort and context inside
    # reasoning are left untouched.
    if "input" in payload and "messages" not in payload:
        strip_vals: Dict[str, Any] = {}
        if "reasoning_effort" in payload:
            strip_vals["reasoning_effort"] = None
        reasoning = payload.get("reasoning")
        mode_popped = False
        if isinstance(reasoning, dict) and reasoning.get("mode") == "pro":
            reasoning.pop("mode", None)
            mode_popped = True
        inp_list = payload.get("input")
        if isinstance(inp_list, list):
            for _item in inp_list:
                if isinstance(_item, dict):
                    _cid = _item.get("call_id")
                    if isinstance(_cid, str) and len(_cid) > 64:
                        _item["call_id"] = bound_tool_call_id(_cid)
        n_before = len(prov.records)
        payload = prov.apply(
            payload, contract, "codex_responses_wire_sanitize", strip_vals
        )
        if len(prov.records) > n_before:
            if mode_popped:
                # The nested mode pop is not expressible as a top-level
                # key removal in Provenance.apply; record it on the
                # rule's fields list as "-reasoning.mode".
                prov.records[-1].fields.append("-reasoning.mode")
        else:
            # Nothing to strip (clean adapter body) — still attribute the
            # sanitize so the request log names the rule that fired.
            prov.records.append(
                ProvenanceRecord(
                    contract_id=contract.id,
                    source=contract.source,
                    rule="codex_responses_wire_sanitize",
                    fields=["-reasoning.mode"] if mode_popped else [],
                )
            )
        return payload

    # Anthropic wire: never emit OpenAI reasoning controls. Reseller
    # gateways that re-expose GPT models on /v1/messages stall empty when
    # these fields arrive (AgentRouter gpt-5.6-sol, 2026-08-20).
    if ctx.wire_format == "anthropic":
        strip_vals = {k: None for k in _OPENAI_REASONING_KEYS if k in payload}
        if strip_vals:
            return prov.apply(
                payload, contract, "anthropic_wire_strip_openai_reasoning", strip_vals
            )
        return payload

    # Function tools + reasoning_effort (2026-09-30, AgentRouter evidence B):
    # gpt-6-astra on /v1/chat/completions 400s with "Function tools with
    # reasoning_effort are not supported ... set reasoning_effort to
    # 'none'". Astra has no "off" — it thinks by default — so on a
    # tool-carrying request suppress the OpenAI reasoning controls
    # entirely (strip any pre-existing keys, emit nothing new); the
    # model's default effort applies.
    # For AgentRouter gpt-5.x / gpt-6.1 (e.g. agentrouter/gpt-5.6-sol,
    # agentrouter/gpt-6.1-sol), omitting reasoning_effort still 400s when
    # function tools are present because the upstream defaults reasoning on;
    # explicitly set reasoning_effort="none" and strip nested reasoning.
    if _has_function_tools(payload):
        if _is_agentrouter_gpt_sol_or_61(ctx.f_val or "", str(payload.get("model") or "")):
            sol_vals: Dict[str, Any] = {"reasoning_effort": "none"}
            if "reasoning" in payload:
                sol_vals["reasoning"] = None
            return prov.apply(payload, contract, "tools_reasoning_strip", sol_vals)
        strip_vals = {k: None for k in ("reasoning_effort", "reasoning") if k in payload}
        if strip_vals:
            return prov.apply(payload, contract, "tools_reasoning_strip", strip_vals)
        return payload

    before_effort = payload.get("reasoning_effort")
    before_reasoning = payload.get("reasoning")

    # Budget-style thinking values (e.g. "32k") coerce to a level here so an
    # invalid effort never reaches the wire; real effort words pass through
    # unchanged and auto stays auto (emits nothing).
    payload = apply_gpt5_reasoning_controls(
        payload, coerce_effort(ctx.effort), ctx.reasoning_mode, ctx.reasoning_context
    )

    # Attribute only what actually changed so the log stays truthful.
    changed: Dict[str, Any] = {}
    if payload.get("reasoning_effort") != before_effort:
        changed["reasoning_effort"] = payload.get("reasoning_effort")
    if payload.get("reasoning") != before_reasoning:
        changed["reasoning"] = payload.get("reasoning")
    if changed:
        prov.apply(payload, contract, "gpt5_reasoning_controls", changed)
    return payload


CONTRACTS = [
    Contract(
        id="gpt-5",
        source=SOURCE,
        priority=100,
        # [56] pulls gpt-6 (Astra) onto the same effort contract. Astra is
        # effort-only: no reasoning_mode/reasoning_context config keys exist
        # for it, so the mode/context extras below stay unset naturally.
        pattern=r"gpt-?[56]",
        apply=_apply_gpt5,
        # Metadata-only requests (mode/context without effort) must still
        # reach the upstream, so this contract opts out of the effort gate.
        always_applies=True,
    ),
]
