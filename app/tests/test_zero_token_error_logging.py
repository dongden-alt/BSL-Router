"""BUG6: terminal 200 with 0/0 tokens must be reclassified as 502 empty error.

Only when no first token ever arrived (ttft falsy). Content that streamed
with missing usage is a telemetry gap and must stay status 200.
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
    obs.console_logs.clear()
    obs.usage_stats.clear()
    if hasattr(obs, "_terminal_end_registry"):
        obs._terminal_end_registry.clear()
    yield
    obs.console_logs.clear()
    obs.usage_stats.clear()
    if hasattr(obs, "_terminal_end_registry"):
        obs._terminal_end_registry.clear()


def test_zero_token_200_end_reclassified_as_502_empty():
    """No first token (ttft falsy) + 0/0 tokens → reclassify 200 → 502 empty."""
    obs.log_request(
        provider="vsllm",
        model="coder-2",
        status=200,
        ttft=0.0,
        in_tokens=0,
        out_tokens=0,
        cached_tokens=0,
        config={},
        total_time=0.5,
        request_id="req_empty_200",
        client="openai",
        stream=True,
        error_msg=None,
    )

    ends = [e for e in obs.console_logs if e.get("event") == "end"]
    assert len(ends) == 1
    assert ends[0]["status"] == 502
    assert ends[0]["error"] == "empty"
    # D1 error-column contract (2026-09-06j): terminal error events DO record
    # a usage row (status 502 + error='empty', 0/0 tokens) so the error-rate
    # card and error column have data — previously errors wrote no row.
    assert len(obs.usage_stats) == 1
    assert obs.usage_stats[0].get("error") == "empty"


def test_zero_token_with_ttft_stays_200():
    """Content flowed (ttft>0) but usage omitted (0/0) is a telemetry gap, not empty."""
    obs.log_request(
        provider="vsllm",
        model="coder-2",
        status=200,
        ttft=0.5,
        in_tokens=0,
        out_tokens=0,
        cached_tokens=0,
        config={},
        total_time=1.0,
        request_id="req_usage_gap",
        client="openai",
        stream=True,
        error_msg=None,
    )

    ends = [e for e in obs.console_logs if e.get("event") == "end"]
    assert len(ends) == 1
    assert ends[0]["status"] == 200
    assert "error" not in ends[0]
    # Stays on the success path: usage block runs (may record a 0-token row).
    assert len(obs.usage_stats) == 1
    assert obs.usage_stats[0].get("out") == 0


def test_zero_token_with_explicit_error_not_overwritten():
    obs.log_request(
        provider="vsllm",
        model="coder-2",
        status=200,
        ttft=0.1,
        in_tokens=0,
        out_tokens=0,
        cached_tokens=0,
        config={},
        error_msg="upstream_reset",
        request_id="req_err_kept",
        client="openai",
        stream=True,
    )
    ends = [e for e in obs.console_logs if e.get("event") == "end"]
    assert len(ends) == 1
    # When error_msg is already set, reclassify condition requires error_msg is None
    # so status stays 200 unless other logic changes it — verify empty rewrite does not fire
    assert ends[0].get("error") == "upstream_reset"


def test_nonzero_tokens_200_stays_success():
    obs.log_request(
        provider="vsllm",
        model="coder-2",
        status=200,
        ttft=0.1,
        in_tokens=10,
        out_tokens=5,
        cached_tokens=0,
        config={},
        request_id="req_ok",
        client="openai",
        stream=False,
    )
    ends = [e for e in obs.console_logs if e.get("event") == "end"]
    assert len(ends) == 1
    assert ends[0]["status"] == 200
    assert "error" not in ends[0]


def test_start_event_not_reclassified():
    """Reclassify only applies to event == end."""
    # log_request_start uses event start; calling log_request with event start if supported
    obs.log_request(
        provider="vsllm",
        model="coder-2",
        status=200,
        ttft=0,
        in_tokens=0,
        out_tokens=0,
        cached_tokens=0,
        config={},
        request_id="req_startish",
        event="start",
    )
    starts = [e for e in obs.console_logs if e.get("event") == "start"]
    assert starts
    assert starts[0]["status"] == 200
