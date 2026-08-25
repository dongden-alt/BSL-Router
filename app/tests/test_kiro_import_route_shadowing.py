"""Route-shadowing regression test for POST /api/oauth/kiro/import.

The dedicated ``/kiro/import`` route used to be registered AFTER the generic
``/{provider}/import`` route. Because FastAPI matches routes in registration
order, ``POST /api/oauth/kiro/import`` was always caught by the generic route,
whose flow-type gate rejected provider ``kiro`` (flowType ``device_code``) with
``This provider does not support native token import``.

The fix moves the dedicated route above the generic one. These tests prove the
dedicated route now answers, and that the generic gate still works for providers
that genuinely do not support native token import.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.main import app  # noqa: E402


def test_kiro_import_reaches_dedicated_route():
    """POST /api/oauth/kiro/import must hit the dedicated route, not the generic gate.

    The dedicated route validates the refreshToken format and rejects a bad token
    with ``Invalid token format``. If the generic route still shadows it, the
    response would instead be ``This provider does not support native token import``.
    """
    with TestClient(app) as client:
        response = client.post(
            "/api/oauth/kiro/import",
            json={"refreshToken": "bad-token"},
        )
    assert response.status_code == 400
    detail = response.json().get("detail", "")
    assert "Invalid token format" in detail, (
        f"Expected dedicated-route error, got: {detail!r} — "
        "the /kiro/import route is still shadowed by the generic /{{provider}}/import route"
    )


def test_generic_import_gate_still_rejects_non_import_providers():
    """POST /api/oauth/claude/import must still be rejected by the generic gate.

    ``claude`` is not an ``import_token`` provider, so the generic route's gate
    must continue to return ``does not support native token import``.
    """
    with TestClient(app) as client:
        response = client.post(
            "/api/oauth/claude/import",
            json={"refreshToken": "any-token"},
        )
    assert response.status_code == 400
    detail = response.json().get("detail", "")
    assert "does not support native token import" in detail
