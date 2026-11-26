"""Regression tests for zero-quality-loss Kiro/Cloudflare outage fallback,
post-content_block_stop ping-tail finalization, 1-key Kiro serialization, and
Claude-Opus-5.5 Studio bridge shields.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.compat.stream_normalizer import StreamNormalizer
import app.main as main_mod

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# 1. Outage fallback trigger classification
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "status,err,expected",
    [
        (524, "<!DOCTYPE html> Error code 524", True),
        (520, "Cloudflare origin error", True),
        (522, "Connection timed out", True),
        (504, "upstream_header_timeout", True),
        (502, "<!DOCTYPE html> <!--[if lt IE 7]> Error code 502", True),
        (503, "The upstream provider returned no answer for this request. Please retry.", True),
        (502, "midstream_transport: peer closed connection without sending complete message body", True),
        (400, "invalid request", False),
        (401, "invalid API key", False),
        (403, "Key này chỉ dùng được sau khi xác thực Telegram", False),
        (429, "rate limit exceeded", False),
        (503, "个人智能路由池没有活跃候选", False),
        (503, "no active merchant", False),
        (502, "invalid api key", False),
    ],
)
def test_outage_fallback_trigger_classification(status, err, expected):
    assert main_mod._is_outage_max_tokens_fallback_trigger(status, err) is expected


# ---------------------------------------------------------------------------
# 2. Request-scoped ephemeral 16384 max_tokens fallback + budget_tokens safety
# ---------------------------------------------------------------------------

def test_fallback_max_tokens_noop_on_fresh_request_and_resets_next_call():
    payload_fresh = {
        "model": "claude-opus-5.5",
        "max_tokens": 65535,
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "max"},
    }
    assert main_mod._apply_request_scoped_fallback_max_tokens(payload_fresh, "pix4k", None) is None
    assert payload_fresh["max_tokens"] == 65535

    payload_retry = dict(payload_fresh)
    applied = main_mod._apply_request_scoped_fallback_max_tokens(
        payload_retry, "pix4k", {"fallback_max_tokens": 16384}
    )
    assert applied == 16384
    assert payload_retry["max_tokens"] == 16384
    assert payload_retry["thinking"] == {"type": "adaptive"}
    assert payload_retry["output_config"] == {"effort": "max"}

    # Next fresh call has retry_state=None -> stays at 65535
    payload_next = dict(payload_fresh)
    assert main_mod._apply_request_scoped_fallback_max_tokens(payload_next, "pix4k", None) is None
    assert payload_next["max_tokens"] == 65535


def test_fallback_max_tokens_respects_enabled_budget_tokens_invariant():
    # Anthropic enabled thinking with 32K budget MUST keep max_tokens > budget_tokens
    payload = {
        "model": "claude-opus-4-6",
        "max_tokens": 65536,
        "thinking": {"type": "enabled", "budget_tokens": 32768},
    }
    applied = main_mod._apply_request_scoped_fallback_max_tokens(
        payload, "pix4k", {"fallback_max_tokens": 16384}
    )
    assert applied == 36864
    assert payload["max_tokens"] == 36864
    assert payload["max_tokens"] > payload["thinking"]["budget_tokens"]

    # When budget + 4096 >= cur_mt, skip clamp rather than raising 400
    payload_tight = {
        "model": "claude-opus-4-6",
        "max_tokens": 34000,
        "thinking": {"type": "enabled", "budget_tokens": 32768},
    }
    assert main_mod._apply_request_scoped_fallback_max_tokens(
        payload_tight, "pix4k", {"fallback_max_tokens": 16384}
    ) is None
    assert payload_tight["max_tokens"] == 34000


def test_fallback_max_tokens_never_raises_smaller_model_and_skips_excluded_providers():
    small = {"model": "fast", "max_tokens": 8192}
    assert main_mod._apply_request_scoped_fallback_max_tokens(
        small, "pix4k", {"fallback_max_tokens": 16384}
    ) is None
    assert small["max_tokens"] == 8192

    for excl in ("antigravity", "gemini-cli", "codex", "bslforge", "chat2api"):
        p = {"model": "m", "max_tokens": 65535}
        assert main_mod._apply_request_scoped_fallback_max_tokens(
            p, excl, {"fallback_max_tokens": 16384}
        ) is None
        assert p["max_tokens"] == 65535


def test_fallback_max_tokens_updates_native_kiro_inference_config():
    kiro_body = {
        "inferenceConfig": {
            "maxTokens": 65535,
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": "max"},
        }
    }
    applied = main_mod._apply_request_scoped_fallback_max_tokens(
        kiro_body, "kiro", {"fallback_max_tokens": 16384}
    )
    assert applied == 16384
    assert kiro_body["inferenceConfig"]["maxTokens"] == 16384


def test_combo_restart_arms_fallback_max_tokens_only_on_outage():
    _, wrapped_cf = main_mod._combo_restart_or_give_up(
        "Claude-Opus-5.5",
        None,
        [("claude-opus-5.5", "pix4k", "max")],
        "Claude-Opus-5.5",
        None,
        None,
        reason="gemini_chain_exhausted",
        status_code=503,
        err_text="The upstream provider returned no answer for this request. Please retry.",
    )
    assert wrapped_cf["fallback_max_tokens"] == 16384

    _, wrapped_429 = main_mod._combo_restart_or_give_up(
        "Claude-Opus-5.5",
        None,
        [("claude-opus-5.5", "pix4k", "max")],
        "Claude-Opus-5.5",
        None,
        None,
        reason="gemini_chain_exhausted",
        status_code=429,
        err_text="rate limit exceeded",
    )
    assert "fallback_max_tokens" not in wrapped_429


# ---------------------------------------------------------------------------
# 3. Post-content_block_stop ping-tail finalizer in StreamNormalizer
# ---------------------------------------------------------------------------

def _sse(event: str, payload: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n".encode("utf-8")


async def _collect(agen):
    out = []
    async for item in agen:
        out.append(item)
    return out


def test_stream_normalizer_closes_post_text_block_ping_tail():
    async def fake_stream():
        # Include realistic Anthropic placeholder output_tokens=1 + cache_read_input_tokens
        yield _sse("message_start", {
            "type": "message_start",
            "message": {"id": "m1", "usage": {"input_tokens": 10, "cache_read_input_tokens": 100, "output_tokens": 1}},
        })
        yield _sse("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}})
        yield _sse("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "plan reasoning text"}})
        yield _sse("content_block_stop", {"type": "content_block_stop", "index": 0})
        yield _sse("content_block_start", {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}})
        yield _sse("content_block_delta", {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Hello world delivered body"}})
        yield _sse("content_block_stop", {"type": "content_block_stop", "index": 1})
        # Simulate kiro.pix4k.com hanging on pings without message_delta/message_stop
        for _ in range(20):
            await asyncio.sleep(0.02)
            yield _sse("ping", {"type": "ping"})

    norm = StreamNormalizer("anthropic_sse", "openai_sse", model_name="claude-opus-5.5", post_block_ping_idle_s=0.05)
    t0 = time.monotonic()
    chunks = asyncio.run(_collect(norm.convert_anthropic_to_openai(fake_stream())))
    elapsed = time.monotonic() - t0
    joined = b"".join(chunks).decode("utf-8")
    assert "Hello world delivered body" in joined
    assert '"finish_reason": "stop"' in joined or '"finish_reason":"stop"' in joined
    # Must preserve message_start prompt/cache tokens AND estimated completion_tokens (>1)
    assert '"prompt_tokens": 110' in joined or '"prompt_tokens":110' in joined
    assert '"cached_tokens": 100' in joined or '"cached_tokens":100' in joined
    assert '"completion_tokens": 11' in joined or '"completion_tokens":11' in joined
    assert "data: [DONE]" in joined
    assert elapsed < 0.35  # terminated well before all 20 pings (~0.40s)


def test_stream_normalizer_closes_post_tool_use_block_with_tool_calls_finish():
    async def fake_stream():
        yield _sse("message_start", {"type": "message_start", "message": {"id": "m2"}})
        yield _sse("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "call_1", "name": "read_file", "input": {}}})
        yield _sse("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"path":"a.py"}'}})
        yield _sse("content_block_stop", {"type": "content_block_stop", "index": 0})
        for _ in range(10):
            await asyncio.sleep(0.02)
            yield _sse("ping", {"type": "ping"})

    norm = StreamNormalizer("anthropic_sse", "openai_sse", model_name="claude-opus-5.5", post_block_ping_idle_s=0.04)
    chunks = asyncio.run(_collect(norm.convert_anthropic_to_openai(fake_stream())))
    joined = b"".join(chunks).decode("utf-8")
    assert "read_file" in joined
    assert '"finish_reason": "tool_calls"' in joined or '"finish_reason":"tool_calls"' in joined
    assert "data: [DONE]" in joined


def test_stream_normalizer_does_not_close_after_thinking_only_stop():
    async def fake_stream():
        yield _sse("message_start", {"type": "message_start", "message": {"id": "m3"}})
        yield _sse("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}})
        yield _sse("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "deep thought"}})
        yield _sse("content_block_stop", {"type": "content_block_stop", "index": 0})
        # Pings between thinking stop and text start MUST NOT terminate the stream!
        for _ in range(4):
            await asyncio.sleep(0.02)
            yield _sse("ping", {"type": "ping"})
        yield _sse("content_block_start", {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}})
        yield _sse("content_block_delta", {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Final answer"}})
        yield _sse("content_block_stop", {"type": "content_block_stop", "index": 1})
        yield _sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 12}})
        yield _sse("message_stop", {"type": "message_stop"})

    norm = StreamNormalizer("anthropic_sse", "openai_sse", model_name="claude-opus-5.5", post_block_ping_idle_s=0.04)
    chunks = asyncio.run(_collect(norm.convert_anthropic_to_openai(fake_stream())))
    joined = b"".join(chunks).decode("utf-8")
    assert "Final answer" in joined


# ---------------------------------------------------------------------------
# 4. 1-Key Kiro serialization gate
# ---------------------------------------------------------------------------

def test_should_serialize_kiro_single_key_only_for_1_key_kiro_targets():
    cfg_1k = {
        "providers": {
            "pix4k": {
                "format": "anthropic",
                "connections": [
                    {"name": "500cr", "base_url": "https://kiro.pix4k.com", "api_key": "k1", "enabled": True},
                    {"name": "Free", "base_url": "https://kiro.pix4k.com", "api_key": "k2", "enabled": False},
                ],
                "models": [{"id": "claude-opus-5.5", "enabled": True}],
            },
            "openai": {
                "format": "openai",
                "connections": [{"name": "c1", "base_url": "https://api.openai.com/v1", "api_key": "k", "enabled": True}],
                "models": [{"id": "gpt-4o", "enabled": True}],
            },
        },
        "tools": {},
    }
    assert main_mod._should_serialize_kiro_single_key(cfg_1k, "pix4k", "claude-opus-5.5") is True
    assert main_mod._should_serialize_kiro_single_key(cfg_1k, "openai", "gpt-4o") is False
    assert main_mod._should_serialize_kiro_single_key(cfg_1k, "bslforge", "kiro-free/glm-5.3-free") is False

    # Enabling a second key disables single-key serialization
    cfg_1k["providers"]["pix4k"]["connections"][1]["enabled"] = True
    assert main_mod._should_serialize_kiro_single_key(cfg_1k, "pix4k", "claude-opus-5.5") is False


# ---------------------------------------------------------------------------
# 5. Studio bridge shields for Claude-Opus-5.5 + kwargs passthrough
# ---------------------------------------------------------------------------

def test_studio_bridge_covers_claude_opus_55_and_forwards_outage_kwargs(monkeypatch):
    import os
    from app import config_state as _cs
    monkeypatch.setattr(_cs, "get_config", lambda: _kiro_config(1))
    studio_root = Path(os.environ.get("BSL_STUDIO_SETUP_ROOT") or (Path(__file__).resolve().parents[3] / "Studio Setup"))
    bridge_path = studio_root / "packages" / "skills-hub" / "hooks" / "bsl-studio-runtime-bridge.py"
    if not bridge_path.is_file():
        pytest.skip("Studio bridge file not present on this machine")
    spec = importlib.util.spec_from_file_location("bsl_studio_bridge_test", bridge_path)
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)

    assert bridge._is_single_key_kiro_opus_model("Claude-Opus-5.5") is True
    assert bridge._is_single_key_kiro_opus_model("custom-claude-opus-5-5") is True

    # Patch a fake combo_resolver module to test 1-leaf chain no-double-tap
    fake_cr = SimpleNamespace(
        resolve_combo=lambda *a, **kw: (None, None, None),
        expand_chain_for_retries=lambda ch, cfg, min_passes=1, max_attempts=24: (list(ch) * min_passes, min_passes),
        count_eligible_keys=lambda cfg, p, m: 1,
    )
    bridge.patch_combo_resolver_module(fake_cr)
    expanded, passes = fake_cr.expand_chain_for_retries(
        [("claude-opus-5.5", "pix4k", "max")], {"providers": {}}, min_passes=2
    )
    assert expanded == [("claude-opus-5.5", "pix4k", "max")]
    assert passes == 1

    # Test against REAL main_mod._combo_restart_or_give_up (zero mock-of-a-mock)
    wrap_calls = []

    def real_like_raise_wrap(model, cfg, req, rs, ch, orig, cbp, sc, err, reason):
        wrap_calls.append((model, (rs or {}).get("pass_no", 1)))
        return "WRAPPED"

    fake_main = SimpleNamespace(
        GEMINI_THOUGHT_BUFFER_CAP_BYTES=256 * 1024,
        _combo_restart_or_give_up=main_mod._combo_restart_or_give_up,
        _raise_combo_wrap=real_like_raise_wrap,
    )
    bridge.apply_studio_opus_kiro_transport_shields(fake_main)
    backoff, wrapped = fake_main._combo_restart_or_give_up(
        "Claude-Opus-5.5",
        None,
        [("claude-opus-5.5", "pix4k", "max"), ("claude-opus-5.5", "pix4k", "max")],
        "Claude-Opus-5.5",
        None,
        None,
        "gemini_chain_exhausted",
        status_code=503,
        err_text="The upstream provider returned no answer for this request. Please retry.",
    )
    assert backoff >= 6.0
    assert wrapped["fallback_max_tokens"] == 16384
    assert wrapped["pass_no"] == 2
    assert len(wrapped["chain"]) == 1  # deduplicated

    # Non-outage 403 must NOT arm fallback_max_tokens even through Studio bridge
    _, wrapped_403 = fake_main._combo_restart_or_give_up(
        "Claude-Opus-5.5",
        None,
        [("claude-opus-5.5", "pix4k", "max")],
        "Claude-Opus-5.5",
        None,
        None,
        "gemini_chain_exhausted",
        status_code=403,
        err_text="invalid api key",
    )
    assert "fallback_max_tokens" not in wrapped_403

    # Pass 1 wraps; Pass 2 stops infinite loop cleanly on 1-key Kiro chains,
    # while mixed combos containing a non-Kiro fallback leaf are NOT prematurely halted!
    assert fake_main._raise_combo_wrap(
        "Claude-Opus-5.5", _kiro_config(1), None, {"pass_no": 1}, [("claude-opus-5.5", "pix4k", "max")],
        "Claude-Opus-5.5", None, 503, "no answer", "gemini_chain_exhausted"
    ) == "WRAPPED"
    assert fake_main._raise_combo_wrap(
        "Claude-Opus-5.5", _kiro_config(1), None, {"pass_no": 2}, [("claude-opus-5.5", "pix4k", "max")],
        "Claude-Opus-5.5", None, 503, "no answer", "gemini_chain_exhausted"
    ) is None
    assert fake_main._raise_combo_wrap(
        "Claude-Opus-5.5", _kiro_config(1), None, {"pass_no": 2},
        [("claude-opus-5.5", "pix4k", "max"), ("gpt-5", "openai", "high")],
        "Claude-Opus-5.5", None, 503, "no answer", "gemini_chain_exhausted"
    ) == "WRAPPED"

    # Without explicit conversation_id header, scope_key must be "" (never global 'default' bucket)
    assert bridge._resolve_kiro_stream_scope_key({"model": "Claude-Opus-5.5"}, None) == ""
    assert bridge._resolve_kiro_stream_scope_key({"model": "Claude-Opus-5.5", "conversation_id": "conv-42"}, None) == "fable-opus:conv-42"


def _kiro_config(num_keys: int = 1) -> dict:
    return {
        "tools": {"output_thinking_squeeze": False},
        "providers": {
            "pix4k": {
                "type": "custom",
                "format": "anthropic",
                "connections": [
                    {
                        "name": f"key-{i}",
                        "enabled": True,
                        "api_key": f"sk-test-{i}",
                        "base_url": "https://kiro.pix4k.com",
                    }
                    for i in range(num_keys)
                ],
                "models": [
                    {
                        "id": "claude-opus-5.5",
                        "enabled": True,
                        "thinking": "max",
                        "max_output_tokens": 65535,
                    }
                ],
            },
            "openai": {
                "type": "custom",
                "format": "openai",
                "connections": [
                    {
                        "name": "oa-0",
                        "enabled": True,
                        "api_key": "sk-oa",
                        "base_url": "https://api.openai.com/v1",
                    }
                ],
                "models": [{"id": "gpt-5", "enabled": True}],
            },
        },
        "aliases": {},
        "combos": [],
    }


def test_bounded_drain_cleanup_never_hangs_on_wedged_socket():
    """_bounded_drain_cleanup must finish within timeout_s even when drain_task,
    source.aclose(), and resp.aclose() all hang inside shielded cleanup."""

    async def _run():
        release = asyncio.Event()

        class _HungClose:
            def __init__(self):
                self.called = False

            async def aclose(self):
                self.called = True
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    await release.wait()

        async def _stuck_drain():
            try:
                await release.wait()
            except asyncio.CancelledError:
                # Simulate shielded httpcore cleanup ignoring cancellation
                await release.wait()

        t = asyncio.ensure_future(_stuck_drain())
        await asyncio.sleep(0.01)
        src = _HungClose()
        resp = _HungClose()
        try:
            t0 = time.monotonic()
            closed = await main_mod._bounded_drain_cleanup(
                t, src, resp, force_close_resp=True, timeout_s=0.05
            )
            elapsed = time.monotonic() - t0
        finally:
            # Always release background mocks so asyncio.run teardown never hangs
            release.set()
            await asyncio.sleep(0.02)
        assert closed is True
        assert resp.called is True
        # When drain_task hangs past timeout_s, source_obj.aclose() MUST be skipped
        # to avoid concurrent-aclose RuntimeError on an active async generator.
        assert src.called is False
        assert elapsed < 0.6

        # When drain_task finishes cleanly before timeout, BOTH source & resp close.
        t_clean = asyncio.ensure_future(asyncio.sleep(0))
        await asyncio.sleep(0.01)
        src2 = _HungClose()
        resp2 = _HungClose()
        closed2 = await main_mod._bounded_drain_cleanup(
            t_clean, src2, resp2, force_close_resp=True, timeout_s=0.05
        )
        release.set()
        assert closed2 is True
        assert src2.called is True
        assert resp2.called is True

    asyncio.run(_run())


def test_kiro_pre_content_idle_seconds_scales_with_prompt_size():
    cfg = _kiro_config(num_keys=1)
    assert main_mod._kiro_pre_content_idle_seconds(cfg, "pix4k", "claude-opus-5.5", prompt_chars=1000) == main_mod.KIRO_PRE_CONTENT_PING_IDLE_S
    assert main_mod._kiro_pre_content_idle_seconds(cfg, "pix4k", "claude-opus-5.5", prompt_chars=250_000) == main_mod.KIRO_PRE_CONTENT_PING_IDLE_LARGE_S
    assert main_mod._kiro_pre_content_idle_seconds(cfg, "openai", "gpt-5", prompt_chars=1000) == 0.0


def test_direct_single_leaf_outage_retry_is_strictly_bounded_to_one_pass():
    cfg = _kiro_config(num_keys=1)
    cfg["combos"] = []
    # Canonical BSL Router active_chain tuple order: (target_model, provider_name, thinking_override)
    chain = [("claude-opus-5.5", "pix4k", None)]

    # Pass 1 outage on direct provider/model -> eligible for 1 bounded 16384 retry
    assert main_mod._is_direct_single_leaf_outage_retry_eligible(
        "pix4k/claude-opus-5.5", chain, cfg, None, 524, "Cloudflare 524 timeout"
    ) is True
    assert main_mod._is_direct_single_leaf_outage_retry_eligible(
        "pix4k/claude-opus-5.5", chain, cfg, {"pass_no": 1}, 503,
        "The upstream provider returned no answer for this request. Please retry."
    ) is True

    # Pass 2 or already-armed fallback -> MUST NOT loop
    assert main_mod._is_direct_single_leaf_outage_retry_eligible(
        "pix4k/claude-opus-5.5", chain, cfg, {"pass_no": 2}, 524, "Cloudflare 524 timeout"
    ) is False
    assert main_mod._is_direct_single_leaf_outage_retry_eligible(
        "pix4k/claude-opus-5.5", chain, cfg, {"pass_no": 1, "fallback_max_tokens": 16384}, 524, "Cloudflare 524"
    ) is False

    # Non-outage 4xx or excluded providers (with canonical (model, provider, thinking) tuples) -> MUST NOT retry
    assert main_mod._is_direct_single_leaf_outage_retry_eligible(
        "pix4k/claude-opus-5.5", chain, cfg, None, 400, "bad request"
    ) is False
    assert main_mod._is_direct_single_leaf_outage_retry_eligible(
        "pix4k/claude-opus-5.5", chain, cfg, None, 403, "invalid api key"
    ) is False
    assert main_mod._is_direct_single_leaf_outage_retry_eligible(
        "antigravity/gemini-4-argon", [("gemini-4-argon", "antigravity", None)], cfg, None, 524, "timeout"
    ) is False
    assert main_mod._is_direct_single_leaf_outage_retry_eligible(
        "bslforge/qwen3.8", [("qwen3.8", "bslforge", None)], cfg, None, 504, "timeout"
    ) is False


def test_gemini_egress_ping_only_zombie_wraps_to_16384_and_resets_next_call(monkeypatch):
    """End-to-end mocked Gemini egress regression test:
    - Request 1 Pass 1 sends max_tokens=65535 + thinking:adaptive/effort:max,
      receives HTTP 200 + message_start + endless `event: ping`s (zero content).
    - Pre-content idle cap fires rapidly without hanging in `finally:`,
      arms ephemeral fallback_max_tokens=16384 for Pass 2, and succeeds on Pass 2.
    - Request 2 (fresh call) automatically resets back to max_tokens=65535!
    """
    import httpx
    import app.config_state as cs

    cfg = _kiro_config(num_keys=1)
    cfg["settings"] = {"combo_infinite_retry": True}
    cfg["combos"] = [{
        "alias": "Claude-Opus-5.5",
        "chain": [{"provider": "pix4k", "model": "claude-opus-5.5"}],
        "thinking": "max",
    }]

    sent_payloads = []
    req_phase = {"force_outage_on_65k": True}

    class _PingZombieStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'event: message_start\ndata: {"type":"message_start","message":{"id":"m_z","role":"assistant","content":[],"usage":{"input_tokens":0,"output_tokens":0}}}\n\n'
            while True:
                yield b'event: ping\ndata: {"type":"ping"}\n\n'
                await asyncio.sleep(0.05)

        async def aclose(self):
            pass

    class _HealthyAnthropicStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'event: message_start\ndata: {"type":"message_start","message":{"id":"m_ok","role":"assistant","content":[],"usage":{"input_tokens":10,"output_tokens":0}}}\n\n'
            yield b'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"thinking","thinking":""}}\n\n'
            yield b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"deep reasoning"}}\n\n'
            yield b'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n'
            yield b'event: content_block_start\ndata: {"type":"content_block_start","index":1,"content_block":{"type":"text","text":""}}\n\n'
            yield b'event: content_block_delta\ndata: {"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"OK_PASS2_ZERO_LOSS"}}\n\n'
            yield b'event: content_block_stop\ndata: {"type":"content_block_stop","index":1}\n\n'
            yield b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":12}}\n\n'
            yield b'event: message_stop\ndata: {"type":"message_stop"}\n\n'

        async def aclose(self):
            pass

    class _MockClient:
        def build_request(self, method, url, **kwargs):
            return httpx.Request(method, url, **kwargs)

        async def send(self, req, stream=False):
            payload = json.loads(req.content.decode("utf-8"))
            sent_payloads.append(payload)
            if req_phase["force_outage_on_65k"] and int(payload.get("max_tokens") or 0) > 16384:
                return httpx.Response(200, request=req, stream=_PingZombieStream())
            return httpx.Response(200, request=req, stream=_HealthyAnthropicStream())

    orig_cfg = cs.get_config()
    try:
        cs.replace_config(cfg)
        monkeypatch.setattr(main_mod, "_get_client_for_proxy", lambda *_a, **_kw: _MockClient())
        monkeypatch.setattr(main_mod, "get_breaker", lambda: None)
        monkeypatch.setattr(main_mod.obs, "log_request_start", lambda **kw: "req-test")
        monkeypatch.setattr(main_mod.obs, "log_request", lambda **kw: None)
        monkeypatch.setattr(main_mod, "KIRO_PRE_CONTENT_PING_IDLE_S", 0.15)
        monkeypatch.setattr(main_mod, "GEMINI_EGRESS_KEEPALIVE_INTERVAL", 0.05)
        monkeypatch.setattr(main_mod, "DRAIN_CLEANUP_TIMEOUT_S", 0.2)

        async def _fast_sleep(_sec, _req=None):
            return False

        monkeypatch.setattr(main_mod, "_sleep_or_disconnect", _fast_sleep)

        async def _run():
            # Request 1: Pass 1 hits ping-zombie at 65535 -> Pass 2 recovers at 16384 with thinking:max intact
            body1 = {
                "model": "Claude-Opus-5.5",
                "messages": [{"role": "user", "content": "Say OK"}],
                "max_tokens": 65535,
                "stream": True,
            }
            resp1 = await main_mod._process_chat_completion(body1, client_wants_anthropic=False, client_wants_gemini=True)
            chunks1 = []
            async for ch in resp1.body_iterator:
                chunks1.append(ch.decode("utf-8", "replace") if isinstance(ch, (bytes, bytearray)) else str(ch))
            joined1 = "".join(chunks1)
            assert "OK_PASS2_ZERO_LOSS" in joined1
            assert sent_payloads[0]["max_tokens"] == 65535
            assert sent_payloads[-1]["max_tokens"] == 16384
            # Full thinking: max preserved on Pass 2!
            assert sent_payloads[-1].get("output_config", {}).get("effort") == "max"

            # Request 2 (fresh request after outage clears): resets back to 65535 automatically!
            req_phase["force_outage_on_65k"] = False
            before_req2 = len(sent_payloads)
            body2 = {
                "model": "Claude-Opus-5.5",
                "messages": [{"role": "user", "content": "Fresh call"}],
                "max_tokens": 65535,
                "stream": True,
            }
            resp2 = await main_mod._process_chat_completion(body2, client_wants_anthropic=False, client_wants_gemini=True)
            chunks2 = []
            async for ch in resp2.body_iterator:
                chunks2.append(ch.decode("utf-8", "replace") if isinstance(ch, (bytes, bytearray)) else str(ch))
            assert "OK_PASS2_ZERO_LOSS" in "".join(chunks2)
            assert len(sent_payloads) == before_req2 + 1
            assert sent_payloads[-1]["max_tokens"] == 65535

        asyncio.run(_run())
    finally:
        cs.replace_config(orig_cfg)


# ---------------------------------------------------------------------------
# 6. Triple Adversarial Crucible Regression Suite
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "status,err,expected",
    [
        (523, "Origin is unreachable", True),
        (525, "SSL handshake failed", True),
        (526, "Invalid SSL certificate", True),
        (529, "OverloadedError: Anthropic overloaded", True),
        (530, "Origin DNS error", True),
        (500, "The upstream provider returned no answer for this request", True),
        (500, "Internal validation error: invalid syntax in tool schema", False),
        (502, "", True),
        (503, '{"error":{"message":"Service Unavailable","type":"api_error"}}', True),
        (400, "Failed to install package dependency during tool validation", False),  # 'stall' in 'install' FP check!
        (500, "Failed to install wheel", False),
        (502, "model_not_found: claude-opus-99 does not exist", False),
    ],
)
def test_crucible_outage_trigger_edge_cases_and_no_install_false_positive(status, err, expected):
    assert main_mod._is_outage_max_tokens_fallback_trigger(status, err) is expected


def test_crucible_usage_from_sse_data_strips_message_start_placeholder_output_tokens():
    msg_start = {
        "type": "message_start",
        "message": {
            "id": "msg_01",
            "usage": {
                "input_tokens": 25,
                "cache_read_input_tokens": 1400,
                "cache_creation_input_tokens": 300,
                "output_tokens": 1,
            },
        },
    }
    u_start = main_mod._usage_from_sse_data(msg_start)
    assert u_start is not None
    assert "output_tokens" not in u_start
    assert u_start["input_tokens"] == 25
    assert u_start["cache_read_input_tokens"] == 1400

    stats = {"ttft": 1.2, "in": 0, "out": 0, "cached": 0, "cache_write": 0, "error": None, "status": 200}
    main_mod._merge_usage_into_stats(stats, u_start)
    assert stats["out"] == 0  # MUST stay 0 so _backfill_missing_usage & 499 checks remain armed!
    assert stats["in"] == 1725
    assert stats["cached"] == 1400
    assert stats["cache_write"] == 300

    # Real message_delta usage still updates stats["out"] normally
    msg_delta = {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 284}}
    u_delta = main_mod._usage_from_sse_data(msg_delta)
    main_mod._merge_usage_into_stats(stats, u_delta)
    assert stats["out"] == 284


def test_crucible_accumulate_sse_stream_backfills_real_tokens_despite_message_start_one():
    import httpx

    class _PingTailBufferedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield _sse("message_start", {
                "type": "message_start",
                "message": {"id": "m_buf", "usage": {"input_tokens": 50, "output_tokens": 1}},
            })
            yield _sse("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
            yield _sse("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "A" * 120}})
            yield _sse("content_block_stop", {"type": "content_block_stop", "index": 0})
            for _ in range(10):
                await asyncio.sleep(0.02)
                yield _sse("ping", {"type": "ping"})

        async def aclose(self):
            pass

    req = httpx.Request("POST", "https://kiro.pix4k.com/v1/messages")
    resp = httpx.Response(200, request=req, stream=_PingTailBufferedStream())
    assembled = asyncio.run(
        main_mod._accumulate_sse_stream(
            resp,
            _is_anthropic_fmt=True,
            _target_model="claude-opus-5.5",
            _post_block_ping_idle_s=0.05,
        )
    )
    assert assembled["choices"][0]["message"]["content"] == "A" * 120
    assert assembled["usage"]["prompt_tokens"] == 50
    assert assembled["usage"]["completion_tokens"] >= 30  # never stuck at placeholder 1!


def test_crucible_stream_normalizer_protects_delayed_tool_use_and_handles_silent_tcp_and_error():
    # 1. tools_in_request=True MUST NOT cut off preamble text after 0.04s when tool_use follows at 0.09s
    async def preamble_then_tool():
        yield _sse("message_start", {"type": "message_start", "message": {"id": "m_t"}})
        yield _sse("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
        yield _sse("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Let me inspect main.py."}})
        yield _sse("content_block_stop", {"type": "content_block_stop", "index": 0})
        await asyncio.sleep(0.09)
        yield _sse("content_block_start", {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "call_9", "name": "read_file", "input": {}}})
        yield _sse("content_block_delta", {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"path":"main.py"}'}})
        yield _sse("content_block_stop", {"type": "content_block_stop", "index": 1})
        yield _sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 25}})
        yield _sse("message_stop", {"type": "message_stop"})

    norm_tools = StreamNormalizer(
        "anthropic_sse", "openai_sse",
        model_name="claude-opus-5.5",
        tools_in_request=True,
        post_block_ping_idle_s=0.04,
    )
    joined_tools = b"".join(asyncio.run(_collect(norm_tools.convert_anthropic_to_openai(preamble_then_tool())))).decode("utf-8")
    assert "read_file" in joined_tools
    assert '"finish_reason": "tool_calls"' in joined_tools or '"finish_reason":"tool_calls"' in joined_tools

    # 2. Silent TCP hang (zero ping frames after content_block_stop) still finalizes via read timeout!
    async def silent_tcp_tail():
        yield _sse("message_start", {"type": "message_start", "message": {"id": "m_sil", "usage": {"input_tokens": 8, "output_tokens": 1}}})
        yield _sse("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
        yield _sse("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Complete answer before TCP silence"}})
        yield _sse("content_block_stop", {"type": "content_block_stop", "index": 0})
        await asyncio.sleep(5.0)

    norm_sil = StreamNormalizer("anthropic_sse", "openai_sse", model_name="claude-opus-5.5", post_block_ping_idle_s=0.06)
    t0 = time.monotonic()
    joined_sil = b"".join(asyncio.run(_collect(norm_sil.convert_anthropic_to_openai(silent_tcp_tail())))).decode("utf-8")
    assert time.monotonic() - t0 < 0.8
    assert "Complete answer before TCP silence" in joined_sil
    assert "data: [DONE]" in joined_sil

    # 3. Mid-stream `event: error` surfaces error chunk and NEVER fabricates finish_reason="stop"
    async def midstream_error():
        yield _sse("message_start", {"type": "message_start", "message": {"id": "m_err"}})
        yield _sse("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
        yield _sse("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Partial"}})
        yield _sse("content_block_stop", {"type": "content_block_stop", "index": 0})
        yield _sse("error", {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded mid-turn"}})

    norm_err = StreamNormalizer("anthropic_sse", "openai_sse", model_name="claude-opus-5.5", post_block_ping_idle_s=0.05)
    joined_err = b"".join(asyncio.run(_collect(norm_err.convert_anthropic_to_openai(midstream_error())))).decode("utf-8")
    assert "Overloaded mid-turn" in joined_err
    assert '"finish_reason": "stop"' not in joined_err and '"finish_reason":"stop"' not in joined_err


def test_crucible_fallback_max_tokens_all_5_dialects_and_64k_budget_clamp():
    rs = {"fallback_max_tokens": 16384}

    p_mct = {"max_completion_tokens": "65535.0"}
    assert main_mod._apply_request_scoped_fallback_max_tokens(p_mct, "openai", rs) == 16384
    assert p_mct["max_completion_tokens"] == 16384

    p_resp = {"max_output_tokens": 65535}
    assert main_mod._apply_request_scoped_fallback_max_tokens(p_resp, "openrouter", rs) == 16384
    assert p_resp["max_output_tokens"] == 16384

    p_gem = {"generationConfig": {"maxOutputTokens": 65535}}
    assert main_mod._apply_request_scoped_fallback_max_tokens(p_gem, "vertex", rs) == 16384
    assert p_gem["generationConfig"]["maxOutputTokens"] == 16384

    # 64K (65536) enabled budget_tokens with max_tokens=65535 clamps budget <= 61439
    # so max_tokens > budget_tokens without reducing max_tokens below 65535
    p_64k = {"max_tokens": 65535, "thinking": {"type": "enabled", "budget_tokens": 65536}}
    applied = main_mod._apply_request_scoped_fallback_max_tokens(p_64k, "pix4k", rs)
    assert applied is None
    assert p_64k["thinking"]["budget_tokens"] == 61439
    assert p_64k["max_tokens"] > p_64k["thinking"]["budget_tokens"]


def test_crucible_estimate_payload_prompt_chars_counts_tools_and_blocks():
    payload = {
        "system": [{"type": "text", "text": "S" * 20_000}],
        "messages": [
            {"role": "user", "content": [{"type": "tool_result", "content": [{"type": "text", "text": "R" * 90_000}]}]},
        ],
        "tools": [{"name": "big_tool", "description": "D" * 100_000, "input_schema": {"type": "object"}}],
    }
    chars = main_mod._estimate_payload_prompt_chars(payload)
    assert chars >= 210_000


def test_crucible_two_leaf_kiro_combo_preserves_pass2_state_and_terminates_cleanly(monkeypatch):
    """2-leaf all-1-key-Kiro combo (e.g. Fable-Opus: pix4k + nguyenha):
    - Pass 1 tries leaf 0 (65535) -> 503, leaf 1 (65535) -> 503
    - Wraps to Pass 2 with fallback_max_tokens=16384 & pass_no=2
    - Leaf 0 on Pass 2 receives 16384 -> 503, advances to Leaf 1 WITH pass_no=2 & 16384 preserved!
    - Leaf 1 on Pass 2 receives 16384 -> 503 -> terminates cleanly after 4 total upstream calls (zero infinite loop!).
    """
    import httpx
    import app.config_state as cs

    cfg = _kiro_config(num_keys=1)
    cfg["providers"]["nguyenha"] = {
        "type": "custom",
        "format": "openai",
        "connections": [{"name": "nh-0", "enabled": True, "api_key": "sk-nh", "base_url": "https://api.nguyenhaxps.com/v1"}],
        "models": [{"id": "claude-opus-4.6", "enabled": True, "thinking": "max", "max_output_tokens": 65535}],
    }
    cfg["settings"] = {"combo_infinite_retry": True}
    cfg["combos"] = [{
        "alias": "Fable-Opus",
        "chain": [
            {"provider": "pix4k", "model": "claude-opus-5.5"},
            {"provider": "nguyenha", "model": "claude-opus-4.6"},
        ],
        "thinking": "max",
    }]

    calls = []

    class _Always503Client:
        def build_request(self, method, url, **kwargs):
            return httpx.Request(method, url, **kwargs)

        async def send(self, req, stream=False):
            p = json.loads(req.content.decode("utf-8"))
            calls.append((str(req.url), p.get("max_tokens")))
            return httpx.Response(
                503,
                request=req,
                content=b'{"error":{"message":"The upstream provider returned no answer for this request."}}',
            )

    orig_cfg = cs.get_config()
    try:
        cs.replace_config(cfg)
        monkeypatch.setattr(main_mod, "_get_client_for_proxy", lambda *_a, **_kw: _Always503Client())
        monkeypatch.setattr(main_mod, "get_breaker", lambda: None)
        monkeypatch.setattr(main_mod.obs, "log_request_start", lambda **kw: "req-fable")
        monkeypatch.setattr(main_mod.obs, "log_request", lambda **kw: None)

        async def _fast_sleep(_sec, _req=None):
            return False

        monkeypatch.setattr(main_mod, "_sleep_or_disconnect", _fast_sleep)

        async def _run():
            body = {
                "model": "Fable-Opus",
                "messages": [{"role": "user", "content": "Test 2-leaf pass propagation"}],
                "max_tokens": 65535,
                "stream": True,
            }
            resp = await main_mod._process_chat_completion(body, client_wants_anthropic=False, client_wants_gemini=True)
            frames = []
            async for ch in resp.body_iterator:
                frames.append(ch.decode("utf-8", "replace") if isinstance(ch, (bytes, bytearray)) else str(ch))
            # expand_chain_for_retries expands 2-leaf chain -> 4 entries per pass;
            # Pass 1 tries 4 entries at 65535, Pass 2 tries 4 entries at 16384, then stops cleanly!
            assert len(calls) == 8
            assert [mt for _, mt in calls] == [65535, 65535, 65535, 65535, 16384, 16384, 16384, 16384]
            assert any("finishReason" in f for f in frames)

        asyncio.run(_run())
    finally:
        cs.replace_config(orig_cfg)


def test_crucible_nonstream_outage_wraps_to_16384_without_swallowing_sentinel(monkeypatch):
    """Non-stream (stream=False) direct single-leaf call:
    - Pass 1 fails with 503 outage -> raises _ComboWrapRestart cleanly (NOT caught by outer except Exception!)
    - Pass 2 retries with max_tokens=16384 and returns 200 JSON!
    """
    import httpx
    import app.config_state as cs

    cfg = _kiro_config(num_keys=1)
    cfg["upstream_stream_buffer"] = {"enabled": False}
    cfg["settings"] = {"combo_infinite_retry": True}
    sent_mts = []

    class _NonStreamClient:
        def build_request(self, method, url, **kwargs):
            return httpx.Request(method, url, **kwargs)

        async def send(self, req, stream=False):
            p = json.loads(req.content.decode("utf-8"))
            mt = int(p.get("max_tokens") or 0)
            sent_mts.append(mt)
            if mt > 16384:
                return httpx.Response(
                    503,
                    request=req,
                    content=b'{"error":{"message":"The upstream provider returned no answer for this request."}}',
                    headers={"content-type": "application/json"},
                )
            return httpx.Response(
                200,
                request=req,
                content=json.dumps({
                    "id": "msg_ns_ok",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-opus-5.5",
                    "content": [{"type": "text", "text": "NONSTREAM_16K_RECOVERED"}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 12, "output_tokens": 6},
                }).encode("utf-8"),
                headers={"content-type": "application/json"},
            )

    orig_cfg = cs.get_config()
    try:
        cs.replace_config(cfg)
        monkeypatch.setattr(main_mod, "_get_client_for_proxy", lambda *_a, **_kw: _NonStreamClient())
        monkeypatch.setattr(main_mod, "get_breaker", lambda: None)
        monkeypatch.setattr(main_mod.obs, "log_request_start", lambda **kw: "req-ns")
        monkeypatch.setattr(main_mod.obs, "log_request", lambda **kw: None)

        async def _fast_sleep(_sec, _req=None):
            return False

        monkeypatch.setattr(main_mod, "_sleep_or_disconnect", _fast_sleep)

        async def _run():
            body = {
                "model": "pix4k/claude-opus-5.5",
                "messages": [{"role": "user", "content": "Say OK"}],
                "max_tokens": 65535,
                "stream": False,
            }
            resp = await main_mod._process_chat_completion(body, client_wants_anthropic=False, client_wants_gemini=False)
            assert resp.status_code == 200
            data = json.loads(resp.body.decode("utf-8"))
            assert data["choices"][0]["message"]["content"] == "NONSTREAM_16K_RECOVERED"
            assert sent_mts == [65535, 16384]

        asyncio.run(_run())
    finally:
        cs.replace_config(orig_cfg)

