"""Orphan-key coverage for per-model connection_indexes.

When a new API key is appended to a provider, models whose
`connection_indexes` allow-list does not include the new index must be
extended with it (only indexes strictly greater than the current max are
added, so deliberate mid-list exclusions survive).

These tests exercise the expansion logic as implemented in
`app.main.load_config` by feeding a monkeypatched config store.
"""
from __future__ import annotations

import app.main as main


def _run_load_config(providers: dict) -> dict:
    """Run load_config against an in-memory config and return the mutated providers.

    `load_config` imports `cs_get_config`/`init_config`/`replace_config` from
    `app.config_state` by name into `app.main`, so we patch the bound names in
    `app.main` directly (plus `_replace_runtime_config`, which hits disk).

    The expansion mutates the providers dict IN PLACE (returned by the fake
    `cs_get_config`), so we read the result from that same dict rather than from
    the `_replace_runtime_config` callback — which only fires when `_dirty`.
    """
    live = {"providers": providers}

    saved_init = main.init_config
    saved_get = main.cs_get_config
    saved_replace = main.replace_config
    saved_runtime_replace = main._replace_runtime_config

    main.init_config = lambda: None
    main.cs_get_config = lambda: live
    main.replace_config = lambda cfg: None
    main._replace_runtime_config = lambda cfg: None

    try:
        main.load_config()
    finally:
        main.init_config = saved_init
        main.cs_get_config = saved_get
        main.replace_config = saved_replace
        main._replace_runtime_config = saved_runtime_replace

    return live["providers"]


def test_three_enabled_expands_ci_from_01_to_all():
    """3 enabled conns, model ci=[0,1] => after expansion ci=[0,1,2]."""
    provider = {
        "connections": [
            {"enabled": True},
            {"enabled": True},
            {"enabled": True},
        ],
        "models": [
            {"id": "m", "enabled": True, "connection_indexes": [0, 1]},
        ],
    }
    out = _run_load_config({"p": provider})
    assert out["p"]["models"][0]["connection_indexes"] == [0, 1, 2]


def test_single_enabled_no_change():
    """model ci=[0] with a single enabled conn stays [0] (nothing to expand)."""
    provider = {
        "connections": [{"enabled": True}],
        "models": [
            {"id": "m", "enabled": True, "connection_indexes": [0]},
        ],
    }
    out = _run_load_config({"p": provider})
    assert out["p"]["models"][0]["connection_indexes"] == [0]


def test_disabled_tail_not_added():
    """Conn idx 3 disabled, model ci=[0,1] => idx 3 must NOT be added."""
    provider = {
        "connections": [
            {"enabled": True},
            {"enabled": True},
            {"enabled": False},
            {"enabled": False},
        ],
        "models": [
            {"id": "m", "enabled": True, "connection_indexes": [0, 1]},
        ],
    }
    out = _run_load_config({"p": provider})
    assert out["p"]["models"][0]["connection_indexes"] == [0, 1]


def test_deliberate_skip_preserved_plus_tail_appended():
    """model ci=[1] deliberate skip of idx 0 stays; enabled tail >1 appended."""
    provider = {
        "connections": [
            {"enabled": True},
            {"enabled": True},
            {"enabled": True},
            {"enabled": True},
        ],
        "models": [
            {"id": "m", "enabled": True, "connection_indexes": [1]},
        ],
    }
    out = _run_load_config({"p": provider})
    # idx 0 stays excluded (deliberate), idx 1 present, 2 and 3 appended
    assert out["p"]["models"][0]["connection_indexes"] == [1, 2, 3]
