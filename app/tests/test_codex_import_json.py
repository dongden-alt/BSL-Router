"""Security and runtime contract tests for Codex JSON OAuth import."""
from __future__ import annotations

import asyncio
import base64
import json
import math
import os
import sys
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
import httpx
from starlette.requests import Request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from fastapi import HTTPException  # noqa: E402

import app.oauth as oauth  # noqa: E402

pytestmark = pytest.mark.timeout(60)


def _jwt(payload: dict) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"{header}.{body}.sig"


def _request(payload=None, *, raw: bytes | None = None, content_length: str | None = "auto", chunks=None) -> Request:
    body = raw if raw is not None else json.dumps(payload).encode()
    headers = [(b"content-type", b"application/json")]
    if content_length == "auto":
        headers.append((b"content-length", str(len(body)).encode()))
    elif content_length is not None:
        headers.append((b"content-length", content_length.encode()))
    pending = list(chunks if chunks is not None else [body])

    async def receive():
        chunk = pending.pop(0) if pending else b""
        return {"type": "http.request", "body": chunk, "more_body": bool(pending)}

    return Request({"type": "http", "method": "POST", "path": "/api/oauth/codex/import-json", "headers": headers, "query_string": b""}, receive)


def _call(payload=None, **kwargs):
    return asyncio.run(oauth.codex_import_json(_request(payload, **kwargs)))


@pytest.fixture()
def isolated():
    import app.config_state as cs
    import app.main as main

    saved = cs.get_config()
    cs.replace_config({"providers": {"codex": {"type": "oauth", "connections": []}}, "admin": {"password_enabled": False}})
    try:
        with patch("app.main._persist_config_snapshot", return_value=None):
            yield cs
    finally:
        with patch("app.main._persist_config_snapshot", return_value=None):
            main._replace_runtime_config(saved)


def _rows(cs):
    return cs.get_config()["providers"]["codex"]["connections"]


def _native(access="at-native", refresh="rt-native", account="acct-native", **extra):
    return {"tokens": {"access_token": access, "refresh_token": refresh,
                       "id_token": _jwt({"email": "native@example.com"}), "account_id": account}, **extra}


def test_native_import_and_sanitized_response(isolated):
    result = _call(_native())
    row = _rows(isolated)[0]
    assert result["success"] and set(result["connection"]) == {"id", "provider", "email", "displayName"}
    assert row["api_key"] == "at-native" and row["refresh_token"] == "rt-native"
    assert row["provider_data"]["chatgptAccountId"] == "acct-native"
    assert row["email"] == "native@example.com" and row["enabled"] is True
    assert "at-native" not in json.dumps(result) and "rt-native" not in json.dumps(result)


def test_raw_id_token_and_token_duplicates_are_not_persisted(isolated):
    secret = _jwt({"email": "native@example.com", "sub": "SECRET-ID-TOKEN"})
    payload = _native()
    payload["tokens"]["id_token"] = secret
    _call(payload)
    data = _rows(isolated)[0]["provider_data"]
    assert not ({"id_token", "idToken", "access_token", "accessToken", "refresh_token", "refreshToken"} & data.keys())
    assert secret not in json.dumps(data)


@pytest.mark.parametrize("shape", [
    {"access_token": "at", "account_id": "acct"},
    {"accessToken": "at", "chatgptAccountId": "acct"},
    {"auth": {"tokens": {"access_token": "at", "accountId": "acct"}}},
])
def test_supported_shapes(shape, isolated):
    _call(shape)
    assert _rows(isolated)[0]["provider_data"]["chatgptAccountId"] == "acct"


def test_access_jwt_supplies_account_and_email(isolated):
    token = _jwt({"email": "access@example.com", "https://api.openai.com/auth": {"chatgpt_account_id": "acct-access", "chatgpt_plan_type": "plus"}})
    _call({"tokens": {"access_token": token}})
    row = _rows(isolated)[0]
    assert row["email"] == "access@example.com"
    assert row["provider_data"] == {"chatgptAccountId": "acct-access", "chatgptPlanType": "plus"}


def test_id_jwt_metadata_fallback(isolated):
    token = _jwt({"email": "id@example.com", "account_id": "acct-id", "plan_type": "team"})
    _call({"tokens": {"access_token": "at", "id_token": token}})
    row = _rows(isolated)[0]
    assert row["provider_data"]["chatgptAccountId"] == "acct-id"
    assert row["email"] == "id@example.com"


def test_explicit_account_wins_claim(isolated):
    token = _jwt({"account_id": "acct-claim"})
    _call({"tokens": {"access_token": token, "account_id": "acct-explicit"}})
    assert _rows(isolated)[0]["provider_data"]["chatgptAccountId"] == "acct-explicit"


@pytest.mark.parametrize("bad", [None, 3, True, {}, [], "   "])
def test_access_alias_requires_nonblank_string(bad, isolated):
    with pytest.raises(HTTPException) as error:
        _call({"tokens": {"access_token": bad, "account_id": "acct"}})
    assert error.value.status_code == 400 and _rows(isolated) == []


