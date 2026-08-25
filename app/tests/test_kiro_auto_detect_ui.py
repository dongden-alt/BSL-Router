"""Tests for 9router-style Kiro auto-detect on modal open.

Covers:
- UI wiring: app.js fires the auto-import probe from showKiroModeSelector and
  targets the #kiro-mode-import-ide button.
- Probe endpoint contract: GET /api/oauth/kiro/auto-import returns found=true
  with source when a valid-format refreshToken is present in the SSO cache.
- Not-found path: empty/absent cache returns found=false with a "login to Kiro"
  hint.
"""
from __future__ import annotations

import json
import pathlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.main import app  # noqa: E402


def test_auto_detect_ui_wiring_present():
    """Source parity: showKiroModeSelector must fire the auto-import probe and
    restyle the #kiro-mode-import-ide button on a hit.
    """
    app_js = _ROOT / "app" / "static" / "app.js"
    source = app_js.read_text(encoding="utf-8")

    selector_idx = source.find("function showKiroModeSelector")
    assert selector_idx != -1, "showKiroModeSelector not found in app.js"
    # Scope to the function body (it is immediately followed by startKiroSocialAuth).
    end_idx = source.find("function startKiroSocialAuth", selector_idx)
    assert end_idx != -1, "Could not determine showKiroModeSelector function bounds"
    function_body = source[selector_idx:end_idx]

    assert "kiro/auto-import" in function_body, (
        "The /api/oauth/kiro/auto-import probe is not fired from "
        "showKiroModeSelector — the modal-open auto-detect is missing"
    )
    assert "kiro-mode-import-ide" in function_body, (
        "#kiro-mode-import-ide not found in showKiroModeSelector — the "
        "Import-from-IDE button is missing"
    )
    assert "var(--success)" in function_body, (
        "The detected-state restyle should use BSL's --success CSS variable "
        "for the green detection banner"
    )


def test_auto_import_probe_found(monkeypatch, tmp_path):
    """GET /api/oauth/kiro/auto-import returns found=true + source when the SSO
    cache holds a valid aorAAAAAG-format refreshToken.
    """
    cache_dir = tmp_path / ".aws" / "sso" / "cache"
    cache_dir.mkdir(parents=True)
    token_file = cache_dir / "kiro-auth-token.json"
    token_file.write_text(
        json.dumps({"refreshToken": "aorAAAAAG.example.refresh.token"}),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        pathlib.Path, "home", staticmethod(lambda: tmp_path)
    )

    with TestClient(app) as client:
        response = client.get("/api/oauth/kiro/auto-import")

    assert response.status_code == 200
    data = response.json()
    assert data["found"] is True
    assert data["refreshToken"] == "aorAAAAAG.example.refresh.token"
    assert data["source"] == "kiro-auth-token.json"


def test_auto_import_probe_not_found(monkeypatch, tmp_path):
    """GET /api/oauth/kiro/auto-import returns found=false with a login hint
    when no valid token exists in the cache.
    """
    monkeypatch.setattr(
        pathlib.Path, "home", staticmethod(lambda: tmp_path)
    )

    with TestClient(app) as client:
        response = client.get("/api/oauth/kiro/auto-import")

    assert response.status_code == 200
    data = response.json()
    assert data["found"] is False
    error = (data.get("error") or "").lower()
    assert "login to kiro" in error or "login to kiro ide" in error, (
        f"Expected a 'login to Kiro' hint in the not-found response, got: {data}"
    )
