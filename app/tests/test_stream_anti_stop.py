"""
Unit tests for the streaming anti-stop detector (Qwen3.8-Max stop fix).

Run:
  .venv\\Scripts\\python -m pytest app/tests/test_stream_anti_stop.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.middleware.quality import (
    StreamTruncationDetector,
    build_continuation_stream_payload,
    build_continuation_payload,
)


def _feed_lines(det, lines):
    for ln in lines:
        det.feed(ln.encode("utf-8"))


def test_openai_length_truncation_detected():
    det = StreamTruncationDetector("hcnsec-vip/qwen3.8-max", enabled=True)
    _feed_lines(det, [
        'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n',
        'data: {"choices":[{"delta":{"content":" world"}}]}\n\n',
        'data: {"choices":[{"delta":{},"finish_reason":"length"}]}\n\n',
        'data: [DONE]\n\n',
    ])
    assert det.truncated
    assert det.partial_text == "Hello world"


def test_openai_stop_not_truncated():
    det = StreamTruncationDetector()
    _feed_lines(det, [
        'data: {"choices":[{"delta":{"content":"Done."}}]}\n\n',
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
    ])
    assert not det.truncated
    assert det.partial_text == "Done."


def test_anthropic_max_tokens_detected():
    det = StreamTruncationDetector()
    _feed_lines(det, [
        'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"part1"}}\n\n',
        'data: {"type":"message_delta","delta":{"stop_reason":"max_tokens"}}\n\n',
    ])
    assert det.truncated
    assert det.partial_text == "part1"


def test_gemini_max_tokens_detected():
    det = StreamTruncationDetector()
    _feed_lines(det, [
        'data: {"candidates":[{"content":{"parts":[{"text":"x"}]}}]}\n\n',
        'data: {"candidates":[{"content":{"parts":[{"text":"y"}]},"finishReason":"MAX_TOKENS"}]}\n\n',
    ])
    assert det.truncated
    assert det.partial_text == "xy"


def test_disabled_detector_is_noop():
    det = StreamTruncationDetector(enabled=False)
    _feed_lines(det, ['data: {"choices":[{"delta":{"content":"x"},"finish_reason":"length"}]}\n\n'])
    assert not det.truncated
    assert det.partial_text == ""


def test_continuation_stream_payload_keeps_stream_true():
    payload = {
        "model": "qwen3.8-max",
        "messages": [{"role": "user", "content": "write essay"}],
        "stream": True,
    }
    cont = build_continuation_stream_payload(payload, "partial text here")
    assert cont is not None
    assert cont["stream"] is True
    assert cont["messages"][-2]["role"] == "assistant"
    assert cont["messages"][-2]["content"] == "partial text here"
    assert cont["messages"][-1]["role"] == "user"
    assert "[BSL_CONTINUE]" in cont["messages"][-1]["content"]


def test_continuation_payload_default_stays_buffered():
    payload = {"model": "qwen3.8-max", "messages": [{"role": "user", "content": "hi"}], "stream": True}
    cont = build_continuation_payload(payload, "partial")
    assert cont is not None
    assert cont["stream"] is False  # legacy S3 behavior unchanged


def test_continuation_empty_text_returns_none():
    assert build_continuation_stream_payload({"messages": []}, "   ") is None


def test_garbage_chunks_fail_open():
    det = StreamTruncationDetector()
    det.feed(b"\xff\xfe broken \x00 garbage")
    det.feed(b'data: not-json\n\n')
    det.feed(b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"length"}]}\n\n')
    assert det.truncated
    assert det.partial_text == "ok"


def test_deepseek_v4_length_truncation_detected():
    det = StreamTruncationDetector("bslforge/opencode-free/deepseek-v4.1-flash-free", enabled=True)
    _feed_lines(det, [
        'data: {"choices":[{"delta":{"content":"step 1"}}]}\n\n',
        'data: {"choices":[{"delta":{"content":" step 2"}}]}\n\n',
        'data: {"choices":[{"delta":{},"finish_reason":"length"}]}\n\n',
        'data: [DONE]\n\n',
    ])
    assert det.truncated
    assert det.partial_text == "step 1 step 2"


def test_main_stream_anti_stop_covers_deepseek():
    main_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py"
    )
    src = open(main_path, encoding="utf-8").read()
    assert 'is_deepseek = matches_contract(f_val, "deepseek-v4")' in src
    assert "and (is_qwen or is_deepseek)" in src
    assert "[bsl-deepseek-complete]" in src
