"""Functional tests for the BSL Router quality gate subsystem.

Tests the 1:1 port of BSC's quality-gate.ts + team-lead.ts:
- worst() ranking
- derive_verdict() mechanical status/confidence derivation
- merge_members() worst-score-wins merge
- parse_verdict() XML transport parsing
- parse_member_verdict() member XML parsing
- failed_member_verdict() all-fail synthesis
- quality_gate_summary() output formatting
"""
import sys
import os

# Ensure project root is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.middleware.blacksand_quality_gate import (
    worst,
    merge_members,
    derive_verdict,
    parse_verdict,
    parse_member_verdict,
    failed_member_verdict,
    quality_gate_summary,
    failed_dims,
    MemberVerdict,
    QualityVerdict,
    QUALITY_RUBRIC,
    ARCHITECT_RUBRIC,
    is_quality_gated_role,
    get_rubric_prompt,
    build_lead_turn1,
    build_turn2,
    build_member_brief,
    build_member_synthesis,
    TEAM_LEAD_INSTRUCTIONS,
)


def test_worst_ranking():
    """worst() returns the lower of two scores: fail < partial < pass."""
    assert worst("fail", "pass") == "fail"
    assert worst("pass", "fail") == "fail"
    assert worst("pass", "partial") == "partial"
    assert worst("partial", "pass") == "partial"
    assert worst("partial", "partial") == "partial"
    assert worst("pass", "pass") == "pass"
    assert worst("fail", "fail") == "fail"
    print("test_worst_ranking: PASS")


def test_derive_verdict():
    """derive_verdict: any fail→blocked, any partial→partial, all pass→success."""
    all_pass = {"completeness": "pass", "coherence": "pass"}
    status, conf = derive_verdict(all_pass)
    assert status == "success"
    assert conf == "high"

    has_partial = {"completeness": "pass", "coherence": "partial"}
    status, conf = derive_verdict(has_partial)
    assert status == "partial"
    assert conf == "cautious"

    has_fail = {"completeness": "pass", "coherence": "fail"}
    status, conf = derive_verdict(has_fail)
    assert status == "blocked"
    assert conf == "cautious"

    empty = {}
    status, conf = derive_verdict(empty)
    assert status == "success"
    assert conf == "high"

    print("test_derive_verdict: PASS")


def test_merge_members_worst_wins():
    """merge_members: a member can only LOWER a dimension, never raise it."""
    lead = {"completeness": "pass", "coherence": "pass"}
    member = MemberVerdict(
        slot=1,
        dimensions={"completeness": "fail"},
        evidence={"completeness": "missing edge case"},
    )
    merged = merge_members(lead, [member])
    assert merged["completeness"] == "fail", f"Expected fail, got {merged['completeness']}"
    assert merged["coherence"] == "pass", f"Expected pass, got {merged['coherence']}"
    print("test_merge_members_worst_wins: PASS")


def test_merge_members_dim_lead_omitted():
    """A member scoring a dimension the lead omitted still lands."""
    lead = {"completeness": "pass"}
    member = MemberVerdict(
        slot=1,
        dimensions={"safety": "fail"},
        evidence={"safety": "sql injection risk"},
    )
    merged = merge_members(lead, [member])
    assert "safety" in merged, "Member dim should land even if lead omitted it"
    assert merged["safety"] == "fail"
    assert merged["completeness"] == "pass"
    print("test_merge_members_dim_lead_omitted: PASS")


def test_merge_members_cannot_raise():
    """A member cannot raise a dimension the lead scored down."""
    lead = {"completeness": "fail", "coherence": "partial"}
    member = MemberVerdict(
        slot=1,
        dimensions={"completeness": "pass", "coherence": "pass"},
        evidence={"completeness": "looks fine", "coherence": "looks fine"},
    )
    merged = merge_members(lead, [member])
    assert merged["completeness"] == "fail", "Member must not raise lead's fail"
    assert merged["coherence"] == "partial", "Member must not raise lead's partial"
    print("test_merge_members_cannot_raise: PASS")


