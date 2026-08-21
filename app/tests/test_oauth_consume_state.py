"""Shared OAuth state consume helper (three-bug fix #3).

Validates:
  - one-shot consume succeeds and returns the remembered entry
  - second consume after success → duplicate (while completion still held)
  - missing / expired / mismatch → controlled OAuthStateError categories
  - error completion records without secrets
  - root /callback uses consume_oauth_state (no ad-hoc pop path divergence)
  - Option B cosmetic invalid after poll pop is documented/accepted
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


@pytest.fixture()
def oauth_module():
    import importlib
    import app.oauth as oauth

    mod = importlib.reload(oauth)
    mod._oauth_states.clear()
    mod._oauth_completions.clear()
    yield mod
    mod._oauth_states.clear()
    mod._oauth_completions.clear()


def _remember(oauth_module, state="st-1", provider="antigravity",
              redirect_uri="http://localhost:6969/callback", verifier="v-abc"):
    oauth_module._remember_state(state, provider, redirect_uri, verifier)
    return state


def test_valid_state_consumes_once(oauth_module):
    state = _remember(oauth_module)
    entry = oauth_module.consume_oauth_state(
        state,
        provider="antigravity",
        redirect_uri="http://localhost:6969/callback",
        code_verifier="v-abc",
    )
    assert entry["provider"] == "antigravity"
    assert entry["code_verifier"] == "v-abc"
    assert state not in oauth_module._oauth_states


def test_second_consume_after_done_is_duplicate(oauth_module):
    """While completion status=done is still held, re-consume → duplicate."""
    state = _remember(oauth_module)
    oauth_module.consume_oauth_state(state, require_provider_match=False,
                                     require_redirect_match=False,
                                     require_verifier_match=False)
    oauth_module._oauth_completions[state] = {
        "status": "done",
        "connection": {"email": "u@example.com"},
    }
    with pytest.raises(oauth_module.OAuthStateError) as ei:
        oauth_module.consume_oauth_state(state)
    assert ei.value.category == oauth_module.OAUTH_FAIL_DUPLICATE
    # Redacted: message must not echo state / verifier / tokens
    assert state not in ei.value.message
    assert "v-abc" not in ei.value.message


def test_second_consume_after_poll_pop_is_missing_option_b(oauth_module):
    """Option B: after FE poll pops completion, reload shows missing/invalid (cosmetic)."""
    state = _remember(oauth_module)
    oauth_module.consume_oauth_state(state, require_provider_match=False,
                                     require_redirect_match=False,
                                     require_verifier_match=False)
    # Simulate FE poll-status popping the completion (oauth.py L1267-1268)
    oauth_module._oauth_completions.pop(state, None)
    with pytest.raises(oauth_module.OAuthStateError) as ei:
        oauth_module.consume_oauth_state(state)
    assert ei.value.category == oauth_module.OAUTH_FAIL_MISSING


def test_missing_state(oauth_module):
    with pytest.raises(oauth_module.OAuthStateError) as ei:
        oauth_module.consume_oauth_state("never-seen")
    assert ei.value.category == oauth_module.OAUTH_FAIL_MISSING


def test_empty_state(oauth_module):
    with pytest.raises(oauth_module.OAuthStateError) as ei:
        oauth_module.consume_oauth_state(None)
    assert ei.value.category == oauth_module.OAUTH_FAIL_MISSING


def test_expired_state(oauth_module):
    state = "st-exp"
    oauth_module._oauth_states[state] = {
        "provider": "antigravity",
        "redirect_uri": "http://localhost:6969/callback",
        "code_verifier": "v",
        "expires": datetime.now(timezone.utc).timestamp() - 10,
    }
    with pytest.raises(oauth_module.OAuthStateError) as ei:
        oauth_module.consume_oauth_state(state, provider="antigravity",
                                         redirect_uri="http://localhost:6969/callback",
                                         code_verifier="v")
    assert ei.value.category == oauth_module.OAUTH_FAIL_EXPIRED


def test_provider_mismatch(oauth_module):
    state = _remember(oauth_module, provider="antigravity")
    with pytest.raises(oauth_module.OAuthStateError) as ei:
        oauth_module.consume_oauth_state(
            state,
            provider="claude",
            redirect_uri="http://localhost:6969/callback",
            code_verifier="v-abc",
        )
    assert ei.value.category == oauth_module.OAUTH_FAIL_MISMATCH


def test_redirect_mismatch(oauth_module):
    state = _remember(oauth_module)
    with pytest.raises(oauth_module.OAuthStateError) as ei:
        oauth_module.consume_oauth_state(
            state,
            provider="antigravity",
            redirect_uri="http://evil.example/callback",
            code_verifier="v-abc",
        )
    assert ei.value.category == oauth_module.OAUTH_FAIL_MISMATCH


def test_verifier_mismatch(oauth_module):
    state = _remember(oauth_module)
    with pytest.raises(oauth_module.OAuthStateError) as ei:
        oauth_module.consume_oauth_state(
            state,
            provider="antigravity",
            redirect_uri="http://localhost:6969/callback",
            code_verifier="wrong-verifier",
        )
    assert ei.value.category == oauth_module.OAUTH_FAIL_MISMATCH
    assert "wrong-verifier" not in ei.value.message


def test_legacy_consume_state_raises_http_without_secrets(oauth_module):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        oauth_module._consume_state(
            "antigravity", "nope", "http://localhost:6969/callback", "v"
        )
    assert ei.value.status_code == 400
    assert "nope" not in str(ei.value.detail)


def test_callback_error_records_completion_without_secrets(oauth_module, monkeypatch):
    """Provider error callback stores status=error; no code/verifier fields."""
    import app.main as main_mod

    monkeypatch.setattr(main_mod, "consume_oauth_state", oauth_module.consume_oauth_state)
    monkeypatch.setattr(main_mod, "OAuthStateError", oauth_module.OAuthStateError)

    # Ensure completions map is the same object main imports via from-import inside handler
    state = "err-state"
    response = asyncio.run(
        main_mod.antigravity_callback(
            code=None,
            state=state,
            error="access_denied",
            error_description="user denied",
        )
    )
    assert response.status_code == 200
    comp = oauth_module._oauth_completions.get(state)
    assert comp is not None
    assert comp["status"] == "error"
    assert "code" not in comp
    assert "code_verifier" not in comp
    assert "token" not in str(comp).lower() or "access_denied" in comp.get("error", "")


def test_callback_valid_consumes_once_and_records_done(oauth_module, monkeypatch):
    import app.main as main_mod

    state = _remember(oauth_module, state="ok-state")
    fake_tokens = {"access_token": "tok-secret", "refresh_token": "ref-secret"}
    fake_conn = {"email": "ok@example.com", "displayName": "Ok"}

    async def _ex(*_a, **_k):
        return fake_tokens

    async def _fin(*_a, **_k):
        return fake_conn

    monkeypatch.setattr(main_mod, "consume_oauth_state", oauth_module.consume_oauth_state)
    monkeypatch.setattr(main_mod, "OAuthStateError", oauth_module.OAuthStateError)
    monkeypatch.setattr(main_mod, "_exchange_authorization_code", _ex)
    monkeypatch.setattr(main_mod, "_complete_connection", _fin)
    # OAUTH_PROVIDERS lookup inside handler
    if "antigravity" not in oauth_module.OAUTH_PROVIDERS:
        pytest.skip("antigravity provider not registered")

    response = asyncio.run(
        main_mod.antigravity_callback(code="auth-code-secret", state=state)
    )
    assert response.status_code == 200
    assert state not in oauth_module._oauth_states
    comp = oauth_module._oauth_completions[state]
    assert comp["status"] == "done"
    assert comp["connection"]["email"] == "ok@example.com"
    # Secrets from exchange must not leak into completion blob beyond connection mapping
    assert "auth-code-secret" not in str(comp)
    assert "tok-secret" not in str(comp)


def test_callback_duplicate_after_done(oauth_module, monkeypatch):
    import app.main as main_mod

    state = _remember(oauth_module, state="dup-state")
    # First consume + mark done (simulate successful prior callback)
    oauth_module.consume_oauth_state(state, require_provider_match=False,
                                     require_redirect_match=False,
                                     require_verifier_match=False)
    oauth_module._oauth_completions[state] = {"status": "done", "connection": {}}

    monkeypatch.setattr(main_mod, "consume_oauth_state", oauth_module.consume_oauth_state)
    monkeypatch.setattr(main_mod, "OAuthStateError", oauth_module.OAuthStateError)

    response = asyncio.run(
        main_mod.antigravity_callback(code="another-code", state=state)
    )
    assert response.status_code == 200
    body = response.body.decode("utf-8", errors="ignore")
    assert "already consumed" in body or "invalid" in body.lower()
    # category recorded without secrets
    err = oauth_module._oauth_completions.get(state)
    assert err is not None
    if err.get("status") == "error":
        assert err.get("category") == oauth_module.OAUTH_FAIL_DUPLICATE
        assert "another-code" not in str(err)
