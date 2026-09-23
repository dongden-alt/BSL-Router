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


# ═══════════════════════════════════════════════════════════════════════════════
# DIVERGENCE 5 — Doubao/Hunyuan/Muse OFF-path behavior (moved 2026-08-20).
#
# WHY LEGACY WAS WRONG:
#   The legacy cascade (line 81) returned the payload UNCHANGED for
#   thinking_suffix in ("auto", "none", "off", "") — i.e., it did NOT emit
#   any reasoning/thinking field.  This left the model's default behavior
#   in effect, which may or may not be what the user intended.
#
# CURRENT (official 2026-08-20 Muse split):
#   Doubao: auto/off/none/'' -> reasoning_effort=minimal (unchanged).
#   Hunyuan: auto/off/none/'' -> chat_template_kwargs.reasoning_effort=no_think.
#   Muse 1.1: auto/'' -> thinking adaptive only (model default depth);
#             none/off -> adaptive + output_config.effort=low (off unsupported).
#   Muse 1.2: auto/'' -> OMIT reasoning_effort (model default xhigh);
#             none/off -> reasoning_effort=minimal (none is HTTP 400 upstream).
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("effort", ["auto", "none", "off", ""])
@pytest.mark.parametrize("f_val", [
    "vsllm-a/doubao-seed-2-0-pro",
    "vsllm-gpt/doubao-seed-2-0-pro",
    "iamhc/hy3",
    "ltn-ai/tencent/hy3",
    "a6api/hy3",
])
def test_doubao_hunyuan_auto_explicit_disable(f_val, effort):
    """auto/off/none/'' -> explicit disable (minimal/no_think), not legacy no-op."""
    out, prov = resolve_thinking(_payload(), f_val, effort)

    if "doubao" in f_val:
        assert out.get("reasoning_effort") == "minimal", f"{f_val} effort={effort!r}: expected minimal"
    elif "hy3" in f_val or "hunyuan" in f_val:
        # Hunyuan uses chat_template_kwargs.reasoning_effort
        chat_kwargs = out.get("chat_template_kwargs", {})
        assert chat_kwargs.get("reasoning_effort") == "no_think", f"{f_val} effort={effort!r}: expected no_think"

    # Provenance recorded
    assert prov.records, f"{f_val} effort={effort!r}: no provenance for explicit disable"


@pytest.mark.parametrize("effort", ["auto", ""])
@pytest.mark.parametrize("f_val", [
    "ltn-ai/meta/muse-spark-1.1",
])
def test_muse11_auto_adaptive_no_output_config(f_val, effort):
    """Muse 1.1 auto/'' -> thinking adaptive, no output_config (model default depth)."""
    out, prov = resolve_thinking(_payload(), f_val, effort)
    assert out.get("thinking") == {"type": "adaptive"}
    assert "output_config" not in out
    assert "reasoning_effort" not in out
    assert prov.records


@pytest.mark.parametrize("effort", ["none", "off"])
@pytest.mark.parametrize("f_val", [
    "ltn-ai/meta/muse-spark-1.1",
])
def test_muse11_off_to_low(f_val, effort):
    """Muse 1.1 none/off -> adaptive + output_config.effort=low (disabling unsupported)."""
    out, prov = resolve_thinking(_payload(), f_val, effort)
    assert out.get("thinking") == {"type": "adaptive"}
    assert out.get("output_config") == {"effort": "low"}
    assert any(r.rule == "off_coerced_to_low" for r in prov.records)


@pytest.mark.parametrize("effort", ["auto", ""])
@pytest.mark.parametrize("f_val", [
    "ltn-ai/meta/muse-spark-1.2",
    "ltn-ai/meta/muse-spark-1.2-contributor",
])
def test_muse12_auto_omits_effort(f_val, effort):
    """Muse 1.2 auto/'' -> omit reasoning_effort (model default xhigh)."""
    out, _prov = resolve_thinking(_payload(), f_val, effort)
    assert "reasoning_effort" not in out


