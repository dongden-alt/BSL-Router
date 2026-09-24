"""Terminal Relay Wall detector — makes `upstream_safety_blocked` (CC Max /
mintrouter claude-fable-5) and `data_inspection_failed` (GLM/zhipu, Qwen)
NON-RECOVERABLE so the combo fallback + never-stop retry stops spamming 400s
against a wall that will never pass.

Context: both bodies return HTTP 400, but 400 is in `_RECOVERABLE`, so the
router treated them as transient: combo fallback advanced to another model in
the SAME walled family, the never-stop wrap (`_raise_combo_wrap`) re-looped
forever, and the console spammed `[400] ... upstream_safety_blocked` every
~15s. `_is_terminal_relay_wall` gates the short-circuits in the stream /
non-stream combo paths and the 400→502 reclassify block so the 400 surfaces
ONCE and the client unblocks immediately.

These tests exercise the REAL module-level detector `app.main._is_terminal_relay_wall`
plus the marker tuple — the same symbol the generators call on the non-200
path. No mirror drift.
"""
import app.main as main


# ──────────────────────────────────────────────────────────────────────────────
# Marker surface
# ──────────────────────────────────────────────────────────────────────────────
def test_terminal_relay_wall_markers_present():
    """Both bodies must be in the marker tuple; the tuple itself must be a tuple."""
    assert isinstance(main._TERMINAL_RELAY_WALL_MARKERS, tuple)
    assert "upstream_safety_blocked" in main._TERMINAL_RELAY_WALL_MARKERS
    assert "data_inspection_failed" in main._TERMINAL_RELAY_WALL_MARKERS


# ──────────────────────────────────────────────────────────────────────────────
# Truthy cases — 400/403 carrying either marker is a terminal wall.
# ──────────────────────────────────────────────────────────────────────────────
def test_wall_upstream_safety_blocked_400():
    body = '{"error":{"code":"upstream_safety_blocked","message":"blocked by upstream"}}'
    assert main._is_terminal_relay_wall(400, body) is True


def test_wall_data_inspection_failed_400():
    body = '{"error":{"message":"data_inspection_failed","details":"content rejected"}}'
    assert main._is_terminal_relay_wall(400, body) is True


def test_wall_case_insensitive():
    """Provider casing must not dodge the detector."""
    assert main._is_terminal_relay_wall(400, "UPSTREAM_SAFETY_BLOCKED") is True
    assert main._is_terminal_relay_wall(400, "Data_Inspection_Failed") is True


def test_wall_403_also_terminal():
    """A 403 carrying the marker is also a hard wall (status gate includes 403)."""
    assert main._is_terminal_relay_wall(403, "err: upstream_safety_blocked here") is True


def test_wall_marker_anywhere_in_body():
    """The marker can be nested anywhere in a long JSON body, not just at the start."""
    long_body = (
        '{"choices":[],"usage":{},"error":{"type":"policy",'
        '"message":"request rejected: data_inspection_failed after scan"}}'
    )
    assert main._is_terminal_relay_wall(400, long_body) is True


# ──────────────────────────────────────────────────────────────────────────────
# Falsy cases — the wall must NOT swallow recoverable errors.
# ──────────────────────────────────────────────────────────────────────────────
def test_not_wall_unknown_field_400_stays_recoverable():
    """A generic 400 (e.g. 'unknown field system') stays recoverable — status 400
    alone is NOT sufficient, the marker must be present."""
    assert main._is_terminal_relay_wall(400, '{"error":"unknown field system"}') is False


def test_not_wall_429_even_with_marker():
    """A 429 carrying the marker is NOT a terminal wall — the status gate keeps
    429 retryable so a transient rate limit on a walled provider can still be
    retried/backed-off."""
    assert main._is_terminal_relay_wall(429, "...upstream_safety_blocked...") is False


def test_not_wall_empty_body_400():
    """An empty 400 body reverts to the legacy recoverable 400 path."""
    assert main._is_terminal_relay_wall(400, "") is False


def test_not_wall_500_with_marker():
    """A 5xx carrying the marker is NOT a terminal wall — 5xx stays recoverable."""
    assert main._is_terminal_relay_wall(500, "data_inspection_failed") is False


def test_not_wall_none_body():
    """None / non-string body falls through fail-open to False (legacy behavior)."""
    assert main._is_terminal_relay_wall(400, None) is False


def test_not_wall_whitespace_only_body():
    """A whitespace-only body has no marker — recoverable."""
    assert main._is_terminal_relay_wall(400, "   \n\t  ") is False


def test_not_wall_other_status_codes():
    """Status codes outside (400, 403) are never terminal walls by this gate."""
    assert main._is_terminal_relay_wall(401, "upstream_safety_blocked") is False
    assert main._is_terminal_relay_wall(404, "data_inspection_failed") is False
    assert main._is_terminal_relay_wall(503, "upstream_safety_blocked") is False


# ──────────────────────────────────────────────────────────────────────────────
# Fail-open contract — a broken body never crashes routing.
# ──────────────────────────────────────────────────────────────────────────────
class _RaisesOnStr:
    """Mimics an object whose str()/lower() raises — proves the detector
    never propagates an exception into the streaming/combo paths."""

    def __str__(self):
        raise RuntimeError("boom")

    def __getattr__(self, name):
        raise RuntimeError(f"boom on {name}")


