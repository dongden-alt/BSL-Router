"""Gemini adapter tool-argument repair — the N-in/N-out batch invariant.

REGRESSION GUARD for the drop syndrome fixed 2026-09-22.

THE DEFECT
----------
`openai_chunk_to_gemini` reassembles each tool call's arguments from many
streamed deltas and parses once on the finish chunk. When the reassembled
buffer did not parse, the loop `continue`d — DELETING that call from the
batch. The IDE received N-1 functionCall parts plus a forced MAX_TOKENS:
a batch missing members, and a model apparently reporting truncation.

WHY IT LOOKED LIKE A GLM PROBLEM
--------------------------------
It scales with argument fragmentation. Measured avg deltas per tool call:
GLM 8.6, Sonnet 6.1, Qwen 4.5 — GLM presents ~2x the reassembly surface, so
it tripped the defect far more often. The severity ordering users reported
(GLM worst, Sonnet/Opus milder, Qwen/Kimi never) tracks fragmentation
density, not model capability.

WHY IT SURVIVED SO LONG
-----------------------
The Anthropic lane has repaired malformed tool args since day one
(stream_normalizer.py::_repair_tool_input). This Gemini lane — the only one
the Antigravity IDE actually speaks (traffic census: candidates=1833,
tool_use=0) — never called any repair. Test harnesses all spoke Anthropic
and therefore exercised the one path that already worked.

THE INVARIANT
-------------
N tool calls in => N functionCall parts out, whenever the arguments are
recoverable. A call is dropped ONLY when genuinely unrecoverable, and only
then may finishReason become MAX_TOKENS.
"""
from __future__ import annotations

import json

import pytest

