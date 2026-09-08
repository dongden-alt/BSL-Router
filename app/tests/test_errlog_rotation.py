"""Boot-time app.err.log cap (Lane 2, 2026-09-07).

scripts/bslrouter.ps1 redirects router stderr with `2>> app.err.log`; the
supervisor holds that handle for the process lifetime, so the file can only
be healed in-process at BOOT via dualstack_serve._selfheal_errlog(). These
tests pin the trigger (>50MB), the tail-keep behavior, and the fail-open
guarantees (missing file / under-cap file / no crash).
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import app.dualstack_serve as ds


@pytest.fixture
def errlog(tmp_path):
    return tmp_path / "app.err.log"


def _write_sized(path, size_bytes, filler=b"x"):
    """Write a file of exactly size_bytes (newline-delimited rows)."""
    with open(path, "wb") as fh:
        remaining = size_bytes
        row = filler * 99 + b"\n"
        while remaining >= len(row):
            fh.write(row)
            remaining -= len(row)
        if remaining:
            fh.write(filler * (remaining - 1) + b"\n" if remaining > 1 else b"x")


def test_no_truncate_under_cap(errlog):
    """File <= 50MB must be left byte-for-byte untouched."""
    _write_sized(errlog, 1024)
    before = errlog.read_bytes()
    assert ds._selfheal_errlog(str(errlog)) is False
    assert errlog.read_bytes() == before


def test_missing_file_no_crash(tmp_path):
    """Missing file is a no-op, never an exception."""
    assert ds._selfheal_errlog(str(tmp_path / "does_not_exist.log")) is False


def test_truncate_over_cap_keeps_tail(errlog):
    """>50MB triggers the heal; result is far under cap and keeps recent rows."""
    # 51MB: 1MB over the trigger. Tail window is 10MB, so ~40MB must be dropped.
    _write_sized(errlog, 51 * 1024 * 1024)
    assert errlog.stat().st_size > ds._ERRLOG_MAX_BYTES
    marker = b"RECENT-MARKER-ROW"
    with open(errlog, "ab") as fh:
        fh.write(marker + b"\n")
    assert ds._selfheal_errlog(str(errlog)) is True
    new_size = errlog.stat().st_size
    assert new_size <= ds._ERRLOG_MAX_BYTES
    # Kept the most recent data (tail window), dropped the ancient bulk.
    assert marker in errlog.read_bytes()
    assert new_size <= ds._ERRLOG_TAIL_BYTES + 4096


def test_truncated_file_has_no_partial_first_row(errlog):
    """The kept tail must start on a full row, never mid-line."""
    row = b"A" * 99 + b"\n"
    with open(errlog, "wb") as fh:
        fh.write(row * (51 * 1024 * 1024 // len(row)))
    assert ds._selfheal_errlog(str(errlog)) is True
    data = errlog.read_bytes()
    lines = data.split(b"\n")
    # First line is a complete row (non-empty, correct width) and the file
    # ends with a newline.
    assert lines[0] == row.rstrip(b"\n")
    assert data.endswith(b"\n")


def test_unwritable_file_fails_open(errlog):
    """A locked/unopenable file must never raise — hygiene is best-effort."""
    _write_sized(errlog, 64)
    with open(errlog, "rb"):
        # Holding a handle is fine on Windows; instead point the helper at a
        # path that exists but cannot be opened r+b: a directory.
        pass
    d = tmp_dir = errlog.parent / "adir"
    d.mkdir()
    assert os.path.exists(d)
    # Directory: getsize succeeds, open r+b fails -> fail-open False, no raise.
    assert ds._selfheal_errlog(str(d)) is False
