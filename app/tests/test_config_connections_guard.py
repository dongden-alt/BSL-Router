"""Config save hardening: connections merge guard (lost-update protection).

THE INCIDENT (2026-08-25): two dashboard tabs (normal + incognito) each held a
full globalConfig snapshot; every save POSTs the WHOLE config, so a stale
tab's debounced autosave silently deleted connections added in the other tab.
User lost keys 011/013 on tabitoken/gorouter/seekai. update_config now treats
a save that would REMOVE connections (or empty a non-empty api_key) as stale
and restores them, unless the body carries an explicit
``_deleted_connection`` opt-out flag from a deliberate deleteConnection.

Mirrors the direct-call pattern of test_thinking_config_authority.py:
main.update_config(_request(...)) with a patched _persist_config_snapshot.
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import sys

import pytest
from starlette.requests import Request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import app.config_state as cs
import app.main as main


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


@pytest.fixture()
def isolated_config(monkeypatch):
    saved = cs.get_config()
    persisted = []
    monkeypatch.setattr(main, "_persist_config_snapshot", lambda cfg: persisted.append(cfg))
    yield persisted
    cs.replace_config(saved)


def _base_config() -> dict:
    """Provider X (tabitoken) with three connections, mirroring the incident."""
    return {
        "providers": {
            "tabitoken": {
                "format": "openai",
                "connections": [
                    {"api_key": "sk-011", "enabled": True},
                    {"api_key": "sk-012", "enabled": True},
                    {"api_key": "sk-013", "enabled": True},
                ],
            },
        },
        "admin": {"password_enabled": False, "password": "123456"},
    }


def test_stale_save_restores_removed_connections(isolated_config, capsys):
    """POST missing 2 of 3 connections (no delete flag) → both restored.

    This is the exact lost-update signature: a stale tab saving a snapshot
    taken before keys 012/013 existed. The genuinely-new body-only connection
    (sk-new) must survive the merge.
    """
    cs.replace_config(_base_config())
    body = copy.deepcopy(_base_config())
    body["providers"]["tabitoken"]["connections"] = [
        {"api_key": "sk-011", "enabled": True},
        {"api_key": "sk-new", "enabled": True},
    ]

    response = asyncio.run(main.update_config(_request("/api/config", body)))
    assert response.status_code == 200

    conns = cs.get_config()["providers"]["tabitoken"]["connections"]
    keys = [c["api_key"] for c in conns]
    assert "sk-012" in keys, "stale save must not delete an existing connection"
    assert "sk-013" in keys, "stale save must not delete an existing connection"
    assert "sk-new" in keys, "genuinely-new body connection must be preserved"
    assert "sk-011" in keys
    assert "[CONFIG-GUARD] provider=tabitoken restored 2 connections" in capsys.readouterr().out


def test_legit_delete_passes_with_flag(isolated_config):
    """POST with 2 conns + _deleted_connection flag → exactly 2, flag not persisted."""
    cs.replace_config(_base_config())
    body = copy.deepcopy(_base_config())
    body["providers"]["tabitoken"]["connections"] = [
        {"api_key": "sk-011", "enabled": True},
        {"api_key": "sk-013", "enabled": True},
    ]
    body["_deleted_connection"] = {"provider": "tabitoken", "api_key": "sk-012"}

    response = asyncio.run(main.update_config(_request("/api/config", body)))
    assert response.status_code == 200

    conns = cs.get_config()["providers"]["tabitoken"]["connections"]
    keys = [c["api_key"] for c in conns]
    assert keys == ["sk-011", "sk-013"], "deliberate delete must not be restored"
    # The opt-out flag is transport-only and must never reach config.yaml.
    assert isolated_config, "config must be persisted"
    assert "_deleted_connection" not in isolated_config[-1]
    assert "_deleted_connection" not in cs.get_config()


def test_stale_save_cannot_empty_api_key(isolated_config, capsys):
    """POST where conn[0].api_key='' → server key survives."""
    cs.replace_config(_base_config())
    body = copy.deepcopy(_base_config())
    body["providers"]["tabitoken"]["connections"] = [
        {"api_key": "sk-011", "enabled": True},
        {"api_key": "", "enabled": True},
        {"api_key": "sk-013", "enabled": True},
    ]

    response = asyncio.run(main.update_config(_request("/api/config", body)))
    assert response.status_code == 200

    conns = cs.get_config()["providers"]["tabitoken"]["connections"]
    assert len(conns) == 3, "emptied key must not fork the connection into two"
    assert conns[1]["api_key"] == "sk-012", "non-empty server key must survive an emptying save"
    assert "[CONFIG-GUARD] provider=tabitoken restored 1 emptied api_key" in capsys.readouterr().out


def test_noop_save_is_untouched(isolated_config, capsys):
    """Identical config POST → connections unchanged, guard stays silent."""
    # NOTE: replace_config encrypts secrets in place, so the body must be
    # built fresh (plaintext) — a deepcopy taken after replace_config would
    # carry encrypted keys and legitimately trip the guard.
    cs.replace_config(_base_config())
    body = _base_config()

    response = asyncio.run(main.update_config(_request("/api/config", body)))
    assert response.status_code == 200

    conns = cs.get_config()["providers"]["tabitoken"]["connections"]
    assert [c["api_key"] for c in conns] == ["sk-011", "sk-012", "sk-013"]
    assert "[CONFIG-GUARD]" not in capsys.readouterr().out, "no-op save must not trip the guard"
