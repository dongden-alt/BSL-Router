"""Regression tests for 2026-10-30 BSL Router reliability & tool-call fixes (REQ-1..REQ-4)."""
from __future__ import annotations

import copy
import json

from app import codex_adapter, main
from app.compat.adapters.gemini import (
    bound_tool_call_id,
    gemini_request_to_openai,
    normalize_model,
    unwrap_request,
)
from app.compat.families import resolve_thinking
from app.middleware.quality import TRANSPORT_DIED_PARTIAL_FLAG, should_splice_continuation
from app.routing.combo_resolver import (
    _parse_chain_entry,
    resolve_alias,
    resolve_combo,
    resolve_combo_alias_redirect,
)


# ---------------------------------------------------------------------------
# REQ-1: Post-content explicit SSE failure -> same-provider continuation flag
# ---------------------------------------------------------------------------

def test_req1_explicit_sse_failure_event_detection():
    sc1, msg1 = main._explicit_sse_failure_event(
        {"type": "response.failed", "response": {"error": {"message": "no upstream provider could serve this request"}}}
    )
    assert sc1 == 503
    assert msg1 == "no upstream provider could serve this request"

    sc2, msg2 = main._explicit_sse_failure_event(
        {"type": "error", "error": {"message": "upstream 503 midstream"}}
    )
    assert sc2 == 503
    assert msg2 == "upstream 503 midstream"

    assert main._explicit_sse_failure_event({"type": "response.output_text.delta", "delta": "hi"}) is None


def test_req1_flag_post_content_explicit_sse_failure_arms_continuation():
    stats_post: dict = {"output_tokens": 260, "tool_calls_count": 0, "status": 503, "error": "no upstream provider could serve this request"}
    main._flag_post_content_explicit_sse_failure(stats_post, None)
    assert stats_post.get(TRANSPORT_DIED_PARTIAL_FLAG) is True

    # Verify should_splice_continuation triggers same-provider continuation when flag is set
    assert should_splice_continuation(
        truncated=False,
        transport_died_partial=bool(stats_post.get(TRANSPORT_DIED_PARTIAL_FLAG)),
        partial_text="Partial response from upstream before midstream 503...",
        infinite_retry_enabled=True,
        used=False,
    ) is True
    # And respects infinite_retry_enabled=False opt-out
    assert should_splice_continuation(
        truncated=False,
        transport_died_partial=bool(stats_post.get(TRANSPORT_DIED_PARTIAL_FLAG)),
        partial_text="Partial response from upstream before midstream 503...",
        infinite_retry_enabled=False,
        used=False,
    ) is False


# ---------------------------------------------------------------------------
# REQ-2: Tool-call/result correlation & multiplicity
# ---------------------------------------------------------------------------

def test_req2_codex_adapter_duplicate_call_id_count_mismatch_synthesizes_missing_output():
    """Two function_call items with the same call_id ('dup_id') and only one
    function_call_output ('dup_id') must synthesize the second output so count
    parity holds (set-only comparison previously missed this)."""
    chat_body = {
        "model": "gpt-5.6-sol",
        "messages": [
            {"role": "user", "content": "run two tools"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "dup_id", "type": "function", "function": {"name": "read_file", "arguments": '{"Path":"a.py"}'}},
                    {"id": "dup_id", "type": "function", "function": {"name": "read_file", "arguments": '{"Path":"b.py"}'}},
                ],
            },
            {"role": "tool", "tool_call_id": "dup_id", "content": "first output"},
        ],
    }
    resp = codex_adapter.openai_to_responses(chat_body)
    fcs = [x for x in resp["input"] if x.get("type") == "function_call" and x.get("call_id") == "dup_id"]
    fcos = [x for x in resp["input"] if x.get("type") == "function_call_output" and x.get("call_id") == "dup_id"]
    assert len(fcs) == 2
    assert len(fcos) == 2


