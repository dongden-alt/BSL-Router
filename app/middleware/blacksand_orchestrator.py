"""
Middleware.blacksand_orchestrator — Balanced-mode orchestration loop.

Python port of Blacksand Code's deterministic phase templates + balanced-tier
expansion + preflight (packages/blacksandcode/src/agent/: phase-templates.ts,
phase-expansion.ts, thinking-modes.ts, member-config.ts, preflight.ts).

Contract: one client request -> N internal upstream calls (phase loop, lead +
1 parallel member on commit-boundary phases, cap 22, 1 round) -> one final
response whose text is the last (synthesis) phase's output, with usage summed
across every internal call.

Sub-role naming: BSL snake_case (``planner_architect``), matching the
``agent_routes`` config keys, NOT the reference's dotted PascalCase.

PURE-ish module: no HTTP. ``run_balanced`` takes an ``execute`` callable that
performs one internal upstream call, so the loop is unit-testable offline
(main.py supplies the real executor that reuses the router's combo/alias
translation + fallback machinery).
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

from app.middleware.coding_category_classifier import (
    CATEGORY_AUDITOR,
    CATEGORY_FAST_CODER,
    CATEGORY_FRONTEND_CODER,
    CATEGORY_GENERAL,
    CATEGORY_PLANNER,
    CATEGORY_POWER_CODER,
    CATEGORY_REFACTOR,
    CATEGORY_SCOUT,
    CATEGORY_ULTRA_CODER,
    CodingCategoryDecision,
    classify_coding_request_category,
)
from app.middleware.bsl_router_utils import resolve_agent_route, _extract_route

# ─── Session phase caps (orchestrator-loop.ts L81-83) ────────────────────────

SESSION_PHASE_CAP_FAST = 18
SESSION_PHASE_CAP_BALANCED = 22
SESSION_PHASE_CAP_DEEP = 30

# Balanced tier members/rounds (member-config.ts): 1 member, 1 round.
BALANCED_MEMBER_COUNT = 1
BALANCED_ROUNDS = 1

# A phase output shorter than this fails the substance gate (one retry, then
# continue). Balanced mode must not fan out N calls for empty answers.
SUBSTANCE_GATE_MIN_CHARS = 200


@dataclass
class PhaseGroup:
    """Metadata for one independently executed reasoning behavior boundary."""

    mode: str
    group_id: str
    idx: int
    total: int
    markers: List[str] = field(default_factory=list)
    brief: str = ""
    boundary: str = ""  # "commit" | "critique" | "synthesize"


@dataclass
class BSPhase:
    """Port of the reference PhaseConfig: one internal upstream call."""

    sub_role: str
    description: str
    model: str = "medium"  # "fast" | "medium" | "reasoning"
    thinking: Optional[str] = None  # "always" | "never" | "scout-fast-retry"
    tools: str = "reasoning"
    max_tokens: int = 8000
    timeout: float = 60.0  # seconds
    reassess_after: bool = False
    phase_group: Optional[PhaseGroup] = None


# ─── Thinking-mode behavior groups (thinking-modes.ts PHASE_GROUPS) ──────────
# Briefs and boundaries are copied verbatim from the TS source. Each group is
# one independently executed LLM turn; "commit" groups spawn the balanced
# parallel member.

PHASE_GROUPS: Dict[str, List[Dict]] = {
    "architect": [
        {"id": "landscape", "markers": ["CONTEXT", "CONSTRAINTS", "CURRENT SHAPE"],
         "brief": "Map the terrain before proposing anything. What exists, what is fixed, what hurts. Commit to the problem shape — the next turn will generate options against this landscape.",
         "boundary": "commit"},
        {"id": "exploration", "markers": ["OPTIONS", "TRADEOFFS"],
         "brief": "Generate 3+ distinct approaches with different tradeoff axes. Score each against AP criteria. No premature convergence — work against the landscape committed above.",
         "boundary": "critique"},
        {"id": "decision", "markers": ["PROPOSED ARCHITECTURE", "RISKS", "SYNTHESIS"],
         "brief": "Commit to one design based on the exploration. Name failure modes and mitigations. Crystallize the first concrete implementation step.",
         "boundary": "synthesize"},
    ],
    "debater": [
        {"id": "claim", "markers": ["CLAIM"],
         "brief": "Identify the load-bearing architectural claims and implicit assumptions. Commit to them — you will not be able to cherry-pick easy targets later.",
         "boundary": "commit"},
        {"id": "attack_cx", "markers": ["ATTACK", "COUNTEREXAMPLE"],
         "brief": "Attack the claims committed above. Find concrete breakpoints (not vague skepticism) and construct realistic failure scenarios. You must work against the specific claims from the prior turn.",
         "boundary": "critique"},
        {"id": "harden", "markers": ["REQUIRED HARDENING"],
         "brief": "Based on the attacks and counterexamples above, specify the exact contract changes, guardrails, tests, or rollbacks required. Rank by architectural impact.",
         "boundary": "synthesize"},
    ],
    "reviewer": [
        {"id": "coverage", "markers": ["COVERAGE"],
         "brief": "Verify the plan addresses all stated requirements. Check dependencies, ordering, and edge cases. Commit to your assessment — the next turn will stress-test it.",
         "boundary": "commit"},
        {"id": "risk", "markers": ["RISK PROBE"],
         "brief": "Stress-test the plan for execution risks: technical feasibility, integration gaps, timeline threats, untested assumptions. Work against the coverage assessment from the prior turn.",
         "boundary": "critique"},
        {"id": "verdict", "markers": ["VERDICT"],
         "brief": "Synthesize coverage gaps and risks into a clear judgment: approve, request specific changes, or reject. Rank issues by impact.",
         "boundary": "synthesize"},
    ],
    "auditor": [
        {"id": "surface", "markers": ["SURFACE SCAN"],
         "brief": "Check correctness, type safety, edge cases, error handling, and API contract compliance. Commit to your initial findings — the next turn will go deeper.",
         "boundary": "commit"},
        {"id": "deep", "markers": ["DEEP PROBE"],
         "brief": "Investigate security vulnerabilities, performance bottlenecks, concurrency issues, and state management risks. Probe beyond what the surface scan found.",
         "boundary": "critique"},
        {"id": "findings", "markers": ["FINDINGS"],
         "brief": "Consolidate all findings with severity rankings (Critical/High/Medium/Low), fix recommendations, and ship-readiness assessment.",
         "boundary": "synthesize"},
    ],
    "logic": [
        {"id": "frame", "markers": ["PROBLEM FRAME"],
         "brief": "Define the exact objective, invariants, inputs, outputs, and constraints. Separate verified facts from assumptions before proposing implementation.",
         "boundary": "commit"},
        {"id": "derive", "markers": ["DERIVATION", "EDGE CASES"],
         "brief": "Derive the smallest correct solution step by step. Challenge it with boundary cases, failure paths, and incompatible assumptions.",
         "boundary": "critique"},
        {"id": "verify", "markers": ["VERIFICATION"],
         "brief": "Commit to the implementation or plan. Specify checks proving correctness, failure handling, and requirement coverage.",
         "boundary": "synthesize"},
    ],
    "researcher": [
        {"id": "scope", "markers": ["RESEARCH SCOPE"],
         "brief": "Define the research question, evidence standard, source boundaries, and unresolved claims before gathering conclusions.",
         "boundary": "commit"},
        {"id": "evidence", "markers": ["EVIDENCE", "CONTRADICTIONS"],
         "brief": "Compare independent evidence, source quality, recency, and contradictions. Distinguish observed facts from inference.",
         "boundary": "critique"},
        {"id": "synthesis", "markers": ["RESEARCH SYNTHESIS"],
         "brief": "Synthesize supported findings, confidence, unresolved gaps, and the minimum additional research needed.",
         "boundary": "synthesize"},
    ],
    "visionary": [
        {"id": "intent", "markers": ["USER INTENT", "DESIGN CONSTRAINTS"],
         "brief": "Establish user intent, accessibility needs, platform constraints, visual hierarchy, and existing design language.",
         "boundary": "commit"},
        {"id": "directions", "markers": ["DESIGN DIRECTIONS", "TRADEOFFS"],
         "brief": "Generate distinct interface directions. Critique usability, responsiveness, accessibility, consistency, and implementation cost.",
         "boundary": "critique"},
        {"id": "specification", "markers": ["DESIGN SPECIFICATION"],
         "brief": "Commit to one direction with concrete layout, states, interactions, responsive behavior, accessibility, and verification criteria.",
         "boundary": "synthesize"},
    ],
    "oracle": [
        {"id": "anchor", "markers": ["REALITY ANCHOR"],
         "brief": "Ground in facts before projecting. Cite real cases, cross-domain patterns, and source biases. Commit to what's known — the next turn forces novelty against this anchor.",
         "boundary": "commit"},
        {"id": "disrupt", "markers": ["CHAOS ENGINE"],
         "brief": "Discard the first 3 cliché ideas. Force lateral combinations with unfamiliar fields. Stress-test: genuinely novel or just contrarian? Work against the anchor.",
         "boundary": "critique"},
        {"id": "verify", "markers": ["CONSTRAINT VERIFICATION"],
         "brief": "Reality-check the surviving ideas. Guerrilla tactics, hidden costs, adoption barriers, execution feasibility. Only ideas surviving ALL lenses proceed.",
         "boundary": "synthesize"},
    ],
}

# modeForSubRole (thinking-modes.ts) in snake_case. Scout.* intentionally has
# NO mode: intake roles must not receive a structured thinking scaffold.
_SUB_ROLE_MODES: Dict[str, str] = {
    "planner_architect": "architect",
    "planner_challenger": "debater",
    "planner_planner": "logic",
    "planner_brainstormer": "oracle",
    "scout_external": "researcher",
    "fast_coder": "logic",
    "power_coder": "logic",
    "ultra_coder": "logic",
    "frontend_coder": "visionary",
    "refactor": "logic",
    "auditor_auditor": "auditor",
    "auditor_reviewer": "reviewer",
}


def mode_for_sub_role(sub_role: str) -> Optional[str]:
    return _SUB_ROLE_MODES.get(sub_role)


# K2 fast-tier per-role briefs (phase-expansion.ts FAST_BRIEF).
FAST_BRIEF: Dict[str, str] = {
    "planner_architect": "Assess scope, key constraints, and propose a high-level approach. Be concise.",
    "planner_challenger": "Identify the top 2 risks and the biggest gap. Be direct and brief.",
    "planner_planner": "Outline a concrete step-by-step plan with clear deliverables.",
    "planner_brainstormer": "Surface divergent perspectives and key unknowns. Be concise.",
    "auditor_reviewer": "Review for actionability and testability. Flag blocking issues only.",
    "auditor_auditor": "Audit code quality, security, and edge cases. Report critical issues only.",
}


# ─── Sub-role system directives (condensed from sub-role-prompts.ts) ─────────
# Each phase's system message = role boundary directive + template description
# + group brief. Condensed to the load-bearing boundary lines because the
# router has no tool-execution surface (see bsl_orchestrator divergences #2);
# the full artifact/write/report tooling in the TS prompts is dead weight here.

SUB_ROLE_DIRECTIVES: Dict[str, str] = {
    "scout_internal": (
        "You are Scout.internal — a read-only local-codebase researcher. You ONLY gather facts and "
        "report findings. You NEVER propose plans, designs, solutions, or recommendations. Report what "
        "you found with file paths and line numbers. STOP after the findings — your turn is done."
    ),
    "scout_external": (
        "You are Scout.external — an external/web research specialist. Gather current, accurate "
        "information from documentation, comparisons, pricing, API references, and release notes. "
        "Cite sources for every claim and flag freshness. Do NOT write or modify code. If reliable "
        "information is unavailable, say so directly instead of extrapolating."
    ),
    "plan_exit": (
        "You are the human-gate checkpoint. In the synchronous proxy there is no interactive pause, "
        "so state plainly: plan approved for packaging, or name the single blocking concern. Be brief."
    ),
    "planner_architect": (
        "You are Planner.architect — a system design and architecture specialist. Design data flow, "
        "component boundaries, dependency direction, and failure modes. You do NOT write implementation "
        "code; architecture stops at interface signatures and module-level shapes. Document at least 3 "
        "alternatives considered with reasons for rejection. Use the role's 8-stage bracket scaffold "
        "([CONTEXT] [CONSTRAINTS] [CURRENT SHAPE] [OPTIONS] [TRADEOFFS] [PROPOSED ARCHITECTURE] [RISKS] "
        "[SYNTHESIS]) as section headers, in order."
    ),
    "planner_challenger": (
        "You are Planner.challenger — an adversarial pressure-tester. Find brittle assumptions, hidden "
        "dependencies, state ownership risks, lifecycle leaks, security holes, and testability gaps in "
        "the architecture draft. Produce a Challenge Summary, Load-Bearing Claims (claim / attack / "
        "counterexample / required hardening), and a Must-Fix Before Planning list. Do NOT write the "
        "final architecture or the plan, and do NOT approve the plan."
    ),
    "planner_planner": (
        "You are Planner.planner — a step-by-step execution planner. Convert goals into ordered, "
        "testable phases: action, files likely affected, verification criteria, dependencies, risks. "
        "Every step needs a clear done-when condition. Deliver the full plan INLINE — no tool calls, "
        "no artifact writes."
    ),
    "planner_brainstormer": (
        "You are Planner.brainstormer — a divergent-thinking ideation partner. Generate 5-10 distinct "
        "options spanning different approaches, each with a 1-2 sentence rationale and brief pros/cons. "
        "Do NOT converge prematurely; expand the solution space."
    ),
    "auditor_reviewer": (
        "You are Auditor.reviewer — the plan-review gate. Review for alignment with the objective, "
        "context coverage, concrete tasks with file paths and verification criteria, correct lane "
        "selection, and no hidden bypass paths. Verdict: APPROVE, REQUEST CHANGES, or NEEDS DISCUSSION."
    ),
    "auditor_auditor": (
        "You are Auditor.auditor — a security and risk auditor. Identify vulnerabilities, data leaks, "
        "auth bypasses, injection vectors, secret exposure, and unsafe permissions. Rank findings "
        "CRITICAL / HIGH / MEDIUM / LOW with the attack vector and a mitigation strategy (not code)."
    ),
    "fast_coder": (
        "You are FastCoder — a lightweight execution lane. Handle typo fixes, single-file edits, "
        "simple logic changes, and diffs of 30 lines or fewer. Show the diff and confirm done with "
        "minimal ceremony. If the task outgrows the lane, say so and recommend PowerCoder."
    ),
    "power_coder": (
        "You are PowerCoder — a balanced execution lane for multi-file features. Implement features "
        "touching 2-10 files, write tests alongside production code, and follow existing codebase "
        "patterns. Deliver the full implementation INLINE. List files touched and tests added."
    ),
    "ultra_coder": (
        "You are UltraCoder — a deep-reasoning execution lane for architectural changes. Prioritize "
        "correctness over speed. Plan what will change and why, then execute, then provide an explicit "
        "before/after behavior comparison. Deliver the full implementation INLINE."
    ),
    "refactor": (
        "You are Refactor — a structural refactoring lane. Extract helpers, deduplicate, rename, "
        "simplify — WITHOUT changing observable behavior. State the invariants preserved and recommend "
        "running existing tests."
    ),
    "frontend_coder": (
        "You are FrontendCoder — a frontend design-to-code specialist. Produce complete, accessible, "
        "responsive implementations: semantic HTML, WCAG AA, CSS custom properties, responsive "
        "breakpoints, hover/focus/active states. Never stubs or placeholders."
    ),
}


def _scout_phase(description: str, timeout: float = 15.0) -> BSPhase:
    return BSPhase(
        sub_role="scout_internal", description=description, model="fast",
        thinking="scout-fast-retry", tools="search", max_tokens=2000, timeout=timeout,
    )


# ─── Phase templates (phase-templates.ts, ordered by specificity) ────────────
# arch-plan carries the full 7-phase pipeline; the rest are 2-phase seeds.
# The reference's human_gate phase (plan_exit) is retained as an inline
# brief checkpoint — the proxy cannot pause for interactive approval.

PHASE_TEMPLATES: Dict[str, List[BSPhase]] = {
    "arch-plan": [
        _scout_phase("Searching codebase for current system state"),
        BSPhase(sub_role="planner_architect",
                description="Analyzing architecture — orchestrator may add Scout.external if more research needed",
                model="reasoning", thinking="always", tools="reasoning",
                max_tokens=16000, timeout=60.0, reassess_after=True),
        BSPhase(sub_role="planner_challenger",
                description="Challenging architecture assumptions before plan production",
                model="reasoning", thinking="always", tools="reasoning",
                max_tokens=12000, timeout=60.0, reassess_after=True),
        BSPhase(sub_role="planner_planner",
                description="Producing the execution plan based on architecture analysis",
                model="medium", thinking="always", tools="read-only",
                max_tokens=12000, timeout=60.0),
        BSPhase(sub_role="auditor_reviewer",
                description="Reviewing the plan — approve or send back to Planner.planner for adjustment",
                model="medium", thinking="always", tools="reasoning",
                max_tokens=10000, timeout=45.0, reassess_after=True),
        BSPhase(sub_role="plan_exit",
                description="Waiting for user approval before task packaging and execution",
                model="medium", thinking="always", tools="human-gate",
                max_tokens=1000, timeout=60.0),
        BSPhase(sub_role="planner_planner",
                description="Creating task files under .brain/cc_tasks for orchestrator dispatch",
                model="medium", thinking="always", tools="write",
                max_tokens=8000, timeout=60.0),
    ],
    "explain": [
        _scout_phase("Searching the codebase to answer the question"),
        BSPhase(sub_role="planner_planner",
                description="Explaining how it works based on the code — no changes made",
                model="medium", thinking="always", tools="read-only",
                max_tokens=6000, timeout=45.0),
    ],
    "bug-fix": [
        _scout_phase("Locating buggy code and error context"),
        # coder lane substituted at match time from the classified category.
        BSPhase(sub_role="fast_coder",
                description="Applying fix — orchestrator decides if verification needed",
                model="fast", tools="full", max_tokens=4000, timeout=30.0,
                reassess_after=True),
    ],
    "feature-build": [
        _scout_phase("Searching for existing patterns and related code"),
        BSPhase(sub_role="planner_architect",
                description="Designing feature — orchestrator decides coder lane",
                model="reasoning", thinking="always", tools="reasoning",
                max_tokens=8000, timeout=60.0, reassess_after=True),
    ],
    "audit": [
        _scout_phase("Gathering code to review"),
        BSPhase(sub_role="auditor_auditor",
                description="Auditing code — deep review for correctness, security, regressions, and verification gaps",
                model="reasoning", thinking="always", tools="reasoning",
                max_tokens=16000, timeout=45.0, reassess_after=True),
    ],
    "refactor": [
        _scout_phase("Understanding current code structure"),
        BSPhase(sub_role="planner_architect",
                description="Designing refactoring approach — orchestrator decides execution",
                model="reasoning", thinking="always", tools="reasoning",
                max_tokens=8000, timeout=60.0, reassess_after=True),
    ],
    "research": [
        BSPhase(sub_role="scout_external",
                description="Searching for external information",
                model="fast", thinking="always", tools="search",
                max_tokens=3000, timeout=20.0),
        BSPhase(sub_role="planner_brainstormer",
                description="Synthesizing findings into insights",
                model="medium", thinking="always", tools="read-only",
                max_tokens=4000, timeout=30.0),
    ],
}

# Intent regexes (phase-templates.ts) used for category -> template matching.
_EXPLAIN_INTENT = re.compile(
    r"\b(explain|walk me through|tell me about|understand|how (?:does|do|is|are|to)|"
    r"what (?:is|are|does|happens)|why (?:does|do|is|are))\b", re.IGNORECASE)
_ACTIONABLE_INTENT = re.compile(
    r"\b(plan|roadmap|improve|build|implement|design|architect|optimi[sz]e|refactor|fix|add|"
    r"create|redesign|rearchitect|migrat|upgrade|integrate|delete|remove|rename|restructure|"
    r"overhaul)\b", re.IGNORECASE)
_FEATURE_BUILD_INTENT = re.compile(
    r"\b(build|create|implement|add)\b.*\b(feature|component|page|endpoint|module)\b",
    re.IGNORECASE)
_BUG_FIX_INTENT = re.compile(r"\b(fix|bug|broken|error|crash|debug)\b", re.IGNORECASE)


# ─── Template matching (deterministic, zero-LLM) ─────────────────────────────

_CODER_LANES = (CATEGORY_FAST_CODER, CATEGORY_POWER_CODER, CATEGORY_ULTRA_CODER)


def match_template(category: str, query: str) -> List[BSPhase]:
    """Pick the phase template for a classified coding category.

    Port of TEMPLATES ordering (cache-architecture-plan collapses into
    arch-plan; pure-arch is the defensive arch-plan fallback). Returns a fresh
    list so callers (expansion) never mutate PHASE_TEMPLATES.
    """
    query = query or ""
    if category == CATEGORY_AUDITOR:
        return _copy_phases("audit")
    if category == CATEGORY_REFACTOR:
        return _copy_phases("refactor")
    if category in _CODER_LANES:
        if _FEATURE_BUILD_INTENT.search(query):
            return _copy_phases("feature-build")
        if _BUG_FIX_INTENT.search(query):
            phases = _copy_phases("bug-fix")
            # Substitute the coder lane from the classified category; the TS
            # build() uses ctx.recommendedCoderLane ?? "FastCoder".
            lane = category if category != CATEGORY_GENERAL else CATEGORY_FAST_CODER
            model = "medium" if lane in (CATEGORY_POWER_CODER, CATEGORY_ULTRA_CODER) else "fast"
            phases[1].sub_role = lane
            phases[1].model = model
            return phases
        return _copy_phases("feature-build")
    if category == CATEGORY_FRONTEND_CODER:
        return _copy_phases("feature-build")
    if category in (CATEGORY_SCOUT, CATEGORY_GENERAL):
        # Scout dispatch: external research when the query reads as research;
        # otherwise the read-only explain pipeline.
        if _EXPLAIN_INTENT.search(query) and not _ACTIONABLE_INTENT.search(query):
            return _copy_phases("explain")
        return _copy_phases("research")
    # Planner (and anything else non-coding): explain for pure questions,
    # arch-plan otherwise. Explain must NOT reach arch-plan because arch-plan
    # ends in a write task-packaging phase — a question terminates read-only.
    if _EXPLAIN_INTENT.search(query) and not _ACTIONABLE_INTENT.search(query):
        return _copy_phases("explain")
    return _copy_phases("arch-plan")


def _copy_phases(template_id: str) -> List[BSPhase]:
    """Deep-copy a template's phases (phase_group is rebuilt, not shared)."""
    copies: List[BSPhase] = []
    for phase in PHASE_TEMPLATES[template_id]:
        group = None
        if phase.phase_group is not None:
            group = PhaseGroup(**vars(phase.phase_group))
        kwargs = dict(vars(phase))
        kwargs["phase_group"] = group
        copies.append(BSPhase(**kwargs))
    return copies


