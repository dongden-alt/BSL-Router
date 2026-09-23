"""Regression tests for the GLM ASCII-block PARALLEL BATCH drop.

Defect (fixed 2026-09-22):
    _parse_tool_call_block returned Optional[Dict], i.e. at most ONE call per
    ASCII tool-call block. GLM emits parallel batches, and reseller channels
    often wrap the WHOLE batch in a single ASCII block. Three shapes lost calls:

      JSON ARRAY   [{...},{...}]   json.loads yields a list; isinstance(dict)
                                   failed, pseudo-XML found no name tag, so the
                                   parser returned None. normalize_glm_tool_calls
                                   then reported changed=False and the ENTIRE
                                   BATCH VANISHED with no log line at all.
      CONCATENATED {...}{...}      json.loads raises Extra data -> None -> lost.
      PSEUDO-XML   name tags x N   re.search matched only the FIRST pair, so a
                                   batch of N silently became 1.

    Only the ASCII parser was affected: the unicode path already used findall,
    and separately-delimited ASCII blocks already worked. That is why the bug
    presented as model-specific flakiness rather than an obvious outage.

Contract asserted here:
    every batch shape yields ALL its members on BOTH the buffered path
    (normalize_glm_tool_calls) and the streaming rescue path
    (parse_streamed_tool_block), with stable per-member ids.
"""
import json

import pytest

from app.middleware.glm_tools import (
    _calls_from_invoke_block,
    _make_tool_call_id,
    _parse_pseudo_xml_calls,
    _parse_tool_call_block,
    _parse_tool_call_block_multi,
    _split_concatenated_objects,
    normalize_glm_tool_calls,
    parse_streamed_tool_block,
)

LT = chr(60)
GT = chr(62)
OPEN = LT + "tool_call" + GT
CLOSE = LT + "/tool_call" + GT

NAMES = ["grep_search", "view_file", "run_command", "list_dir"]

# -- The three batch shapes GLM actually emits inside ONE block --------------
ARR = json.dumps([
    {"name": "grep_search", "arguments": {"Query": "503"}},
    {"name": "view_file", "arguments": {"AbsolutePath": "D:\\\\a.py"}},
    {"name": "run_command", "arguments": {"CommandLine": "git status"}},
    {"name": "list_dir", "arguments": {"DirectoryPath": "D:\\\\p"}},
])

CONCAT = "".join(
    json.dumps({"name": n, "arguments": {"k": i}}) for i, n in enumerate(NAMES)
)

PXML = "".join(
    LT + "name" + GT + n + LT + "/name" + GT
    + LT + "arguments" + GT + json.dumps({"k": i}) + LT + "/arguments" + GT
    for i, n in enumerate(NAMES)
)

# 5th dialect: ChatML tool_use/invoke/parameter blocks emitted by
# GLM-5.3-flash-max (chat2api guest lane). One outer tool_use wrapper holding
# a 4-member parallel batch (NAMES order, one k=i parameter each), so the same
# "every batch member recovered" assertions exercise the invoke parser too.
TOOL_USE = LT + "tool_use" + GT
TOOL_USE_CLOSE = LT + "/tool_use" + GT
INVOKE = (
    TOOL_USE
    + "".join(
        LT + "invoke name=" + n + GT
        + LT + "parameter name=k" + GT + str(i) + LT + "/parameter" + GT
        + LT + "/invoke" + GT
        for i, n in enumerate(NAMES)
    )
    + TOOL_USE_CLOSE
)

SINGLE = json.dumps({"name": "grep_search", "arguments": {"Query": "503"}})

BATCH_SHAPES = [
    ("json_array", ARR),
    ("concatenated_objects", CONCAT),
    ("repeated_pseudo_xml", PXML),
    ("tool_use_invoke", INVOKE),
]


# -- Layer 1: the batch parser itself ----------------------------------------
@pytest.mark.parametrize("label,body", BATCH_SHAPES)
def test_multi_parser_recovers_every_batch_member(label, body):
    calls = _parse_tool_call_block_multi(body)
    assert len(calls) == 4, "%s: expected 4 calls, got %d" % (label, len(calls))
    assert [c["function"]["name"] for c in calls] == NAMES
    for c in calls:
        assert c["type"] == "function"
        assert isinstance(c["function"]["arguments"], str)
        json.loads(c["function"]["arguments"])  # each must be valid JSON


