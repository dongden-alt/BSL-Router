"""Mid-stream transport death + continuation splice: TRANSPORT_DIED_PARTIAL_FLAG
gate semantics and the continuation status-guard helper (DEFECT B2,
"force stop 502", 2026-10-09).

LOUD NOTICE - faithful local reimplementation:
The real ``_continuation_status_guard`` closure and the two real gates (the
``out == 0`` entry gate and the ``_delivered`` flag gate) live in nested
functions of the request handler inside the ~16k-line ``app/main.py`` and are
genuinely unreachable offline: reaching them requires a live upstream
transport death mid-stream. The behaviour tests below therefore exercise a
FAITHFUL LOCAL REIMPLEMENTATION of those paths. They are pinned to the real
source by the source-text tests (test_gate_* / test_helper_*), which parse
``app/main.py`` directly, so a refactor that changes the real gates or the
helper contract fails this file even though the reimplementation is local.
"""

import pathlib
import re

from app.middleware.quality import TRANSPORT_DIED_PARTIAL_FLAG

MAIN_PY = pathlib.Path(__file__).resolve().parents[1] / "main.py"

_FALLBACK_DEF = r"(?m)^[ \t]*(?:async )?def _midstream_transport_fallback\b"
_GUARDED_DEF = r"(?m)^[ \t]*(?:async )?def _transport_guarded\b"


# ---------------------------------------------------------------------------
# tiny local fakes - no network, no app.main import, no monkeypatching
# ---------------------------------------------------------------------------


class _FakeEmit(object):
    """Stand-in for the stream_guard emit helper (emitted / byte_count)."""

    def __init__(self, emitted=False, byte_count=0):
        self.emitted = emitted
        self.byte_count = byte_count


class _FakeDetector(object):
    """Stand-in for the anti-stop detector (partial_text only)."""

    def __init__(self, partial_text=""):
        self.partial_text = partial_text


def _dead_stats():
    """stats as the mid-stream transport death leaves them (502 + flag set)."""
    return {
        "status": 502,
        "error": "mid-stream transport death",
        "out": 0,
        TRANSPORT_DIED_PARTIAL_FLAG: True,
    }


# ---------------------------------------------------------------------------
# faithful local reimplementation of the two gates and the commit helper
# (pinned to the real source by the source-text tests below)
# ---------------------------------------------------------------------------


def _flag_gates(stats, emitted, partial_text):
    """Mirror of the two gates in _midstream_transport_fallback (app/main.py):

    _delivered = emitted OR partial_text OR out > 0
    entry gate: ``stats.get("out", 0) == 0`` - NOT ``_delivered``;
    may_fallback() inside the block is the real emission veto.
    flag gate: ``if _delivered:`` sets TRANSPORT_DIED_PARTIAL_FLAG.
    """
    _delivered = bool(emitted) or bool(partial_text) or stats.get("out", 0) > 0
    entry_gate_opens = stats.get("out", 0) == 0
    flag_set = bool(_delivered)
    return _delivered, entry_gate_opens, flag_set


def _make_commit(stats, detector, emit):
    """Faithful reimplementation of app/main.py's _continuation_status_guard.

    Captures pre-splice status/error/text/bytes and returns
    ``_commit(chunks_yielded, cont_bytes)`` which restores stats to
    200/None and pops TRANSPORT_DIED_PARTIAL_FLAG only when the splice
    delivered content AND stats' status/error still match the captured
    pre-splice values (a second transport death writes a fresh 502/error
    that must survive).
    """
    _pre_status = stats.get("status")
    _pre_error = stats.get("error")
    _pre_text_len = len(detector.partial_text or "")
    _pre_bytes = int(getattr(emit, "byte_count", 0) or 0)

    def _commit(chunks_yielded, cont_bytes):
        if chunks_yielded <= 0:
            return
        if not (
            cont_bytes > 0
            or len(detector.partial_text or "") > _pre_text_len
            or int(getattr(emit, "byte_count", 0) or 0) > _pre_bytes
        ):
            return
        if stats.get("status") != _pre_status or stats.get("error") != _pre_error:
            return
        stats["status"] = 200
        stats["error"] = None
        stats.pop(TRANSPORT_DIED_PARTIAL_FLAG, None)

    return _commit


