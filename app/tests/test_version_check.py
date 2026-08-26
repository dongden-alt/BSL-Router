"""Real GitHub update check for /api/version/check.

The sidebar probe used to hardcode hasUpdate:false. It now queries GitHub
releases (cached for _VERSION_CHECK_CACHE_TTL seconds) and returns
currentVersion / hasUpdate / latestVersion / releaseUrl / error.
"""
from __future__ import annotations

import asyncio

import app.main as main


class _FakeResp:
    def __init__(self, status_code: int, data: dict):
        self.status_code = status_code
        self._data = data

    def json(self):
        return self._data


def _patch_deps(fake_get, fake_cfg=None):
    """Monkeypatch http_client + cs_get_config; return restore callable."""
    live = {"update": fake_cfg or {}}
    saved_get = main.http_client
    saved_cs = main.cs_get_config

    class _FakeClient:
        get = staticmethod(fake_get)

    main.http_client = _FakeClient()
    main.cs_get_config = lambda: live
    main._VERSION_CHECK_CACHE["ts"] = 0.0
    main._VERSION_CHECK_CACHE["payload"] = None

    def _restore():
        main.http_client = saved_get
        main.cs_get_config = saved_cs

    return _restore


def _run(fake_get, fake_cfg=None):
    restore = _patch_deps(fake_get, fake_cfg)
    try:
        return asyncio.run(main.version_check())
    finally:
        restore()


def test_newer_release_detected():
    calls = {"n": 0}

    async def fake_get(*a, **k):
        calls["n"] += 1
        return _FakeResp(200, {"tag_name": "v9.9.9", "html_url": "https://example.com/9.9.9"})

    result = _run(fake_get)
    assert result["currentVersion"] is not None
    assert result["hasUpdate"] is True
    assert result["latestVersion"] == "9.9.9"
    assert result["releaseUrl"] == "https://example.com/9.9.9"
    assert result["error"] == ""
    assert calls["n"] == 1


def test_no_update_when_same_version():
    async def fake_get(*a, **k):
        return _FakeResp(200, {"tag_name": "v1.0.3", "html_url": "https://example.com/1.0.3"})

    saved_rvf = main._read_version_file
    main._read_version_file = lambda: "1.0.3"
    try:
        result = _run(fake_get)
    finally:
        main._read_version_file = saved_rvf

    assert result["hasUpdate"] is False
    assert result["latestVersion"] == "1.0.3"


def test_404_no_error():
    async def fake_get(*a, **k):
        return _FakeResp(404, {})

    result = _run(fake_get)
    assert result["hasUpdate"] is False
    assert result["latestVersion"] is None
    assert result["error"] == ""


def test_exception_populates_error():
    async def fake_get(*a, **k):
        raise RuntimeError("network down")

    result = _run(fake_get)
    assert result["hasUpdate"] is False
    assert result["latestVersion"] is None
    assert "network down" in result["error"]


def test_cache_avoids_refetch():
    calls = {"n": 0}

    async def fake_get(*a, **k):
        calls["n"] += 1
        return _FakeResp(200, {"tag_name": "v9.9.9", "html_url": "https://example.com/9.9.9"})

    restore = _patch_deps(fake_get)
    try:
        # First call populates the cache.
        asyncio.run(main.version_check())
        assert calls["n"] == 1
        # Second call within TTL hits cache — no new http_client.get.
        asyncio.run(main.version_check())
        assert calls["n"] == 1
        # Third call still cached.
        asyncio.run(main.version_check())
        assert calls["n"] == 1
    finally:
        restore()
