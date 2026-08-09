"""Tests for GLM/DeepSeek unicode tool-call delimiter normalization.

These tests guard the middleware in ``app/middleware/glm_tools.py`` that
rescues tool calls emitted as inline unicode-delimited text rather than
structured ``tool_calls`` arrays.

GLM, DeepSeek, and Qwen model families emit tool-call blocks whose special
tokens render with glyphs that are NOT plain ASCII:

  - the pipe may be ASCII '|' (U+007C) or FULLWIDTH VERTICAL LINE
    U+FF5C (rendered as a wide vertical bar)
  - the separator between "tool", "calls", and "begin"/"end" may be ASCII
    '_' (U+005F) or LOWER ONE EIGHTH BLOCK U+2581 (rendered as a low bar)

The regexes therefore accept every combination. All non-ASCII characters in
this file are written as explicit unicode escapes (e.g. backslash-u-FF5C) so
that no editor or filesystem encoding round-trip can corrupt them.
"""

from app.middleware.glm_tools import normalize_glm_tool_calls


# Written as explicit unicode escapes so encoding cannot corrupt them.
PIPE = "｜"  # FULLWIDTH VERTICAL LINE
BLK = "▁"   # LOWER ONE EIGHTH BLOCK


def _wrap(content: str) -> dict:
    """Build a minimal OpenAI-format chat completion payload."""
    return {
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ]
    }


def _calls(payload: dict):
    return payload["choices"][0]["message"].get("tool_calls")


def _content(payload: dict):
    return payload["choices"][0]["message"].get("content")


# ── 1. ASCII regression guard ─────────────────────────────────────────────────
def test_ascii_delimiters_still_parse():
    ascii_form = (
        '<|tool_calls_begin|>'
        '{"name":"Read","arguments":{"path":"x"}}'
        '<|tool_calls_end|>'
    )
    out, changed = normalize_glm_tool_calls(_wrap(ascii_form), "glm-4")
    assert changed is True
    calls = _calls(out)
    assert calls is not None and len(calls) == 1
    assert calls[0]["function"]["name"] == "Read"
    assert calls[0]["function"]["arguments"] == '{"path": "x"}'


# ── 2. Fullwidth delimiters (exact L33 comment form) ─────────────────────────
def test_fullwidth_delimiters_parse():
    fw_form = (
        f'<{PIPE}tool{BLK}calls{BLK}begin{PIPE}>'
        f'{{"name":"Read","arguments":{{"path":"x"}}}}'
        f'<{PIPE}tool{BLK}calls{BLK}end{PIPE}>'
    )
    out, changed = normalize_glm_tool_calls(_wrap(fw_form), "glm-4")
    assert changed is True
    calls = _calls(out)
    assert calls is not None and len(calls) == 1
    assert calls[0]["function"]["name"] == "Read"
    assert calls[0]["function"]["arguments"] == '{"path": "x"}'


# ── 3. Fullwidth pipe + ASCII underscore mixed form ──────────────────────────
def test_fullwidth_pipe_with_ascii_underscore_parses():
    mixed = (
        f'<{PIPE}tool_calls_begin{PIPE}>'
        f'{{"name":"Read","arguments":{{"path":"x"}}}}'
        f'<{PIPE}tool_calls_end{PIPE}>'
    )
    out, changed = normalize_glm_tool_calls(_wrap(mixed), "glm-4")
    assert changed is True
    calls = _calls(out)
    assert calls is not None and len(calls) == 1
    assert calls[0]["function"]["name"] == "Read"


# ── 4. No raw markup leaks into content ──────────────────────────────────────
def test_no_raw_markup_leaks_into_content():
    fw_form = (
        f'<{PIPE}tool{BLK}calls{BLK}begin{PIPE}>'
        f'{{"name":"Read","arguments":{{"path":"x"}}}}'
        f'<{PIPE}tool{BLK}calls{BLK}end{PIPE}>'
    )
    out, changed = normalize_glm_tool_calls(_wrap(fw_form), "glm-4")
    assert changed is True
    content = _content(out)
    # When the entire content was tool-call markup, the normalized content is
    # empty/None — never the raw markup itself.
    assert content in (None, "")
    assert content is None or PIPE not in content
    assert content is None or BLK not in content
    assert content is None or "tool_calls_begin" not in (content or "")


