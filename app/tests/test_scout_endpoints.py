"""
Tests for scout endpoints, resolver metadata injection, and canonical URL integration.
"""
import pytest
from app.utils.model_resolver import resolve_model_conn, _choose_connection_for_model
from app.utils.url_normalization import build_custom_text_upstream_url
from app.middleware.compaction import apply_compaction
from app.scouts.vision import _resolve_vision_candidates
from app.models import ChatCompletionRequest, Message


def _make_config():
    return {
        "providers": {
            "prov-openai": {
                "format": "openai",
                "connections": [
                    {"base_url": "https://api.openai.com/v1", "api_key": "sk-openai", "enabled": True}
                ],
                "models": [{"id": "model-openai", "enabled": True}]
            },
            "prov-anthropic": {
                "format": "anthropic",
                "connections": [
                    {"base_url": "https://api.anthropic.com", "api_key": "sk-anthropic", "enabled": True}
                ],
                "models": [{"id": "model-anthropic", "enabled": True}]
            },
            "prov-override": {
                "format": "openai",
                "connections": [
                    {
                        "base_url": "https://override.host",
                        "api_key": "sk-override",
                        "enabled": True,
                        "format": "anthropic"  # connection-level override
                    }
                ],
                "models": [{"id": "model-override", "enabled": True}]
            }
        },
        "aliases": {
            "alias-openai": {"provider": "prov-openai", "model": "model-openai"},
            "alias-anthropic": {"provider": "prov-anthropic", "model": "model-anthropic"}
        },
        "combos": [
            {
                "alias": "combo-mix",
                "chain": [
                    {"provider": "prov-anthropic", "model": "model-anthropic"},
                    {"provider": "prov-openai", "model": "model-openai"}
                ]
            }
        ]
    }


def test_resolver_injects_format_and_type_without_mutation():
    config = _make_config()
    
    # 1. Alias lookup
    conn, model = resolve_model_conn(config, "alias-openai")
    assert conn is not None
    assert conn.get("format") == "openai"
    assert conn.get("type") is None  # not configured
    
    # Verify the original config is NOT mutated (format is not attached to original conn dict)
    original_conn = config["providers"]["prov-openai"]["connections"][0]
    assert "format" not in original_conn

    # 2. Combo lookup
    conn, model = resolve_model_conn(config, "combo-mix")
    assert conn is not None
    # First chain entry is prov-anthropic
    assert conn.get("format") == "anthropic"
    assert model == "model-anthropic"

    # 3. Connection-level override
    conn, model = resolve_model_conn(config, "model-override")
    assert conn is not None
    assert conn.get("format") == "anthropic"  # overridden from provider's openai default


def test_double_suffix_normalization_in_canonical_builder():
    # Verify our underlying builder handles user-pasted endpoints correctly
    assert build_custom_text_upstream_url("https://example.com/v1/chat/completions", "openai") == "https://example.com/v1/chat/completions"
    assert build_custom_text_upstream_url("https://example.com/chat/completions", "openai") == "https://example.com/v1/chat/completions"
    assert build_custom_text_upstream_url("https://example.com/v1beta/api", "openai") == "https://example.com/v1beta/api/v1/chat/completions"
def test_vision_candidate_filtering():
    config = _make_config()
    # The scout now supports both OpenAI and Anthropic formats.
    # Both model-anthropic and model-openai should be resolved.
    candidates = _resolve_vision_candidates(config, "combo-mix")
    # chain has [model-anthropic (kept), model-openai (kept)]
    assert len(candidates) == 2
    # First candidate: Anthropic format
    conn0, model0, prov0 = candidates[0]
    assert prov0 == "prov-anthropic"
    assert model0 == "model-anthropic"
    assert conn0.get("format") == "anthropic"
    # Second candidate: OpenAI format
    conn1, model1, prov1 = candidates[1]
    assert prov1 == "prov-openai"
    assert model1 == "model-openai"
    assert conn1.get("format") == "openai"
