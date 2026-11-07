# -*- coding: utf-8 -*-
"""Isolated Sandbox Simulation for Blacksand Studio (Antigravity IDE `v1beta`/`v1internal`
Gemini wire) & Claude Code CLI (`/v1/messages` Anthropic wire + `/v1/chat/completions`
OpenAI wire) across:
  1. `codex/*` OAuth (`codex/gpt-5.6-codex`, `codex/gpt-5.3-codex`, `codex/gpt-5.6-sol`)
  2. `antigravity/*` OAuth (`antigravity/claude-sonnet-4-6`, `antigravity/gemini-3.1-pro-high`)
  3. `agentrouter/*` (`agentrouter/gpt-5.6-sol`, `agentrouter-o/gpt-5.6-sol`,
     `agentrouter/gpt-6.1-sol`, `agentrouter-o/gpt-6.1-sol`)
  4. `chat2api/*` (all 20 Chat2API coding account lanes routed through BSL Router)

Verifies a full 3-turn, 5-tool-call coding session (`read_file`, `grep_search`,
`run_command` -> `edit_file`, `run_command` -> final synthesis) with zero tool-call
schema, pairing, index-collapse, or truncation errors.
"""
from __future__ import annotations

import asyncio
import copy
import json
from typing import Any, Dict, List

import pytest

from app import codex_adapter
from app.compat.adapters.gemini import (
    _new_state,
    bound_tool_call_id,
    extract_openai_tool_schemas,
    gemini_request_to_openai,
    normalize_model,
    openai_chunk_to_gemini,
    openai_response_to_gemini,
    unwrap_request,
)
from app.compat.families import resolve_thinking
from app.middleware.agentrouter_policy import agentrouter_nfkd_transcode
from app.middleware.anthropic_tools import (
    AnthropicStreamToolGuard,
    repair_anthropic_response_tool_uses,
)
from app.middleware.glm_tools import normalize_glm_tool_calls


GEMINI_TOOLS_DECLARATION = [
    {
        "functionDeclarations": [
            {
                "name": "read_file",
                "description": "Read file contents",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "Path": {"type": "STRING"},
                        "StartLine": {"type": "INTEGER"},
                        "EndLine": {"type": "INTEGER"},
                    },
                    "required": ["Path"],
                },
            },
            {
                "name": "grep_search",
                "description": "Search file contents",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "SearchPath": {"type": "STRING"},
                        "Query": {"type": "STRING"},
                    },
                    "required": ["SearchPath", "Query"],
                },
            },
            {
                "name": "run_command",
                "description": "Run a shell command",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "CommandLine": {"type": "STRING"},
                        "WaitMsBeforeAsync": {"type": "INTEGER"},
                        "toolSummary": {"type": "STRING"},
                    },
                    "required": ["CommandLine", "WaitMsBeforeAsync", "toolSummary"],
                },
            },
            {
                "name": "edit_file",
                "description": "Edit a file",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "Path": {"type": "STRING"},
                        "Target": {"type": "STRING"},
                        "Replacement": {"type": "STRING"},
                    },
                    "required": ["Path", "Target", "Replacement"],
                },
            },
        ]
    }
]


def _make_codex_sse_bytes(events: List[tuple[str, Dict[str, Any]]]) -> List[bytes]:
    chunks: List[bytes] = []
    for ev_name, payload in events:
        frame = f"event: {ev_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        chunks.append(frame.encode("utf-8"))
    return chunks


async def _aiter_bytes(chunks: List[bytes]):
    for c in chunks:
        yield c