# ── 5. DeepSeek native format (inner markers + ```json fence) ───────────────
def test_deepseek_native_format_parses():
    payload = (
        f'<{PIPE}tool{BLK}calls{BLK}begin{PIPE}>'
        f'{PIPE}tool{BLK}call{BLK}begin{PIPE}'
        f'function{PIPE}tool{BLK}call{BLK}sep{PIPE}read_file'
        f'\n```json\n{{"file": "a.txt"}}\n```'
        f'{PIPE}tool{BLK}call{BLK}end{PIPE}'
        f'<{PIPE}tool{BLK}calls{BLK}end{PIPE}>'
    )
    out, changed = normalize_glm_tool_calls(_wrap(payload), "deepseek-chat")
    assert changed is True
    calls = _calls(out)
    assert calls is not None and len(calls) == 1
    assert calls[0]["function"]["name"] == "read_file"
    assert calls[0]["function"]["arguments"] == '{"file": "a.txt"}'
    # finish_reason should reflect the tool call
    assert out["choices"][0]["finish_reason"] == "tool_calls"
    # No raw markup leaks
    assert _content(out) in (None, "")


# ── 6. Plain prose passes through unchanged ─────────────────────────────────
def test_non_tool_content_is_untouched():
    prose = "Hello! I can help with that. Here is a list:\n- one\n- two"
    out, changed = normalize_glm_tool_calls(_wrap(prose), "glm-4")
    assert changed is False
    assert _content(out) == prose
    assert _calls(out) is None


# ── 7. Multiple tool calls in one outer block ───────────────────────────────
def test_multiple_tool_calls_in_one_block():
    payload = (
        f'<{PIPE}tool{BLK}calls{BLK}begin{PIPE}>'
        f'{PIPE}tool{BLK}call{BLK}begin{PIPE}'
        f'function{PIPE}tool{BLK}call{BLK}sep{PIPE}first_tool'
        f'\n```json\n{{"a": 1}}\n```'
        f'{PIPE}tool{BLK}call{BLK}end{PIPE}'
        f'{PIPE}tool{BLK}call{BLK}begin{PIPE}'
        f'function{PIPE}tool{BLK}call{BLK}sep{PIPE}second_tool'
        f'\n```json\n{{"b": 2}}\n```'
        f'{PIPE}tool{BLK}call{BLK}end{PIPE}'
        f'<{PIPE}tool{BLK}calls{BLK}end{PIPE}>'
    )
    out, changed = normalize_glm_tool_calls(_wrap(payload), "deepseek-chat")
    assert changed is True
    calls = _calls(out)
    assert calls is not None and len(calls) == 2
    assert calls[0]["function"]["name"] == "first_tool"
    assert calls[0]["function"]["arguments"] == '{"a": 1}'
    assert calls[1]["function"]["name"] == "second_tool"
    assert calls[1]["function"]["arguments"] == '{"b": 2}'


# ── 8. Junction asymmetry regression (AUDIT 2026-08-05) ──────────────────────
#
# The outer regex was widened to accept a PIPE at word junctions, but the inner
# regex kept a narrower '_'/U+2581-only class. DeepSeek emitting pipe junctions
# in its INNER markers therefore matched the outer block, failed the inner
# match, produced ZERO tool calls, and leaked raw markup into assistant text —
# the exact symptom the delimiter fix was written to eliminate.
#
# These tests bind to that asymmetry: reverting _GLM_INNER_TC_RE to a '[_▁]'
# class must fail tests 8 and 9.
def test_inner_markers_with_pipe_junctions():
    """Inner call markers using PIPE at every junction must still parse."""
    payload = (
        f'<{PIPE}tool{PIPE}calls{PIPE}begin{PIPE}>'
        f'{PIPE}tool{PIPE}call{PIPE}begin{PIPE}'
        f'function{PIPE}tool{PIPE}call{PIPE}sep{PIPE}read_file'
        f'\n```json\n{{"path": "a.txt"}}\n```'
        f'{PIPE}tool{PIPE}call{PIPE}end{PIPE}'
        f'<{PIPE}tool{PIPE}calls{PIPE}end{PIPE}>'
    )
    out, changed = normalize_glm_tool_calls(_wrap(payload), "deepseek-v4-pro")
    assert changed is True
    calls = _calls(out)
    assert calls is not None and len(calls) == 1
    # The "function|tool|call|sep|" prefix must be stripped from the name.
    assert calls[0]["function"]["name"] == "read_file"
    assert calls[0]["function"]["arguments"] == '{"path": "a.txt"}'
    # No raw markup may survive into the assistant text.
    assert PIPE not in (_content(out) or "")