def test_detector_fail_open_on_bad_body():
    """A pathological body object must not raise; detector returns False and the
    caller keeps legacy recoverable behavior."""
    assert main._is_terminal_relay_wall(400, _RaisesOnStr()) is False


# ──────────────────────────────────────────────────────────────────────────────
# Contract — _RECOVERABLE contents are untouched (other 400s stay recoverable).
# ──────────────────────────────────────────────────────────────────────────────
def test_recoverable_set_unchanged():
    """The wall detector must NOT mutate _RECOVERABLE: plain 400 is still in it
    so generic recoverable 400s still advance the combo chain."""
    assert 400 in main._RECOVERABLE
    # The full documented recoverable set is preserved.
    expected = {400, 401, 403, 404, 405, 408, 409, 413, 422, 429,
               500, 502, 503, 504, 524, 525, 526}
    assert main._RECOVERABLE == expected


# ──────────────────────────────────────────────────────────────────────────────
# Provider-flavored bodies — the Gemini/Anthropic egress paths decode the raw
# upstream error body and pass (status_code, err_text) to the detector. Verify
# the detector classifies the real provider error shapes, not just compact ones.
# ──────────────────────────────────────────────────────────────────────────────
def test_wall_gemini_flavored_json_body():
    """A Gemini-flavored JSON error body (candidates[] absent, error block
    carrying data_inspection_failed) must classify as a terminal wall — this is
    the exact shape the Gemini egress path hands to the detector."""
    body = (
        '{"candidates":[],"promptFeedback":{},"error":{"code":400,'
        '"message":"data_inspection_failed: content rejected by safety filter",'
        '"status":"INVALID_ARGUMENT"}}'
    )
    assert main._is_terminal_relay_wall(400, body) is True


def test_wall_anthropic_flavored_body():
    """An Anthropic-shaped error body still trips the marker scan."""
    body = (
        '{"type":"error","error":{"type":"invalid_request_error",'
        '"message":"upstream_safety_blocked by content policy"}}'
    )
    assert main._is_terminal_relay_wall(400, body) is True


# ──────────────────────────────────────────────────────────────────────────────
# Egress coverage — the detector must be wired into the 3 streaming egress
# chokepoints so a wall never advances the chain / never wraps anywhere.
# Asserts the guard lines exist in the live source (source-level contract).
# ──────────────────────────────────────────────────────────────────────────────
def test_egress_guard_wiring_present():
    """The 3 unguarded streaming egress paths must now call the detector. This
    reads the live module source and asserts each chokepoint carries the guard,
    so a regression that drops a guard fails loudly instead of re-opening the
    infinite-retry loop on a terminal wall."""
    import inspect
    src = inspect.getsource(main)

    # Gap 1 — _raise_combo_wrap chokepoint (every caller's wrap becomes a no-op
    # on a wall).
    assert "_is_terminal_relay_wall(status_code, err_text)" in src, (
        "Gap 1: _raise_combo_wrap must guard on _is_terminal_relay_wall"
    )
    assert '[TerminalWall] suppressing never-stop wrap' in src, (
        "Gap 1: _raise_combo_wrap missing [TerminalWall] log line"
    )

    # Gap 2 — _raise_gemini_combo_fallback helper.
    assert '[TerminalWall] gemini egress - terminal relay wall' in src, (
        "Gap 2: _raise_gemini_combo_fallback missing [TerminalWall] log line"
    )

    # Gap 3 — Anthropic egress + Anthropic→OpenAI egress.
    assert "_is_wall_ae = _is_terminal_relay_wall(resp.status_code, err_text)" in src, (
        "Gap 3a: Anthropic egress missing _is_wall_ae detector call"
    )
    assert "if not _is_wall_ae and active_chain and _fb_next_idx < len(active_chain):" in src, (
        "Gap 3a: Anthropic egress chain-advance not gated on not _is_wall_ae"
    )
    assert "if not _is_wall_ae:" in src, (
        "Gap 3a: Anthropic egress trailing _raise_combo_wrap not guarded"
    )
    assert "_is_wall_ao = _is_terminal_relay_wall(resp.status_code, err_text)" in src, (
        "Gap 3b: Anthropic→OpenAI egress missing _is_wall_ao detector call"
    )
    assert "if not _is_wall_ao and active_chain and _fb_next_idx < len(active_chain):" in src, (
        "Gap 3b: Anthropic→OpenAI egress chain-advance not gated on not _is_wall_ao"
    )
    assert "if not _is_wall_ao and not _emit.emitted:" in src, (
        "Gap 3b: Anthropic→OpenAI egress trailing _raise_combo_wrap not guarded"
    )


def test_detector_is_single_definition():
    """The detector must be REUSED, not redefined. There must be exactly one
    `def _is_terminal_relay_wall(` in the module — the spec forbids a mirror."""
    import inspect
    src = inspect.getsource(main)
    assert src.count("def _is_terminal_relay_wall(") == 1, (
        "_is_terminal_relay_wall must be defined exactly once (reuse, not redefine)"
    )
