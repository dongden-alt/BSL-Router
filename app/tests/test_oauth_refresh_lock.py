"""Concurrency tests for the OAuth refresh lock (anti-thundering-herd)."""

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


@pytest.fixture()
def oauth_module():
    import importlib
    import app.oauth as oauth

    return importlib.reload(oauth)


def _expired_connection():
    return {
        "id": "conn-1",
        "api_key": "old-token",
        "refresh_token": "refresh-token",
        "expires_at": (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat(),
        "token_type": "oauth",
    }


def _make_updater(conn):
    """Mirror production: _update_connection_token mutates the SAME dict object
    the callers hold (config connection identity), so waiters see the refresh."""

    def fake_update(
        provider, conn_id, new_access_token, new_refresh_token, new_expires_at, extra_pd=None
    ):
        fake_update.call_count += 1
        conn["api_key"] = new_access_token
        conn["refresh_token"] = new_refresh_token or conn["refresh_token"]
        conn["expires_at"] = new_expires_at

    fake_update.call_count = 0
    return fake_update


def test_concurrent_refresh_coalesces_to_single_refresh(oauth_module):
    """20 concurrent callers on an expired token must trigger exactly ONE refresh."""
    conn = _expired_connection()
    calls = {"n": 0}

    async def fake_refresh(*args, **kwargs):
        calls["n"] += 1
        await asyncio.sleep(0.02)  # let other callers pile up on the lock
        return {"access_token": "new-token", "expires_in": 3600}

    with patch.object(
        oauth_module, "_update_connection_token", new=_make_updater(conn)
    ) as updater, patch.dict(oauth_module._REFRESH_DISPATCH, {"fake": fake_refresh}):

        async def main():
            return await asyncio.gather(
                *[
                    oauth_module.ensure_fresh_token("fake", conn, {})
                    for _ in range(20)
                ]
            )

        results = asyncio.run(main())

    assert calls["n"] == 1
    assert updater.call_count == 1
    assert all(r == "new-token" for r in results)


def test_force_refresh_waiters_reuse_leader_token(oauth_module):
    """force=True waiters must not each force-refresh after the leader wins."""
    conn = _expired_connection()
    calls = {"n": 0}

    async def fake_refresh(*args, **kwargs):
        calls["n"] += 1
        await asyncio.sleep(0.02)
        return {"access_token": "new-token", "expires_in": 3600}

    with patch.object(
        oauth_module, "_update_connection_token", new=_make_updater(conn)
    ) as updater, patch.dict(oauth_module._REFRESH_DISPATCH, {"fake": fake_refresh}):

        async def main():
            return await asyncio.gather(
                *[
                    oauth_module.ensure_fresh_token("fake", conn, {}, force=True)
                    for _ in range(10)
                ]
            )

        results = asyncio.run(main())

    assert calls["n"] == 1
    assert updater.call_count == 1
    assert all(r == "new-token" for r in results)


def test_fresh_token_skips_lock_refresh(oauth_module):
    """A non-expired token must not refresh at all."""
    conn = _expired_connection()
    conn["expires_at"] = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
    conn["api_key"] = "fresh-token"

    async def fake_refresh(*args, **kwargs):
        raise AssertionError("should not be called")

    with patch.dict(oauth_module._REFRESH_DISPATCH, {"fake": fake_refresh}):
        token = asyncio.run(oauth_module.ensure_fresh_token("fake", conn, {}))

    assert token == "fresh-token"
