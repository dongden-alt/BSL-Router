"""Tests for app/middleware/kiro_filter_guard.py (Kiro content-filter + context-window guard).

Trigger strings are built from code points so this file never contains a literal trigger tag
(the Studio bridge rewrites such tags in transit, which would make the test text unreliable).
"""

import re

import pytest

from app.middleware import kiro_filter_guard as kg

pytestmark = pytest.mark.unit

LT, GT, LB, RB, LC, RC = chr(60), chr(62), chr(91), chr(93), chr(123), chr(125)
W = "thinking"
BAD = re.compile(r"[\x3c\x5b\x7b(]\s*/?\s*(think|thinking|reasoning)", re.I)
CFG = {"tools": {}}  # defaults: enabled, providers=[nguyenha], nguyenha budget 160000


def tag(word=W, o=LT, c=GT, close=False):
    return o + ("/" if close else "") + word + c


# ---------------------------------------------------------------- tag guard

@pytest.mark.parametrize(
    "trigger",
    [tag(), tag(o=LB, c=RB), tag(o=LC, c=RC), tag(o="(", c=")"), tag("think"), tag("reasoning", LB, RB),
     tag(close=True), tag("THINKING", LB, RB)],
)
def test_bracketed_triggers_are_rewritten(trigger):
    out, n = kg.sanitize_text(f"Rule: In {trigger} plan first.")
    assert n == 1
    assert not BAD.search(out)
    assert "internal-analysis-" in out


def test_replacement_has_no_padding_spaces():
    # Live probe: the word padded with two spaces on each side trips the filter.
    out, _ = kg.sanitize_text("In " + tag() + " plan")
    assert out == "In internal-analysis-start plan"


def test_close_tag_maps_to_end_word():
    out, n = kg.sanitize_text(tag() + "x" + tag(close=True))
    assert n == 2 and out == "internal-analysis-startxinternal-analysis-end"


def test_inline_and_fenced_code_triggers_rewritten_other_code_untouched():
    # Live probe under thinking=adaptive + effort=max + cognitive anchor:
    # leaving `<thinking>` inside inline backticks still triggers Kiro content_filter.
    inline = "`" + tag() + "`"
    other_code = "`const x = 42;`"
    src = f"Use {inline} and {other_code} here.\n```xml\n{tag()}y{tag(close=True)}\n```"
    out, n = kg.sanitize_text(src)
    assert n == 3
    assert other_code in out
    assert "`internal-analysis-start`" in out
    assert not BAD.search(out)


def test_plain_text_untouched():
    for s in ["plain thinking text", "reasoning about it", "", "no trigger"]:
        assert kg.sanitize_text(s) == (s, 0)
    assert kg.sanitize_text(None) == (None, 0)


def test_messages_openai_shape_including_tool_args():
    msgs = [
        {"role": "system", "content": "sys " + tag()},
        {"role": "user", "content": [{"type": "text", "text": tag(o=LB, c=RB)}, {"type": "image_url", "image_url": {"url": tag()}}]},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "edit", "arguments": '{"x": "' + tag() + '"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "ok " + tag()},
    ]
    assert kg.sanitize_messages(msgs) == 4
    assert not BAD.search(msgs[0]["content"])
    assert not BAD.search(msgs[1]["content"][0]["text"])
    assert msgs[1]["content"][1]["image_url"]["url"] == tag()  # images untouched
    assert not BAD.search(msgs[2]["tool_calls"][0]["function"]["arguments"])
    assert not BAD.search(msgs[3]["content"])


def test_messages_anthropic_shape():
    msgs = [
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t", "name": "edit", "input": {"text": tag(), "nested": {"y": tag("think", LB, RB)}}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": [{"type": "text", "text": tag()}]}]},
    ]
    assert kg.sanitize_messages(msgs) == 3
    assert not BAD.search(str(msgs))


# ---------------------------------------------------------------- provider scope + config

