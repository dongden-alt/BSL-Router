"""Real GitHub update check for /api/version/check.

The sidebar probe used to hardcode hasUpdate:false. It now queries GitHub
releases (cached for _VERSION_CHECK_CACHE_TTL seconds) and returns
currentVersion / hasUpdate / latestVersion / releaseUrl / error. The latest
version is the max semver of the Release tag AND the repo tags, so a
tag-only "post-tag wave" release (no Release object) is still detected.
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


def _url_fake(release=None, tags=None, raise_on=None, counter=None):
    """Build a fake http_client.get that dispatches on the requested URL."""
    async def fake_get(url, *a, **k):
        if counter is not None:
            counter["n"] = counter.get("n", 0) + 1
        if raise_on and raise_on in url:
            raise RuntimeError("network down")
        if "/tags" in url:
            if tags is None:
                return _FakeResp(404, {})
            return _FakeResp(200, [{"name": n} for n in tags])
        return _FakeResp(*(release if release else (200, {"tag_name": "v1.0.4"})))
    return fake_get


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
    fake_get = _url_fake(
        release=(200, {"tag_name": "v9.9.9", "html_url": "https://example.com/9.9.9"}),
        tags=["v9.9.9"],
        counter=calls,
    )

    result = _run(fake_get)
    assert result["currentVersion"] is not None
    assert result["hasUpdate"] is True
    assert result["latestVersion"] == "9.9.9"
    assert result["releaseUrl"] == "https://example.com/9.9.9"
    assert result["error"] == ""
    # Two HTTP calls now: /releases/latest + /tags (tag fallback).
    assert calls["n"] == 2


def test_no_update_when_same_version():
    fake_get = _url_fake(
        release=(200, {"tag_name": "v1.0.3", "html_url": "https://example.com/1.0.3"}),
        tags=["v1.0.3"],
    )

    saved_rvf = main._read_version_file
    main._read_version_file = lambda: "1.0.3"
    try:
        result = _run(fake_get)
    finally:
        main._read_version_file = saved_rvf

    assert result["hasUpdate"] is False
    assert result["latestVersion"] == "1.0.3"


def test_404_no_error():
    fake_get = _url_fake(release=(404, {}), tags=None)

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
    fake_get = _url_fake(
        release=(200, {"tag_name": "v9.9.9", "html_url": "https://example.com/9.9.9"}),
        tags=["v9.9.9"],
        counter=calls,
    )

    restore = _patch_deps(fake_get)
    try:
        # First call populates the cache (2 HTTP calls: release + tags).
        asyncio.run(main.version_check())
        assert calls["n"] == 2
        # Second call within TTL hits cache — no new http_client.get.
        asyncio.run(main.version_check())
        assert calls["n"] == 2
        # Third call still cached.
        asyncio.run(main.version_check())
        assert calls["n"] == 2
    finally:
        restore()


# ── Tag-fallback tests (post-tag-wave releases with no Release object) ────────


def test_tag_newer_than_release_detected():
    fake_get = _url_fake(
        release=(200, {"tag_name": "v1.0.4"}),
        tags=["v1.0.5", "v1.0.4"],
    )

    saved_rvf = main._read_version_file
    main._read_version_file = lambda: "1.0.4"
    try:
        result = _run(fake_get)
    finally:
        main._read_version_file = saved_rvf

    assert result["latestVersion"] == "1.0.5"
    assert result["hasUpdate"] is True
    assert result["releaseUrl"].endswith("/tags")


def test_tag_only_no_false_update_for_current():
    fake_get = _url_fake(
        release=(200, {"tag_name": "v1.0.4", "html_url": "https://example.com/1.0.4"}),
        tags=["v1.0.5"],
    )

    saved_rvf = main._read_version_file
    main._read_version_file = lambda: "1.0.5"
    try:
        result = _run(fake_get)
    finally:
        main._read_version_file = saved_rvf

    assert result["latestVersion"] == "1.0.5"
    assert result["hasUpdate"] is False


def test_nonsemver_tags_ignored():
    fake_get = _url_fake(
        release=(200, {"tag_name": "v1.0.2", "html_url": "https://example.com/1.0.2"}),
        tags=["pre-extraction-snapshot", "v1.0.2"],
    )

    saved_rvf = main._read_version_file
    main._read_version_file = lambda: "1.0.1"
    try:
        result = _run(fake_get)
    finally:
        main._read_version_file = saved_rvf

    assert result["latestVersion"] == "1.0.2"


def test_release_newer_than_tags_wins():
    fake_get = _url_fake(
        release=(200, {"tag_name": "v9.9.9", "html_url": "https://example.com/9.9.9"}),
        tags=["v1.0.5"],
    )

    result = _run(fake_get)
    # Max semver wins, not last-write — the Release tag is newer than all tags.
    assert result["latestVersion"] == "9.9.9"
    assert result["releaseUrl"] == "https://example.com/9.9.9"


def test_tags_fetch_failure_falls_back():
    fake_get = _url_fake(
        release=(200, {"tag_name": "v1.0.4", "html_url": "https://example.com/1.0.4"}),
        raise_on="/tags",
    )

    saved_rvf = main._read_version_file
    main._read_version_file = lambda: "1.0.4"
    try:
        result = _run(fake_get)
    finally:
        main._read_version_file = saved_rvf

    assert result["latestVersion"] == "1.0.4"
    assert result["hasUpdate"] is False
    assert result["error"] == ""
