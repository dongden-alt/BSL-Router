"""Compaction order-preserving reconstruction + smart gate tests.

Regression coverage for the glm-5.3-flash coder-2 "messages 参数非法" 400s:
the old `_identify_compactable_tail` returned `extra_pinned + pinned_recent_raw`,
hoisting mid-history tool-chain messages to the FRONT of the rebuilt array —
the first non-system message became assistant tool_use / user tool_result and
upstream validators rejected the request (92% of that model's messages-param
errors, production-proven).

Also covers: smart min-savings gate (Part B.3), calibrated estimator +
record_usage EMA (Part B.1/B.2), reconstruction validator fail-open
(Part A.4), first-user-message pinning (Part D1), and TAIL-TRIM default-lock
(Part D2). Pure unit — no network, no router boot.
"""
from __future__ import annotations

from typing import List

from app.middleware import compaction as C
from app.middleware.compaction import (
    Message,
    _identify_compactable_tail,
    _validate_reconstruction,
    record_usage,
    _resolve_token_ratio,
    _calibrated_count_tokens,
    _count_tokens,
)


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _sys(text: str = "system prompt") -> Message:
    return Message(role="system", content=text)


def _user(text: str) -> Message:
    return Message(role="user", content=text)


def _asst(text: str, tool_calls: list | None = None) -> Message:
    return Message(role="assistant", content=text, tool_calls=tool_calls)


def _tool(tc_id: str, text: str = "tool output") -> Message:
    return Message(role="tool", content=text, tool_call_id=tc_id)


def _tc(tc_id: str, name: str = "read_file") -> dict:
    return {"id": tc_id, "type": "function",
            "function": {"name": name, "arguments": "{}"}}


def _old_history(n_pairs: int = 6) -> List[Message]:
    """`n_pairs` old user/assistant turns with big filler text (compactable)."""
    filler = "x" * 400
    msgs: List[Message] = []
    for i in range(n_pairs):
        msgs.append(_user(f"old task step {i}: {filler}"))
        msgs.append(_asst(f"old response {i}: {filler}"))
    return msgs


# ─── Part A: order preservation ──────────────────────────────────────────────

def test_tool_chain_not_hoisted_to_front():
    """A mid-history tool chain must keep its original position, not be moved
    to the front of the pinned group (the production 400 bug)."""
    msgs = [_sys(), _user("first task"), _asst("working")] + _old_history(4)
    # Tool chain sits in the MIDDLE of the old history
    msgs += [
        _user("run the build"),
        _asst("calling tool", tool_calls=[_tc("call_1")]),
        _tool("call_1"),
        _asst("build done"),
    ]
    msgs += _old_history(2)
    msgs += [_user("recent question"), _asst("recent answer")]

    sys_msgs, compactable, pinned = _identify_compactable_tail(msgs, pin_turns=3)

    # The tool chain messages must be pinned, not compacted
    pinned_ids = {id(m) for m in pinned}
    tool_msgs = [m for m in msgs if m.tool_call_id == "call_1" or m.tool_calls]
    assert all(id(m) in pinned_ids for m in tool_msgs)

    # Rebuilt array (sys + pinned) preserves the ORIGINAL relative order of
    # pinned messages — the fix means `pinned` is an ordered subsequence of
    # non_sys, never a hoisted concatenation.
    def _sig(m):
        return (m.role, C._msg_text(m))

    pinned_sigs = [_sig(m) for m in pinned]
    orig_non_sys_sigs = [_sig(m) for m in msgs if m.role != "system"]
    pos = 0
    for s in pinned_sigs:
        pos = orig_non_sys_sigs.index(s, pos) + 1  # raises if out of order
    assert True

    # The first non-system message of the rebuilt array is a user message
    first_non_sys = next(m for m in sys_msgs + pinned if m.role != "system")
    assert first_non_sys.role == "user"


