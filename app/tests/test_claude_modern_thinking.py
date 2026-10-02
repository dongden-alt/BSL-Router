"""Regression tests for the Claude-4 reasoning-contract router.

Operator policy (user, 2026-07-22): ONLY Opus 4.6 antigravity* SKUs use a 32k
thinking BUDGET; every other Opus/Sonnet uses a thinking LEVEL. This test locks
that split so a future edit can't silently coerce the antigravity budget into an
effort level (the earlier _coerce_effort blanket-fix bug) or force enabled+budget
onto a 4.7/4.8 SKU that rejects it with a 400.

AgentRouter addendum (2026-09-30): on agentrouter* routes the modern contract
is adaptive thinking ONLY — output_config is never co-emitted. The relay's
Bedrock lane (InvokeModelWithResponseStream) 400s with a ValidationException
when `thinking` AND `output_config` ride the same request; the lane accepts
one or the other, exactly like the budget path. Non-agentrouter providers
keep the adaptive+effort shape.

Run: .venv\\Scripts\\python -m pytest app/tests/test_claude_modern_thinking.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.main import _claude_modern_thinking, _budget_tokens
from app.compat.families import resolve_thinking


# ── _budget_tokens ──────────────────────────────────────────────────────
def test_budget_tokens_detects_budget_forms():
    assert _budget_tokens("32k") == 32768
    assert _budget_tokens("32768") == 32768
    assert _budget_tokens("16k") == 16384
    assert _budget_tokens("64k") == 65536


def test_budget_tokens_rejects_levels_and_tiny_ints():
    for level in ("max", "high", "medium", "low", "xhigh", "adaptive", "enabled", "", None):
        assert _budget_tokens(level) is None
    # A bare tiny integer is NOT a budget.
    assert _budget_tokens("2") is None
    assert _budget_tokens("512") is None


# ── Opus 4.6 antigravity* → budget contract ─────────────────────────────
def test_opus_46_antigravity_uses_budget():
    think, effort, budget = _claude_modern_thinking("vsllm-a/claude-opus-4-6-antigravity", "32k")
    assert think == {"type": "enabled", "budget_tokens": 32768}
    assert effort is None
    assert budget == 32768


def test_opus_46_antigravity_ultra_uses_budget():
    think, effort, budget = _claude_modern_thinking(
        "vsllm-a/claude-opus-4-6-antigravity-ultra", "32k"
    )
    assert think == {"type": "enabled", "budget_tokens": 32768}
    assert budget == 32768
    assert "output_config" not in str(effort)  # no effort level emitted


def test_opus_46_antigravity_dotted_name_also_budget():
    # The dotted antigravity id must match the same 4[.-]6 + antigravity gate.
    think, effort, budget = _claude_modern_thinking(
        "vsllm-a/claude-opus-4.6-antigravity", "32k"
    )
    assert think["type"] == "enabled"
    assert budget == 32768


def test_non_antigravity_opus_46_stays_on_level_even_with_32k():
    # POLICY: ONLY opus-4.6-antigravity* uses the budget. A plain opus-4.6
    # configured with thinking:32k must be coerced to a LEVEL, not a budget.
    think, effort, budget = _claude_modern_thinking("vietapi-a/claude-opus-4.6", "32k")
    assert think == {"type": "adaptive"}
    assert budget is None
    assert effort == "max"  # 32k coerced to a level


# ── Every other Opus/Sonnet → level contract ────────────────────────────
def test_opus_48_uses_effort_level_not_budget():
    think, effort, budget = _claude_modern_thinking("pix4k/claude-opus-4.8-thinking", "max")
    assert think == {"type": "adaptive"}
    assert effort == "max"
    assert budget is None


def test_sonnet_uses_effort_level():
    think, effort, budget = _claude_modern_thinking("pix4k/claude-sonnet-5-thinking", "high")
    assert think == {"type": "adaptive"}
    assert effort == "high"
    assert budget is None


def test_opus_47_never_forced_to_budget_even_if_misconfigured_32k():
    # Safety gate: a mis-set 32k on a 4.7 SKU must NOT produce enabled+budget
    # (which 4.7/4.8 reject with 400). It stays adaptive, and 32k is coerced
    # to a valid level.
    think, effort, budget = _claude_modern_thinking("vsllm-a/claude-opus-4-7", "32k")
    assert think == {"type": "adaptive"}
    assert budget is None
    assert effort == "max"  # 32k coerced to a level, never leaked raw


def test_opus_46_with_level_value_stays_on_level():
    # Non-antigravity opus-4.6 configured with thinking:max (see vsllm-a
    # claude-opus-4-6) must use the level path, not a budget.
    think, effort, budget = _claude_modern_thinking("vsllm-a/claude-opus-4-6", "max")
    assert think == {"type": "adaptive"}
    assert effort == "max"
    assert budget is None


# ── AgentRouter* routes: adaptive thinking ONLY (2026-09-30) ────────────
# Live evidence: [BSL][18d9f9d3c0] END status=400 provider=agentrouter
# model=claude-opus-5 error="InvokeModelWithResponseStream: ... StatusCode:
# 400, ValidationException" with thinking=effort:max resolved via the
# adaptive_effort rule that co-emitted BOTH thinking and output_config.
# Bedrock accepts one or the other — the budget path in the same function
# already documents "drop output_config (the two are mutually exclusive)".
#
# The gate is scoped to the two LIVE agentrouter egress wires: anthropic
# (provider `agentrouter`, where the ValidationException was observed) and
# openai-responses (sibling `agentrouter-o`, same agentrouter.org Bedrock
# upstream). No live agentrouter route uses the bare openai wire.
_AR_IDS = [
    "agentrouter/claude-opus-5",
    "agentrouter/claude-opus-4-8",
    "agentrouter-o/claude-opus-5",
]
_AR_WIRES = ("anthropic", "openai-responses")


def _ar_payload():
    return {"model": "x", "messages": [], "max_tokens": 8192}


def test_agentrouter_adaptive_emits_thinking_only():
    for f_val in _AR_IDS:
        for wire in _AR_WIRES:
            out, prov = resolve_thinking(_ar_payload(), f_val, "max", wire_format=wire)
            assert out.get("thinking") == {"type": "adaptive"}, f"{f_val}/{wire}: {out}"
            assert "output_config" not in out, (
                f"{f_val}/{wire}: thinking + output_config co-emitted — Bedrock "
                f"ValidationException (2026-09-30): {out}"
            )
            assert prov.records, f"{f_val}/{wire}: no provenance"
            assert any(r.rule == "adaptive_thinking_only" for r in prov.records), (
                f"{f_val}/{wire}: {prov.summary()}"
            )


def test_agentrouter_drops_preexisting_output_config():
    """A stale/inherited output_config on the payload must be removed too —
    the None-value deletion convention, same as the anthropic-wire strip."""
    payload = _ar_payload()
    payload["output_config"] = {"effort": "high"}  # inherited stale field
    out, prov = resolve_thinking(payload, "agentrouter/claude-opus-5", "max",
                                 wire_format="anthropic")
    assert out.get("thinking") == {"type": "adaptive"}
    assert "output_config" not in out
    rec = next(r for r in prov.records if r.rule == "adaptive_thinking_only")
    assert "-output_config" in rec.fields, rec.fields


def test_agentrouter_openai_wire_keeps_legacy_shape():
    """No live agentrouter route uses the bare openai wire (the two live
    lanes are anthropic and openai-responses), and the legacy-parity
    characterization sweep pins openai-wire resolution to the legacy
    adaptive+effort shape — the strip must NOT fire there."""
    out, prov = resolve_thinking(_ar_payload(), "agentrouter/claude-opus-5", "max",
                                 wire_format="openai")
    assert out.get("thinking") == {"type": "adaptive"}
    assert out.get("output_config") == {"effort": "max"}
    assert any(r.rule == "adaptive_effort" for r in prov.records), prov.summary()


def test_agentrouter_budget_path_untouched():
    """Opus 4.6 antigravity + 32k on an agentrouter route still takes the
    budget contract (enabled + budget_tokens + output_config removed) — the
    agentrouter gate must not disturb the budget branch."""
    out, _ = resolve_thinking(
        _ar_payload(), "agentrouter/claude-opus-4-6-antigravity", "32k",
        wire_format="anthropic",
    )
    assert out["thinking"] == {"type": "enabled", "budget_tokens": 32768}
    assert "output_config" not in out


def test_non_agentrouter_keeps_adaptive_plus_effort():
    """The strip is agentrouter-gated: every other provider keeps the
    adaptive + output_config.effort shape (conformance rows lock it)."""
    out, prov = resolve_thinking(_ar_payload(), "pix4k/claude-opus-4.8-thinking", "max",
                                 wire_format="anthropic")
    assert out.get("thinking") == {"type": "adaptive"}
    assert out.get("output_config") == {"effort": "max"}
    assert any(r.rule == "adaptive_effort" for r in prov.records), prov.summary()
