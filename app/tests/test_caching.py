"""Focused GPT-5.6 OpenAI cache-routing regression tests."""

from app.middleware.caching import PromptCachingAdapter
from app.models import ChatCompletionRequest, Message


class _Observability:
    _CONSOLE_LOG_PATH = "unused"

    def __init__(self):
        self.console_logs = []
        self.persisted = []

    def _persist_entry(self, path, entry):
        self.persisted.append((path, entry))


def _payload(system_text="s" * 1200):
    return {
        "messages": [
            {"role": "system", "content": system_text},
            {"role": "user", "content": "mutable user tail must not affect the key"},
        ],
    }


def _cached(model, payload=None, tools=None, obs=None):
    return PromptCachingAdapter.apply_provider_caching(
        payload if payload is not None else _payload(),
        "openai",
        model,
        tools_config=tools or {},
        obs=obs,
    )


def test_gpt56_key_is_stable_for_routing_variants_and_family_specific():
    sol_base = _cached("gpt-5.6-sol")
    sol_variant = _cached("gpt-5.6-sol-pro20x-thinking")
    terra = _cached("gpt-5.6-terra-pro20x")
    luna = _cached("gpt-5.6-luna")

    assert sol_base["prompt_cache_key"] == sol_variant["prompt_cache_key"]
    assert sol_base["prompt_cache_key"].startswith("bsl-cache-gpt-5.6-sol-")
    assert terra["prompt_cache_key"].startswith("bsl-cache-gpt-5.6-terra-")
    assert luna["prompt_cache_key"].startswith("bsl-cache-gpt-5.6-luna-")
    for payload in (sol_base, sol_variant, terra, luna):
        assert "prompt_cache_breakpoint" not in payload
        assert "prompt_cache_options" not in payload
    assert len({sol_base["prompt_cache_key"], terra["prompt_cache_key"], luna["prompt_cache_key"]}) == 3


def test_gpt56_changed_static_prefix_changes_key_without_using_user_tail():
    first = _cached("gpt-5.6-sol", _payload("a" * 1200))
    second = _cached("gpt-5.6-sol", _payload("b" * 1200))
    assert first["prompt_cache_key"] != second["prompt_cache_key"]


def test_gpt56_preserves_caller_key_and_retention():
    payload = _payload()
    payload["prompt_cache_key"] = "caller-selected-key"
    payload["prompt_cache_retention"] = "24h"
    obs = _Observability()

    result = _cached("gpt-5.6-sol", payload, {"caching_tracker_enabled": True}, obs)

    assert result["prompt_cache_key"] == "caller-selected-key"
    assert result["prompt_cache_retention"] == "24h"
    assert obs.console_logs[-1]["cache_hint"] == "preserved"
    assert "caller-selected-key" not in str(obs.console_logs[-1])


def test_gpt56_gate_and_short_prefix_do_not_generate_a_key():
    disabled = _cached(
        "gpt-5.6-sol",
        _payload(),
        {"caching_openai_key_bound": False},
    )
    short = _cached("gpt-5.6-sol", _payload("short"))

    assert "prompt_cache_key" not in disabled
    assert "prompt_cache_key" not in short


def test_non_gpt56_openai_stays_implicit_only():
    obs = _Observability()
    result = _cached("gpt-5.5", _payload(), {"caching_tracker_enabled": True}, obs)

    assert "prompt_cache_key" not in result
    assert obs.console_logs[-1]["strategy"] == "implicit-prefix"
    assert obs.console_logs[-1]["cache_hint"] == "static-first-sorted"


def test_gpt56_tracker_reports_safe_routing_states():
    generated_obs = _Observability()
    _cached("gpt-5.6-sol", _payload(), {"caching_tracker_enabled": True}, generated_obs)
    assert generated_obs.console_logs[-1]["cache_hint"] == "generated"

    preserved_obs = _Observability()
    caller_payload = _payload()
    caller_payload["prompt_cache_key"] = "caller-selected-key"
    _cached("gpt-5.6-sol", caller_payload, {"caching_tracker_enabled": True}, preserved_obs)
    assert preserved_obs.console_logs[-1]["cache_hint"] == "preserved"

    short_obs = _Observability()
    _cached("gpt-5.6-sol", _payload("short"), {"caching_tracker_enabled": True}, short_obs)
    assert short_obs.console_logs[-1]["cache_hint"] == "too-short"

    disabled_obs = _Observability()
    _cached("gpt-5.6-sol", _payload(), {
        "caching_openai_key_bound": False,
        "caching_tracker_enabled": True,
    }, disabled_obs)
    assert disabled_obs.console_logs[-1]["cache_hint"] == "disabled"