def test_user_first_invariant_after_reconstruction():
    """After full reconstruction the first non-system message is the summary
    user message inserted at the position of the first dropped message."""
    first_task = _user("THE ORIGINAL TASK")
    msgs = [_sys(), first_task] + _old_history(5) + [
        _user("task with tools"),
        _asst("tool time", tool_calls=[_tc("t9")]),
        _tool("t9"),
        _asst("done"),
        _user("final question"),
    ]
    sys_msgs, compactable, pinned = _identify_compactable_tail(msgs, pin_turns=3)
    assert all(id(m) != id(first_task) for m in compactable)  # D1 pin

    # Insert the summary at the position of the first dropped message (as
    # apply_compaction does), keeping everything else in original order.
    summary = Message(role="user", content="[CONTEXT SUMMARY]\nstate map")
    compactable_ids = {id(m) for m in compactable}
    rebuilt = []
    inserted = False
    for m in msgs:
        if id(m) in compactable_ids:
            if not inserted:
                rebuilt.append(summary)
                inserted = True
            continue
        rebuilt.append(m)
    assert _validate_reconstruction(msgs, rebuilt)
    first = next(m for m in rebuilt if m.role != "system")
    assert first.role == "user"
    # First non-system user message is either the pinned original task (D1)
    # or, when no earlier user message survives, the summary itself.
    if first is not first_task:
        assert first is summary and first.content.startswith("[CONTEXT SUMMARY]")


def test_tool_pairing_intact_no_orphans():
    """Reconstruction must not create orphan tool_use / tool_result pairs."""
    msgs = [_sys()] + _old_history(5) + [
        _user("tool task"),
        _asst("call", tool_calls=[_tc("t1"), _tc("t2")]),
        _tool("t1"),
        _tool("t2"),
        _asst("results in"),
        _user("next"),
    ]
    sys_msgs, compactable, pinned = _identify_compactable_tail(msgs, pin_turns=3)
    assert not any(m.tool_calls or m.tool_call_id for m in compactable)

    summary = Message(role="user", content="[CONTEXT SUMMARY] s")
    compactable_ids = {id(m) for m in compactable}
    rebuilt = []
    inserted = False
    for m in msgs:
        if id(m) in compactable_ids:
            if not inserted:
                rebuilt.append(summary)
                inserted = True
            continue
        rebuilt.append(m)
    assert _validate_reconstruction(msgs, rebuilt)

    use = C._tool_use_ids(rebuilt)
    res = C._tool_result_ids(rebuilt)
    assert use == {"t1", "t2"} and res == {"t1", "t2"}


# ─── Part A.4: validator fail-open ───────────────────────────────────────────

def test_validator_catches_injected_orphan():
    """An orphan tool_result injected by a bad reconstruction → validator False."""
    msgs = [_sys(), _user("task"), _asst("hi"), _user("bye")]
    good = [_sys(), _user("task"), _asst("hi"), _user("bye")]
    assert _validate_reconstruction(msgs, good)

    # Orphan tool_result with no matching tool_use
    orphaned = good + [_tool("ghost_id")]
    assert not _validate_reconstruction(msgs, orphaned)

    # Orphan tool_use with no matching tool_result
    orphan_use = [_sys(), _user("task"),
                  _asst("call", tool_calls=[_tc("lonely")]),
                  _user("bye")]
    assert not _validate_reconstruction(msgs, orphan_use)


def test_validator_catches_reordered_pinned():
    """Reordered pinned messages must fail the order invariant."""
    msgs = [_sys(), _user("A"), _asst("a"), _user("B"), _asst("b"), _user("C")]
    reordered = [_sys(), _user("B"), _asst("b"), _user("A"), _asst("a"), _user("C")]
    assert not _validate_reconstruction(msgs, reordered)


def test_validator_catches_non_user_first():
    msgs = [_sys(), _user("task"), _asst("hi")]
    bad = [_asst("hi"), _user("task")]
    assert not _validate_reconstruction(msgs, bad)


# ─── Part B: calibrated estimator + smart gate ───────────────────────────────

def test_default_token_ratio_calibration():
    C._TOKEN_RATIO.clear()
    C._TOKEN_RATIO.update({"global": C.DEFAULT_TOKEN_RATIO})
    assert _resolve_token_ratio("glm-5.3-flash") == 1.75
    assert _resolve_token_ratio("anything") == 1.75
    raw = _count_tokens([_user("y" * 400)])
    assert _calibrated_count_tokens([_user("y" * 400)], "glm-5.3-flash") == int(raw * 1.75)


def test_per_model_ratio_longest_prefix_wins():
    C._TOKEN_RATIO.clear()
    C._TOKEN_RATIO.update({"global": 1.75, "glm": 2.0, "glm-5.3": 2.5})
    assert _resolve_token_ratio("glm-5.3-flash") == 2.5
    assert _resolve_token_ratio("glm-5.2") == 2.0
    assert _resolve_token_ratio("deepseek-v4") == 1.75


