"""
Model conformance table.

Adding a model = adding a ROW here. Each row asserts the exact reasoning
payload BSL will send, so a contract regression is caught at test time
rather than as an upstream 400 in production.

This is the test that answers "does GLM behave the same through every
provider that serves it?" — CROSS_PROVIDER_MODELS below asserts that
identical model ids resolve identically regardless of which reseller
carries them, which was NOT true before this refactor.

Run:
  .venv\\Scripts\\python -m pytest app/tests/test_family_conformance.py -q
"""
import os
import sys

import pytest
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.compat.families import resolve_thinking


def _payload():
    return {"model": "x", "messages": [], "max_tokens": 8192}


# ─────────────────────────────────────────────────────────────────────
# Conformance rows: (f_val, effort, expected reasoning fields)
# Only reasoning-owned keys are asserted; max_tokens is checked only
# where a contract deliberately raises it.
# ─────────────────────────────────────────────────────────────────────
CONFORMANCE = [
    # ── OpenAI ──
    ("vsllm-gpt/gpt-5.5", "high",
     {"reasoning_effort": "high", "reasoning": {"effort": "high"}}),

    # ── OpenRouter (aggregator normalizes everything) ──
    ("openrouter/anthropic/claude-sonnet-4", "max",
     {"reasoning": {"effort": "max", "exclude": False}}),

    # ── Gemini: shape depends on generation AND transport.
    # Rows below use the OpenAI-compatible gateway (the default and the
    # most common in config.yaml). Native + anthropic transports are
    # asserted in test_family_divergences.py.
    ("google/gemini-3.1-pro", "high", {"reasoning_effort": "high"}),
    ("google/gemini-2.5-pro", "32k", {"reasoning_effort": "medium"}),

    # ── Anthropic: three contract generations ──
    ("pix4k/claude-opus-4.8-thinking", "max",
     {"thinking": {"type": "adaptive"}, "output_config": {"effort": "max"}}),
    ("vsllm-a/claude-opus-4-6-antigravity", "32k",
     {"thinking": {"type": "enabled", "budget_tokens": 32768}}),
    ("pix4k/claude-sonnet-5-thinking", "high",
     {"thinking": {"type": "adaptive"}, "output_config": {"effort": "high"}}),
    ("pix4k/fable-5", "max",
     {"thinking": {"type": "adaptive"}, "output_config": {"effort": "max"}}),
    # Claude 3.x: budget vocabulary -> enabled + budget_tokens
    ("anthropic/claude-3-5-sonnet-20241022", "32k",
     {"thinking": {"type": "enabled", "budget_tokens": 32768}}),
    # Claude 3.x with a non-budget word falls back to bare adaptive
    ("anthropic/claude-3-5-sonnet-20241022", "max",
     {"thinking": {"type": "adaptive"}}),

    # ── Grok ──
    ("xai/grok-4.5", "high", {"reasoning_effort": "high"}),

    # ── DeepSeek: deliberately redundant triple shape ──
    ("iamhc/DeepSeek-V4-Pro", "high",
     {"thinking": {"type": "enabled"}, "reasoning_effort": "high",
      "output_config": {"effort": "high"}}),

    # ── Kimi: SAME VENDOR, INCOMPATIBLE CONTRACTS ──
    ("moonshot/kimi-k3", "high", {"reasoning_effort": "high"}),
    # K3 rejects 'medium' with a 400 — a stale value must be coerced to 'max'.
    ("moonshot/kimi-k3", "medium", {"reasoning_effort": "max"}),
    ("iamhc/kimi-k2.7-code", "max", {"enable_thinking": True}),

    # ── Qwen — TWO axes, version-dependent (see families/qwen.py) ──
    # 3.7 and older: HYBRID boolean; no effort enum is published, so an
    # effort must NOT be sent. (Previously this row sent reasoning_effort=max
    # to a boolean-only model — the bug this revision fixes.)
    ("hcnsec-vip/Qwen3.7-Max", "max", {"enable_thinking": True}),
    # 3.8-max: enable_thinking + reasoning_effort {low, medium, xhigh}; xhigh
    # is the published default AND ceiling.
    ("vsllm-gpt/qwen3.8-max", "xhigh",
     {"enable_thinking": True, "reasoning_effort": "xhigh"}),
    ("vsllm-gpt/qwen3.8-max", "medium",
     {"enable_thinking": True, "reasoning_effort": "medium"}),

    # ── GLM: version-dependent effort vocabulary ──
    ("iamhc/glm-5.2", "max",
     {"thinking": {"type": "enabled"}, "reasoning_effort": "max"}),
    ("hcnsec-vip/glm-5.1", "enable", {"thinking": {"type": "enabled"}}),
    ("hcnsec-vip/glm-5.1", "adaptive", {"thinking": {"type": "adaptive"}}),

    # ── MiniMax ──
    ("iamhc/Minimax-M3", "enable", {"thinking": {"type": "enabled"}}),
]