@pytest.mark.parametrize("field,bad", [
    ("refresh_token", None), ("id_token", {}), ("account_id", []),
    ("chatgptAccountId", 7), ("email", False), ("plan_type", {}),
])
def test_optional_alias_types_are_strict(field, bad, isolated):
    with pytest.raises(HTTPException) as error:
        _call({"access_token": "at", "account_id": "acct", field: bad})
    assert error.value.status_code == 400 and _rows(isolated) == []


@pytest.mark.parametrize("payload", [
    {"access_token": "one", "accessToken": "two", "account_id": "acct"},
    {"access_token": "one", "account_id": "acct", "tokens": {"access_token": "two"}},
    {"access_token": "one", "account_id": "acct", "auth": {"access_token": "two"}},
    {"access_token": "at", "account_id": "one", "tokens": {"accountId": "two"}},
    {"access_token": "at", "account_id": "acct", "expires_at": 1900000000, "tokens": {"expiresAt": 1900000001}},
])
def test_conflicting_aliases_wrappers_and_nested_values_rejected(payload, isolated):
    with pytest.raises(HTTPException) as error:
        _call(payload)
    assert error.value.status_code == 400 and _rows(isolated) == []


@pytest.mark.parametrize("account", ["bad\r\nInjected: yes", "bad\x00id", "bad\x7fid", "accént"])
def test_account_id_non_ascii_and_control_characters_rejected(account, isolated):
    with pytest.raises(HTTPException) as error:
        _call({"access_token": "at", "account_id": account})
    assert error.value.status_code == 400 and account not in error.value.detail and _rows(isolated) == []


@pytest.mark.parametrize("claim_source", ["access", "id"])
def test_metadata_derived_account_id_must_be_ascii_header_safe(claim_source, isolated):
    token = _jwt({"account_id": "accént"})
    payload = {"access_token": token if claim_source == "access" else "at"}
    if claim_source == "id":
        payload["id_token"] = token
    with pytest.raises(HTTPException) as error:
        _call(payload)
    assert error.value.status_code == 400 and "accént" not in error.value.detail and _rows(isolated) == []


@pytest.mark.parametrize("field,value", [
    ("access_token", "secret\r\nInjected: yes"),
    ("access_token", "sëcret"),
    ("refresh_token", "refresh\nsecret"),
    ("refresh_token", "réfresh"),
    ("id_token", "identity\x00secret"),
    ("id_token", "idéntity"),
])
def test_token_header_unsafe_characters_rejected_without_echo(field, value, isolated):
    payload = {"access_token": "at", "account_id": "acct", field: value}
    with pytest.raises(HTTPException) as error:
        _call(payload)
    assert error.value.status_code == 400 and value not in error.value.detail and _rows(isolated) == []


def test_benign_token_outer_spaces_are_trimmed(isolated):
    _call({"access_token": "  access  ", "refresh_token": " refresh ", "account_id": "acct"})
    row = _rows(isolated)[0]
    assert row["api_key"] == "access" and row["refresh_token"] == "refresh"


def test_missing_account_rejected(isolated):
    with pytest.raises(HTTPException) as error:
        _call({"access_token": "at"})
    assert error.value.status_code == 400 and "account ID" in error.value.detail


def test_invalid_account_claim_type_rejected(isolated):
    with pytest.raises(HTTPException) as error:
        _call({"access_token": _jwt({"account_id": {"bad": True}})})
    assert error.value.status_code == 400


@pytest.mark.parametrize("value", ["2030-03-17T12:00:00Z", "2030-03-17T12:00:00+02:00", 1900000000, 1900000000000])
def test_expiry_iso_seconds_and_milliseconds_normalized(value, isolated):
    _call({"access_token": "at", "account_id": "acct", "expires_at": value})
    stored = datetime.fromisoformat(_rows(isolated)[0]["expires_at"])
    assert stored.tzinfo is not None and stored.astimezone(timezone.utc).year == 2030


def test_cliproxy_expired_iso_supported(isolated):
    _call({"access_token": "at", "account_id": "acct", "expired": "2030-03-17T12:00:00Z"})
    assert _rows(isolated)[0]["expires_at"].startswith("2030-03-17T12:00:00")


def test_access_jwt_exp_precedes_id_jwt_exp(isolated):
    access = _jwt({"account_id": "acct", "exp": 1900000000})
    identity = _jwt({"exp": 2000000000})
    _call({"access_token": access, "id_token": identity})
    assert _rows(isolated)[0]["expires_at"].startswith("2030-03-17")


@pytest.mark.parametrize("field,value", [
    ("expires_at", "2030-01-01T00:00:00"), ("expires_at", True),
    ("expires_at", math.inf), ("expires_at", 10**100),
    ("expires_in", None), ("expires_in", math.nan), ("expires_in", math.inf),
    ("expires_in", 10**400),
])
def test_invalid_expiry_rejected(field, value, isolated):
    with pytest.raises(HTTPException) as error:
        _call({"access_token": "at", "account_id": "acct", field: value})
    assert error.value.status_code == 400 and _rows(isolated) == []


