"""Regression tests for the tool-call accumulator INDEX COLLAPSE.

Defect (fixed 2026-09-22):
    openai_chunk_to_gemini keyed accumulator slots on tc.get("index", 0).
    The OpenAI streaming spec says each tool-call delta carries an `index`,
    but upstreams routed through the MITM -- notably GLM-5.3 via vsllm, and
    to a lesser degree claude-opus-4-6-antigravity -- OMIT it. Every call in a
    parallel batch then resolved to key 0, so

        slot["args"] += fn["arguments"]

    concatenated N independent argument objects into a single buffer:

        {"Query":"503"}{"AbsolutePath":"D:\\a.py"}{"CommandLine":"git status"}

    json.loads on that buffer raises "Extra data: line 1 column 16", the whole
    slot was dropped, and a batch of 4 became 0 emitted functionCall parts.
    The IDE saw no tool calls at all and reported the turn as empty.

    This is the mechanism behind the "Extra data" drops in production logs and
    explains the model-specific severity ranking the user observed:
    glm-5.3 worst, claude-opus-4-6-antigravity milder, kimi/qwen unaffected
    (those upstreams do emit `index`).

Contract asserted here:
    slot identity is resolved by index when present, else by call id, else by
    attachment to the most recently opened slot -- so batch membership and
    emission order survive a missing index on BOTH full and fragmented streams.
"""
import json

import pytest

from app.compat.adapters.gemini import _new_state, openai_chunk_to_gemini


def _tc_delta(tool_calls, finish=None, idx=0):
    """Build one OpenAI streaming chunk carrying tool-call deltas."""
    delta = {"tool_calls": tool_calls} if tool_calls else {}
    choice = {"index": idx, "delta": delta}
    if finish:
        choice["finish_reason"] = finish
    return {"id": "resp_test", "model": "glm-5.3", "choices": [choice]}


def _run(chunks):
    """Feed chunks through the adapter; return the accumulated Gemini parts."""
    state = _new_state()
    frames = []
    for ch in chunks:
        out = openai_chunk_to_gemini(ch, state)
        if out:
            frames.append(out)
    return state, frames


def _function_calls(frames):
    """Pull every functionCall part out of all emitted frames."""
    calls = []
    for f in frames:
        resp = f.get("response") or {}
        for cand in (resp.get("candidates") or []):
            for part in ((cand.get("content") or {}).get("parts") or []):
                if "functionCall" in part:
                    calls.append(part["functionCall"])
    return calls


# ensure_tool_meta_fields (gemini.py) intentionally injects these two keys into
# every functionCall so the Antigravity IDE schema validation passes. They are
# NOT model-authored, so argument comparisons must strip them; otherwise every
# assertion here would fail regardless of whether the accumulator is correct.
META_FIELDS = ("toolSummary", "toolAction")


def _model_args(args):
    """Return only the model-authored argument keys."""
    if not isinstance(args, dict):
        return args
    return {k: v for k, v in args.items() if k not in META_FIELDS}


NAMES = ["grep_search", "view_file", "run_command", "list_dir"]
ARGSETS = [
    {"Query": "503"},
    {"AbsolutePath": "D:\\a.py"},
    {"CommandLine": "git status"},
    {"DirectoryPath": "D:\\p"},
]


def _open_delta(i, with_index, fragmented=False):
    """Opening delta for call i: carries id + name + (optionally) index."""
    tc = {
        "id": "call_%d" % i,
        "type": "function",
        "function": {"name": NAMES[i], "arguments": ""},
    }
    if not fragmented:
        tc["function"]["arguments"] = json.dumps(ARGSETS[i])
    if with_index:
        tc["index"] = i
    return tc


def _frag_delta(i, piece, with_index):
    """Argument-continuation delta for call i."""
    tc = {"type": "function", "function": {"arguments": piece}}
    if with_index:
        tc["index"] = i
    return tc


@pytest.mark.parametrize("with_index", [True, False], ids=["with_index", "no_index"])
def test_batch_survives_whole_arg_stream(with_index):
    """A complete batch, each call arriving with its full arguments."""
    chunks = [_tc_delta([_open_delta(i, with_index)]) for i in range(4)]
    chunks.append(_tc_delta([], finish="tool_calls"))
    _, frames = _run(chunks)
    calls = _function_calls(frames)
    assert len(calls) == 4, "expected 4 calls, got %d" % len(calls)
    assert [c["name"] for c in calls] == NAMES


@pytest.mark.parametrize("with_index", [True, False], ids=["with_index", "no_index"])
def test_fragmented_arguments_do_not_concatenate_across_calls(with_index):
    """The core defect: argument fragments streamed across several chunks.

    With a missing index these used to pile into one slot, producing a buffer
    like {"a":1}{"b":2} that fails to parse. Each call's arguments must stay
    in its own slot and parse independently.
    """
    chunks = []
    for i in range(4):
        chunks.append(_tc_delta([_open_delta(i, with_index, fragmented=True)]))
        # Stream the arguments in two pieces.
        whole = json.dumps(ARGSETS[i])
        cut = len(whole) // 2
        chunks.append(_tc_delta([_frag_delta(i, whole[:cut], with_index)]))
        chunks.append(_tc_delta([_frag_delta(i, whole[cut:], with_index)]))
    chunks.append(_tc_delta([], finish="tool_calls"))

    _, frames = _run(chunks)
    calls = _function_calls(frames)
    assert len(calls) == 4, "expected 4 calls, got %d" % len(calls)
    assert [c["name"] for c in calls] == NAMES
    for i, c in enumerate(calls):
        assert _model_args(c["args"]) == ARGSETS[i], (
            "call %d args corrupted (cross-slot concatenation): %r" % (i, c["args"])
        )
        # The IDE-required meta fields must still be present on every call.
        for field in META_FIELDS:
            assert field in c["args"], "call %d missing %s" % (i, field)