@pytest.mark.parametrize("effort", ["none", "off"])
@pytest.mark.parametrize("f_val", [
    "ltn-ai/meta/muse-spark-1.2",
    "ltn-ai/meta/muse-spark-1.2-contributor",
])
def test_muse12_off_to_minimal(f_val, effort):
    """Muse 1.2 none/off -> reasoning_effort=minimal (400 avoidance)."""
    out, prov = resolve_thinking(_payload(), f_val, effort)
    assert out.get("reasoning_effort") == "minimal"
    assert any(r.rule == "off_coerced_to_minimal" for r in prov.records)


# ═══════════════════════════════════════════════════════════════════════════════
# DIVERGENCE 5 (continued) — Explicit effort coercion for new families.
#
# WHY LEGACY WAS WRONG:
#   The legacy cascade had ZERO branches for Doubao/Hunyuan/Kat-coder/Muse,
#   so any explicit non-OFF effort was a silent no-op (payload returned
#   unchanged).  New contracts now coerce each family's native vocabulary.
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("f_val,effort,expected", [
    ("vsllm-a/doubao-seed-2-0-pro", "high", "high"),
    ("vsllm-a/doubao-seed-2-0-pro", "max", "high"),
    ("vsllm-a/doubao-seed-2-0-pro", "enable", "high"),
    ("vsllm-a/doubao-seed-2-0-pro", "low", "low"),
])
def test_doubao_explicit_effort_coerces(f_val, effort, expected):
    out, prov = resolve_thinking(_payload(), f_val, effort)
    assert out.get("reasoning_effort") == expected
    assert prov.records


@pytest.mark.parametrize("f_val,effort,expected", [
    ("iamhc/hy3", "low", "low"),
    ("iamhc/hy3", "medium", "high"),
    ("iamhc/hy3", "max", "high"),
    ("ltn-ai/tencent/hy3", "high", "high"),
])
def test_hunyuan_explicit_effort_coerces(f_val, effort, expected):
    out, prov = resolve_thinking(_payload(), f_val, effort)
    assert out["chat_template_kwargs"]["reasoning_effort"] == expected
    assert prov.records


# Moved 2026-08-20: Muse 1.1 no longer emits top-level reasoning_effort.
# Explicit depths land in output_config.effort; enable keeps adaptive only.
@pytest.mark.parametrize("f_val,effort,expected_oc", [
    ("ltn-ai/meta/muse-spark-1.1", "high", "high"),
    ("ltn-ai/meta/muse-spark-1.1", "max", "xhigh"),
    ("ltn-ai/meta/muse-spark-1.1", "xhigh", "xhigh"),
])
def test_muse11_explicit_effort_via_output_config(f_val, effort, expected_oc):
    out, prov = resolve_thinking(_payload(), f_val, effort)
    assert out.get("thinking") == {"type": "adaptive"}
    assert out.get("output_config") == {"effort": expected_oc}
    assert "reasoning_effort" not in out
    assert prov.records


def test_muse11_enable_adaptive_only_no_output_config():
    out, prov = resolve_thinking(_payload(), "ltn-ai/meta/muse-spark-1.1", "enable")
    assert out.get("thinking") == {"type": "adaptive"}
    assert "output_config" not in out
    assert "reasoning_effort" not in out
    assert prov.records


@pytest.mark.parametrize("f_val,effort,expected", [
    ("ltn-ai/meta/muse-spark-1.2", "high", "high"),
    ("ltn-ai/meta/muse-spark-1.2", "max", "xhigh"),
    ("ltn-ai/meta/muse-spark-1.2", "xhigh", "xhigh"),
    ("ltn-ai/meta/muse-spark-1.2", "ultra", "ultra"),
])
def test_muse12_explicit_effort_coerces(f_val, effort, expected):
    out, prov = resolve_thinking(_payload(), f_val, effort)
    assert out.get("reasoning_effort") == expected
    assert prov.records


@pytest.mark.parametrize("f_val", [
    "iamhc/kat-coder-pro-v2.5",
    "ltn-ai/kwaipilot/kat-coder-pro-v2.5",
])
@pytest.mark.parametrize("effort", ["enable", "high", "32k"])
def test_kat_coder_passthrough(f_val, effort):
    out, prov = resolve_thinking(_payload(), f_val, effort)
    assert out.get("reasoning_effort") == effort
    assert prov.records


