import json

import pytest

import app.observability as obs


@pytest.fixture(autouse=True)
def _isolate_persistence(tmp_path, monkeypatch):
    """Redirect JSONL persistence paths to a fresh temp dir for every test.

    The module loads persisted history from disk at import time, which could
    leak real-server data into test assertions. Pointing both paths at a
    per-test temp directory keeps the in-memory lists clean (reloaded empty
    per test) and isolates file writes from the real data/ directory.
    """
    monkeypatch.setattr(obs, "_USAGE_LOG_PATH", str(tmp_path / "usage_stats.jsonl"))
    monkeypatch.setattr(obs, "_CONSOLE_LOG_PATH", str(tmp_path / "console_logs.jsonl"))
    # Persistence is append-only; start each test with empty in-memory state
    # so length assertions are deterministic regardless of import-time loads.
    obs.console_logs.clear()
    obs.usage_stats.clear()
    yield


def setup_function():
    obs.console_logs.clear()
    obs.usage_stats.clear()


def test_log_request_start_appends_and_prints(capsys):
    request_id = obs.log_request_start(
        provider="vietapi",
        model="coder-2",
        config={},
        stream=True,
        client="anthropic",
        upstream_url="https://example.com/v1/messages",
        request_id="req_test",
    )

    captured = capsys.readouterr().out
    assert request_id == "req_test"
    assert "[BSL][req_test] START" in captured
    assert "client=anthropic" in captured
    assert "provider=vietapi" in captured
    assert len(obs.console_logs) == 1
    assert obs.console_logs[0]["event"] == "start"
    assert obs.console_logs[0]["request_id"] == "req_test"
    assert obs.console_logs[0]["stream"] is True


def test_log_request_end_appends_prints_and_tracks_usage(capsys):
    obs.log_request(
        provider="vsllm",
        model="coder-3",
        status=200,
        ttft=0.1234,
        in_tokens=100,
        out_tokens=50,
        cached_tokens=10,
        config={},
        total_time=1.2345,
        request_id="req_done",
        client="anthropic",
        stream=False,
        upstream_url="https://example.com/v1/chat/completions",
    )

    captured = capsys.readouterr().out
    assert "[BSL][req_done] END" in captured
    assert "status=200" in captured
    assert "ttft=123.4ms" in captured
    assert "in=100 out=50 cached=10" in captured
    assert len(obs.console_logs) == 1
    entry = obs.console_logs[0]
    assert entry["event"] == "end"
    assert entry["request_id"] == "req_done"
    assert entry["client"] == "anthropic"
    assert entry["stream"] is False
    assert len(obs.usage_stats) == 1
    assert obs.usage_stats[0]["out"] == 50


def test_trace_redacts_secret_like_values(capsys):
    obs.log_request_start(
        provider="secret-provider",
        model="model-x",
        config={"providers": {"p": {"connections": [{"api_key": "sk-secret"}]}}},
        stream=False,
        client="openai",
        upstream_url="https://example.com/v1?api_key=sk-secret",
        request_id="req_secret",
    )
    obs.log_request(
        provider="secret-provider",
        model="model-x",
        status=500,
        ttft=0,
        in_tokens=0,
        out_tokens=0,
        cached_tokens=0,
        config={},
        error_msg="Authorization Bearer sk-secret failed",
        request_id="req_secret",
        client="openai",
        stream=False,
        upstream_url="https://example.com/v1?api_key=sk-secret",
    )

    captured = capsys.readouterr().out
    assert "sk-secret" not in captured
    assert "[redacted]" in captured
    assert obs.console_logs[0]["upstream_url"] == "[redacted]"
    assert obs.console_logs[1]["error"] == "[redacted]"


def _read_jsonl(path):
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def test_usage_stats_persisted_to_file(capsys):
    obs.log_request(
        provider="vietapi",
        model="coder-2",
        status=200,
        ttft=0.1,
        in_tokens=100,
        out_tokens=50,
        cached_tokens=10,
        config={},
        total_time=1.0,
        request_id="req_persist_u",
        client="anthropic",
        stream=False,
    )
    capsys.readouterr()  # drain stdout trace

    lines = _read_jsonl(obs._USAGE_LOG_PATH)
    assert len(lines) >= 1
    last = lines[-1]
    assert last["provider"] == "vietapi"
    assert last["model"] == "coder-2"
    assert last["out"] == 50
    assert last["cost"] is not None


def test_console_logs_persisted_to_file(capsys):
    obs.log_request_start(
        provider="vietapi",
        model="coder-2",
        config={},
        stream=True,
        client="anthropic",
        upstream_url="https://example.com/v1/messages",
        request_id="req_persist_c",
    )
    capsys.readouterr()  # drain stdout trace

    lines = _read_jsonl(obs._CONSOLE_LOG_PATH)
    assert len(lines) >= 1
    last = lines[-1]
    assert last["event"] == "start"
    assert last["request_id"] == "req_persist_c"
    assert last["model"] == "coder-2"


def test_load_persisted_reads_file(tmp_path):
    p = tmp_path / "console_logs.jsonl"
    entries = [
        {"event": "start", "n": 1},
        {"event": "end", "n": 2},
        {"event": "start", "n": 3},
    ]
    with open(p, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")

    # Full read returns all entries in order.
    loaded = obs._load_persisted(str(p))
    assert loaded == entries

    # max_entries keeps the most recent (tail) entries.
    loaded_tail = obs._load_persisted(str(p), max_entries=2)
    assert loaded_tail == entries[-2:]


def test_load_persisted_skips_corrupt_lines(tmp_path):
    p = tmp_path / "corrupt.jsonl"
    with open(p, "w", encoding="utf-8") as f:
        f.write(json.dumps({"ok": 1}) + "\n")
        f.write("this is not json\n")
        f.write(json.dumps({"ok": 2}) + "\n")
        f.write("\n")  # blank line

    loaded = obs._load_persisted(str(p))
    assert loaded == [{"ok": 1}, {"ok": 2}]


def test_persistence_fail_open(tmp_path):
    # Path inside a directory that does not exist — must NOT raise.
    bad = str(tmp_path / "nonexistent_subdir" / "file.jsonl")

    obs._persist_entry(bad, {"a": 1})
    # Loading a missing path must also fail open and return [].
    assert obs._load_persisted(bad) == []


def test_duplicate_stream_end_prefers_client_disconnect_without_double_counting(capsys):
    common = {
        "provider": "pix4k",
        "model": "claude-opus-4.8",
        "ttft": 6.0,
        "in_tokens": 63,
        "out_tokens": 0,
        "cached_tokens": 0,
        "config": {},
        "total_time": 6.0,
        "request_id": "req_stream_disconnect",
        "client": "openai",
        "stream": True,
    }

    obs.log_request(status=200, **common)
    obs.log_request(status=499, error_msg="client_disconnected", **common)

    visible_ends = [entry for entry in obs.console_logs if entry.get("event") == "end"]
    assert len(visible_ends) == 1
    assert visible_ends[0]["status"] == 499
    assert visible_ends[0]["error"] == "client_disconnected"
    assert len(obs.usage_stats) == 1

    persisted = _read_jsonl(obs._CONSOLE_LOG_PATH)
    assert [entry["event"] for entry in persisted] == ["end", "end_correction"]
    assert persisted[-1]["status"] == 499
    assert "status=499" not in capsys.readouterr().out