def test_index_less_slots_are_distinct_not_collapsed():
    """Direct assertion on the accumulator: no shared key collapse."""
    state = _new_state()
    for i in range(4):
        openai_chunk_to_gemini(_tc_delta([_open_delta(i, False)]), state)
    accum = state["toolCallAccum"]
    assert len(accum) == 4, "slots collapsed to %d" % len(accum)
    assert len({s["name"] for s in accum.values()}) == 4


def test_index_present_uses_index_keying():
    """When upstream DOES send index, keying must follow it (no behaviour
    change for kimi/qwen-style upstreams)."""
    state = _new_state()
    for i in range(3):
        openai_chunk_to_gemini(_tc_delta([_open_delta(i, True)]), state)
    assert len(state["toolCallAccum"]) == 3
    assert all(k[0] == "i" for k in state["toolCallAccum"]), (
        "index-bearing deltas should use ('i', index) keys"
    )


def test_index_less_uses_sequence_keying():
    state = _new_state()
    for i in range(3):
        openai_chunk_to_gemini(_tc_delta([_open_delta(i, False)]), state)
    assert all(k[0] == "s" for k in state["toolCallAccum"]), (
        "index-less deltas should use ('s', seq) keys"
    )
    assert len(state["toolCallAccum"]) == 3


def test_mixed_index_and_id_keying_does_not_collide():
    """Homogeneous tuple keys keep the finish flush's sorted() safe even when
    a stream mixes both keying schemes."""
    state = _new_state()
    openai_chunk_to_gemini(_tc_delta([_open_delta(0, True)]), state)
    openai_chunk_to_gemini(_tc_delta([_open_delta(1, False)]), state)
    assert len(state["toolCallAccum"]) == 2
    # sorted() must not raise TypeError on mixed-but-homogeneous tuples.
    assert sorted(state["toolCallAccum"].keys())


def test_same_id_continuation_reuses_slot():
    """A repeated id must append to the SAME slot, not allocate a new one."""
    state = _new_state()
    openai_chunk_to_gemini(_tc_delta([_open_delta(0, False, fragmented=True)]), state)
    openai_chunk_to_gemini(_tc_delta([_frag_delta(0, '{"a":', False)]), state)
    tc = {"id": "call_0", "type": "function", "function": {"arguments": "1}"}}
    openai_chunk_to_gemini(_tc_delta([tc]), state)
    assert len(state["toolCallAccum"]) == 1
    slot = next(iter(state["toolCallAccum"].values()))
    assert json.loads(slot["args"]) == {"a": 1}


def test_argument_only_delta_without_id_attaches_to_open_slot():
    """Some upstreams send bare argument continuations with neither index nor
    id. These must attach to the most recently opened slot rather than to a
    shared default key."""
    state = _new_state()
    openai_chunk_to_gemini(_tc_delta([_open_delta(0, False, fragmented=True)]), state)
    bare = {"type": "function", "function": {"arguments": '{"Query":"503"}'}}
    openai_chunk_to_gemini(_tc_delta([bare]), state)
    assert len(state["toolCallAccum"]) == 1
    slot = next(iter(state["toolCallAccum"].values()))
    assert json.loads(slot["args"]) == {"Query": "503"}


def test_bool_index_is_not_treated_as_int():
    """True/False are ints in Python; an upstream sending a boolean index must
    not silently become slot 0/1."""
    state = _new_state()
    tc = {"id": "call_b", "type": "function",
          "index": True, "function": {"name": "f", "arguments": "{}"}}
    openai_chunk_to_gemini(_tc_delta([tc]), state)
    assert len(state["toolCallAccum"]) == 1
    key = next(iter(state["toolCallAccum"].keys()))
    assert key[0] == "s", "boolean index should fall through to id keying"


def test_interleaved_two_call_fragments_stay_separate():
    """Worst realistic case: two calls interleaved with no index. Order of
    arrival must not merge their buffers."""
    state = _new_state()
    chunks = [
        _tc_delta([_open_delta(0, False, fragmented=True)]),
        _tc_delta([_open_delta(1, False, fragmented=True)]),
        _tc_delta([{"id": "call_0", "type": "function",
                    "function": {"arguments": '{"Query":"503"}'}}]),
        _tc_delta([{"id": "call_1", "type": "function",
                    "function": {"arguments": '{"CommandLine":"ls"}'}}]),
    ]
    for ch in chunks:
        openai_chunk_to_gemini(ch, state)
    assert len(state["toolCallAccum"]) == 2
    by_name = {s["name"]: s for s in state["toolCallAccum"].values()}
    assert json.loads(by_name["grep_search"]["args"]) == {"Query": "503"}
    assert json.loads(by_name["view_file"]["args"]) == {"CommandLine": "ls"}


def test_lazy_state_init_supplies_new_fields():
    """Callers may pass a bare {}; the new bookkeeping fields must be created."""
    state = {}
    openai_chunk_to_gemini(_tc_delta([_open_delta(0, False)]), state)
    for field in ("toolCallAccum", "toolCallIdKeys", "toolCallSeq", "toolCallLastKey"):
        assert field in state, "lazy init missed %s" % field


def test_single_call_still_works():
    """Control: the ordinary single-call path must not regress."""
    chunks = [_tc_delta([_open_delta(0, True)]), _tc_delta([], finish="tool_calls")]
    _, frames = _run(chunks)
    calls = _function_calls(frames)
    assert len(calls) == 1
    assert calls[0]["name"] == "grep_search"
