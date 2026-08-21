"""Thinking vocab fixes (2026-08-20): GLM-5.3 low/high/max, Grok 4.6+ xhigh, Hy3 interleaved."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.compat.families import resolve_thinking


def rt(family, effort):
    payload = {"model": "x"}
    out, _ = resolve_thinking(
        payload, family, None, reasoning_mode=None, reasoning_context=None, wire_format="openai"
    )
    return out


class TestGLM53:
    def test_low(self):
        p = resolve_thinking({}, "glm-5.3", "low")[0]
        assert p["thinking"] == {"type": "enabled"}
        assert p["reasoning_effort"] == "low"

    def test_high(self):
        p = resolve_thinking({}, "glm-5.3", "high")[0]
        assert p["reasoning_effort"] == "high"

    def test_max(self):
        p = resolve_thinking({}, "glm-5.3", "max")[0]
        assert p["reasoning_effort"] == "max"

    def test_medium_coerced_to_high(self):
        p = resolve_thinking({}, "glm-5.3", "medium")[0]
        assert p["reasoning_effort"] == "high"

    def test_xhigh_coerced_to_max(self):
        p = resolve_thinking({}, "glm-5.3", "xhigh")[0]
        assert p["reasoning_effort"] == "max"

    def test_off_forced_on_low(self):
        """GLM-5.3 always thinks: off is coerced to enabled + low (1210 fix)."""
        p = resolve_thinking({"model": "x"}, "glm-5.3", "off")[0]
        assert p["thinking"] == {"type": "enabled"}
        assert p["reasoning_effort"] == "low"

    def test_auto_forced_on_low(self):
        p = resolve_thinking({"model": "x"}, "glm-5.3", "auto")[0]
        assert p["thinking"] == {"type": "enabled"}
        assert p["reasoning_effort"] == "low"

    def test_empty_effort_forced_on_low(self):
        p = resolve_thinking({"model": "x"}, "glm-5.3", "")[0]
        assert p["thinking"] == {"type": "enabled"}
        assert p["reasoning_effort"] == "low"

    def test_adaptive_coerced_high(self):
        """Switch word 'adaptive' is not accepted by 5.3 upstream (1210 fix)."""
        p = resolve_thinking({}, "glm-5.3", "adaptive")[0]
        assert p["thinking"] == {"type": "enabled"}
        assert p["reasoning_effort"] == "high"

    def test_enable_coerced_high(self):
        p = resolve_thinking({}, "glm-5.3", "enable")[0]
        assert p["thinking"] == {"type": "enabled"}
        assert p["reasoning_effort"] == "high"

    def test_anthropic_wire_fval_forced_on(self):
        """Real failing route f_val shape: provider/glm-5.3-anthropic."""
        p = resolve_thinking({}, "vsllm-gpt/glm-5.3-anthropic", "off")[0]
        assert p["thinking"] == {"type": "enabled"}
        assert p["reasoning_effort"] == "low"

    def test_client_disabled_overridden(self):
        """Client passthrough thinking:{type:disabled} must be overridden."""
        p = resolve_thinking(
            {"thinking": {"type": "disabled"}}, "glm-5.3", "off"
        )[0]
        assert p["thinking"] == {"type": "enabled"}
        assert p["reasoning_effort"] == "low"


class TestGrokXhigh:
    def test_grok46_xhigh_kept(self):
        p = resolve_thinking({}, "grok-4.6", "xhigh")[0]
        assert p["reasoning_effort"] == "xhigh"

    def test_grok45_xhigh_coerced_high(self):
        p = resolve_thinking({}, "grok-4.5", "xhigh")[0]
        assert p["reasoning_effort"] == "high"

    def test_grok46_high(self):
        p = resolve_thinking({}, "grok-4.6", "high")[0]
        assert p["reasoning_effort"] == "high"


class TestHy3:
    def test_high_effort(self):
        p = resolve_thinking({}, "hy3", "high")[0]
        ctk = p["chat_template_kwargs"]
        assert ctk["reasoning_effort"] == "high"
        assert ctk["interleaved_thinking"] is True

    def test_low(self):
        p = resolve_thinking({}, "hy3", "low")[0]
        assert p["chat_template_kwargs"]["reasoning_effort"] == "low"

    def test_no_think(self):
        p = resolve_thinking({}, "hy3", "no_think")[0]
        assert p["chat_template_kwargs"]["reasoning_effort"] == "no_think"

    def test_medium_coerced_high(self):
        p = resolve_thinking({}, "hy3", "medium")[0]
        assert p["chat_template_kwargs"]["reasoning_effort"] == "high"

    def test_sampling_defaults_filled(self):
        p = resolve_thinking({}, "hy3", "low")[0]
        assert p["temperature"] == 0.9
        assert p["top_p"] == 1.0

    def test_client_interleaved_preserved(self):
        p = resolve_thinking(
            {"chat_template_kwargs": {"interleaved_thinking": False}}, "hy3", "low"
        )[0]
        assert p["chat_template_kwargs"]["interleaved_thinking"] is False
