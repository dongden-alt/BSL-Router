"""Ox Alpha reasoning-effort contract (2026-08-24)."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.compat.families import resolve_thinking
from app.middleware.thinking_fallback import is_thinking_param_rejection


class TestOxAlphaCapture:
    def test_vsllm_low(self):
        p = resolve_thinking({}, "vsllm-o/stealth/ox-alpha", "low")[0]
        assert p["reasoning_effort"] == "low"

    def test_opencode_zen_high(self):
        p = resolve_thinking({}, "opencode-zen/x-preview-f-free", "high")[0]
        assert p["reasoning_effort"] == "high"


class TestOxAlphaNonCapture:
    def test_qwen_preview(self):
        _, prov = resolve_thinking({}, "qwen/qwen3.6-max-preview", "high")
        assert not any(r.contract_id == "ox-alpha" for r in prov.records)

    def test_qwen38_preview(self):
        _, prov = resolve_thinking({}, "qwen3.8-max-preview", "high")
        assert not any(r.contract_id == "ox-alpha" for r in prov.records)

    def test_alibaba_qwen(self):
        _, prov = resolve_thinking({}, "alibaba/qwen3-max-preview", "high")
        assert not any(r.contract_id == "ox-alpha" for r in prov.records)

    def test_tencent_hy3(self):
        _, prov = resolve_thinking({}, "tencent/hy3-preview", "high")
        assert not any(r.contract_id == "ox-alpha" for r in prov.records)


class TestOxAlphaVocab:
    def test_medium(self):
        p = resolve_thinking({}, "vsllm-o/stealth/ox-alpha", "medium")[0]
        assert p["reasoning_effort"] == "medium"

    def test_xhigh_to_max(self):
        p = resolve_thinking({}, "vsllm-o/stealth/ox-alpha", "xhigh")[0]
        assert p["reasoning_effort"] == "max"

    def test_garbage_to_max(self):
        p = resolve_thinking({}, "vsllm-o/stealth/ox-alpha", "banana")[0]
        assert p["reasoning_effort"] == "max"

    def test_budget_32k_to_max(self):
        p = resolve_thinking({}, "vsllm-o/stealth/ox-alpha", "32k")[0]
        assert p["reasoning_effort"] == "max"

    def test_budget_16k_to_medium(self):
        p = resolve_thinking({}, "vsllm-o/stealth/ox-alpha", "16k")[0]
        assert p["reasoning_effort"] == "medium"


class TestOxAlphaAutoOff:
    def test_auto_no_field(self):
        p = resolve_thinking({}, "vsllm-o/stealth/ox-alpha", "auto")[0]
        assert "reasoning_effort" not in p

    def test_off_no_field(self):
        p = resolve_thinking({}, "vsllm-o/stealth/ox-alpha", "off")[0]
        assert "reasoning_effort" not in p


class TestOxAlphaFallback:
    def test_compact_1210(self):
        assert is_thinking_param_rejection(
            400, '{"error":{"code":1210,"message":"unsupported reasoning config"}}'
        ) is True

    def test_spaced_1210(self):
        assert is_thinking_param_rejection(
            400, '{"error":{"code": 1210,"message":"unsupported reasoning config"}}'
        ) is True

    def test_unrelated_400(self):
        assert is_thinking_param_rejection(
            400, '{"error":{"code":401,"message":"bad key"}}'
        ) is False


class TestOxAlphaAnthropicWire:
    def test_strip(self):
        payload = {"reasoning_effort": "high", "model": "x"}
        out, _ = resolve_thinking(
            payload,
            "vsllm-o/stealth/ox-alpha",
            "high",
            wire_format="anthropic",
        )
        assert "reasoning_effort" not in out
