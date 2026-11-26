"""Focused regression tests for LOCK-C96E13836A Wave 0.1 (RC-1, Gap R-2, G0-6).

Covers:
1. _MAX_OUTPUT_TOKENS_CAP raised to 32768 in gemini.py.
2. gemini_frame_has_body_or_tool_content, sawContentFilter tracking, and
   _gemini_frame_commits_transcript ignoring thought-only frames.
3. End-to-end gemini_egress_stream / antigravity_generate streaming continuity:
   a) Thought-only + reasoning_tokens > 0 + empty finishReason="STOP" advances
      combo chain to the next leaf cleanly (no out-of-order finish frame, no
      empty body crash).
   b) Thought-only on single/exhausted leaf flushes _thought_buf first in order
      and emits BSL_NO_OUTPUT_NOTICE with finishReason="STOP" (never empty text).
   c) GEMINI_THOUGHT_BUFFER_CAP_BYTES exceeded with zero body/tool output emits
      BSL_NO_OUTPUT_NOTICE before SSE_DONE (discards empty finish frame).
   d) < 160-char pure prose refusal and finish_reason="content_filter" advance
      combo chain pre-commit without leaking refusal text.
   e) Legitimate < 160-char reply, >= 160-char prose, and functionCall flush
      thoughts + body + finish in strict chronological order without false refusal.
   f) Upstream HTTP 400 content_filter advances combo chain pre-commit, bare
      OpenAI SSE {"error": ...} content_filter classifies as 400, and HTTP 404
      logs [Upstream404] with provider, model, and account ID.
"""
import asyncio
import builtins
import io
import json
import os
import sys

import httpx
import pytest
from starlette.requests import Request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import app.config_state as cs
import app.main as main
from app.compat.adapters.gemini import (
    BSL_NO_OUTPUT_NOTICE,
    _MAX_OUTPUT_TOKENS_CAP,
    gemini_frame_has_body_or_tool_content,
    gemini_request_to_openai,
    openai_chunk_to_gemini,
)


class _BytesStream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self):
        self.closed = True


