import pytest

from app.utils.url_normalization import (
    build_custom_models_probe_url,
    build_custom_text_upstream_url,
    canonicalize_custom_text_base_url,
    normalize_custom_text_provider_urls,
)


@pytest.mark.parametrize(
    "raw, expected",
    [
        (" https://host/v1/ ", "https://host"),
        ("https://host/v1/chat/completions", "https://host"),
        ("https://host/chat/completions", "https://host"),
        ("https://host/v1/messages", "https://host"),
        ("https://host/messages", "https://host"),
        ("https://host/v1/responses", "https://host"),
        ("https://host/responses", "https://host"),
        ("https://host/api/openai/v1", "https://host/api/openai"),
        ("https://host/api/openai/v1/chat/completions", "https://host/api/openai"),
        ("https://host/v1beta/openai", "https://host/v1beta/openai"),
        ("https://host/api/coding/v3", "https://host/api/coding/v3"),
        ("http://localhost:11434/v1", "http://localhost:11434"),
    ],
)
def test_canonicalize_custom_text_base_url(raw, expected):
    assert canonicalize_custom_text_base_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "host/v1",
        "ftp://host/v1",
        "file:///tmp/provider",
        "https:///v1",
        "https://user:pass@host/v1",
        "https://host/v1?key=value",
        "https://host/v1#fragment",
    ],
)
def test_canonicalize_rejects_malformed_or_unsafe_urls(raw):
    with pytest.raises(ValueError):
        canonicalize_custom_text_base_url(raw)


@pytest.mark.parametrize(
    "provider_format, suffix",
    [
        ("openai", "/v1/chat/completions"),
        ("gemini", "/v1beta/models/gemini-pro:generateContent"),
        ("openai-responses", "/v1/chat/completions"),
        ("anthropic", "/v1/messages"),
    ],
)
def test_build_custom_text_upstream_url_is_format_aware(provider_format, suffix):
    assert build_custom_text_upstream_url("https://host/v1/chat/completions", provider_format) == f"https://host{suffix}"


def test_builders_avoid_double_suffixes():
    assert build_custom_text_upstream_url("https://host/v1", "openai") == "https://host/v1/chat/completions"
    assert build_custom_text_upstream_url("https://host/v1/messages", "anthropic") == "https://host/v1/messages"
    assert build_custom_models_probe_url("https://host/v1") == "https://host/v1/models"


def test_models_probe_preserves_nonterminal_vendor_prefix():
    assert build_custom_models_probe_url("https://host/v1beta/openai") == "https://host/v1beta/openai/v1/models"


def test_unknown_text_format_is_rejected():
    with pytest.raises(ValueError):
        build_custom_text_upstream_url("https://host", "openai-image")


def test_config_normalizer_canonicalizes_custom_text_connection_v1():
    source = {
        "providers": {
            "custom-openai": {
                "type": "custom",
                "format": "openai",
                "connections": [{"base_url": "https://host/v1"}],
            }
        }
    }

    normalized = normalize_custom_text_provider_urls(source)

    assert normalized["providers"]["custom-openai"]["connections"][0]["base_url"] == "https://host"


@pytest.mark.parametrize("provider_format", ["openai", "openai-responses", "anthropic", "gemini"])
def test_config_normalizer_supports_all_custom_text_formats(provider_format):
    source = {
        "providers": {
            provider_format: {
                "type": "custom",
                "format": provider_format,
                "connections": [{"base_url": "https://host/v1/chat/completions"}],
            }
        }
    }

    normalized = normalize_custom_text_provider_urls(source)

    assert normalized["providers"][provider_format]["connections"][0]["base_url"] == "https://host"


def test_config_normalizer_canonicalizes_legacy_provider_base_url():
    source = {
        "providers": {
            "legacy": {
                "type": "custom",
                "format": "anthropic",
                "base_url": "https://host/api/v1/messages",
            }
        }
    }

    normalized = normalize_custom_text_provider_urls(source)

    assert normalized["providers"]["legacy"]["base_url"] == "https://host/api"


def test_config_normalizer_leaves_builtin_provider_unchanged():
    source = {
        "providers": {
            "builtin": {
                "type": "builtin",
                "format": "openai",
                "base_url": "https://host/v1",
                "connections": [{"base_url": "https://connection/v1"}],
            }
        }
    }

    assert normalize_custom_text_provider_urls(source) == source


@pytest.mark.parametrize("provider_format", ["openai-image", "openai-video"])
def test_config_normalizer_leaves_custom_media_provider_unchanged(provider_format):
    source = {
        "providers": {
            "media": {
                "type": "custom",
                "format": provider_format,
                "base_url": "https://host/v1",
                "connections": [{"base_url": "https://connection/v1"}],
            }
        }
    }

    assert normalize_custom_text_provider_urls(source) == source


def test_config_normalizer_error_names_provider_and_connection():
    source = {
        "providers": {
            "broken": {
                "type": "custom",
                "format": "gemini",
                "connections": [{"name": "primary", "base_url": "not-a-url"}],
            }
        }
    }

    with pytest.raises(ValueError, match="Provider 'broken' connection 'primary' base_url"):
        normalize_custom_text_provider_urls(source)


def test_config_normalizer_does_not_mutate_original_input():
    source = {
        "providers": {
            "custom": {
                "type": "custom",
                "format": "openai-responses",
                "base_url": "https://provider/v1/responses",
                "connections": [
                    {"base_url": "https://valid/v1"},
                    {"base_url": "malformed"},
                ],
            }
        }
    }
    original = {
        "providers": {
            "custom": {
                "type": "custom",
                "format": "openai-responses",
                "base_url": "https://provider/v1/responses",
                "connections": [
                    {"base_url": "https://valid/v1"},
                    {"base_url": "malformed"},
                ],
            }
        }
    }

    with pytest.raises(ValueError):
        normalize_custom_text_provider_urls(source)

    assert source == original
