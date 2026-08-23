"""
Regression tests for the 2026-08-23 dual-bug fix.

Bug A - kilocode tools[0].type 400:
    get_profile() ignored the config-level `format` override for providers
    that are registered in PROFILES with an anthropic_messages profile.
    kilocode is registered as anthropic_messages, but the operator pointed
    the connection at an OpenAI-compatible gateway (api.kilo.ai/api/gateway)
    with format: openai. The registry override won, so BSL normalized tool
    schemas into Anthropic shape (name/input_schema) which an OpenAI gateway
    rejects with "The request parameter tools[0].type is invalid or missing".

Bug B - router self-disconnect ("network issue connecting to the server"):
    HEADER_WAIT_TIMEOUT was 90s. Reasoning-heavy models (e.g. GLM-5.3 at
    effort:max with 100k+ token context) legitimately think >90s before the
    first byte; the bound killed a healthy-but-slow stream, producing
    status=504 ttft=0 error=upstream_header_timeout. The header wait must
    only rescue dead/silent TCP connections, not kill slow reasoning.
"""

from app.compat.provider_profiles import (
    get_profile,
    is_anthropic_compatible,
)


# -- Bug A: config format override must beat a registered profile ------------


def test_kilocode_openai_override_beats_anthropic_registry():
    """format=openai on a registered anthropic provider -> openai_chat."""
    profile = get_profile("kilocode", {"format": "openai"})
    assert profile.upstream_protocol == "openai_chat"
    assert profile.base_url_kind == "openai_compatible"
    assert profile.endpoint_path == "/chat/completions"
    assert not is_anthropic_compatible(profile)


def test_kilocode_anthropic_override_keeps_anthropic():
    """format=anthropic on a registered anthropic provider stays anthropic."""
    profile = get_profile("kilocode", {"format": "anthropic"})
    assert profile.upstream_protocol == "anthropic_messages"
    assert is_anthropic_compatible(profile)


def test_kilocode_no_config_uses_registry_default():
    """Without a config override, the registry profile applies unchanged."""
    profile = get_profile("kilocode", None)
    assert profile.upstream_protocol == "anthropic_messages"
    assert is_anthropic_compatible(profile)


def test_unregistered_openai_provider_stays_openai():
    """Unregistered providers with format=openai stay openai_chat."""
    profile = get_profile("tabitoken", {"format": "openai"})
    assert profile.upstream_protocol == "openai_chat"
    assert not is_anthropic_compatible(profile)


def test_openai_image_format_not_clobbered():
    """format=openai-image is an image route, not a chat override."""
    profile = get_profile("kilocode", {"format": "openai-image"})
    # openai-image must NOT trigger the openai chat override path;
    # the provider falls through to its registry profile.
    assert profile.upstream_protocol == "anthropic_messages"


# -- Bug B: header wait timeout must tolerate slow reasoning preambles -------


def test_header_wait_timeout_is_generous():
    """HEADER_WAIT_TIMEOUT must be >= 300s so reasoning models are not killed.

    Evidence (2026-08-23 20:41:06): x5m5x/glm-5.3 at effort:max with a
    140k-token context produced zero bytes for >90s; the old 90s bound
    aborted the stream (status=504, ttft=0, error=upstream_header_timeout)
    and the client saw "network issue connecting to the server".
    """
    from app import main as bsl_main

    assert bsl_main.HEADER_WAIT_TIMEOUT >= 300.0, (
        f"HEADER_WAIT_TIMEOUT={bsl_main.HEADER_WAIT_TIMEOUT}s is too short; "
        "reasoning-heavy models legitimately think >90s before the first byte"
    )
