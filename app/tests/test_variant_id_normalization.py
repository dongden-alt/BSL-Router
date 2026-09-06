"""
Variant-ID separator normalization parity locks.

Model IDs where the version separator is a DASH instead of a DOT
(glm-5-3, gpt-5-6-sol, qwen-3-8, ...) must resolve to the SAME family
contract and payload as their dotted canonicals. The normalization lives
in exactly one place — ThinkingContext.__post_init__ (families/_base.py) —
which rewrites the MATCH TARGET (f_val) with digit-dash-digit ->
digit-dot-digit before any contract regex runs. It is match-only: the
upstream payload model field and exact-id config lookups are untouched.

The UI mirror lives in app.js getThinkingSpec (same regex, same intent).

Run:
  python -m pytest app/tests/test_variant_id_normalization.py -v
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.compat.families import resolve_thinking
from app.compat.families._base import ThinkingContext


def _payload(**extra):
    body = {"model": "x", "messages": [], "max_tokens": 8192}
    body.update(extra)
    return body


# ─────────────────────────────────────────────────────────────────────────
# Unit: the normalization itself
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,normalized", [
    ("iamhc/glm-5-3", "iamhc/glm-5.3"),
    ("vsllm-gpt/gpt-5-6-sol", "vsllm-gpt/gpt-5.6-sol"),
    ("prov/qwen-3-8-max", "prov/qwen-3.8-max"),  # dash BEFORE digits preserved; qwen-?3\.8 matches both
    # Word dashes are NOT version separators — untouched.
    ("moonshot/kimi-k3", "moonshot/kimi-k3"),
    ("openai/gpt-6-astra", "openai/gpt-6-astra"),
    ("xai/grok-4-6-non-reasoning", "xai/grok-4.6-non-reasoning"),
    # Canonical dotted ids round-trip unchanged.
    ("iamhc/glm-5.3", "iamhc/glm-5.3"),
    ("meta/muse-spark-1.1", "meta/muse-spark-1.1"),
    # Empty / None safe.
    ("", ""),
])
def test_thinking_context_normalizes_match_target(raw, normalized):
    assert ThinkingContext(f_val=raw).f_val == normalized


def test_normalization_does_not_touch_payload_model_field():
    """Match-only: the upstream payload's model id must never be rewritten."""
    src = _payload(model="iamhc/glm-5-3")
    out, _ = resolve_thinking(src, "iamhc/glm-5-3", "high")
    assert out["model"] == "iamhc/glm-5-3"


# ─────────────────────────────────────────────────────────────────────────
# Parity matrix: dashed variant ≡ dotted canonical
# ─────────────────────────────────────────────────────────────────────────

def _assert_parity(dashed, dotted, effort, **kwargs):
    """resolve_thinking must produce IDENTICAL payload + provenance for the
    dashed variant and the dotted canonical."""
    out_d, prov_d = resolve_thinking(_payload(), dashed, effort, **kwargs)
    out_c, prov_c = resolve_thinking(_payload(), dotted, effort, **kwargs)
    assert out_d == out_c, (
        f"payload diverged for {dashed!r} vs {dotted!r} (effort={effort!r}): "
        f"{out_d} != {out_c}"
    )
    assert prov_d.as_list() == prov_c.as_list(), (
        f"provenance diverged for {dashed!r} vs {dotted!r} (effort={effort!r}): "
        f"{prov_d.summary()} != {prov_c.summary()}"
    )
    return out_d


# GLM-5.3 — thinking always on, three-word effort vocab (low/high/max).

@pytest.mark.parametrize("effort", ["none", "low", "medium", "xhigh", "max", "off", "auto", "garbage"])
def test_glm53_dashed_dotted_parity(effort):
    out = _assert_parity("iamhc/glm-5-3", "iamhc/glm-5.3", effort)
    # Sanity: the dotted canonical really is the 5.3 contract.
    assert out.get("thinking") == {"type": "enabled"}
    assert out.get("reasoning_effort") in ("low", "high", "max")


@pytest.mark.parametrize("effort", ["none", "low", "medium", "xhigh", "max", "off", "auto", "garbage"])
def test_glm53_anthropic_dashed_dotted_parity(effort):
    _assert_parity("glm-5-3-anthropic", "glm-5.3-anthropic", effort)


# GLM-5.2 — none/minimal disable thinking; others coerce to high/max.

@pytest.mark.parametrize("effort", ["none", "minimal", "low", "high", "max"])
def test_glm52_dashed_dotted_parity(effort):
    out = _assert_parity("iamhc/glm-5-2", "iamhc/glm-5.2", effort)
    if effort in ("none", "minimal"):
        assert out.get("thinking") == {"type": "disabled"}
    else:
        assert out.get("reasoning_effort") in ("high", "max")


# GPT-5.6 family (Sol) — reasoning + mode + context metadata.

@pytest.mark.parametrize("effort", ["high", "max", "auto"])
def test_gpt56_sol_dashed_dotted_parity(effort):
    _assert_parity(
        "vsllm-gpt/gpt-5-6-sol", "vsllm-gpt/gpt-5.6-sol", effort,
        reasoning_mode="pro", reasoning_context="all_turns",
    )


# GPT-5.4.

@pytest.mark.parametrize("effort", ["low", "xhigh"])
def test_gpt54_dashed_dotted_parity(effort):
    out = _assert_parity("vsllm-gpt/gpt-5-4", "vsllm-gpt/gpt-5.4", effort)
    assert out.get("reasoning_effort") == effort


