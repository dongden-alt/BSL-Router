"""
Middleware.bsl_orchestrator_gates — The four deterministic gates.

Port of Blacksand Code's admission.ts + scope-gate.ts + thinking-gate.ts +
the rubric half of quality-gate.ts.

FOUR GATES, four distinct concerns — they are not interchangeable:

  ADMISSION  validates a PLAN before it enters the state machine
             (schema, unknown roles, cycles, tier caps)
  SCOPE      governs what an agent may DO
             (does / notDoes / reroute, with a <scope_reject> escape)
  THINKING   governs what an agent may REASON WITH
             (DEEP vs LEAN; bars foreign multi-stage protocols)
  QUALITY    supplies the RUBRIC an agent is scored against
             (verdict derivation itself lives in bsl_orchestrator)

PURE MODULE — no I/O. Every function is a pure string/data transform.

Role names use BSL snake_case to match the existing ``agent_routes`` config keys
(``planner_architect``), not the reference's dotted PascalCase
(``Planner.architect``). Keeping one spelling avoids a translation layer that
would silently drop unmapped roles.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from app.middleware.bsl_orchestrator import PhaseConfig

# ─── Registered roles ────────────────────────────────────────────────────────
# Must stay in lockstep with the granular agent ids in
# bsl_agentic_benchmark_sheet._MATRIX and the UI matrix rows. A role absent here
# is rejected by the admission gate, which is the intended failure mode: an
# unknown role means the plan references an agent we cannot route.
REGISTERED_SUB_ROLES = frozenset(
    {
        "scout",
        "vision",
        "planner",
        "planner_architect",
        "planner_challenger",
        "planner_challenger_member",
        "planner_planner",
        "auditor",
        "auditor_reviewer",
        "auditor_reviewer_member",
        "auditor_auditor",
        "auditor_auditor_member",
        "fast_coder",
        "power_coder",
        "ultra_coder",
        "refactor",
        "frontend_coder",
    }
)

# Non-agent markers that are valid phase sub_roles but route to no model.
GATE_MARKERS = frozenset({"plan_exit"})

# Tier phase caps. Mirrors the reference's fast=3 / balanced=6 / deep=10.
TIER_CAPS: Dict[str, int] = {"fast": 3, "balanced": 6, "deep": 10}


# ─── Admission gate ──────────────────────────────────────────────────────────


class AdmissionResult:
    """Outcome of plan validation. First failure wins."""

    __slots__ = ("ok", "reason", "rejected_phase")

    def __init__(
        self, ok: bool, reason: str = "", rejected_phase: Optional[int] = None
    ) -> None:
        self.ok = ok
        self.reason = reason
        self.rejected_phase = rejected_phase

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"AdmissionResult(ok={self.ok}, reason={self.reason!r}, phase={self.rejected_phase})"


def _check_schema(phases: List[PhaseConfig]) -> AdmissionResult:
    for i, p in enumerate(phases):
        if not p.sub_role:
            return AdmissionResult(False, f"phase {i}: missing sub_role", i)
        if not p.description:
            return AdmissionResult(False, f"phase {i}: missing description", i)
    return AdmissionResult(True)


def _check_roles(phases: List[PhaseConfig]) -> AdmissionResult:
    for i, p in enumerate(phases):
        if p.sub_role not in REGISTERED_SUB_ROLES and p.sub_role not in GATE_MARKERS:
            return AdmissionResult(False, f'phase {i}: unknown role "{p.sub_role}"', i)
    return AdmissionResult(True)


def _check_cycles(phases: List[PhaseConfig]) -> AdmissionResult:
    """Reject a (sub_role, description) pair that repeats after exactly 1 phase.

    A→B→A is a ping-pong loop: the plan oscillates without progressing. Wider
    gaps are legitimate (an architect may reappear after a genuine revision
    round), so only the distance-2 repeat is rejected.
    """
    seen: Dict[str, int] = {}
    for i, p in enumerate(phases):
        key = f"{p.sub_role}::{p.description}"
        prev = seen.get(key)
        if prev is not None and i - prev == 2:
            return AdmissionResult(
                False, f"cycle detected: {p.sub_role} repeats after 1 phase", i
            )
        seen[key] = i
    return AdmissionResult(True)


def _check_tier_cap(phases: List[PhaseConfig], tier: str) -> AdmissionResult:
    cap = TIER_CAPS.get(tier, TIER_CAPS["balanced"])
    if len(phases) > cap:
        return AdmissionResult(
            False, f'tier "{tier}" allows max {cap} phases, got {len(phases)}'
        )
    return AdmissionResult(True)


def _check_fast_gating(phases: List[PhaseConfig]) -> AdmissionResult:
    """Defense in depth only.

    The real fast-tier invariant is that the template layer never BUILDS a
    multi-phase fast plan. This catches hand-constructed phases that already
    carry reassess_after before admission; it is not the primary guard.
    """
    for i, p in enumerate(phases):
        if p.reassess_after:
            return AdmissionResult(
                False, f"phase {i}: reassess_after not allowed on fast tier", i
            )
    return AdmissionResult(True)


def validate_plan(phases: List[PhaseConfig], tier: Optional[str]) -> AdmissionResult:
    """Validate a plan before it enters the orchestration state machine."""
    if not phases:
        return AdmissionResult(False, "empty plan")

    t = (tier or "balanced").lower()

    for check in (_check_schema, _check_roles, _check_cycles):
        result = check(phases)
        if not result.ok:
            return result

    cap = _check_tier_cap(phases, t)
    if not cap.ok:
        return cap

    if t == "fast":
        gate = _check_fast_gating(phases)
        if not gate.ok:
            return gate

    return AdmissionResult(True)


# ─── Scope gate ──────────────────────────────────────────────────────────────
# Per-role action boundaries. ``reroute`` names the role to hand off to when a
# task is out of scope.
#
# NOTE ON FIDELITY: the reference's scopes reference real tool use ("local-
# codebase search"). BSL Router executes no tools, so scout/vision here reason
# over context supplied in the request rather than actually searching. The scope
# text is worded accordingly — promising a search we cannot perform would produce
# confidently fabricated findings.

SUBROLE_SCOPE: Dict[str, Dict[str, str]] = {
    "scout": {
        "does": "summarize the provided context and report findings only",
        "not_does": "planning, design, architectural decisions, or writing code",
        "reroute": "planner_architect",
    },
    "vision": {
        "does": "analyze supplied images and provide detailed textual descriptions",
        "not_does": "planning, design, architectural decisions, or writing code",
        "reroute": "planner_architect",
    },
    "planner": {
        "does": "general planning and task framing",
        "not_does": "writing implementation code",
        "reroute": "planner_architect",
    },
    "planner_architect": {
        "does": "system design, interfaces, shapes, and tradeoff analysis",
        "not_does": "writing implementation code",
        "reroute": "power_coder",
    },
    "planner_challenger": {
        "does": "adversarially challenge architecture assumptions before planning",
        "not_does": (
            "writing final architecture, producing execution plans, or approving work"
        ),
        "reroute": "planner_architect",
    },
    "planner_challenger_member": {
        "does": "challenge one assigned aspect of the architecture",
        "not_does": "producing the final architecture or execution plan",
        "reroute": "planner_challenger",
    },
    "planner_planner": {
        "does": "produce an execution plan from prior architecture",
        "not_does": "designing from scratch",
        "reroute": "planner_architect",
    },
    "auditor": {
        "does": "general review and approval",
        "not_does": "implementing code",
        "reroute": "auditor_reviewer",
    },
    "auditor_reviewer": {
        "does": "review and approve or return a plan",
        "not_does": "implementing code",
        "reroute": "planner_planner",
    },
    "auditor_reviewer_member": {
        "does": "review one assigned dimension of the plan",
        "not_does": "implementing code or issuing the final verdict",
        "reroute": "auditor_reviewer",
    },
    "auditor_auditor": {
        "does": "deep code audit and quality assessment",
        "not_does": "implementing fixes or writing code",
        "reroute": "power_coder",
    },
    "auditor_auditor_member": {
        "does": "audit one assigned dimension of the code",
        "not_does": "implementing fixes or issuing the final verdict",
        "reroute": "auditor_auditor",
    },
    "fast_coder": {
        "does": "implement code for simple, well-scoped tasks",
        "not_does": "design or architecture — if those are needed first, reroute",
        "reroute": "planner_architect",
    },
    "power_coder": {
        "does": "implement code for medium-complexity tasks",
        "not_does": "design or architecture — if those are needed first, reroute",
        "reroute": "planner_architect",
    },
    "ultra_coder": {
        "does": "implement code for complex, heavy-weight tasks",
        "not_does": "design or architecture — if those are needed first, reroute",
        "reroute": "planner_architect",
    },
    "refactor": {
        "does": "refactor and clean up existing code within defined scope",
        "not_does": "new feature design or architecture decisions",
        "reroute": "planner_architect",
    },
    "frontend_coder": {
        "does": "implement frontend/UI code within defined scope",
        "not_does": "design, architecture, or backend logic outside the UI layer",
        "reroute": "planner_architect",
    },
}


# ─── Thinking gate ───────────────────────────────────────────────────────────

# Roles for which deep multi-stage reasoning is core to the persona.
_DEEP_ROLES = frozenset(
    {
        "planner_architect",
        "planner_challenger",
        "planner_challenger_member",
        "planner_planner",
        "planner",
        "auditor_reviewer",
        "auditor_reviewer_member",
        "auditor_auditor",
        "auditor_auditor_member",
        "auditor",
        "ultra_coder",
        "power_coder",
        "frontend_coder",
        "refactor",
    }
)

# Roles that answer leanly but may re-probe once.
_FAST_ROLES = frozenset({"fast_coder", "scout"})


# ─── Quality rubric ──────────────────────────────────────────────────────────
# The dimensions each reviewing role scores. Verdict DERIVATION lives in
# bsl_orchestrator.derive_verdict — a model never writes its own verdict, only
# per-dimension scores.

QUALITY_RUBRIC: Dict[str, Dict[str, object]] = {
    "challenger": {
        "dims": ["completeness", "coherence", "feasibility", "risk_awareness"],
        "prompt": (
            "Your work will be evaluated on 4 dimensions:\n"
            "1. COMPLETENESS: Are all aspects of the task addressed?\n"
            "2. COHERENCE: Is the reasoning internally consistent and logically structured?\n"
            "3. FEASIBILITY: Can the proposed approach be implemented with available resources?\n"
            "4. RISK_AWARENESS: Are edge cases, failure modes, and trade-offs identified?\n"
            "Score each dimension as pass, partial, or fail. The overall verdict is"
            " mechanically derived — do not state an overall verdict yourself."
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
            "Score each dimension as pass, partial, or fail. The overall verdict is"
            " mechanically derived — do not state an overall verdict yourself."
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
            "Score each dimension as pass, partial, or fail. The overall verdict is"
            " mechanically derived — do not state an overall verdict yourself."
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


