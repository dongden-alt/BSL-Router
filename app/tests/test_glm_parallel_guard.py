"""Pure pytest unit tests for app.middleware.glm_parallel_guard (no network)."""

from app.middleware.glm_parallel_guard import (
    apply_glm_parallel_guard,
    inject_disable_parallel_tool_use,
    is_glm_model,
)


class TestIsGlmModel:
    def test_glm_lowercase(self):
        assert is_glm_model("glm-5.2") is True

    def test_glm_uppercase(self):
        assert is_glm_model("GLM-5.3-Flash") is True

    def test_non_glm(self):
        assert is_glm_model("gpt-5.5") is False

    def test_empty_string(self):
        assert is_glm_model("") is False

    def test_none(self):
        assert is_glm_model(None) is False


class TestInjectDisableParallelToolUse:
    def test_no_tools_key_unchanged(self):
        payload = {"model": "glm-5.2", "messages": []}
        result = inject_disable_parallel_tool_use(payload)
        assert result is payload
        assert "tool_choice" not in payload

    def test_empty_tools_unchanged(self):
        payload = {"tools": []}
        result = inject_disable_parallel_tool_use(payload)
        assert result is payload

    def test_tools_no_tool_choice_gains_auto(self):
        payload = {"tools": [{"name": "t"}]}
        result = inject_disable_parallel_tool_use(payload)
        assert result["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}

    def test_tool_choice_any_type_preserved(self):
        payload = {"tools": [{"name": "t"}], "tool_choice": {"type": "any"}}
        result = inject_disable_parallel_tool_use(payload)
        assert result["tool_choice"] == {"type": "any", "disable_parallel_tool_use": True}

    def test_tool_choice_named_tool_preserved(self):
        payload = {"tools": [{"name": "t"}], "tool_choice": {"type": "tool", "name": "x"}}
        result = inject_disable_parallel_tool_use(payload)
        assert result["tool_choice"] == {"type": "tool", "name": "x", "disable_parallel_tool_use": True}

    def test_tool_choice_string_auto_normalized(self):
        payload = {"tools": [{"name": "t"}], "tool_choice": "auto"}
        result = inject_disable_parallel_tool_use(payload)
        assert result["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}

    def test_tool_choice_string_any_preserved(self):
        payload = {"tools": [{"name": "t"}], "tool_choice": "any"}
        result = inject_disable_parallel_tool_use(payload)
        assert result["tool_choice"] == {"type": "any", "disable_parallel_tool_use": True}

    def test_already_flagged_idempotent(self):
        payload = {
            "tools": [{"name": "t"}],
            "tool_choice": {"type": "auto", "disable_parallel_tool_use": True},
        }
        result = inject_disable_parallel_tool_use(payload)
        assert result is payload

    def test_input_not_mutated(self):
        payload = {"tools": [{"name": "t"}], "tool_choice": {"type": "any"}}
        original = {"tools": [{"name": "t"}], "tool_choice": {"type": "any"}}
        result = inject_disable_parallel_tool_use(payload)
        assert payload == original
        assert result is not payload

    def test_non_dict_passthrough(self):
        assert inject_disable_parallel_tool_use(None) is None


class TestApplyGlmParallelGuard:
    TOOLS_CFG = {"glm_no_parallel_tools": True}

    def test_glm_model_with_tools_injected(self):
        payload = {"tools": [{"name": "t"}]}
        result = apply_glm_parallel_guard(payload, "glm-5.2", self.TOOLS_CFG)
        assert result["tool_choice"]["disable_parallel_tool_use"] is True

    def test_non_glm_model_unchanged(self):
        payload = {"tools": [{"name": "t"}]}
        result = apply_glm_parallel_guard(payload, "gpt-5.5", self.TOOLS_CFG)
        assert result is payload

    def test_config_off_noop_even_for_glm(self):
        payload = {"tools": [{"name": "t"}]}
        result = apply_glm_parallel_guard(
            payload, "glm-5.2", {"glm_no_parallel_tools": False}
        )
        assert result is payload

    def test_default_missing_key_enabled(self):
        payload = {"tools": [{"name": "t"}]}
        result = apply_glm_parallel_guard(payload, "glm-5.2", {})
        assert result["tool_choice"]["disable_parallel_tool_use"] is True