def test_record_usage_ema_moves_ratio_toward_actual(monkeypatch):
    C._TOKEN_RATIO.clear()
    C._TOKEN_RATIO.update({"global": 1.75})
    monkeypatch.setattr(C, "_RATIO_EMA_ALPHA", 1.0)  # full step for test clarity
    # Model consistently consumes 2x the estimate → ratio must move up
    for _ in range(5):
        record_usage("test-model", 1000, 2000)
    assert abs(C._TOKEN_RATIO["test-model"] - 2.0) < 1e-9
    # Clamped to [1.0, 3.0]: a 10x sample must not blow past 3.0
    record_usage("test-model", 1000, 10000)
    assert C._TOKEN_RATIO["test-model"] <= 3.0
    # Zero/negative inputs are ignored
    record_usage("test-model", 0, 5000)
    assert C._TOKEN_RATIO["test-model"] <= 3.0


def test_min_savings_gate_skips_small_tail(monkeypatch, capsys):
    """Production failing case: tail ~4% of total → skip (Part B.3)."""
    from app.models import ChatCompletionRequest
    # Huge PINNED tool chains mid-history, tiny compactable turns around
    # them: tail (small old turns) is a tiny share of total → skip
    # (production failing case: estimated savings ~4%). The pinned
    # tool_result messages carry the huge payload; the compactable
    # "old q"/"old a" turns are deliberately tiny.
    msgs = [_sys(), _user("small task")]
    for n in range(10):
        msgs += [_asst(f"tool call {n}", tool_calls=[_tc(f"big{n}")]),
                 _tool(f"big{n}", "huge tool output " + "z" * 30000)]
    msgs += [_user("old q " + "x" * 100), _asst("old a " + "x" * 100)]
    msgs += [_user("final q"), _asst("final a")]  # true recent turns at the end
    req = ChatCompletionRequest(model="glm-5.3-flash", messages=msgs)
    config = {"tools": {
        "compaction_enabled": True,
        "compaction_threshold": 100000,  # high-water above total so Gate 6b doesn't fire
        "compaction_min_savings_pct": 30,
        "compaction_code_strip_turns": 1,  # pin only last 2 msgs → old pairs compactable
    }}
    import asyncio
    result = asyncio.run(C.apply_compaction(req, None, config, "glm-provider"))
    assert result is req  # fail-open returns the same request
    out = capsys.readouterr().out
    assert "savings too small" in out


def test_min_savings_gate_runs_at_large_tail(monkeypatch, capsys):
    """Tail ~40%+ of total → gate passes, proceeds toward model resolution."""
    from app.models import ChatCompletionRequest
    # 8 old pairs (compactable) vs 1 pinned pair → tail is the clear majority
    msgs = [_sys()] + _old_history(8) + [_user("recent q"), _asst("recent a")]
    req = ChatCompletionRequest(model="glm-5.3-flash", messages=msgs)
    config = {"tools": {
        "compaction_enabled": True,
        "compaction_threshold": 100,
        "compaction_min_savings_pct": 30,
        "compaction_code_strip_turns": 1,
        # No compaction_model configured → stops after gates with a log line
        # proving it got PAST the savings gate.
    }}
    import asyncio
    asyncio.run(C.apply_compaction(req, None, config, "glm-provider"))
    out = capsys.readouterr().out
    assert "savings too small" not in out
    assert "No compaction_model configured" in out


def test_zero_min_savings_disables_gate(monkeypatch, capsys):
    """compaction_min_savings_pct=0 → gate disabled entirely."""
    from app.models import ChatCompletionRequest
    huge_pinned = [_asst("pinned " + "z" * 20000) for _ in range(3)]
    msgs = [_sys()] + _old_history(3) + huge_pinned
    req = ChatCompletionRequest(model="glm-5.3-flash", messages=msgs)
    config = {"tools": {
        "compaction_enabled": True,
        "compaction_threshold": 100,
        "compaction_min_savings_pct": 0,
        "compaction_code_strip_turns": 1,
    }}
    import asyncio
    asyncio.run(C.apply_compaction(req, None, config, "glm-provider"))
    out = capsys.readouterr().out
    assert "savings too small" not in out


# ─── Part D1: first-user-message pinning ─────────────────────────────────────

