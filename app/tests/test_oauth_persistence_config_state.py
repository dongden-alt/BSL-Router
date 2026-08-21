"""OAuth persistence regression (all-OAuth-provider silent-failure fix).

ROOT CAUSE: the config_state refactor deleted the module-level ``main.config``
global. oauth.py's two writers still read it:

  - ``_save_connection`` raised AttributeError on ``main_app.config`` — the
    exception escaped to callers, so every provider login showed the provider
    side as "successful" but nothing was ever written to config.yaml.
  - ``_update_connection_token`` returned early on the same AttributeError,
    silently dropping every refreshed token.

The sanctioned mutation path (config_state.py get_mutable_config docstring +
test_config_state_contract.py) is: mutate a get_mutable_config() deep copy,
then commit via main._replace_runtime_config() (persist -> swap -> breaker).
``_persist_config_snapshot`` is patched out so no test touches the real
config.yaml.
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
            "prov-a": {"type": "oauth", "connections": []},
            "prov-b": {"type": "apikey", "connections": [{"id": "b1", "api_key": "kb"}]},
        },
        "admin": {"password_enabled": False, "password": "123456"},
    })
    try:
        with patch("app.main._persist_config_snapshot", return_value=None):
            yield oauth, cs, main
    finally:
        with patch("app.main._persist_config_snapshot", return_value=None):
            main._replace_runtime_config(saved)


def test_save_connection_persists_without_main_config_global(oauth_and_cs):
    """The previously-crashing path: _save_connection must append + commit."""
    oauth, cs, main = oauth_and_cs
    result = oauth._save_connection(
        "prov-a", "authorization_code_pkce", "AT-1", "RT-1",
        "2026-12-31T00:00:00+00:00", "u@example.com", "User", {"id_token": "t"},
    )
    assert result["provider"] == "prov-a"
    assert result["email"] == "u@example.com"
    live = cs.get_config()
    conns = live["providers"]["prov-a"]["connections"]
    assert len(conns) == 1
    assert conns[0]["api_key"] == "AT-1"
    assert conns[0]["refresh_token"] == "RT-1"
    assert conns[0]["auth_method"] == "authorization_code_pkce"
    assert conns[0]["enabled"] is True


def test_save_connection_bootstraps_missing_provider(oauth_and_cs):
    """A provider with no config entry gets {type: oauth, connections: [...]}."""
    oauth, cs, _main = oauth_and_cs
    oauth._save_connection("prov-new", "device_code", "TOK", None, None, None, None, {})
    live = cs.get_config()
    entry = live["providers"]["prov-new"]
    assert entry["type"] == "oauth"
    assert entry["connections"][0]["api_key"] == "TOK"


def test_save_connection_disk_failure_raises_500_and_rolls_back(oauth_and_cs):
    """If persist fails, the appended connection must be popped and a 500 raised."""
    oauth, cs, main = oauth_and_cs
    from fastapi import HTTPException
    from unittest.mock import patch

    with patch.object(main, "_replace_runtime_config", side_effect=OSError("disk full")):
        with pytest.raises(HTTPException) as ei:
            oauth._save_connection("prov-a", "device_code", "X", None, None, None, None, {})
    assert ei.value.status_code == 500
    # Rollback: the connection must not linger in memory when disk write failed.
    assert cs.get_config()["providers"]["prov-a"]["connections"] == []


def test_update_connection_token_persists_refresh(oauth_and_cs):
    """Token refresh must update the existing connection and commit."""
    oauth, cs, _main = oauth_and_cs
    result = oauth._save_connection("prov-a", "device_code", "OLD-AT", "OLD-RT",
                                    "2026-01-01T00:00:00+00:00", None, None, {})
    conn_id = result["id"]
    oauth._update_connection_token("prov-a", conn_id, "NEW-AT", "NEW-RT",
                                   "2027-01-01T00:00:00+00:00", {"k": "v"})
    live = cs.get_config()
    conn = live["providers"]["prov-a"]["connections"][0]
    assert conn["api_key"] == "NEW-AT"
    assert conn["refresh_token"] == "NEW-RT"
    assert conn["expires_at"] == "2027-01-01T00:00:00+00:00"
    assert conn["provider_data"]["k"] == "v"


def test_update_connection_token_unknown_id_is_noop(oauth_and_cs):
    """Refreshing a vanished connection must not touch config or raise."""
    oauth, cs, main = oauth_and_cs
    from unittest.mock import patch

    with patch.object(main, "_replace_runtime_config") as spy:
        oauth._update_connection_token("prov-a", "does-not-exist", "AT", None, None, None)
    spy.assert_not_called()


def test_oauth_writers_do_not_read_main_config_global(oauth_and_cs):
    """Source guard: neither writer may reference main_app.config again."""
    oauth, _cs, _main = oauth_and_cs
    import inspect

    for fn in (oauth._save_connection, oauth._update_connection_token):
        src = inspect.getsource(fn)
        assert "main_app.config" not in src.replace("main_app.config global", ""), (
            f"{fn.__name__} reads the deleted main_app.config global — "
            "OAuth persistence would silently fail again."
        )
        assert "_replace_runtime_config" in src, (
            f"{fn.__name__} must commit through main._replace_runtime_config "
            "(persist -> swap -> breaker), the only sanctioned runtime swap path."
        )
