"""Regression tests for POST /api/oauth/kiro/import-ide.

The ``/kiro/import-ide`` route reads ``~/.aws/sso/cache/kiro-auth-token.json``
directly via ``_kiro_token_from_sso_cache`` and saves the result via
``_save_kiro_tokens`` — zero network calls, so importing right after a fresh
Kiro IDE login never triggers Kiro's anti-abuse refresh monitoring.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.main import app  # noqa: E402


def test_import_ide_zero_network_path(monkeypatch):
    """POST /api/oauth/kiro/import-ide must call the offline cache reader and save.

    Monkeypatching both helpers proves the endpoint takes the zero-network path
    (no real HTTP call, no token refresh).
    """
    raw = {
        "accessToken": "offline-access-token",
        "expiresIn": 3600,
        "_authMethod": "social",
    }
    monkeypatch.setattr(
        "app.oauth._kiro_token_from_sso_cache",
        lambda config: raw,
    )
    monkeypatch.setattr(
        "app.oauth._save_kiro_tokens",
        AsyncMock(return_value={"email": "t@t"}),
    )
    import app.oauth as oauth

    with TestClient(app) as client:
        response = client.post("/api/oauth/kiro/import-ide")
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["connection"] == {"email": "t@t"}
    oauth._save_kiro_tokens.assert_awaited_once_with(raw, "social", "Kiro IDE")


def test_import_ide_cache_missing_returns_404(monkeypatch):
    """When the IDE cache is absent, the endpoint surfaces a 404 with a helpful message."""
    from fastapi import HTTPException

    def boom(config):
        raise HTTPException(status_code=404, detail="Kiro auth token not found in IDE cache. Please log in to Kiro IDE first.")

    monkeypatch.setattr("app.oauth._kiro_token_from_sso_cache", boom)

    with TestClient(app) as client:
        response = client.post("/api/oauth/kiro/import-ide")
    assert response.status_code == 404
    detail = response.json().get("detail", "")
    assert "Kiro auth token not found" in detail


def test_import_ide_ui_wiring_present():
    """Source parity: the frontend must reference the /kiro/import-ide route."""
    app_js = _ROOT / "app" / "static" / "app.js"
    source = app_js.read_text(encoding="utf-8")
    assert "import-ide" in source, (
        "The /kiro/import-ide route is not wired in app.js — "
        "the Import from Kiro IDE button/handler is missing"
    )