# ═══════════════════════════════════════════════════════════════════════════════
# DIVERGENCE 6 — GLM-5.2/5.3 graded path is VERSION-SPLIT (official 2026-08-20).
#
# WHY LEGACY WAS WRONG:
#   The legacy cascade did NOT branch on GLM-5.2 vs 5.1: it emitted
#   output_config.effort=<raw effort> for every GLM model.  So
#   medium/xhigh reached upstream unchanged — both are rejected with a
#   400 by GLM-5.2/5.3.  The new contract detects graded versions
#   (glm-5.[23]) and applies official per-version mappings:
#     5.3: none/minimal/low->low; medium/high->high; xhigh/max->max
#          (ONLY low/high/max accepted upstream)
#     5.2: none/minimal -> thinking disabled (no reasoning_effort);
#          low/medium->high; xhigh->max; high/max pass as high/max
#   emits reasoning_effort (when on) instead of output_config.effort.
#   Full lock: test_official_thinking_parity.py.
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("f_val", ["iamhc/glm-5.3", "hcnsec-vip/glm-5.3-anthropic"])
@pytest.mark.parametrize("effort,expected", [
    ("low", "low"), ("medium", "high"), ("high", "high"),
    ("max", "max"), ("xhigh", "max"), ("none", "low"), ("minimal", "low"),
])
def test_glm53_graded_coerces_and_uses_reasoning_effort(f_val, effort, expected):
    """GLM-5.3 maps into the three-word vocab and uses reasoning_effort."""
    out, prov = resolve_thinking(_payload(), f_val, effort)
    assert out["thinking"] == {"type": "enabled"}
    assert out["reasoning_effort"] == expected
    assert out["reasoning_effort"] in ("low", "high", "max")
    assert "output_config" not in out


@pytest.mark.parametrize("effort,expected", [
    ("low", "high"), ("medium", "high"), ("high", "high"),
    ("max", "max"), ("xhigh", "max"),
])
def test_glm52_graded_on_thinking_coerces_and_uses_reasoning_effort(effort, expected):
    """GLM-5.2 on-thinking path: low/medium->high, xhigh->max."""
    out, _ = resolve_thinking(_payload(), "iamhc/glm-5.2", effort)
    assert out["thinking"] == {"type": "enabled"}
    assert out["reasoning_effort"] == expected
    assert "output_config" not in out


