"""
Deliberate divergences from the legacy Engine B cascade.

test_family_characterization.py asserts "the refactor changed nothing".
This file is its counterpart: it asserts the places where the refactor
changed something ON PURPOSE, because the legacy behavior was a bug.

Every test here must state WHY the legacy behavior was wrong.

Run:
  .venv\\Scripts\\python -m pytest app/tests/test_family_divergences.py -q
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.compat.families import resolve_thinking


def _payload():
    return {"model": "x", "messages": [], "max_tokens": 8192}


def _tc(payload):
    """Extract generationConfig.thinkingConfig, or None."""
    gc = payload.get("generationConfig")
    if not isinstance(gc, dict):
        return None
    tc = gc.get("thinkingConfig")
    return tc if isinstance(tc, dict) else None


GEMINI_25 = "vsllm-g/gemini-2.5-pro"
GEMINI_3X = "vsllm-g/gemini-3.1-pro"


# ─────────────────────────────────────────────────────────────────────
# DIVERGENCE 1 — Gemini thinking is TRANSPORT-DEPENDENT.
#
# WHY LEGACY WAS WRONG (two independent bugs):
#
#   (a) WRONG SHAPE. Google's generateContent API takes thinking at
#       generationConfig.thinkingConfig.{thinkingBudget|thinkingLevel}.
#       Legacy emitted Anthropic-shaped keys at the TOP level:
#         2.5 -> {"thinking_config": {"budget_tokens": N}}
#         3.x -> {"thinkingLevel": "high"}
#       Google rejects unknown top-level fields, so native-gemini
#       requests 400'd and thinking never reached the model.
#
#   (b) TRANSPORT-BLIND. config.yaml serves the SAME Gemini models over
#       three transports (native gemini, openai-compatible, anthropic-
#       shaped). Legacy emitted one shape to all three, so at most one
#       could ever have been right.
# ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("effort,expected_budget", [
    ("16k", 16384),
    ("32k", 32768),
    ("64k", 65536),
    ("128k", 131072),
])
def test_gemini25_native_budget_words(effort, expected_budget):
    """2.5 over NATIVE transport: numeric budget, nested."""
    out, _ = resolve_thinking(_payload(), GEMINI_25, effort, wire_format="gemini")
    tc = _tc(out)
    assert tc is not None, f"no generationConfig.thinkingConfig. Got: {out}"
    assert tc["thinkingBudget"] == expected_budget


@pytest.mark.parametrize("effort,expected_budget", [
    ("low", 8192),
    ("medium", 24576),
    ("high", 32768),
    ("max", 65536),
])
def test_gemini25_native_effort_words_coerce_to_budget(effort, expected_budget):
    """DIVERGENCE: legacy dropped effort words for 2.5 entirely, so
    `thinking: high` on a 2.5 model was a silent no-op."""
    out, _ = resolve_thinking(_payload(), GEMINI_25, effort, wire_format="gemini")
    assert _tc(out)["thinkingBudget"] == expected_budget


@pytest.mark.parametrize("effort", ["enable", "adaptive"])
def test_gemini25_native_unknown_vocab_uses_dynamic(effort):
    """DIVERGENCE: unrecognised words now request Google's dynamic-thinking
    sentinel (-1) rather than sending nothing."""
    out, _ = resolve_thinking(_payload(), GEMINI_25, effort, wire_format="gemini")
    assert _tc(out)["thinkingBudget"] == -1


@pytest.mark.parametrize("effort", ["low", "medium", "high", "max"])
def test_gemini3x_native_uses_nested_level(effort):
    """3.x over NATIVE transport: enum level, nested — not top-level."""
    out, _ = resolve_thinking(_payload(), GEMINI_3X, effort, wire_format="gemini")
    tc = _tc(out)
    assert tc is not None, f"no generationConfig.thinkingConfig. Got: {out}"
    assert tc["thinkingLevel"] == effort
    assert tc["includeThoughts"] is True
    assert "thinkingLevel" not in out, "leaked to top level — the legacy bug"


# ── Transport correctness ────────────────────────────────────────────

@pytest.mark.parametrize("f_val", [GEMINI_25, GEMINI_3X])
def test_gemini_openai_transport_uses_reasoning_effort(f_val):
    """DIVERGENCE: over an OpenAI-compatible gateway the only field that
    means anything is reasoning_effort. Legacy sent generationConfig /
    thinking_config, which the gateway ignores — thinking never applied."""
    out, _ = resolve_thinking(_payload(), f_val, "high", wire_format="openai")
    assert out.get("reasoning_effort") == "high"
    assert "generationConfig" not in out
    assert "thinking_config" not in out


@pytest.mark.parametrize("f_val", [GEMINI_25, GEMINI_3X])
def test_gemini_anthropic_transport_uses_thinking_block(f_val):
    """Over an anthropic-shaped endpoint, thinking must be the Anthropic
    block — and must carry a numeric budget, not an enum."""
    out, _ = resolve_thinking(_payload(), f_val, "32k", wire_format="anthropic")
    assert out["thinking"]["type"] == "enabled"
    assert out["thinking"]["budget_tokens"] == 32768
    assert "generationConfig" not in out
    assert "reasoning_effort" not in out


@pytest.mark.parametrize("f_val", [GEMINI_25, GEMINI_3X])
@pytest.mark.parametrize("effort", ["high", "max", "32k", "enable"])
def test_native_transport_never_emits_foreign_keys(f_val, effort):
    """Root-cause guard: any non-Google top-level field is a 400."""
    out, _ = resolve_thinking(_payload(), f_val, effort, wire_format="gemini")
    for banned in ("thinking", "thinking_config", "reasoning_effort",
                   "reasoning", "output_config", "thinkingLevel"):
        assert banned not in out, (
            f"{f_val} effort={effort!r} emitted {banned!r} on the native "
            f"transport — Google will 400. Got: {out}"
        )


@pytest.mark.parametrize("wire,stale_key,stale_val", [
    ("gemini", "thinking", {"type": "enabled", "budget_tokens": 4096}),
    ("gemini", "reasoning_effort", "low"),
    ("openai", "generationConfig", {"thinkingConfig": {"thinkingBudget": 999}}),
    ("anthropic", "generationConfig", {"thinkingConfig": {"thinkingBudget": 999}}),
    ("anthropic", "reasoning_effort", "low"),
])
def test_stale_foreign_keys_are_stripped(wire, stale_key, stale_val):
    """If an upstream layer already attached a field belonging to a
    DIFFERENT transport, the contract strips it rather than forwarding."""
    payload = _payload()
    payload[stale_key] = stale_val

    out, prov = resolve_thinking(payload, GEMINI_3X, "high", wire_format=wire)
    assert stale_key not in out, f"{stale_key!r} survived on wire={wire}"
    assert prov.records, "strip happened with no provenance recorded"


@pytest.mark.parametrize("wire", ["gemini", "openai", "anthropic"])
@pytest.mark.parametrize("effort", ["off", "none", ""])
def test_thinking_off_emits_nothing_on_any_transport(wire, effort):
    """Disabled thinking must not create an empty container."""
    out, _ = resolve_thinking(_payload(), GEMINI_3X, effort, wire_format=wire)
    assert _tc(out) is None, f"wire={wire} effort={effort!r} emitted: {out}"
    for k in ("thinking", "reasoning_effort", "includeThoughts"):
        assert k not in out


def test_native_preserves_unrelated_generation_config():
    """The contract writes INTO generationConfig; it must not clobber
    sampling params a caller already placed there."""
    payload = _payload()
    payload["generationConfig"] = {"temperature": 0.7, "topP": 0.9}

    out, _ = resolve_thinking(payload, GEMINI_25, "32k", wire_format="gemini")
    gc = out["generationConfig"]
    assert gc["temperature"] == 0.7, "clobbered an existing generationConfig key"
    assert gc["topP"] == 0.9
    assert gc["thinkingConfig"]["thinkingBudget"] == 32768


def test_same_setting_differs_by_transport():
    """The point of the whole fix, stated as one assertion: one logical
    setting, three transports, three different correct payloads."""
    shapes = {
        wire: resolve_thinking(_payload(), GEMINI_3X, "high", wire_format=wire)[0]
        for wire in ("gemini", "openai", "anthropic")
    }
    assert _tc(shapes["gemini"])["thinkingLevel"] == "high"
    assert shapes["openai"]["reasoning_effort"] == "high"
    assert shapes["anthropic"]["thinking"]["type"] == "enabled"

    # And no two are the same payload.
    assert shapes["gemini"] != shapes["openai"] != shapes["anthropic"]


# ─────────────────────────────────────────────────────────────────────
# DIVERGENCE 2 — Kimi K2 strips temperature/top_p unconditionally.
#
# WHY LEGACY WAS WRONG:
#   The legacy cascade only stripped sampling parameters (temperature,
#   top_p, etc.) for K3 and Qwen — NOT for K2 models.  But Moonshot's
#   official docs state that K2.7-code and K2.6 do NOT allow temperature
#   modification; sending it causes unpredictable behavior or silent
#   override.  The new _sanitize_k2 strips temperature/top_p for ALL K2
#   models, matching the K3/Qwen sanitize pattern.
# ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("f_val", [
    "iamhc/kimi-k2.7-code",
    "iamhc/kimi-k2.6",
    "iamhc/kimi-k2.5",
    "moonshot/kimi-k2.7-code",
])
def test_k2_strips_forbidden_sampling_params(f_val):
    """K2.7-code/K2.6 docs: temperature is not modifiable — strip it.

    Legacy did NOT strip these for K2; the new contract does.  This test
    locks the deliberate divergence.
    """
    payload = {
        "model": "x", "messages": [], "max_tokens": 8192,
        "temperature": 0.7, "top_p": 0.9,
    }
    out, prov = resolve_thinking(payload, f_val, "off")
    assert "temperature" not in out, (
        f"{f_val}: temperature survived — would cause upstream rejection"
    )
    assert "top_p" not in out, (
        f"{f_val}: top_p survived — would cause upstream rejection"
    )
    assert prov.records, "K2 sanitize wrote with no provenance"


def test_k2_strips_sampling_even_when_thinking_enabled():
    """Sanitize is unconditional — must fire even when thinking is on."""
    payload = {
        "model": "x", "messages": [], "max_tokens": 8192,
        "temperature": 0.7, "top_p": 0.9,
    }
    out, _ = resolve_thinking(payload, "iamhc/kimi-k2.7-code", "max")
    assert "temperature" not in out
    assert "top_p" not in out
    assert out.get("enable_thinking") is True


# ─────────────────────────────────────────────────────────────────────
# DIVERGENCE 3 — qwencoder provider no longer matched as Qwen.
#
# WHY LEGACY WAS WRONG:
#   The legacy cascade used `re.search(r'qwen', f_val)` to detect Qwen
#   models.  But `f_val` is "provider/model" — so the PROVIDER name
#   `qwencoder` matched, and every model it served (claude-opus, gpt-5.6,
#   deepseek-v4, glm-5.2, etc.) got Qwen's sampling-param strip +
#   reasoning_effort injection.  Non-Qwen models received the wrong
#   reasoning shape, and sampling params were silently stripped from
#   models that accept them.  The new `qwen(?!coder)` pattern uses a
#   negative lookahead to exclude the provider name.
# ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("f_val", [
    "qwencoder/claude-opus-4.8",
    "qwencoder/gpt-5.6-sol",
    "qwencoder/deepseek-v4-pro",
    "qwencoder/glm-5.2",
    "qwencoder/minimax-m3",
])
def test_qwencoder_provider_not_matched_as_qwen(f_val):
    """The `qwencoder` provider must NOT trigger the Qwen contract.

    Legacy matched `qwen` in `qwencoder` and stripped sampling params +
    injected reasoning_effort for every model it served.  The new
    `qwen(?!coder)` pattern correctly excludes the provider name.
    """
    payload = {
        "model": "x", "messages": [], "max_tokens": 8192,
        "temperature": 0.7, "top_p": 0.9,
    }
    out, prov = resolve_thinking(payload, f_val, "high")

    # Temperature/top_p must SURVIVE — these are not Qwen models.
    assert "temperature" in out, (
        f"{f_val}: temperature was stripped by Qwen contract — "
        "qwencoder provider matched the old `r'qwen'` regex"
    )
    assert "top_p" in out, (
        f"{f_val}: top_p was stripped by Qwen contract — provider false match"
    )

    # Provenance must NOT include the qwen contract.
    contract_ids = {r.contract_id for r in prov.records}
    assert "qwen" not in contract_ids, (
        f"{f_val}: matched the qwen contract — provider-name false match. "
        f"Contracts: {contract_ids}"
    )


# ─────────────────────────────────────────────────────────────────────
# DIVERGENCE 4 — K3 rejects 'medium' effort; coerce to 'max'.
#
# WHY LEGACY WAS WRONG:
#   The legacy K3 branch accepted ("low", "medium", "high", "max") as
#   valid effort words.  But per official Moonshot docs (2026-08-02),
#   K3 only supports "low", "high", "max" (default "max").  Passing
#   "medium" causes a 400 Bad Request.  The new _sanitize_k3 coerces
#   stale 'medium' to 'max' and _k3_effort never emits 'medium'.
# ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("f_val", [
    "moonshot/kimi-k3",
    "vsllm-gpt/kimi-k3",
    "ltn-ai/kimi-k3",
    "ltn-ai/kimi-k3-fast",
    "openrouter/moonshotai/kimi-k3",
])
def test_k3_coerces_medium_to_max(f_val):
    """K3 rejects 'medium' with a 400 — coerce to 'max'.

    Legacy accepted 'medium' as valid for K3; the new code correctly
    coerces it to 'max' per Moonshot docs.
    """
    out, _ = resolve_thinking(_payload(), f_val, "medium")
    assert out["reasoning_effort"] == "max", (
        f"{f_val} effort=medium: expected 'max' (coerced), got "
        f"'{out['reasoning_effort']}' — K3 rejects 'medium' with a 400"
    )
    # 'medium' must NEVER survive in the payload.
    assert out["reasoning_effort"] != "medium"
