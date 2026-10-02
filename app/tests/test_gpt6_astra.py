"""GPT-6 Astra (vsllm-r) family-contract coverage (2026-09-06).

Astra reasons via effort LEVELS ONLY: low|medium|high|xhigh|max. There is no
'none' (upstream 400s) and no mode/context axes (gpt-5.6 Sol/Terra features).
The gpt-5 family contract was widened (gpt-?5 -> gpt-?[56] in
app/compat/families/openai.py) so gpt-6-astra engages
apply_gpt5_reasoning_controls:

  - config thinking: high  -> reasoning_effort=high + reasoning.effort=high
    (the same dual-emit shape the gpt-5.6 conformance row locks)
  - budget-style "32k"     -> coerced to a valid level, never emitted raw
  - thinking: auto         -> NOTHING emitted (auto = emit nothing in BSL)
  - tools present          -> NO reasoning controls at all (2026-09-30,
    AgentRouter evidence B: "Function tools with reasoning_effort are not
    supported for gpt-6-astra in /v1/chat/completions. To use function
    tools, use /v1/responses or set reasoning_effort to 'none'." Astra has
    no "off" — it thinks by default — so omitting the effort on
    tool-carrying requests is safe; the model's default effort stays on.)

The UI (app/static/app.js getThinkingSpec) adds a gpt-?6 branch with
mandatory:true so no 'off' is offered for Astra.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.compat.families import CONTRACTS, matches_contract, resolve_thinking

_ASTRA = "vsllm-r/gpt-6-astra"
_VALID_LEVELS = {"low", "medium", "high", "xhigh", "max"}


def _payload():
    return {"model": "x", "messages": [], "max_tokens": 8192}


class TestFamilyMatch:
    def test_vsllm_r_routed_id_matches_gpt5_contract(self):
        assert matches_contract(_ASTRA, "gpt-5")

    def test_bare_astra_id_matches(self):
        assert matches_contract("gpt-6-astra", "gpt-5")

    def test_bare_gpt6_without_hyphen_matches(self):
        assert matches_contract("gpt6", "gpt-5")

    def test_widened_pattern_present(self):
        contract = next(c for c in CONTRACTS if c.id == "gpt-5")
        assert contract.pattern == r"gpt-?[56]"


class TestEffortEmit:
    def test_high_dual_emit(self):
        out, prov = resolve_thinking(_payload(), _ASTRA, "high")
        assert out["reasoning_effort"] == "high"
        assert out["reasoning"]["effort"] == "high"

    def test_xhigh_passthrough(self):
        out, _ = resolve_thinking(_payload(), _ASTRA, "xhigh")
        assert out["reasoning_effort"] == "xhigh"

    def test_max_passthrough(self):
        out, _ = resolve_thinking(_payload(), _ASTRA, "max")
        assert out["reasoning_effort"] == "max"

    def test_no_mode_context_axes(self):
        out, _ = resolve_thinking(_payload(), _ASTRA, "high")
        assert "mode" not in out["reasoning"]
        assert "context" not in out["reasoning"]

    def test_provenance_names_gpt5_contract(self):
        _out, prov = resolve_thinking(_payload(), _ASTRA, "high")
        assert prov.records, "no provenance recorded"
        assert {r.contract_id for r in prov.records} == {"gpt-5"}


class TestCoercion:
    def test_budget_32k_coerced_to_valid_level(self):
        out, _ = resolve_thinking(_payload(), _ASTRA, "32k")
        effort = out["reasoning_effort"]
        assert effort in _VALID_LEVELS, f"budget leaked raw onto the wire: {effort!r}"
        assert effort == "max"  # 32k > 16k -> max per coerce_effort

    def test_ultra_coerced_to_max(self):
        out, _ = resolve_thinking(_payload(), _ASTRA, "ultra")
        assert out["reasoning_effort"] == "max"
        assert out["reasoning"]["effort"] == "max"


class TestAutoEmitsNothing:
    def test_auto_no_reasoning_fields(self):
        out, prov = resolve_thinking(_payload(), _ASTRA, "auto")
        assert "reasoning_effort" not in out
        assert "reasoning" not in out
        assert not prov.records, f"auto unexpectedly attributed: {prov.records}"

    def test_off_no_reasoning_fields(self):
        out, _ = resolve_thinking(_payload(), _ASTRA, "off")
        assert "reasoning_effort" not in out
        assert "reasoning" not in out

    def test_auto_leaves_client_reasoning_untouched(self):
        payload = _payload()
        payload["reasoning"] = {"effort": "high"}
        out, _ = resolve_thinking(payload, _ASTRA, "auto")
        assert out["reasoning"] == {"effort": "high"}


class TestAnthropicWire:
    def test_openai_reasoning_keys_stripped_on_anthropic_wire(self):
        """Client-supplied OpenAI reasoning keys must not reach /v1/messages."""
        payload = _payload()
        payload["reasoning_effort"] = "high"
        payload["reasoning"] = {"effort": "high"}
        out, prov = resolve_thinking(payload, _ASTRA, "high", wire_format="anthropic")
        assert "reasoning_effort" not in out
        assert "reasoning" not in out
        assert any(
            r.rule == "anthropic_wire_strip_openai_reasoning" for r in prov.records
        )

    def test_clean_payload_is_noop_on_anthropic_wire(self):
        """No keys present -> nothing to strip -> no provenance noise."""
        out, prov = resolve_thinking(_payload(), _ASTRA, "high", wire_format="anthropic")
        assert "reasoning_effort" not in out
        assert "reasoning" not in out
        assert not prov.records


class TestToolsSuppressReasoning:
    """Function tools + reasoning_effort are mutually exclusive for astra on
    /v1/chat/completions (AgentRouter 400, 2026-09-30, evidence B). A payload
    carrying a non-empty tools list gets NO reasoning_effort/reasoning at all;
    pre-existing keys are stripped with attribution. Astra thinks by default,
    so the omission is safe."""

    def test_tools_present_no_reasoning_controls(self):
        payload = _payload()
        payload["tools"] = [{"type": "function", "function": {"name": "read_file"}}]
        out, _ = resolve_thinking(payload, _ASTRA, "max")
        assert "reasoning_effort" not in out, (
            f"astra + tools leaked reasoning_effort: {out}"
        )
        assert "reasoning" not in out

    def test_tools_present_strips_preexisting_reasoning_keys(self):
        payload = _payload()
        payload["tools"] = [{"type": "function", "function": {"name": "read_file"}}]
        payload["reasoning_effort"] = "high"
        payload["reasoning"] = {"effort": "high"}
        out, prov = resolve_thinking(payload, _ASTRA, "max")
        assert "reasoning_effort" not in out
        assert "reasoning" not in out
        assert any(r.rule == "tools_reasoning_strip" for r in prov.records), (
            prov.summary()
        )

    def test_tools_present_clean_payload_no_provenance_noise(self):
        """tools present but no pre-existing reasoning keys -> nothing to
        strip -> no provenance record, no emission."""
        payload = _payload()
        payload["tools"] = [{"type": "function", "function": {"name": "read_file"}}]
        out, prov = resolve_thinking(payload, _ASTRA, "max")
        assert "reasoning_effort" not in out
        assert "reasoning" not in out
        assert not prov.records, prov.summary()

    def test_empty_tools_list_still_emits_effort(self):
        """An EMPTY tools list is not 'carrying tools' — effort still emits."""
        payload = _payload()
        payload["tools"] = []
        out, _ = resolve_thinking(payload, _ASTRA, "max")
        assert out["reasoning_effort"] == "max"
        assert out["reasoning"]["effort"] == "max"

    def test_tools_absent_effort_still_emitted(self):
        """Existing behavior is preserved when tools are absent."""
        out, _ = resolve_thinking(_payload(), _ASTRA, "max")
        assert out["reasoning_effort"] == "max"
        assert out["reasoning"]["effort"] == "max"

    def test_anthropic_wire_strip_fires_before_tools_gate(self):
        """Anthropic-wire strip has priority over the tools gate: on
        /v1/messages the OpenAI keys are removed by the wire rule regardless
        of tools presence (the stall-empty lane, 2026-08-20)."""
        payload = _payload()
        payload["tools"] = [{"type": "function", "function": {"name": "x"}}]
        payload["reasoning_effort"] = "high"
        out, prov = resolve_thinking(payload, _ASTRA, "max", wire_format="anthropic")
        assert "reasoning_effort" not in out
        assert "reasoning" not in out
        assert any(
            r.rule == "anthropic_wire_strip_openai_reasoning" for r in prov.records
        )