def _gemini_terminal_error_would_emit(stats):
    """Mirror of the Gemini-lane terminal-error guard (~L12188 in app/main.py):
    it only fires when stats' error is truthy AND status is an int in
    [400, 500) and not 499. A restored 200/None makes it False, so no
    terminal_error_frame is yielded and the stream ends on SSE_DONE."""
    error = stats.get("error")
    status = stats.get("status")
    return bool(error and isinstance(status, int) and 400 <= status and status != 499)


# ---------------------------------------------------------------------------
# source-text helpers: read app/main.py, strip full-line comments, normalise
# (a comment *documenting* a line must not satisfy a presence assertion)
# ---------------------------------------------------------------------------


def _main_source():
    return MAIN_PY.read_text(encoding="utf-8")


def _fallback_region():
    """Source from the _midstream_transport_fallback def up to (excluding) the
    _transport_guarded def - includes the FIX 2 helper, which sits between."""
    src = _main_source()
    start = re.search(_FALLBACK_DEF, src)
    assert start is not None, "def _midstream_transport_fallback not found"
    rest = src[start.end():]
    end = re.search(_GUARDED_DEF, rest)
    assert end is not None, "def _transport_guarded not found after fallback"
    return src[start.start(): start.end() + end.start()]


def _region_code():
    """Comment-stripped, whitespace-normalised fallback region lines.

    Stripping comments before matching is required: a comment *documenting* a
    removed line would otherwise satisfy a presence assertion (repo gotcha).
    """
    stripped = "\n".join(
        line
        for line in _fallback_region().splitlines()
        if not line.lstrip().startswith("#")
    )
    normalised = [" ".join(line.split()) for line in stripped.splitlines()]
    return "\n" + "\n".join(normalised) + "\n"


# ---------------------------------------------------------------------------
# source-text tests: pin the real gates and the real helper contract
# ---------------------------------------------------------------------------


def test_entry_gate_is_out_zero_not_delivered():
    code = _region_code()
    # ENTRY gate is the ORIGINAL out == 0 test, NOT _delivered: a thought-only
    # Gemini stream (emitted=False, out=0, non-empty partial_text) must still
    # reach the combo failover block, where may_fallback() is the real veto.
    assert "\nif stats.get(\"out\", 0) == 0:\n" in code
    assert "\nif not _delivered:\n" not in code


def test_flag_gate_is_delivered():
    code = _region_code()
    assert "\nif _delivered:\n" in code


def test_delivered_or_terms():
    code = _region_code()
    needle = (
        "\n_delivered = bool(getattr(_emit, \"emitted\", False)) "
        "or bool(partial_text) or stats.get(\"out\", 0) > 0\n"
    )
    assert needle in code
    assert "2026-10-09" in _fallback_region()


def test_helper_source_contract():
    code = _region_code()
    region = _fallback_region()
    for needle in (
        "\ndef _continuation_status_guard(_emit):\n",
        "\n_pre_status = stats.get(\"status\")\n",
        "\n_pre_error = stats.get(\"error\")\n",
        "\n_pre_text_len = len(_detector.partial_text or \"\")\n",
        "\n_pre_bytes = int(getattr(_emit, \"byte_count\", 0) or 0)\n",
        "\ndef _commit(chunks_yielded: int, cont_bytes: int) -> None:\n",
        "\nstats[\"status\"] = 200\n",
        "\nstats[\"error\"] = None\n",
        "\nstats.pop(TRANSPORT_DIED_PARTIAL_FLAG, None)\n",
        "\nflush=True,\n",
    ):
        assert needle in code, "missing in app/main.py: %s" % needle.strip()
    assert "continuation restored status 200 after transport death" in region
    # the helper docstring must cite the defect and the fix date
    assert "DEFECT B2" in region
    assert "2026-10-09" in region


