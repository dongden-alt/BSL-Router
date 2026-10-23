"""Regression coverage for Codex Responses SSE error and tool events."""
import asyncio
import json

from app.codex_adapter import responses_sse_to_openai_sse


def _sse(events):
    return b"".join(
        f"event: {name}\ndata: {json.dumps(data)}\n\n".encode()
        for name, data in events
    )


async def _aiter(payload):
    yield payload


def _frames(payload):
    async def collect():
        return b"".join([chunk async for chunk in responses_sse_to_openai_sse(_aiter(payload))])
    raw = asyncio.run(collect()).decode()
    return [json.loads(line[6:]) for line in raw.splitlines() if line.startswith("data: ")]


def test_response_failed_is_visible_and_terminal():
    frames = _frames(_sse([
        ("response.failed", {"response": {"model": "codex", "error": {"message": "upstream boom"}}}),
    ]))
    assert frames
    assert any("upstream boom" in c.get("choices", [{}])[0].get("delta", {}).get("content", "") for c in frames)
    assert any(c["choices"][0]["finish_reason"] == "error" for c in frames)


def test_healthy_tool_call_and_usage_are_preserved():
    frames = _frames(_sse([
        ("response.output_item.done", {"item": {"type": "function_call", "id": "call_1", "name": "lookup", "arguments": "{\"q\":1}"}}),
        ("response.completed", {"response": {"model": "codex", "output": [{"type": "function_call", "id": "call_1"}], "usage": {"input_tokens": 3, "output_tokens": 4}}}),
    ]))
    assert any(c["choices"][0]["delta"].get("tool_calls") for c in frames)
    finish = frames[-1]
    assert finish["choices"][0]["finish_reason"] == "tool_calls"
    assert finish["usage"]["completion_tokens"] == 4


def test_response_incomplete_is_surfaced():
    frames = _frames(_sse([
        ("response.incomplete", {"response": {"incomplete_details": {"reason": "max_output_tokens"}}}),
    ]))
    assert any("max_output_tokens" in c.get("choices", [{}])[0].get("delta", {}).get("content", "") for c in frames)
    assert any(c["choices"][0]["finish_reason"] == "error" for c in frames)


def test_reasoning_summary_delta_and_error_event_conversion():
    frames = _frames(_sse([
        ("response.reasoning_summary_text.delta", {"model": "gpt-6.1-sol", "delta": "Thinking step 1..."}),
        ("error", {"error": {"message": "upstream stream interrupted", "code": "server_error"}}),
    ]))
    assert any(
        c.get("choices", [{}])[0].get("delta", {}).get("reasoning_content") == "Thinking step 1..."
        for c in frames
    )
    assert any(
        "upstream stream interrupted" in c.get("choices", [{}])[0].get("delta", {}).get("content", "")
        for c in frames
    )
    assert frames[-1]["choices"][0]["finish_reason"] == "error"


def test_codex_event_carries_output_excludes_scaffolding_echo():
    from app.main import _codex_event_carries_output

    created_echo = {
        "type": "response.created",
        "response": {
            "id": "resp_123",
            "instructions": 'Use "function_call":{"name":"bash"} and "content":"hello"',
            "tools": [{"type": "function", "name": "bash", "description": '"text":"run"'}],
        },
    }
    assert _codex_event_carries_output(created_echo) is False
    assert _codex_event_carries_output({"type": "response.in_progress", "response": created_echo["response"]}) is False
    assert _codex_event_carries_output({"type": "response.output_text.delta", "delta": "Hi"}) is True
    assert _codex_event_carries_output({"type": "response.reasoning_summary_text.delta", "delta": "Hmm"}) is True
    assert _codex_event_carries_output({
        "type": "response.output_item.done",
        "item": {"type": "function_call", "name": "bash", "arguments": "{\"cmd\":\"ls\"}"},
    }) is True


def test_truncation_detector_and_continuation_payload_support_codex_responses():
    from app.middleware.quality import StreamTruncationDetector, build_continuation_payload

    det = StreamTruncationDetector(enabled=True, accumulate=True)
    det.feed(
        b'event: response.output_text.delta\n'
        b'data: {"type":"response.output_text.delta","delta":"Partial answer "}\n\n'
        b'event: response.output_item.done\n'
        b'data: {"type":"response.output_item.done","item":{"type":"function_call","name":"read_file","arguments":"{\\"path\\":\\"a.py\\"}"}}\n\n'
        b'event: response.incomplete\n'
        b'data: {"type":"response.incomplete","response":{"incomplete_details":{"reason":"max_output_tokens"}}}\n\n'
    )
    assert det.partial_text == "Partial answer "
    assert det.tool_call_chars > 0
    assert det.truncated is True

    orig = {
        "model": "gpt-6.1-sol",
        "instructions": "Be concise.",
        "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Write code"}]}],
        "stream": True,
    }
    cont = build_continuation_payload(orig, det.partial_text, stream=False)
    assert cont is not None
    assert "messages" not in cont
    assert len(cont["input"]) == 3
    assert cont["input"][1]["role"] == "assistant"
    assert cont["input"][1]["content"][0]["text"] == "Partial answer "
    assert cont["input"][2]["role"] == "user"
    assert cont["stream"] is True