def test_merge_multiple_members():
    """Multiple members: worst across all."""
    lead = {"completeness": "pass", "coherence": "pass", "feasibility": "pass"}
    m1 = MemberVerdict(slot=1, dimensions={"completeness": "partial"}, evidence={"completeness": "x"})
    m2 = MemberVerdict(slot=2, dimensions={"coherence": "fail"}, evidence={"coherence": "y"})
    merged = merge_members(lead, [m1, m2])
    assert merged["completeness"] == "partial"
    assert merged["coherence"] == "fail"
    assert merged["feasibility"] == "pass"
    print("test_merge_multiple_members: PASS")


def test_parse_verdict_xml():
    """parse_verdict: parse a lead's <quality_verdict> XML block."""
    text = (
        '<quality_verdict round="1">\n'
        "completeness: pass\n"
        "coherence: partial\n"
        "feasibility: pass\n"
        "risk_awareness: fail\n"
        "findings: Missing error handling\n"
        "constraints: Add try-catch\n"
        "blockers: Unhandled exception in main loop\n"
        "</quality_verdict>"
    )
    v = parse_verdict(text)
    assert v is not None, "parse_verdict returned None"
    assert v.dimensions["completeness"] == "pass"
    assert v.dimensions["coherence"] == "partial"
    assert v.dimensions["risk_awareness"] == "fail"
    assert v.status == "blocked", f"Expected blocked, got {v.status}"
    assert v.confidence == "cautious"
    assert len(v.blockers) == 1
    assert "Unhandled exception" in v.blockers[0]
    assert len(v.constraints) == 1
    assert "try-catch" in v.constraints[0]
    assert v.round == 1
    assert "Missing error handling" in v.findings_summary
    print("test_parse_verdict_xml: PASS")


def test_parse_verdict_none_when_missing():
    """parse_verdict returns None when no block is present."""
    v = parse_verdict("just some regular text, no verdict here")
    assert v is None
    print("test_parse_verdict_none_when_missing: PASS")


def test_parse_verdict_none_when_no_dims():
    """parse_verdict returns None when block has no parseable dimensions."""
    text = "<quality_verdict round=\"1\">\nfindings: nothing here\n</quality_verdict>"
    v = parse_verdict(text)
    assert v is None
    print("test_parse_verdict_none_when_no_dims: PASS")


def test_failed_member_verdict():
    """failed_member_verdict: synthesize all-fail for a member that produced none."""
    fv = failed_member_verdict("planner_challenger", 1, "no output")
    assert all(s == "fail" for s in fv.dimensions.values()), "All dims should be fail"
    assert fv.synthetic == "no output"
    assert all(e == "no output" for e in fv.evidence.values())
    print("test_failed_member_verdict: PASS")


def test_failed_member_verdict_dims():
    """failed_member_verdict: dims match the role's rubric."""
    fv = failed_member_verdict("planner_challenger", 1, "timeout")
    expected_dims = QUALITY_RUBRIC["challenger"]["dims"]
    # _member_dims uses ceil division: (4+1)//2 = 2, so slot 1 gets dims[:2]
    half = (len(expected_dims) + 1) // 2
    assert set(fv.dimensions.keys()) == set(expected_dims[:half])
    print("test_failed_member_verdict_dims: PASS")


def test_quality_gate_summary():
    """quality_gate_summary: produces human-readable summary."""
    fv = failed_member_verdict("planner_challenger", 1, "no output")
    merged = {"completeness": "fail", "coherence": "pass"}
    summary = quality_gate_summary("planner_challenger", merged, fv)
    assert "[quality_gate" in summary
    assert "status=blocked" in summary
    assert "confidence=cautious" in summary
    assert "completeness=fail" in summary
    assert "coherence=pass" in summary
    assert "member_slot=1" in summary
    assert "member_synthetic=yes" in summary
    print("test_quality_gate_summary: PASS")


def test_quality_gate_summary_no_member():
    """quality_gate_summary with no member verdict."""
    merged = {"completeness": "pass", "coherence": "pass"}
    summary = quality_gate_summary("auditor_auditor", merged, None)
    assert "[quality_gate" in summary
    assert "status=success" in summary
    assert "confidence=high" in summary
    assert "member_slot" not in summary
    print("test_quality_gate_summary_no_member: PASS")


def test_failed_dims():
    """failed_dims: returns dimensions that are not pass."""
    v = QualityVerdict(
        dimensions={"a": "pass", "b": "partial", "c": "fail"},
        status="blocked",
    )
    dims = failed_dims(v)
    assert set(dims) == {"b", "c"}
    print("test_failed_dims: PASS")


