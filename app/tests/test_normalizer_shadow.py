"""N2 — normalizer_v2 shadow wiring tests (shadow_run + capped JSONL writer).

Covers the spec matrix:
  1. default disabled → None, zero to_canonical work, no file write
  2. enabled+shadow+clean openai-chat body → ok:true record, returns None
  3. enabled+shadow+lossy body (RT transforms an unknown part) → ok:false +
     non-empty mismatch_paths
  4. mode "active" → returns the rebuilt dict (still logs)
  5. fail-open: converter raising → None + error record, never raises
  6. invalid mode string → treated as disabled (one warn per process)
  7. rotation: tiny-cap rotator keeps the live file under cap + .1 exists
  8. endpoint_dialect mapping helper

Plus writer-internals: queue drop-on-full, enqueue-with-loop, direct-write
fallback, and the module-level isolation guard (tests must never touch the
real .brain/logs directory — same hole the capture-log conftest closes).

asyncio.run(scenario()) style — no pytest-asyncio plugin in this venv
(matches app/tests/test_capture_log_rotation.py).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import app.middleware.normalizer_shadow as nsh  # noqa: E402


# ── Isolation: every test gets a tmp log path + reset module globals ─────────

@pytest.fixture(autouse=True)
def _isolate_shadow_state(tmp_path, monkeypatch):
    """Redirect the shadow logger into tmp_path and reset ALL module globals
    (queue/writer/boot-heal/warn flags) so tests are order-independent and
    can never reach the real .brain/logs directory."""
    monkeypatch.setattr(nsh, "_SHADOW_LOG_PATH", str(tmp_path / "normalizer_shadow.jsonl"))
    monkeypatch.setattr(nsh, "_shadow_queue", None)
    monkeypatch.setattr(nsh, "_shadow_writer_task", None)
    monkeypatch.setattr(nsh, "_shadow_boot_healed", False)
    monkeypatch.setattr(nsh, "_invalid_mode_warned", False)
    yield


@pytest.fixture
def log_path():
    return nsh._SHADOW_LOG_PATH


@pytest.fixture
def direct_writer(monkeypatch):
    """Bypass the async queue so records land on disk synchronously inside
    asyncio.run — exercises _shadow_write_direct (rotation included) rather
    than asserting against an undrained queue."""
    monkeypatch.setattr(nsh, "_shadow_line", nsh._shadow_write_direct)


def _enabled_cfg(mode="shadow", dialects=None, log_mismatches=True):
    return {
        "tools": {
            "normalizer_v2": {
                "enabled": True,
                "mode": mode,
                "log_mismatches": log_mismatches,
                "dialects": dialects if dialects is not None else {
                    "openai-chat": True, "responses": True, "gemini": True},
            }
        }
    }


def _read_records(log_path):
    with open(log_path, "r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


CLEAN_OPENAI_BODY = {
    "model": "gpt-5.6-sol",
    "messages": [
        {"role": "system", "content": "You are terse."},
        {"role": "user", "content": [
            {"type": "text", "text": "describe this"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}},
        ]},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "look", "arguments": "{\"q\": \"cat\"}"}},
        ]},
        {"role": "tool", "tool_call_id": "call_1", "content": "a cat"},
    ],
}

# RT loss: openai-chat ingress has no mapping for an "emoji" part → canonical
# "unknown"; the egress re-renders it as a json.dumps text part → part type
# flips unknown→text on the rebuilt side (verified against normalizer_v2).
LOSSY_OPENAI_BODY = {
    "model": "gpt-5.6-sol",
    "messages": [
        {"role": "user", "content": [
            {"type": "text", "text": "hi"},
            {"type": "emoji", "emoji": "🎉"},
        ]},
    ],
}


# ── 1. Disabled gates ────────────────────────────────────────────────────────

def test_default_disabled_returns_none_without_work(log_path, monkeypatch):
    calls = {"n": 0}
    real = nsh.to_canonical

    def counting(dialect, body):
        calls["n"] += 1
        return real(dialect, body)

    monkeypatch.setattr(nsh, "to_canonical", counting)

    async def scenario():
        # config without the section entirely, and with enabled: False.
        for cfg in ({}, {"tools": {}}, {"tools": {"normalizer_v2": {}}},
                        {"tools": {"normalizer_v2": {"enabled": False}}}):
            result = await nsh.shadow_run(
                "openai-chat", CLEAN_OPENAI_BODY, endpoint="/v1/chat/completions",
                config=cfg, provider=None, model="gpt-5.6-sol")
            assert result is None
        return calls["n"]

    assert asyncio.run(scenario()) == 0
    assert not os.path.exists(log_path)


def test_dialect_flagged_off_returns_none_without_work(log_path, monkeypatch):
    calls = {"n": 0}
    real = nsh.to_canonical

    def counting(dialect, body):
        calls["n"] += 1
        return real(dialect, body)

    monkeypatch.setattr(nsh, "to_canonical", counting)

    async def scenario():
        return await nsh.shadow_run(
            "openai-chat", CLEAN_OPENAI_BODY, endpoint="/v1/chat/completions",
            config=_enabled_cfg(dialects={"openai-chat": False}),
            provider=None, model="gpt-5.6-sol")

    assert asyncio.run(scenario()) is None
    assert calls["n"] == 0
    assert not os.path.exists(log_path)


# ── 2. Shadow mode, clean body → ok:true record, body untouched ────────────

def test_shadow_clean_body_logs_ok_record(log_path, direct_writer):
    async def scenario():
        return await nsh.shadow_run(
            "openai-chat", CLEAN_OPENAI_BODY, endpoint="/v1/chat/completions",
            config=_enabled_cfg(), provider="prov-x", model="gpt-5.6-sol")

    assert asyncio.run(scenario()) is None  # shadow never swaps the body
    records = _read_records(log_path)
    assert len(records) == 1
    rec = records[0]
    assert rec["ok"] is True
    assert rec["mismatch_paths"] == []
    assert rec["endpoint"] == "/v1/chat/completions"
    assert rec["dialect"] == "openai-chat"
    assert rec["model"] == "gpt-5.6-sol"
    assert rec["provider"] == "prov-x"
    assert rec["body_bytes"] > 0
    assert "ts" in rec


def test_shadow_clean_gemini_and_responses_bodies(log_path, direct_writer):
    gemini_body = {
        "systemInstruction": {"parts": [{"text": "sys"}]},
        "contents": [{"role": "user", "parts": [
            {"text": "hi"},
            {"inlineData": {"mimeType": "image/png", "data": "QUJD"}},
        ]}],
    }
    responses_body = {
        "model": "gpt-5.6-sol",
        "instructions": "sys",
        "input": [{"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "hi"},
        ]}],
    }

    async def scenario():
        out = []
        out.append(await nsh.shadow_run(
            "gemini", gemini_body, endpoint="/v1beta/models/gem-2.5-pro:generateContent",
            config=_enabled_cfg(), provider=None, model="gem-2.5-pro"))
        out.append(await nsh.shadow_run(
            "responses", responses_body, endpoint="/v1/responses",
            config=_enabled_cfg(), provider=None, model="gpt-5.6-sol"))
        return out

    assert asyncio.run(scenario()) == [None, None]
    records = _read_records(log_path)
    assert [r["ok"] for r in records] == [True, True]
    assert [r["dialect"] for r in records] == ["gemini", "responses"]


# ── 3. Shadow mode, lossy body → ok:false + mismatch_paths ───────────────────

def test_shadow_lossy_body_flags_mismatch(log_path, direct_writer):
    async def scenario():
        return await nsh.shadow_run(
            "openai-chat", LOSSY_OPENAI_BODY, endpoint="/v1/chat/completions",
            config=_enabled_cfg(), provider=None, model="gpt-5.6-sol")

    assert asyncio.run(scenario()) is None  # shadow mode: still no swap
    rec = _read_records(log_path)[0]
    assert rec["ok"] is False
    assert len(rec["mismatch_paths"]) > 0
    # The unknown "emoji" part re-enters as a text part → type flip flagged.
    assert "/messages/0/parts/1/type" in rec["mismatch_paths"]


def test_shadow_log_mismatches_false_suppresses_ok_records(log_path, direct_writer):
    async def scenario():
        clean = await nsh.shadow_run(
            "openai-chat", CLEAN_OPENAI_BODY, endpoint="/v1/chat/completions",
            config=_enabled_cfg(log_mismatches=False), provider=None, model="m")
        lossy = await nsh.shadow_run(
            "openai-chat", LOSSY_OPENAI_BODY, endpoint="/v1/chat/completions",
            config=_enabled_cfg(log_mismatches=False), provider=None, model="m")
        return clean, lossy

    clean_ret, lossy_ret = asyncio.run(scenario())
    assert clean_ret is None and lossy_ret is None
    records = _read_records(log_path)
    assert len(records) == 1  # only the ok:false divergence record survives
    assert records[0]["ok"] is False


# ── 4. Active mode returns the rebuilt dict ──────────────────────────────────

def test_active_mode_returns_rebuilt_dict(log_path, direct_writer):
    async def scenario():
        return await nsh.shadow_run(
            "openai-chat", CLEAN_OPENAI_BODY, endpoint="/v1/chat/completions",
            config=_enabled_cfg(mode="active"), provider=None, model="gpt-5.6-sol")

    rebuilt = asyncio.run(scenario())
    assert isinstance(rebuilt, dict)
    # The swap candidate is the round-tripped shape, not the original object.
    assert rebuilt is not CLEAN_OPENAI_BODY
    assert rebuilt["messages"][2]["tool_call_id"] == "call_1"
    assert rebuilt["system"] == "You are terse."
    # And it is semantically clean per the module's own comparator.
    assert nsh.semantic_mismatches("openai-chat", CLEAN_OPENAI_BODY, rebuilt) == []
    # Still logged.
    assert _read_records(log_path)[0]["ok"] is True


def test_active_mode_never_swaps_a_lossy_or_non_dict_body(log_path, direct_writer):
    async def scenario():
        lossy = await nsh.shadow_run(
            "openai-chat", LOSSY_OPENAI_BODY, endpoint="/v1/chat/completions",
            config=_enabled_cfg(mode="active"), provider=None, model="m")
        non_dict = await nsh.shadow_run(
            "openai-chat", [1, 2, 3], endpoint="/v1/chat/completions",
            config=_enabled_cfg(mode="active"), provider=None, model="m")
        return lossy, non_dict

    assert asyncio.run(scenario()) == (None, None)
    records = _read_records(log_path)
    assert len(records) == 1  # lossy logged, non-dict skipped silently
    assert records[0]["ok"] is False


# ── 5. Fail-open on converter exceptions ─────────────────────────────────────

def test_fail_open_to_canonical_raises(log_path, direct_writer, monkeypatch):
    def boom(dialect, body):
        raise RuntimeError("boom-canonical")

    monkeypatch.setattr(nsh, "to_canonical", boom)

    async def scenario():
        return await nsh.shadow_run(
            "openai-chat", CLEAN_OPENAI_BODY, endpoint="/v1/chat/completions",
            config=_enabled_cfg(), provider=None, model="m")

    assert asyncio.run(scenario()) is None  # no raise, body would be kept
    rec = _read_records(log_path)[0]
    assert rec["ok"] is False
    assert "boom-canonical" in rec["error"]
    assert "RuntimeError" in rec["error"]
    assert len(rec["error"]) <= 200


def test_fail_open_from_canonical_raises(log_path, direct_writer, monkeypatch):
    def boom(dialect, canonical, ctx=None):
        raise ValueError("boom-egress")

    monkeypatch.setattr(nsh, "from_canonical", boom)

    async def scenario():
        return await nsh.shadow_run(
            "openai-chat", CLEAN_OPENAI_BODY, endpoint="/v1/chat/completions",
            config=_enabled_cfg(mode="active"), provider=None, model="m")

    assert asyncio.run(scenario()) is None  # active mode also fails open
    rec = _read_records(log_path)[0]
    assert rec["ok"] is False
    assert "boom-egress" in rec["error"]


def test_fail_open_writer_failure_swallowed(log_path, monkeypatch):
    # Even when the record writer explodes, shadow_run must return None (or the
    # active rebuilt dict) without raising — inference is never blocked.
    def boom_write(rec):
        raise OSError("disk on fire")

    monkeypatch.setattr(nsh, "_shadow_line", boom_write)

    async def scenario():
        return await nsh.shadow_run(
            "openai-chat", CLEAN_OPENAI_BODY, endpoint="/v1/chat/completions",
            config=_enabled_cfg(), provider=None, model="m")

    assert asyncio.run(scenario()) is None
    assert not os.path.exists(log_path)


# ── 6. Invalid mode → disabled, one warn per process ─────────────────────────

def test_invalid_mode_treated_as_disabled(log_path, monkeypatch, capsys):
    calls = {"n": 0}
    real = nsh.to_canonical

    def counting(dialect, body):
        calls["n"] += 1
        return real(dialect, body)

    monkeypatch.setattr(nsh, "to_canonical", counting)

    async def scenario():
        out = []
        for _ in range(3):
            out.append(await nsh.shadow_run(
                "openai-chat", CLEAN_OPENAI_BODY, endpoint="/v1/chat/completions",
                config=_enabled_cfg(mode="banana"), provider=None, model="m"))
        return out

    assert asyncio.run(scenario()) == [None, None, None]
    assert calls["n"] == 0  # disabled before any normalization work
    assert not os.path.exists(log_path)
    warned = [ln for ln in capsys.readouterr().out.splitlines()
              if "[NormalizerShadow]" in ln]
    assert len(warned) == 1  # exactly ONE warn per process, not per request
    assert "banana" in warned[0]


# ── 7. Rotation (tiny cap) ────────────────────────────────────────────────────

def test_rotation_small_cap(log_path, monkeypatch):
    monkeypatch.setattr(nsh, "_SHADOW_CAP_BYTES", 2048)

    rec = {"ts": "t", "endpoint": "/x", "dialect": "openai-chat", "model": "m",
           "provider": None, "ok": True, "mismatch_paths": [], "body_bytes": 10}
    for _ in range(30):  # 30 × ~140B ≈ 4.2KB > cap 2048
        nsh._shadow_write_direct(rec)

    assert os.path.exists(log_path)
    assert os.path.getsize(log_path) < 2048  # live file always under cap
    assert os.path.exists(log_path + ".1")  # rotated segment exists, non-empty
    assert os.path.getsize(log_path + ".1") > 0
    assert not os.path.exists(log_path + ".2")  # single-slot rotation, no pileup


def test_rotate_capped_file_boundaries(log_path, monkeypatch):
    cap = 64
    monkeypatch.setattr(nsh, "_SHADOW_CAP_BYTES", cap)

    def write(path, data):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "ab") as fh:
            fh.write(data)

    # Under-cap file left alone.
    write(log_path, b"a" * 8)
    nsh._rotate_capped_file(log_path, cap)
    assert os.path.getsize(log_path) == 8

    # At-cap file rotates to .1; a stale .1 is replaced, not piled into .2.
    write(log_path, b"b" * 56)  # 8 + 56 = 64 >= cap
    nsh._rotate_capped_file(log_path, cap)
    assert os.path.exists(log_path + ".1")
    assert not os.path.exists(log_path)
    write(log_path, b"c" * 64)
    nsh._rotate_capped_file(log_path, cap)
    with open(log_path + ".1", "rb") as fh:
        assert fh.read() == b"c" * 64
    assert not os.path.exists(log_path + ".2")


# ── Writer internals: queue drop-on-full / enqueue / direct fallback ──────────

def test_shadow_line_drop_on_full_and_enqueue(log_path, monkeypatch):
    async def scenario():
        queue = asyncio.Queue(maxsize=1)
        monkeypatch.setattr(nsh, "_shadow_queue", queue)
        queue.put_nowait({"n": 0})
        nsh._shadow_line({"n": 1})  # queue full → dropped silently
        nsh._shadow_line({"n": 2})  # still full → dropped silently
        assert queue.qsize() == 1
        assert queue.get_nowait() == {"n": 0}

        nsh._shadow_line({"n": 3})  # room now → enqueued
        assert queue.get_nowait() == {"n": 3}

    asyncio.run(scenario())
    assert not os.path.exists(log_path)  # nothing hit disk (queue never drained)


def test_shadow_line_direct_write_fallback_no_loop(log_path):
    # No running loop (tests/CLI probes) → record lands synchronously.
    nsh._shadow_line({"ts": "t", "endpoint": "/probe", "dialect": "gemini",
                      "model": "m", "provider": None, "ok": True,
                      "mismatch_paths": [], "body_bytes": 3})
    recs = _read_records(log_path)
    assert len(recs) == 1
    assert recs[0]["endpoint"] == "/probe"


def test_boot_self_heal_truncates_oversized_file(log_path, monkeypatch):
    # Simulate a file that blew past the cap while the router was down; the
    # lazy boot-heal in _ensure_shadow_writer must truncate it immediately.
    monkeypatch.setattr(nsh, "_SHADOW_CAP_BYTES", 64)
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with open(log_path, "wb") as fh:
        fh.write(b"x" * 100)

    async def scenario():
        nsh._ensure_shadow_writer()
        # The heal runs via to_thread — give the loop a beat to complete it.
        await asyncio.sleep(0.05)

    asyncio.run(scenario())
    assert not os.path.exists(log_path) or os.path.getsize(log_path) < 100
    assert os.path.exists(log_path + ".1")  # healed by rotation, not deletion


# ── 8. endpoint_dialect mapping helper ────────────────────────────────────────

def test_endpoint_dialect_map():
    assert nsh.endpoint_dialect("/v1/chat/completions") == "openai-chat"
    assert nsh.endpoint_dialect("/gemini/v1/chat/completions") == "openai-chat"
    assert nsh.endpoint_dialect("/v1/responses") == "responses"
    assert nsh.endpoint_dialect("/v1internal:generateContent") == "gemini"
    assert nsh.endpoint_dialect("/v1internal:streamGenerateContent") == "gemini"
    assert nsh.endpoint_dialect("/v1beta/models/gemini-2.5-pro:generateContent") == "gemini"
    assert nsh.endpoint_dialect("/v1/models/gemini-2.5-pro:streamGenerateContent") == "gemini"
    assert nsh.endpoint_dialect("/v1alpha/models/gemini-2.5-pro:generateContent") == "gemini"
    # NOT wired: anthropic dialect is not built; non-inference endpoints stay out.
    assert nsh.endpoint_dialect("/v1/messages") is None
    assert nsh.endpoint_dialect("/anthropic/v1/messages") is None
    assert nsh.endpoint_dialect("/v1/images/generations") is None
    assert nsh.endpoint_dialect("/v1/videos/generations") is None
    assert nsh.endpoint_dialect("/api/scan-keys") is None
    assert nsh.endpoint_dialect("") is None
    assert nsh.endpoint_dialect(None) is None
    assert nsh.endpoint_dialect(123) is None


# ── 9. N3 (2026-09-06) — reasoning/thinking semantic compare ─────────────────
# The comparator must compare reasoning parts semantically (text + signatures /
# thoughtSignature), not raw dict equality. Before the N3 fix the thinking tuple
# carried ONLY the text, so a rebuilt body that dropped or replaced a
# thoughtSignature compared EQUAL (verified: missing-sig and divergent-sig
# pairs returned [] mismatch paths) — a real reasoning-signature loss was
# reported ok:true. The additive fix carries part.signature and the
# Gemini-attached extras.thought_signature for text/thinking/tool_use parts.

GEMINI_THOUGHT_SIG_BODY = {
    "model": "gem",
    "contents": [
        {"role": "model", "parts": [
            {"text": "ponder", "thought": True},
            {"thoughtSignature": "sig-1"},
            {"functionCall": {"name": "look", "args": {"q": "cat"}}},
            {"thoughtSignature": "sig-2"},
        ]},
    ],
}


def test_gemini_thought_signature_roundtrip_compares_clean(log_path, direct_writer):
    """Gemini thoughtSignature attach/detach is symmetric through the RT: the
    ingress attaches each standalone thoughtSignature part to the PREVIOUS
    canonical part and the egress re-emits it as its own part, so original ==
    rebuilt byte-shape and the comparator must report NO false mismatch
    (attach/detach asymmetry must not flag — it re-canonicalizes both sides).
    """
    async def scenario():
        return await nsh.shadow_run(
            "gemini", GEMINI_THOUGHT_SIG_BODY,
            endpoint="/v1beta/models/gem:generateContent",
            config=_enabled_cfg(), provider=None, model="gem")

    assert asyncio.run(scenario()) is None  # shadow never swaps
    rec = _read_records(log_path)[0]
    assert rec["ok"] is True, rec
    assert rec["mismatch_paths"] == []


def test_comparator_flags_dropped_thought_signature():
    """Regression (the N3 gap): a rebuilt body that DROPS a thoughtSignature
    part must be flagged — before the fix this compared equal because the
    thinking/text tuples carried no signature field at all."""
    rebuilt_missing_sig = {
        "model": "gem",
        "contents": [
            {"role": "model", "parts": [
                {"text": "ponder", "thought": True},
                {"functionCall": {"name": "look", "args": {"q": "cat"}}},
            ]},
        ],
    }
    paths = nsh.semantic_mismatches("gemini", GEMINI_THOUGHT_SIG_BODY, rebuilt_missing_sig)
    assert paths, "dropped thoughtSignature must be flagged, not compared equal"
    assert any("/parts" in p for p in paths), paths


def test_comparator_flags_thinking_signature_divergence():
    """Signature divergence (same thinking text, different signature) is
    flagged — including the redacted/empty-text thinking shape, where the
    signature is the ONLY distinguishing content, so it must never compare
    equal against a different signature."""
    rebuilt_diff_sig = {
        "model": "gem",
        "contents": [
            {"role": "model", "parts": [
                {"text": "ponder", "thought": True},
                {"thoughtSignature": "sig-OTHER"},
                {"functionCall": {"name": "look", "args": {"q": "cat"}}},
                {"thoughtSignature": "sig-2"},
            ]},
        ],
    }
    paths = nsh.semantic_mismatches("gemini", GEMINI_THOUGHT_SIG_BODY, rebuilt_diff_sig)
    assert paths, "divergent thoughtSignature must be flagged"
    assert any("/parts" in p for p in paths), paths

    # Redacted/empty-text thinking: signature-only divergence on an empty
    # thought part — the ONLY semantic content is the signature.
    orig_redacted = {
        "model": "gem",
        "contents": [
            {"role": "model", "parts": [
                {"text": "", "thought": True},
                {"thoughtSignature": "sig-REDACTED-A"},
            ]},
        ],
    }
    rebuilt_redacted = {
        "model": "gem",
        "contents": [
            {"role": "model", "parts": [
                {"text": "", "thought": True},
                {"thoughtSignature": "sig-REDACTED-B"},
            ]},
        ],
    }
    paths_r = nsh.semantic_mismatches("gemini", orig_redacted, rebuilt_redacted)
    assert paths_r, "empty-text (redacted) thinking with a different signature must be flagged"


def test_comparator_thinking_text_divergence_still_flagged():
    """The pre-existing semantic text compare still works and stays additive:
    same signature, different thinking text → flagged (the fix must not have
    weakened the text compare)."""
    rebuilt_diff_text = {
        "model": "gem",
        "contents": [
            {"role": "model", "parts": [
                {"text": "PONDER-CHANGED", "thought": True},
                {"thoughtSignature": "sig-1"},
                {"functionCall": {"name": "look", "args": {"q": "cat"}}},
                {"thoughtSignature": "sig-2"},
            ]},
        ],
    }
    paths = nsh.semantic_mismatches("gemini", GEMINI_THOUGHT_SIG_BODY, rebuilt_diff_text)
    assert paths
    assert any("/parts" in p for p in paths), paths
