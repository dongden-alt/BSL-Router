"""
Regression coverage for the false `zero_output_tokens` combo-fallback /
terminal error frames on tool-call-only (and usage-omitting text) streams.

Root cause: the TTFT content detectors at three egress sites in app/main.py
only matched `content` / `reasoning_content` / `text` deltas. A DeepSeek
tool-call-only reply (or any usage-omitting reseller stream) therefore left
`stats["ttft"] == 0`, so the post-pump zero-token gates at Site 2 (Anthropic
egress) and Site 4 (Anthropic->OpenAI egress) fired AFTER a healthy stream
had already emitted tool blocks. The result: a spurious `_anthropic_terminal_
error_frames("zero_output_tokens")` interleaved with flushed tool_use blocks,
plus a duplicated message_stop — Claude Code reported "tool call error".

The fix: a module-level `_openai_chunk_carries_output` helper that also
recognises `tool_calls` / `function_call` / `finish_reason: tool_calls`,
plugged into all three detectors, plus `and not stats.get("ttft")` added to
the Site 2 and Site 4 zero-token gates.
"""

import asyncio
import builtins
import io
import json

import httpx

import app.config_state as cs
import app.main as main


# ──────────────────────────────────────────────────────────────────────────────
# Chunk helpers — OpenAI SSE frames.
# ──────────────────────────────────────────────────────────────────────────────

def _openai_sse(data_str: str) -> bytes:
    """One OpenAI SSE data frame."""
    return f"data: {data_str}\n\n".encode()


def _done() -> bytes:
    return b"data: [DONE]\n\n"


def _chunk(model: str, delta: dict, finish_reason=None) -> bytes:
    payload = {
        "id": f"chatcmpl-{model}",
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [{
            "index": 0,
            "delta": delta,
            "finish_reason": finish_reason,
        }],
    }
    return _openai_sse(json.dumps(payload))


def _toolcall_stream(model: str) -> list[bytes]:
    """A tool-call-only OpenAI SSE stream (no usage)."""
    return [
        _chunk(model, {"role": "assistant"}),
        _chunk(model, {
            "tool_calls": [{
                "index": 0,
                "id": "call_toolonly_001",
                "type": "function",
                "function": {"name": "get_weather", "arguments": ""},
            }],
        }),
        _chunk(model, {
            "tool_calls": [{
                "index": 0,
                "function": {"arguments": '{"city": "Han'},
            }],
        }),
        _chunk(model, {
            "tool_calls": [{
                "index": 0,
                "function": {"arguments": 'oi"}'},
            }],
        }),
        # The two fragments above concatenate to {"city": "Hanoi"}
        # (the normalizer accumulates arg_fragments without re-parsing)
        _chunk(model, {}, finish_reason="tool_calls"),
        _done(),
    ]


def _text_stream(model: str, text: str = "hello world") -> list[bytes]:
    """A plain-text OpenAI SSE stream (no usage)."""
    return [
        _chunk(model, {"role": "assistant"}),
        _chunk(model, {"content": text}),
        _chunk(model, {}, finish_reason="stop"),
        _done(),
    ]


# ──────────────────────────────────────────────────────────────────────────────
# Scripted client — mirrors test_mapped_gemini_combo_fallback.py.
# ──────────────────────────────────────────────────────────────────────────────

class _ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]):
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


class _Breaker:
    enabled = True
    stream_stall_timeout = 10.0

    @staticmethod
    def filter_healthy_connections(_provider, _model, connections):
        return connections


class _ScriptedClient:
    def __init__(self, behaviors: dict):
        self.behaviors = behaviors
        self.models = []

    def build_request(self, method, url, **kwargs):
        return httpx.Request(method, url, **kwargs)

    async def send(self, request, stream=False):
        payload = json.loads(request.content)
        model = payload["model"]
        self.models.append(model)
        behavior = self.behaviors.get(model)
        if behavior is None:
            raise httpx.ConnectError(f"no scripted behavior for model {model!r}")
        if callable(behavior):
            return await behavior(request)
        return httpx.Response(200, request=request, stream=behavior)


# ──────────────────────────────────────────────────────────────────────────────
# Config — two-provider combo "DS-TOOL", both openai format, thinking off.
# ──────────────────────────────────────────────────────────────────────────────

def _config() -> dict:
    providers = {}
    for provider, model in (("p0", "m0"), ("p1", "m1")):
        providers[provider] = {
            "type": "custom",
            "format": "openai",
            "connections": [{
                "enabled": True,
                "api_key": "test",
                "base_url": f"https://{provider}.invalid",
            }],
            "models": [{"id": model, "enabled": True, "thinking": "off"}],
        }
    return {
        "tools": {"output_thinking_squeeze": False},
        "providers": providers,
        "combos": [{
            "alias": "DS-TOOL",
            "strategy": "fallback",
            "chain": [
                {"provider": "p0", "model": "m0"},
                {"provider": "p1", "model": "m1"},
            ],
        }],
        "aliases": {},
    }