class _SequenceClient:
    """Deterministic fake httpx client returning queued responses per send()."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []

    def build_request(self, method, url, **kwargs):
        return httpx.Request(method, url, **kwargs)

    async def send(self, request, stream=False):
        self.requests.append(request)
        if not self._responses:
            raise AssertionError(f"Unexpected extra upstream send() to {request.url}")
        nxt = self._responses.pop(0)
        if callable(nxt):
            return nxt(request)
        return nxt


def _sse_chunk(obj) -> bytes:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode("utf-8")


def _stream_resp(sse_objects, status_code=200) -> httpx.Response:
    raw_chunks = []
    for item in sse_objects:
        if isinstance(item, (bytes, bytearray)):
            raw_chunks.append(bytes(item))
        elif item == "[DONE]":
            raw_chunks.append(b"data: [DONE]\n\n")
        else:
            raw_chunks.append(_sse_chunk(item))
    return httpx.Response(
        status_code,
        headers={"content-type": "text/event-stream"},
        stream=_BytesStream(raw_chunks),
    )


def _json_error_resp(status_code: int, payload: dict) -> httpx.Response:
    raw = json.dumps(payload).encode("utf-8")
    return httpx.Response(
        status_code,
        headers={"content-type": "application/json"},
        stream=_BytesStream([raw]),
    )


def _request(path="/v1internal:streamGenerateContent", body=None, headers=None):
    encoded = json.dumps(body or {}).encode("utf-8")
    delivered = False
    header_pairs = [
        (b"content-type", b"application/json"),
        (b"user-agent", b"antigravity/2.26.0"),
    ]
    for name, value in (headers or {}).items():
        header_pairs.append((name.encode("latin-1"), value.encode("latin-1")))

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": encoded, "more_body": False}
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "query_string": b"alt=sse",
            "headers": header_pairs,
            "scheme": "http",
            "server": ("127.0.0.1", 6969),
            "client": ("127.0.0.1", 50000),
        },
        receive,
    )


def _ag_envelope(model="gemini-4-argon-high", prompt="Inspect workspace"):
    return {
        "project": "test-project",
        "requestId": "req-rc1",
        "model": model,
        "userAgent": "antigravity",
        "request": {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "tools": [
                {
                    "functionDeclarations": [
                        {
                            "name": "read_file",
                            "description": "Read a file",
                            "parameters": {
                                "type": "OBJECT",
                                "properties": {"Path": {"type": "STRING"}},
                                "required": ["Path"],
                            },
                        }
                    ]
                }
            ],
        },
    }


def _combo_config(chain, infinite_retry=False, leaf1_conn_extra=None):
    conn1 = {
        "enabled": True,
        "api_key": "k-leaf1",
        "base_url": "https://leaf1.invalid/v1",
    }
    if leaf1_conn_extra:
        conn1.update(leaf1_conn_extra)
    return {
        "server": {"port": 6969},
        "settings": {"combo_infinite_retry": infinite_retry},
        "providers": {
            "leaf1": {
                "format": "openai",
                "connections": [conn1],
                "models": [{"id": "m1", "enabled": True}],
            },
            "leaf2": {
                "format": "openai",
                "connections": [
                    {
                        "enabled": True,
                        "api_key": "k-leaf2",
                        "base_url": "https://leaf2.invalid/v1",
                    }
                ],
                "models": [{"id": "m2", "enabled": True}],
            },
        },
        "combos": [{"alias": "rc1-combo", "chain": list(chain)}],
        "aliases": {},
        "antigravity_integration": {
            "enabled": True,
            "mappings": {
                "gemini-4-argon-high": "rc1-combo" if len(chain) > 1 else chain[0]
            },
        },
    }


def _isolate_router_side_effects(monkeypatch):
    real_open = builtins.open

    def open_without_forensics(path, *args, **kwargs):
        normalized = str(path).replace("\\", "/")
        if normalized.endswith((
            ".brain/logs/antigravity_inbound.jsonl",
            ".brain/logs/outbound_upstream.jsonl",
        )):
            return io.StringIO()
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", open_without_forensics)
    monkeypatch.setattr(main, "bench_leaf", lambda *a, **kw: None)
    monkeypatch.setattr(main, "_dump_upstream_failure", lambda *a, **kw: None)
    monkeypatch.setattr(main.obs, "log_request_start", lambda *a, **kw: "req-test")
    monkeypatch.setattr(main.obs, "log_request", lambda *a, **kw: None)
    _brk_fn = getattr(main, "get_breaker", None)
    _brk = _brk_fn() if callable(_brk_fn) else None
    if _brk is not None:
        monkeypatch.setattr(_brk, "record_outcome", lambda *a, **kw: None)
        if hasattr(_brk, "is_open"):
            monkeypatch.setattr(_brk, "is_open", lambda *a, **kw: (False, None))


async def _collect_ag_stream(req):
    resp = await main.antigravity_generate(req)
    if hasattr(resp, "body_iterator"):
        raw = b"".join([chunk async for chunk in resp.body_iterator])
    else:
        raw = resp.body
    frames = []
    saw_done = raw.endswith(main.GEMINI_SSE_DONE)
    for line in raw.splitlines():
        if line in (b"data: [DONE]", b": [DONE]"):
            saw_done = True
        elif line.startswith(b"data: "):
            frames.append(json.loads(line[6:].decode("utf-8")))
    return resp, raw, frames, saw_done


# ─── 1. Adapter Unit Contracts ────────────────────────────────────────────────


def test_max_output_tokens_cap_raised_to_32768():
    assert _MAX_OUTPUT_TOKENS_CAP == 32768
    clamped = gemini_request_to_openai(
        {
            "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
            "generationConfig": {"maxOutputTokens": 99999},
        },
        "gpt-test",
    )
    assert clamped["max_tokens"] == 32768

    below_cap = gemini_request_to_openai(
        {
            "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
            "generationConfig": {"maxOutputTokens": 24576},
        },
        "gpt-test",
    )
    assert below_cap["max_tokens"] == 24576


def test_gemini_frame_has_body_or_tool_content_and_saw_content_filter():
    assert gemini_frame_has_body_or_tool_content(None) is False
    assert gemini_frame_has_body_or_tool_content({}) is False
    assert gemini_frame_has_body_or_tool_content("bad") is False

    thought_frame = {
        "response": {
            "candidates": [
                {"content": {"role": "model", "parts": [{"thought": True, "text": "reasoning"}]}}
            ]
        }
    }
    empty_finish_frame = {
        "response": {
            "candidates": [
                {"content": {"role": "model", "parts": [{"text": ""}]}, "finishReason": "STOP"}
            ]
        }
    }
    notice_frame = {
        "response": {
            "candidates": [
                {
                    "content": {"role": "model", "parts": [{"text": BSL_NO_OUTPUT_NOTICE}]},
                    "finishReason": "STOP",
                }
            ]
        }
    }
    vis_frame = {
        "response": {
            "candidates": [{"content": {"role": "model", "parts": [{"text": "real output"}]}}]
        }
    }
    fc_frame = {
        "response": {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [{"functionCall": {"name": "read_file", "args": {"Path": "a.py"}}}],
                    }
                }
            ]
        }
    }
    inline_frame = {
        "response": {
            "candidates": [
                {"content": {"role": "model", "parts": [{"inlineData": {"mimeType": "image/png", "data": "AA=="}}]}}
            ]
        }
    }

    assert gemini_frame_has_body_or_tool_content(thought_frame) is False
    assert gemini_frame_has_body_or_tool_content(empty_finish_frame) is False
    assert gemini_frame_has_body_or_tool_content(notice_frame) is False
    assert gemini_frame_has_body_or_tool_content(vis_frame) is True
    assert gemini_frame_has_body_or_tool_content(fc_frame) is True
    assert gemini_frame_has_body_or_tool_content(inline_frame) is True

    # Transcript commit helper must also ignore thought-only frames.
    assert main._gemini_frame_commits_transcript(thought_frame["response"], BSL_NO_OUTPUT_NOTICE) is False
    assert main._gemini_frame_commits_transcript(empty_finish_frame["response"], BSL_NO_OUTPUT_NOTICE) is False
    assert main._gemini_frame_commits_transcript(vis_frame["response"], BSL_NO_OUTPUT_NOTICE) is True
    assert main._gemini_frame_commits_transcript(fc_frame["response"], BSL_NO_OUTPUT_NOTICE) is True

    # sawContentFilter tracking on finish_reason="content_filter"
    state = {}
    openai_chunk_to_gemini(
        {
            "id": "c1",
            "model": "m1",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "content_filter"}],
        },
        state,
    )
    assert state.get("sawContentFilter") is True


def test_bare_openai_sse_content_filter_error_classified_as_recoverable_400():
    bare_sse = {
        "error": {
            "message": "Output blocked by content filtering policy",
            "code": "content_filter",
        }
    }
    status_err = main._explicit_sse_failure_event(bare_sse)
    assert status_err is not None
    status_code, msg = status_err
    assert status_code == 400
    assert "content filtering policy" in msg
    assert main._is_recoverable_content_filter_400(400, msg) is True
    assert main._is_terminal_relay_wall(400, msg) is False
    assert main._is_unrecoverable_payload_400(400, msg) is False


# ─── 2. End-to-End Gemini Egress Stream Continuity (RC-1 + Gap R-2) ──────────


def test_thought_only_with_reasoning_tokens_advances_combo_to_healthy_leaf(monkeypatch):
    """Leaf 1 emits ONLY reasoning_content + usage.completion_tokens=150 + STOP.
    Combo MUST advance to Leaf 2 without leaking Leaf 1 thoughts or empty STOP."""
    _isolate_router_side_effects(monkeypatch)
    cs.replace_config(_combo_config(["leaf1/m1", "leaf2/m2"], infinite_retry=False))

    leaf1_resp = _stream_resp([
        {"id": "l1", "model": "m1", "choices": [{"index": 0, "delta": {"reasoning_content": "Leaf1 stuck thinking..."}}]},
        {
            "id": "l1",
            "model": "m1",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 80,
                "completion_tokens": 150,
                "completion_tokens_details": {"reasoning_tokens": 150},
            },
        },
        "[DONE]",
    ])
    leaf2_resp = _stream_resp([
        {"id": "l2", "model": "m2", "choices": [{"index": 0, "delta": {"reasoning_content": "Leaf2 plan"}}]},
        {"id": "l2", "model": "m2", "choices": [{"index": 0, "delta": {"content": "Completed workspace check cleanly."}}]},
        {"id": "l2", "model": "m2", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        "[DONE]",
    ])
    client = _SequenceClient([leaf1_resp, leaf2_resp])
    monkeypatch.setattr(main, "_get_client_for_proxy", lambda _p=None: client)

    _, raw, frames, saw_done = asyncio.run(_collect_ag_stream(_request(body=_ag_envelope())))

    assert len(client.requests) == 2
    assert saw_done is True
    assert b"Leaf1 stuck thinking" not in raw

    # Chronological order on Leaf 2: thought first -> visible text -> finishReason="STOP" last
    parts_seq = [f["response"]["candidates"][0]["content"]["parts"][0] for f in frames]
    assert parts_seq[0] == {"thought": True, "text": "Leaf2 plan"}
    assert parts_seq[1] == {"text": "Completed workspace check cleanly."}
    assert frames[-1]["response"]["candidates"][0]["finishReason"] == "STOP"


def test_thought_only_on_single_exhausted_leaf_flushes_thoughts_then_notice(monkeypatch):
    """When no fallback leaf remains, thought-only stream flushes _thought_buf in order
    and emits BSL_NO_OUTPUT_NOTICE with finishReason='STOP' (never empty text '')."""
    _isolate_router_side_effects(monkeypatch)
    cs.replace_config(_combo_config(["leaf1/m1"], infinite_retry=False))

    leaf1_resp = _stream_resp([
        {"id": "l1", "model": "m1", "choices": [{"index": 0, "delta": {"reasoning_content": "Step A. "}}]},
        {"id": "l1", "model": "m1", "choices": [{"index": 0, "delta": {"reasoning_content": "Step B."}}]},
        {
            "id": "l1",
            "model": "m1",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 45},
        },
        "[DONE]",
    ])
    client = _SequenceClient([leaf1_resp])
    monkeypatch.setattr(main, "_get_client_for_proxy", lambda _p=None: client)

    _, _, frames, saw_done = asyncio.run(_collect_ag_stream(_request(body=_ag_envelope())))

    assert saw_done is True
    assert len(frames) == 3
    assert frames[0]["response"]["candidates"][0]["content"]["parts"] == [{"thought": True, "text": "Step A. "}]
    assert "finishReason" not in frames[0]["response"]["candidates"][0]
    assert frames[1]["response"]["candidates"][0]["content"]["parts"] == [{"thought": True, "text": "Step B."}]
    assert "finishReason" not in frames[1]["response"]["candidates"][0]

    final_cand = frames[2]["response"]["candidates"][0]
    assert final_cand["finishReason"] == "STOP"
    assert final_cand["content"]["parts"] == [{"text": BSL_NO_OUTPUT_NOTICE}]


def test_thought_buffer_cap_exceeded_with_zero_body_emits_valid_notice(monkeypatch):
    """If GEMINI_THOUGHT_BUFFER_CAP_BYTES overflows mid-stream and upstream still
    finishes with zero visible/tool output, client gets BSL_NO_OUTPUT_NOTICE (never '')."""
    _isolate_router_side_effects(monkeypatch)
    monkeypatch.setattr(main, "GEMINI_THOUGHT_BUFFER_CAP_BYTES", 24)
    cs.replace_config(_combo_config(["leaf1/m1", "leaf2/m2"], infinite_retry=False))

    leaf1_resp = _stream_resp([
        {
            "id": "l1",
            "model": "m1",
            "choices": [{"index": 0, "delta": {"reasoning_content": "Long reasoning chunk exceeding 24 bytes cap"}}],
        },
        {"id": "l1", "model": "m1", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        "[DONE]",
    ])
    client = _SequenceClient([leaf1_resp])
    monkeypatch.setattr(main, "_get_client_for_proxy", lambda _p=None: client)

    _, _, frames, saw_done = asyncio.run(_collect_ag_stream(_request(body=_ag_envelope())))

    assert saw_done is True
    # Must never emit an empty {"text": ""} finish frame
    for f in frames:
        parts = f["response"]["candidates"][0]["content"]["parts"]
        assert parts != [{"text": ""}]
    assert frames[-1]["response"]["candidates"][0]["finishReason"] == "STOP"
    assert frames[-1]["response"]["candidates"][0]["content"]["parts"] == [{"text": BSL_NO_OUTPUT_NOTICE}]


def test_short_prose_refusal_and_content_filter_advance_combo_precommit(monkeypatch):
    """< 160-char pure prose refusal OR finish_reason='content_filter' benches leaf 1
    and advances combo to leaf 2 without leaking refusal frames."""
    _isolate_router_side_effects(monkeypatch)
    cs.replace_config(_combo_config(["leaf1/m1", "leaf2/m2"], infinite_retry=False))

    # Case A: < 160-char pure prose refusal
    refusal_resp = _stream_resp([
        {"id": "l1", "model": "m1", "choices": [{"index": 0, "delta": {"reasoning_content": "Policy check"}}]},
        {"id": "l1", "model": "m1", "choices": [{"index": 0, "delta": {"content": "I'm sorry, but I can't assist with that request."}}]},
        {"id": "l1", "model": "m1", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        "[DONE]",
    ])
    healthy_resp = _stream_resp([
        {"id": "l2", "model": "m2", "choices": [{"index": 0, "delta": {"content": "Diagnostic audit complete: 0 issues."}}]},
        {"id": "l2", "model": "m2", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        "[DONE]",
    ])
    client_a = _SequenceClient([refusal_resp, healthy_resp])
    monkeypatch.setattr(main, "_get_client_for_proxy", lambda _p=None: client_a)

    _, raw_a, frames_a, saw_done_a = asyncio.run(_collect_ag_stream(_request(body=_ag_envelope())))
    assert len(client_a.requests) == 2
    assert saw_done_a is True
    assert b"can't assist" not in raw_a
    assert b"Diagnostic audit complete: 0 issues." in raw_a
    assert frames_a[-1]["response"]["candidates"][0]["finishReason"] == "STOP"

    # Case B: finish_reason="content_filter"
    cf_resp = _stream_resp([
        {"id": "l1", "model": "m1", "choices": [{"index": 0, "delta": {"content": "Partial output"}}]},
        {"id": "l1", "model": "m1", "choices": [{"index": 0, "delta": {}, "finish_reason": "content_filter"}]},
        "[DONE]",
    ])
    healthy_resp_b = _stream_resp([
        {"id": "l2", "model": "m2", "choices": [{"index": 0, "delta": {"content": "Fallback leaf succeeded cleanly."}}]},
        {"id": "l2", "model": "m2", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        "[DONE]",
    ])
    client_b = _SequenceClient([cf_resp, healthy_resp_b])
    monkeypatch.setattr(main, "_get_client_for_proxy", lambda _p=None: client_b)

    _, raw_b, _, saw_done_b = asyncio.run(_collect_ag_stream(_request(body=_ag_envelope())))
    assert len(client_b.requests) == 2
    assert saw_done_b is True
    assert b"Partial output" not in raw_b
    assert b"Fallback leaf succeeded cleanly." in raw_b


def test_legitimate_short_reply_and_function_call_preserve_chronological_order(monkeypatch):
    """Short non-refusal text (< 160 chars) and tool calls must NOT false-trigger
    refusal fallback and must flush buffered thoughts BEFORE body/tool + STOP."""
    _isolate_router_side_effects(monkeypatch)
    cs.replace_config(_combo_config(["leaf1/m1", "leaf2/m2"], infinite_retry=False))

    # Case A: < 160-char normal answer
    short_ok = _stream_resp([
        {"id": "l1", "model": "m1", "choices": [{"index": 0, "delta": {"reasoning_content": "Checking line 42"}}]},
        {"id": "l1", "model": "m1", "choices": [{"index": 0, "delta": {"content": "Done - fixed line 42."}}]},
        {"id": "l1", "model": "m1", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        "[DONE]",
    ])
    client_short = _SequenceClient([short_ok])
    monkeypatch.setattr(main, "_get_client_for_proxy", lambda _p=None: client_short)

    _, _, frames_short, saw_done_short = asyncio.run(_collect_ag_stream(_request(body=_ag_envelope())))
    assert len(client_short.requests) == 1
    assert saw_done_short is True
    assert frames_short[0]["response"]["candidates"][0]["content"]["parts"] == [{"thought": True, "text": "Checking line 42"}]
    assert frames_short[1]["response"]["candidates"][0]["content"]["parts"] == [{"text": "Done - fixed line 42."}]
    assert frames_short[-1]["response"]["candidates"][0]["finishReason"] == "STOP"

    # Case B: functionCall immediately flushes thought buffer without waiting for 160 chars
    tool_ok = _stream_resp([
        {"id": "l1", "model": "m1", "choices": [{"index": 0, "delta": {"reasoning_content": "Need to read config.py"}}]},
        {
            "id": "l1",
            "model": "m1",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "read_file", "arguments": '{"Path":"config.py"}'},
                            }
                        ]
                    },
                }
            ],
        },
        {"id": "l1", "model": "m1", "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        "[DONE]",
    ])
    client_tool = _SequenceClient([tool_ok])
    monkeypatch.setattr(main, "_get_client_for_proxy", lambda _p=None: client_tool)

    _, _, frames_tool, saw_done_tool = asyncio.run(_collect_ag_stream(_request(body=_ag_envelope())))
    assert len(client_tool.requests) == 1
    assert saw_done_tool is True
    assert frames_tool[0]["response"]["candidates"][0]["content"]["parts"] == [
        {"thought": True, "text": "Need to read config.py"}
    ]
    fc_parts = frames_tool[1]["response"]["candidates"][0]["content"]["parts"]
    assert any("functionCall" in p and p["functionCall"]["name"] == "read_file" for p in fc_parts)


def test_upstream_400_content_filter_and_404_account_forensics_advance_combo(monkeypatch, capsys):
    """HTTP 400 content filtering policy advances combo pre-commit, and HTTP 404
    logs [Upstream404] with provider, model, and account identifier."""
    _isolate_router_side_effects(monkeypatch)
    cs.replace_config(
        _combo_config(
            ["leaf1/m1", "leaf2/m2"],
            infinite_retry=False,
            leaf1_conn_extra={"email": "ops-seat-7@blacksand.dev"},
        )
    )

    # 1. HTTP 400 Output blocked by content filtering policy -> advances to leaf2
    cf_400 = _json_error_resp(
        400, {"error": {"message": "Output blocked by content filtering policy", "type": "invalid_request_error"}}
    )
    ok_leaf2 = _stream_resp([
        {"id": "l2", "model": "m2", "choices": [{"index": 0, "delta": {"content": "Recovered from 400 content filter."}}]},
        {"id": "l2", "model": "m2", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        "[DONE]",
    ])
    client_400 = _SequenceClient([cf_400, ok_leaf2])
    monkeypatch.setattr(main, "_get_client_for_proxy", lambda _p=None: client_400)

    _, raw_400, _, saw_done_400 = asyncio.run(_collect_ag_stream(_request(body=_ag_envelope())))
    out_400 = capsys.readouterr().out
    assert len(client_400.requests) == 2
    assert saw_done_400 is True
    assert b"Recovered from 400 content filter." in raw_400
    assert "[ContentFilter400]" in out_400
    assert "ops-seat-7@blacksand.dev" in out_400

    # 2. HTTP 404 -> logs [Upstream404] with provider, model, and account ID + advances combo
    err_404 = _json_error_resp(404, {"error": {"message": "Requested entity was not found"}})
    ok_leaf2_b = _stream_resp([
        {"id": "l2", "model": "m2", "choices": [{"index": 0, "delta": {"content": "Recovered from 404."}}]},
        {"id": "l2", "model": "m2", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        "[DONE]",
    ])
    client_404 = _SequenceClient([err_404, ok_leaf2_b])
    monkeypatch.setattr(main, "_get_client_for_proxy", lambda _p=None: client_404)

    _, raw_404, _, saw_done_404 = asyncio.run(_collect_ag_stream(_request(body=_ag_envelope())))
    out_404 = capsys.readouterr().out
    assert len(client_404.requests) == 2
    assert saw_done_404 is True
    assert b"Recovered from 404." in raw_404
    assert "[Upstream404] provider=leaf1 model=m1 account=conn#0(ops-seat-7@blacksand.dev)" in out_404


def test_substantial_thought_only_triggers_one_shot_same_leaf_continuation_then_combo_failover(monkeypatch):
    """When leaf 1 emits >= 400 chars of valid reasoning_content and cuts off before
    body/tool output:
    1) Single-leaf (or combo leaf 1) performs 1-shot same-leaf continuation carrying
       '[Prior reasoning completed:' and flushes Attempt 1 thoughts + Attempt 2 tool/body.
    2) If the 1-shot same-leaf continuation ALSO emits zero body/tool, it advances
       cleanly to leaf 2 with the clean original request messages."""
    _isolate_router_side_effects(monkeypatch)
    long_thought = "Analyzing architecture and call graph for router continuity. " * 8  # > 480 chars

    # Case A: Single-leaf direct routed model recovers via 1-shot continuation!
    cs.replace_config(_combo_config(["leaf1/m1"], infinite_retry=False))
    attempt1_thought_only = _stream_resp([
        {"id": "l1a", "model": "m1", "choices": [{"index": 0, "delta": {"reasoning_content": long_thought}}]},
        {"id": "l1a", "model": "m1", "choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]},
        "[DONE]",
    ])
    attempt2_tool_call = _stream_resp([
        {
            "id": "l1b",
            "model": "m1",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_cont_1",
                                "type": "function",
                                "function": {"name": "read_file", "arguments": '{"Path":"app/main.py"}'},
                            }
                        ]
                    },
                }
            ],
        },
        {"id": "l1b", "model": "m1", "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        "[DONE]",
    ])
    client_cont = _SequenceClient([attempt1_thought_only, attempt2_tool_call])
    monkeypatch.setattr(main, "_get_client_for_proxy", lambda _p=None: client_cont)

    _, _, frames_cont, saw_done_cont = asyncio.run(_collect_ag_stream(_request(body=_ag_envelope())))
    assert len(client_cont.requests) == 2
    sent_2 = json.loads(client_cont.requests[1].content.decode("utf-8"))
    assert "[Prior reasoning completed:" in sent_2["messages"][-1]["content"]
    assert saw_done_cont is True
    # Attempt 1 buffered thought preserved ahead of Attempt 2 functionCall!
    assert frames_cont[0]["response"]["candidates"][0]["content"]["parts"][0]["thought"] is True
    assert any(
        "functionCall" in p and p["functionCall"]["name"] == "read_file"
        for f in frames_cont
        for p in f["response"]["candidates"][0]["content"]["parts"]
    )

    # Case B: Continuation ALSO returns thought-only -> advances to Leaf 2 with clean messages!
    cs.replace_config(_combo_config(["leaf1/m1", "leaf2/m2"], infinite_retry=False))
    c_attempt1 = _stream_resp([
        {"id": "l1c", "model": "m1", "choices": [{"index": 0, "delta": {"reasoning_content": long_thought}}]},
        {"id": "l1c", "model": "m1", "choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]},
        "[DONE]",
    ])
    c_attempt2_still_empty = _stream_resp([
        {"id": "l1d", "model": "m1", "choices": [{"index": 0, "delta": {"reasoning_content": long_thought}}]},
        {"id": "l1d", "model": "m1", "choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]},
        "[DONE]",
    ])
    c_leaf2_ok = _stream_resp([
        {"id": "l2ok", "model": "m2", "choices": [{"index": 0, "delta": {"content": "Leaf 2 completed after 1-shot continuation."}}]},
        {"id": "l2ok", "model": "m2", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        "[DONE]",
    ])
    client_failover = _SequenceClient([c_attempt1, c_attempt2_still_empty, c_leaf2_ok])
    monkeypatch.setattr(main, "_get_client_for_proxy", lambda _p=None: client_failover)

    _, raw_fo, _, saw_done_fo = asyncio.run(_collect_ag_stream(_request(body=_ag_envelope())))
    assert len(client_failover.requests) == 3
    sent_leaf2 = json.loads(client_failover.requests[2].content.decode("utf-8"))
    # Leaf 2 receives clean original messages (no leaked continuation nudge from Leaf 1)
    assert "[Prior reasoning completed:" not in json.dumps(sent_leaf2["messages"])
    assert saw_done_fo is True
    assert b"Leaf 2 completed after 1-shot continuation." in raw_fo
    assert b"Analyzing architecture and call graph" not in raw_fo
