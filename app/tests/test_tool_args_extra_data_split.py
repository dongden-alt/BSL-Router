"""Regression tests: the 2026-10-15 codex parallel-tool-call merge drop loop.

BUG (client=gemini -> provider=codex gpt-5.6-sol, Antigravity IDE):
``_codex_event_to_openai_chunk`` stamped EVERY ``response.output_item.done``
function_call with OpenAI index 0. The Gemini accumulator keys by explicit
index first (INDEX-COLLAPSE FIX 2026-09-22), so N parallel calls collapsed
into ONE slot and their COMPLETE argument objects concatenated:

    {"AbsolutePath": "...SKILL.md", "EndLine": 500}"CommandLine": "$dir...

json.loads failed "Extra data: line 1 column ~240 (char ~239)" -- exactly the
end of the FIRST call's complete JSON. The repair ladder had no rung for
multi-document buffers, so the slot was DROPPED, finishReason forced
MAX_TOKENS, the IDE retried the turn, the model re-emitted the parallel
batch, and the identical merge reproduced -- the "continuously face this
error" report (11 occurrences, app.out.log L228566 + L242363-242858).

Three defense layers, each pinned here:
  L1  codex adapter: per-stream per-call_id index allocation
  L2  gemini accumulator: index-collision + id-collision guards
  L3  repair ladder: split_extra_documents + missing-`{` rung
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app import codex_adapter
from app.compat.adapters.gemini import (
    _new_state,
    get_tool_arg_stats,
    openai_chunk_to_gemini,
)
from app.middleware.tool_arg_repair import (
    repair_json_arguments,
    split_extra_documents,
)


# -- helpers ----------------------------------------------------------------

async def _aiter(items):
    for item in items:
        yield item


def _sse_bytes(events):
    chunks = []
    for name, data in events:
        chunks.append(f"event: {name}\n".encode())
        chunks.append(f"data: {json.dumps(data)}\n\n".encode())
    return chunks


def _collect_sse(events):
    async def _run():
        out = []
        async for chunk in codex_adapter.responses_sse_to_openai_sse(
            _aiter(_sse_bytes(events))
        ):
            out.append(chunk)
        return out

    raw = asyncio.run(_run())
    return [json.loads(c.decode("utf-8").split("data: ", 1)[1]) for c in raw]


def _fc_done(call_id, name, arguments):
    return (
        "response.output_item.done",
        {"item": {"type": "function_call", "call_id": call_id,
                  "name": name, "arguments": arguments}},
    )


def _completed(output):
    return (
        "response.completed",
        {"response": {"id": "resp_x", "model": "gpt-5.6-sol",
                      "status": "completed", "output": output, "usage": {}}},
    )


def _chunk(tool_calls=None, finish=None):
    delta = {}
    if tool_calls is not None:
        delta["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-test",
        "model": "gpt-5.6-sol",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


def _flush_parts(state):
    g = openai_chunk_to_gemini(_chunk(finish="tool_calls"), state)
    parts = []
    finish = None
    for cand in g["response"]["candidates"]:
        if cand.get("finishReason"):
            finish = cand["finishReason"]
        for part in (cand.get("content", {}) or {}).get("parts", []) or []:
            if "functionCall" in part:
                parts.append(part["functionCall"])
    return parts, finish


# -- L1: adapter per-stream index ledger ------------------------------------

class TestAdapterIndexLedger:
    def test_parallel_done_events_get_distinct_indices(self):
        """The exact production shape: two parallel function_call
        done-events in one stream. Pre-fix both carried index 0 and the
        Gemini accumulator concatenated their arguments."""
        parsed = _collect_sse([
            _fc_done("call_w", "view_file", '{"AbsolutePath": "a.py"}'),
            _fc_done("call_t", "grep_search", '{"Query": "def foo"}'),
            _completed([]),
        ])
        tcs = [c["choices"][0]["delta"]["tool_calls"][0] for c in parsed
               if c["choices"][0]["delta"].get("tool_calls")]
        assert [t["index"] for t in tcs] == [0, 1]
        assert [t["function"]["name"] for t in tcs] == ["view_file", "grep_search"]
        assert [t["id"] for t in tcs] == ["call_w", "call_t"]

    def test_single_call_keeps_pinned_index_zero(self):
        """Direct-call contract pinned by test_codex_adapter_tool_roles:
        the first (and here only) call must still be index 0."""
        chunk = codex_adapter._codex_event_to_openai_chunk(
            "response.output_item.done",
            {"item": {"type": "function_call", "call_id": "call_w",
                      "name": "get_weather", "arguments": '{"city": "Paris"}'}},
        )
        assert chunk["choices"][0]["delta"]["tool_calls"][0]["index"] == 0

    def test_same_call_id_reuses_its_index(self):
        """A re-emission under the same call_id keeps one stable index
        (no phantom third slot for what is one logical call)."""
        parsed = _collect_sse([
            _fc_done("call_w", "view_file", '{"AbsolutePath": "a.py"}'),
            _fc_done("call_w", "view_file", '{"AbsolutePath": "a.py"}'),
            _completed([]),
        ])
        tcs = [c["choices"][0]["delta"]["tool_calls"][0] for c in parsed
               if c["choices"][0]["delta"].get("tool_calls")]
        assert [t["index"] for t in tcs] == [0, 0]


# -- L2: accumulator collision guards ----------------------------------------

class TestAccumulatorCollisionGuards:
    def test_same_explicit_index_distinct_ids_do_not_merge(self):
        """Simulates the PRE-FIX codex upstream (both calls stamped index 0):
        the guard must re-key the second call instead of concatenating."""
        state = _new_state()
        openai_chunk_to_gemini(_chunk(tool_calls=[
            {"index": 0, "id": "call_w",
             "function": {"name": "view_file",
                          "arguments": '{"AbsolutePath": "a.py"}'}},
        ]), state)
        openai_chunk_to_gemini(_chunk(tool_calls=[
            {"index": 0, "id": "call_t",
             "function": {"name": "grep_search",
                          "arguments": '{"Query": "def foo"}'}},
        ]), state)
        parts, finish = _flush_parts(state)
        assert len(parts) == 2, "index collision must not merge two calls"
        assert finish == "STOP", "a recovered batch must not report MAX_TOKENS"
        assert {p["name"] for p in parts} == {"view_file", "grep_search"}
        by_name = {p["name"]: p["args"] for p in parts}
        assert by_name["view_file"]["AbsolutePath"] == "a.py"
        assert by_name["grep_search"]["Query"] == "def foo"

    def test_same_id_distinct_names_do_not_merge(self):
        """Upstream id fallback can hand two DISTINCT calls the same id."""
        state = _new_state()
        openai_chunk_to_gemini(_chunk(tool_calls=[
            {"id": "call_dupe",
             "function": {"name": "view_file",
                          "arguments": '{"AbsolutePath": "a.py"}'}},
        ]), state)
        openai_chunk_to_gemini(_chunk(tool_calls=[
            {"id": "call_dupe",
             "function": {"name": "run_command",
                          "arguments": '{"CommandLine": "dir"}'}},
        ]), state)
        parts, finish = _flush_parts(state)
        assert len(parts) == 2
        assert finish == "STOP"
        assert {p["name"] for p in parts} == {"view_file", "run_command"}

    def test_legit_same_index_continuation_still_merges(self):
        """No regression: same index + same id across fragments is ONE call
        (the ordinary delta-continuation pattern)."""
        state = _new_state()
        openai_chunk_to_gemini(_chunk(tool_calls=[
            {"index": 0, "id": "call_w",
             "function": {"name": "view_file", "arguments": '{"AbsolutePath":'}},
        ]), state)
        openai_chunk_to_gemini(_chunk(tool_calls=[
            {"index": 0, "function": {"arguments": ' "a.py"}'}},
        ]), state)
        parts, finish = _flush_parts(state)
        assert len(parts) == 1
        assert parts[0]["args"]["AbsolutePath"] == "a.py"


# -- L3a: split_extra_documents ----------------------------------------------

class TestSplitExtraDocuments:
    def test_two_complete_documents_split(self):
        docs = split_extra_documents('{"a": 1}{"b": 2}')
        assert docs == ['{"a": 1}', '{"b": 2}']

    def test_three_documents_split(self):
        docs = split_extra_documents('{"a":1}{"b":2}{"c":3}')
        assert docs == ['{"a":1}', '{"b":2}', '{"c":3}']

    def test_single_document_returns_unchanged(self):
        s = '{"a": 1}'
        assert split_extra_documents(s) == [s]

    def test_garbage_tail_refuses_split(self):
        s = '{"a": 1}xyz'
        assert split_extra_documents(s) == [s]

    def test_pure_garbage_refuses_split(self):
        s = '}{ not json at all }{'
        assert split_extra_documents(s) == [s]

    def test_empty_input(self):
        assert split_extra_documents("") == [""]
        assert split_extra_documents("   ") == ["   "]


# -- L3b: missing-opening-brace rung -----------------------------------------

class TestMissingOpeningBrace:
    def test_lone_key_value_fragment_recovers(self):
        """The L228566 shape: raw args began mid-object with no opening
        brace ('"CommandLine": "$dir = ...')."""
        repaired, was = repair_json_arguments('"CommandLine": "echo hi"')
        assert was is True
        assert json.loads(repaired) == {"CommandLine": "echo hi"}

    def test_truncated_lone_fragment_recovers(self):
        repaired, was = repair_json_arguments('"CommandLine": "echo hi')
        assert was is True
        parsed = json.loads(repaired)
        assert parsed["CommandLine"].startswith("echo hi")


# -- end-to-end through the flush --------------------------------------------

class TestFlushSplitPath:
    def test_merged_slot_buffer_recovers_two_calls(self):
        """One slot whose fragments concatenate two COMPLETE documents --
        the shape the live router dropped with phantom MAX_TOKENS 11 times."""
        state = _new_state()
        openai_chunk_to_gemini(_chunk(tool_calls=[
            {"index": 0, "id": "call_merged",
             "function": {"name": "merged_tool", "arguments": '{"a": '}},
        ]), state)
        openai_chunk_to_gemini(_chunk(tool_calls=[
            {"index": 0, "function": {"arguments": '1}{"b": 2}'}},
        ]), state)
        parts, finish = _flush_parts(state)
        assert len(parts) == 2, "split must recover both documents"
        assert finish == "STOP", "recovered calls must not signal MAX_TOKENS"
        assert sorted(p["args"].get("a", p["args"].get("b")) for p in parts) == [1, 2]

    def test_split_counter_increments(self):
        before = get_tool_arg_stats()
        state = _new_state()
        openai_chunk_to_gemini(_chunk(tool_calls=[
            {"index": 0, "id": "call_m",
             "function": {"name": "merged_tool",
                          "arguments": '{"a": 1}{"b": 2}'}},
        ]), state)
        _flush_parts(state)
        assert get_tool_arg_stats()["split"] == before["split"] + 1

    def test_genuinely_unrecoverable_still_drops_and_signals_max_tokens(self):
        """Honest-failure invariant preserved: the split rung must not
        become blanket permissiveness for real garbage."""
        state = _new_state()
        openai_chunk_to_gemini(_chunk(tool_calls=[
            {"index": 0, "id": "call_bad",
             "function": {"name": "broken",
                          "arguments": '}{ not json at all }{'}},
        ]), state)
        parts, finish = _flush_parts(state)
        assert len(parts) == 0
        assert finish == "MAX_TOKENS"
