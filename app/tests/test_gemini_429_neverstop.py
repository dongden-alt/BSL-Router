"""Regression: 429 on a Gemini-client stream start must NEVER force-stop.

Evidence (2026-09-04, .brain/logs/app.out.log req 18d208ae15/6c, hy3 combo):
the Gemini stream-start non-200 handler gated the never-stop wrap call on
`_next_gemini_combo_retry_state() is not None`. The wrap lives INSIDE
_raise_gemini_combo_fallback for the exhausted (None) case, so when the final
chain entry answered 429 the helper was never called: no wrap print in the
log, terminal 429 frame, force stop.

Two locks:
  1. Behavioural: final-entry 429 at stream start wraps to the next pass and
     the client receives a recovered success stream (no terminal 429 frame).
  2. Source-level: the `is not None` gate may never return in front of the
     stream-start combo-fallback call — exhaustion is handled INSIDE the
     helper by the never-stop wrap.
"""

from __future__ import annotations

import asyncio
import builtins
import inspect
import io
import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app.config_state as cs
import app.main as main


class _ChunkStream(httpx.AsyncByteStream):
    def __init__(self, *chunks: bytes):
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


class _Breaker:
    """Breaker that never filters — isolates the test from CB state."""

    enabled = True
    stream_stall_timeout = 10.0

    @staticmethod
    def filter_healthy_connections(_provider, _model, connections):
        return connections


class _ScriptedClient:
    def __init__(self, behaviors):
        self.behaviors = behaviors
        self.models = []

    def build_request(self, method, url, **kwargs):
        return httpx.Request(method, url, **kwargs)

    async def send(self, request, stream=False):
        payload = json.loads(request.content)
        model = payload["model"]
        self.models.append(model)
        behavior = self.behaviors[model]
        if callable(behavior):
            return await behavior(request)
        return httpx.Response(200, request=request, stream=behavior)


def _openai_chunk(model: str, text: str = "", finish_reason=None) -> bytes:
    payload = {
        "id": f"chatcmpl-{model}",
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [{
            "index": 0,
            "delta": {"content": text} if text else {},
            "finish_reason": finish_reason,
        }],
    }
    return f"data: {json.dumps(payload)}\n\ndata: [DONE]\n\n".encode()


def _success_stream(model: str, text: str = "recovered-after-wrap") -> _ChunkStream:
    return _ChunkStream(_openai_chunk(model, text, "stop"))


def _config() -> dict:
    providers = {
        "b-ai": {
            "type": "custom",
            "format": "openai",
            "connections": [{
                "enabled": True,
                "api_key": "test",
                "base_url": "https://b-ai.invalid",
            }],
            "models": [{"id": "hy3", "enabled": True, "thinking": "off"}],
        },
    }
    return {
        # Knob ON is the default in production; the force-stop bug lived here.
        "settings": {"combo_infinite_retry": True},
        "tools": {"output_thinking_squeeze": False},
        "providers": providers,
        "combos": [{
            "alias": "Hy3",
            "strategy": "fallback",
            "chain": [{"provider": "b-ai", "model": "hy3"}],
        }],
        "aliases": {},
    }


def _install(monkeypatch, client):
    real_open = builtins.open

    def open_without_forensics(path, *args, **kwargs):
        if str(path).replace("\\", "/").endswith(".brain/logs/outbound_upstream.jsonl"):
            return io.StringIO()
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", open_without_forensics)
    cs.replace_config(_config())
    monkeypatch.setattr(main, "_get_client_for_proxy", lambda *_args: client)
    monkeypatch.setattr(main, "get_breaker", lambda: _Breaker())
    monkeypatch.setattr(main.obs, "log_request_start", lambda **kwargs: "req-test")
    monkeypatch.setattr(main.obs, "log_request", lambda **kwargs: None)
    monkeypatch.setattr(main, "GEMINI_EGRESS_KEEPALIVE_INTERVAL", 0.005)
    monkeypatch.setattr(main, "GEMINI_EGRESS_CONNECT_KEEPALIVE_INTERVAL", 0.005)
    monkeypatch.setattr(main, "GEMINI_EGRESS_CONNECT_TIMEOUT", 0.5)
    monkeypatch.setattr(main, "GEMINI_EGRESS_BODY_STALL_TIMEOUT", 0.1)

    # Repeated upstream 429s must not soft-ban the leaf (the wrap is the recovery).
    import app.error_prevention as ep
    monkeypatch.setattr(ep, "check_ban", lambda *a, **k: (False, None, 0))


async def _collect():
    response = await main._process_chat_completion(
        {
            "model": "Hy3",
            "_bsl_original_model": "gemini-pro-agent",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": True,
        },
        client_wants_gemini=True,
    )
    return [chunk async for chunk in response.body_iterator]


def test_final_entry_429_streamstart_wraps_instead_of_force_stop(monkeypatch):
    """Entry-N 429 at stream start must wrap to the next pass, not terminate.

    429 on dials 1..3 (incl. the FINAL entry — where the old code
    force-stopped), success on dial 4: proves the wrap executed and the
    client got the recovered pass-2 stream instead of a terminal 429 frame.
    """

    async def rate_limited_then_recover(request):
        if len(client.models) < 4:
            return httpx.Response(
                429,
                request=request,
                stream=_ChunkStream(
                    b'{"error":{"message":"TPM limit exceeded"}}'),
                headers={"content-type": "application/json"},
            )
        return httpx.Response(
            200, request=request, stream=_success_stream("hy3"))

    client = _ScriptedClient({"hy3": rate_limited_then_recover})
    _install(monkeypatch, client)

    chunks = asyncio.run(_collect())
    output = b"".join(chunks)

    # The wrap happened: dials continued past final-entry exhaustion.
    assert len(client.models) >= 3, (
        "final-entry 429 must wrap to the next pass (never-stop), "
        f"only {len(client.models)} upstream dials observed"
    )
    # The client received the recovered pass-2 stream, not a terminal 429 frame.
    assert b"recovered-after-wrap" in output, (
        "client stream must contain the retried pass's model output"
    )
    assert b"TPM limit exceeded" not in output, (
        "the terminal-429 force-stop frame must not reach the client"
    )
    assert output.rstrip().endswith(b"data: [DONE]"), (
        "stream must end with the valid Gemini terminal sentinel"
    )


def test_streamstart_gate_removed_source_pin():
    """Source-level lock: no `is not None` gate in front of the wrap call.

    The gate made the helper's exhausted-case wrap branch unreachable from
    the stream-start site — exactly the 429 force-stop this fix removes.
    """
    src = inspect.getsource(main)
    assert "_next_gemini_combo_retry_state() is not None" not in src, (
        "stream-start handler must call _raise_gemini_combo_fallback "
        "unconditionally: the exhausted case is handled INSIDE the helper "
        "by the never-stop wrap"
    )
    # And the stream-start call site itself still exists (ungated).
    assert src.count("_raise_gemini_combo_fallback(resp.status_code") >= 1, (
        "the stream-start combo-fallback call must remain present"
    )