# ─── Expansion (phase-expansion.ts) ──────────────────────────────────────────

_GROUP_BRIEF_MAX = 120


def _abbreviated_brief(brief: str) -> str:
    if len(brief) <= _GROUP_BRIEF_MAX:
        return brief
    return brief[: _GROUP_BRIEF_MAX - 3] + "..."


def expand_reasoning_phases(
    phases: List[BSPhase], tier: str = "balanced"
) -> List[BSPhase]:
    """Split grouped reasoning roles into independently executed phase turns.

    balanced: full group splitting — every role with a thinking mode becomes
    len(PHASE_GROUPS[mode]) turns, each carrying its group brief.
    fast: no split; the role's FAST_BRIEF is injected into the description.
    Scout.* roles gather evidence in one native turn and never expand.
    """
    expanded: List[BSPhase] = []
    for phase in phases:
        if phase.phase_group is not None:
            expanded.append(phase)
            continue
        mode = mode_for_sub_role(phase.sub_role)
        if not mode or mode not in PHASE_GROUPS:
            expanded.append(phase)
            continue
        if phase.sub_role.startswith("scout_"):
            expanded.append(phase)
            continue
        if tier == "fast":
            brief = FAST_BRIEF.get(phase.sub_role)
            if brief:
                fast = dict(vars(phase))
                fast["description"] = f"{phase.description}\n\n[FAST] {brief}"
                expanded.append(BSPhase(**fast))
            else:
                expanded.append(phase)
            continue
        groups = PHASE_GROUPS[mode]
        for idx, group in enumerate(groups):
            kwargs = dict(vars(phase))
            kwargs["description"] = (
                f"{phase.description} — {_abbreviated_brief(group['brief'])}"
            )
            # Intermediate group phases never reassess early (TS Bug #2 fix).
            kwargs["reassess_after"] = (
                phase.reassess_after if idx == len(groups) - 1 else False
            )
            kwargs["phase_group"] = PhaseGroup(
                mode=mode,
                group_id=group["id"],
                idx=idx,
                total=len(groups),
                markers=list(group["markers"]),
                brief=group["brief"],
                boundary=group["boundary"],
            )
            expanded.append(BSPhase(**kwargs))
    return expanded


