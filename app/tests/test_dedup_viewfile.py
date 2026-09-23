"""Unit tests for the in-window view_file dedup middleware.

Contract asserted here:
    1. No-tools / no-duplicates / no-view_file requests pass through unchanged.
    2. Duplicate reads of the SAME path+range are stripped, FIRST occurrence kept.
    3. Distinct line ranges on the same path are NOT deduped.
    4. Removal is ATOMIC PER PAIR: every functionResponse in the result has a
       matching functionCall — no orphan half is ever produced. This is the
       invariant that keeps the gemini.py FIFO id-minter (L407-513) in sync;
       breaking it would desync every downstream tool_call_id.
    5. The deduped request, fed through the real `gemini_request_to_openai`
       adapter, yields no orphan tool_call_id (every `tool` message id is
       declared by a preceding assistant `tool_calls`).
    6. Fail-open: a request that raises inside the transform comes back
       unchanged with zeroed stats.
"""
import copy
import json

import pytest

from app.middleware.dedup_viewfile import dedup_viewfile_pairs
from app.compat.adapters.gemini import gemini_request_to_openai


# ─── helpers ─────────────────────────────────────────────────────────────────

def _fc(path, start=None, end=None):
    """A view_file functionCall part."""
    args = {"AbsolutePath": path}
    if start is not None:
        args["StartLine"] = start
    if end is not None:
        args["EndLine"] = end
    return {"functionCall": {"name": "view_file", "args": args}}


def _fr(name="view_file", result="CONTENT"):
    """A matching functionResponse part."""
    return {"functionResponse": {"name": name, "response": {"result": result}}}


def _model_turn(*parts):
    return {"role": "model", "parts": list(parts)}


def _user_turn(*parts):
    return {"role": "user", "parts": list(parts)}


def _req(*contents, tools=None):
    """Build a minimal Gemini request envelope."""
    r = {"contents": list(contents)}
    if tools is not None:
        r["tools"] = tools
    return r


def _tools_decl():
    """A tools array declaring view_file so the request looks realistic."""
    return [{"functionDeclarations": [{"name": "view_file"}]}]


def _orphan_tool_ids(openai_body):
    """Return tool_call_ids referenced by `tool` messages but never declared
    by a preceding assistant `tool_calls` block — these are the FIFO-breaking
    orphans the dedup must never introduce."""
    declared = set()
    orphans = []
    for msg in openai_body.get("messages", []):
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                declared.add(tc.get("id"))
        elif msg.get("role") == "tool":
            tid = msg.get("tool_call_id")
            if tid and tid not in declared:
                orphans.append(tid)
    return orphans


def _view_file_call_ids(openai_body):
    """Every tool_call id whose function name is view_file."""
    ids = []
    for msg in openai_body.get("messages", []):
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                if tc.get("function", {}).get("name") == "view_file":
                    ids.append(tc.get("id"))
    return ids


# ─── 1. No tools / no view_file ─────────────────────────────────────────────

def test_no_tools_passes_through_unchanged():
    req = _req(_user_turn({"text": "hello"}))
    passed = copy.deepcopy(req)
    out, stats = dedup_viewfile_pairs(passed)
    assert out is passed  # zero-mutation path returns the SAME object
    assert stats["duplicates_removed"] == 0
    assert stats["bytes_saved_est"] == 0


def test_empty_contents_unchanged():
    out, stats = dedup_viewfile_pairs({"contents": []})
    assert stats["duplicates_removed"] == 0
    assert out.get("contents") == []


def test_view_file_calls_but_all_distinct_kept():
    """3 view_file calls, 3 distinct paths -> all kept."""
    req = _req(
        _model_turn(_fc("/tmp/a.txt")),
        _user_turn(_fr()),
        _model_turn(_fc("/tmp/b.txt")),
        _user_turn(_fr()),
        _model_turn(_fc("/tmp/c.txt")),
        _user_turn(_fr()),
        tools=_tools_decl(),
    )
    out, stats = dedup_viewfile_pairs(copy.deepcopy(req))
    assert stats["duplicates_removed"] == 0
    assert stats["paths_seen"] == 3
    # Same number of content entries, same parts.
    assert len(out["contents"]) == len(req["contents"])


# ─── 2. Duplicates stripped, first kept ──────────────────────────────────────

