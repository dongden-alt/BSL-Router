"""Regression tests for Usage tab, console log, zero-token, and upstream/downstream
token & cache accounting fixes across BSL Router.
"""
import asyncio
import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import app.codex_adapter as codex_adapter
import app.main as main
import app.observability as obs
from app.compat.adapters.gemini import _usage_metadata
from app.compat.stream_normalizer import StreamNormalizer
from app.middleware.caching import CacheWarmthTracker
from app.middleware.quality import StreamTruncationDetector
from app.normalizer import UniversalNormalizer


@pytest.fixture
def isolated_usage_store(tmp_path):
    """Isolate SQLite usage store, JSONL paths, and in-memory lists per test."""
    old_db = obs.usage_db_path
    old_usage_jsonl = obs._USAGE_LOG_PATH
    old_console_jsonl = obs._CONSOLE_LOG_PATH
    old_stats = list(obs.usage_stats)
    old_logs = list(obs.console_logs)
    old_reg = dict(obs._terminal_end_registry)

    db_path = str(tmp_path / "usage_stats.sqlite3")
    obs.usage_db_path = db_path
    obs._USAGE_LOG_PATH = str(tmp_path / "usage_stats.jsonl")
    obs._CONSOLE_LOG_PATH = str(tmp_path / "console_logs.jsonl")
    obs.usage_stats.clear()
    obs.console_logs.clear()
    obs._terminal_end_registry.clear()
    obs.init_usage_store()

    try:
        yield db_path, tmp_path
    finally:
        obs.usage_db_path = old_db
        obs._USAGE_LOG_PATH = old_usage_jsonl
        obs._CONSOLE_LOG_PATH = old_console_jsonl
        obs.usage_stats[:] = old_stats
        obs.console_logs[:] = old_logs
        obs._terminal_end_registry.clear()
        obs._terminal_end_registry.update(old_reg)


def test_authenticity_provider_excluded_from_usage_store_and_queries(isolated_usage_store):
    db_path, _ = isolated_usage_store

    obs.log_request(
        provider="authenticity",
        model="claude-opus-5-5",
        status=200,
        ttft=0.05,
        in_tokens=0,
        out_tokens=0,
        cached_tokens=0,
        config={},
        total_time=0.05,
        request_id="req_auth_probe_1",
        client="authenticity:identity_direct",
        stream=False,
    )
    obs.log_request(
        provider="vsllm-o",
        model="claude-opus-4-6",
        status=200,
        ttft=0.5,
        in_tokens=1200,
        out_tokens=300,
        cached_tokens=800,
        config={},
        total_time=1.5,
        request_id="req_real_1",
        client="claude-code",
        stream=True,
    )

    # Authenticity is still visible in console_logs for trace visibility,
    # but NEVER pollutes usage_stats or SQLite usage_events.
    assert any(r.get("provider") == "authenticity" for r in obs.console_logs)
    assert all(r.get("provider") != "authenticity" for r in obs.usage_stats)
    assert len(obs.usage_stats) == 1
    assert obs.usage_stats[0]["provider"] == "vsllm-o"

    # Even if a legacy row with provider='authenticity' exists in SQLite,
    # query_usage_events and query_usage_summary exclude it.
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """INSERT INTO usage_events
               (ts, ts_epoch, provider, model, ttft_ms, total_time_ms,
                in_cached, cache_write_tokens, in_uncached, out, cost, savings, error)
               VALUES ('2026-10-23T00:00:00', 1792713600.0, 'authenticity', 'probe-model',
                       1.0, 1.0, 0, 0, 0, 0, 0.0, 0.0, 'http_502')"""
        )
        conn.commit()

    ev = obs.query_usage_events(limit=100)
    assert ev["total"] == 1
    assert [r["provider"] for r in ev["entries"]] == ["vsllm-o"]

    summary = obs.query_usage_summary()
    assert summary["totals"]["requests"] == 1
    assert [p["provider"] for p in summary["providers"]] == ["vsllm-o"]


