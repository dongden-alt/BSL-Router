"""StepFun step-5 family contract tests (2026-09-23).

low/medium/high pass through; xhigh/max -> high; budget style -> high;
out-of-vocab efforts (enable/adaptive/unknown words) CLAMP to high — the
documented vocabulary is low/medium/high and the gateway 400s unknown
values (Grok clamp precedent); off/auto/"" -> no reasoning keys.
Anthropic wire uses output_config.effort and strips reasoning_effort;
openai wire uses top-level reasoning_effort and strips output_config;
gemini wire leaves the payload untouched.

The contract is scoped to step-5 ids (pattern step-?5): step-3.x flash
models never match and keep exact legacy behavior — see
TestStepFunOutOfScope below and test_family_divergences.py.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.compat.families import resolve_thinking


def _openai(effort, family="prov/step-5-preview"):
    p, _ = resolve_thinking({}, family, effort, wire_format="openai")
    return p


def _anthropic(effort, family="prov/step-5-preview"):
    p, _ = resolve_thinking({}, family, effort, wire_format="anthropic")
    return p


def _gemini(effort, family="prov/step-5-preview"):
    p, _ = resolve_thinking({}, family, effort, wire_format="gemini")
    return p


class TestStepFunOpenAIWire:
    def test_low(self):
        assert _openai("low")["reasoning_effort"] == "low"

    def test_medium(self):
        assert _openai("medium")["reasoning_effort"] == "medium"

    def test_high(self):
        assert _openai("high")["reasoning_effort"] == "high"

    def test_xhigh_collapses_to_high(self):
        assert _openai("xhigh")["reasoning_effort"] == "high"

    def test_max_collapses_to_high(self):
        assert _openai("max")["reasoning_effort"] == "high"

    def test_budget_style_collapses_to_high(self):
        assert _openai("32k")["reasoning_effort"] == "high"

    def test_unknown_clamps_to_high(self):
        # Unknown words clamp to high — the documented vocab is
        # low/medium/high and the gateway 400s unknown values
        # (Grok clamp precedent; Kat-coder is the only passthrough
        # family, and only because its vocab is undocumented).
        assert _openai("ultra")["reasoning_effort"] == "high"

    def test_enable_clamps_to_high(self):
        assert _openai("enable")["reasoning_effort"] == "high"

    def test_adaptive_clamps_to_high(self):
        assert _openai("adaptive")["reasoning_effort"] == "high"

    def test_off_writes_nothing(self):
        p = _openai("off")
        assert "reasoning_effort" not in p
        assert "output_config" not in p

    def test_auto_writes_nothing(self):
        p = _openai("auto")
        assert "reasoning_effort" not in p
        assert "output_config" not in p

    def test_empty_writes_nothing(self):
        p = _openai("")
        assert "reasoning_effort" not in p
        assert "output_config" not in p


class TestStepFunAnthropicWire:
    def test_high_uses_output_config_effort(self):
        p = _anthropic("high")
        assert p["output_config"] == {"effort": "high"}
        assert "reasoning_effort" not in p

    def test_low_uses_output_config_effort(self):
        p = _anthropic("low")
        assert p["output_config"] == {"effort": "low"}
        assert "reasoning_effort" not in p

    def test_enable_clamps_to_high(self):
        p = _anthropic("enable")
        assert p["output_config"] == {"effort": "high"}
        assert "reasoning_effort" not in p

    def test_provenance_names_step5_contract(self):
        _, prov = resolve_thinking(
            {}, "prov/step-5-preview", "high", wire_format="anthropic"
        )
        ids = [r.contract_id for r in prov.records]
        assert "step-5" in ids


class TestStepFunGeminiWire:
    def test_payload_unchanged(self):
        p = _gemini("high")
        assert "reasoning_effort" not in p
        assert "output_config" not in p
        assert p == {}

    def test_low_unchanged(self):
        p = _gemini("low")
        assert p == {}


class TestStepFunProviderSegment:
    def test_stepfun_segment_fires_contract(self):
        # The MODEL-ID segment (step-5-preview) is what matches the scoped
        # pattern — the bare "stepfun" provider segment no longer does.
        p, prov = resolve_thinking({}, "stepfun/step-5-preview", "high",
                                   wire_format="openai")
        assert p["reasoning_effort"] == "high"
        assert any(r.contract_id == "step-5" for r in prov.records)

    def test_step5_no_dash_matches(self):
        p, _ = resolve_thinking({}, "step5-preview", "high", wire_format="openai")
        assert p["reasoning_effort"] == "high"


class TestStepFunInheritedContainerStrip:
    def test_openai_wire_strips_inherited_output_config(self):
        # An inherited output_config (e.g. from an anthropic-shaped upstream)
        # must be removed on the openai wire — strict gateways 400 on unknown
        # top-level fields.
        p, _ = resolve_thinking(
            {"output_config": {"effort": "stale"}},
            "prov/step-5-preview", "high", wire_format="openai",
        )
        assert p["reasoning_effort"] == "high"
        assert "output_config" not in p

    def test_anthropic_wire_strips_inherited_reasoning_effort(self):
        p, _ = resolve_thinking(
            {"reasoning_effort": "stale"},
            "prov/step-5-preview", "high", wire_format="anthropic",
        )
        assert p["output_config"] == {"effort": "high"}
        assert "reasoning_effort" not in p


class TestStepFunOutOfScope:
    """step-3.x flash ids are OUT of contract scope: their reasoning-effort
    acceptance was never verified, legacy emitted nothing for them, and the
    scoped pattern (step-?5) preserves that parity."""
    def test_step37_flash_no_reasoning_keys(self):
        p, prov = resolve_thinking({}, "prov/stepfun/step-3.7-flash", "high",
                                   wire_format="openai")
        assert "reasoning_effort" not in p
        assert "output_config" not in p
        assert not any(r.contract_id == "step-5" for r in prov.records)

    def test_step35_flash_no_reasoning_keys(self):
        p, prov = resolve_thinking({}, "prov/stepfun/step-3.5-flash", "enable",
                                   wire_format="openai")
        assert "reasoning_effort" not in p
        assert "output_config" not in p
        assert not any(r.contract_id == "step-5" for r in prov.records)
