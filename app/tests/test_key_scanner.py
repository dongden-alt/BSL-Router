"""Tests for the provider key security scanner."""
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.security.key_scanner import (
    scan_single_key, scan_provider_config, ScanResult, Finding,
)


class TestScanSingleKey:
    def test_clean_key_clean_url(self):
        findings = scan_single_key("sk-clean-key", "https://api.openai.com/v1", "openai")
        assert len(findings) == 0

    def test_exfil_url_ngrok(self):
        findings = scan_single_key("sk-test", "https://evil.ngrok.io/v1", "openai")
        blocks = [f for f in findings if f.severity == "block"]
        assert len(blocks) > 0
        assert blocks[0].category == "exfil_url"

    def test_exfil_url_pastebin(self):
        findings = scan_single_key("sk-test", "https://pastebin.com/v1", "openai")
        blocks = [f for f in findings if f.severity == "block"]
        assert len(blocks) > 0

    def test_key_injection_shell(self):
        findings = scan_single_key("sk-test; rm -rf /", "https://api.openai.com/v1", "openai")
        blocks = [f for f in findings if f.severity == "block"]
        assert len(blocks) > 0
        assert blocks[0].category == "key_injection"

    def test_key_injection_xss(self):
        findings = scan_single_key("sk-test<script>alert(1)</script>", "https://api.openai.com/v1", "openai")
        blocks = [f for f in findings if f.severity == "block"]
        assert len(blocks) > 0

    def test_key_injection_sql(self):
        findings = scan_single_key("sk-test' OR 1=1 --", "https://api.openai.com/v1", "openai")
        blocks = [f for f in findings if f.severity == "block"]
        assert len(blocks) > 0

    def test_credential_harvesting_query_param(self):
        findings = scan_single_key("sk-test", "https://api.example.com/v1?api_key=steal", "openai")
        blocks = [f for f in findings if f.severity == "block"]
        assert len(blocks) > 0
        assert blocks[0].category == "credential_harvesting"

    def test_local_network_exfil(self):
        findings = scan_single_key("sk-test", "http://127.0.0.1:8080/v1", "openai")
        blocks = [f for f in findings if f.severity == "block"]
        assert len(blocks) > 0
        assert any(f.category == "local_network_exfil" for f in blocks)

    def test_insecure_http(self):
        findings = scan_single_key("sk-test", "http://api.example.com/v1", "openai")
        blocks = [f for f in findings if f.severity == "block"]
        assert len(blocks) > 0
        assert any(f.category == "insecure_transport" for f in blocks)

    def test_url_spoofing_subdomain(self):
        findings = scan_single_key("sk-test", "https://api.openai.com.evil.com/v1", "openai")
        warns = [f for f in findings if f.severity == "warn"]
        assert len(warns) > 0
        assert any(f.category == "url_spoofing" for f in warns)

    def test_clean_custom_https(self):
        findings = scan_single_key("sk-test", "https://my-proxy.example.com/v1", "openai")
        blocks = [f for f in findings if f.severity == "block"]
        assert len(blocks) == 0


class TestScanProviderConfig:
    def _make_clean_config(self):
        return {
            "providers": {
                "openai": {
                    "format": "openai",
                    "connections": [
                        {"name": "Primary", "api_key": "sk-openai-clean",
                         "base_url": "https://api.openai.com/v1", "enabled": True}
                    ],
                },
            },
        }

    def test_clean_config_passes(self):
        config = self._make_clean_config()
        result = scan_provider_config(config)
        assert result.passed is True
        assert len(result.findings) == 0

    def test_blocked_config_fails(self):
        config = {
            "providers": {
                "evil": {
                    "format": "openai",
                    "connections": [
                        {"name": "Exfil", "api_key": "sk-test",
                         "base_url": "https://evil.ngrok.io/v1", "enabled": True}
                    ],
                },
            },
        }
        result = scan_provider_config(config)
        assert result.passed is False
        assert any(f.severity == "block" for f in result.findings)

    def test_duplicate_keys_warning(self):
        config = {
            "providers": {
                "prov1": {
                    "format": "openai",
                    "connections": [
                        {"name": "C1", "api_key": "sk-same-key",
                         "base_url": "https://api.openai.com/v1", "enabled": True}
                    ],
                },
                "prov2": {
                    "format": "openai",
                    "connections": [
                        {"name": "C2", "api_key": "sk-same-key",
                         "base_url": "https://api.together.xyz/v1", "enabled": True}
                    ],
                },
            },
        }
        result = scan_provider_config(config)
        assert result.passed is True
        dup_findings = [f for f in result.findings if f.category == "duplicate_keys"]
        assert len(dup_findings) > 0

    def test_empty_config(self):
        result = scan_provider_config({})
        assert result.passed is True

    def test_summary_generated(self):
        config = self._make_clean_config()
        result = scan_provider_config(config)
        assert "PASSED" in result.summary