# ---------------------------------------------------------------------------
# 1. Codex OAuth (`codex/*`) — Blacksand Studio & Claude Code 3-Turn Sandbox
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "codex_model",
    ["codex/gpt-5.6-codex", "codex/gpt-5.3-codex", "codex/gpt-5.6-sol"],
)
def test_codex_oauth_blacksand_studio_and_claude_code_3turn_sandbox(codex_model: str):
    """Simulate a 3-turn, 5-tool-call coding session on Codex OAuth (`codex/*`)
    from Blacksand Studio (`v1internal` Gemini wire) and Claude Code (`/v1/messages`)."""

    async def _run():
        # ── Turn 1: Blacksand Studio sends Gemini request with 4 tools ──
        gem_contents: List[Dict[str, Any]] = [
            {"role": "user", "parts": [{"text": "Inspect toolbridge.py, grep parse_tool_uses, and run pytest."}]},
        ]
        wrapper_t1 = {
            "model": f"models/{codex_model}",
            "request": {
                "systemInstruction": {"parts": [{"text": "You are Antigravity."}]},
                "contents": copy.deepcopy(gem_contents),
                "tools": copy.deepcopy(GEMINI_TOOLS_DECLARATION),
            },
        }
        oai_req_t1 = gemini_request_to_openai(unwrap_request(wrapper_t1), normalize_model(wrapper_t1["model"]))
        tool_schemas = extract_openai_tool_schemas(oai_req_t1.get("tools"))

        # Convert OpenAI request -> Codex /responses body
        codex_body_t1 = codex_adapter.openai_to_responses(copy.deepcopy(oai_req_t1))
        assert codex_body_t1["stream"] is True
        assert codex_body_t1["store"] is False
        assert len(codex_body_t1["tools"]) == 4

        # Codex upstream streams 3 parallel function_call items (`response.output_item.done`)
        t1_fc_items = [
            {
                "type": "function_call",
                "call_id": "call_codex_read_1",
                "name": "read_file",
                # Intentionally uses lowercase alias `path` to verify Antigravity schema canonicalization -> `Path`
                "arguments": json.dumps({"path": "app/toolbridge.py", "StartLine": 1, "EndLine": 50}),
            },
            {
                "type": "function_call",
                "call_id": "call_codex_grep_2",
                "name": "grep_search",
                "arguments": json.dumps({"SearchPath": "app/toolbridge.py", "Query": "lines[-14:]"}),
            },
            {
                "type": "function_call",
                "call_id": "call_codex_cmd_3",
                "name": "run_command",
                # Intentionally omits WaitMsBeforeAsync & toolSummary to verify auto-injection on run_command ONLY
                "arguments": json.dumps({"command": "pytest -q"}),
            },
        ]
        codex_sse_t1 = _make_codex_sse_bytes([
            ("response.output_item.done", {"type": "response.output_item.done", "item": t1_fc_items[0]}),
            ("response.output_item.done", {"type": "response.output_item.done", "item": t1_fc_items[1]}),
            ("response.output_item.done", {"type": "response.output_item.done", "item": t1_fc_items[2]}),
            ("response.completed", {"type": "response.completed", "response": {"model": codex_model, "output": t1_fc_items, "usage": {"input_tokens": 120, "output_tokens": 45}}}),
        ])

        state_t1 = _new_state()
        state_t1["tool_schemas"] = tool_schemas
        state_t1["is_antigravity"] = True
        gem_frames_t1: List[Dict[str, Any]] = []
        async for raw_line in codex_adapter.responses_sse_to_openai_sse(_aiter_bytes(codex_sse_t1)):
            line_str = raw_line.decode("utf-8").strip()
            if line_str.startswith("data:"):
                chunk_obj = json.loads(line_str[5:].strip())
                gframe = openai_chunk_to_gemini(chunk_obj, state_t1)
                if gframe:
                    gem_frames_t1.append(gframe)

        # Extract emitted functionCall parts from Turn 1 Gemini stream
        fcs_t1 = [
            p["functionCall"]
            for gf in gem_frames_t1
            for cand in gf["response"]["candidates"]
            for p in cand["content"]["parts"]
            if "functionCall" in p
        ]
        assert [fc["name"] for fc in fcs_t1] == ["read_file", "grep_search", "run_command"]
        # Verify strict Antigravity schema compliance:
        # 1. `read_file` has `Path` (canonicalized from `path`) and ZERO `toolAction` / `toolSummary`
        assert fcs_t1[0]["args"] == {"Path": "app/toolbridge.py", "StartLine": 1, "EndLine": 50}
        assert "toolAction" not in fcs_t1[0]["args"] and "toolSummary" not in fcs_t1[0]["args"]
        # 2. `grep_search` preserves `lines[-14:]` and has ZERO `toolAction` / `toolSummary`
        assert fcs_t1[1]["args"] == {"SearchPath": "app/toolbridge.py", "Query": "lines[-14:]"}
        assert "toolAction" not in fcs_t1[1]["args"] and "toolSummary" not in fcs_t1[1]["args"]
        # 3. `run_command` has `CommandLine`, `WaitMsBeforeAsync`, `toolSummary`, and ZERO `toolAction`
        assert fcs_t1[2]["args"]["CommandLine"] == "pytest -q"
        assert isinstance(fcs_t1[2]["args"]["WaitMsBeforeAsync"], int)
        assert isinstance(fcs_t1[2]["args"]["toolSummary"], str) and fcs_t1[2]["args"]["toolSummary"]
        assert "toolAction" not in fcs_t1[2]["args"]

        # ── Turn 2: Blacksand Studio sends Turn 1 functionCall + functionResponse history ──
        gem_contents.append({"role": "model", "parts": [{"functionCall": fc} for fc in fcs_t1]})
        gem_contents.append({
            "role": "user",
            "parts": [
                {"functionResponse": {"name": "read_file", "response": {"result": "MARKER_READ_001"}}},
                {"functionResponse": {"name": "grep_search", "response": {"result": "MARKER_GREP_002"}}},
                {"functionResponse": {"name": "run_command", "response": {"result": "MARKER_CMD_003"}}},
            ],
        })
        wrapper_t2 = {
            "model": f"models/{codex_model}",
            "request": {
                "systemInstruction": {"parts": [{"text": "You are Antigravity."}]},
                "contents": copy.deepcopy(gem_contents),
                "tools": copy.deepcopy(GEMINI_TOOLS_DECLARATION),
            },
        }
        oai_req_t2 = gemini_request_to_openai(unwrap_request(wrapper_t2), normalize_model(wrapper_t2["model"]))
        codex_body_t2 = codex_adapter.openai_to_responses(copy.deepcopy(oai_req_t2))
        fc_in_t2 = [x for x in codex_body_t2["input"] if x.get("type") == "function_call"]
        fco_in_t2 = [x for x in codex_body_t2["input"] if x.get("type") == "function_call_output"]
        assert len(fc_in_t2) == 3 and len(fco_in_t2) == 3
        assert [x["call_id"] for x in fc_in_t2] == [x["call_id"] for x in fco_in_t2]

        # Codex streams Turn 2 parallel tool calls (`edit_file`, `run_command`)
        t2_fc_items = [
            {
                "type": "function_call",
                "call_id": "call_codex_edit_4",
                "name": "edit_file",
                "arguments": json.dumps({"Path": "app/toolbridge.py", "Target": "old()", "Replacement": "new()"}),
            },
            {
                "type": "function_call",
                "call_id": "call_codex_cmd_5",
                "name": "run_command",
                "arguments": json.dumps({"CommandLine": "pytest -q", "WaitMsBeforeAsync": 5000, "toolSummary": "Run pytest"}),
            },
        ]
        codex_sse_t2 = _make_codex_sse_bytes([
            ("response.output_item.done", {"type": "response.output_item.done", "item": t2_fc_items[0]}),
            ("response.output_item.done", {"type": "response.output_item.done", "item": t2_fc_items[1]}),
            ("response.completed", {"type": "response.completed", "response": {"model": codex_model, "output": t2_fc_items, "usage": {"input_tokens": 220, "output_tokens": 40}}}),
        ])
        state_t2 = _new_state()
        state_t2["tool_schemas"] = tool_schemas
        state_t2["is_antigravity"] = True
        gem_frames_t2: List[Dict[str, Any]] = []
        async for raw_line in codex_adapter.responses_sse_to_openai_sse(_aiter_bytes(codex_sse_t2)):
            line_str = raw_line.decode("utf-8").strip()
            if line_str.startswith("data:"):
                gframe = openai_chunk_to_gemini(json.loads(line_str[5:].strip()), state_t2)
                if gframe:
                    gem_frames_t2.append(gframe)

        fcs_t2 = [
            p["functionCall"]
            for gf in gem_frames_t2
            for cand in gf["response"]["candidates"]
            for p in cand["content"]["parts"]
            if "functionCall" in p
        ]
        assert [fc["name"] for fc in fcs_t2] == ["edit_file", "run_command"]

        # ── Turn 3: Final synthesis after 5 tool calls ──
        gem_contents.append({"role": "model", "parts": [{"functionCall": fc} for fc in fcs_t2]})
        gem_contents.append({
            "role": "user",
            "parts": [
                {"functionResponse": {"name": "edit_file", "response": {"result": "MARKER_EDIT_004"}}},
                {"functionResponse": {"name": "run_command", "response": {"result": "MARKER_CMD_005"}}},
            ],
        })
        wrapper_t3 = {
            "model": f"models/{codex_model}",
            "request": {
                "systemInstruction": {"parts": [{"text": "You are Antigravity."}]},
                "contents": copy.deepcopy(gem_contents),
                "tools": copy.deepcopy(GEMINI_TOOLS_DECLARATION),
            },
        }
        oai_req_t3 = gemini_request_to_openai(unwrap_request(wrapper_t3), normalize_model(wrapper_t3["model"]))
        codex_body_t3 = codex_adapter.openai_to_responses(copy.deepcopy(oai_req_t3))
        fc_in_t3 = [x for x in codex_body_t3["input"] if x.get("type") == "function_call"]
        fco_in_t3 = [x for x in codex_body_t3["input"] if x.get("type") == "function_call_output"]
        assert len(fc_in_t3) == 5 and len(fco_in_t3) == 5
        assert [x["call_id"] for x in fc_in_t3] == [x["call_id"] for x in fco_in_t3]

        codex_sse_t3 = _make_codex_sse_bytes([
            ("response.output_text.delta", {"type": "response.output_text.delta", "delta": "All 5 tool calls verified cleanly."}),
            ("response.completed", {"type": "response.completed", "response": {"model": codex_model, "output": [], "usage": {"input_tokens": 310, "output_tokens": 12}}}),
        ])
        assembled_t3 = await codex_adapter.assemble_responses_from_sse(_aiter_bytes(codex_sse_t3))
        assert any(
            c.get("text") == "All 5 tool calls verified cleanly."
            for item in assembled_t3.get("output", [])
            for c in item.get("content", [])
            if isinstance(c, dict)
        )

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# 2. Antigravity OAuth (`antigravity/*`) — Thought Signatures & Mid-Reasoning Filter
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "ag_model",
    ["antigravity/claude-sonnet-4-6", "antigravity/gemini-3.1-pro-high"],
)
def test_antigravity_oauth_blacksand_studio_and_claude_code_sandbox(ag_model: str):
    """Verify Antigravity OAuth (`antigravity/*`) preserves `thoughtSignature` across
    multi-turn parallel tool calls, filters phantom mid-`<thinking>` tool calls,
    and bounds >64-char MCP tool_call_ids."""
    long_mcp_id = "call_mcp_bsl_code_atlas_search_graph_" + ("9" * 45)
    assert len(long_mcp_id) > 64

    wrapper = {
        "model": f"models/{ag_model}",
        "request": {
            "contents": [
                {"role": "user", "parts": [{"text": "Read and edit file"}]},
                {
                    "role": "model",
                    "parts": [
                        {
                            "functionCall": {"id": long_mcp_id, "name": "read_file", "args": {"Path": "app/main.py"}},
                            "thoughtSignature": "sig-gemini31-turn1-alpha",
                        },
                        {
                            "functionCall": {"name": "grep_search", "args": {"SearchPath": "app", "Query": "def main"}},
                            "thoughtSignature": "sig-gemini31-turn1-beta",
                        },
                    ],
                },
                {
                    "role": "user",
                    "parts": [
                        {
                            "functionResponse": {"id": long_mcp_id, "name": "read_file", "response": {"result": "line 1\nline 2"}},
                            "thoughtSignature": "sig-gemini31-resp-alpha",
                        },
                        {
                            "functionResponse": {"name": "grep_search", "response": {"result": "app/main.py:10"}},
                        },
                    ],
                },
            ],
            "tools": copy.deepcopy(GEMINI_TOOLS_DECLARATION),
        },
    }
    oai_req = gemini_request_to_openai(unwrap_request(wrapper), normalize_model(wrapper["model"]))
    tool_schemas = extract_openai_tool_schemas(oai_req.get("tools"))

    asst_msg = oai_req["messages"][1]
    assert len(asst_msg["tool_calls"]) == 2
    assert len(asst_msg["tool_calls"][0]["id"]) <= 64
    assert asst_msg["tool_calls"][0]["id"] == bound_tool_call_id(long_mcp_id)
    assert asst_msg["tool_calls"][0]["thought_signature"] == "sig-gemini31-turn1-alpha"
    assert asst_msg["tool_calls"][1]["thought_signature"] == "sig-gemini31-turn1-beta"

    tool_msgs = [m for m in oai_req["messages"] if m["role"] == "tool"]
    assert len(tool_msgs) == 2
    assert tool_msgs[0]["tool_call_id"] == asst_msg["tool_calls"][0]["id"]
    assert tool_msgs[1]["tool_call_id"] == asst_msg["tool_calls"][1]["id"]

    # Simulate Turn 2 streaming response where a thinking model quotes a phantom tool call
    # mid-reasoning before emitting the real post-reasoning parallel tool calls (`edit_file`, `run_command`).
    state = _new_state()
    state["tool_schemas"] = tool_schemas
    state["is_antigravity"] = True

    stream_chunks = [
        {"choices": [{"index": 0, "delta": {"reasoning_content": "Let me check previous <tool_use>read_file</tool_use>..."}, "finish_reason": None}]},
        # Phantom tool call emitted mid-reasoning:
        {
            "choices": [{
                "index": 0,
                "delta": {
                    "tool_calls": [{
                        "index": 0,
                        "id": "call_phantom_mid_think",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"Path": "nonexistent.py"}'},
                    }]
                },
                "finish_reason": None,
            }]
        },
        # Subsequent reasoning delta marks the earlier slot as `from_reasoning=True`:
        {"choices": [{"index": 0, "delta": {"reasoning_content": "Now I will apply the real edit and run pytest."}, "finish_reason": None}]},
        # Real post-reasoning parallel tool calls:
        {
            "choices": [{
                "index": 0,
                "delta": {
                    "tool_calls": [
                        {
                            "index": 1,
                            "id": "call_real_edit",
                            "type": "function",
                            "function": {"name": "edit_file", "arguments": '{"Path": "app/main.py", "Target": "a", "Replacement": "b"}'},
                            "thought_signature": "sig-turn2-edit",
                        },
                        {
                            "index": 2,
                            "id": "call_real_cmd",
                            "type": "function",
                            "function": {"name": "run_command", "arguments": '{"CommandLine": "pytest -q"}'},
                            "thought_signature": "sig-turn2-cmd",
                        },
                    ]
                },
                "finish_reason": None,
            }]
        },
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ]

    emitted_frames = [
        gf for ch in stream_chunks
        if (gf := openai_chunk_to_gemini(ch, state)) is not None
    ]
    emitted_fcs = [
        p
        for gf in emitted_frames
        for cand in gf["response"]["candidates"]
        for p in cand["content"]["parts"]
        if "functionCall" in p
    ]
    # Phantom mid-reasoning tool call is filtered out; only the 2 real calls remain with their thoughtSignatures!
    assert [p["functionCall"]["name"] for p in emitted_fcs] == ["edit_file", "run_command"]
    assert emitted_fcs[0]["thoughtSignature"] == "sig-turn2-edit"
    assert emitted_fcs[1]["thoughtSignature"] == "sig-turn2-cmd"
    assert "toolAction" not in emitted_fcs[0]["functionCall"]["args"]
    assert "toolAction" not in emitted_fcs[1]["functionCall"]["args"]
    assert emitted_fcs[1]["functionCall"]["args"]["WaitMsBeforeAsync"] == 5000