def test_first_user_message_never_compactable():
    """The original task statement is pinned even when it's deep in the
    compactable region, and it keeps its original relative position."""
    first_task = _user("THE ORIGINAL TASK — fix the login bug")
    msgs = [_sys(), first_task] + _old_history(8) + [
        _user("recent"), _asst("recent"), _user("recent2"), _asst("recent2"),
        _user("recent3"), _asst("recent3"),
    ]
    sys_msgs, compactable, pinned = _identify_compactable_tail(msgs, pin_turns=3)
    # First user message must NOT be in the compactable set
    assert all(id(first_task) != id(m) for m in compactable)
    assert any(id(m) == id(first_task) for m in pinned)

    # In the rebuilt array it comes BEFORE the summary and before everything
    # that came after it in the original order.
    summary = Message(role="user", content="[CONTEXT SUMMARY] s")
    compactable_ids = {id(m) for m in compactable}
    rebuilt = []
    for m in msgs:
        if id(m) in compactable_ids:
            rebuilt.append(summary)
            continue
        rebuilt.append(m)
    assert _validate_reconstruction(msgs, rebuilt)
    assert rebuilt.index(first_task) < rebuilt.index(summary)
    # And after the summary, only messages that originally followed the first
    # compactable message may appear… simplest check: original subsequence order
    non_sum = [m for m in rebuilt if m is not summary]
    orig_idx = [msgs.index(m) for m in non_sum]
    assert orig_idx == sorted(orig_idx)


def test_first_user_in_summary_input_never():
    """Summary input (compactable) never contains the first user message."""
    first_task = _user("original goal statement")
    msgs = [_sys(), first_task] + ([_user("old q"), _asst("old a")] * 3)
    sys_msgs, compactable, pinned = _identify_compactable_tail(msgs, pin_turns=2)
    assert first_task not in compactable
    assert all(id(m) != id(first_task) for m in compactable)


# ─── Part D2: TAIL-TRIM default lock ─────────────────────────────────────────

def test_tail_trim_unreachable_by_default(monkeypatch, capsys):
    """Missing/0 compaction_tail_trim_threshold → trim path never fires."""
    from app.models import ChatCompletionRequest
    msgs = [_sys()] + _old_history(8) + [
        _user("r1"), _asst("r1"), _user("r2"), _asst("r2"), _user("r3"), _asst("r3"),
    ]
    req = ChatCompletionRequest(model="glm-5.3-flash", messages=msgs)
    original_len = len(req.messages)

    for tools in (
        {"compaction_enabled": True, "compaction_threshold": 100},          # missing
        {"compaction_enabled": True, "compaction_threshold": 100,
         "compaction_tail_trim_threshold": 0},                              # explicit 0
    ):
        # No compaction_model → we should see the config-skip log, NOT tail-trim
        cfg = {"tools": dict(tools)}
        import asyncio
        asyncio.run(C.apply_compaction(req, None, cfg, "glm-provider"))
        out = capsys.readouterr().out
        assert "TAIL-TRIM" not in out
        assert len(req.messages) == original_len or "No compaction_model" in out


def test_tail_trim_when_enabled_preserves_order_and_first_user(capsys):
    """Explicitly enabled tail-trim still preserves order + first-user pin."""
    from app.models import ChatCompletionRequest
    first_task = _user("THE ORIGINAL TASK")
    msgs = [_sys(), first_task] + _old_history(6) + [
        _user("r1"), _asst("r1"), _user("r2"), _asst("r2"), _user("r3"), _asst("r3"),
    ]
    req = ChatCompletionRequest(model="glm-5.3-flash", messages=msgs)
    config = {"tools": {
        "compaction_enabled": True,
        "compaction_threshold": 100,
        "compaction_min_savings_pct": 0,
        "compaction_tail_trim_threshold": 100,  # total > 100 → trim fires
    }}
    import asyncio
    result = asyncio.run(C.apply_compaction(req, None, config, "glm-provider"))
    out = capsys.readouterr().out
    assert "TAIL-TRIM" in out
    # First user message survived the drop
    assert any(m is first_task for m in result.messages)
    # Order preserved among kept messages
    kept_sigs = [(m.role, C._msg_text(m)) for m in result.messages]
    assert kept_sigs == sorted(kept_sigs, key=lambda s: [
        i for i, m in enumerate(msgs) if (m.role, C._msg_text(m)) == s
    ][0]) or _validate_reconstruction(msgs, result.messages)
    # First non-system message is still a user message
    assert next(m for m in result.messages if m.role != "system").role == "user"


# ─── Part D3: quality telemetry log line ─────────────────────────────────────

def test_success_log_reports_kept_vs_summarized():
    import inspect
    src = inspect.getsource(C.apply_compaction)
    assert "first-user" in src and "tool-chain" in src and "summarized" in src


def test_summary_prompt_hardened():
    """Part D4: goal/decision preservation requirement in the prompt."""
    import inspect
    src = inspect.getsource(C._call_compaction_model)
    assert "Preserve the user's original goal and all explicit user decisions/constraints word-for-word" in src
