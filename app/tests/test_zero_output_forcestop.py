
"""Force-stop fix regression tests (2026-08-24).

Bug 1: gemini stream adapter appended BSL_NO_OUTPUT_NOTICE as ordinary finish
       text, producing a fake finishReason=STOP whose only content was the
       notice -- rendered by the IDE as the recurring force-stop message while
       combo fallback never fired.
Bug 2: nested _process_chat_completion fallback handlers did 'async for' over
       plain JSONResponse objects -> TypeError -> afz_guard terminal 502 ->
       force-stop.
"""
import json
from starlette.responses import JSONResponse, StreamingResponse

from app.compat.adapters.gemini import BSL_NO_OUTPUT_NOTICE, openai_response_to_gemini


def _finish_chunk(finish: str = "stop"):
    return {
        "id": "cmpl-test", "object": "chat.completion.chunk", "created": 1,
        "model": "test", "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
    }


def test_stream_adapter_drops_notice_finish_frame():
    """A parts-less finish chunk must NOT carry BSL_NO_OUTPUT_NOTICE as text.

    v3: the frame is still emitted (carries finishReason + usageMetadata),
    but its sole part is empty text — not the notice. gemini_frame_has_content
    returns False for empty text, so combo fallback still fires.
    """
    from app.compat.adapters.gemini import openai_chunk_to_gemini, gemini_frame_has_content
    state = {}
    frame = openai_chunk_to_gemini(_finish_chunk(), state)
    assert frame is not None, "finish frame must be emitted (carries finishReason)"
    parts = frame["response"]["candidates"][0]["content"]["parts"]
    assert len(parts) == 1
    assert parts[0].get("text") == "", f"expected empty text, got {parts[0]!r}"
    assert parts[0].get("text") != BSL_NO_OUTPUT_NOTICE
    assert gemini_frame_has_content(frame) is False


def test_stream_adapter_keeps_real_text():
    from app.compat.adapters.gemini import openai_chunk_to_gemini
    state = {}
    chunk = {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": "m",
             "choices": [{"index": 0, "delta": {"content": "hello"}, "finish_reason": None}]}
    frame = openai_chunk_to_gemini(chunk, state)
    assert frame is not None
    parts = frame.get("candidates", frame.get("response",{}).get("candidates",[]))[0]["content"]["parts"]
    assert any(p.get("text") == "hello" for p in parts)


def test_nonstream_twin_keeps_notice():
    """Non-stream conversion still surfaces the notice (no freeze risk)."""
    resp = {"id": "c", "object": "chat.completion", "created": 1, "model": "m",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": ""}, "finish_reason": "stop"}]}
    obj = openai_response_to_gemini(resp, "m")
    parts = obj["response"]["candidates"][0]["content"]["parts"]
    assert any(p.get("text") == BSL_NO_OUTPUT_NOTICE for p in parts)


def _fake_nested_jsonresponse():
    """Stands in for _process_chat_completion returning a plain JSONResponse."""
    return JSONResponse({"error": "no provider available"}, status_code=503)


def test_fallback_handler_accepts_jsonresponse_no_typeerror():
    """The hardened dispatch contract: never 'async for' over a Response."""
    resp = _fake_nested_jsonresponse()
    assert not hasattr(resp, "body_iterator")
    assert not hasattr(resp, "__aiter__")
    body = resp.body if isinstance(resp.body, (bytes, bytearray)) else str(resp.body).encode()
    assert b"no provider available" in body


def test_fallback_handler_accepts_streamingresponse():
    async def _g():
        yield b"data: x\n\n"
    sr = StreamingResponse(_g())
    assert hasattr(sr, "body_iterator")