def test_duplicate_200_ok_deduplicated_and_upgraded_on_higher_tokens(isolated_usage_store):
    # First 200 OK log (pre-continuation or initial log_request)
    obs.log_request(
        provider="a6api",
        model="claude-opus-4-6-thinking",
        status=200,
        ttft=1.2,
        in_tokens=1000,
        out_tokens=200,
        cached_tokens=600,
        config={},
        total_time=2.5,
        request_id="req_dup_200",
        client="claude-code",
        stream=False,
    )
    # Identical second 200 OK log (e.g. _log_nonstream_success on passthrough)
    obs.log_request(
        provider="a6api",
        model="claude-opus-4-6-thinking",
        status=200,
        ttft=1.2,
        in_tokens=1000,
        out_tokens=200,
        cached_tokens=600,
        config={},
        total_time=2.6,
        request_id="req_dup_200",
        client="claude-code",
        stream=False,
    )

    assert len(obs.usage_stats) == 1
    ev1 = obs.query_usage_events(limit=50)
    assert ev1["total"] == 1
    assert ev1["entries"][0]["out"] == 200

    # Third call with higher token count (e.g. after S3/S6 continuation merge)
    obs.log_request(
        provider="a6api",
        model="claude-opus-4-6-thinking",
        status=200,
        ttft=1.2,
        in_tokens=1500,
        out_tokens=550,
        cached_tokens=900,
        config={},
        total_time=4.0,
        request_id="req_dup_200",
        client="claude-code",
        stream=False,
    )

    assert len(obs.usage_stats) == 1
    assert obs.usage_stats[0]["out"] == 550
    assert obs.usage_stats[0]["in_cached"] == 900
    assert obs.usage_stats[0]["in_uncached"] == 600

    ev2 = obs.query_usage_events(limit=50)
    assert ev2["total"] == 1
    assert ev2["entries"][0]["out"] == 550
    assert ev2["entries"][0]["in_cached"] == 900
    assert ev2["entries"][0]["in_uncached"] == 600


def test_merge_usage_into_stats_preserves_anthropic_message_start_on_message_delta():
    stats = {"in": 0, "out": 0, "cached": 0, "cache_write": 0}
    # Anthropic message_start carries input_tokens + cache_read_input_tokens, output_tokens=0
    main._merge_usage_into_stats(
        stats,
        {
            "input_tokens": 557,
            "cache_read_input_tokens": 37052,
            "cache_creation_input_tokens": 0,
            "output_tokens": 0,
        },
    )
    assert stats["in"] == 37609
    assert stats["cached"] == 37052
    assert stats["out"] == 0

    # Anthropic message_delta carries ONLY output_tokens (no input_tokens or cache_read)
    main._merge_usage_into_stats(stats, {"output_tokens": 58})
    assert stats["in"] == 37609
    assert stats["cached"] == 37052
    assert stats["out"] == 58
    assert stats["stream_completed"] is True


def test_backfill_missing_usage_partial_and_tool_calls():
    messages = [{"role": "user", "content": "Hello world! " * 20}]

    # Case 1: in > 0, out == 0 with delivered text -> backfills out only
    stats1 = {"in": 1200, "out": 0, "cached": 800, "cache_write": 0}
    main._backfill_missing_usage(stats1, messages, "Delivered answer text " * 10)
    assert stats1["in"] == 1200
    assert stats1["out"] > 0
    assert stats1.get("usage_estimated") is True

    # Case 2: out > 0, in == 0 -> backfills in only
    stats2 = {"in": 0, "out": 45, "cached": 0, "cache_write": 0}
    main._backfill_missing_usage(stats2, messages, "Short answer")
    assert stats2["out"] == 45
    assert stats2["in"] > 0
    assert stats2.get("usage_estimated") is True

    # Case 3: tool-call-only stream (delivered_text="", delivered_chars > 0)
    stats3 = {"in": 0, "out": 0, "cached": 0, "cache_write": 0}
    main._backfill_missing_usage(stats3, messages, "", delivered_chars=320)
    assert stats3["out"] == 80
    assert stats3["in"] > 0

    # Case 4: dead leaf (no text, no tool_call_chars, out == 0) -> stays 0
    stats4 = {"in": 0, "out": 0, "cached": 0, "cache_write": 0}
    main._backfill_missing_usage(stats4, messages, "", delivered_chars=0)
    assert stats4["in"] == 0
    assert stats4["out"] == 0
    assert not stats4.get("usage_estimated")


def test_stream_truncation_detector_tracks_tool_call_chars_and_bare_reasoning():
    det = StreamTruncationDetector(enabled=False, accumulate=True)
    chunk_oai = (
        b'data: {"choices":[{"delta":{"reasoning":"step 1 ","tool_calls":[{"index":0,"function":{"name":"read_file","arguments":"{\\"path\\":\\"a.py\\"}"}}]}}]}\n\n'
    )
    det.feed(chunk_oai)
    assert det.partial_text == "step 1 "
    assert det.tool_call_chars == len("read_file") + len('{"path":"a.py"}')

    det_anth = StreamTruncationDetector(enabled=False, accumulate=True)
    det_anth.feed(
        b'data: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"toolu_1","name":"bash"}}\n\n'
        b'data: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{\\"cmd\\":\\"ls\\"}"}}\n\n'
    )
    assert det_anth.tool_call_chars == len("bash") + len('{"cmd":"ls"}')