@pytest.mark.parametrize("f_val,effort,expected", CONFORMANCE)
def test_conformance(f_val, effort, expected):
    out, prov = resolve_thinking(_payload(), f_val, effort)
    for key, want in expected.items():
        assert key in out, f"{f_val}: missing {key}. Got: {out}"
        assert out[key] == want, (
            f"{f_val} {key}: expected {want}, got {out[key]}"
        )
    assert prov.records, f"{f_val}: no provenance recorded"


# ─────────────────────────────────────────────────────────────────────
# Cross-provider consistency.
#
# The original complaint: "the same model behaves differently depending
# on which provider serves it." These assert that is no longer possible
# for the reasoning axis. Transport (format: anthropic|openai|kiro)
# legitimately still varies per provider and is declared in config.yaml.
# ─────────────────────────────────────────────────────────────────────
CROSS_PROVIDER_MODELS = [
    ("glm-5.2", ["iamhc", "hcnsec-vip", "vsllm-gpt", "pix4k", "vietapi-a"], "max"),
    ("kimi-k3", ["iamhc", "hcnsec-vip", "moonshot"], "high"),
    ("Qwen3.7-Max", ["iamhc", "hcnsec-vip"], "max"),
    ("DeepSeek-V4-Pro", ["iamhc", "hcnsec-vip", "vietapi-a"], "high"),
]


@pytest.mark.parametrize("model_id,providers,effort", CROSS_PROVIDER_MODELS)
def test_same_model_resolves_identically_across_providers(model_id, providers, effort):
    results = {}
    for p in providers:
        out, prov = resolve_thinking(_payload(), f"{p}/{model_id}", effort)
        reasoning_only = {
            k: v for k, v in out.items() if k not in ("model", "messages", "max_tokens")
        }
        results[p] = (reasoning_only, {r.contract_id for r in prov.records})

    first_provider = providers[0]
    first_payload, first_contracts = results[first_provider]
    for p in providers[1:]:
        payload, contracts = results[p]
        assert payload == first_payload, (
            f"{model_id} diverges by provider — the exact bug this refactor fixes.\n"
            f"  {first_provider}: {first_payload}\n"
            f"  {p}: {payload}"
        )
        assert contracts == first_contracts, (
            f"{model_id} resolved via different contracts:\n"
            f"  {first_provider}: {first_contracts}\n"
            f"  {p}: {contracts}"
        )


def test_every_contract_has_a_conformance_row():
    """A contract with no row can regress silently."""
    from app.compat.families import CONTRACTS

    covered = set()
    for f_val, effort, _expected in CONFORMANCE:
        _out, prov = resolve_thinking(_payload(), f_val, effort)
        covered.update(r.contract_id for r in prov.records)

    all_ids = {c.id for c in CONTRACTS}
    missing = all_ids - covered
    assert not missing, (
        f"Contracts with no conformance row: {sorted(missing)}. "
        "Add a row to CONFORMANCE above."
    )


def test_qwen_and_k3_reject_sampling_params_even_when_thinking_off():
    """Sanitization must not be gated on the thinking setting.

    Regression guard: if these strips were ever moved into the effort-gated
    apply path, an effort=off request would ship temperature to a model
    that rejects it with a 400.
    """
    for f_val in ("hcnsec-vip/Qwen3.7-Max", "moonshot/kimi-k3"):
        payload = {
            "model": "x", "messages": [], "max_tokens": 8192,
            "temperature": 0.7, "top_p": 0.9, "n": 1,
        }
        out, _prov = resolve_thinking(payload, f_val, "off")
        for banned in ("temperature", "top_p", "n"):
            assert banned not in out, (
                f"{f_val} with thinking=off still sent {banned} — would 400 upstream"
            )
        # K3 is a THINKING-ONLY model: it cannot disable reasoning, so even at
        # 'off' it must carry an effort. Qwen is HYBRID: 'off' means off, so it
        # must NOT inject an effort (injecting one was the defect being fixed).
        if "kimi-k3" in f_val:
            assert "reasoning_effort" in out, (
                f"{f_val}: always-on model dropped reasoning_effort at off"
            )
        else:
            assert "reasoning_effort" not in out, (
                f"{f_val}: hybrid model injected reasoning_effort at off — "
                "'off' would silently mean 'on'"
            )
