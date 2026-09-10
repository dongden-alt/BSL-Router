"""Quality gate subsystem — port of BSC quality-gate.ts + team-lead.ts.

Mechanical verdict derivation: any fail → blocked, any partial → partial,
all pass → success. Members can only LOWER a dimension, never raise it.
A member that produced no parseable verdict is represented as all-fail,
not absent: silent loss must not be indistinguishable from success.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Literal

# ── Types ───────────────────────────────────────────────────────────────

DimensionScore = Literal["pass", "partial", "fail"]

_RANK: Dict[str, int] = {"fail": 0, "partial": 1, "pass": 2}


def worst(a: str, b: str) -> str:
    """Lower of two scores. fail < partial < pass."""
    return a if _RANK.get(a, 0) <= _RANK.get(b, 0) else b


# ── Dataclasses ─────────────────────────────────────────────────────────


@dataclass
class MemberVerdict:
    """A single member's scores for the dimensions it was assigned.

    Members cover a deterministic subset of the role's rubric (see
    ``_member_dims``). A member that produced no parseable verdict is
    represented as an all-fail MemberVerdict, not as an absent one.
    """

    slot: int
    dimensions: Dict[str, str] = field(default_factory=dict)
    evidence: Dict[str, str] = field(default_factory=dict)
    synthetic: Optional[str] = None  # set when synthesized rather than parsed


@dataclass
class QualityVerdict:
    """Mechanically-derived quality verdict from dimension scores."""

    dimensions: Dict[str, str] = field(default_factory=dict)
    status: str = "success"  # "success" | "partial" | "blocked"
    confidence: str = "high"  # "high" | "cautious"
    constraints: List[str] = field(default_factory=list)
    blockers: List[str] = field(default_factory=list)
    round: int = 1
    findings_summary: str = ""


# ── Mechanical functions ────────────────────────────────────────────────


def merge_members(
    lead: Dict[str, str], members: List[MemberVerdict]
) -> Dict[str, str]:
    """Worst-score-wins merge of member scores into the lead's.

    A member can only LOWER a dimension, never raise it — the lead cannot
    talk its way past a member's fail, and a member cannot inflate a
    dimension the lead scored down. Dimensions no member scored are left
    exactly as the lead reported them.

    A member scoring a dimension the lead omitted still lands: the lead
    dropping a dimension must not launder a member's fail.
    """
    out = dict(lead)
    for m in members:
        for dim, score in m.dimensions.items():
            if dim in out and out[dim]:
                out[dim] = worst(out[dim], score)
            else:
                out[dim] = score
    return out


def derive_verdict(dimensions: Dict[str, str]) -> Tuple[str, str]:
    """Derive status and confidence from dimension scores. Pure function.

    Returns ``(status, confidence)``:
    - any fail    → ("blocked", "cautious")
    - any partial → ("partial", "cautious")
    - all pass    → ("success", "high")
    """
    scores = list(dimensions.values())
    if any(s == "fail" for s in scores):
        return ("blocked", "cautious")
    if any(s == "partial" for s in scores):
        return ("partial", "cautious")
    return ("success", "high")


def failed_dims(verdict: QualityVerdict) -> List[str]:
    """Return dimensions that are not pass (partial or fail)."""
    return [
        dim for dim, score in verdict.dimensions.items() if score != "pass"
    ]


# ── Quality rubric (quality-gate.ts QUALITY_RUBRIC) ────────────────────

QUALITY_RUBRIC: Dict[str, Dict] = {
    "challenger": {
        "dims": ["completeness", "coherence", "feasibility", "risk_awareness"],
        "prompt": (
            "Your work will be evaluated on 4 dimensions:\n"
            "1. COMPLETENESS: Are all aspects of the task addressed?\n"
            "2. COHERENCE: Is the reasoning internally consistent and logically structured?\n"
            "3. FEASIBILITY: Can the proposed approach be implemented with available resources?\n"
            "4. RISK_AWARENESS: Are edge cases, failure modes, and trade-offs identified?\n"
            "Score each dimension as pass, partial, or fail."
            " The overall verdict is mechanically derived."
        ),
    },
    "reviewer": {
        "dims": ["completeness", "coherence", "actionability", "testability"],
        "prompt": (
            "Your work will be evaluated on 4 dimensions:\n"
            "1. COMPLETENESS: Are all aspects of the task addressed?\n"
            "2. COHERENCE: Is the reasoning internally consistent and logically structured?\n"
            "3. ACTIONABILITY: Can a developer act on this directly without ambiguity?\n"
            "4. TESTABILITY: Are success criteria and verification steps clearly defined?\n"
            "Score each dimension as pass, partial, or fail."
            " The overall verdict is mechanically derived."
        ),
    },
    "auditor": {
        "dims": ["completeness", "coherence", "correctness", "safety"],
        "prompt": (
            "Your work will be evaluated on 4 dimensions:\n"
            "1. COMPLETENESS: Are all aspects of the task addressed?\n"
            "2. COHERENCE: Is the reasoning internally consistent and logically structured?\n"
            "3. CORRECTNESS: Does the code do what it claims? Are there bugs or logic errors?\n"
            "4. SAFETY: Are there security issues, data loss risks, or breaking changes?\n"
            "Score each dimension as pass, partial, or fail."
            " The overall verdict is mechanically derived."
        ),
    },
}

ARCHITECT_RUBRIC = (
    "Your architecture draft will be evaluated by a quality gate on these dimensions:\n"
    "1. COMPLETENESS: All aspects addressed\n"
    "2. COHERENCE: Internally consistent and logically structured\n"
    "3. FEASIBILITY: Implementable with available resources\n"
    "4. RISK_AWARENESS: Edge cases, failure modes, and trade-offs identified\n"
    "Ensure your draft addresses each dimension proactively."
)

# ── BSL sub_role → BSC rubric role mapping ─────────────────────────────

_RUBRIC_ROLE_MAP: Dict[str, str] = {
    # Planner family
    "planner_challenger": "challenger",
    "planner_architect": "challenger",
    # Auditor family
    "auditor_reviewer": "reviewer",
    "auditor_auditor": "auditor",
}


def _rubric_dims_for_role(role: str) -> List[str]:
    """Map a BSL sub_role to its quality rubric dimension list."""
    rubric_key = _RUBRIC_ROLE_MAP.get(role)
    if not rubric_key:
        return []
    return QUALITY_RUBRIC.get(rubric_key, {}).get("dims", [])


def _member_dims(role: str, slot: int) -> List[str]:
    """Deterministic dimension split across member slots (team-lead.ts).

    slot=1 → first half of dimensions, slot=2 → second half.
    Ceil division so slot 1 gets the extra when the count is odd.
    """
    dims = _rubric_dims_for_role(role)
    half = (len(dims) + 1) // 2
    return dims[:half] if slot == 1 else dims[half:]


# ── Verdict transport XML parsers (team-lead.ts) ───────────────────────

_VERDICT_RE = re.compile(
    r'<quality_verdict(?:\s+round="(\d+)")?>([\s\S]*?)</quality_verdict>'
)
_DIM_RE = re.compile(r'^(\w+):\s*(pass|partial|fail)\s*$', re.MULTILINE)
_MEMBER_RE = re.compile(
    r'<!-- member_synthesis_final -->\s*'
    r'<member_verdict(?:\s+slot="(\d+)")?>([\s\S]*?)</member_verdict>'
)
_MEMBER_DIM_RE = re.compile(
    r'^(\w+):\s*(pass|partial|fail)\s*(?:[\u2014\-\u2013|]\s*(.*))?$',
    re.MULTILINE,
)


def _verdict_field(body: str, name: str) -> Optional[str]:
    """Extract a named field from the verdict body text."""
    m = re.search(rf'^{name}:\s*(.+)$', body, re.MULTILINE)
    return m.group(1).strip() if m else None


def _split_list(raw: Optional[str]) -> List[str]:
    """Split semicolon-separated list, filtering empties."""
    if not raw:
        return []
    return [s.strip() for s in raw.split(";") if s.strip()]


def parse_verdict(text: str, round_num: int = 1) -> Optional[QualityVerdict]:
    """Parse a lead's ``<quality_verdict>`` XML block from text output.

    Returns None when no block is present or no dimension line parses.
    """
    match = _VERDICT_RE.search(text or "")
    if not match:
        return None
    block_round = int(match.group(1)) if match.group(1) else round_num
    body = match.group(2)
    dims: Dict[str, str] = {}
    for m in _DIM_RE.finditer(body):
        dims[m.group(1).lower()] = m.group(2)
    if not dims:
        return None
    findings = _verdict_field(body, "findings")
    constraints = _verdict_field(body, "constraints")
    blockers = _verdict_field(body, "blockers")
    status, confidence = derive_verdict(dims)
    return QualityVerdict(
        dimensions=dims,
        status=status,
        confidence=confidence,
        constraints=_split_list(constraints),
        blockers=_split_list(blockers),
        round=block_round or round_num,
        findings_summary=findings or "",
    )


def parse_member_verdict(
    text: str, role: str, slot: int
) -> Optional[MemberVerdict]:
    """Parse a member's ``<member_verdict>`` XML block from text output.

    Returns None when no block is present or no dimension line parses.
    Unassigned dimensions are dropped: a member must not score outside
    its own scope.
    """
    match = _MEMBER_RE.search(text or "")
    if not match:
        return None
    allowed = set(_member_dims(role, slot))
    dimensions: Dict[str, str] = {}
    evidence: Dict[str, str] = {}
    for m in _MEMBER_DIM_RE.finditer(match.group(2)):
        dim_name = m.group(1).lower()
        if allowed and dim_name not in allowed:
            continue
        dimensions[dim_name] = m.group(2)
        evidence[dim_name] = (m.group(3) or "").strip()
    if not dimensions:
        return None
    # All assigned dimensions must be present with evidence
    if allowed:
        for dim in allowed:
            if dim not in dimensions or not evidence.get(dim):
                return None
    slot_num = int(match.group(1)) if match.group(1) else slot
    return MemberVerdict(
        slot=slot_num, dimensions=dimensions, evidence=evidence
    )


def failed_member_verdict(
    role: str, slot: int, why: str
) -> MemberVerdict:
    """Synthesize an all-fail verdict for a member that produced none.

    A member that emitted nothing consumable did not do its job, and
    treating that as neutral makes silent loss indistinguishable from
    success — the exact hole worst-score-wins closes.
    """
    dims = _member_dims(role, slot)
    return MemberVerdict(
        slot=slot,
        dimensions={d: "fail" for d in dims},
        evidence={d: why for d in dims},
        synthetic=why,
    )


def quality_gate_summary(
    sub_role: str,
    merged_dims: Dict[str, str],
    member_verdict: Optional[MemberVerdict],
) -> str:
    """Build a human-readable summary line for the quality gate result.

    Appended to the lead's output text so downstream phases and the user
    can see the mechanical verdict.
    """
    status, confidence = derive_verdict(merged_dims)
    dims_str = ", ".join(f"{d}={s}" for d, s in merged_dims.items())
    member_info = ""
    if member_verdict is not None:
        member_info = (
            f" member_slot={member_verdict.slot}"
            f" member_synthetic={'yes' if member_verdict.synthetic else 'no'}"
        )
    return (
        f"\n\n[quality_gate \u2014 {sub_role}] "
        f"status={status} confidence={confidence} "
        f"dims={{{dims_str}}}{member_info}"
    )


# ── Prompt builders (port of team-lead.ts buildLeadTurn1/buildTurn2/etc) ──

TEAM_LEAD_INSTRUCTIONS = (
    "You are a TEAM LEAD for this quality review phase. "
    "You will evaluate the work across multiple dimensions.\n\n"
    "You have TWO turns:\n\n"
    "TURN 1 \u2014 ANALYZE & DISPATCH:\n"
    "1. Analyze the work against each dimension. Write 2-3 sentences per dimension.\n"
    "2. If you need deeper investigation, use dispatch_scout to spawn scout tasks "
    "(up to the limit specified).\n"
    "3. Each scout should investigate a specific concern.\n"
    "4. Do NOT emit a verdict in Turn 1. This is analysis only.\n\n"
    "TURN 2 \u2014 SYNTHESIZE & VERDICT (triggered after scout results arrive):\n"
    "1. Incorporate scout findings into your analysis.\n"
    "2. Score each dimension: pass, partial, or fail.\n"
    "3. Emit your verdict in this EXACT format:\n\n"
    '<quality_verdict round="N">\n'
    "dimension_name: pass|partial|fail\n"
    "... (one line per dimension)\n"
    "findings: One paragraph summarizing your assessment.\n"
    "constraints: Semicolon-separated mandatory constraints (if any partial).\n"
    "blockers: Semicolon-separated blockers (if any fail).\n"
    "</quality_verdict>\n\n"
    "The overall verdict is MECHANICALLY DERIVED:\n"
    "- Any fail \u2192 blocked\n"
    "- Any partial \u2192 partial (constraints become mandatory for the next phase)\n"
    "- All pass \u2192 success\n\n"
    "Be precise. Partial dimensions generate constraints the next phase MUST address."
)


def _dim_description(role: str, dim: str) -> str:
    """Extract per-dimension description from the QUALITY_RUBRIC prompt text."""
    rubric_key = _RUBRIC_ROLE_MAP.get(role, role)
    entry = QUALITY_RUBRIC.get(rubric_key)
    if not entry:
        return ""
    m = re.search(
        rf'^\d+\.\s*{re.escape(dim.upper())}:\s*(.+)$',
        entry["prompt"],
        re.MULTILINE,
    )
    return m.group(1).strip() if m else ""


def build_lead_turn1(role: str, task: str, member_count: int) -> str:
    """Build the lead's Turn-1 user-message prompt.

    Port of BSC team-lead.ts buildLeadTurn1. Tells the lead about background
    members and to wait for their results before emitting a verdict.
    """
    rubric_key = _RUBRIC_ROLE_MAP.get(role, role)
    entry = QUALITY_RUBRIC.get(rubric_key)
    lines = [
        f"TASK: {task}",
        "",
        entry["prompt"] if entry else "",
        "",
    ]
    if member_count > 0:
        lines.append(
            f"{member_count} background members are investigating specific "
            "dimensions in parallel. Wait for their results as "
            "<background_task_results>, then incorporate into your analysis."
        )
    else:
        lines.append("No background members spawned. Investigate all dimensions yourself.")
    lines.append("")
    lines.append(
        "If you need deeper investigation on a dimension, "
        "use dispatch_scout (max 1 per dimension)."
    )
    if member_count > 0:
        lines.append(
            "Analyze each dimension. Do NOT emit the verdict yet \u2014 "
            "wait for background results first."
        )
    else:
        lines.append("Analyze each dimension, then emit your verdict.")
    return "\n".join(lines)


def build_turn2(role: str, task: str) -> str:
    """Build the lead's Turn-2 synthesis prompt.

    Port of BSC team-lead.ts buildTurn2. Instructs the lead to emit the
    final <quality_verdict> block with exact format.
    """
    rubric_key = _RUBRIC_ROLE_MAP.get(role, role)
    dims = QUALITY_RUBRIC.get(rubric_key, {}).get("dims", [])
    return "\n".join([
        "Scout results injected above (if any).",
        "",
        "Synthesize and emit your final quality verdict.",
        f"Score each dimension: {', '.join(dims)}" if dims else "Score each dimension.",
        "",
        "Emit your verdict in this EXACT format:",
        '<quality_verdict round="N">',
        *[f"{d}: pass|partial|fail" for d in dims],
        "findings: One paragraph summarizing your assessment.",
        "constraints: Semicolon-separated mandatory constraints (if any partial).",
        "blockers: Semicolon-separated blockers (if any fail).",
        "</quality_verdict>",
        "",
        "The overall verdict is MECHANICALLY DERIVED:",
        "- Any fail \u2192 blocked",
        "- Any partial \u2192 partial (constraints become mandatory for next phase)",
        "- All pass \u2192 success",
    ])


def build_member_brief(role: str, slot: int, task: str) -> str:
    """Build a dimension-focused brief for a background member.

    Port of BSC team-lead.ts buildMemberBrief. Each member covers a subset
    of the role's quality dimensions. slot=1 → first half, slot=2 → second half.
    """
    rubric_key = _RUBRIC_ROLE_MAP.get(role, role)
    entry = QUALITY_RUBRIC.get(rubric_key)
    if not entry:
        return task
    my_dims = _member_dims(role, slot)
    lines = [
        f"TASK: {task}",
        "",
        f"You are Member {slot} on this review team. "
        "You are responsible for these dimensions ONLY:",
        *[
            f"{i + 1}. {d.upper()}: {_dim_description(role, d)}"
            for i, d in enumerate(my_dims)
        ],
        "",
        "Investigate the work focusing on your assigned dimensions.",
        "Use Read, Grep, and Glob tools to inspect the code/artifacts directly.",
        "Do NOT dispatch scouts \u2014 investigate yourself.",
        "DO NOT emit <member_verdict> in this turn. Emitting it early VOIDS "
        "your score \u2014 all dimensions will be marked fail.",
        f'The terminal synthesis turn will use: '
        f'<!-- member_synthesis_final --> '
        f'<member_verdict slot="{slot}">...</member_verdict>',
        "A missing or malformed block scores ALL your dimensions as fail.",
    ]
    return "\n".join(lines)


def build_member_synthesis(role: str, slot: int) -> str:
    """Build the member's terminal synthesis turn prompt.

    Port of BSC team-lead.ts buildMemberSynthesis. Instructs the member to
    resolve commit + critique into the terminal <member_verdict> transport block.
    """
    dims = _member_dims(role, slot)
    return "\n".join([
        f"SYNTHESIS TURN \u2014 Member {slot}",
        "",
        "Resolve your commit and critique into final scores. Use concrete evidence.",
        "Emit no prose after this block.",
        "<!-- member_synthesis_final -->",
        f'<member_verdict slot="{slot}">',
        *[f"{d}: pass|partial|fail \u2014 one line of concrete evidence" for d in dims],
        "</member_verdict>",
        "",
        "Scores outside your assigned dimensions are ignored. "
        "Missing or malformed output fails every assigned dimension.",
    ])


def is_quality_gated_role(sub_role: str) -> bool:
    """Return True if this sub_role has a quality rubric (i.e. is quality-gated)."""
    return sub_role in _RUBRIC_ROLE_MAP


def get_rubric_prompt(sub_role: str) -> str:
    """Get the rubric prompt text for a quality-gated sub_role. Empty if none."""
    rubric_key = _RUBRIC_ROLE_MAP.get(sub_role, "")
    return QUALITY_RUBRIC.get(rubric_key, {}).get("prompt", "")