def test_extract_usage_tokens_all_dialects_without_double_counting():
    # 1. Standard OpenAI (prompt_tokens is inclusive of cached_tokens)
    u_oai = {
        "prompt_tokens": 10000,
        "completion_tokens": 500,
        "total_tokens": 10500,
        "prompt_tokens_details": {"cached_tokens": 8000, "cache_write_tokens": 500},
    }
    assert main._extract_usage_tokens(u_oai) == (10000, 500, 8000)
    assert main._extract_cache_write_tokens(u_oai) == 500

    # 2. Standard Anthropic (input_tokens is exclusive of cache_read + cache_creation)
    u_anth = {
        "input_tokens": 557,
        "cache_read_input_tokens": 37052,
        "cache_creation_input_tokens": 1200,
        "output_tokens": 58,
    }
    assert main._extract_usage_tokens(u_anth) == (557 + 37052 + 1200, 58, 37052)
    assert main._extract_cache_write_tokens(u_anth) == 1200

    # 3. Hybrid relay where input_tokens is ALREADY inclusive (proven by total_tokens)
    u_hybrid = {
        "input_tokens": 69372,
        "cache_read_input_tokens": 68352,
        "output_tokens": 1895,
        "total_tokens": 71267,
    }
    assert main._extract_usage_tokens(u_hybrid) == (69372, 1895, 68352)

    # 4. OpenAI Responses API / Codex (input_tokens + input_tokens_details.cached_tokens)
    u_codex = {
        "input_tokens": 5000,
        "output_tokens": 200,
        "input_tokens_details": {"cached_tokens": 4000},
    }
    assert main._extract_usage_tokens(u_codex) == (5000, 200, 4000)

    # 5. DeepSeek prompt_cache_hit_tokens + prompt_cache_miss_tokens (without prompt_tokens)
    u_ds = {
        "prompt_cache_hit_tokens": 6000,
        "prompt_cache_miss_tokens": 1500,
        "completion_tokens": 300,
    }
    assert main._extract_usage_tokens(u_ds) == (7500, 300, 6000)

    # 6. Gemini usageMetadata shape
    u_gem = {
        "promptTokenCount": 9000,
        "candidatesTokenCount": 400,
        "cachedContentTokenCount": 7000,
    }
    assert main._extract_usage_tokens(u_gem) == (9000, 400, 7000)

    # CacheWarmthTracker matches inclusive total across dialects
    tracker = CacheWarmthTracker()
    assert tracker.update("codex", 0, u_codex) == pytest.approx(4000 / 5000)
    assert tracker.update("hybrid", 0, u_hybrid) == pytest.approx(68352 / 69372)