def test_req2_codex_adapter_1to1_unambiguous_orphan_output_reconciles_call_id():
    """When 1 function_call is missing its output and 1 orphan function_call_output
    has a mismatched call_id, reconcile the 1:1 pair by rewriting the orphan output's
    call_id to the missing call's call_id instead of synthesizing a fake error."""
    chat_body = {
        "model": "gpt-5.6-sol",
        "messages": [
            {"role": "user", "content": "read file"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "call_expected_1", "type": "function", "function": {"name": "read_file", "arguments": '{"Path":"a.py"}'}},
                ],
            },
            {"role": "tool", "tool_call_id": "call_mismatched_99", "content": "real file contents"},
        ],
    }
    resp = codex_adapter.openai_to_responses(chat_body)
    fcs = [x for x in resp["input"] if x.get("type") == "function_call"]
    fcos = [x for x in resp["input"] if x.get("type") == "function_call_output"]
    assert len(fcs) == 1 and len(fcos) == 1
    assert fcos[0]["call_id"] == "call_expected_1"
    assert fcos[0]["output"] == "real file contents"


def test_req2_gemini_ingress_preserves_and_bounds_explicit_ids():
    """Explicit functionCall.id and functionResponse.id on Gemini parts must be
    preserved (and bounded via bound_tool_call_id if >64 chars) and paired by id
    even when two same-named calls are answered out of order."""
    long_id_1 = "explicit_call_alpha_" + ("x" * 60)
    long_id_2 = "explicit_call_beta_" + ("y" * 60)
    wrapper = {
        "model": "models/gemini-3.1-pro-high",
        "request": {
            "contents": [
                {"role": "user", "parts": [{"text": "read two files"}]},
                {
                    "role": "model",
                    "parts": [
                        {"functionCall": {"id": long_id_1, "name": "read_file", "args": {"Path": "one.py"}}},
                        {"functionCall": {"id": long_id_2, "name": "read_file", "args": {"Path": "two.py"}}},
                    ],
                },
                {
                    "role": "user",
                    "parts": [
                        # Answered in reverse order using explicit IDs on the same tool name:
                        {"functionResponse": {"id": long_id_2, "name": "read_file", "response": {"result": "two_content"}}},
                        {"functionResponse": {"id": long_id_1, "name": "read_file", "response": {"result": "one_content"}}},
                    ],
                },
            ]
        },
    }
    ob = gemini_request_to_openai(unwrap_request(wrapper), normalize_model(wrapper["model"]))
    asst = ob["messages"][1]
    expected_id_1 = bound_tool_call_id(long_id_1)
    expected_id_2 = bound_tool_call_id(long_id_2)
    assert len(expected_id_1) <= 64 and len(expected_id_2) <= 64
    assert [tc["id"] for tc in asst["tool_calls"]] == [expected_id_1, expected_id_2]

    tool_msgs = [m for m in ob["messages"] if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == [expected_id_2, expected_id_1]
    assert json.loads(tool_msgs[0]["content"]) == "two_content"
    assert json.loads(tool_msgs[1]["content"]) == "one_content"


# ---------------------------------------------------------------------------
# REQ-3: AgentRouter GPT-5.6 / GPT-6.1 function-tool reasoning_effort policy
# ---------------------------------------------------------------------------

_SAMPLE_FUNC_TOOLS = [
    {"type": "function", "function": {"name": "get_weather", "parameters": {"type": "object"}}}
]


def test_req3_agentrouter_gpt56_and_gpt61_with_function_tools_emit_none():
    for f_val, model in [
        ("agentrouter/gpt-5.6-sol", "gpt-5.6-sol"),
        ("agentrouter-o/gpt-5.6-sol", "gpt-5.6-sol"),
        ("agentrouter/gpt-6.1-sol", "gpt-6.1-sol"),
        ("agentrouter-o/gpt-6.1", "gpt-6.1"),
    ]:
        body, _ = resolve_thinking(
            {
                "model": model,
                "messages": [{"role": "user", "content": "hi"}],
                "tools": copy.deepcopy(_SAMPLE_FUNC_TOOLS),
                "reasoning": {"mode": "pro", "effort": "high"},
            },
            f_val,
            "xhigh",
            reasoning_mode="pro",
            reasoning_context="all_turns",
        )
        assert body.get("reasoning_effort") == "none", f"failed for {f_val}"
        assert "reasoning" not in body, f"nested reasoning not stripped for {f_val}"


def test_req3_non_agentrouter_gpt6_astra_with_tools_omits_reasoning_controls():
    body, _ = resolve_thinking(
        {
            "model": "gpt-6-astra",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": copy.deepcopy(_SAMPLE_FUNC_TOOLS),
            "reasoning_effort": "high",
            "reasoning": {"effort": "high"},
        },
        "vsllm-r/gpt-6-astra",
        "high",
    )
    assert "reasoning_effort" not in body
    assert "reasoning" not in body


def test_req3_anthropic_wire_with_tools_never_emits_openai_reasoning_effort():
    body, _ = resolve_thinking(
        {
            "model": "gpt-6.1-sol",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": copy.deepcopy(_SAMPLE_FUNC_TOOLS),
            "reasoning_effort": "high",
        },
        "agentrouter-o/gpt-6.1-sol",
        "high",
        wire_format="anthropic",
    )
    assert "reasoning_effort" not in body
    assert "reasoning" not in body


# ---------------------------------------------------------------------------
# REQ-4: Recursive alias resolution with cycle detection
# ---------------------------------------------------------------------------

def test_req4_multi_hop_alias_resolution_and_cycle_safety():
    config = {
        "providers": {
            "prov_a": {
                "models": [{"id": "concrete-m1", "enabled": True}],
                "connections": [{"api_key": "k1", "enabled": True}],
            }
        },
        "aliases": {
            "hop1": {"model": "hop2", "thinking": "high"},
            "hop2": {"model": "hop3", "thinking": "low"},
            "hop3": {"model": "concrete-m1", "provider": "prov_a"},
            "bare_hop1": {"model": "bare_hop2"},
            "bare_hop2": {"model": "concrete-m1"},
            "to_combo_1": {"model": "to_combo_2"},
            "to_combo_2": {"model": "my-combo", "provider": "__combo__"},
            "cycle_a": {"model": "cycle_b"},
            "cycle_b": {"model": "cycle_a"},
        },
        "combos": [
            {
                "alias": "my-combo",
                "strategy": "fallback",
                "chain": ["hop1"],
            }
        ],
    }

    # 3-hop alias -> concrete provider/model
    t_mod, t_prov = resolve_alias("hop1", None, config)
    assert (t_mod, t_prov) == ("concrete-m1", "prov_a")

    # _parse_chain_entry preserves outer/first thinking override across hops
    cm, c_target, c_prov, c_think = _parse_chain_entry("hop1", config)
    assert (c_target, c_prov, c_think) == ("concrete-m1", "prov_a", "high")

    # Multi-hop alias -> bare model resolves in combo chain via provider scan
    combo_res = resolve_combo("my-combo", None, config, {})
    assert combo_res.matched is True
    assert combo_res.active_chain == [("concrete-m1", "prov_a", "high")]

    # Multi-hop alias -> combo redirect
    redirected, did_redirect = resolve_combo_alias_redirect("to_combo_1", None, config)
    assert (redirected, did_redirect) == ("my-combo", True)

    # Bare multi-hop alias without provider returns target_model so Step 2 scans target_model
    b_mod, b_prov = resolve_alias("bare_hop1", None, config)
    assert (b_mod, b_prov) == ("concrete-m1", None)

    # 2-cycle terminates deterministically without raising
    c_mod, c_prov = resolve_alias("cycle_a", None, config)
    assert c_mod in ("cycle_a", "cycle_b")
    assert c_prov is None