@pytest.mark.parametrize("label,body", BATCH_SHAPES)
def test_batch_members_get_distinct_ids(label, body):
    """Distinct ids are required: a colliding id breaks client-side
    request/response correlation, which is why _make_tool_call_id is seeded
    with the member index."""
    calls = _parse_tool_call_block_multi(body)
    ids = [c["id"] for c in calls]
    assert len(set(ids)) == len(ids), "colliding tool_call ids: %s" % ids


def test_single_object_still_parses_to_one_call():
    calls = _parse_tool_call_block_multi(SINGLE)
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "grep_search"


def test_legacy_single_call_contract_preserved():
    """_parse_tool_call_block keeps its Optional[Dict] contract for callers
    that only need the first member."""
    call = _parse_tool_call_block(SINGLE)
    assert call is not None
    assert call["function"]["name"] == "grep_search"
    assert _parse_tool_call_block(ARR)["function"]["name"] == "grep_search"


def test_legacy_returns_none_for_unparseable():
    assert _parse_tool_call_block("not json at all") is None
    assert _parse_tool_call_block("") is None
    assert _parse_tool_call_block_multi("") == []


# -- Layer 1b: helper units --------------------------------------------------
def test_concat_splitter_needs_two_objects():
    """Returning [] for a lone object lets callers fall through to the next
    strategy instead of mistaking one object for a batch."""
    assert _split_concatenated_objects(SINGLE) == []
    assert len(_split_concatenated_objects(CONCAT)) == 4


def test_concat_splitter_stops_at_first_invalid_object():
    """Fail-soft: salvage the objects that DO decode rather than rejecting
    the whole span."""
    good = json.dumps({"name": "a", "arguments": {}})
    partial = good + good + '{"name": "broken"'
    assert len(_split_concatenated_objects(partial)) == 2


def test_pseudo_xml_pairs_match_positionally():
    calls = _parse_pseudo_xml_calls(PXML)
    assert [c["function"]["name"] for c in calls] == NAMES
    for i, c in enumerate(calls):
        assert json.loads(c["function"]["arguments"]) == {"k": i}


def test_pseudo_xml_missing_arguments_defaults_to_empty_object():
    """A call that omits its arguments element must still be emitted rather
    than dropped -- an empty object is recoverable, a lost call is not."""
    body = LT + "name" + GT + "list_dir" + LT + "/name" + GT
    calls = _parse_pseudo_xml_calls(body)
    assert len(calls) == 1
    assert calls[0]["function"]["arguments"] == "{}"


def test_pseudo_xml_uneven_pairing_does_not_crash():
    body = (LT + "name" + GT + "a" + LT + "/name" + GT
            + LT + "name" + GT + "b" + LT + "/name" + GT
            + LT + "arguments" + GT + "{}" + LT + "/arguments" + GT)
    calls = _parse_pseudo_xml_calls(body)
    assert len(calls) == 2


# -- Layer 2: the buffered / non-streaming production path -------------------
def _wrap(content):
    return {"choices": [{
        "index": 0,
        "message": {"role": "assistant", "content": content},
        "finish_reason": "stop",
    }]}


@pytest.mark.parametrize("label,body", BATCH_SHAPES)
def test_normalize_extracts_whole_batch(label, body):
    out, changed = normalize_glm_tool_calls(_wrap(OPEN + body + CLOSE), "glm-5.3")
    assert changed is True, "%s: batch was not detected" % label
    tcs = out["choices"][0]["message"]["tool_calls"]
    assert len(tcs) == 4, "%s: expected 4, got %d" % (label, len(tcs))
    assert [t["function"]["name"] for t in tcs] == NAMES


def test_normalize_promotes_finish_reason_to_tool_calls():
    out, _ = normalize_glm_tool_calls(_wrap(OPEN + ARR + CLOSE), "glm-5.3")
    assert out["choices"][0]["finish_reason"] == "tool_calls"


