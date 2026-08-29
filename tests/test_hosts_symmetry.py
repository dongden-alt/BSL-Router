"""Tests for the Antigravity hosts hijack symmetry in app.main._sync_antigravity_hosts.

WHY THIS EXISTS
---------------
The `daily-cloudcode-pa.googleapis.com` hosts hijack must exist if and only if a
verified BSL MITM listener is alive on the MITM port. Boot used to ADD the
hijack whenever the integration flag was true, but MITM no longer auto-starts
(launcher default is App-only). The router therefore wrote a DNS hijack at boot
with nothing listening behind it.

Verified on the live machine 2026-08-29: hosts were written at 20:33:32 by the
20:32:23 router boot, but MITM :443 only came up at 21:20:41 -- a 47-minute
window where Antigravity's inference traffic was pointed at a dead
127.0.0.1:443 socket, failing silently.

These tests are pure unit tests of the helper. They monkeypatch app.main.HOSTS_PATH
to a tmp_path file and NEVER touch the real hosts file (no elevation required).
Each fixture is seeded with the realistic shapes the parser must survive: a
`127.0.0.1 localhost` mapping, a comment, and a blank line.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.main as main  # noqa: E402

# Realistic seed: localhost mapping, a comment, and a blank line.
SEED = (
    "127.0.0.1 localhost\n"
    "# This is a comment line\n"
    "\n"
)


@pytest.fixture
def hosts(tmp_path, monkeypatch):
    """Write a realistic seed hosts file and point the module at it."""
    p = tmp_path / "hosts"
    p.write_text(SEED, encoding="utf-8")
    monkeypatch.setattr(main, "HOSTS_PATH", str(p))
    return p


def test_add_when_missing(hosts):
    note = main._sync_antigravity_hosts(True)
    assert note.startswith("added:")
    content = hosts.read_text(encoding="utf-8")
    assert "127.0.0.1 daily-cloudcode-pa.googleapis.com # bsl-router" in content


def test_add_is_idempotent(hosts):
    main._sync_antigravity_hosts(True)
    before = hosts.read_text(encoding="utf-8")
    note = main._sync_antigravity_hosts(True)
    assert note == "already_present"
    assert hosts.read_text(encoding="utf-8") == before


def test_remove_when_present(hosts):
    main._sync_antigravity_hosts(True)
    note = main._sync_antigravity_hosts(False)
    assert note == "removed:1"
    content = hosts.read_text(encoding="utf-8")
    assert "daily-cloudcode-pa.googleapis.com" not in content


def test_remove_is_idempotent(hosts):
    main._sync_antigravity_hosts(True)
    main._sync_antigravity_hosts(False)
    before = hosts.read_bytes()
    note = main._sync_antigravity_hosts(False)
    assert note == "already_absent"
    assert hosts.read_bytes() == before


def test_remove_preserves_untagged_user_entries(hosts):
    """A hand-written intercept line WITHOUT the BSL tag survives a remove pass."""
    main._sync_antigravity_hosts(True)
    # User manually added a mapping with no tag.
    with hosts.open("a", encoding="utf-8") as fh:
        fh.write("127.0.0.1 daily-cloudcode-pa.googleapis.com\n")
    main._sync_antigravity_hosts(False)
    content = hosts.read_text(encoding="utf-8")
    # The untagged user line must remain.
    assert "127.0.0.1 daily-cloudcode-pa.googleapis.com\n" in content
    # The tagged (managed) line must be gone.
    assert "# bsl-router" not in content


def test_remove_never_touches_auth_domain(hosts):
    """The auth domain `cloudcode-pa.googleapis.com` is a SUBSTRING of the
    intercept domain. A remove pass must NOT touch a tagged auth entry -- that
    would break Google login."""
    # Add a tagged auth-domain line (the login-breaking case).
    with hosts.open("a", encoding="utf-8") as fh:
        fh.write("127.0.0.1 cloudcode-pa.googleapis.com # bsl-router\n")
    main._sync_antigravity_hosts(False)
    content = hosts.read_text(encoding="utf-8")
    # Auth entry survives untouched.
    assert "127.0.0.1 cloudcode-pa.googleapis.com # bsl-router" in content


def test_permission_error_is_reported_not_raised(hosts, monkeypatch):
    """A PermissionError on either read or write is reported as a note, never raised."""
    import builtins

    def _boom(*args, **kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(builtins, "open", _boom)
    note = main._sync_antigravity_hosts(True)
    assert note == "permission_denied"
    # No exception escaped the helper.
    note2 = main._sync_antigravity_hosts(False)
    assert note2 == "permission_denied"


def test_no_write_when_no_change(hosts, monkeypatch):
    """On a no-op (already_present), no write-mode open occurs."""
    import builtins

    main._sync_antigravity_hosts(True)  # add it

    writes = {"count": 0}
    real_open = builtins.open

    def counting_open(file, mode="r", *args, **kwargs):
        if any(ch in str(mode) for ch in ("w", "a", "+")):
            writes["count"] += 1
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", counting_open)
    note = main._sync_antigravity_hosts(True)  # already present -> no-op
    assert note == "already_present"
    assert writes["count"] == 0


def test_reconcile_leaves_hosts_untouched_on_inspection_error(hosts):
    """REGRESSION for app.main.lifespan boot guard (2026-08-30).

    When ``_mitm_runtime_status`` returns ``inspection_error`` (probe failed but
    the port may well be alive behind it), the boot reconcile must NOT strip the
    existing hijack. A transient probe failure must never remove a valid hijack.

    Here the hosts file ALREADY holds a tagged hijack and the probe reports an
    error. The helper must report 'inspection inconclusive' and leave the file
    byte-identical (no removal, no write).
    """
    note = main._sync_antigravity_hosts(True)  # seed a valid hijack
    assert note.startswith("added:")
    before = hosts.read_bytes()

    status = {"server": None, "inspection_error": "probe timeout", "port": 443}
    result = main._reconcile_hosts_from_status(status)

    assert result.startswith("inspection inconclusive")
    assert "hosts unchanged" in result
    # File unchanged: the valid hijack was NOT stripped by the failed probe.
    assert hosts.read_bytes() == before
    content = hosts.read_text(encoding="utf-8")
    assert "127.0.0.1 daily-cloudcode-pa.googleapis.com # bsl-router" in content


def test_reconcile_adds_when_live_and_absent(desired_absent):
    """REGRESSION for app.main.lifespan boot guard (2026-08-30).

    When the probe is clean (no ``inspection_error``) and reports a live BSL MITM
    server, the boot reconcile must ADD the hijack even though it was absent at
    boot. This is the positive branch the guard protects -- liveness, not the
    integration flag, is ground truth.
    """
    status = {"server": True, "inspection_error": None, "port": 443}
    result = main._reconcile_hosts_from_status(status)
    assert result.startswith("added:")
    content = desired_absent.read_text(encoding="utf-8")
    assert "127.0.0.1 daily-cloudcode-pa.googleapis.com # bsl-router" in content


def test_reconcile_removes_when_port_genuinely_empty(hosts):
    """REGRESSION for app.main.lifespan boot guard (2026-08-30).

    When the probe is clean (no ``inspection_error``) and authoritatively reports
    ``server: False``, the boot reconcile must REMOVE the existing hijack. This is
    the primary purpose of the whole fix: a router boot with NO listener on the
    MITM port must not leave Antigravity's inference traffic pointing at a dead
    ``127.0.0.1:443`` socket.

    This test kills the M2 over-eager mutant that reports inconclusive whenever
    ``server`` is falsy -- i.e. one that NEVER removes the hijack. Only the
    real ``{inspection_error: None, server: False}`` row exercises the removal
    decision at the reconcile level, so this is the mutation that M2 cannot
    survive once it exists.
    """
    note = main._sync_antigravity_hosts(True)  # seed a valid hijack
    assert note.startswith("added:")

    status = {"server": False, "inspection_error": None, "port": 443}
    result = main._reconcile_hosts_from_status(status)

    assert result == "removed:1"
    content = hosts.read_text(encoding="utf-8")
    assert "daily-cloudcode-pa.googleapis.com" not in content


@pytest.fixture
def desired_absent(tmp_path, monkeypatch):
    """A hosts file with NO Antigravity hijack yet (the at-boot absent case)."""
    p = tmp_path / "hosts"
    p.write_text(SEED, encoding="utf-8")
    monkeypatch.setattr(main, "HOSTS_PATH", str(p))
    return p