def test_scope_only_listed_providers():
    p_kiro = {"messages": [{"role": "user", "content": "In " + tag() + " plan"}]}
    p_other = {"messages": [{"role": "user", "content": "In " + tag() + " plan"}]}
    kg.apply_kiro_egress_guards(p_kiro, "nguyenha", "claude-opus-5-5", CFG)
    kg.apply_kiro_egress_guards(p_other, "pix4k", "claude-opus-5.5", CFG)
    assert not BAD.search(p_kiro["messages"][0]["content"])
    assert BAD.search(p_other["messages"][0]["content"])


def test_config_overrides_and_disable():
    cfg = {"tools": {"kiro_filter_guard": {"providers": ["kiro"], "max_input_tokens_by_provider": {"kiro": 90000}}}}
    assert kg.filter_providers(cfg) == frozenset({"kiro"})
    assert kg.max_input_tokens_for("kiro", cfg) == 90000
    assert kg.max_input_tokens_for("nguyenha", cfg) is None
    off = {"tools": {"kiro_filter_guard": {"enabled": False}}}
    p = {"messages": [{"role": "user", "content": tag()}]}
    kg.apply_kiro_egress_guards(p, "nguyenha", "m", off)
    assert p["messages"][0]["content"] == tag()
    assert kg.max_input_tokens_for("nguyenha", {"tools": {"kiro_filter_guard": {"max_input_tokens_by_provider": {"nguyenha": "bad"}}}}) is None


def test_anthropic_top_level_system_sanitized():
    p = {"system": [{"type": "text", "text": tag()}], "messages": [{"role": "user", "content": "hi"}]}
    kg.apply_kiro_egress_guards(p, "nguyenha", "m", CFG)
    assert not BAD.search(p["system"][0]["text"])
    p2 = {"system": "s " + tag(), "messages": []}
    kg.apply_kiro_egress_guards(p2, "nguyenha", "m", CFG)
    assert not BAD.search(p2["system"])


def test_fail_open_on_garbage():
    assert kg.apply_kiro_egress_guards(None, "nguyenha", "m", CFG) is None
    assert kg.apply_kiro_egress_guards({"messages": "x"}, "nguyenha", "m", CFG) == {"messages": "x"}


# ---------------------------------------------------------------- context window guard

def test_estimate_tokens():
    assert kg.estimate_tokens("") == 0
    assert kg.estimate_tokens("a" * 350) == 101
    assert kg.estimate_tokens("\u1ea1" * 100) == 101