# ---------------------------------------------------------------------------
# 3. AgentRouter (`agentrouter/*` & `agentrouter-o/*`) — Function-Tool Policy & NFKD
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "f_val,model_id",
    [
        ("agentrouter/gpt-5.6-sol", "gpt-5.6-sol"),
        ("agentrouter-o/gpt-5.6-sol", "gpt-5.6-sol"),
        ("agentrouter/gpt-6.1-sol", "gpt-6.1-sol"),
        ("agentrouter-o/gpt-6.1-sol", "gpt-6.1-sol"),
    ],
)
def test_agentrouter_blacksand_studio_and_claude_code_sandbox(f_val: str, model_id: str):
    """Simulate a 3-turn multi-tool session through AgentRouter (`gpt-5.6-sol` & `gpt-6.1-sol`)
    for both OpenAI wire (Blacksand Studio / normalizer) and Anthropic wire (Claude Code)."""
    wrapper = {
        "model": f"models/{model_id}",
        "request": {
            "contents": [
                {"role": "user", "parts": [{"text": "Kiểm tra hệ thống và sửa lỗi file app/main.py"}]},
            ],
            "tools": copy.deepcopy(GEMINI_TOOLS_DECLARATION),
        },
    }
    oai_req = gemini_request_to_openai(unwrap_request(wrapper), normalize_model(wrapper["model"]))
    oai_req["reasoning"] = {"mode": "pro", "effort": "xhigh"}

    # 1. Verify NFKD Vietnamese transcoding decomposes precomposed VN codepoints for agentrouter*
    changed, summary = agentrouter_nfkd_transcode(f_val.split("/")[0], oai_req["messages"])
    assert changed is True and summary.startswith("nfkd:")

    # 2. Verify OpenAI wire function-tool reasoning policy forces reasoning_effort="none" and strips nested reasoning
    resolved_oai, _ = resolve_thinking(
        copy.deepcopy(oai_req),
        f_val,
        "xhigh",
        reasoning_mode="pro",
        reasoning_context="all_turns",
        wire_format="openai",
    )
    assert resolved_oai.get("reasoning_effort") == "none"
    assert "reasoning" not in resolved_oai

    # 3. Verify Claude Code Anthropic wire (`wire_format="anthropic"`) never injects OpenAI `reasoning_effort`
    resolved_anth, _ = resolve_thinking(
        copy.deepcopy(oai_req),
        f_val,
        "xhigh",
        reasoning_mode="pro",
        reasoning_context="all_turns",
        wire_format="anthropic",
    )
    assert "reasoning_effort" not in resolved_anth
    assert "reasoning" not in resolved_anth


