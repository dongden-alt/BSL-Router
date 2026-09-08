"""END console rows must be ONE physical line (Lane 2, 2026-09-07).

Multi-line error bodies (tracebacks, nested error dicts) used to break the
printed `[BSL][...] END` row across several physical lines, making
app.out.log END rows ungreppable. log_request must flatten every error
rendering: strings get newlines collapsed, dicts get compact JSON with
default=str, and _short_error_token stays untouched.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import app.observability as obs


@pytest.fixture(autouse=True)
def _isolate_persistence(tmp_path, monkeypatch):
    monkeypatch.setattr(obs, "_USAGE_LOG_PATH", str(tmp_path / "usage_stats.jsonl"))
    monkeypatch.setattr(obs, "_CONSOLE_LOG_PATH", str(tmp_path / "console_logs.jsonl"))


def _end_row(capsys):
    return capsys.readouterr().out


def _call_log_request(error_msg, status=502):
    obs.log_request(
        provider="test-prov",
        model="test-model",
        status=status,
        ttft=0.01,
        in_tokens=10,
        out_tokens=5,
        cached_tokens=0,
        config={},
        error_msg=error_msg,
        total_time=0.05,
        request_id=f"req_sl_{id(error_msg) % 100000}",
    )


def test_string_error_newlines_collapsed_to_one_line(capsys):
    _call_log_request("boom\nsecond line\n  third line")
    out = _end_row(capsys)
    end_rows = [ln for ln in out.splitlines() if "END" in ln]
    assert end_rows, "expected an END row on stdout"
    assert len(end_rows) == 1, f"END row split across lines:\n{out}"
    assert "boom\\nsecond line" in end_rows[0]
    assert "\\n  third line" in end_rows[0]


def test_traceback_error_greppable_on_end_line(capsys):
    traceback_body = 'Traceback (most recent call last):\n  File "x.py", line 1\nTimeoutError: upstream dead'
    _call_log_request(traceback_body)
    out = _end_row(capsys)
    end_rows = [ln for ln in out.splitlines() if "END" in ln]
    assert len(end_rows) == 1
    # A grep for a token from the error body finds it ON the END line.
    assert "TimeoutError:" in end_rows[0]
    assert "Traceback" in end_rows[0]


def test_dict_error_serialized_compact_single_line(capsys):
    err = {"type": "upstream_error", "detail": "line one\nline two", "code": 502}
    _call_log_request(err)
    out = _end_row(capsys)
    end_rows = [ln for ln in out.splitlines() if "END" in ln]
    assert len(end_rows) == 1, f"dict error broke the END row:\n{out}"
    assert '"type":"upstream_error"' in end_rows[0]
    assert "line one\\nline two" in end_rows[0]
    # No raw newline survived inside the rendered error section.
    assert "line one\nline two" not in end_rows[0]


def test_non_serializable_object_uses_default_str(capsys):
    class Weird:
        def __str__(self):
            return "weird-object\nwith newline"

    _call_log_request({"obj": Weird(), "nested": {"k": [1, 2]}})
    out = _end_row(capsys)
    end_rows = [ln for ln in out.splitlines() if "END" in ln]
    assert len(end_rows) == 1, f"default=str path broke the END row:\n{out}"
    assert "weird-object\\nwith newline" in end_rows[0]


def test_short_error_token_unchanged():
    """_short_error_token keeps its own contract (separate column)."""
    assert obs._short_error_token(None) is None
    assert obs._short_error_token("a\nb") == "a b"
    assert obs._short_error_token("x" * 200) == "x" * 80


def test_single_line_error_helper_direct():
    assert obs._single_line_error(None) is None
    assert obs._single_line_error("a\r\nb\rc\nd") == "a\\nb\\nc\\nd"
    assert obs._single_line_error({"b": 1}) == '{"b":1}'
    # max_len truncates the flattened text, never adds newlines.
    out = obs._single_line_error("y" * 300, max_len=100)
    assert len(out) == 100 and "\n" not in out