# ─── Preflight (preflight.ts) ────────────────────────────────────────────────

@dataclass
class MissingSlot:
    role: str  # sub_role or member slot key, e.g. "planner_challenger:1"
    slot: str = "primary"


@dataclass
class DegradedSlot:
    role: str
    slot: str  # "fallback_1" | "fallback_2"


@dataclass
class PreflightResult:
    status: str = "ready"  # "ready" | "degraded" | "blocked"
    missing: List[MissingSlot] = field(default_factory=list)
    degraded: List[DegradedSlot] = field(default_factory=list)


def _valid_route(entry) -> bool:
    """A slot is usable when it resolves to a non-empty primary model."""
    if not entry:
        return False
    primary, _ = _extract_route(entry)
    return bool(primary)


def preflight_slots(
    phases: List[BSPhase],
    agent_routes: Dict,
    member_routes: Dict,
    tier: str = "balanced",
) -> PreflightResult:
    """Validate every model slot the plan will touch.

    Blocking: a role's lead Primary (or the balanced member slot ':1' on a
    commit-boundary phase) is absent. Informational: a fallback slot is absent.
    """
    result = PreflightResult()
    role_keys: List[str] = []
    for phase in phases:
        key = _role_route_key(phase.sub_role)
        if key and key not in role_keys:
            role_keys.append(key)

    for key in role_keys:
        route = resolve_agent_route(agent_routes or {}, key)
        if not _valid_route(route):
            result.missing.append(MissingSlot(role=key, slot="primary"))
        elif isinstance(route, dict):
            for fallback in ("fallback_1", "fallback_2"):
                if not (isinstance(route.get(fallback), str) and route[fallback]):
                    result.degraded.append(DegradedSlot(role=key, slot=fallback))

    if tier in ("balanced", "deep"):
        for phase in phases:
            if not phase.phase_group or phase.phase_group.boundary != "commit":
                continue
            key = _role_route_key(phase.sub_role)
            for i in range(1, 2 if tier == "balanced" else 3):
                slot_key = f"{key}:{i}"
                if not _valid_route((member_routes or {}).get(key)):
                    result.missing.append(MissingSlot(role=slot_key, slot="primary"))

    if result.missing:
        result.status = "blocked"
    elif result.degraded:
        result.status = "degraded"
    else:
        result.status = "ready"
    return result


