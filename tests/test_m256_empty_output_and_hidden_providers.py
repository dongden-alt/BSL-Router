"""M256 Regression Tests: Universal Empty-Candidate Notice & Invisible (`hidden: true`) Provider Exclusion."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import app.main as main_mod
from app.compat.adapters.gemini import BSL_NO_OUTPUT_NOTICE
from app.security.key_scanner import scan_provider_config


def test_resolve_namespaced_model_excludes_hidden_explicit_namespace():
    fake_cfg = {
        "providers": {
            "secret-prov": {
                "hidden": True,
                "namespace": "secret.ns",
                "models": [{"id": "gpt-secret", "enabled": True}],
            },
            "public-prov": {
                "hidden": False,
                "namespace": "pub.ns",
                "models": [{"id": "gpt-public", "enabled": True}],
            },
        }
    }
    with patch.object(main_mod, "cs_get_config", return_value=fake_cfg):
        assert main_mod._resolve_namespaced_model("secret.ns/gpt-secret") == (None, None)
        assert main_mod._resolve_namespaced_model("secret.ns/unlisted-new-model") == (None, None)
        assert main_mod._resolve_namespaced_model("pub.ns/unlisted-new-model") == (
            "public-prov",
            "unlisted-new-model",
        )


def test_key_scanner_excludes_hidden_providers_by_default():
    cfg = {
        "providers": {
            "hidden-custom": {
                "hidden": True,
                "format": "openai",
                "connections": [{"name": "c0", "api_key": "sk-short", "base_url": "http://example.com"}],
            },
            "visible-clean": {
                "hidden": False,
                "format": "openai",
                "connections": [],
            },
        }
    }
    res_default = scan_provider_config(cfg)
    assert all(f.provider != "hidden-custom" for f in res_default.findings)

    res_explicit = scan_provider_config(cfg, include_hidden=True)
    assert any(f.provider == "hidden-custom" for f in res_explicit.findings)


def test_quota_status_excludes_hidden_providers_on_untargeted_scan():
    fake_cfg = {
        "providers": {
            "hidden-custom": {"hidden": True, "connections": []},
            "visible-prov": {"hidden": False, "connections": []},
        }
    }
    with patch.object(main_mod, "cs_get_config", return_value=fake_cfg):
        resp_all = asyncio.run(main_mod.get_quota_status_endpoint(provider=None, force=False))
        payload_all = json.loads(resp_all.body.decode("utf-8"))
        assert "hidden-custom" not in payload_all["providers"]
        assert "visible-prov" in payload_all["providers"]

        # Explicit operator lookup by provider ID still works
        resp_explicit = asyncio.run(main_mod.get_quota_status_endpoint(provider="hidden-custom", force=False))
        payload_explicit = json.loads(resp_explicit.body.decode("utf-8"))
        assert "hidden-custom" in payload_explicit["providers"]


def test_empty_output_exempt_helper():
    assert main_mod._is_empty_output_exempt("tab_jump_flash") is True
    assert main_mod._is_empty_output_exempt("tab-autocomplete") is True
    assert main_mod._is_empty_output_exempt("claude-opus-4-6-thinking") is False
    assert main_mod._is_empty_output_exempt("gemini-4-carbon-high") is False
    assert len(BSL_NO_OUTPUT_NOTICE) > 20
