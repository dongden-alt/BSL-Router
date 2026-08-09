"""Regression tests for the 2026-08-03 config.yaml wipe.

ROOT CAUSE: _persist_config_snapshot used a bare open("config.yaml","w"),
which truncates the file the instant it opens. A kill/restart mid-write left
config.yaml as an empty `{}`. The running server kept serving from memory,
but on next restart the antigravity provider vanished and its models 404'd.
This was misread upstream as an "expired OAuth token".

Guarantees under test:
  1. ATOMIC â€” a good snapshot round-trips to disk intact (temp+os.replace).
  2. NEVER-WIPE â€” an empty/degenerate snapshot must NOT clobber an existing
     non-empty config.yaml.
"""
import os
import sys

import pytest
import yaml

# Resolve the repo root from this file's location. A hardcoded absolute path only
# works on the machine it was written on; every other clone gets ModuleNotFoundError.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app import main as main_app


def _good_config():
    return {
        "providers": {
            "antigravity": {
                "connections": [
                    {"id": "c1", "api_key": "tok", "refresh_token": "rt", "token_type": "oauth"}
                ],
                "models": [{"id": "gemini-3.5-flash-high", "enabled": True}],
            }
        },
        "server": {"port": 6969},
    }


def test_atomic_persist_round_trip(tmp_path, monkeypatch):
    """A good snapshot must be written to disk intact and re-readable."""
    monkeypatch.chdir(tmp_path)
    main_app._persist_config_snapshot(_good_config())

    assert os.path.exists("config.yaml")
    back = yaml.safe_load(open("config.yaml", encoding="utf-8"))
    assert back["providers"]["antigravity"]["models"][0]["id"] == "gemini-3.5-flash-high"
    # No temp file should linger after a successful atomic replace.
    leftovers = [f for f in os.listdir(".") if f.endswith(".tmp")]
    assert leftovers == [], f"temp files leaked: {leftovers}"


def test_empty_snapshot_never_wipes_existing_config(tmp_path, monkeypatch):
    """An empty providers snapshot must NOT overwrite a rich on-disk config."""
    monkeypatch.chdir(tmp_path)
    # Seed a rich config on disk.
    with open("config.yaml", "w", encoding="utf-8") as f:
        yaml.dump(_good_config(), f)
    before = open("config.yaml", encoding="utf-8").read()

    # Attempt to persist a degenerate snapshot (simulates empty in-memory config).
    main_app._persist_config_snapshot({})
    main_app._persist_config_snapshot({"providers": {}})

    after = open("config.yaml", encoding="utf-8").read()
    assert after == before, "config.yaml was wiped by a degenerate snapshot"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))


# ---------------------------------------------------------------------------
# Windows sharing-violation retry (discovered empirically, 2026-08-04)
#
# The first version of the atomic fix passed both tests above and was still
# wrong: under load, 21 of 200 os.replace() calls raised PermissionError
# (WinError 5) because another handle held config.yaml open. Atomicity held,
# but writes were SILENTLY DROPPED -- trading a rare wipe for frequent
# save-loss, which is worse (no error shown, change gone on restart).
# ---------------------------------------------------------------------------
import threading


def test_transient_sharing_violation_is_retried(tmp_path, monkeypatch):
    """A transient PermissionError on replace must be retried, not dropped."""
    monkeypatch.chdir(tmp_path)
    with open("config.yaml", "w", encoding="utf-8") as f:
        yaml.dump(_good_config(), f)

    real_replace = os.replace
    calls = {"n": 0}

    def flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] <= 3:  # fail the first 3 attempts, then succeed
            raise PermissionError(5, "Access is denied")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", flaky_replace)

    updated = _good_config()
    updated["providers"]["antigravity"]["models"][0]["id"] = "gemini-3.5-pro"
    main_app._persist_config_snapshot(updated)

    monkeypatch.setattr(os, "replace", real_replace)
    back = yaml.safe_load(open("config.yaml", encoding="utf-8"))
    assert back["providers"]["antigravity"]["models"][0]["id"] == "gemini-3.5-pro", \
        "write was dropped instead of retried"
    assert calls["n"] == 4, f"expected 3 failures then success, got {calls['n']} calls"


def test_permanent_replace_failure_is_reported_loudly(tmp_path, monkeypatch, capsys):
    """If every retry fails, say so â€” a dropped config write is data loss."""
    monkeypatch.chdir(tmp_path)
    with open("config.yaml", "w", encoding="utf-8") as f:
        yaml.dump(_good_config(), f)
    before = open("config.yaml", encoding="utf-8").read()

    def always_denied(src, dst):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(os, "replace", always_denied)
    # It must ALSO raise, not just log: oauth.py:948 pops the half-added
    # connection and returns HTTP 500 on OSError, and main.py:1441/1599 only
    # assign `config = new_config` if this returns cleanly. Swallowing would
    # report success for a write that never happened.
    with pytest.raises(OSError):
        main_app._persist_config_snapshot(_good_config())

    out = capsys.readouterr().out
    assert "FAILED to persist" in out, "silent data loss: no error reported"
    assert "UNSAVED" in out, "message must state the config is unsaved"
    # The existing file must survive a failed write untouched.
    assert open("config.yaml", encoding="utf-8").read() == before
    # And no temp file may be left behind.
    assert [f for f in os.listdir(".") if f.endswith(".tmp")] == []


def test_never_observed_truncated_under_concurrent_reads(tmp_path, monkeypatch):
    """The core guarantee: readers never see a truncated config.

    This is what the original bare open(...,"w") violated -- it truncated on
    open, so any reader (or a crash) in that window saw an empty file.
    """
    monkeypatch.chdir(tmp_path)
    main_app._persist_config_snapshot(_good_config())

    observations = []
    stop = threading.Event()

    def sampler():
        while not stop.is_set():
            try:
                observations.append(os.path.getsize("config.yaml"))
            except OSError:
                observations.append(-1)  # missing == also a violation

    t = threading.Thread(target=sampler)
    t.start()
    try:
        for _ in range(40):
            main_app._persist_config_snapshot(_good_config())
    finally:
        stop.set()
        t.join()

    assert observations, "sampler collected nothing"
    bad = [s for s in observations if s <= 8]
    assert bad == [], f"config observed truncated/missing {len(bad)}x of {len(observations)}"


def test_backup_preserves_last_known_good(tmp_path, monkeypatch):
    """A .bak of the previous config enables manual recovery."""
    monkeypatch.chdir(tmp_path)
    first = _good_config()
    main_app._persist_config_snapshot(first)

    second = _good_config()
    second["providers"]["antigravity"]["models"][0]["id"] = "changed"
    main_app._persist_config_snapshot(second)

    assert os.path.exists("config.yaml.bak")
    bak = yaml.safe_load(open("config.yaml.bak", encoding="utf-8"))
    assert bak["providers"]["antigravity"]["models"][0]["id"] == "gemini-3.5-flash-high"


def test_first_run_empty_config_still_allowed(tmp_path, monkeypatch):
    """The never-wipe gate must not block a legitimate first write.

    With no config on disk there is nothing to lose, so an empty providers
    snapshot is valid. Over-blocking here would break fresh installs.
    """
    monkeypatch.chdir(tmp_path)
    assert not os.path.exists("config.yaml")
    main_app._persist_config_snapshot({"providers": {}, "server": {"port": 6969}})
    assert os.path.exists("config.yaml"), "first-run write was wrongly refused"

