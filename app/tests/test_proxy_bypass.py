"""D3 (2026-09-06): per-connection proxy_bypass flag — egress client selection.

WHY: connections routed via the MITM lane fail in two classes (SSL
self-signed-cert mistrust, upstream-unreachable — .brain/logs/ccpa_failures.jsonl).
A connection-level ``proxy_bypass: true`` must make the egress layer connect
DIRECT, eliminating both classes at once. The direct (no-proxy) hardened
client primitive already existed — _get_client_for_proxy(None) returns the
global hardened http_client — the flag just makes it reachable per connection.

Coverage:
  1. _connection_wants_proxy_bypass — validation-on-read coercion matrix
     (bool / hand-edited string spellings / numbers / garbage).
  2. _effective_egress_proxy — bypass suppresses the proxy (+ one [ProxyBypass]
     log line only when a proxy was actually suppressed); no flag passes the
     proxy through unchanged.
  3. Client-builder primitives — None resolves DIRECT (global http_client /
     verify=False ssl-disabled client cached under "").
  4. Wiring — all three egress selection sites (chat zone, images, videos)
     route through _effective_egress_proxy (source-anchored, per the
     inspect.getsource precedent in test_combo_budget.py).
  5. The flag survives resolve_active_connection (shallow-copy enrichment).
  6. update_config persists a connection carrying proxy_bypass (no stripping).
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import sys

import pytest
from starlette.requests import Request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import app.main as main
from app.utils.model_resolver import resolve_active_connection


# ── 1. Validation-on-read coercion ──────────────────────────────────────────

class TestProxyBypassCoercion:
    @pytest.mark.parametrize("raw,expected", [
        (True, True),
        (False, False),
        (None, False),
        ("true", True),
        ("True", True),
        ("TRUE", True),
        ("  yes  ", True),
        ("1", True),
        ("on", True),
        ("false", False),
        ("0", False),
        ("", False),
        ("junk", False),
        (1, True),
        (0, False),
        (2, True),
        (0.0, False),
        ([], False),
        ({}, False),
    ])
    def test_matrix(self, raw, expected):
        assert main._connection_wants_proxy_bypass({"proxy_bypass": raw}) is expected

    def test_missing_flag_is_false(self):
        assert main._connection_wants_proxy_bypass({"api_key": "sk-x"}) is False

    def test_none_connection_is_false(self):
        assert main._connection_wants_proxy_bypass(None) is False
        assert main._connection_wants_proxy_bypass({}) is False


# ── 2. Effective egress proxy ────────────────────────────────────────────────

class TestEffectiveEgressProxy:
    def test_bypass_suppresses_proxy_and_logs(self, capsys):
        out = main._effective_egress_proxy(
            {"proxy_url": "http://127.0.0.1:8080", "proxy_bypass": True}, "myprov"
        )
        assert out is None
        captured = capsys.readouterr()
        assert "[ProxyBypass]" in captured.out
        assert "myprov" in captured.out
        assert "http://127.0.0.1:8080" in captured.out

    def test_bypass_without_proxy_is_silent(self, capsys):
        out = main._effective_egress_proxy({"proxy_bypass": True})
        assert out is None
        assert "[ProxyBypass]" not in capsys.readouterr().out

    @pytest.mark.parametrize("raw", [True, "true", 1])
    def test_bypass_spellings_all_suppress(self, raw):
        conn = {"proxy_url": "http://p:1", "proxy_bypass": raw}
        assert main._effective_egress_proxy(conn, "p") is None

    def test_no_flag_passes_proxy_through(self, capsys):
        conn = {"proxy_url": "http://127.0.0.1:8080"}
        out = main._effective_egress_proxy(conn, "myprov")
        assert out == "http://127.0.0.1:8080"
        assert "[ProxyBypass]" not in capsys.readouterr().out

    def test_non_dict_connection_yields_none(self):
        assert main._effective_egress_proxy(None, "p") is None
        assert main._effective_egress_proxy({}, "p") is None


# ── 3. Direct (no-proxy) client primitives ──────────────────────────────────

class TestDirectClientPrimitives:
    def test_get_client_for_proxy_none_returns_global_direct_client(self, monkeypatch):
        sentinel = object()
        monkeypatch.setattr(main, "http_client", sentinel)
        assert main._get_client_for_proxy(None) is sentinel
        assert main._get_client_for_proxy("") is sentinel

    def test_get_client_for_proxy_url_builds_proxied_client(self, monkeypatch):
        built = []
        monkeypatch.setattr(
            main, "_build_hardened_client",
            lambda proxy_url=None, verify=True: built.append((proxy_url, verify)) or f"client@{proxy_url}",
        )
        monkeypatch.setattr(main, "_proxy_clients", {})
        assert main._get_client_for_proxy("http://mitm:8080") == "client@http://mitm:8080"
        assert built == [("http://mitm:8080", True)]
        # Cached: second call does not rebuild.
        assert main._get_client_for_proxy("http://mitm:8080") == "client@http://mitm:8080"
        assert built == [("http://mitm:8080", True)]

    def test_ssl_disabled_client_none_is_direct_and_unverified(self, monkeypatch):
        built = []
        monkeypatch.setattr(
            main, "_build_hardened_client",
            lambda proxy_url=None, verify=True: built.append((proxy_url, verify)) or f"client@{proxy_url}@{verify}",
        )
        monkeypatch.setattr(main, "_ssl_disabled_clients", {})
        out = main._get_ssl_disabled_client(None)
        assert built == [(None, False)]
        # Cache key "" so sibling direct ssl-disabled connections share it.
        assert main._ssl_disabled_clients.get("") is out

    def test_bypass_reaches_ssl_disabled_path(self, monkeypatch, capsys):
        """Composition: ssl_verify:false + proxy_bypass:true -> direct, unverified."""
        conn = {"proxy_url": "http://mitm:8080", "proxy_bypass": True}
        assert main._effective_egress_proxy(conn, "prov") is None
        built = []
        monkeypatch.setattr(
            main, "_build_hardened_client",
            lambda proxy_url=None, verify=True: built.append((proxy_url, verify)) or "c",
        )
        monkeypatch.setattr(main, "_ssl_disabled_clients", {})
        main._get_ssl_disabled_client(main._effective_egress_proxy(conn, "prov"))
        assert built == [(None, False)]
        assert list(main._ssl_disabled_clients) == [""]
        capsys.readouterr()


# ── 4. Egress wiring (source-anchored) ──────────────────────────────────────

class TestEgressWiring:
    def test_chat_zone_routes_through_effective_proxy(self):
        src = inspect.getsource(main._process_chat_completion)
        assert "_egress_proxy = _effective_egress_proxy(active_conn, provider_name)" in src
        assert "_get_ssl_disabled_client(_egress_proxy)" in src
        assert "_get_client_for_proxy(_egress_proxy)" in src
        # No bare proxy_url lookup may remain in the client-selection zone.
        assert '_get_client_for_proxy(active_conn.get("proxy_url"))' not in src
        assert '_get_ssl_disabled_client(active_conn.get("proxy_url"))' not in src

    def test_images_egress_routes_through_effective_proxy(self):
        src = inspect.getsource(main.images_generations)
        assert (
            "_get_client_for_proxy(_effective_egress_proxy(active_conn, provider_name))"
            in src
        )
        assert '_get_client_for_proxy(active_conn.get("proxy_url"))' not in src

    def test_videos_egress_routes_through_effective_proxy(self):
        src = inspect.getsource(main.videos_generations)
        assert (
            "_get_client_for_proxy(_effective_egress_proxy(active_conn, provider_name))"
            in src
        )
        assert '_get_client_for_proxy(active_conn.get("proxy_url"))' not in src


# ── 5. Flag survives connection resolution ──────────────────────────────────

class TestFlagFlowsThroughResolver:
    def test_resolve_active_connection_preserves_proxy_bypass(self):
        config = {
            "providers": {
                "prov": {
                    "format": "openai",
                    "connections": [
                        {
                            "api_key": "sk-1",
                            "proxy_url": "http://mitm:8080",
                            "proxy_bypass": True,
                            "enabled": True,
                        },
                    ],
                },
            },
        }
        conn, idx = resolve_active_connection(config, "prov", "m1")
        assert idx == 0
        assert conn["proxy_bypass"] is True
        assert conn["proxy_url"] == "http://mitm:8080"


# ── 6. Config save persistence ──────────────────────────────────────────────

def _request(path: str, body: dict) -> Request:
    encoded = json.dumps(body).encode("utf-8")
    delivered = False

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": encoded, "more_body": False}
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
            "scheme": "http",
            "server": ("127.0.0.1", 6969),
            "client": ("127.0.0.1", 50000),
        },
        receive,
    )


class TestConfigSavePersistsFlag:
    def test_update_config_keeps_proxy_bypass_on_connection(self, monkeypatch):
        import app.config_state as cs

        saved = cs.get_config()
        persisted = []
        monkeypatch.setattr(main, "_persist_config_snapshot", lambda cfg: persisted.append(cfg))
        try:
            body = {
                "providers": {
                    "mitmprov": {
                        "format": "openai",
                        "connections": [
                            {
                                "api_key": "sk-a",
                                "proxy_url": "http://mitm:8080",
                                "proxy_bypass": True,
                                "enabled": True,
                            },
                        ],
                    },
                },
                "admin": {"password_enabled": False, "password": "123456"},
            }
            resp = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
                main.update_config(_request("/api/config", body))
            )
            assert resp.status_code == 200
            assert persisted, "config snapshot must be persisted"
            conn = persisted[-1]["providers"]["mitmprov"]["connections"][0]
            assert conn.get("proxy_bypass") is True
            assert conn.get("proxy_url") == "http://mitm:8080"
        finally:
            cs.replace_config(saved)