@pytest.mark.parametrize("effort", ["none", "minimal"])
def test_glm52_none_minimal_disables_thinking(effort):
    """GLM-5.2 none/minimal stop thinking (official: model stops thinking)."""
    out, _ = resolve_thinking(_payload(), "iamhc/glm-5.2", effort)
    assert out["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in out


# ═══════════════════════════════════════════════════════════════════════════════
# Divergence — Fable/Mythos 5.1 (claude-next-51): always-on adaptive, clamped vocab
# ═══════════════════════════════════════════════════════════════════════════════
# Divergence (2026-09-06): the legacy cascade's fable-?5 pattern matched 5.1
# ids, passing switch words through raw (effort='adaptive' caused upstream
# 400s) and dropping non-vocabulary efforts entirely. 5.1 is adaptive-only with
# vocab low/medium/high/xhigh/max and NO off: anything else clamps to the
# documented default 'high'; budgets coerce (32k→max, 16k→medium).
# Full lock: test_fable_51_thinking.py.

@pytest.mark.parametrize("f_val", ["tokenrouter/anthropic/claude-fable-5.1",
                                   "claude-mythos-5-1"])
@pytest.mark.parametrize("effort,expected", [
    ("off", "high"), ("auto", "high"), ("none", "high"), ("", "high"),
    ("adaptive", "high"), ("banana", "high"),
    ("low", "low"), ("medium", "medium"), ("high", "high"),
    ("xhigh", "xhigh"), ("max", "max"),
])
def test_fable51_alwayson_adaptive_clamps(f_val, effort, expected):
    """5.1 never disables thinking; unknown efforts clamp to 'high'."""
    out, _ = resolve_thinking(_payload(), f_val, effort)
    assert out["thinking"] == {"type": "adaptive"}
    assert out["output_config"] == {"effort": expected}


@pytest.mark.parametrize("effort,expected", [("32k", "max"), ("16k", "medium")])
def test_fable51_budgets_coerce(effort, expected):
    out, _ = resolve_thinking(_payload(), "tokenrouter/anthropic/claude-fable-5.1", effort)
    assert out["thinking"] == {"type": "adaptive"}
    assert out["output_config"] == {"effort": expected}


# ─────────────────────────────────────────────────────────────────────
# DIVERGENCE 7 — Grok effort is version-gated; unknowns default to high;
#                presence_penalty/frequency_penalty/stop are stripped.
#
# WHY LEGACY WAS WRONG:
#   The legacy cascade passed the effort through for Grok, so xhigh
#   reached grok-4.5 which does not support it (per xAI docs, xhigh
#   is available on grok-4.6 and later only).  Unknown words also
#   passed through and the vendor rejects them.  Legacy never stripped
#   presence_penalty/frequency_penalty/stop, which reasoning models
#   reject.  New code: version-gate xhigh, coerce unknowns -> high,
#   sanitize always pops the three incompatible keys.
#   Full lock: test_official_thinking_parity.py.
# ─────────────────────────────────────────────────────────────────────

def test_grok45_xhigh_coerces_to_high():
    out, _ = resolve_thinking(_payload(), "xai/grok-4.5", "xhigh")
    assert out["reasoning_effort"] == "high"


def test_grok46_xhigh_kept():
    out, _ = resolve_thinking(_payload(), "xai/grok-4.6", "xhigh")
    assert out["reasoning_effort"] == "xhigh"


def test_grok420_xhigh_kept():
    out, _ = resolve_thinking(_payload(), "xai/grok-4.20", "xhigh")
    assert out["reasoning_effort"] == "xhigh"


def test_grok_unknown_effort_defaults_to_high():
    out, _ = resolve_thinking(_payload(), "xai/grok-4.6", "banana")
    assert out["reasoning_effort"] == "high"


def test_grok_strips_reasoning_incompatible_params():
    body = _payload()
    body["presence_penalty"] = 0.5
    body["frequency_penalty"] = 0.25
    body["stop"] = ["\n"]
    out, _ = resolve_thinking(body, "xai/grok-4.5", "high")
    assert "presence_penalty" not in out
    assert "frequency_penalty" not in out
    assert "stop" not in out
    assert out["reasoning_effort"] == "high"


# ─────────────────────────────────────────────────────────────────────
# DIVERGENCE 8 — DeepSeek V4 drops output_config (Chinese reseller 400).
#
# WHY LEGACY WAS WRONG:
#   Legacy (and the original family contract) emitted a "triple shape":
#     thinking:{type:enabled} + reasoning_effort + output_config.effort
#   Live x5m5x (and other Chinese OpenAI-compatible gateways) reject
#   unknown top-level fields with:
#     400 invalid_request_error: "未知请求字段：output_config"
#   Dual shape (thinking + reasoning_effort) is accepted; output_config
#   is not. The thinking-fallback detector also missed the Chinese
#   phrasing, so BSL never degraded-and-retried.
# ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("f_val", [
    "x5m5x/deepseek-v4-pro",
    "iamhc/DeepSeek-V4-Pro",
    "hcnsec-vip/deepseek-v4-pro",
])
@pytest.mark.parametrize("effort", ["high", "max"])
def test_deepseek_v4_omits_output_config(f_val, effort):
    """DeepSeek V4 must not emit output_config — Chinese resellers 400 it."""
    payload = _payload()
    payload["output_config"] = {"effort": "high"}  # inherited stale field
    out, prov = resolve_thinking(payload, f_val, effort)
    assert "output_config" not in out, (
        f"{f_val}: output_config survived — x5m5x 400s this field. Got: {out}"
    )
    assert out.get("thinking") == {"type": "enabled"}
    assert out.get("reasoning_effort") == effort
    assert any(r.rule == "dual_shape" for r in prov.records), prov.summary()