# ---------------------------------------------------------------------------
# 4. All Chat2API Account Coding Models via BSL Router (`chat2api/*`)
# ---------------------------------------------------------------------------

CHAT2API_ROUTER_CODING_MODELS = [
    "chat2api/qoder-paid/qwen3.8-max",
    "chat2api/qoder-free/qwen3.8-max-free",
    "chat2api/workbuddy-paid/glm-5.3",
    "chat2api/workbuddy-free/deepseek-v4.1-flash-free",
    "chat2api/intern-free/glm-5.3",
    "chat2api/kiro-free/claude-sonnet-4.5-free",
    "chat2api/kiro-paid/claude-sonnet-4.5-free",
    "chat2api/kiro-power/claude-fable-5.1",
    "chat2api/gemini-paid/gemini-4-argon",
    "chat2api/gemini-free/gemini-3.8-flash-free",
    "chat2api/chatgpt-paid/gpt-6-1-sol",
    "chat2api/chatgpt-free/gpt-6-1-sol-free",
    "chat2api/claude-paid/claude-sonnet-5-5",
    "chat2api/claude-free/claude-sonnet-5-5-free",
    "chat2api/grok-paid/grok-4.7",
    "chat2api/grok-cli/grok-4.7",
    "chat2api/deepseek-free/deepseek-v4.1-flash",
    "chat2api/zai-free/glm-5.3",
    "chat2api/arena-free/kimi-k2.7-code",
    "chat2api/zcode-free/zcode-glm-5.3",
]