def test_normalize_strips_blocks_from_content():
    out, _ = normalize_glm_tool_calls(_wrap(OPEN + ARR + CLOSE), "glm-5.3")
    content = out["choices"][0]["message"]["content"]
    assert content is None or "tool_call" not in content


def test_normalize_separate_blocks_still_work():
    """Control: the shape the parser was originally designed for must not
    regress now that a single block can hold a batch."""
    content = "".join(
        OPEN + json.dumps({"name": n, "arguments": {"k": i}}) + CLOSE
        for i, n in enumerate(NAMES)
    )
    out, changed = normalize_glm_tool_calls(_wrap(content), "glm-5.3")
    assert changed is True
    assert len(out["choices"][0]["message"]["tool_calls"]) == 4


def test_normalize_skips_when_structured_tool_calls_present():
    payload = _wrap(OPEN + ARR + CLOSE)
    payload["choices"][0]["message"]["tool_calls"] = [
        {"id": "x", "type": "function", "function": {"name": "f", "arguments": "{}"}}
    ]
    out, changed = normalize_glm_tool_calls(payload, "glm-5.3")
    assert changed is False
    assert len(out["choices"][0]["message"]["tool_calls"]) == 1


def test_normalize_leaves_prose_untouched():
    out, changed = normalize_glm_tool_calls(_wrap("plain prose answer"), "glm-5.3")
    assert changed is False
    assert out["choices"][0]["message"]["content"] == "plain prose answer"


def test_normalize_fails_open_on_garbage():
    out, changed = normalize_glm_tool_calls(_wrap(OPEN + "{{{{" + CLOSE), "glm-5.3")
    assert changed is False


# -- Layer 3: the streaming rescue path --------------------------------------
@pytest.mark.parametrize("label,body", BATCH_SHAPES)
def test_streaming_rescue_recovers_whole_batch(label, body):
    calls = parse_streamed_tool_block(body, unicode_path=False)
    assert len(calls) == 4, "%s: expected 4, got %d" % (label, len(calls))
    assert [c["function"]["name"] for c in calls] == NAMES


def test_streaming_rescue_fails_open_on_garbage():
    assert parse_streamed_tool_block("{{{ not json", unicode_path=False) == []


def test_streaming_rescue_unicode_batch_control():
    """Control: the unicode path already handled batches via findall. It must
    keep working after the ASCII path was fixed."""
    P, S = chr(0xFF5C), chr(0x2581)

    def uni(name, args):
        return (P + "tool" + S + "call" + S + "begin" + P + "function" + P
                + "tool" + S + "call" + S + "sep" + P + name + "\n"
                + "```json\n" + args + "\n```\n"
                + P + "tool" + S + "call" + S + "end" + P + "\n")

    body = "".join(uni(n, json.dumps({"k": i})) for i, n in enumerate(NAMES))
    calls = parse_streamed_tool_block(body, unicode_path=True)
    assert len(calls) == 4
    assert [c["function"]["name"] for c in calls] == NAMES


# -- Layer 4: real production capture ----------------------------------------
def test_production_capture_grep_search_shape():
    """Real args shape captured from app.out.log during the drop incident:
    grep_search with an unquoted Includes value. The batch parser must accept
    the corrected JSON form without regressing."""
    body = json.dumps([
        {"name": "grep_search",
         "arguments": {"Includes": "*.py", "MatchPerLine": True, "Query": "410",
                       "SearchPath": "D:\\\\Projects\\\\Chat2API\\\\app"}},
        {"name": "view_file",
         "arguments": {"AbsolutePath": "D:\\\\Projects\\\\Chat2API\\\\app\\\\main.py"}},
    ])
    calls = _parse_tool_call_block_multi(body)
    assert len(calls) == 2
    args = json.loads(calls[0]["function"]["arguments"])
    assert args["Includes"] == "*.py"
    assert args["MatchPerLine"] is True


def test_id_seeding_matches_helper():
    """Ids must be reproducible across processes (sha1, not PYTHONHASHSEED-
    randomized hash) so retries correlate."""
    calls = _parse_tool_call_block_multi(ARR)
    for i, c in enumerate(calls):
        assert c["id"] == _make_tool_call_id(i, NAMES[i], c["function"]["arguments"])