def test_helper_single_definition_and_call_sites():
    src = _main_source()
    # ONE shared helper, no copy-paste variants at the splice sites...
    assert src.count("def _continuation_status_guard") == 1
    # ...and exactly three call sites (1 def line + 3 calls = 4 occurrences)
    assert src.count("_continuation_status_guard(_emit)") == 4


def test_commit_called_at_all_three_splice_sites():
    src = _main_source()
    # OpenAI egress, Anthropic egress (_raw_ok_chain), Gemini egress
    # (_openai_source_chain) all commit through the same helper + counters.
    assert src.count("_cont_commit(_cont_chunks, _cont_bytes)") == 3


def test_openai_splice_feeds_detector():
    src = _main_source()
    lines = src.splitlines()
    anchor = next(
        i for i, line in enumerate(lines) if '_attempt, "openai-cont"):' in line
    )
    # Window the splice loop by LINE, never by raw character count. A fixed
    # character slice starting mid-line at the anchor is silently eaten by any
    # comment block placed above the loop body: the implementation legitimately
    # documents the fix in an 8-line comment right there, which pushed
    # `_emit.mark_emitted(_cc)` out of a 900-char window and turned a passing
    # invariant into `ValueError: substring not found`. Walk forward to the
    # loop's own `yield _cc` (comment lines cannot terminate the search), with a
    # hard cap so a missing yield fails loudly instead of scanning the file.
    end = anchor
    limit = min(len(lines), anchor + 60)
    while end < limit:
        stripped = lines[end].lstrip()
        if not stripped.startswith("#") and stripped.startswith("yield _cc"):
            break
        end += 1
    assert end < limit, "openai-cont splice loop never yields _cc within 60 lines"
    window = "\n".join(
        line
        for line in lines[anchor: end + 1]
        if not line.lstrip().startswith("#")
    )
    assert "_detector.feed(_cc)" in window
    # Presence is asserted BEFORE the ordering comparison on purpose: without it
    # a future removal of mark_emitted surfaces as a ValueError from str.index
    # rather than a readable assertion. Comment-stripping stays mandatory (repo
    # gotcha: a comment documenting a removal otherwise satisfies an absence
    # assertion).
    assert "_emit.mark_emitted(_cc)" in window
    assert window.index("_detector.feed(_cc)") < window.index("_emit.mark_emitted(_cc)")


# ---------------------------------------------------------------------------
# behaviour tests: flag gate semantics (reported bug)
# ---------------------------------------------------------------------------


def test_flag_set_when_emitted_and_partial_text():
    # the reported bug: content WAS delivered before the death, so the flag
    # must be set even though out == 0 (usage frame never arrived).
    stats = {"status": 502, "error": "died", "out": 0}
    delivered, entry_opens, flag_set = _flag_gates(
        stats, emitted=True, partial_text="buffered reasoning"
    )
    assert delivered is True
    assert flag_set is True


def test_flag_not_set_when_nothing_delivered():
    stats = {"status": 502, "error": "died", "out": 0}
    delivered, entry_opens, flag_set = _flag_gates(stats, emitted=False, partial_text="")
    assert delivered is False
    assert flag_set is False


def test_flag_set_on_out_gt_zero_backcompat():
    stats = {"status": 502, "error": "died", "out": 512}
    delivered, entry_opens, flag_set = _flag_gates(stats, emitted=False, partial_text="")
    assert delivered is True
    assert flag_set is True


def test_flag_set_and_entry_gate_opens_for_thought_only_stream():
    # STEP 1 regression pin: a thought-only stream (emitted=False, out=0,
    # non-empty partial_text) sets the flag AND still opens the entry gate,
    # so the combo failover block stays reachable and may_fallback() - the
    # real emission veto - decides, not the entry gate.
    stats = {"status": 502, "error": "died", "out": 0}
    delivered, entry_opens, flag_set = _flag_gates(
        stats, emitted=False, partial_text="thought deltas buffered pre-commit"
    )
    assert delivered is True
    assert entry_opens is True
    assert flag_set is True


