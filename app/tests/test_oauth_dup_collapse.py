"""OAuth duplicate-connection collapse regression (Bug 2, 2026-09-16).

ROOT CAUSE: configs accumulated duplicate connection rows sharing one email
(oauth login) or one id (antigravity had 31x id ``nWkQqW6RZBPwblBG``, same
email). ``_save_connection`` only replaced the FIRST email match and broke;
``_update_connection_token`` only refreshed the FIRST id match. Round-robin
then picked the stale duplicates and served 401s.

FIX: ``_save_connection`` collapses ALL email matches into the freshest row
(by ``expires_at``, preserving that row's id) and deletes the rest;
``_update_connection_token`` refreshes EVERY row sharing the id.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


@pytest.fixture()
def oauth_and_cs():
    import app.oauth as oauth
    import app.config_state as cs
    import app.main as main
    from unittest.mock import patch

    saved = cs.get_config()
    cs.replace_config({
        "providers": {
            "prov-oauth": {"type": "oauth", "connections": []},
        },
        "admin": {"password_enabled": False, "password": "123456"},
    })
    try:
        with patch("app.main._persist_config_snapshot", return_value=None):
            yield oauth, cs, main
    finally:
        with patch("app.main._persist_config_snapshot", return_value=None):
            main._replace_runtime_config(saved)


def _seed(oauth_and_cs, connections):
    oauth, cs, _main = oauth_and_cs
    cs.replace_config({
        "providers": {"prov-oauth": {"type": "oauth", "connections": connections}},
        "admin": {"password_enabled": False, "password": "123456"},
    })
    return oauth, cs


def test_save_connection_collapses_duplicate_emails(oauth_and_cs):
    """3 same-email rows -> exactly 1 survives, keeping the freshest row's id."""
    oauth, cs = _seed(oauth_and_cs, [
        {"id": "id-old", "email": "u@x.com", "api_key": "K1",
         "expires_at": "2020-01-01T00:00:00+00:00", "token_type": "oauth"},
        {"id": "id-fresh", "email": "u@x.com", "api_key": "K2",
         "expires_at": "2027-01-01T00:00:00+00:00", "token_type": "oauth"},
        {"id": "id-mid", "email": "u@x.com", "api_key": "K3",
         "expires_at": "2023-01-01T00:00:00+00:00", "token_type": "oauth"},
    ])
    result = oauth._save_connection(
        "prov-oauth", "device_code", "AT-new", "RT-new",
        "2028-06-01T00:00:00+00:00", "u@x.com", "User", {},
    )
    conns = cs.get_config()["providers"]["prov-oauth"]["connections"]
    assert len(conns) == 1, f"expected 1 collapsed connection, got {len(conns)}"
    # Freshest pre-existing row (id-fresh) supplies the preserved id.
    assert result["id"] == "id-fresh"
    assert conns[0]["id"] == "id-fresh"
    assert conns[0]["api_key"] == "AT-new"
    assert conns[0]["refresh_token"] == "RT-new"
    assert conns[0]["expires_at"] == "2028-06-01T00:00:00+00:00"


def test_save_connection_no_email_appends(oauth_and_cs):
    """No-email saves keep legacy append behaviour (no collapse key)."""
    oauth, cs = _seed(oauth_and_cs, [
        {"id": "id-a", "email": "u@x.com", "api_key": "K1", "token_type": "oauth"},
    ])
    oauth._save_connection("prov-oauth", "device_code", "TOK", None, None, None, None, {})
    conns = cs.get_config()["providers"]["prov-oauth"]["connections"]
    assert len(conns) == 2


def test_update_connection_token_refreshes_all_duplicate_ids(oauth_and_cs):
    """3 rows sharing one id -> ALL get the refreshed token (no stale picks)."""
    oauth, cs = _seed(oauth_and_cs, [
        {"id": "dup-id", "email": "u@x.com", "api_key": "OLD",
         "refresh_token": "ORT", "token_type": "oauth"},
        {"id": "dup-id", "email": "u@x.com", "api_key": "OLD",
         "refresh_token": "ORT", "token_type": "oauth"},
        {"id": "dup-id", "email": "u@x.com", "api_key": "OLD",
         "refresh_token": "ORT", "token_type": "oauth"},
    ])
    oauth._update_connection_token(
        "prov-oauth", "dup-id", "NEW-AT", "NEW-RT", "2027-01-01T00:00:00+00:00", None,
    )
    conns = cs.get_config()["providers"]["prov-oauth"]["connections"]
    assert len(conns) == 3
    for c in conns:
        assert c["api_key"] == "NEW-AT"
        assert c["refresh_token"] == "NEW-RT"
        assert c["expires_at"] == "2027-01-01T00:00:00+00:00"


def test_update_connection_token_leaves_other_ids_untouched(oauth_and_cs):
    """Only matching-id rows are refreshed; distinct ids are not clobbered."""
    oauth, cs = _seed(oauth_and_cs, [
        {"id": "keep", "email": "a@x.com", "api_key": "KA", "token_type": "oauth"},
        {"id": "dup-id", "email": "b@x.com", "api_key": "OLD", "token_type": "oauth"},
    ])
    oauth._update_connection_token("prov-oauth", "dup-id", "NEW-AT", None, None, None)
    conns = cs.get_config()["providers"]["prov-oauth"]["connections"]
    by_id = {c["id"]: c for c in conns}
    assert by_id["keep"]["api_key"] == "KA"
    assert by_id["dup-id"]["api_key"] == "NEW-AT"