# -- Layer 4b: the 5th dialect -- ChatML tool_use/invoke/parameter ----------
# Defect (fixed 2026-09-23): GLM-5.3-flash-max via the chat2api guest lane
# emits tool calls in a ChatML dialect that the four existing parsers all
# miss:
#
#   <tool_use><invoke name=get_weather><parameter name=city>Hanoi</parameter>
#   </invoke></tool_use>
#
# With no parser matching, normalize_glm_tool_calls left tool_calls empty and
# the whole batch was dropped (the recurring GLM-5.3 tool-batch drop bug). The
# invoke parser recovers every <invoke> in a block (so a 2+ invoke batch keeps
# all members), coerces parameter values that are valid JSON to native types,
# skips invokes with no usable name, and stays fail-open on malformed input.

# VERBATIM live capture (real newlines, exactly as captured on the wire).
LIVE_CAPTURE = (
    LT + "tool_use" + GT + "\n"
    + LT + "invoke name=get_weather" + GT + "\n"
    + LT + "parameter name=city" + GT + "Hanoi" + LT + "/parameter" + GT + "\n"
    + LT + "/invoke" + GT + "\n"
    + LT + "/tool_use" + GT
)


def test_invoke_dialect_live_capture_recovers_one_call():
    """The verbatim production capture must yield exactly one call with the
    captured name and arguments -- not an empty batch (the drop symptom)."""
    calls = _parse_tool_call_block_multi(LIVE_CAPTURE)
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "get_weather"
    assert json.loads(calls[0]["function"]["arguments"]) == {"city": "Hanoi"}


def test_invoke_dialect_batch_two_invokes_distinct_ids():
    """One tool_use wrapper holding TWO invokes is a parallel batch: both
    members must be recovered with distinct ids and correct arguments each."""
    body = (
        TOOL_USE
        + LT + "invoke name=read_file" + GT
        + LT + "parameter name=path" + GT + "a.txt" + LT + "/parameter" + GT
        + LT + "/invoke" + GT
        + LT + "invoke name=read_file" + GT
        + LT + "parameter name=path" + GT + "b.txt" + LT + "/parameter" + GT
        + LT + "/invoke" + GT
        + TOOL_USE_CLOSE
    )
    calls = _parse_tool_call_block_multi(body)
    assert len(calls) == 2
    ids = [c["id"] for c in calls]
    assert len(set(ids)) == len(ids), "colliding ids: %s" % ids
    assert [c["function"]["name"] for c in calls] == ["read_file", "read_file"]
    assert json.loads(calls[0]["function"]["arguments"]) == {"path": "a.txt"}
    assert json.loads(calls[1]["function"]["arguments"]) == {"path": "b.txt"}


def test_invoke_dialect_bare_invoke_without_wrapper_still_recovered():
    """A bare invoke with no outer tool_use wrapper must still be recovered --
    the invoke parser scans the whole block text, not just wrapped content."""
    body = (
        LT + "invoke name=get_weather" + GT
        + LT + "parameter name=city" + GT + "Hanoi" + LT + "/parameter" + GT
        + LT + "/invoke" + GT
    )
    calls = _parse_tool_call_block_multi(body)
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "get_weather"
    assert json.loads(calls[0]["function"]["arguments"]) == {"city": "Hanoi"}


def test_invoke_dialect_parameter_value_valid_json_becomes_native():
    """A parameter value that is itself valid JSON parses to the native type,
    not a stringified blob: options here must be a dict."""
    body = (
        TOOL_USE
        + LT + "invoke name=search" + GT
        + LT + "parameter name=options" + GT
        + json.dumps({"limit": 5})
        + LT + "/parameter" + GT
        + LT + "/invoke" + GT
        + TOOL_USE_CLOSE
    )
    calls = _parse_tool_call_block_multi(body)
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "search"
    opts = json.loads(calls[0]["function"]["arguments"])["options"]
    assert opts == {"limit": 5}
    assert isinstance(opts, dict), "valid-JSON value must be native, not a string"