def test_two_duplicates_same_path():
    """calls [A, A, A] -> only first A kept, 2 pairs removed."""
    req = _req(
        _model_turn(_fc("/tmp/a.txt")),
        _user_turn(_fr(result="A1")),
        _model_turn(_fc("/tmp/a.txt")),
        _user_turn(_fr(result="A2")),
        _model_turn(_fc("/tmp/a.txt")),
        _user_turn(_fr(result="A3")),
        tools=_tools_decl(),
    )
    out, stats = dedup_viewfile_pairs(copy.deepcopy(req))
    assert stats["duplicates_removed"] == 2
    assert stats["paths_seen"] == 1
    # Only the first A pair survives.
    fcs = [p for c in out["contents"] for p in c.get("parts", [])
           if isinstance(p, dict) and "functionCall" in p]
    frs = [p for c in out["contents"] for p in c.get("parts", [])
           if isinstance(p, dict) and "functionResponse" in p]
    assert len(fcs) == 1
    assert len(frs) == 1


def test_mixed_paths_first_occurrence_kept():
    """[A, B, A, C, B] -> [A, B, C] kept (first occurrence of each)."""
    req = _req(
        _model_turn(_fc("/tmp/a.txt")),
        _user_turn(_fr()),
        _model_turn(_fc("/tmp/b.txt")),
        _user_turn(_fr()),
        _model_turn(_fc("/tmp/a.txt")),  # dup A
        _user_turn(_fr()),
        _model_turn(_fc("/tmp/c.txt")),
        _user_turn(_fr()),
        _model_turn(_fc("/tmp/b.txt")),  # dup B
        _user_turn(_fr()),
        tools=_tools_decl(),
    )
    out, stats = dedup_viewfile_pairs(copy.deepcopy(req))
    assert stats["duplicates_removed"] == 2  # dup A + dup B
    assert stats["paths_seen"] == 3
    paths = []
    for c in out["contents"]:
        for p in c.get("parts", []):
            if isinstance(p, dict) and "functionCall" in p:
                paths.append(p["functionCall"]["args"]["AbsolutePath"])
    assert paths == ["/tmp/a.txt", "/tmp/b.txt", "/tmp/c.txt"]


# ─── 3. Distinct line ranges NOT deduped ─────────────────────────────────────

def test_distinct_line_ranges_are_kept():
    """Same path, different StartLine -> both kept (distinct reads)."""
    req = _req(
        _model_turn(_fc("/tmp/a.txt", start=1, end=10)),
        _user_turn(_fr()),
        _model_turn(_fc("/tmp/a.txt", start=50, end=60)),
        _user_turn(_fr()),
        tools=_tools_decl(),
    )
    out, stats = dedup_viewfile_pairs(copy.deepcopy(req))
    assert stats["duplicates_removed"] == 0
    assert stats["paths_seen"] == 2  # two distinct range keys


def test_identical_line_ranges_are_deduped():
    """Same path + identical StartLine/EndLine -> duplicate, removed."""
    req = _req(
        _model_turn(_fc("/tmp/a.txt", start=1, end=10)),
        _user_turn(_fr()),
        _model_turn(_fc("/tmp/a.txt", start=1, end=10)),
        _user_turn(_fr()),
        tools=_tools_decl(),
    )
    out, stats = dedup_viewfile_pairs(copy.deepcopy(req))
    assert stats["duplicates_removed"] == 1


def test_whole_file_vs_ranged_not_deduped():
    """Whole-file read then a ranged read of the same path -> both kept."""
    req = _req(
        _model_turn(_fc("/tmp/a.txt")),                       # whole file
        _user_turn(_fr()),
        _model_turn(_fc("/tmp/a.txt", start=5, end=8)),       # ranged
        _user_turn(_fr()),
        tools=_tools_decl(),
    )
    out, stats = dedup_viewfile_pairs(copy.deepcopy(req))
    assert stats["duplicates_removed"] == 0
    assert stats["paths_seen"] == 2


# ─── 4. Orphan-proofing: atomic paired removal ───────────────────────────────