def _role_route_key(sub_role: str) -> str:
    """Map a sub-role to its agent_routes key (parent fallback handled by
    resolve_agent_route). plan_exit routes as the planner lane."""
    if sub_role == "plan_exit":
        return "planner"
    if sub_role == "scout_internal":
        return "scout"
    return sub_role


# ─── Orchestration result ────────────────────────────────────────────────────

@dataclass
class PhaseTraceEntry:
    phase_idx: int
    sub_role: str
    model: str
    boundary: str
    tokens_in: int = 0
    tokens_out: int = 0
    ms: float = 0.0
    member: bool = False


@dataclass
class OrchestratorResult:
    final_text: str = ""
    trace: List[PhaseTraceEntry] = field(default_factory=list)
    phases_total: int = 0
    capped: bool = False
    degraded: List[str] = field(default_factory=list)
    template_id: str = ""
    category: str = ""
    tokens_in: int = 0
    tokens_out: int = 0


# execute(role_model, messages, max_tokens, timeout) -> {text, tokens_in, tokens_out}
ExecuteFn = Callable[..., Awaitable[Dict]]


def _resolve_model_for_role(sub_role: str, agent_routes: Dict, member_routes: Dict) -> str:
    """Resolve the primary model for a sub-role; members prefer member_routes."""
    route_key = _role_route_key(sub_role)
    route = resolve_agent_route(agent_routes or {}, route_key)
    if route:
        primary, _ = _extract_route(route)
        if primary:
            return primary
    member = (member_routes or {}).get(route_key)
    if member:
        primary, _ = _extract_route(member)
        if primary:
            return primary
    return ""