def test_invoke_dialect_unquoted_attributes_recovered():
    """Production capture uses unquoted attribute values (name=get_weather);
    the parser must accept them exactly as it accepts quoted ones."""
    body = (
        LT + "invoke name=get_weather" + GT
        + LT + "parameter name=city" + GT + "Hanoi" + LT + "/parameter" + GT
        + LT + "/invoke" + GT
    )
    calls = _parse_tool_call_block_multi(body)
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "get_weather"
    assert json.loads(calls[0]["function"]["arguments"]) == {"city": "Hanoi"}


def test_invoke_dialect_malformed_fail_open_no_crash():
    """Fail-open contract: an unclosed invoke or an invoke missing its name
    yields no calls and never raises."""
    # Unclosed invoke (no closing tag) -> nothing matches, no calls.
    unclosed = (
        TOOL_USE
        + LT + "invoke name=x" + GT
        + LT + "parameter name=city" + GT + "Hanoi" + LT + "/parameter" + GT
        + TOOL_USE_CLOSE
    )
    assert _parse_tool_call_block_multi(unclosed) == []
    assert _calls_from_invoke_block(unclosed) == []
    # Invoke present but nameless -> skipped, no calls.
    nameless = (
        TOOL_USE
        + LT + "invoke" + GT
        + LT + "parameter name=city" + GT + "Hanoi" + LT + "/parameter" + GT
        + LT + "/invoke" + GT
        + TOOL_USE_CLOSE
    )
    assert _parse_tool_call_block_multi(nameless) == []
    assert _calls_from_invoke_block(nameless) == []


def test_streaming_rescue_recovers_invoke_batch():
    """The streaming rescue path (parse_streamed_tool_block) recovers a
    2-invoke batch with distinct ids and correct arguments, mirroring the
    buffered path."""
    body = (
        LT + "invoke name=read_file" + GT
        + LT + "parameter name=path" + GT + "a.txt" + LT + "/parameter" + GT
        + LT + "/invoke" + GT
        + LT + "invoke name=read_file" + GT
        + LT + "parameter name=path" + GT + "b.txt" + LT + "/parameter" + GT
        + LT + "/invoke" + GT
    )
    calls = parse_streamed_tool_block(body, unicode_path=False)
    assert len(calls) == 2
    ids = [c["id"] for c in calls]
    assert len(set(ids)) == len(ids), "colliding ids: %s" % ids
    assert [c["function"]["name"] for c in calls] == ["read_file", "read_file"]
    assert json.loads(calls[0]["function"]["arguments"]) == {"path": "a.txt"}
    assert json.loads(calls[1]["function"]["arguments"]) == {"path": "b.txt"}


def test_invoke_dialect_quoted_attributes_recovered():
    """Quoted attribute values (double or single quoted) are accepted too, so a
    reseller that quotes the name still round-trips."""
    body = (
        TOOL_USE
        + LT + 'invoke name="get_weather"' + GT
        + LT + "parameter name='city'" + GT + "Hanoi" + LT + "/parameter" + GT
        + LT + "/invoke" + GT
        + TOOL_USE_CLOSE
    )
    calls = _parse_tool_call_block_multi(body)
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "get_weather"
    assert json.loads(calls[0]["function"]["arguments"]) == {"city": "Hanoi"}


def test_invoke_dialect_normalize_extracts_and_strips():
    """End-to-end on the buffered path: normalize_glm_tool_calls lifts a
    tool_use/invoke batch into structured tool_calls, strips the wrapper from
    content, and promotes finish_reason."""
    body = LIVE_CAPTURE
    payload = {"choices": [{
        "index": 0,
        "message": {"role": "assistant", "content": body},
        "finish_reason": "stop",
    }]}
    out, changed = normalize_glm_tool_calls(payload, "glm-5.3")
    assert changed is True
    tcs = out["choices"][0]["message"]["tool_calls"]
    assert len(tcs) == 1
    assert tcs[0]["function"]["name"] == "get_weather"
    assert json.loads(tcs[0]["function"]["arguments"]) == {"city": "Hanoi"}
    content = out["choices"][0]["message"]["content"]
    assert content is None or "tool_use" not in content
    assert out["choices"][0]["finish_reason"] == "tool_calls"