# ──────────────────────────────────────────────────────────────────────────────
# Install — monkeypatches mirroring the mapped-gemini harness.
# ──────────────────────────────────────────────────────────────────────────────

def _install(monkeypatch, client, *, breaker=None):
    real_open = builtins.open

    def open_without_forensics(path, *args, **kwargs):
        if str(path).replace("\\", "/").endswith(".brain/logs/outbound_upstream.jsonl"):
            return io.StringIO()
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", open_without_forensics)
    cs.replace_config(_config())
    monkeypatch.setattr(main, "_get_client_for_proxy", lambda *_args: client)
    monkeypatch.setattr(main, "get_breaker", lambda: breaker)
    monkeypatch.setattr(main.obs, "log_request_start", lambda **kwargs: "req")
    monkeypatch.setattr(main.obs, "log_request", lambda **kwargs: None)
    # Keep these small so any unintended stall path surfaces quickly; the
    # streams here are fully scripted and return instantly.
    monkeypatch.setattr(main, "GEMINI_EGRESS_KEEPALIVE_INTERVAL", 0.005)
    monkeypatch.setattr(main, "GEMINI_EGRESS_CONNECT_KEEPALIVE_INTERVAL", 0.005)
    monkeypatch.setattr(main, "GEMINI_EGRESS_CONNECT_TIMEOUT", 0.03)
    monkeypatch.setattr(main, "GEMINI_EGRESS_BODY_STALL_TIMEOUT", 0.01)


async def _collect(body=None, client_wants_anthropic=True):
    response = await main._process_chat_completion(
        body or {
            "model": "DS-TOOL",
            "_bsl_original_model": "claude-code",
            "messages": [
                {"role": "user", "content": "what is the weather in hanoi?"},
            ],
            "tools": [{
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get current weather",
                    "parameters": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                    },
                },
            }],
            "stream": True,
        },
        client_wants_anthropic=client_wants_anthropic,
    )
    return [chunk async for chunk in response.body_iterator]


# ──────────────────────────────────────────────────────────────────────────────
# SSE frame parser — decodes byte frames into a list of dicts.
# ──────────────────────────────────────────────────────────────────────────────

def _parse_sse_frames(frames):
    out = []
    for fr in frames:
        text = fr.decode("utf-8") if isinstance(fr, (bytes, bytearray)) else str(fr)
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                payload = line[len("data:"):].strip()
                if not payload or payload == "[DONE]":
                    out.append({"__raw__": payload, "__done__": payload == "[DONE]"})
                    continue
                try:
                    out.append(json.loads(payload))
                except json.JSONDecodeError:
                    out.append({"__raw__": payload})
    return out


def _event_types(frames):
    """Ordered list of Anthropic event types in the frame sequence."""
    parsed = _parse_sse_frames(frames)
    return [p.get("type") for p in parsed if isinstance(p, dict) and p.get("type")]


# ──────────────────────────────────────────────────────────────────────────────
# A — Anthropic egress: tool-call-only stream, no false fallback, no error tail.
# ──────────────────────────────────────────────────────────────────────────────

def test_toolcall_only_stream_anthropic_egress_no_false_fallback(monkeypatch):
    """Site 2 (_raw_ok, Anthropic egress).

    m0 returns a tool-call-only stream with no usage. Before the fix the
    zero-token gate fired post-emission and spliced a terminal error frame
    (containing 'zero_output_tokens') ahead of the flushed tool_use blocks,
    producing two message_stop events and an 'error' event. After the fix
    the helper recognises tool_calls as output, ttft is set, and the stream
    closes cleanly with a single message_stop and real tool_use blocks.
    """
    client = _ScriptedClient({
        "m0": _ChunkStream(_toolcall_stream("m0")),
        "m1": _ChunkStream(_text_stream("m1", "fallback-should-not-reach")),
    })
    _install(monkeypatch, client, breaker=_Breaker())

    chunks = asyncio.run(_collect())
    output = b"".join(chunks)
    types = _event_types(chunks)

    # No false advance: only the healthy first leaf is dialed.
    assert client.models == ["m0"], f"expected only m0, got {client.models}"
    # The tool call is present in the output.
    assert b'"tool_calls"' in output or b"tool_use" in output, \
        "tool call must reach the client"
    # At least one content_block_start (the tool_use block) and an
    # input_json_delta carrying the argument fragments.
    assert types.count("content_block_start") >= 1, \
        f"expected >=1 content_block_start, got {types}"
    # input_json_delta arrives NESTED as delta.type inside content_block_delta
    # events — check the payload shape, not the top-level event type.
    parsed = _parse_sse_frames(chunks)
    assert any(
        isinstance(p, dict)
        and p.get("type") == "content_block_delta"
        and isinstance(p.get("delta"), dict)
        and p["delta"].get("type") == "input_json_delta"
        for p in parsed
    ), f"expected an input_json_delta payload, got {parsed}"
    # EXACTLY ONE message_stop — the normalizer's own terminal. The false
    # fallback path would have added a second (from the terminal builder).
    assert types.count("message_stop") == 1, \
        f"expected exactly one message_stop, got {types.count('message_stop')}: {types}"
    # NO error event interleaved with the tool calls.
    assert "error" not in types, f"no error event allowed, got {types}"
    # The stall reason text must not appear anywhere in the bytes.
    assert b"zero_output_tokens" not in output, \
        "zero_output_tokens must not appear in the output"


