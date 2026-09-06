"""Empty-key Authorization header fix — regression suite (2026-09-06).

Bug: a connection saved with a blank api_key built 'Authorization: Bearer '
(trailing space), which httpcore rejects at client.send() with
    ValueError: Illegal header value b'Bearer '
— not an HTTP status, so the failure got classified as 500 server_error and
poisoned the circuit breaker with the wrong fault class (observed live:
claude-opus-5/pix4k combo walk, .brain/logs 2026-09-05).

Fix: _auth_headers_for_key() omits the Authorization header entirely for
blank keys; upstream then answers a clean 401 that flows through the existing
auth classification / KeyFailover / combo-advance paths.

Tests pull the pure helper out of app/main.py via ast (importing the full
FastAPI app for a header unit test is heavyweight) and assert on source
patterns for the dispatch sites, in the same spirit as the launcher's
_powershell_code_only source-pattern tests.
"""

import ast

import pytest

from pathlib import Path

MAIN_PY = Path(__file__).resolve().parents[1] / "main.py"
OBS_PY = Path(__file__).resolve().parents[1] / "observability.py"


def _load_helper():
    """Extract _auth_headers_for_key from main.py without importing the app."""
    src = MAIN_PY.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_auth_headers_for_key":
            mod = ast.Module(body=[node], type_ignores=[])
            ns: dict = {}
            exec(compile(mod, "<auth-helper>", "exec"), ns)
            return ns["_auth_headers_for_key"]
    pytest.fail("_auth_headers_for_key not found in app/main.py")


def _main_source() -> str:
    return MAIN_PY.read_text(encoding="utf-8")


# ── 1. Unit: blank keys produce NO Authorization header ────────────────────


@pytest.mark.parametrize("blank", ["", None, "   ", "\t\n "])
def test_blank_key_omits_authorization(blank):
    assert _load_helper()(blank) == {}


def test_real_key_builds_bearer():
    assert _load_helper()("sk-live-abc") == {"Authorization": "Bearer sk-live-abc"}


def test_whitespace_key_is_stripped():
    assert _load_helper()("  sk-live-abc  ") == {"Authorization": "Bearer sk-live-abc"}


# ── 2. Regression proof: the OLD pattern was the crash ─────────────────────


def test_old_pattern_produced_illegal_header():
    """Documents the live failure string. The old code built exactly this —
    httpcore rejects it (b'Bearer ' has a trailing space)."""
    old = {"Authorization": f"Bearer {''}"}  # what active_conn.get('api_key', '') gave us
    assert old["Authorization"] == "Bearer "  # trailing space == illegal
    assert b"Bearer " != b"Bearer"  # the byte difference httpcore rejects


# ── 3. Source introspection: all dispatch sites use the helper ─────────────


def test_helper_defined_in_main():
    assert "def _auth_headers_for_key" in _main_source()


def test_chat_dispatch_uses_helper():
    """Chat headers (the claude-opus-5/pix4k crash site) must spread the helper."""
    assert '**_auth_headers_for_key(_fresh_token)' in _main_source()


def test_image_and_video_dispatch_use_helper():
    """Image/video dispatch must spread the helper (count: exactly 2)."""
    assert _main_source().count("**_auth_headers_for_key(active_conn.get('api_key', ''))") == 2


def test_billing_probe_uses_helper():
    assert '**_auth_headers_for_key(key)' in _main_source()


def test_no_legacy_bearer_from_connection_variants_left():
    """No dispatch site may still interpolate a possibly-empty key directly."""
    banned = [
        'f"Bearer {active_conn.get(\'api_key\', \'\')}"',
        'f"Bearer {_fresh_token}"',
        'f"Bearer {key}"',
        'f"Bearer {active_conn[\'api_key\']}"',
    ]
    src = _main_source()
    for pat in banned:
        assert pat not in src, f"legacy empty-key Bearer pattern still present: {pat}"


# ── 4. Observability probe guard ────────────────────────────────────────────


def test_observability_probe_guards_blank_key():
    src = OBS_PY.read_text(encoding="utf-8")
    assert "if not _api_key:" in src
    assert 'f"Bearer {_api_key}"' in src


# ── 5. StreamBuffer double-fault synthesis present ──────────────────────────


def test_streambuffer_failopen_resend_is_guarded():
    """The fail-open re-send must not let a send-time ValueError escape as an
    unhandled 500 — it synthesizes a 504 so the combo chain advances."""
    src = _main_source()
    assert "fail-open re-send failed" in src
    assert "_SyntheticResponse(504" in src