def test_mixed_outer_sep_inner_pipe_junctions():
    """Models mix forms freely: outer may use U+2581 while inner uses PIPE."""
    payload = (
        f'<{PIPE}tool{BLK}calls{BLK}begin{PIPE}>'
        f'{PIPE}tool{PIPE}call{PIPE}begin{PIPE}'
        f'function{PIPE}tool{PIPE}call{PIPE}sep{PIPE}list_dir'
        f'\n```json\n{{"dir": "."}}\n```'
        f'{PIPE}tool{PIPE}call{PIPE}end{PIPE}'
        f'<{PIPE}tool{BLK}calls{BLK}end{PIPE}>'
    )
    out, changed = normalize_glm_tool_calls(_wrap(payload), "deepseek-v4-pro")
    assert changed is True
    calls = _calls(out)
    assert calls is not None and len(calls) == 1
    assert calls[0]["function"]["name"] == "list_dir"
    assert calls[0]["function"]["arguments"] == '{"dir": "."}'


# ── 9. Malformed arguments are forwarded, not silently emptied ───────────────
def test_malformed_arguments_are_forwarded_verbatim():
    """Unparseable arguments must reach the client, not be replaced with '{}'.

    Substituting an empty object produces a plausible-but-wrong call that the
    model cannot distinguish from success, so it never retries. Forwarding keeps
    the failure visible and lets the model self-correct.
    """
    payload = (
        f'<{PIPE}tool{BLK}calls{BLK}begin{PIPE}>'
        f'{PIPE}tool{BLK}call{BLK}begin{PIPE}'
        f'function{PIPE}tool{BLK}call{BLK}sep{PIPE}write_file'
        f'\n```json\n{{"path": "a.txt", "content": "unterminated\n```'
        f'{PIPE}tool{BLK}call{BLK}end{PIPE}'
        f'<{PIPE}tool{BLK}calls{BLK}end{PIPE}>'
    )
    out, _ = normalize_glm_tool_calls(_wrap(payload), "deepseek-v4-pro")
    calls = _calls(out)
    assert calls is not None and len(calls) == 1
    args = calls[0]["function"]["arguments"]
    assert args != "{}", "malformed arguments must not be silently emptied"
    assert "unterminated" in args


# ── 10. Tool-call ids: unique per call, stable across processes ──────────────
def test_identical_parallel_calls_get_distinct_ids():
    """Two identical calls must not share an id — it breaks correlation."""
    inner = (
        f'{PIPE}tool{BLK}call{BLK}begin{PIPE}'
        f'function{PIPE}tool{BLK}call{BLK}sep{PIPE}read_file'
        f'\n```json\n{{"path": "a.txt"}}\n```'
        f'{PIPE}tool{BLK}call{BLK}end{PIPE}'
    )
    payload = (
        f'<{PIPE}tool{BLK}calls{BLK}begin{PIPE}>'
        + inner + inner +
        f'<{PIPE}tool{BLK}calls{BLK}end{PIPE}>'
    )
    out, _ = normalize_glm_tool_calls(_wrap(payload), "deepseek-v4-pro")
    calls = _calls(out)
    assert calls is not None and len(calls) == 2
    assert calls[0]["id"] != calls[1]["id"], "identical parallel calls collided"


def test_tool_call_id_is_deterministic():
    """Ids must not depend on PYTHONHASHSEED (randomized per process)."""
    from app.middleware.glm_tools import _make_tool_call_id

    first = _make_tool_call_id(0, "read_file", '{"path": "a.txt"}')
    second = _make_tool_call_id(0, "read_file", '{"path": "a.txt"}')
    assert first == second
    assert first.startswith("call_")
    # Index participates, so the same call at a different position differs.
    assert _make_tool_call_id(1, "read_file", '{"path": "a.txt"}') != first