def test_no_orphan_functioncall_left():
    """A functionCall marked for removal must not leave its functionResponse
    behind, and vice versa. After dedup, every functionResponse has a
    preceding matching functionCall."""
    req = _req(
        _model_turn(_fc("/tmp/a.txt")),
        _user_turn(_fr()),
        _model_turn(_fc("/tmp/a.txt")),  # dup
        _user_turn(_fr()),               # its response
        _model_turn(_fc("/tmp/b.txt")),
        _user_turn(_fr()),
        tools=_tools_decl(),
    )
    out, _ = dedup_viewfile_pairs(copy.deepcopy(req))

    # Walk parts in order: a stack of pending call names. Each functionResponse
    # must find a matching pending functionCall of the same name.
    pending = []
    for c in out["contents"]:
        for p in c.get("parts", []):
            if not isinstance(p, dict):
                continue
            if "functionCall" in p:
                pending.append(p["functionCall"]["name"])
            elif "functionResponse" in p:
                rname = p["functionResponse"]["name"]
                assert rname in pending, (
                    "ORPHAN functionResponse '%s' has no matching functionCall" % rname
                )
                pending.remove(rname)
    # No leftover unmatched functionCalls either.
    assert pending == [], "ORPHAN functionCall(s) left without response: %s" % pending


def test_orphan_response_in_input_is_kept():
    """An orphan functionResponse already present in the input (no prior call)
    must be KEPT, not assumed to be a duplicate."""
    req = _req(
        _user_turn(_fr()),  # orphan response, no preceding call
        _model_turn(_fc("/tmp/a.txt")),
        _user_turn(_fr()),
        tools=_tools_decl(),
    )
    out, stats = dedup_viewfile_pairs(copy.deepcopy(req))
    assert stats["duplicates_removed"] == 0
    # Both functionResponses survive: the orphan AND the a.txt response. The
    # orphan must not be dropped just because it has no matching call.
    frs = [p for c in out["contents"] for p in c.get("parts", [])
           if isinstance(p, dict) and "functionResponse" in p]
    assert len(frs) == 2


def test_shared_content_entry_partial_removal():
    """Multiple parts in ONE content entry: removing one part keeps the other
    and does not drop the entry."""
    req = _req(
        # model turn: text + a functionCall that will be a dup
        _model_turn({"text": "let me recheck"}, _fc("/tmp/a.txt")),
        _user_turn(_fr()),
        _model_turn(_fc("/tmp/a.txt")),  # dup
        _user_turn(_fr()),
        tools=_tools_decl(),
    )
    out, stats = dedup_viewfile_pairs(copy.deepcopy(req))
    assert stats["duplicates_removed"] == 1
    # First model turn keeps its text part AND its functionCall.
    first = out["contents"][0]
    assert first["role"] == "model"
    assert any("text" in p for p in first["parts"])
    assert any("functionCall" in p for p in first["parts"])


def test_content_entry_dropped_when_emptied():
    """If every part of a content entry is removed, the entry itself is
    dropped (no empty role:parts[] block left in the wire)."""
    req = _req(
        _model_turn(_fc("/tmp/a.txt")),
        _user_turn(_fr()),
        # A pure functionResponse user turn whose call was a dup -> both halves
        # removed -> this user entry should vanish entirely.
        _model_turn(_fc("/tmp/a.txt")),  # dup call (only part)
        _user_turn(_fr()),               # only part -> entry emptied
        tools=_tools_decl(),
    )
    out, stats = dedup_viewfile_pairs(copy.deepcopy(req))
    assert stats["duplicates_removed"] == 1
    # The emptied user turn (index 3 in input) must be gone.
    # Remaining contents: [model A, user A-resp]. The dup pair is gone.
    assert len(out["contents"]) == 2


# ─── 5. End-to-end through the real adapter: no orphan tool_call_id ──────────

def _assert_no_orphan_through_adapter(out):
    """Feed the deduped Gemini request through the REAL adapter and assert
    every tool message's id was declared by a preceding assistant."""
    openai_body = gemini_request_to_openai(out, "glm-5.3")
    orphans = _orphan_tool_ids(openai_body)
    assert orphans == [], "orphan tool_call_id(s) after adapter: %s" % orphans
    return openai_body


def test_end_to_end_no_orphan_tool_call_ids():
    """The integration smoke: dedup then convert; no orphan tool_call_id."""
    req = _req(
        _model_turn(_fc("/tmp/a.txt")),
        _user_turn(_fr(result="A1")),
        _model_turn(_fc("/tmp/a.txt")),   # dup
        _user_turn(_fr(result="A2")),
        _model_turn(_fc("/tmp/b.txt")),
        _user_turn(_fr(result="B")),
        tools=_tools_decl(),
    )
    out, stats = dedup_viewfile_pairs(copy.deepcopy(req))
    assert stats["duplicates_removed"] == 1
    openai_body = _assert_no_orphan_through_adapter(out)
    # Exactly two view_file tool_call ids (a.txt first, b.txt) — the dup is gone.
    vf_ids = _view_file_call_ids(openai_body)
    assert len(vf_ids) == 2
    assert len(set(vf_ids)) == 2, "tool_call ids must be unique: %s" % vf_ids