# ──────────────────────────────────────────────────────────────────────────────
# B — Raw OpenAI passthrough: tool-call-only stream, no egress conversion.
# ──────────────────────────────────────────────────────────────────────────────

def test_toolcall_only_stream_raw_openai_egress_passthrough(monkeypatch):
    """Site 1 (raw_upstream, OpenAI passthrough).

    OpenAI client, openai-format provider → no egress conversion. The
    zero-token gate already carries `and not stats.get("ttft")`; the helper
    ensures tool_calls set ttft so the gate stays closed. Assert only m0
    dialed, tool_calls present, exactly one [DONE], no error frame.
    """
    client = _ScriptedClient({
        "m0": _ChunkStream(_toolcall_stream("m0")),
        "m1": _ChunkStream(_text_stream("m1", "fallback-should-not-reach")),
    })
    _install(monkeypatch, client, breaker=_Breaker())

    # OpenAI client (no Anthropic, no Gemini) → raw_upstream path.
    chunks = asyncio.run(_collect(client_wants_anthropic=False))
    output = b"".join(chunks)
    parsed = _parse_sse_frames(chunks)

    assert client.models == ["m0"], f"expected only m0, got {client.models}"
    assert b'"tool_calls"' in output, "tool_calls must be present in raw output"
    done_count = sum(1 for p in parsed if isinstance(p, dict) and p.get("__done__"))
    assert done_count == 1, f"expected exactly one [DONE], got {done_count}"
    assert not any(
        isinstance(p, dict) and isinstance(p.get("error"), dict) for p in parsed
    ), f"no error frame allowed, got {parsed}"
    assert b"zero_output_tokens" not in output


# ──────────────────────────────────────────────────────────────────────────────
# C — Anthropic egress: plain text stream without usage, no error tail.
# ──────────────────────────────────────────────────────────────────────────────

def test_text_stream_without_usage_anthropic_egress_no_error_tail(monkeypatch):
    """Site 2 gate guard for text streams.

    m0 returns plain text with no usage chunk. The helper matches
    `"content":"..."` so ttft is set and the zero-token gate stays closed.
    Exactly one message_stop, no error event, no zero_output_tokens text.
    """
    client = _ScriptedClient({
        "m0": _ChunkStream(_text_stream("m0", "the capital is hanoi")),
        "m1": _ChunkStream(_text_stream("m1", "fallback-should-not-reach")),
    })
    _install(monkeypatch, client, breaker=_Breaker())

    chunks = asyncio.run(_collect())
    output = b"".join(chunks)
    types = _event_types(chunks)

    assert client.models == ["m0"], f"expected only m0, got {client.models}"
    assert b"the capital is hanoi" in output
    assert types.count("message_stop") == 1, \
        f"expected exactly one message_stop, got {types.count('message_stop')}"
    assert "error" not in types, f"no error event allowed, got {types}"
    assert b"zero_output_tokens" not in output


# ──────────────────────────────────────────────────────────────────────────────
# D — Unit tests for `_openai_chunk_carries_output`.
# ──────────────────────────────────────────────────────────────────────────────

def test_helper_compact_tool_calls_non_empty():
    assert main._openai_chunk_carries_output('"tool_calls":[{"index":0}]') is True


def test_helper_spaced_tool_calls_non_empty():
    assert main._openai_chunk_carries_output('"tool_calls": [{"index":0}]') is True


def test_helper_empty_tool_calls_collection_is_not_output():
    assert main._openai_chunk_carries_output('"tool_calls":[]') is False
    assert main._openai_chunk_carries_output('"tool_calls": []') is False


def test_helper_finish_reason_tool_calls_compact():
    assert main._openai_chunk_carries_output('"finish_reason":"tool_calls"') is True


def test_helper_finish_reason_tool_calls_spaced():
    assert main._openai_chunk_carries_output('"finish_reason": "tool_calls"') is True


def test_helper_spaced_content_non_empty():
    assert main._openai_chunk_carries_output('"content": "hello"') is True


def test_helper_empty_content_is_not_output():
    assert main._openai_chunk_carries_output('"content":""') is False
    assert main._openai_chunk_carries_output('"content": ""') is False


def test_helper_empty_string_is_not_output():
    assert main._openai_chunk_carries_output("") is False


def test_helper_legacy_function_call_non_empty():
    assert main._openai_chunk_carries_output('"function_call":{"name":"x"}') is True
    assert main._openai_chunk_carries_output('"function_call": {"name":"x"}') is True


def test_helper_empty_function_call_is_not_output():
    assert main._openai_chunk_carries_output('"function_call":{}') is False
    assert main._openai_chunk_carries_output('"function_call": {}') is False