def _long_session(n=30, size=7000):
    msgs = [{"role": "system", "content": "SYS"}, {"role": "user", "content": "<CONTEXT_SUMMARY>anchor</CONTEXT_SUMMARY>"}]
    for i in range(n):
        msgs.append({"role": "user", "content": f"ASK-{i} " + "x" * size})
        msgs.append({"role": "assistant", "content": "", "tool_calls": [{"id": f"c{i}", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": "y" * size})
    msgs.append({"role": "user", "content": "ACTIVE_TURN"})
    return msgs


def test_under_budget_no_change():
    msgs = [{"role": "system", "content": "s"}] + [{"role": "user", "content": "hi"}] * 6
    assert kg.apply_context_window_guard(msgs, 160000) is None


def test_over_budget_compacts_with_digest():
    msgs = _long_session()
    original_head = msgs[1]["content"]
    res = kg.apply_context_window_guard(msgs, 20000)
    assert res is not None and len(res) < len(msgs)
    assert res[0]["content"] == "SYS"
    assert kg.BUDGET_MARKER in res[1]["content"]
    assert "ASK-0" in res[1]["content"] and "read_file x" in res[1]["content"]
    assert res[2]["role"] != "tool"
    assert res[-1]["content"] == "ACTIVE_TURN"
    assert sum(kg.estimate_tokens(kg._msg_text(m)) for m in res) <= 20000
    assert msgs[1]["content"] == original_head  # input list not mutated


def test_extra_tokens_counts_top_level_system():
    msgs = [{"role": "user", "content": "x" * 3500}] * 5
    assert kg.apply_context_window_guard(msgs, 8000) is None
    assert kg.apply_context_window_guard(msgs, 8000, extra_tokens=5000) is not None


def test_egress_applies_budget_only_for_budgeted_provider():
    assert kg.max_input_tokens_for("nguyenha", CFG) == 550000
    assert kg.max_input_tokens_for("kiro", CFG) == 550000
    cfg_small = {"tools": {"kiro_filter_guard": {"max_input_tokens_by_provider": {"nguyenha": 160000}}}}
    p1 = {"messages": _long_session(n=40, size=20000)}
    p2 = {"messages": _long_session(n=40, size=20000)}
    kg.apply_kiro_egress_guards(p1, "nguyenha", "m", cfg_small)
    kg.apply_kiro_egress_guards(p2, "pix4k", "m", cfg_small)
    assert len(p1["messages"]) < len(p2["messages"])
    assert sum(kg.estimate_tokens(kg._msg_text(m)) for m in p1["messages"]) <= 160000


def test_is_kiro_target_5_signals():
    cfg_mixed = {
        "providers": {
            "nguyenha": {
                "models": [
                    {"id": "claude-opus-5-5", "owned_by": "kiro"},
                    {"id": "claude-opus-4-6", "owned_by": "antigravity"},
                ]
            },
            "custom_aws": {
                "base_url": "https://runtime.us-east-1.kiro.dev",
                "models": [{"id": "claude-sonnet-5"}],
            },
            "relay_x": {
                "models": [{"id": "claude-opus-5-5-kiro"}, {"id": "claude-opus-5"}],
            },
        },
        "tools": {},
    }
    # Signal 1: per-model owned_by distinguishes kiro vs antigravity on the same provider
    assert kg.is_kiro_target("nguyenha", "claude-opus-5-5", cfg_mixed) is True
    assert kg.is_kiro_target("nguyenha", "claude-opus-4-6", cfg_mixed) is False
    # Non-kiro model on nguyenha must NOT have its tags rewritten
    p_ag = {"messages": [{"role": "user", "content": "In " + tag() + " plan"}]}
    kg.apply_kiro_egress_guards(p_ag, "nguyenha", "claude-opus-4-6", cfg_mixed)
    assert BAD.search(p_ag["messages"][0]["content"])

    # Signal 2: native kiro provider, kiro host URL, or 'kiro' in model ID
    assert kg.is_kiro_target("kiro", "claude-opus-5", cfg_mixed) is True
    assert kg.is_kiro_target("custom_aws", "claude-sonnet-5", cfg_mixed) is True
    assert kg.is_kiro_target("relay_x", "claude-opus-5-5-kiro", cfg_mixed) is True
    assert kg.is_kiro_target("relay_x", "claude-opus-5", cfg_mixed) is False

    # Signal 3: runtime auto-learned after content_filter hit
    kg.register_kiro_content_filter_hit("relay_x", "claude-opus-5")
    assert kg.is_kiro_target("relay_x", "claude-opus-5", cfg_mixed) is True
    assert kg.max_input_tokens_for("relay_x", cfg_mixed, target_model="claude-opus-5") == 550000
    kg._LEARNED_KIRO_TARGETS.discard(("relay_x", "claude-opus-5"))


def test_contract_tag_and_thinking_header_rewritten():
    raw = (
        "<cognitive_thinking_contract mode=\"logic\">\n"
        "CHATBOT THINKING HEADER: Thinking · Mode: LOGIC\n"
        "</cognitive_thinking_contract>"
    )
    out, n = kg.sanitize_text(raw)
    assert n == 3
    assert "cognitive_thinking_contract" not in out
    assert "CHATBOT THINKING HEADER" not in out
    assert "cognitive_analysis_contract" in out
    assert "CHATBOT ANALYSIS HEADER: Analysis" in out


def test_source_file_has_no_literal_trigger():
    import inspect
    assert not BAD.search(inspect.getsource(kg))