from app.compat.adapters.gemini import (
    _new_state,
    get_tool_arg_stats,
    openai_chunk_to_gemini,
    openai_response_to_gemini,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _chunk(tool_calls=None, finish=None, content=None, reasoning=None):
    delta = {}
    if tool_calls is not None:
        delta["tool_calls"] = tool_calls
    if content is not None:
        delta["content"] = content
    if reasoning is not None:
        delta["reasoning_content"] = reasoning
    return {
        "id": "chatcmpl-test",
        "model": "glm-5.3",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


def _drive(calls, finish="tool_calls"):
    """Stream `calls` = [(name, [arg_fragment, ...]), ...] through the adapter.

    Fragments are interleaved across calls exactly as a real SSE stream
    delivers them (round-robin by fragment index), then a finish chunk
    triggers the flush. Returns (function_call_parts, finish_reason).
    """
    state = _new_state()
    frames = []

    opening = [
        {"index": i, "id": f"call_{name}_{i}", "function": {"name": name, "arguments": ""}}
        for i, (name, _f) in enumerate(calls)
    ]
    g = openai_chunk_to_gemini(_chunk(tool_calls=opening), state)
    if g:
        frames.append(g)

    for fi in range(max(len(f) for _n, f in calls)):
        deltas = [
            {"index": i, "function": {"arguments": frags[fi]}}
            for i, (_n, frags) in enumerate(calls)
            if fi < len(frags)
        ]
        if deltas:
            g = openai_chunk_to_gemini(_chunk(tool_calls=deltas), state)
            if g:
                frames.append(g)

    g = openai_chunk_to_gemini(_chunk(finish=finish), state)
    if g:
        frames.append(g)

    parts, finish_reason = [], None
    for fr in frames:
        for cand in fr.get("response", {}).get("candidates", []):
            if cand.get("finishReason"):
                finish_reason = cand["finishReason"]
            for part in (cand.get("content", {}) or {}).get("parts", []) or []:
                if "functionCall" in part:
                    parts.append(part["functionCall"])
    return parts, finish_reason


# ── The core invariant ────────────────────────────────────────────────────────

class TestBatchIntegrity:
    """Every case here dropped a call before the 2026-09-22 fix."""

    def test_clean_batch_is_unaffected(self):
        parts, finish = _drive([
            ("read_file", ['{"path":', ' "app/main.py"}']),
            ("grep_search", ['{"pattern":', ' "def foo"}']),
            ("list_dir", ['{"path":', ' "app/"}']),
        ])
        assert len(parts) == 3
        assert finish == "STOP"
        assert parts[0]["args"]["path"] == "app/main.py"

    def test_truncated_mid_string_call_is_preserved(self):
        """Was: 3 sent -> 2 emitted, MAX_TOKENS."""
        parts, finish = _drive([
            ("read_file", ['{"pa', 'th": ', '"app/', 'main.py"}']),
            ("grep_search", ['{"pat', 'tern": "de', 'f fo']),  # cut mid-string
            ("list_dir", ['{"path":', ' "app/"}']),
        ])
        assert len(parts) == 3, "truncated call must be repaired, not dropped"
        assert finish == "STOP", "a complete batch must not report MAX_TOKENS"
        assert [p["name"] for p in parts] == ["read_file", "grep_search", "list_dir"]
        assert parts[1]["args"]["pattern"] == "def fo"

    def test_unbalanced_brace_call_is_preserved(self):
        parts, finish = _drive([
            ("read_file", ['{"path": "app/main.py"}']),
            ("grep_search", ['{"pattern": "x", "opts": {"i": true']),
            ("list_dir", ['{"path": "app/"}']),
        ])
        assert len(parts) == 3
        assert finish == "STOP"
        assert parts[1]["args"]["opts"]["i"] is True

    def test_heavy_parallel_batch_two_of_six_truncated(self):
        """The headline case: was 6 sent -> 4 emitted."""
        parts, finish = _drive([
            ("read_file", ['{"path": "a.py"}']),
            ("read_file", ['{"path": "b.py"}']),
            ("grep_search", ['{"pattern": "cut']),
            ("list_dir", ['{"path": "c/"}']),
            ("read_file", ['{"path": "d.p']),
            ("list_dir", ['{"path": "e/"}']),
        ])
        assert len(parts) == 6, "no member of a 6-call batch may be lost"
        assert finish == "STOP"

    def test_trailing_comma_call_is_preserved(self):
        parts, finish = _drive([
            ("read_file", ['{"path": "a.py",}']),
            ("list_dir", ['{"path": "b/"}']),
        ])
        assert len(parts) == 2
        assert finish == "STOP"
        assert parts[0]["args"]["path"] == "a.py"

    def test_extreme_fragmentation_one_char_per_delta(self):
        """GLM-style worst case: arguments split to single characters."""
        args = '{"path": "app/compat/adapters/gemini.py"}'
        parts, finish = _drive([
            ("read_file", list(args)),
            ("list_dir", ['{"path": "app/"}']),
        ])
        assert len(parts) == 2
        assert finish == "STOP"
        assert parts[0]["args"]["path"] == "app/compat/adapters/gemini.py"

    def test_large_batch_of_twelve(self):
        calls = [(f"read_file", ['{"path": "f%d.py"}' % i]) for i in range(12)]
        parts, finish = _drive(calls)
        assert len(parts) == 12
        assert finish == "STOP"


# ── Honest failure is preserved ───────────────────────────────────────────────

class TestUnrecoverableStillDrops:
    """The fix must not become blanket permissiveness."""

    def test_unrecoverable_args_still_drop_and_signal_max_tokens(self):
        parts, finish = _drive([
            ("read_file", ['{"path": "ok.py"}']),
            ("broken", ['}{ not json at all }{']),
        ])
        assert len(parts) == 1, "genuinely unrecoverable args must still drop"
        assert finish == "MAX_TOKENS", "a real drop must still signal truncation"

    def test_empty_args_are_not_an_error(self):
        parts, finish = _drive([("no_args_tool", [""])])
        assert len(parts) == 1
        assert finish == "STOP"


# ── Existing behaviour must not regress ───────────────────────────────────────

class TestNoRegressions:
    def test_thought_signature_survives_repair(self):
        """Must not regress the Gemini 3.1-Pro 400 signature fix."""
        state = _new_state()
        openai_chunk_to_gemini(_chunk(tool_calls=[{
            "index": 0, "id": "call_0",
            "function": {"name": "read_file", "arguments": '{"path": "a.p'},
            "thought_signature": "SIG_ABC123",
        }]), state)
        g = openai_chunk_to_gemini(_chunk(finish="tool_calls"), state)

        parts = g["response"]["candidates"][0]["content"]["parts"]
        fc = [p for p in parts if "functionCall" in p]
        assert len(fc) == 1, "signed call must survive repair"
        assert fc[0]["thoughtSignature"] == "SIG_ABC123"
        assert fc[0]["functionCall"]["args"]["path"] == "a.p"

    def test_ide_metadata_is_injected_into_repaired_args(self):
        """§8.16 injection must still apply after a repair."""
        parts, _finish = _drive([("read_file", ['{"path": "a.p'])])
        assert len(parts) == 1
        args = parts[0]["args"]
        assert "toolSummary" in args and "toolAction" in args

    def test_text_and_reasoning_parts_unaffected(self):
        state = _new_state()
        g1 = openai_chunk_to_gemini(_chunk(reasoning="thinking..."), state)
        g2 = openai_chunk_to_gemini(_chunk(content="hello"), state)
        p1 = g1["response"]["candidates"][0]["content"]["parts"][0]
        p2 = g2["response"]["candidates"][0]["content"]["parts"][0]
        assert p1 == {"thought": True, "text": "thinking..."}
        assert p2 == {"text": "hello"}

    def test_repair_counters_increment(self):
        before = get_tool_arg_stats()
        _drive([("read_file", ['{"path": "a.p'])])
        after = get_tool_arg_stats()
        assert after["repaired"] == before["repaired"] + 1


# ── Non-streaming twin ────────────────────────────────────────────────────────

class TestNonStreamingTwin:
    def test_malformed_args_are_repaired_not_masked_as_empty(self):
        """Was: args={} — which the IDE reports as TOOL_CALL_INCOMPLETE."""
        out = openai_response_to_gemini({
            "id": "resp_1",
            "choices": [{
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {"content": None, "tool_calls": [
                    {"id": "c1", "function": {"name": "read_file",
                                              "arguments": '{"path": "app/main.p'}},
                ]},
            }],
        }, "glm-5.3")

        parts = out["response"]["candidates"][0]["content"]["parts"]
        fc = [p for p in parts if "functionCall" in p]
        assert len(fc) == 1
        assert fc[0]["functionCall"]["args"]["path"] == "app/main.p", \
            "arguments must be repaired, not blanked to {}"

    def test_unrecoverable_still_falls_back_to_empty(self):
        out = openai_response_to_gemini({
            "id": "resp_2",
            "choices": [{
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {"content": None, "tool_calls": [
                    {"id": "c1", "function": {"name": "t", "arguments": '}{garbage}{'}},
                ]},
            }],
        }, "glm-5.3")

        fc = [p for p in out["response"]["candidates"][0]["content"]["parts"]
              if "functionCall" in p]
        assert len(fc) == 1
        assert "path" not in fc[0]["functionCall"]["args"]


# ── Type guard: a repair must yield an OBJECT to be usable as args ────────────

class TestNonObjectRepairIsRefused:
    """The ladder can legitimately repair a fragment into a valid NON-object
    (e.g. '[[[[' -> '[[[[]]]]', a nested array). Tool arguments must always be
    a JSON object, so `_repair_tool_args` refuses anything else rather than
    shipping a structurally invalid call to the IDE."""

    @pytest.mark.parametrize("frag", ['[[[[', '[1, 2', '"just a string', '12345'])
    def test_non_object_repair_does_not_become_args(self, frag):
        parts, finish = _drive([
            ("good_tool", ['{"path": "ok.py"}']),
            ("bad_tool", [frag]),
        ])
        # The good call always survives; the non-object one must never ship
        # a non-dict as its arguments.
        assert any(p["name"] == "good_tool" for p in parts)
        for p in parts:
            assert isinstance(p["args"], dict)
        if len(parts) == 1:
            assert finish == "MAX_TOKENS", "a real drop must signal truncation"