def test_huge_duration_direct_api_is_generic_400_without_mutation_or_secret_echo(isolated):
    secret = "huge-duration-secret"
    with pytest.raises(HTTPException) as error:
        _call({"access_token": secret, "account_id": "acct", "expires_in": 10**400})
    assert error.value.status_code == 400
    assert secret not in error.value.detail
    assert _rows(isolated) == []


def test_expired_without_refresh_rejected(isolated):
    with pytest.raises(HTTPException) as error:
        _call({"access_token": "at", "account_id": "acct", "expires_at": 1})
    assert error.value.status_code == 400 and _rows(isolated) == []


def test_expired_with_refresh_saved_and_refresh_consumer_compatible(isolated, monkeypatch):
    _call({"access_token": "old", "refresh_token": "refresh", "account_id": "acct", "expires_at": 1})
    row = _rows(isolated)[0]
    refresh = AsyncMock(return_value={"access_token": "new", "expires_in": 3600})
    monkeypatch.setitem(oauth._REFRESH_DISPATCH, "codex", refresh)
    monkeypatch.setattr(oauth, "_pace_refresh", AsyncMock())
    monkeypatch.setattr(oauth, "_update_connection_token", lambda *args, **kwargs: None)
    assert asyncio.run(oauth.ensure_fresh_token("codex", row, {})) == "new"
    refresh.assert_awaited_once()


@pytest.mark.parametrize("raw", [b'{"access_token":"at","account_id":"acct"', b'[1]', b'\xff', b'{"a":1,"a":2}'])
def test_malformed_nonobject_utf8_and_duplicate_json_rejected(raw, isolated):
    with pytest.raises(HTTPException) as error:
        _call(raw=raw)
    assert error.value.status_code == 400 and _rows(isolated) == []


def test_stream_cap_without_content_length(isolated):
    chunks = [b'{"x":"', b"a" * (256 * 1024), b'"}']
    with pytest.raises(HTTPException) as error:
        _call(content_length=None, chunks=chunks)
    assert error.value.status_code == 413 and _rows(isolated) == []


def test_fake_small_content_length_cannot_bypass_stream_cap(isolated):
    with pytest.raises(HTTPException) as error:
        _call(content_length="2", raw=b" " * (256 * 1024 + 1))
    assert error.value.status_code == 413


def test_declared_oversize_rejected_before_stream(isolated):
    with pytest.raises(HTTPException) as error:
        _call(content_length=str(300 * 1024), raw=b"{}")
    assert error.value.status_code == 413


def test_deep_json_and_nonfinite_json_rejected_without_500(isolated):
    nested = b'{"a":' * 40 + b"{}" + b"}" * 40
    for raw in (nested, b'{"access_token":"at","account_id":"acct","expires_in":NaN}'):
        with pytest.raises(HTTPException) as error:
            _call(raw=raw)
        assert error.value.status_code == 400


def test_no_network_on_ordinary_import(isolated):
    with patch("httpx.AsyncClient", side_effect=AssertionError("network used")):
        _call(_native())


def test_duplicate_email_upsert_policy_preserved(isolated):
    first = _call(_native(access="one"))["connection"]["id"]
    second = _call(_native(access="two"))["connection"]["id"]
    assert len(_rows(isolated)) == 1 and first == second and _rows(isolated)[0]["api_key"] == "two"


def test_real_config_state_encrypts_tokens_but_not_id_token(isolated):
    import app.config_state as cs
    _call(_native(access="SECRET-ACCESS", refresh="SECRET-REFRESH"))
    raw = json.dumps(cs._config)
    assert "SECRET-ACCESS" not in raw and "SECRET-REFRESH" not in raw
    assert "SECRET-ID-TOKEN" not in raw and "id_token" not in raw


def test_chatgpt_account_header_contract_from_import(isolated):
    _call(_native(account="acct-header"))
    row = _rows(isolated)[0]
    headers = {}
    account = row["provider_data"].get("chatgptAccountId")
    if account:
        headers["ChatGPT-Account-ID"] = str(account)
    built = httpx.Headers({**headers, "Authorization": f"Bearer {row['api_key']}"})
    assert dict(built) == {"chatgpt-account-id": "acct-header", "authorization": "Bearer at-native"}


def test_persist_failure_rolls_back(isolated):
    import app.main as main
    with patch.object(main, "_replace_runtime_config", side_effect=OSError("disk full")):
        with pytest.raises(HTTPException) as error:
            _call(_native())
    assert error.value.status_code == 500 and _rows(isolated) == []


def test_endpoint_requires_admin_session_when_password_enabled(isolated):
    import app.config_state as cs
    from fastapi.testclient import TestClient
    import app.main as main

    with TestClient(main.app) as client:
        cs.replace_config({"providers": {"codex": {"type": "oauth", "connections": []}}, "admin": {"password_enabled": True, "password": "123456"}})
        response = client.post("/api/oauth/codex/import-json", json=_native())
    assert response.status_code == 401 and response.json().get("auth_required") is True
