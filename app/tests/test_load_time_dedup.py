"""Load-time connection dedup regression (2026-09-17).

ROOT CAUSE: save-path dedup (``_save_connection`` collapse, 2026-09-16) only
fires on NEW OAuth logins. Configs that already carried duplicate connection
rows on disk (antigravity: 32 rows for one account) were reloaded verbatim on
every boot, so round-robin kept picking stale duplicates and serving 401s.

FIX: ``load_config`` runs ``_dedup_connection_rows`` over every provider at
boot — collapsing rows that share an ``id``, or an ``email`` when
``token_type == "oauth"`` — keeps the freshest row (expires_at/imported_at),
remaps model ``connection_indexes``, and marks the config dirty so the cleaned
state is persisted.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import app.main as main


def _conn(cid=None, email=None, expires=None, imported=None, token_type=None):
    c = {}
    if cid is not None:
        c["id"] = cid
    if email is not None:
        c["email"] = email
    c["api_key"] = f"KEY-{cid or 'x'}"
    if expires is not None:
        c["expires_at"] = expires
    if imported is not None:
        c["imported_at"] = imported
    if token_type is not None:
        c["token_type"] = token_type
    return c


# ---------- unit: _dedup_connection_rows ----------

def test_no_connections_is_noop():
    assert main._dedup_connection_rows({}) == 0
    assert main._dedup_connection_rows({"connections": []}) == 0
    assert main._dedup_connection_rows({"connections": [_conn(cid="a")]}) == 0


def test_distinct_rows_untouched():
    p = {"connections": [
        _conn(cid="a", email="a@x.com", token_type="oauth"),
        _conn(cid="b", email="b@x.com", token_type="oauth"),
    ]}
    assert main._dedup_connection_rows(p) == 0
    assert len(p["connections"]) == 2


def test_duplicate_ids_collapse_keeps_freshest():
    p = {"connections": [
        _conn(cid="dup", expires="2020-01-01T00:00:00+00:00"),
        _conn(cid="dup", expires="2027-01-01T00:00:00+00:00"),
        _conn(cid="dup", expires="2023-01-01T00:00:00+00:00"),
    ]}
    assert main._dedup_connection_rows(p) == 2
    assert len(p["connections"]) == 1
    assert p["connections"][0]["expires_at"] == "2027-01-01T00:00:00+00:00"


def test_oauth_same_email_different_ids_collapse():
    """The antigravity case: re-imports minted new ids for one account."""
    p = {"connections": [
        _conn(cid="id-old", email="u@x.com", token_type="oauth", expires="2020-01-01T00:00:00+00:00"),
        _conn(cid="id-new", email="u@x.com", token_type="oauth", expires="2027-01-01T00:00:00+00:00"),
    ]}
    assert main._dedup_connection_rows(p) == 1
    assert len(p["connections"]) == 1
    assert p["connections"][0]["id"] == "id-new"


def test_api_key_same_email_not_collapsed():
    """Email grouping applies ONLY to oauth rows; api-key rows keep legacy behaviour."""
    p = {"connections": [
        _conn(cid="a", email="u@x.com"),
        _conn(cid="b", email="u@x.com"),
    ]}
    assert main._dedup_connection_rows(p) == 0
    assert len(p["connections"]) == 2


def test_imported_at_used_when_no_expires():
    p = {"connections": [
        _conn(cid="dup", imported="2026-01-01T00:00:00+00:00"),
        _conn(cid="dup", imported="2026-09-01T00:00:00+00:00"),
    ]}
    assert main._dedup_connection_rows(p) == 1
    assert p["connections"][0]["imported_at"] == "2026-09-01T00:00:00+00:00"


def test_connection_indexes_remapped():
    p = {
        "connections": [
            _conn(cid="keep-a"),
            _conn(cid="dup", expires="2020-01-01T00:00:00+00:00"),   # removed (stale)
            _conn(cid="dup", expires="2027-01-01T00:00:00+00:00"),   # survivor
            _conn(cid="keep-b"),
        ],
        "models": [
            {"id": "m1", "connection_indexes": [0, 1, 2, 3]},
            {"id": "m2", "connection_indexes": [1]},
        ],
    }
    assert main._dedup_connection_rows(p) == 1
    # surviving order: keep-a, dup(fresh), keep-b -> new indexes 0,1,2
    assert p["models"][0]["connection_indexes"] == [0, 1, 2]
    assert p["models"][1]["connection_indexes"] == [1]


def test_index_chain_resolution_latest_freshest_wins():
    """A->B->C supersede chain: all collapse to the freshest row C."""
    p = {"connections": [
        _conn(cid="dup", expires="2020-01-01T00:00:00+00:00"),
        _conn(cid="dup", expires="2023-01-01T00:00:00+00:00"),
        _conn(cid="dup", expires="2027-01-01T00:00:00+00:00"),
    ]}
    assert main._dedup_connection_rows(p) == 2
    assert len(p["connections"]) == 1
    assert p["connections"][0]["expires_at"] == "2027-01-01T00:00:00+00:00"


# ---------- integration: load_config hook ----------

def _load_config_patches(seeded):
    captured = {}

    def _capture_runtime(cfg):
        captured["runtime"] = cfg

    def _capture_replace(cfg):
        captured["replaced"] = cfg
        return cfg

    patches = [
        patch("app.main.init_config", lambda: None),
        patch("app.main.cs_get_config", lambda: seeded),
        patch("app.main._replace_runtime_config", _capture_runtime),
        patch("app.main.init_breaker", lambda cfg: None),
        patch("app.main._validate_antigravity_integration_config", lambda cfg: cfg),
        patch("app.main.replace_config", _capture_replace),
    ]
    return patches, captured


def _seeded_config(connections, models):
    return {"providers": {"antigravity": {
        "type": "oauth", "connections": connections, "models": models}}}


def test_load_config_dedups_and_persists():
    seeded = _seeded_config(
        [_conn(cid="dup", email="u@x.com", token_type="oauth", expires="2020-01-01T00:00:00+00:00"),
         _conn(cid="dup", email="u@x.com", token_type="oauth", expires="2027-01-01T00:00:00+00:00")],
        [{"id": "m1", "enabled": False}],  # disabled: multi-key expansion must not fire
    )
    patches, captured = _load_config_patches(seeded)
    for p in patches:
        p.start()
    try:
        main.load_config()
    finally:
        for p in patches:
            p.stop()
    conns = captured["replaced"]["providers"]["antigravity"]["connections"]
    assert len(conns) == 1
    assert conns[0]["expires_at"] == "2027-01-01T00:00:00+00:00"
    # dirty -> runtime config persisted with the cleaned state
    assert "runtime" in captured
    assert len(captured["runtime"]["providers"]["antigravity"]["connections"]) == 1


def test_load_config_clean_config_not_dirtied():
    seeded = _seeded_config(
        [_conn(cid="solo", email="u@x.com", token_type="oauth")],
        [{"id": "m1", "enabled": False}],
    )
    patches, captured = _load_config_patches(seeded)
    for p in patches:
        p.start()
    try:
        main.load_config()
    finally:
        for p in patches:
            p.stop()
    assert "runtime" not in captured  # nothing to persist
    assert len(captured["replaced"]["providers"]["antigravity"]["connections"]) == 1
