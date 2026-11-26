"""Tests for prompt_tool_bridge (xingchi Chat Completions tool bridging) & SSL/terminal guards."""
import asyncio
import json
import ssl

import app.main as main
from app.compat.prompt_tool_bridge import (
    apply_prompt_tool_bridge,
    needs_prompt_tool_bridge,
    parse_prompt_tool_calls_in_completion,
    wrap_sse_with_prompt_tool_bridge,
)


def test_needs_prompt_tool_bridge_gated():
    tools = [{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}}]
    assert needs_prompt_tool_bridge("xingchi", "gpt-6.1-sol", {"tools": tools}) is True
    assert needs_prompt_tool_bridge("xingchi", "gpt-6-sol", {"tools": tools}) is True
    assert needs_prompt_tool_bridge("xingchi", "gpt-6-astra", {"tools": tools}) is False
    assert needs_prompt_tool_bridge("xingchi", "gpt-6.1-sol", {"tools": []}) is False
    assert needs_prompt_tool_bridge("itspn7", "gpt-6.2-sol", {"tools": tools}) is False


def test_apply_prompt_tool_bridge_rewrites_messages_and_strips_tools():
    payload = {
        "model": "gpt-6.1-sol",
        "messages": [
            {"role": "system", "content": "Be concise."},
            {"role": "user", "content": "Check d:/a.txt"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"d:/a.txt"}'}}
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "hello world"},
        ],
        "tools": [{"type": "function", "function": {"name": "read_file", "description": "Read a file", "parameters": {"type": "object"}}}],
        "tool_choice": "auto",
        "parallel_tool_calls": True,
    }
    bridged = apply_prompt_tool_bridge(payload)
    assert "tools" not in bridged
    assert "tool_choice" not in bridged
    assert "parallel_tool_calls" not in bridged
    assert bridged["messages"][0]["role"] == "system"
    assert "read_file" in bridged["messages"][0]["content"]
    assert "<tool_call>" in bridged["messages"][2]["content"]
    assert "tool_calls" not in bridged["messages"][2]
    assert bridged["messages"][3]["role"] == "user"
    assert "hello world" in bridged["messages"][3]["content"]


def test_parse_prompt_tool_calls_in_completion_both_formats():
    resp = {
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": 'Checking file.\n<tool_call>\n{"name":"read_file","arguments":{"path":"x.py"}}\n</tool_call>',
            },
            "finish_reason": "stop",
        }]
    }
    parsed, changed = parse_prompt_tool_calls_in_completion(resp)
    assert changed is True
    ch = parsed["choices"][0]
    assert ch["finish_reason"] == "tool_calls"
    assert ch["message"]["content"] == "Checking file."
    assert len(ch["message"]["tool_calls"]) == 1
    assert ch["message"]["tool_calls"][0]["function"]["name"] == "read_file"
    assert json.loads(ch["message"]["tool_calls"][0]["function"]["arguments"]) == {"path": "x.py"}


def test_wrap_sse_with_prompt_tool_bridge_splits_chunks():
    async def _raw_sse():
        deltas = [
            {"role": "assistant", "content": "OK.\n<tool_"},
            {"content": 'call>\n{"name": "get_weather", '},
            {"content": '"arguments": {"city": "Hanoi"}}\n</tool_call>'},
        ]
        for d in deltas:
            yield f"data: {json.dumps({'id': 'c1', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'gpt-6.1-sol', 'choices': [{'index': 0, 'delta': d, 'finish_reason': None}]})}\n\n".encode()
        yield f"data: {json.dumps({'id': 'c1', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'gpt-6.1-sol', 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 10, 'completion_tokens': 5}})}\n\n".encode()
        yield b"data: [DONE]\n\n"

    async def _collect():
        out = []
        async for b in wrap_sse_with_prompt_tool_bridge(_raw_sse(), "gpt-6.1-sol"):
            out.append(b.decode())
        return "".join(out)

    full = asyncio.run(_collect())
    events = [json.loads(line[5:].strip()) for line in full.splitlines() if line.startswith("data:") and "[DONE]" not in line]
    tool_deltas = [tc for ev in events for ch in ev.get("choices", []) for tc in ((ch.get("delta") or {}).get("tool_calls") or [])]
    finishes = [ch.get("finish_reason") for ev in events for ch in ev.get("choices", []) if ch.get("finish_reason")]
    assert len(tool_deltas) == 1
    assert tool_deltas[0]["function"]["name"] == "get_weather"
    assert json.loads(tool_deltas[0]["function"]["arguments"]) == {"city": "Hanoi"}
    assert finishes == ["tool_calls"]


def test_build_hardened_client_verify_false_propagates_to_transport():
    client = main._build_hardened_client(verify=False)
    try:
        assert client._transport._pool._ssl_context.verify_mode == ssl.CERT_NONE
    finally:
        asyncio.run(client.aclose())


def test_unrecoverable_payload_terminal_markers():
    assert main._is_unrecoverable_payload_400(400, "gpt-6.1-sol requires Responses for tool calls; this account only supports Chat Completions") is True
    assert main._is_unrecoverable_payload_400(403, '{"code":"RQ_VND_UNPRICED"}') is True
    assert main._is_unrecoverable_payload_400(402, '{"code":"RQ_VND_INSUFFICIENT"}') is True
    assert main._is_unrecoverable_payload_400(502, '{"error":{"message":"Gemini upstream (session_unavailable): gemini.google.com tab on :9222 is signed out","code":502}}') is True
    assert main._is_unrecoverable_payload_400(502, '<!DOCTYPE html><title>itspn7.com | 502: Bad gateway</title>') is False


def test_build_next_retry_state_preserves_pass_no_and_lineage():
    prev = {"chain": ["a", "b"], "idx": 0, "pass_no": 3, "orig_request_id": "req123"}
    nxt = main._build_next_retry_state(prev, ["a", "b"], 1, None, "orig", None)
    assert nxt["idx"] == 1
    assert nxt["pass_no"] == 3
    assert nxt["orig_request_id"] == "req123"


def test_process_chat_completion_disconnect_stops_prestream_wrap(monkeypatch):
    class _FakeReq:
        def __init__(self):
            self.calls = 0
            self.state = type("S", (), {})()
            self.headers = {}

        async def is_disconnected(self):
            self.calls += 1
            return self.calls >= 2

    async def _always_wrap(*args, **kwargs):
        raise main._ComboWrapRestart(0.1, {"chain": ["x"], "idx": 0, "pass_no": 2})

    monkeypatch.setattr(main, "_process_chat_completion_pass", _always_wrap)
    req = _FakeReq()
    resp = asyncio.run(main._process_chat_completion({"model": "Gemini-4-Carbon"}, request=req))
    assert resp.status_code == 499
    assert req.calls >= 2