def test_end_to_end_three_duplicates_no_orphans():
    """Stress: [A,A,A] deduped -> 1 kept, adapter emits exactly 1 view_file id."""
    req = _req(
        _model_turn(_fc("/tmp/x.txt")),
        _user_turn(_fr(result="1")),
        _model_turn(_fc("/tmp/x.txt")),
        _user_turn(_fr(result="2")),
        _model_turn(_fc("/tmp/x.txt")),
        _user_turn(_fr(result="3")),
        tools=_tools_decl(),
    )
    out, stats = dedup_viewfile_pairs(copy.deepcopy(req))
    assert stats["duplicates_removed"] == 2
    openai_body = _assert_no_orphan_through_adapter(out)
    assert len(_view_file_call_ids(openai_body)) == 1


def test_end_to_end_mixed_paths_no_orphans():
    """[A,B,A,C,B] deduped -> [A,B,C]; adapter ids all unique, no orphans."""
    req = _req(
        _model_turn(_fc("/tmp/a.txt")),
        _user_turn(_fr()),
        _model_turn(_fc("/tmp/b.txt")),
        _user_turn(_fr()),
        _model_turn(_fc("/tmp/a.txt")),
        _user_turn(_fr()),
        _model_turn(_fc("/tmp/c.txt")),
        _user_turn(_fr()),
        _model_turn(_fc("/tmp/b.txt")),
        _user_turn(_fr()),
        tools=_tools_decl(),
    )
    out, stats = dedup_viewfile_pairs(copy.deepcopy(req))
    assert stats["duplicates_removed"] == 2
    openai_body = _assert_no_orphan_through_adapter(out)
    assert len(_view_file_call_ids(openai_body)) == 3
    ids = _view_file_call_ids(openai_body)
    assert len(set(ids)) == 3


# ─── 6. Fail-open ────────────────────────────────────────────────────────────

def test_fail_open_on_non_dict():
    out, stats = dedup_viewfile_pairs("not a dict")  # type: ignore[arg-type]
    assert out == "not a dict"
    assert stats == {"duplicates_removed": 0, "bytes_saved_est": 0, "paths_seen": 0}


def test_fail_open_on_garbage_contents():
    out, stats = dedup_viewfile_pairs({"contents": "should-be-list"})
    assert out == {"contents": "should-be-list"}
    assert stats["duplicates_removed"] == 0


# ─── Path normalization ──────────────────────────────────────────────────────

def test_path_normalization_case_insensitive():
    """Windows paths differing only in case are the same read."""
    req = _req(
        _model_turn(_fc("D:\\Projects\\App\\Main.py")),
        _user_turn(_fr()),
        _model_turn(_fc("d:\\projects\\app\\main.py")),  # same path, lowercased
        _user_turn(_fr()),
        tools=_tools_decl(),
    )
    out, stats = dedup_viewfile_pairs(copy.deepcopy(req))
    assert stats["duplicates_removed"] == 1


def test_path_normalization_json_string_args():
    """When args arrive as a JSON string, the AbsolutePath is still extracted."""
    fc = {
        "functionCall": {
            "name": "view_file",
            "args": json.dumps({"AbsolutePath": "D:\\\\a.py"}),
        }
    }
    req = _req(
        _model_turn(fc),
        _user_turn(_fr()),
        _model_turn(fc),  # dup
        _user_turn(_fr()),
        tools=_tools_decl(),
    )
    out, stats = dedup_viewfile_pairs(copy.deepcopy(req))
    assert stats["duplicates_removed"] == 1


def test_stats_paths_seen_counts_distinct_paths():
    req = _req(
        _model_turn(_fc("/tmp/a.txt")),
        _user_turn(_fr()),
        _model_turn(_fc("/tmp/a.txt")),
        _user_turn(_fr()),
        _model_turn(_fc("/tmp/b.txt")),
        _user_turn(_fr()),
        tools=_tools_decl(),
    )
    _, stats = dedup_viewfile_pairs(copy.deepcopy(req))
    assert stats["paths_seen"] == 2  # a + b, the dup a is not re-counted