def test_downstream_protocol_translators_preserve_cache_and_invariants():
    # 1. Codex -> OpenAI JSON
    codex_resp = {
        "id": "resp_1",
        "status": "completed",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
        "usage": {
            "input_tokens": 5000,
            "output_tokens": 100,
            "input_tokens_details": {"cached_tokens": 4200},
        },
    }
    oai_from_codex = codex_adapter.responses_json_to_openai(codex_resp, model="gpt-5.4")
    assert oai_from_codex["usage"]["prompt_tokens"] == 5000
    assert oai_from_codex["usage"]["prompt_tokens_details"]["cached_tokens"] == 4200

    # 2. Anthropic JSON -> OpenAI JSON -> Anthropic JSON round-trip
    anth_resp = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-4-6",
        "content": [{"type": "text", "text": "hello"}],
        "stop_reason": "end_turn",
        "usage": {
            "input_tokens": 500,
            "cache_read_input_tokens": 3000,
            "cache_creation_input_tokens": 200,
            "output_tokens": 80,
        },
    }
    oai_resp = UniversalNormalizer.anthropic_response_to_openai(anth_resp)
    assert oai_resp["usage"]["prompt_tokens"] == 3700
    assert oai_resp["usage"]["completion_tokens"] == 80
    assert oai_resp["usage"]["prompt_tokens_details"] == {
        "cached_tokens": 3000,
        "cache_write_tokens": 200,
    }

    anth_roundtrip = UniversalNormalizer.openai_response_to_anthropic(oai_resp)
    assert anth_roundtrip["usage"] == {
        "input_tokens": 500,
        "output_tokens": 80,
        "cache_read_input_tokens": 3000,
        "cache_creation_input_tokens": 200,
    }

    # 3. Gemini _usage_metadata from Anthropic & OpenAI usage dicts
    gem_meta_anth = _usage_metadata(anth_resp["usage"])
    assert gem_meta_anth["promptTokenCount"] == 3700
    assert gem_meta_anth["candidatesTokenCount"] == 80
    assert gem_meta_anth["cachedContentTokenCount"] == 3000

    # 4. StreamNormalizer: Anthropic SSE -> OpenAI SSE -> Anthropic SSE
    async def _run_streams():
        anth_sse = [
            b'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_s","role":"assistant","usage":{"input_tokens":400,"cache_read_input_tokens":2600,"cache_creation_input_tokens":100,"output_tokens":0}}}\n\n',
            b'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n',
            b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hi"}}\n\n',
            b'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n',
            b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":25}}\n\n',
            b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
        ]

        async def _aiter(items):
            for it in items:
                yield it

        norm_a2o = StreamNormalizer("anthropic_sse", "openai_sse", model_name="claude-test")
        oai_chunks = [c async for c in norm_a2o.convert_anthropic_to_openai(_aiter(anth_sse))]
        oai_text = b"".join(oai_chunks).decode("utf-8")
        usage_frames = []
        for line in oai_text.splitlines():
            if line.startswith("data: ") and line[6:].strip() != "[DONE]":
                obj = json.loads(line[6:])
                if obj.get("usage"):
                    usage_frames.append(obj["usage"])
        assert len(usage_frames) == 1
        assert usage_frames[0]["prompt_tokens"] == 3100
        assert usage_frames[0]["completion_tokens"] == 25
        assert usage_frames[0]["prompt_tokens_details"] == {
            "cached_tokens": 2600,
            "cache_write_tokens": 100,
        }

        norm_o2a = StreamNormalizer("openai_sse", "anthropic_sse", model_name="claude-test")
        anth_chunks = [c async for c in norm_o2a.convert_openai_to_anthropic(_aiter(oai_chunks))]
        anth_text = b"".join(anth_chunks).decode("utf-8")
        delta_usages = []
        for line in anth_text.splitlines():
            if line.startswith("data: "):
                obj = json.loads(line[6:])
                if obj.get("type") == "message_delta":
                    delta_usages.append(obj.get("usage"))
        assert len(delta_usages) == 1
        assert delta_usages[0] == {
            "input_tokens": 400,
            "output_tokens": 25,
            "cache_read_input_tokens": 2600,
            "cache_creation_input_tokens": 100,
        }

    asyncio.run(_run_streams())


def test_post_completion_disconnect_does_not_mark_499_and_error_preserves_in_tokens(isolated_usage_store):
    # 1. Stream completion detection prevents false 499 on teardown
    stats = {"status": 200, "in": 500, "out": 0, "cached": 0, "error": None}
    assert main._should_mark_client_disconnected(stats) is True

    main._mark_stream_completed_if_terminal(stats, {"choices": [{"delta": {}, "finish_reason": "stop"}]})
    assert stats.get("stream_completed") is True
    assert main._should_mark_client_disconnected(stats) is False

    # 2. Late 499 after already-logged 200 OK with out_tokens > 0 does NOT overwrite 200 OK
    obs.log_request(
        provider="vsllm-o",
        model="kimi-k2.5",
        status=200,
        ttft=0.4,
        in_tokens=2000,
        out_tokens=150,
        cached_tokens=1000,
        config={},
        total_time=1.8,
        request_id="req_completed_stream",
        client="probe",
        stream=True,
    )
    obs.log_request(
        provider="vsllm-o",
        model="kimi-k2.5",
        status=499,
        ttft=0.4,
        in_tokens=2000,
        out_tokens=150,
        cached_tokens=1000,
        config={},
        error_msg="client_disconnected",
        total_time=1.81,
        request_id="req_completed_stream",
        client="probe",
        stream=True,
    )
    matching_logs = [r for r in obs.console_logs if r.get("request_id") == "req_completed_stream"]
    assert len(matching_logs) == 1
    assert matching_logs[0]["status"] == 200
    assert matching_logs[0].get("error") is None

    # 3. Genuine mid-stream error preserves in_cached, cache_write_tokens, and in_uncached
    obs.log_request(
        provider="a6api",
        model="claude-opus-4-6-thinking",
        status=499,
        ttft=1.1,
        in_tokens=37609,
        out_tokens=0,
        cached_tokens=37052,
        cache_write_tokens=100,
        config={},
        error_msg="client_disconnected",
        total_time=5.0,
        request_id="req_genuine_abort",
        client="claude-code",
        stream=True,
    )
    err_rows = [r for r in obs.usage_stats if r.get("error") == "client_disconnected"]
    assert len(err_rows) == 1
    assert err_rows[0]["in_cached"] == 37052
    assert err_rows[0]["cache_write_tokens"] == 100
    assert err_rows[0]["in_uncached"] == 457
