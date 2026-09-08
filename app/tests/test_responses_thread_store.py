"""N4 — Responses API thread store tests (previous_response_id emulation).

Covers:
  1. Gate: tools.responses_thread_store default OFF / malformed config → False.
  2. Store: LRU cap eviction, TTL lazy expiry, sweep-on-write, loop-safe lock,
     put/get/lookup/collect_chain (chain walk, cycle guard, broken links).
  3. Stitch-in: list input, string input, unknown id (unchanged), non-dict body,
     no mutation of the original body, chain ordering (oldest first).
  4. Response-object builders: message extraction, tool_calls → function_call,
     nameless tool-call skip, usage mapping, non-dict payloads.
  5. capture_response: non-streaming JSON 200 → Responses object + stored;
     error status / non-JSON / non-chat payload → passthrough; streaming →
     verbatim byte passthrough + tee-accumulate + store under the client-
     visible chatcmpl id; GeneratorExit → nothing stored.
  6. Endpoint integration (TestClient): OFF default unchanged shape; ON →
     resp_ id + object:"response" + threading across turns.
  7. Wiring: monkeypatched converter/process asserts stitch happens BEFORE
     the OpenAI conversion and capture wraps the exact returned response.

asyncio.run(scenario()) style — no pytest-asyncio plugin in this venv
(matches app/tests/test_capture_log_rotation.py, test_normalizer_shadow.py).
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import app.main as main  # noqa: E402
import app.middleware.responses_thread_store as rts  # noqa: E402


# ── Isolation: fresh store per test (no cross-test LRU/TTL leakage) ──────────

@pytest.fixture(autouse=True)
def _fresh_store():
    rts.reset_store()
    yield
    rts.reset_store()


# ── 1. Gate ──────────────────────────────────────────────────────────────────

def test_gate_default_off():
    assert rts.thread_store_enabled({}) is False
    assert rts.thread_store_enabled(None) is False
    assert rts.thread_store_enabled({"tools": {}}) is False
    assert rts.thread_store_enabled({"tools": {"responses_thread_store": {}}}) is False


def test_gate_enabled_explicitly():
    assert rts.thread_store_enabled(
        {"tools": {"responses_thread_store": {"enabled": True}}}) is True


def test_gate_malformed_config_never_raises():
    for bad in ([], "x", 5, {"tools": None}, {"tools": 3},
                {"tools": {"responses_thread_store": 7}}):
        assert rts.thread_store_enabled(bad) is False


# ── 2. Store mechanics ───────────────────────────────────────────────────────

def test_put_get_roundtrip_without_running_loop():
    st = rts.ResponsesThreadStore()
    items = [{"type": "message", "role": "assistant",
              "content": [{"type": "output_text", "text": "hi"}]}]
    st._put_sync("resp_1", items, None)
    got = st._get_sync("resp_1")
    assert got is not None and got[0] == items and got[1] is None
    assert len(st) == 1


def test_get_returns_a_copy_not_the_stored_list():
    st = rts.ResponsesThreadStore()
    items = [{"a": 1}]
    st._put_sync("resp_1", items, None)
    got = st._get_sync("resp_1")[0]
    got.append({"b": 2})
    assert st._get_sync("resp_1")[0] == [{"a": 1}]


def test_rejects_empty_or_invalid_id_and_items():
    st = rts.ResponsesThreadStore()
    st._put_sync("", [{"a": 1}], None)
    st._put_sync(None, [{"a": 1}], None)
    st._put_sync("resp_x", [], None)
    st._put_sync("resp_x", "not-a-list", None)
    assert len(st) == 0


def test_lru_cap_evicts_oldest():
    st = rts.ResponsesThreadStore(max_threads=3)
    for i in range(5):
        st._put_sync(f"resp_{i}", [{"i": i}], None)
    assert len(st) == 3
    assert st._get_sync("resp_0") is None
    assert st._get_sync("resp_1") is None
    assert st._get_sync("resp_2")[0] == [{"i": 2}]
    assert st._get_sync("resp_4")[0] == [{"i": 4}]


def test_lru_touch_protects_recently_used():
    st = rts.ResponsesThreadStore(max_threads=3)
    for i in range(3):
        st._put_sync(f"resp_{i}", [{"i": i}], None)
    assert st._get_sync("resp_0") is not None  # touch MRU
    st._put_sync("resp_3", [{"i": 3}], None)   # evicts resp_1 (LRU), not resp_0
    assert st._get_sync("resp_0") is not None
    assert st._get_sync("resp_1") is None


def test_ttl_lazy_expiry_on_access():
    st = rts.ResponsesThreadStore(ttl_seconds=10.0)
    st._put_sync("resp_1", [{"a": 1}], None)
    # age the entry beyond TTL
    ts, items, prev = st._entries["resp_1"]
    st._entries["resp_1"] = (ts - 11.0, items, prev)
    assert st._get_sync("resp_1") is None
    assert "resp_1" not in st._entries  # lazily removed


def test_sweep_on_write_purges_expired():
    st = rts.ResponsesThreadStore(ttl_seconds=10.0)
    st._put_sync("resp_old", [{"a": 1}], None)
    ts, items, prev = st._entries["resp_old"]
    st._entries["resp_old"] = (ts - 100.0, items, prev)
    st._last_sweep -= rts.SWEEP_INTERVAL_SECONDS + 1.0  # force sweep window open
    st._put_sync("resp_new", [{"b": 2}], None)
    assert "resp_old" not in st._entries
    assert st._get_sync("resp_new") is not None


def test_lock_rebinds_across_event_loops():
    st = rts.ResponsesThreadStore()
    asyncio.run(st.put("resp_a", [{"a": 1}]))
    asyncio.run(st.get("resp_a"))  # different loop → lock must be rebuilt
    asyncio.run(st.put("resp_b", [{"b": 1}]))
    assert st._get_sync("resp_a") is not None
    assert st._get_sync("resp_b") is not None


def test_collect_chain_walks_oldest_first():
    st = rts.ResponsesThreadStore()
    st._put_sync("resp_a", [{"turn": "a"}], None)
    st._put_sync("resp_b", [{"turn": "b"}], "resp_a")
    st._put_sync("resp_c", [{"turn": "c"}], "resp_b")
    items = asyncio.run(st.collect_chain("resp_c"))
    assert items == [{"turn": "a"}, {"turn": "b"}, {"turn": "c"}]


def test_collect_chain_broken_link_yields_suffix():
    st = rts.ResponsesThreadStore()
    st._put_sync("resp_a", [{"turn": "a"}], None)
    st._put_sync("resp_c", [{"turn": "c"}], "resp_missing")  # parent evicted
    items = asyncio.run(st.collect_chain("resp_c"))
    assert items == [{"turn": "c"}]


def test_collect_chain_cycle_guard():
    """A cycle (client-referenced loop) must terminate with every reachable
    turn exactly once — never an infinite loop, never duplicates."""
    st = rts.ResponsesThreadStore()
    st._put_sync("resp_a", [{"turn": "a"}], "resp_c")
    st._put_sync("resp_b", [{"turn": "b"}], "resp_a")
    st._put_sync("resp_c", [{"turn": "c"}], "resp_b")
    items = asyncio.run(st.collect_chain("resp_c"))  # would loop forever without the guard
    assert items == [{"turn": "a"}, {"turn": "b"}, {"turn": "c"}]


def test_collect_chain_depth_cap():
    st = rts.ResponsesThreadStore()
    prev = None
    for i in range(rts.MAX_CHAIN_DEPTH + 10):
        st._put_sync(f"resp_{i}", [{"i": i}], prev)
        prev = f"resp_{i}"
    items = asyncio.run(st.collect_chain(prev))
    assert len(items) == rts.MAX_CHAIN_DEPTH


def test_unknown_id_collects_empty():
    assert asyncio.run(rts.get_store().collect_chain("resp_never")) == []


# ── 3. Stitch-in ─────────────────────────────────────────────────────────────

def _enable_cfg():
    return {"tools": {"responses_thread_store": {"enabled": True}}}


def test_stitch_disabled_gate_returns_body_unchanged(monkeypatch):
    monkeypatch.setattr(rts, "thread_store_enabled", lambda cfg: False)
    body = {"model": "m", "input": [{"type": "message", "role": "user", "content": "q"}],
            "previous_response_id": "resp_1"}
    assert rts.thread_store_enabled({}) is False  # gate helper itself
    # stitch_previous_response itself is gate-agnostic (endpoint gates first);
    # assert it still fails open on an unknown id.
    assert asyncio.run(rts.stitch_previous_response(body)) is body


def test_stitch_unknown_id_returns_original_body():
    body = {"model": "m", "input": "hello", "previous_response_id": "resp_ghost"}
    assert asyncio.run(rts.stitch_previous_response(body)) is body


def test_stitch_prepends_items_for_string_input():
    st = rts.reset_store()
    st._put_sync("resp_1", [{"type": "message", "role": "assistant",
                             "content": [{"type": "output_text", "text": "hi"}]}], None)
    body = {"model": "m", "input": "follow-up", "previous_response_id": "resp_1"}
    out = asyncio.run(rts.stitch_previous_response(body, store=st))
    assert out is not body
    assert out["input"][0]["type"] == "message"
    assert out["input"][-1] == {"type": "message", "role": "user", "content": "follow-up"}
    assert body["input"] == "follow-up"  # original untouched


def test_stitch_prepends_items_for_list_input():
    st = rts.reset_store()
    st._put_sync("resp_1", [{"type": "function_call", "call_id": "c1",
                             "name": "f", "arguments": "{}"}], None)
    body = {"model": "m",
            "input": [{"type": "message", "role": "user", "content": "go"}],
            "previous_response_id": "resp_1"}
    out = asyncio.run(rts.stitch_previous_response(body, store=st))
    assert out["input"][0]["type"] == "function_call"
    assert out["input"][1]["content"] == "go"
    assert body["input"] == [{"type": "message", "role": "user", "content": "go"}]


def test_stitch_full_chain_ordering():
    st = rts.reset_store()
    st._put_sync("resp_a", [{"turn": "a"}], None)
    st._put_sync("resp_b", [{"turn": "b"}], "resp_a")
    body = {"model": "m", "input": [{"turn": "now"}], "previous_response_id": "resp_b"}
    out = asyncio.run(rts.stitch_previous_response(body, store=st))
    assert [it["turn"] for it in out["input"]] == ["a", "b", "now"]


def test_stitch_no_previous_id_is_noop():
    body = {"model": "m", "input": "hello"}
    assert asyncio.run(rts.stitch_previous_response(body)) is body


def test_stitch_non_dict_body_passthrough():
    assert asyncio.run(rts.stitch_previous_response("str")) == "str"
    assert asyncio.run(rts.stitch_previous_response(None)) is None
    assert asyncio.run(rts.stitch_previous_response([1, 2])) == [1, 2]


def test_stitch_whitespace_id_treated_as_absent():
    body = {"model": "m", "input": "x", "previous_response_id": "   "}
    assert asyncio.run(rts.stitch_previous_response(body)) is body


# ── 4. Response-object builders ──────────────────────────────────────────────

def _chat_payload(text="hi", tool_calls=None, usage=None, model="gpt-x"):
    msg: dict = {"role": "assistant", "content": text}
    if tool_calls is not None:
        msg["tool_calls"] = tool_calls
    payload = {"id": "chatcmpl-1", "model": model,
               "choices": [{"index": 0, "message": msg, "finish_reason": "stop"}]}
    if usage is not None:
        payload["usage"] = usage
    return payload


def test_output_items_from_chat_message_text():
    items = rts._output_items_from_chat(_chat_payload("hello world"))
    assert items == [{"type": "message", "id": items[0]["id"], "role": "assistant",
                      "content": [{"type": "output_text", "text": "hello world"}]}]
    assert items[0]["id"].startswith("msg_")


def test_output_items_from_chat_content_parts():
    payload = _chat_payload(None)
    payload["choices"][0]["message"]["content"] = [
        {"type": "text", "text": "a"}, {"type": "output_text", "text": "b"},
        {"type": "other", "x": 1},
    ]
    items = rts._output_items_from_chat(payload)
    assert items[0]["content"] == [{"type": "output_text", "text": "ab"}]


def test_output_items_tool_calls_and_nameless_skip():
    payload = _chat_payload(None, tool_calls=[
        {"id": "call_1", "type": "function",
         "function": {"name": "lookup", "arguments": "{\"q\":1}"}},
        {"id": "call_2", "type": "function", "function": {"name": "", "arguments": "{}"}},
    ])
    items = rts._output_items_from_chat(payload)
    assert len(items) == 1
    assert items[0] == {"type": "function_call", "id": items[0]["id"],
                        "call_id": "call_1", "name": "lookup",
                        "arguments": "{\"q\":1}"}


def test_output_items_dict_arguments_serialized():
    payload = _chat_payload(None, tool_calls=[
        {"id": "call_1", "function": {"name": "f", "arguments": {"q": 1}}}])
    items = rts._output_items_from_chat(payload)
    assert json.loads(items[0]["arguments"]) == {"q": 1}
    payload["choices"][0]["message"]["tool_calls"] = [
        {"id": "call_9", "function": {"name": "f", "arguments": None}}]
    items = rts._output_items_from_chat(payload)
    assert items[0]["arguments"] == "{}"


def test_output_items_empty_payload_is_safe():
    assert rts._output_items_from_chat(None) == []
    assert rts._output_items_from_chat({}) == []
    assert rts._output_items_from_chat({"choices": []}) == []
    assert rts._output_items_from_chat({"choices": [{"message": {"content": None}}]}) == []


def test_build_response_object_shape():
    obj = rts.build_response_object(
        "resp_k", _chat_payload("hi", usage={"prompt_tokens": 3, "completion_tokens": 2,
                                             "total_tokens": 5}),
        previous_response_id="resp_prev")
    assert obj["id"] == "resp_k"
    assert obj["object"] == "response"
    assert obj["status"] == "completed"
    assert obj["model"] == "gpt-x"
    assert obj["previous_response_id"] == "resp_prev"
    assert obj["output"][0]["content"] == [{"type": "output_text", "text": "hi"}]
    assert obj["usage"]["input_tokens"] == 3
    assert obj["usage"]["output_tokens"] == 2
    assert obj["usage"]["total_tokens"] == 5
    assert obj["usage"]["input_tokens_details"] == {"cached_tokens": None}


def test_build_response_object_non_dict_payload():
    obj = rts.build_response_object("resp_k", "junk")
    assert obj["output"] == [] and obj["model"] == ""
    obj2 = rts.build_response_object("resp_k", None)
    assert obj2["output"] == []


# ── 5. capture_response ──────────────────────────────────────────────────────

def _json_response(payload, status=200):
    from starlette.responses import JSONResponse
    return JSONResponse(payload, status_code=status)


def test_capture_non_streaming_stores_and_rewrites():
    st = rts.reset_store()
    resp = _json_response(_chat_payload("hello"))
    out = asyncio.run(rts.capture_response(resp, store=st))
    assert out is not resp
    obj = json.loads(out.body)
    assert obj["id"].startswith("resp_")
    assert obj["object"] == "response"
    assert obj["output"][0]["content"] == [{"type": "output_text", "text": "hello"}]
    stored = asyncio.run(st.get(obj["id"]))
    assert stored == obj["output"]


def test_capture_records_previous_response_id():
    st = rts.reset_store()
    resp = _json_response(_chat_payload("x"))
    obj = json.loads(asyncio.run(rts.capture_response(resp, previous_response_id="resp_p",
                                                      store=st)).body)
    assert obj["previous_response_id"] == "resp_p"
    _items, prev = st._get_sync(obj["id"])
    assert prev == "resp_p"


def test_capture_passthrough_non_2xx():
    st = rts.reset_store()
    resp = _json_response({"error": {"message": "boom"}}, status=502)
    assert asyncio.run(rts.capture_response(resp, store=st)) is resp
    assert len(st) == 0


def test_capture_passthrough_non_json():
    st = rts.reset_store()
    resp = rts.Response(content=b"plain", status_code=200, media_type="text/plain")
    assert asyncio.run(rts.capture_response(resp, store=st)) is resp
    assert len(st) == 0


def test_capture_passthrough_non_chat_payload():
    st = rts.reset_store()
    resp = _json_response({"not": "a chat completion"})
    assert asyncio.run(rts.capture_response(resp, store=st)) is resp
    assert len(st) == 0


def test_capture_none_response():
    assert asyncio.run(rts.capture_response(None)) is None


def test_capture_empty_output_not_stored():
    st = rts.reset_store()
    resp = _json_response(_chat_payload(""))  # empty text, no tool calls
    obj = json.loads(asyncio.run(rts.capture_response(resp, store=st)).body)
    assert obj["output"] == []
    assert len(st) == 0  # nothing worth threading


def _stream_response(chunks):
    from starlette.responses import StreamingResponse

    async def gen():
        for c in chunks:
            yield c

    return StreamingResponse(gen(), media_type="text/event-stream")


def test_capture_streaming_passthrough_bytes_and_stores():
    st = rts.reset_store()
    sse = (
        b'data: {"id":"chatcmpl-abc","model":"gpt-x","choices":[{"index":0,'
        b'"delta":{"content":"He"}}]}\n\n'
        b'data: {"id":"chatcmpl-abc","model":"gpt-x","choices":[{"index":0,'
        b'"delta":{"content":"llo"}}]}\n\n'
        b'data: {"id":"chatcmpl-abc","choices":[{"index":0,"delta":{},'
        b'"finish_reason":"stop"}]}\n\n'
        b"data: [DONE]\n\n"
    )
    resp = _stream_response([sse[:40], sse[40:110], sse[110:]])
    out = asyncio.run(rts.capture_response(resp, store=st))
    assert isinstance(out, rts.StreamingResponse)
    body = b"".join(asyncio.run(_collect(out)))
    assert body == sse  # byte-for-byte passthrough
    stored_key, items = None, None
    # the store key is the client-visible chat completion id
    found = st._get_sync("chatcmpl-abc")
    assert found is not None
    stored_key = "chatcmpl-abc"
    items = found[0]
    assert items[0]["content"] == [{"type": "output_text", "text": "Hello"}]
    assert stored_key == "chatcmpl-abc"


async def _collect(stream_resp):
    chunks = []
    async for chunk in stream_resp.body_iterator:
        chunks.append(chunk if isinstance(chunk, bytes) else str(chunk).encode())
    return chunks


def test_capture_streaming_partial_lines_buffered():
    st = rts.reset_store()
    line1 = b'data: {"id":"chatcmpl-p","choices":[{"index":0,"delta":{"content":"A"}}]}\n\n'
    line2 = b'data: {"id":"chatcmpl-p","choices":[{"index":0,"delta":{"content":"B"}}]}\n\n'
    resp = _stream_response([line1[:20], line1[20:] + line2[:25], line2[25:]])
    out = asyncio.run(rts.capture_response(resp, store=st))
    asyncio.run(_collect(out))
    found = st._get_sync("chatcmpl-p")
    assert found is not None
    assert found[0][0]["content"] == [{"type": "output_text", "text": "AB"}]


def test_capture_streaming_tool_call_deltas():
    st = rts.reset_store()
    # Arguments arrive as a two-delta split: '{"q":' + '1}' → {"q": 1}
    chunks = [
        b'data: {"id":"chatcmpl-t","choices":[{"index":0,"delta":{"tool_calls":'
        b'[{"index":0,"id":"call_1","function":{"name":"look","arguments":"{\\"q\\":"}}]}}]}\n\n',
        b'data: {"id":"chatcmpl-t","choices":[{"index":0,"delta":{"tool_calls":'
        b'[{"index":0,"function":{"arguments":"1}"}}]}}]}\n\n',
        b'data: [DONE]\n\n',
    ]
    resp = _stream_response(chunks)
    out = asyncio.run(rts.capture_response(resp, store=st))
    asyncio.run(_collect(out))  # the wrapped tee must drive, not the raw iterator
    found = st._get_sync("chatcmpl-t")
    assert found is not None
    fc = found[0][0]
    assert fc["type"] == "function_call" and fc["name"] == "look"
    assert fc["call_id"] == "call_1"
    assert json.loads(fc["arguments"]) == {"q": 1}


def test_capture_streaming_generator_exit_stores_nothing():
    st = rts.reset_store()

    async def broken():
        yield b'data: {"id":"chatcmpl-z","choices":[{"index":0,"delta":{"content":"x"}}]}\n\n'
        raise GeneratorExit

    resp = _stream_response([])  # shell only; swap the iterator
    object.__setattr__(resp, "body_iterator", broken())
    out = asyncio.run(rts.capture_response(resp, store=st))
    with pytest.raises(GeneratorExit):
        asyncio.run(_collect(out))
    assert len(st) == 0


def test_capture_streaming_malformed_sse_never_raises():
    st = rts.reset_store()
    chunks = [
        b"data: not-json\n\n",
        b"\x80\x81\xff garbage\n\n",
        b'data: {"choices": "not-a-list"}\n\n',
        b"data: [DONE]\n\n",
    ]
    resp = _stream_response(chunks)
    out = asyncio.run(rts.capture_response(resp, store=st))
    body = b"".join(asyncio.run(_collect(out)))
    assert b"garbage" in body  # passthrough intact
    assert len(st) == 0


def test_accumulator_direct_feed_string_chunks():
    acc = rts._StreamAccumulator()
    acc.feed('data: {"id":"chatcmpl-s","choices":[{"index":0,"delta":{"content":"z"}}]}\n\n')
    key, items = acc.finish()
    assert key == "chatcmpl-s"
    assert items[0]["content"] == [{"type": "output_text", "text": "z"}]


def test_accumulator_ignore_non_data_lines_and_done():
    acc = rts._StreamAccumulator()
    acc.feed(b": keepalive\n\n")
    acc.feed(b"event: ping\n\n")
    acc.feed(b"data: [DONE]\n\n")
    key, items = acc.finish()
    assert items == []
    assert key.startswith("resp_")  # fallback id


# ── 6. Endpoint integration (TestClient) ─────────────────────────────────────

def _patch_upstream(monkeypatch, text="hello from upstream"):
    """Replace _process_chat_completion with a canned chat completion."""
    captured = {}

    async def fake_process(chat_body, request=None, **_kw):
        captured["body"] = chat_body
        from starlette.responses import JSONResponse
        return JSONResponse({
            "id": "chatcmpl-fixed", "model": chat_body.get("model", "m"),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })

    monkeypatch.setattr(main, "_process_chat_completion", fake_process)
    return captured


def test_endpoint_default_off_passthrough_chat_shape(monkeypatch):
    captured = _patch_upstream(monkeypatch)
    client = TestClient(main.app)
    resp = client.post("/v1/responses", json={
        "model": "m", "input": "hi",
        "previous_response_id": "resp_ghost"})
    assert resp.status_code == 200
    obj = resp.json()
    assert obj["id"] == "chatcmpl-fixed"  # NOT rewritten — store is OFF
    assert obj.get("object") != "response"
    assert captured["body"]["messages"][0]["role"] == "user"
    assert captured["body"]["messages"][0]["content"] == "hi"
    assert len(rts.get_store()) == 0  # zero storage while OFF


def test_endpoint_default_off_zero_module_calls(monkeypatch):
    """OFF → literally zero calls into the thread-store module."""
    calls = []
    monkeypatch.setattr(rts, "stitch_previous_response",
                        lambda *a, **k: calls.append("stitch"))
    monkeypatch.setattr(rts, "capture_response",
                        lambda *a, **k: calls.append("capture"))
    client = TestClient(main.app)
    client.post("/v1/responses", json={"model": "m", "input": "hi"})
    assert calls == []


def test_endpoint_on_wraps_response_object(monkeypatch):
    _patch_upstream(monkeypatch)
    monkeypatch.setattr(main, "cs_get_config", lambda: _enable_cfg())
    client = TestClient(main.app)
    resp = client.post("/v1/responses", json={"model": "m", "input": "hi"})
    assert resp.status_code == 200
    obj = resp.json()
    assert obj["id"].startswith("resp_")
    assert obj["object"] == "response"
    assert obj["status"] == "completed"
    assert obj["output"][0]["content"] == [{"type": "output_text", "text": "hello from upstream"}]
    stored = asyncio.run(rts.get_store().get(obj["id"]))
    assert stored == obj["output"]


def test_endpoint_on_threads_two_turns(monkeypatch):
    captured = _patch_upstream(monkeypatch)
    monkeypatch.setattr(main, "cs_get_config", lambda: _enable_cfg())
    client = TestClient(main.app)
    r1 = client.post("/v1/responses", json={"model": "m", "input": "turn one"}).json()
    r2 = client.post("/v1/responses", json={
        "model": "m", "input": "turn two", "previous_response_id": r1["id"]}).json()
    assert r2["previous_response_id"] == r1["id"]
    msgs = captured["body"]["messages"]
    # instructions absent; stitched parent output → user turn
    assert msgs[0]["role"] == "assistant"
    assert msgs[0]["content"] == "hello from upstream"
    assert msgs[-1]["content"] == "turn two"


def test_endpoint_on_unknown_previous_id_fails_open(monkeypatch):
    captured = _patch_upstream(monkeypatch)
    monkeypatch.setattr(main, "cs_get_config", lambda: _enable_cfg())
    client = TestClient(main.app)
    resp = client.post("/v1/responses", json={
        "model": "m", "input": "hi", "previous_response_id": "resp_ghost"})
    assert resp.status_code == 200
    msgs = captured["body"]["messages"]
    assert len(msgs) == 1 and msgs[0]["content"] == "hi"


def test_endpoint_on_upstream_error_passthrough(monkeypatch):
    """Error responses must pass through unre-written even when ON."""

    async def fake_process(chat_body, request=None, **_kw):
        from starlette.responses import JSONResponse
        return JSONResponse({"error": {"message": "upstream down"}}, status_code=502)

    monkeypatch.setattr(main, "_process_chat_completion", fake_process)
    monkeypatch.setattr(main, "cs_get_config", lambda: _enable_cfg())
    client = TestClient(main.app)
    resp = client.post("/v1/responses", json={"model": "m", "input": "hi"})
    assert resp.status_code == 502
    assert resp.json()["error"]["message"] == "upstream down"
    assert len(rts.get_store()) == 0


def test_endpoint_on_streaming_passthrough(monkeypatch):
    """Streaming stays byte-identical while the tee stores the assembled turn."""

    async def fake_process(chat_body, request=None, **_kw):
        sse = (
            b'data: {"id":"chatcmpl-live","model":"m","choices":[{"index":0,'
            b'"delta":{"content":"stream ok"}}]}\n\n'
            b"data: [DONE]\n\n"
        )

        async def gen():
            yield sse

        return rts.StreamingResponse(gen(), media_type="text/event-stream")

    monkeypatch.setattr(main, "_process_chat_completion", fake_process)
    monkeypatch.setattr(main, "cs_get_config", lambda: _enable_cfg())
    client = TestClient(main.app)
    with client.stream("POST", "/v1/responses", json={"model": "m", "input": "hi"}) as r:
        assert r.status_code == 200
        body = b"".join(r.iter_bytes())
    assert body == (
        b'data: {"id":"chatcmpl-live","model":"m","choices":[{"index":0,'
        b'"delta":{"content":"stream ok"}}]}\n\n'
        b"data: [DONE]\n\n"
    )
    assert rts.get_store()._get_sync("chatcmpl-live") is not None


def test_endpoint_wiring_order_stitch_before_conversion(monkeypatch):
    """The stitch lands on the responses body BEFORE responses_to_chat runs."""
    seen = {}

    real_convert = main.ResponsesConverter.responses_to_chat

    def spy_convert(body):
        seen["at_convert"] = [it.get("type") for it in body.get("input", [])
                              if isinstance(it, dict)]
        return real_convert(body)

    async def fake_process(chat_body, request=None, **_kw):
        seen["chat_messages"] = chat_body["messages"]
        from starlette.responses import JSONResponse
        return JSONResponse({
            "id": "chatcmpl-1", "model": "m",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}]})

    monkeypatch.setattr(main.ResponsesConverter, "responses_to_chat", staticmethod(spy_convert))
    monkeypatch.setattr(main, "_process_chat_completion", fake_process)
    monkeypatch.setattr(main, "cs_get_config", lambda: _enable_cfg())
    st = rts.reset_store()
    # Stored items are exactly what build_response_object produces.
    st._put_sync("resp_parent", [{"type": "message", "id": "msg_1", "role": "assistant",
                                  "content": [{"type": "output_text", "text": "parent"}]}], None)
    client = TestClient(main.app)
    client.post("/v1/responses", json={
        "model": "m",
        "input": [{"type": "message", "role": "user", "content": "now"}],
        "previous_response_id": "resp_parent"})
    assert seen["at_convert"] == ["message", "message"]  # stitched item arrives first
    # converter maps the stitched history correctly
    assert seen["chat_messages"][0]["role"] == "assistant"
    assert seen["chat_messages"][0]["content"] == "parent"
    assert seen["chat_messages"][-1]["content"] == "now"


def test_endpoint_on_instructions_ride_along(monkeypatch):
    """instructions still become the system message on stitched turns."""
    captured = _patch_upstream(monkeypatch)
    monkeypatch.setattr(main, "cs_get_config", lambda: _enable_cfg())
    st = rts.reset_store()
    st._put_sync("resp_p", [{"type": "message", "role": "assistant",
                             "content": [{"type": "output_text", "text": "old"}]}], None)
    client = TestClient(main.app)
    client.post("/v1/responses", json={
        "model": "m", "instructions": "be terse", "input": "next",
        "previous_response_id": "resp_p"})
    msgs = captured["body"]["messages"]
    assert msgs[0]["role"] == "system" and msgs[0]["content"] == "be terse"
    assert msgs[1]["content"] == "old"
    assert msgs[-1]["content"] == "next"
