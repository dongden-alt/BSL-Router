"""Unit tests for Cursor OAuth refresh path: import, mapping, refresh dispatch, and ensure_fresh_token integration.

Follows patterns from test_codex_kiro_oauth.py. All tests are offline (monkeypatched network/DB).
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest


_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from fastapi import HTTPException


# ── 1. _map_cursor passes refreshToken through ──────────────────────────────────


def test_map_cursor_passes_refresh_token():
    from app import oauth

    tokens = {"accessToken": "at", "refreshToken": "rt", "expiresIn": 3600, "machineId": "m1"}
    result = oauth._map_cursor(tokens)
    assert result["accessToken"] == "at"
    assert result["refreshToken"] == "rt"
    assert result["expiresIn"] == 3600
    assert result["providerSpecificData"]["machineId"] == "m1"


def test_map_cursor_refresh_token_none_when_absent():
    from app import oauth

    tokens = {"accessToken": "at", "expiresIn": 3600}
    result = oauth._map_cursor(tokens)
    assert result["refreshToken"] is None


# ── 2. _cursor_token_from_state_db reads refreshToken ──────────────────────────


def test_cursor_token_from_state_db_reads_refresh_token(tmp_path):
    from app import oauth
    import sqlite3

    db_path = tmp_path / "state.vscdb"
    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute("CREATE TABLE Item (key TEXT PRIMARY KEY, value TEXT)")
        # accessToken stored as JSON string
        conn.execute(
            "INSERT INTO Item (key, value) VALUES (?, ?)",
            ("cursorAuth/accessToken", json.dumps({"accessToken": "AT-123"})),
        )
        # refreshToken stored as plain string (some Cursor versions)
        conn.execute(
            "INSERT INTO Item (key, value) VALUES (?, ?)",
            ("cursorAuth/refreshToken", json.dumps({"refreshToken": "RT-456"})),
        )
        conn.execute(
            "INSERT INTO Item (key, value) VALUES (?, ?)",
            ("storage.serviceMachineId", json.dumps("MID-789")),
        )
    conn.close()

    cfg = {"tokenStoragePath": str(db_path)}
    result = oauth._cursor_token_from_state_db(cfg)
    assert result["accessToken"] == "AT-123"
    assert result["refreshToken"] == "RT-456"
    assert result["machineId"] == "MID-789"
    assert result["expiresIn"] > 0  # derived from JWT or fallback


def test_cursor_token_from_state_db_refresh_token_none_when_absent(tmp_path):
    from app import oauth
    import sqlite3

    db_path = tmp_path / "state.vscdb"
    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute("CREATE TABLE Item (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            "INSERT INTO Item (key, value) VALUES (?, ?)",
            ("cursorAuth/accessToken", json.dumps({"accessToken": "AT-ONLY"})),
        )
        conn.execute(
            "INSERT INTO Item (key, value) VALUES (?, ?)",
            ("storage.serviceMachineId", json.dumps("MID")),
        )
    conn.close()

    cfg = {"tokenStoragePath": str(db_path)}
    result = oauth._cursor_token_from_state_db(cfg)
    assert result["accessToken"] == "AT-ONLY"
    assert result["refreshToken"] is None


# ── 3. _cursor_refresh_token POSTs correct form ────────────────────────────────


def test_cursor_refresh_token_posts_correct_form():
    from app import oauth

    captured = {}

    async def fake_post_form(url, data, label):
        captured["url"] = url
        captured["data"] = data
        captured["label"] = label
        return {"access_token": "NEW-AT", "refresh_token": "NEW-RT", "expires_in": 7200}

    async def _run():
        return await oauth._cursor_refresh_token("OLD-RT")

    with patch.object(oauth, "_post_form", fake_post_form):
        result = asyncio.run(_run())

    assert captured["url"] == "https://api2.cursor.sh/oauth/token"
    assert captured["data"] == {
        "grant_type": "refresh_token",
        "client_id": "KbZUR41cY7W6zRSdpSUJ7I7mLYBKOCmB",
        "refresh_token": "OLD-RT",
    }
    assert captured["label"] == "Cursor token refresh"
    assert result["access_token"] == "NEW-AT"
    assert result["refresh_token"] == "NEW-RT"
    assert result["expires_in"] > 0


# ── 4. Rotation: new refresh_token returned; absent keeps original ─────────────


def test_cursor_refresh_token_rotation_new_token():
    from app import oauth

    async def fake_post_form(url, data, label):
        return {"access_token": "NEW-AT", "refresh_token": "ROTATED-RT"}

    async def _run():
        return await oauth._cursor_refresh_token("OLD-RT")

    with patch.object(oauth, "_post_form", fake_post_form):
        result = asyncio.run(_run())

    assert result["refresh_token"] == "ROTATED-RT"


def test_cursor_refresh_token_rotation_keeps_original():
    from app import oauth

    async def fake_post_form(url, data, label):
        return {"access_token": "NEW-AT"}  # no refresh_token in response

    async def _run():
        return await oauth._cursor_refresh_token("OLD-RT")

    with patch.object(oauth, "_post_form", fake_post_form):
        result = asyncio.run(_run())

    assert result["refresh_token"] == "OLD-RT"


# ── 5. should_logout true → HTTPException 401 ──────────────────────────────────


def test_cursor_refresh_token_should_logout_raises_401():
    from app import oauth

    async def fake_post_form(url, data, label):
        return {"should_logout": True}

    async def _run():
        return await oauth._cursor_refresh_token("OLD-RT")

    with patch.object(oauth, "_post_form", fake_post_form):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(_run())
        assert exc.value.status_code == 401
        assert "should_logout" in exc.value.detail


# ── 6. Missing access_token → HTTPException 502 ────────────────────────────────


def test_cursor_refresh_token_missing_access_token_raises_502():
    from app import oauth

    async def fake_post_form(url, data, label):
        return {"refresh_token": "RT"}  # no access_token

    async def _run():
        return await oauth._cursor_refresh_token("OLD-RT")

    with patch.object(oauth, "_post_form", fake_post_form):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(_run())
        assert exc.value.status_code == 502
        assert "no access token" in exc.value.detail


# ── 7. "cursor" registered in _REFRESH_DISPATCH ────────────────────────────────


def test_cursor_registered_in_refresh_dispatch():
    from app import oauth

    assert "cursor" in oauth._REFRESH_DISPATCH
    assert oauth._REFRESH_DISPATCH["cursor"] is oauth._cursor_refresh_token


# ── 8. ensure_fresh_token end-to-end with expired cursor connection ────────────


def test_ensure_fresh_token_cursor_end_to_end():
    from app import oauth

    fake_conn = {
        "api_key": "OLD-AT",
        "refresh_token": "RT-OLD",
        "expires_at": "2000-01-01T00:00:00+00:00",  # expired
    }

    async def fake_refresh(refresh_token, config=None):
        return {"access_token": "NEW-AT", "refresh_token": "RT-NEW", "expires_in": 3600}

    async def _run():
        return await oauth.ensure_fresh_token("cursor", fake_conn, {})

    with patch.dict(oauth._REFRESH_DISPATCH, {"cursor": fake_refresh}), \
         patch.object(oauth, "_update_connection_token") as updater, \
         patch.object(oauth, "_pace_refresh", new=AsyncMock()):
        new_token = asyncio.run(_run())

    assert new_token == "NEW-AT"
    updater.assert_called_once()
    # Verify the updater received the rotated refresh token
    call_kwargs = updater.call_args.kwargs if updater.call_args.kwargs else updater.call_args.args
    # _update_connection_token(provider, conn_id, new_at, new_rt, new_exp, extra=None)
    assert call_kwargs[3] == "RT-NEW" or call_kwargs["new_refresh_token"] == "RT-NEW"