def _phase_system_message(phase: BSPhase) -> str:
    directive = SUB_ROLE_DIRECTIVES.get(phase.sub_role, "")
    parts = [f"[Blacksand phase — {phase.sub_role}] {phase.description}"]
    if directive:
        parts.append(directive)
    if phase.phase_group is not None:
        pg = phase.phase_group
        parts.append(
            f"Reasoning boundary: {pg.boundary.upper()} (group {pg.idx + 1}/{pg.total} "
            f"'{pg.group_id}'). {pg.brief}"
        )
    return "\n\n".join(parts)


async def run_balanced(
    request,
    config: dict,
    execute: ExecuteFn,
    category_decision: Optional[CodingCategoryDecision] = None,
) -> OrchestratorResult:
    """Run the full balanced-mode orchestration loop for one request.

    1. classify (free Scout)  2. match template (deterministic)  3. expand
    4. preflight (blocked -> degrade honestly: lead-only, reason logged)
    5. loop cap=22: lead call + parallel member on commit boundaries,
       substance gate (>=200 chars, one retry)  6. final phase IS the response
    7. usage summed across all internal calls.
    """
    if category_decision is None:
        category_decision = classify_coding_request_category(request)
    # Extract the current user text for template intent regexes.
    try:
        from app.middleware.coding_category_classifier import _extract_request_text

        query = _extract_request_text(request)
    except Exception:
        query = ""

    from app.middleware.bsl_agentic_ultra_router import _get_bsl_agentic_ultra_cfg

    cfg = _get_bsl_agentic_ultra_cfg(config)
    agent_routes = cfg.get("agent_routes") or {}
    member_routes = cfg.get("member_routes") or {}
    orchestration_cfg = cfg.get("orchestration") or {}
    phase_cap = int(orchestration_cfg.get("phase_cap", SESSION_PHASE_CAP_BALANCED))

    phases = match_template(category_decision.category, query)
    template_id = _template_id_for(category_decision.category, query)
    phases = expand_reasoning_phases(phases, tier="balanced")

    result = OrchestratorResult(
        template_id=template_id, category=category_decision.category
    )

    pre = preflight_slots(phases, agent_routes, member_routes, tier="balanced")
    skip_member = False
    if pre.status == "blocked":
        lead_missing = [m.role for m in pre.missing if ":" not in m.role]
        member_missing = [m.role for m in pre.missing if ":" in m.role]
        if lead_missing:
            # A lead slot missing is fatal for that role; run degraded with
            # whatever routes resolve (execute receives "" and must fall back
            # to the global chain — the executor owns that fail-open).
            for role in lead_missing:
                result.degraded.append(f"missing_lead_primary:{role}")
        if member_missing:
            skip_member = True
            for slot in member_missing:
                result.degraded.append(f"missing_member_primary:{slot}")
    elif pre.status == "degraded":
        for d in pre.degraded:
            result.degraded.append(f"missing_fallback:{d.role}/{d.slot}")

    # ── Phase loop (cap from config, default 22) ──────────────────────────
    context_outputs: List[str] = []
    final_text = ""
    n = len(phases)
    executed = 0
    for idx, phase in enumerate(phases):
        if executed >= phase_cap:
            result.capped = True
            print(
                f"[BSLAgenticUltra] phase cap {phase_cap} reached — "
                f"truncating after {executed} phases",
                flush=True,
            )
            break
        messages = [
            {"role": "system", "content": _phase_system_message(phase)},
            {"role": "user", "content": (request and _current_user_text(request)) or ""},
        ]
        if context_outputs:
            messages.append({
                "role": "user",
                "content": "Prior phase outputs (in order):\n\n"
                + "\n\n---\n\n".join(context_outputs),
            })

        lead_model = _resolve_model_for_role(phase.sub_role, agent_routes, member_routes)

        async def _call(role_model: str, is_member: bool):
            t0 = time.monotonic()
            out = await execute(
                role_model or "general",
                messages,
                phase.max_tokens,
                phase.timeout,
            )
            boundary = phase.phase_group.boundary if phase.phase_group else ""
            result.trace.append(PhaseTraceEntry(
                phase_idx=idx, sub_role=phase.sub_role, model=role_model,
                boundary=boundary, tokens_in=int(out.get("tokens_in", 0)),
                tokens_out=int(out.get("tokens_out", 0)),
                ms=(time.monotonic() - t0) * 1000.0, member=is_member,
            ))
            return out

        # Balanced parallel member: launched on commit-boundary phases and
        # gathered WITH the lead so both run concurrently (member-config.ts:
        # balanced = lead + 1 member, 1 round).
        boundary = phase.phase_group.boundary if phase.phase_group else ""
        member_model = ""
        if boundary == "commit" and not skip_member:
            member_key = _role_route_key(phase.sub_role)
            member_route = (member_routes or {}).get(member_key)
            member_model, _ = _extract_route(member_route or {})
            if not member_model:
                if f"member_route_missing:{member_key}" not in result.degraded:
                    result.degraded.append(f"member_route_missing:{member_key}")

        lead_task = asyncio.ensure_future(_call(lead_model, is_member=False))
        member_task = (
            asyncio.ensure_future(_call(member_model, is_member=True))
            if member_model
            else None
        )
        lead_out = await lead_task
        lead_text = str(lead_out.get("text", "") or "")
        member_text = ""
        if member_task is not None:
            member_out = await member_task
            member_text = str(member_out.get("text", "") or "")

        # Substance gate: one retry on a thin output, then continue.
        if len(lead_text.strip()) < SUBSTANCE_GATE_MIN_CHARS:
            print(
                f"[BSLAgenticUltra] phase {idx} ({phase.sub_role}) failed substance "
                f"gate ({len(lead_text.strip())} chars) — retrying once",
                flush=True,
            )
            retry_out = await _call(lead_model, is_member=False)
            retry_text = str(retry_out.get("text", "") or "")
            if len(retry_text.strip()) >= len(lead_text.strip()):
                lead_text, lead_out = retry_text, retry_out

        if member_text:
            # Merge: member output appended to context (visible to next phases).
            context_outputs.append(
                f"[{phase.sub_role} member {member_model} — {boundary}]\n{member_text}"
            )

        context_outputs.append(
            f"[{phase.sub_role} — {boundary or 'lead'}]\n{lead_text}"
        )
        final_text = lead_text
        executed += 1

    result.final_text = final_text
    result.phases_total = executed
    result.tokens_in = sum(t.tokens_in for t in result.trace)
    result.tokens_out = sum(t.tokens_out for t in result.trace)
    return result