# Qwen3.8-max — real effort enum {low, medium, xhigh}.

@pytest.mark.parametrize("effort", ["low", "xhigh", "none"])
def test_qwen38_dashed_dotted_parity(effort):
    out = _assert_parity("prov/qwen-3-8-max", "prov/qwen3.8-max", effort)
    if effort == "low":
        assert out.get("reasoning_effort") == "low"
    if effort == "xhigh":
        assert out.get("reasoning_effort") == "xhigh"


# Kimi K2.7-code — always-on thinking, enable_thinking boolean path.

@pytest.mark.parametrize("effort", ["enable", "off"])
def test_kimi_k27_code_dashed_dotted_parity(effort):
    out = _assert_parity("prov/kimi-k2-7-code", "prov/kimi-k2.7-code", effort)
    if effort == "enable":
        assert out.get("enable_thinking") is True
    # effort=off is not explicit → apply gated off, payloads untouched (equal).


# Muse Spark 1.1 — version-sensitive via ctx.f_val numeric parse
# (muse.py _parse_muse_version), NOT raw-input parsing. The dotted regex
# r"muse[-_\s]?spark[-_\s]?(\d+(?:\.\d+)?)" only captures "1.1" when the
# separator is a dot, so "muse-spark-1-1" previously parsed as (1, 0) →
# 1.1 wire by the <=(1,1) fallback. After normalization both parse as
# (1, 1) and take the true 1.1 path.

@pytest.mark.parametrize("effort", ["low", "max"])
def test_muse_spark_11_dashed_dotted_parity(effort):
    out = _assert_parity("prov/muse-spark-1-1", "prov/muse-spark-1.1", effort)
    # 1.1 wire: adaptive thinking + output_config.effort, no reasoning_effort.
    # low passes through; max coerces to xhigh (deepest real tier).
    assert out.get("thinking") == {"type": "adaptive"}
    assert out.get("output_config") == {"effort": "low" if effort == "low" else "xhigh"}
    assert "reasoning_effort" not in out


# ─────────────────────────────────────────────────────────────────────────
# Regression guards: already separator-tolerant behavior must not break.
# ─────────────────────────────────────────────────────────────────────────

def test_claude_opus46_antigravity_budget_unchanged():
    """Opus 4.6 antigravity budget path (word dashes around 4-6 were already
    tolerated by r'opus.*4[.-]6'); normalization must not disturb it."""
    out, _ = resolve_thinking(
        _payload(), "pix4k/claude-opus-4-6-antigravity-ultra", "32k"
    )
    assert out.get("thinking") == {"type": "enabled", "budget_tokens": 32768}
    assert out["max_tokens"] >= 32768 + 32768


def test_grok_46_accepts_xhigh_vs_45_coerced():
    """grok.py _supports_xhigh uses r'grok[-_. ]*(\\d+)(?:[.-](\\d+))?' —
    already dash-tolerant. Dashed ids must keep identical behavior."""
    out46, _ = resolve_thinking(_payload(), "prov/grok-4-6", "xhigh")
    assert out46.get("reasoning_effort") == "xhigh"
    out45, _ = resolve_thinking(_payload(), "prov/grok-4-5", "xhigh")
    assert out45.get("reasoning_effort") == "high"  # 4.5 coerces xhigh -> high


def test_grok_dashed_dotted_parity_xhigh():
    _assert_parity("prov/grok-4-6", "prov/grok-4.6", "xhigh")
    _assert_parity("prov/grok-4-5", "prov/grok-4.5", "xhigh")


def test_fable_5_1_hits_claude_next_51_contract():
    """Fable/Mythos 5.1 routes to claude-next-51 (adaptive-only, official
    docs 2026-09-06 — 'enabled' 400s upstream), dashed or dotted. The
    effort=high payload shape matches the 5.x contract, so the provenance
    assertion is what pins the NEW contract id."""
    for f_val in ("vsllm-a/fable-5-1", "vsllm-a/fable-5.1"):
        out, prov = resolve_thinking(_payload(), f_val, "high")
        assert out.get("thinking") == {"type": "adaptive"}, f_val
        assert out.get("output_config") == {"effort": "high"}, f_val
        assert [r.contract_id for r in prov.records] == ["claude-next-51"], f_val
    _assert_parity("vsllm-a/fable-5-1", "vsllm-a/fable-5.1", "high")


def test_word_dash_ids_still_match_their_contracts():
    """Digit-dash-digit normalization must not break word-dash detectors."""
    # kimi-k3 (K3 contract, not K2): effort coerced into low/high/max.
    out, _ = resolve_thinking(_payload(), "moonshot/kimi-k3", "medium")
    assert out.get("reasoning_effort") == "max"  # medium not in K3 vocab → default max
    # gpt-6-astra: mandatory effort passthrough.
    out, _ = resolve_thinking(_payload(), "openai/gpt-6-astra", "xhigh")
    assert out.get("reasoning_effort") == "xhigh"
    # non-reasoning SKU suffix untouched (word dash preserved → still excluded
    # from reasoning controls entirely, dashed or dotted).
    out_nr, prov_nr = resolve_thinking(_payload(), "xai/grok-4-6-non-reasoning", "high")
    out_nr_dot, _ = resolve_thinking(_payload(), "xai/grok-4.6-non-reasoning", "high")
    assert out_nr == out_nr_dot
    assert prov_nr.summary() == "none"  # no thinking fields for non-reasoning SKUs