# ─────────────────────────────────────────────────────────────────────
# DIVERGENCE 9 — GPT-5 on Anthropic wire strips OpenAI reasoning keys.
#
# WHY LEGACY WAS WRONG:
#   GPT-5 always emitted reasoning_effort + reasoning.mode regardless of
#   transport. Live AgentRouter probe 2026-08-20 for gpt-5.6-sol:
#     /v1/messages plain              -> content "OK"
#     /v1/messages + reasoning_effort -> stalls after message_start (empty)
#     /v1/chat/completions + effort   -> content "OK"
#   On the anthropic wire those OpenAI keys produce a zombie empty
#   stream. Strip them; on openai wire keep the existing GPT-5 shape.
# ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("f_val", [
    "agentrouter/gpt-5.6-sol",
    "vsllm-gpt/gpt-5.6-terra",
])
def test_gpt5_anthropic_wire_strips_openai_reasoning(f_val):
    """GPT-5 over /v1/messages must not emit reasoning_effort/reasoning."""
    payload = _payload()
    payload["reasoning_effort"] = "max"
    payload["reasoning"] = {"effort": "max", "mode": "pro"}
    out, prov = resolve_thinking(
        payload, f_val, "max", reasoning_mode="pro", wire_format="anthropic"
    )
    for banned in ("reasoning_effort", "reasoning", "output_config", "thinking"):
        assert banned not in out, (
            f"{f_val} anthropic wire leaked {banned!r}: {out}"
        )
    assert any(
        r.rule == "anthropic_wire_strip_openai_reasoning" for r in prov.records
    ), prov.summary()


def test_gpt5_openai_wire_still_emits_reasoning_controls():
    """OpenAI wire must keep GPT-5 reasoning_effort + reasoning.mode."""
    out, _ = resolve_thinking(
        _payload(),
        "agentrouter/gpt-5.6-sol",
        "max",
        reasoning_mode="pro",
        wire_format="openai",
    )
    assert out.get("reasoning_effort") == "max"
    assert out.get("reasoning") == {"effort": "max", "mode": "pro"}


# ─────────────────────────────────────────────────────────────────────
# DIVERGENCE 10 — GPT-6 (Astra) joins the GPT-5 effort contract, and
#                 budget-style thinking values coerce to a level
#                 (2026-09-06, gpt-6-astra first-class enablement).
#
# WHY LEGACY WAS WRONG (two bugs):
#
#   (a) INVISIBLE MODEL. The legacy `gpt-?5` detector matched NOTHING for
#       gpt-6 ids, so vsllm-r/gpt-6-astra ran with its configured effort
#       silently dropped — no reasoning control ever reached upstream.
#       The contract pattern widened to gpt-?[56]. Astra is EFFORT-ONLY:
#       low|medium|high|xhigh|max (upstream 400s on 'none'), and it has no
#       reasoning_mode/reasoning_context config keys, so the gpt-5.6
#       mode/context extras stay unset naturally.
#
#   (b) RAW BUDGET LEAK. For gpt-5.x, legacy passed budget-style thinking
#       values straight through: thinking: 32k -> reasoning_effort="32k",
#       an invalid effort value upstream rejects. coerce_effort now maps
#       budgets to levels inside the contract (<=16k -> medium, else max).
# ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("f_val", [
    "vsllm-r/gpt-6-astra",
    "openai/gpt-6-astra",
    "arena-web/gpt-6-astra-max",
    "gpt-6",        # bare id
    "gpt6-mini",    # hyphen-less id
])
@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
def test_gpt6_astra_emits_effort(f_val, effort):
    """Legacy silently dropped every effort for gpt-6 ids; now they emit."""
    out, prov = resolve_thinking(_payload(), f_val, effort)
    assert out.get("reasoning_effort") == effort, (
        f"{f_val} effort={effort!r}: got {out.get('reasoning_effort')!r} — "
        "gpt-6 must engage the gpt-5 effort contract"
    )
    reasoning = out.get("reasoning", {})
    assert reasoning.get("effort") == effort
    # Effort-only family: no mode/context axes (gpt-5.6 Sol/Terra features).
    assert "mode" not in reasoning
    assert "context" not in reasoning
    assert prov.records, f"{f_val}: no provenance"