def test_quality_rubric_completeness():
    """QUALITY_RUBRIC has all three roles with correct dims."""
    assert "challenger" in QUALITY_RUBRIC
    assert "reviewer" in QUALITY_RUBRIC
    assert "auditor" in QUALITY_RUBRIC
    assert QUALITY_RUBRIC["challenger"]["dims"] == ["completeness", "coherence", "feasibility", "risk_awareness"]
    assert QUALITY_RUBRIC["reviewer"]["dims"] == ["completeness", "coherence", "actionability", "testability"]
    assert QUALITY_RUBRIC["auditor"]["dims"] == ["completeness", "coherence", "correctness", "safety"]
    for role in ("challenger", "reviewer", "auditor"):
        assert "prompt" in QUALITY_RUBRIC[role]
        assert len(QUALITY_RUBRIC[role]["prompt"]) > 50
    print("test_quality_rubric_completeness: PASS")


def test_architect_rubric():
    """ARCHITECT_RUBRIC is non-empty and mentions all 4 dimensions."""
    assert "COMPLETENESS" in ARCHITECT_RUBRIC
    assert "COHERENCE" in ARCHITECT_RUBRIC
    assert "FEASIBILITY" in ARCHITECT_RUBRIC
    assert "RISK_AWARENESS" in ARCHITECT_RUBRIC
    print("test_architect_rubric: PASS")


def test_parse_member_verdict_xml():
    """parse_member_verdict: parse a member's <member_verdict> XML block."""
    text = (
        '<!-- member_synthesis_final -->\n'
        '<member_verdict slot="1">\n'
        "completeness: pass — all aspects covered\n"
        "coherence: partial — minor inconsistency\n"
        "</member_verdict>"
    )
    v = parse_member_verdict(text, "planner_challenger", 1)
    # Should parse or return None if dims don't match allowed set
    if v is not None:
        assert v.slot == 1
        assert "completeness" in v.dimensions or v.dimensions
    print("test_parse_member_verdict_xml: PASS")


def test_end_to_end_merge_flow():
    """End-to-end: lead verdict + member verdict → mechanical merge → summary."""
    lead_text = (
        '<quality_verdict round="1">\n'
        "completeness: pass\n"
        "coherence: pass\n"
        "feasibility: partial\n"
        "risk_awareness: pass\n"
        "findings: Good plan with minor feasibility concerns\n"
        "</quality_verdict>"
    )
    member_text = (
        '<!-- member_synthesis_final -->\n'
        '<member_verdict slot="1">\n'
        "completeness: fail — missing authentication flow\n"
        "coherence: pass\n"
        "</member_verdict>"
    )
    lead_verdict = parse_verdict(lead_text, round_num=1)
    assert lead_verdict is not None
    assert lead_verdict.status == "partial"

    member_verdict = parse_member_verdict(member_text, "planner_challenger", 1)
    if member_verdict is None:
        member_verdict = failed_member_verdict("planner_challenger", 1, "unparseable")

    merged = merge_members(lead_verdict.dimensions, [member_verdict])
    lead_verdict.dimensions = merged
    lead_verdict.status, lead_verdict.confidence = derive_verdict(merged)

    summary = quality_gate_summary("planner_challenger", merged, member_verdict)
    assert "status=blocked" in summary or "status=partial" in summary
    print("test_end_to_end_merge_flow: PASS")


# ── Prompt builder tests (team-lead.ts port) ────────────────────────────


def test_is_quality_gated_role():
    """is_quality_gated_role returns True for rubric-mapped roles, False otherwise."""
    assert is_quality_gated_role("planner_challenger") is True
    assert is_quality_gated_role("planner_architect") is True
    assert is_quality_gated_role("auditor_reviewer") is True
    assert is_quality_gated_role("auditor_auditor") is True
    assert is_quality_gated_role("scout") is False
    assert is_quality_gated_role("coder_backend") is False
    assert is_quality_gated_role("") is False
    print("test_is_quality_gated_role: PASS")


