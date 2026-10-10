"""Codex Responses-API tool-role conversion (bug 2026-10-09).

`openai_to_responses` used to copy every non-system chat role verbatim into
the Responses `input[]` array, so a chat tool result (role:"tool") reached
Codex as input[6] with an illegal role:

    [400] Invalid value: 'tool'. Supported values are: 'assistant',
    'system', 'developer', and 'user'. (param: input[6])

These tests pin the fixed contract: whitelisted roles only, tool history as
TOP-LEVEL function_call / function_call_output sibling items (mirroring
normalizer_v2._egress_responses), concatenated system instructions, tool
forwarding behind the BSL_CODEX_FORWARD_TOOLS kill-switch, and the return
direction (Codex function_call events -> OpenAI delta.tool_calls chunks).
All offline, no network.
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

LEGAL_ROLES = {"user", "assistant", "system", "developer"}
TOOL_ITEM_TYPES = {"function_call", "function_call_output"}


# ── helpers ──────────────────────────────────────────────────────────────────

async def _aiter(items):
    for item in items:
        yield item


def _sse_bytes(events: list[tuple[str, dict]]) -> list[bytes]:
    """Build synthetic SSE byte chunks from (event_name, data) pairs."""
    chunks = []
    for name, data in events:
        chunks.append(f"event: {name}\n".encode())
        chunks.append(f"data: {json.dumps(data)}\n\n".encode())
    return chunks


def _collect_sse(events: list[tuple[str, dict]]) -> list[dict]:
    """Run the SSE converter over synthetic events, return parsed chunks."""
    async def _run():
        out = []
        async for chunk in codex_adapter.responses_sse_to_openai_sse(
            _aiter(_sse_bytes(events))
        ):
            out.append(chunk)
        return out

    raw = asyncio.run(_run())
    return [json.loads(c.decode("utf-8").split("data: ", 1)[1]) for c in raw]


def _assert_input_invariant(result: dict) -> None:
    """Every input[] item: legal message role OR function_call* item type."""
    for i, item in enumerate(result["input"]):
        assert isinstance(item, dict), f"input[{i}] is not a dict: {item!r}"
        if "type" in item:
            assert item["type"] in TOOL_ITEM_TYPES, (
                f"input[{i}] carries item type {item['type']!r}: {item!r}"
            )
        else:
            assert item.get("role") in LEGAL_ROLES, (
                f"input[{i}] carries non-whitelisted role "
                f"{item.get('role')!r}: {item!r}"
            )


# ── 1. the reported failure ──────────────────────────────────────────────────

def test_reported_failure_tool_role_at_index_6():
    """Exact reported shape: tool result at messages[6] must never reach
    input[] as role:"tool" — it becomes a top-level function_call_output
    with the right call_id/output."""
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Weather in Paris?"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_a", "type": "function",
             "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'}},
        ]},
        {"role": "tool", "tool_call_id": "call_a", "content": "18C, clear"},
        {"role": "user", "content": "And in Lyon?"},
        {"role": "assistant", "content": "Checking Lyon."},
        {"role": "tool", "tool_call_id": "call_b", "content": "21C, windy"},
        {"role": "user", "content": "Summarize."},
    ]
    result = codex_adapter.openai_to_responses({"messages": messages})

    _assert_input_invariant(result)
    assert not any(item.get("role") == "tool" for item in result["input"])

    outputs = [i for i in result["input"] if i.get("type") == "function_call_output"]
    assert len(outputs) == 2
    by_call = {o["call_id"]: o["output"] for o in outputs}
    assert by_call == {"call_a": "18C, clear", "call_b": "21C, windy"}


def test_general_role_invariant_across_shapes():
    """Sweep invariant: no input[] item ever carries a non-whitelisted role,
    whatever the incoming history contains."""
    messages = [
        {"role": "system", "content": "sys one"},
        {"role": "user", "content": "hi"},
        {"role": "banana", "content": "??"},
        {"role": "", "content": "no role"},
        {"role": "developer", "content": "dev note"},
        {"role": "tool", "tool_call_id": "c1", "content": "r1"},
        {"role": "function", "tool_call_id": "c2", "content": "r2"},
        {"role": "assistant", "content": "text then tools", "tool_calls": [
            {"id": "c3", "type": "function",
             "function": {"name": "f1", "arguments": {"k": 1}}},
            {"type": "function", "function": {"arguments": "no name: skip"}},
        ]},
        {"role": "tool", "content": "orphan, no call id"},
    ]
    result = codex_adapter.openai_to_responses({"messages": messages})
    _assert_input_invariant(result)


# ── 2. assistant tool_calls -> top-level function_call items ────────────────

def test_assistant_tool_calls_string_arguments():
    messages = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "f1", "arguments": '{"a": 1}'}},
            {"id": "c2", "type": "function",
             "function": {"name": "f2", "arguments": '{"b": 2}'}},
        ]},
    ]
    result = codex_adapter.openai_to_responses({"messages": messages})
    calls = [i for i in result["input"] if i.get("type") == "function_call"]
    assert calls == [
        {"type": "function_call", "call_id": "c1", "name": "f1", "arguments": '{"a": 1}'},
        {"type": "function_call", "call_id": "c2", "name": "f2", "arguments": '{"b": 2}'},
    ]


def test_assistant_tool_calls_dict_arguments_become_json_string():
    messages = [
        {"role": "assistant", "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "f1", "arguments": {"k": [1, 2]}}},
        ]},
    ]
    result = codex_adapter.openai_to_responses({"messages": messages})
    call = next(i for i in result["input"] if i.get("type") == "function_call")
    assert isinstance(call["arguments"], str)
    assert json.loads(call["arguments"]) == {"k": [1, 2]}


def test_assistant_tool_calls_missing_arguments_and_id():
    messages = [
        {"role": "assistant", "tool_calls": [
            {"type": "function", "function": {"name": "f1"}},
        ]},
    ]
    result = codex_adapter.openai_to_responses({"messages": messages})
    call = next(i for i in result["input"] if i.get("type") == "function_call")
    assert call["arguments"] == "{}"
    assert call["call_id"] == "call_0"
    assert call["name"] == "f1"


def test_assistant_tool_call_without_name_skipped():
    messages = [
        {"role": "assistant", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"arguments": "{}"}},
            {"id": "c2", "type": "function", "function": {"name": "f2", "arguments": "{}"}},
        ]},
    ]
    result = codex_adapter.openai_to_responses({"messages": messages})
    calls = [i for i in result["input"] if i.get("type") == "function_call"]
    assert [c["call_id"] for c in calls] == ["c2"]


def test_assistant_text_precedes_function_call_items():
    messages = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "Let me check.",
         "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "f1", "arguments": "{}"}},
        ]},
    ]
    result = codex_adapter.openai_to_responses({"messages": messages})
    input_items = result["input"]
    text_idx = next(i for i, it in enumerate(input_items) if it.get("role") == "assistant")
    fc_idx = next(i for i, it in enumerate(input_items) if it.get("type") == "function_call")
    assert input_items[text_idx]["content"] == "Let me check."
    assert text_idx < fc_idx


# ── 3. tool results ──────────────────────────────────────────────────────────

def test_tool_result_missing_call_id_degrades_to_user():
    messages = [
        {"role": "user", "content": "q"},
        {"role": "tool", "content": "orphan result"},
        {"role": "tool", "tool_call_id": "", "content": "empty id"},
    ]
    result = codex_adapter.openai_to_responses({"messages": messages})
    _assert_input_invariant(result)
    degraded = [it for it in result["input"] if it.get("role") == "user"]
    assert len(degraded) == 3  # original user + two degraded tool results
    assert degraded[1]["content"].startswith("[tool result]\norphan result")
    assert degraded[2]["content"].startswith("[tool result]\nempty id")
    # never a null/empty call_id anywhere
    for it in result["input"]:
        if "call_id" in it:
            assert it["call_id"]
    assert not any(it.get("type") == "function_call_output" for it in result["input"])


def test_tool_result_list_content_flattened():
    messages = [
        {"role": "tool", "tool_call_id": "c1",
         "content": [{"type": "text", "text": "18C"}]},
    ]
    result = codex_adapter.openai_to_responses({"messages": messages})
    out = next(i for i in result["input"] if i.get("type") == "function_call_output")
    assert out["output"] == "18C"


# ── 4. system / developer / unknown roles ────────────────────────────────────

def test_multiple_system_messages_concatenate_in_order():
    messages = [
        {"role": "system", "content": "First."},
        {"role": "user", "content": "hi"},
        {"role": "system", "content": "Second."},
        {"role": "system", "content": "Third."},
    ]
    result = codex_adapter.openai_to_responses({"messages": messages})
    assert result["instructions"] == "First.\n\nSecond.\n\nThird."
    assert all(it.get("role") != "system" for it in result["input"])


def test_no_system_message_leaves_instructions_omitted():
    result = codex_adapter.openai_to_responses(
        {"messages": [{"role": "user", "content": "hi"}]}
    )
    assert "instructions" not in result


def test_developer_role_passes_through():
    messages = [{"role": "developer", "content": "dev note"}]
    result = codex_adapter.openai_to_responses({"messages": messages})
    assert result["input"] == [{"role": "developer", "content": "dev note"}]


@pytest.mark.parametrize("bad_role", ["", "banana", None, "Tool"])
def test_unknown_roles_coerce_to_user(bad_role):
    messages = [{"role": bad_role, "content": "mystery"}]
    result = codex_adapter.openai_to_responses({"messages": messages})
    _assert_input_invariant(result)
    assert result["input"] == [{"role": "user", "content": "mystery"}]


# ── 5. tool-declaration forwarding ───────────────────────────────────────────

_TOOL_HISTORY = [
    {"role": "user", "content": "check the weather"},
    {"role": "assistant", "tool_calls": [
        {"id": "call_w", "type": "function",
         "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'}},
    ]},
    {"role": "tool", "tool_call_id": "call_w", "content": "18C"},
]
_DECLARED_TOOLS = [
    {"type": "function", "function": {
        "name": "get_weather",
        "description": "Get current weather",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
    }},
]


def test_tools_forwarded_by_default(monkeypatch):
    monkeypatch.delenv("BSL_CODEX_FORWARD_TOOLS", raising=False)
    body = {
        "messages": [dict(m) for m in _TOOL_HISTORY],
        "tools": [dict(t) for t in _DECLARED_TOOLS],
    }
    result = codex_adapter.openai_to_responses(body)
    assert result["tools"] == [{
        "type": "function",
        "name": "get_weather",
        "description": "Get current weather",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
    }]
    # The pop-loop still strips the caller's dict either way.
    assert "tools" not in body


@pytest.mark.parametrize("off", ["0", "false", "no", "FALSE", "No", " 0 "])
def test_tools_forwarding_kill_switch(monkeypatch, off):
    monkeypatch.setenv("BSL_CODEX_FORWARD_TOOLS", off)
    body = {
        "messages": [dict(m) for m in _TOOL_HISTORY],
        "tools": [dict(t) for t in _DECLARED_TOOLS],
    }
    result = codex_adapter.openai_to_responses(body)
    assert "tools" not in result
    assert "tools" not in body


def test_forward_tools_helper_defaults_enabled(monkeypatch):
    monkeypatch.delenv("BSL_CODEX_FORWARD_TOOLS", raising=False)
    assert codex_adapter._codex_forward_tools_enabled() is True
    monkeypatch.setenv("BSL_CODEX_FORWARD_TOOLS", "1")
    assert codex_adapter._codex_forward_tools_enabled() is True
    for off in ("0", "false", "no", "FALSE"):
        monkeypatch.setenv("BSL_CODEX_FORWARD_TOOLS", off)
        assert codex_adapter._codex_forward_tools_enabled() is False


def test_minimal_tool_declaration_omits_optional_keys(monkeypatch):
    monkeypatch.delenv("BSL_CODEX_FORWARD_TOOLS", raising=False)
    body = {
        "messages": [dict(m) for m in _TOOL_HISTORY],
        "tools": [{"type": "function", "function": {"name": "bare"}}],
    }
    result = codex_adapter.openai_to_responses(body)
    assert result["tools"] == [{"type": "function", "name": "bare"}]


def test_legacy_functions_key_converted(monkeypatch):
    monkeypatch.delenv("BSL_CODEX_FORWARD_TOOLS", raising=False)
    body = {
        "messages": [dict(m) for m in _TOOL_HISTORY],
        "functions": [{
            "name": "get_weather",
            "description": "Get current weather",
            "parameters": {"type": "object"},
        }],
    }
    result = codex_adapter.openai_to_responses(body)
    assert result["tools"] == [{
        "type": "function",
        "name": "get_weather",
        "description": "Get current weather",
        "parameters": {"type": "object"},
    }]
    assert "functions" not in body


def test_bare_tools_declaration_without_tool_history_keeps_strip(monkeypatch):
    """Forwarding is scoped to tool-carrying requests; a bare declaration
    keeps the legacy strip behaviour pinned by
    test_codex_kiro_oauth.test_strips_unsupported_params."""
    monkeypatch.delenv("BSL_CODEX_FORWARD_TOOLS", raising=False)
    body = {
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [dict(t) for t in _DECLARED_TOOLS],
    }
    result = codex_adapter.openai_to_responses(body)
    assert "tools" not in result
    assert "tools" not in body


# ── 6. regression guard: forced store/stream/effort unchanged ───────────────

def test_store_stream_effort_regression_guard():
    body = {
        "messages": [dict(m) for m in _TOOL_HISTORY],
        "stream": False,
        "store": True,
        "reasoning_effort": "max",
        "max_tokens": 100,
        "temperature": 0.7,
    }
    result = codex_adapter.openai_to_responses(body)
    assert result["store"] is False
    assert result["stream"] is True
    assert result["reasoning"]["effort"] == "xhigh"
    assert "max_tokens" not in result
    assert "temperature" not in result


# ── 7. Phase 2: return direction (Codex -> OpenAI chunks) ───────────────────

_FC_ITEM = {"type": "function_call", "call_id": "call_w",
            "name": "get_weather", "arguments": '{"city": "Paris"}'}


def test_output_item_done_function_call_becomes_tool_calls_delta():
    chunk = codex_adapter._codex_event_to_openai_chunk(
        "response.output_item.done", {"item": dict(_FC_ITEM)}
    )
    assert chunk is not None
    tc = chunk["choices"][0]["delta"]["tool_calls"][0]
    assert tc == {
        "index": 0,
        "id": "call_w",
        "type": "function",
        "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'},
    }
    assert chunk["choices"][0]["finish_reason"] is None


def test_output_item_done_non_function_call_still_ignored():
    chunk = codex_adapter._codex_event_to_openai_chunk(
        "response.output_item.done",
        {"item": {"type": "message", "role": "assistant", "content": []}},
    )
    assert chunk is None


def test_function_call_arguments_delta_passthrough():
    chunk = codex_adapter._codex_event_to_openai_chunk(
        "response.function_call_arguments.delta", {"delta": '{"city"'}
    )
    assert chunk is not None
    tc = chunk["choices"][0]["delta"]["tool_calls"][0]
    assert tc["index"] == 0
    assert tc["function"]["arguments"] == '{"city"'
    assert chunk["choices"][0]["finish_reason"] is None


def test_completed_with_function_call_finishes_tool_calls():
    data = {"response": {
        "id": "resp_x", "model": "gpt-5.5", "status": "completed",
        "output": [dict(_FC_ITEM)],
        "usage": {"input_tokens": 4, "output_tokens": 2},
    }}
    chunk = codex_adapter._codex_event_to_openai_chunk("response.completed", data)
    assert chunk["choices"][0]["finish_reason"] == "tool_calls"


def test_completed_text_only_finishes_stop():
    data = {"response": {
        "id": "resp_x", "model": "gpt-5.5", "status": "completed",
        "output": [{"type": "message", "content": []}],
        "usage": {"input_tokens": 4, "output_tokens": 2},
    }}
    chunk = codex_adapter._codex_event_to_openai_chunk("response.completed", data)
    assert chunk["choices"][0]["finish_reason"] == "stop"


def test_sse_tool_only_stream_end_to_end():
    events = [
        ("response.created", {"id": "resp_x"}),
        ("response.output_item.done", {"item": dict(_FC_ITEM)}),
        ("response.completed", {"response": {
            "id": "resp_x", "model": "gpt-5.5", "status": "completed",
            "output": [dict(_FC_ITEM)],
            "usage": {"input_tokens": 4, "output_tokens": 2},
        }}),
    ]
    parsed = _collect_sse(events)
    assert len(parsed) == 3
    assert parsed[0]["choices"][0]["delta"] == {"role": "assistant"}
    tc = parsed[1]["choices"][0]["delta"]["tool_calls"][0]
    assert tc["function"]["name"] == "get_weather"
    assert tc["id"] == "call_w"
    assert parsed[2]["choices"][0]["finish_reason"] == "tool_calls"
    assert parsed[2]["usage"]["prompt_tokens"] == 4


def test_sse_text_then_tool_call_stream():
    events = [
        ("response.output_text.delta", {"delta": "Let me check.", "model": "gpt-5.5"}),
        ("response.function_call_arguments.delta", {"delta": '{"city"'}),
        ("response.output_item.done", {"item": dict(_FC_ITEM)}),
        ("response.completed", {"response": {
            "id": "resp_x", "model": "gpt-5.5", "status": "completed",
            "output": [dict(_FC_ITEM)], "usage": {},
        }}),
    ]
    parsed = _collect_sse(events)
    assert len(parsed) == 5
    assert parsed[0]["choices"][0]["delta"] == {"role": "assistant"}
    assert parsed[1]["choices"][0]["delta"]["content"] == "Let me check."
    assert parsed[2]["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"] == '{"city"'
    assert parsed[3]["choices"][0]["delta"]["tool_calls"][0]["function"]["name"] == "get_weather"
    assert parsed[4]["choices"][0]["finish_reason"] == "tool_calls"