@pytest.mark.parametrize("router_model", CHAT2API_ROUTER_CODING_MODELS)
def test_chat2api_coding_models_via_bsl_router_blacksand_studio_and_claude_code(router_model: str):
    """Simulate a 3-turn, 5-tool-call coding session for every Chat2API coding account family
    through BSL Router's Blacksand Studio (Gemini `v1internal` wire) and Claude Code
    (`/v1/messages` Anthropic `AnthropicStreamToolGuard` + `repair_anthropic_response_tool_uses`)
    pipelines."""
    provider_name, sub_model = router_model.split("/", 1)
    assert provider_name == "chat2api"

    # SG-01 invariant: `chat2api/kiro-*` has `provider_name == 'chat2api'` so it must NEVER
    # trigger the `provider_name == 'kiro'` AWS CodeWhisperer transform.
    assert (provider_name == "kiro") is False

    # ── Part A: Blacksand Studio (`v1internal` Gemini wire -> OpenAI -> Chat2API -> Gemini wire) ──
    wrapper_t1 = {
        "model": f"models/{sub_model}",
        "request": {
            "systemInstruction": {"parts": [{"text": "You are Blacksand Studio."}]},
            "contents": [
                {"role": "user", "parts": [{"text": "Read file, grep, and run pytest."}]},
            ],
            "tools": copy.deepcopy(GEMINI_TOOLS_DECLARATION),
        },
    }
    oai_t1 = gemini_request_to_openai(unwrap_request(wrapper_t1), normalize_model(wrapper_t1["model"]))
    tool_schemas = extract_openai_tool_schemas(oai_t1.get("tools"))

    # Simulate Chat2API returning Turn 1 parallel tool calls (including GLM/XML repair check)
    raw_oai_resp_t1 = {
        "id": "chatcmpl-c2a-t1",
        "object": "chat.completion",
        "model": sub_model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_c2a_1",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": json.dumps({"path": "app/toolbridge.py", "StartLine": 1, "EndLine": 40}),
                            },
                        },
                        {
                            "id": "call_c2a_2",
                            "type": "function",
                            "function": {
                                "name": "grep_search",
                                "arguments": json.dumps({"SearchPath": "app/toolbridge.py", "Query": "lines[-14:]"}),
                            },
                        },
                        {
                            "id": "call_c2a_3",
                            "type": "function",
                            "function": {
                                "name": "run_command",
                                "arguments": json.dumps({"command": "pytest -q"}),
                            },
                        },
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140},
    }
    normalize_glm_tool_calls(raw_oai_resp_t1)
    gem_resp_t1 = openai_response_to_gemini(raw_oai_resp_t1, sub_model, tool_schemas=tool_schemas)
    parts_t1 = gem_resp_t1["response"]["candidates"][0]["content"]["parts"]
    fcs_t1 = [p["functionCall"] for p in parts_t1 if "functionCall" in p]
    assert [fc["name"] for fc in fcs_t1] == ["read_file", "grep_search", "run_command"]
    assert fcs_t1[0]["args"] == {"Path": "app/toolbridge.py", "StartLine": 1, "EndLine": 40}
    assert fcs_t1[1]["args"] == {"SearchPath": "app/toolbridge.py", "Query": "lines[-14:]"}
    assert fcs_t1[2]["args"]["CommandLine"] == "pytest -q"
    assert fcs_t1[2]["args"]["WaitMsBeforeAsync"] == 5000
    assert "toolAction" not in fcs_t1[0]["args"] and "toolAction" not in fcs_t1[2]["args"]

    # ── Part B: Claude Code CLI (`/v1/messages` Anthropic wire) 3-turn tool guard & repair ──
    anthropic_resp_t2 = {
        "id": "msg_c2a_t2",
        "type": "message",
        "role": "assistant",
        "model": sub_model,
        "content": [
            {
                "type": "tool_use",
                "id": "toolu_c2a_edit_4",
                "name": "edit_file",
                "input": {"Path": "app/toolbridge.py", "Target": "old()", "Replacement": "new()"},
            },
            {
                "type": "tool_use",
                "id": "toolu_c2a_cmd_5",
                "name": "run_command",
                "input": {"CommandLine": "pytest -q", "WaitMsBeforeAsync": 5000, "toolSummary": "Run pytest"},
            },
        ],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 180, "output_tokens": 35},
    }
    repair_anthropic_response_tool_uses(anthropic_resp_t2)
    tu_blocks = [b for b in anthropic_resp_t2["content"] if b.get("type") == "tool_use"]
    assert [b["name"] for b in tu_blocks] == ["edit_file", "run_command"]
    assert tu_blocks[0]["input"]["Path"] == "app/toolbridge.py"
    assert tu_blocks[1]["input"]["CommandLine"] == "pytest -q"

    # Also verify streaming AnthropicStreamToolGuard on fragmented + malformed JSON arguments
    guard = AnthropicStreamToolGuard(tools_in_request=True)
    events_in = [
        ("content_block_start", {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "toolu_stream_1", "name": "edit_file", "input": {}}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"Path": "app/toolbridge.py", '}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '"Target": "lines[-14:]", "Replacement": "lines[-16:]"}'}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 1}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 25}}),
    ]
    out_bytes: List[bytes] = []
    for ev_type, ev_data in events_in:
        raw_frame = f"event: {ev_type}\ndata: {json.dumps(ev_data, ensure_ascii=False)}\n\n".encode("utf-8")
        out_bytes.extend(guard.feed(raw_frame))
    out_bytes.extend(guard.flush())

    assembled_json = ""
    for b_frame in out_bytes:
        for ln in b_frame.decode("utf-8").splitlines():
            if ln.startswith("data:"):
                ev = json.loads(ln[5:].strip())
                if ev.get("type") == "content_block_delta" and (ev.get("delta") or {}).get("type") == "input_json_delta":
                    assembled_json += ev["delta"]["partial_json"]

    assert json.loads(assembled_json) == {
        "Path": "app/toolbridge.py",
        "Target": "lines[-14:]",
        "Replacement": "lines[-16:]",
    }