def _current_user_text(request) -> str:
    """Best-effort current user turn text for the loop's user message."""
    try:
        from app.middleware.coding_category_classifier import _extract_request_text

        return _extract_request_text(request)
    except Exception:
        messages = getattr(request, "messages", None) or []
        for msg in reversed(messages):
            if getattr(msg, "role", "") == "user":
                content = getattr(msg, "content", "")
                if isinstance(content, str):
                    return content
        return ""


def _template_id_for(category: str, query: str) -> str:
    """Mirror match_template's branch structure for the template id label."""
    query = query or ""
    if category == CATEGORY_AUDITOR:
        return "audit"
    if category == CATEGORY_REFACTOR:
        return "refactor"
    if category in _CODER_LANES:
        if _FEATURE_BUILD_INTENT.search(query):
            return "feature-build"
        if _BUG_FIX_INTENT.search(query):
            return "bug-fix"
        return "feature-build"
    if category == CATEGORY_FRONTEND_CODER:
        return "feature-build"
    if category in (CATEGORY_SCOUT, CATEGORY_GENERAL):
        if _EXPLAIN_INTENT.search(query) and not _ACTIONABLE_INTENT.search(query):
            return "explain"
        return "research"
    if _EXPLAIN_INTENT.search(query) and not _ACTIONABLE_INTENT.search(query):
        return "explain"
    return "arch-plan"