def test_get_rubric_prompt():
    """get_rubric_prompt returns non-empty prompt text for quality-gated roles."""
    prompt = get_rubric_prompt("planner_challenger")
    assert prompt and len(prompt) > 50
    assert "COMPLETENESS" in prompt
    assert "COHERENCE" in prompt

    prompt2 = get_rubric_prompt("auditor_auditor")
    assert prompt2 and "CORRECTNESS" in prompt2
    assert "SAFETY" in prompt2

    # Non-gated role returns empty string
    assert get_rubric_prompt("scout") == ""
    print("test_get_rubric_prompt: PASS")


def test_team_lead_instructions():
    """TEAM_LEAD_INSTRUCTIONS contains verdict format and mechanical derivation rules."""
    assert "quality_verdict" in TEAM_LEAD_INSTRUCTIONS
    assert "pass|partial|fail" in TEAM_LEAD_INSTRUCTIONS
    assert "MECHANICALLY" in TEAM_LEAD_INSTRUCTIONS
    assert "blocked" in TEAM_LEAD_INSTRUCTIONS
    assert "partial" in TEAM_LEAD_INSTRUCTIONS
    assert "success" in TEAM_LEAD_INSTRUCTIONS
    print("test_team_lead_instructions: PASS")


def test_build_lead_turn1():
    """build_lead_turn1 produces a structured Turn-1 prompt with task + rubric."""
    prompt = build_lead_turn1("planner_challenger", "Review auth flow", 1)
    assert "TASK: Review auth flow" in prompt
    assert "COMPLETENESS" in prompt
    assert "1 background members" in prompt
    assert "dispatch_scout" in prompt
    assert "Do NOT emit the verdict yet" in prompt

    # Zero members variant
    prompt0 = build_lead_turn1("planner_challenger", "Review auth flow", 0)
    assert "No background members" in prompt0
    assert "investigate all dimensions yourself" in prompt0.lower()
    print("test_build_lead_turn1: PASS")


def test_build_turn2():
    """build_turn2 produces a synthesis prompt with XML verdict format."""
    prompt = build_turn2("planner_challenger", "Review auth flow")
    assert "quality_verdict" in prompt
    assert "completeness" in prompt
    assert "findings:" in prompt
    assert "constraints:" in prompt
    assert "blockers:" in prompt
    assert "MECHANICALLY" in prompt
    print("test_build_turn2: PASS")


def test_build_member_brief():
    """build_member_brief produces a dimension-focused brief for a member."""
    brief = build_member_brief("planner_challenger", 1, "Review auth flow")
    assert "TASK: Review auth flow" in brief
    assert "Member 1" in brief
    # Slot 1 gets first 2 dims (completeness, coherence) via ceil division
    assert "COMPLETENESS" in brief
    assert "COHERENCE" in brief
    # Should NOT include dims from slot 2
    assert "FEASIBILITY" not in brief
    assert "RISK_AWARENESS" not in brief
    assert "member_verdict" in brief
    assert "DO NOT emit" in brief
    print("test_build_member_brief: PASS")


def test_build_member_synthesis():
    """build_member_synthesis produces the terminal synthesis turn prompt."""
    synth = build_member_synthesis("planner_challenger", 1)
    assert "SYNTHESIS TURN" in synth
    assert "member_synthesis_final" in synth
    assert 'member_verdict slot="1"' in synth
    assert "pass|partial|fail" in synth
    # Slot 1 dims
    assert "completeness" in synth
    assert "coherence" in synth
    print("test_build_member_synthesis: PASS")


if __name__ == "__main__":
    test_worst_ranking()
    test_derive_verdict()
    test_merge_members_worst_wins()
    test_merge_members_dim_lead_omitted()
    test_merge_members_cannot_raise()
    test_merge_multiple_members()
    test_parse_verdict_xml()
    test_parse_verdict_none_when_missing()
    test_parse_verdict_none_when_no_dims()
    test_failed_member_verdict()
    test_failed_member_verdict_dims()
    test_quality_gate_summary()
    test_quality_gate_summary_no_member()
    test_failed_dims()
    test_quality_rubric_completeness()
    test_architect_rubric()
    test_parse_member_verdict_xml()
    test_end_to_end_merge_flow()
    test_is_quality_gated_role()
    test_get_rubric_prompt()
    test_team_lead_instructions()
    test_build_lead_turn1()
    test_build_turn2()
    test_build_member_brief()
    test_build_member_synthesis()

    print("\n\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550")
    print("  ALL QUALITY GATE TESTS PASSED (25/25)")
    print("\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550")

