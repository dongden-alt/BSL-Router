"""Regression tests for Kiro refresh-on-import (2026-08-25 fix).

The 'bearer token invalid' trap: import-ide saved the IDE SSO cache's
accessToken as-is even when it expired weeks earlier, so the first Test/
request failed upstream. Now a stale cache (expires_in <= 120s) with a
refreshToken is refreshed ONCE before saving; a failed refresh raises 502
with an actionable hint instead of persisting a dead connection. Fresh
tokens keep the original zero-network guarantee.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from fastapi import HTTPException

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.main import app  # noqa: E402


def _stale_raw():
    return {
        "access_token": "dead-access-token",
        "refresh_token": "aorAAAAAG.live.refresh.token",
        "expires_in": 1,  # clamped expiry of an already-past expiresAt
        "_authMethod": "social",
    }


def _fresh_result():
    return {
        "access_token": "refreshed-access-token",
        "refresh_token": "aorAAAAAG.live.refresh.token",
        "expires_in": 3600,
        "_authMethod": "social",
        "_startUrl": None,  # setdefault("_startUrl", raw's value) in _kiro_refresh_if_stale
    }


def test_stale_cache_is_refreshed_before_save(monkeypatch):
    monkeypatch.setattr("app.oauth._kiro_token_from_sso_cache", lambda config: _stale_raw())
    refresh = AsyncMock(return_value=_fresh_result())
    monkeypatch.setattr("app.oauth._kiro_refresh_token", refresh)
    save = AsyncMock(return_value={"email": "t@t"})
    monkeypatch.setattr("app.oauth._save_kiro_tokens", save)

    with TestClient(app) as client:
        response = client.post("/api/oauth/kiro/import-ide")

    assert response.status_code == 200
    assert response.json()["success"] is True
    refresh.assert_awaited_once()
    save.assert_awaited_once_with(_fresh_result(), "social", "Kiro IDE")


def test_failed_refresh_never_persists_dead_token(monkeypatch):
    monkeypatch.setattr("app.oauth._kiro_token_from_sso_cache", lambda config: _stale_raw())
    refresh = AsyncMock(side_effect=HTTPException(status_code=502, detail="upstream rejected"))
    monkeypatch.setattr("app.oauth._kiro_refresh_token", refresh)
    save = AsyncMock(return_value={"email": "t@t"})
    monkeypatch.setattr("app.oauth._save_kiro_tokens", save)

    with TestClient(app) as client:
        response = client.post("/api/oauth/kiro/import-ide")

    assert response.status_code == 502
    detail = response.json().get("detail", "")
    assert "could not be refreshed" in detail
    assert "Builder ID" in detail  # points at the device-code login
    save.assert_not_awaited()


def test_fresh_cache_keeps_zero_network(monkeypatch):
    fresh = {"access_token": "live-access-token", "expiresIn": 3600, "_authMethod": "social"}
    monkeypatch.setattr("app.oauth._kiro_token_from_sso_cache", lambda config: fresh)
    refresh = AsyncMock(return_value=_fresh_result())
    monkeypatch.setattr("app.oauth._kiro_refresh_token", refresh)
    save = AsyncMock(return_value={"email": "t@t"})
    monkeypatch.setattr("app.oauth._save_kiro_tokens", save)

    with TestClient(app) as client:
        response = client.post("/api/oauth/kiro/import-ide")

    assert response.status_code == 200
    refresh.assert_not_awaited()  # zero-network preserved for live sessions
    save.assert_awaited_once()


def test_device_login_ui_is_primary(monkeypatch):
    """Source parity: the Kiro selector must present the device-code login as
    the primary action (btn-primary, listed before the IDE import)."""
    app_js = _ROOT / "app" / "static" / "app.js"
    source = app_js.read_text(encoding="utf-8")
    start = source.find("function showKiroModeSelector")
    end = source.find("function startKiroSocialAuth", start)
    body = source[start:end]

    builder_idx = body.find('id="kiro-mode-builder"')
    import_idx = body.find('id="kiro-mode-import-ide"')
    assert builder_idx != -1 and import_idx != -1
    assert builder_idx < import_idx, "Device-code login button must be listed first"
    # class= follows id= inside the opening tag — slice the WHOLE opening tag.
    builder_btn_start = body.rfind("<button", 0, builder_idx)
    builder_tag_end = body.find(">", builder_idx)
    builder_tag = body[builder_btn_start:builder_tag_end]
    assert "btn-primary" in builder_tag, (
        "The Builder-ID device login should be the primary-styled action"
    )