def test_gpt56_retention_24h_injected_experimentally():
    payload = _payload()
    result = _cached("gpt-5.6-sol", payload, {
        "caching_openai_key_bound": True,
        "caching_openai_retention_24h": True,
    })
    assert result["prompt_cache_retention"] == "24h"
    assert result["prompt_cache_key"].startswith("bsl-cache-gpt-5.6-sol-")


def test_gpt56_retention_24h_preserves_caller_override():
    payload = _payload()
    payload["prompt_cache_retention"] = "30m"
    result = _cached("gpt-5.6-sol", payload, {
        "caching_openai_key_bound": True,
        "caching_openai_retention_24h": True,
    })
    assert result["prompt_cache_retention"] == "30m"


def test_gpt56_retention_24h_off_by_default():
    result = _cached("gpt-5.6-sol", _payload())
    assert "prompt_cache_retention" not in result


def test_gpt56_retention_24h_survives_static_sort_pipeline():
    """Integration: retention must survive static_sort → model_dump → provider_caching."""
    request = ChatCompletionRequest(
        model="gpt-5.6-sol",
        messages=[Message(role="system", content="s" * 1200)],
    )
    tools = {"caching_openai_key_bound": True, "caching_openai_retention_24h": True}
    result = PromptCachingAdapter.apply_static_first_sort(request, tools)
    assert result.prompt_cache_key.startswith("bsl-cache-gpt-5.6-sol-")
    assert result.prompt_cache_retention == "24h"
    # Verify retention survives serialization (the actual runtime path to upstream)
    dumped = result.model_dump(exclude_none=True)
    assert dumped["prompt_cache_key"].startswith("bsl-cache-gpt-5.6-sol-")
    assert dumped["prompt_cache_retention"] == "24h"


def test_gpt56_tracker_distinguishes_bsl_generated_from_preserved():
    """After static_sort sets a bsl-cache- key, provider_caching must report 'bsl-generated' not 'preserved'."""
    request = ChatCompletionRequest(
        model="gpt-5.6-sol",
        messages=[Message(role="system", content="s" * 1200)],
    )
    tools = {"caching_openai_key_bound": True, "caching_tracker_enabled": True}
    PromptCachingAdapter.apply_static_first_sort(request, tools)
    # Now simulate the runtime: dump → apply_provider_caching
    payload = request.model_dump(exclude_none=True)
    obs = _Observability()
    result = PromptCachingAdapter.apply_provider_caching(
        payload, "openai", "gpt-5.6-sol", tools_config=tools, obs=obs
    )
    assert obs.console_logs[-1]["cache_hint"] == "bsl-generated"
    assert result["prompt_cache_key"].startswith("bsl-cache-")


def test_malformed_payload_fails_open_without_unsupported_cache_fields():
    malformed = {"messages": "not-a-list", "prompt_cache_retention": "24h"}
    result = _cached("gpt-5.6-sol", malformed)
    assert result is malformed
    assert result["prompt_cache_retention"] == "24h"
    assert "prompt_cache_key" not in result
    assert PromptCachingAdapter.apply_provider_caching(
        None, "openai", "gpt-5.6-sol", tools_config={}
    ) is None


def test_universal_static_sort_path_keeps_caller_retention_and_adds_key():
    request = ChatCompletionRequest(
        model="gpt-5.6-sol-pro20x",
        messages=[
            Message(role="user", content="mutable tail"),
            Message(role="system", content="s" * 1200),
        ],
        prompt_cache_retention="24h",
    )

    result = PromptCachingAdapter.apply_static_first_sort(request, {})

    assert result.messages[0].role == "system"
    assert result.prompt_cache_key.startswith("bsl-cache-gpt-5.6-sol-")
    assert result.prompt_cache_retention == "24h"


def test_key_bound_routing_is_not_disabled_by_static_sort_gate():
    request = ChatCompletionRequest(
        model="gpt-5.6-terra",
        messages=[Message(role="system", content="s" * 1200)],
    )

    result = PromptCachingAdapter.apply_static_first_sort(
        request, {"caching_static_sort": False}
    )

    assert result.prompt_cache_key.startswith("bsl-cache-gpt-5.6-terra-")