# ---------------------------------------------------------------------------
# behaviour tests: helper commit semantics
# ---------------------------------------------------------------------------


def test_commit_restores_200_on_success():
    stats = _dead_stats()
    detector = _FakeDetector("")
    commit = _make_commit(stats, detector, _FakeEmit())
    detector.partial_text = "complete answer body from the continuation"
    commit(3, 87)
    assert stats["status"] == 200
    assert stats["error"] is None
    assert TRANSPORT_DIED_PARTIAL_FLAG not in stats


def test_commit_noop_when_no_chunks():
    stats = _dead_stats()
    commit = _make_commit(stats, _FakeDetector(""), _FakeEmit())
    commit(0, 999)
    assert stats["status"] == 502
    assert stats["error"] == "mid-stream transport death"
    assert TRANSPORT_DIED_PARTIAL_FLAG in stats


def test_commit_noop_when_no_growth():
    stats = _dead_stats()
    detector = _FakeDetector("frozen text")
    emit = _FakeEmit(byte_count=10)
    commit = _make_commit(stats, detector, emit)
    commit(2, 0)  # empty chunks, no text growth, no byte_count growth
    assert stats["status"] == 502
    assert stats["error"] is not None
    assert TRANSPORT_DIED_PARTIAL_FLAG in stats


def test_commit_success_via_text_growth_only():
    # cont_bytes == 0 (local byte count can lag) but detector text grew:
    # condition 2 is an OR, so the restore still fires.
    stats = _dead_stats()
    detector = _FakeDetector("")
    commit = _make_commit(stats, detector, _FakeEmit())
    detector.partial_text = "grew during the splice"
    commit(1, 0)
    assert stats["status"] == 200
    assert stats["error"] is None
    assert TRANSPORT_DIED_PARTIAL_FLAG not in stats


def test_commit_success_via_lagged_byte_count_only():
    stats = _dead_stats()
    emit = _FakeEmit()
    commit = _make_commit(stats, _FakeDetector(""), emit)
    emit.byte_count = 64
    commit(1, 0)
    assert stats["status"] == 200
    assert stats["error"] is None


def test_commit_preserves_second_death():
    # condition 3: the continuation ITSELF died mid-way, so
    # _midstream_transport_fallback ran again and wrote a FRESH 502 + error
    # into stats; the commit must NOT mask that genuine second death.
    stats = _dead_stats()
    detector = _FakeDetector("")
    commit = _make_commit(stats, detector, _FakeEmit())
    stats["status"] = 502
    stats["error"] = "second transport death during continuation"
    detector.partial_text = "partial continuation text"
    commit(5, 120)
    assert stats["status"] == 502
    assert stats["error"] == "second transport death during continuation"
    assert TRANSPORT_DIED_PARTIAL_FLAG in stats

    # a changed status value alone also blocks the restore
    stats2 = _dead_stats()
    commit2 = _make_commit(stats2, _FakeDetector(""), _FakeEmit())
    stats2["status"] = 500
    commit2(5, 120)
    assert stats2["status"] == 500
    assert stats2["error"] == "mid-stream transport death"


def test_gemini_lane_terminal_error_frame_suppressed_after_restore():
    stats = _dead_stats()
    # before the restore: the Gemini-lane 400<=status guard WOULD emit a
    # terminal_error_frame(502) on top of a complete answer ("force stop 502")
    assert _gemini_terminal_error_would_emit(stats) is True
    detector = _FakeDetector("")
    commit = _make_commit(stats, detector, _FakeEmit())
    detector.partial_text = "complete answer"
    commit(4, 33)
    assert stats["status"] == 200
    assert stats["error"] is None
    # after the restore: the guard predicate is False -> no terminal error
    # frame, the stream ends on SSE_DONE
    assert _gemini_terminal_error_would_emit(stats) is False
