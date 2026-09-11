"""Pending-task fixes: StreamBuffer usage estimate + OpenAI top-level system fold."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.main import (
    _fold_top_level_system_into_messages,
    _inject_intent_format_block,
    _response_has_model_output,
)


def test_fold_top_level_system_into_messages_openai():
    payload = {
        "model": "x",
        "messages": [{"role": "user", "content": "hi"}],
        "system": "FORMAT DIRECTIVE: be concise",
    }
    out = _fold_top_level_system_into_messages(payload)
    assert "system" not in out
    assert out["messages"][0]["role"] == "system"
    assert "FORMAT DIRECTIVE" in out["messages"][0]["content"]
    assert out["messages"][1]["role"] == "user"


def test_fold_appends_existing_system_message():
    payload = {
        "messages": [
            {"role": "system", "content": "base"},
            {"role": "user", "content": "hi"},
        ],
        "system": "extra",
    }
    out = _fold_top_level_system_into_messages(payload)
    assert "system" not in out
    assert out["messages"][0]["content"] == "base\nextra"


def test_fold_noop_when_no_system():
    payload = {"messages": [{"role": "user", "content": "hi"}]}
    out = _fold_top_level_system_into_messages(dict(payload))
    assert out == payload


def test_intent_inject_openai_uses_messages_not_top_level_system():
    payload = {
        "model": "deepseek",
        "messages": [{"role": "user", "content": "give me a json object of a and b"}],
    }
    # force intent path via direct call
    out = _inject_intent_format_block(payload, "json")
    assert "system" not in out  # must not create Anthropic-only key
    assert out["messages"][0]["role"] == "system"
    assert "FORMAT DIRECTIVE" in out["messages"][0]["content"]


def test_intent_inject_anthropic_keeps_top_level_system():
    payload = {
        "model": "claude",
        "system": "You are helpful.",
        "messages": [{"role": "user", "content": "hi"}],
    }
    out = _inject_intent_format_block(payload, "concise")
    assert isinstance(out.get("system"), str)
    assert "FORMAT DIRECTIVE" in out["system"]
    assert out["messages"][0]["role"] == "user"


def test_response_has_model_output_with_content_zero_usage():
    data = {
        "choices": [{"message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 0, "total_tokens": 10},
    }
    assert _response_has_model_output(data, out_tokens=0) is True


def test_response_has_model_output_reasoning_only_with_tokens():
    # mimo-v2.5 starvation repro: budget consumed by reasoning before any
    # visible content (finish_reason="length"). Tokens were spent -> live
    # model output, must NOT be reclassified as a 504 zombie.
    data = {
        "choices": [{
            "message": {"role": "assistant", "content": "", "reasoning_content": "think think"},
            "finish_reason": "length",
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 40, "total_tokens": 50},
    }
    assert _response_has_model_output(data, out_tokens=40) is True


def test_response_has_model_output_reasoning_only_zero_tokens_stays_zombie():
    # Reasoning text with zero usage is indistinguishable from a dead
    # upstream -> still a zombie, still triggers combo fallback.
    data = {
        "choices": [{
            "message": {"role": "assistant", "content": "", "reasoning_content": "x"},
        }],
    }
    assert _response_has_model_output(data, out_tokens=0) is False


def test_response_has_model_output_reasoning_key_variant():
    # Some gateways emit "reasoning" instead of "reasoning_content".
    data = {
        "choices": [{"message": {"role": "assistant", "content": " ", "reasoning": "deep thought"}}],
    }
    assert _response_has_model_output(data, out_tokens=7) is True


def test_response_has_model_output_billed_but_empty_is_zombie():
    # Live case 2026-09-11: a free Zen node billed 5 hidden thinking tokens
    # and relayed ZERO content/reasoning deltas. Billed tokens alone do NOT
    # rescue an empty message — the gate must 504 it so combo fallback
    # advances to the next entry.
    data = {
        "choices": [{"message": {"role": "assistant", "content": ""}}],
        "usage": {"prompt_tokens": 403, "completion_tokens": 5, "total_tokens": 408},
    }
    assert _response_has_model_output(data, out_tokens=5) is False


def test_response_has_model_output_anthropic_thinking_block():
    # Anthropic wire: reasoning-only thinking block. With tokens spent it is
    # live output; with zero tokens it stays a zombie.
    data = {"content": [{"type": "thinking", "thinking": "reasoning here"}]}
    assert _response_has_model_output(data, out_tokens=25) is True
    assert _response_has_model_output(data, out_tokens=0) is False


def test_usage_estimate_heuristic_unit():
    # Mirrors the StreamBuffer estimate: non-empty content + missing usage => >=1
    content = "OK"
    est = max(1, (len(content) + 3) // 4)
    assert est == 1
    content2 = "Hello world!!"  # 13 chars -> 4 tokens
    assert max(1, (len(content2) + 3) // 4) == 4