@pytest.mark.parametrize("f_val", [
    "vsllm-r/gpt-6-astra",
    "vsllm-gpt/gpt-5.5",
])
def test_gpt_auto_emits_nothing(f_val):
    """auto stays emit-nothing for both generations (legacy parity)."""
    out, _ = resolve_thinking(_payload(), f_val, "auto")
    assert "reasoning_effort" not in out
    assert "reasoning" not in out


@pytest.mark.parametrize("f_val,effort,expected", [
    ("vsllm-gpt/gpt-5.5", "32k", "max"),
    ("vsllm-gpt/gpt-5.5", "16k", "medium"),
    ("vsllm-gpt/gpt-5.6-terra", "32k", "max"),
    ("vsllm-r/gpt-6-astra", "32k", "max"),
    ("vsllm-gpt/gpt-5.5", "32768", "max"),  # raw token count, no 'k'
])
def test_gpt_budget_thinking_coerces_to_valid_level(f_val, effort, expected):
    """Legacy emitted budget words RAW (reasoning_effort="32k" — invalid);
    the contract coerces them to a real level before emission."""
    out, _ = resolve_thinking(_payload(), f_val, effort)
    got = out.get("reasoning_effort")
    assert got == expected, (
        f"{f_val} effort={effort!r}: expected coerced {expected!r}, got {got!r}"
    )


# ─────────────────────────────────────────────────────────────────────
# DIVERGENCE — StepFun step-5: scoped pattern + out-of-vocab clamps
# (2026-09-23).
#
# WHY LEGACY WAS WRONG:
#   The legacy cascade had ZERO branches for stepfun models, so every
#   operator effort was silently dropped — reasoning never reached
#   upstream. The new step-5 contract emits reasoning_effort with the
#   DOCUMENTED vocabulary low/medium/high; anything out of vocab
#   (enable/adaptive/unknown words/budgets) clamps to 'high' — the
#   strict gateway 400s unknown values (house precedent:
#   test_grok_unknown_effort_defaults_to_high; Kat-coder is the only
#   passthrough family, and only because its vocab is undocumented).
#
#   The pattern is scoped to step-?5 ids so the 8 step-3.x-flash config
#   models keep EXACT legacy behavior — their reasoning-effort
#   acceptance was never verified. Full lock: test_stepfun_reasoning.py.
# ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("effort", ["enable", "adaptive", "max", "xhigh", "ultra", "32k"])
def test_step5_out_of_vocab_clamps_to_high(effort):
    """step-5 documents low/medium/high only; everything else clamps."""
    out, _ = resolve_thinking(_payload(), "iamhc/step-5-preview", effort)
    assert out.get("reasoning_effort") == "high", (
        f"step-5 effort={effort!r}: expected clamp to 'high', got "
        f"{out.get('reasoning_effort')!r} — the gateway 400s unknown values"
    )


@pytest.mark.parametrize("f_val", [
    "kilocode/stepfun/step-3.7-flash",
    "commandcode/stepfun/Step-3.5-Flash",
])
@pytest.mark.parametrize("effort", ["low", "enable", "max"])
def test_step3_flash_models_remain_legacy_untouched(f_val, effort):
    """step-3.x flash ids never match the scoped pattern — legacy parity."""
    out, prov = resolve_thinking(_payload(), f_val, effort)
    assert "reasoning_effort" not in out, (
        f"{f_val} effort={effort!r}: out-of-scope model emitted "
        f"reasoning_effort: {out}"
    )
    assert "output_config" not in out
    contract_ids = {r.contract_id for r in prov.records}
    assert "step-5" not in contract_ids, (
        f"{f_val}: matched the step-5 contract — step-3.x must stay legacy. "
        f"Contracts: {contract_ids}"
    )
