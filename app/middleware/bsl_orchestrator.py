"""
Middleware.bsl_orchestrator — Deterministic multi-phase orchestration core.

Port of Blacksand Code's orchestrator (packages/blacksandcode/src/agent/
orchestrator.ts + quality-gate.ts) into BSL Router.

THREE-LAYER ARCHITECTURE (from the reference):
  Layer 1 (deterministic): phase templates for known patterns — ZERO LLM cost.
  Layer 2 (LLM):           decomposition for novel tasks (not yet enabled here).
  Layer 3 (feedback):      deterministic gate between phases; LLM only when the
                           gate genuinely cannot decide.

The load-bearing property is that ``feedback_gate`` returns ``None`` ONLY when
resolution is genuinely ambiguous. Every happy path resolves with zero extra
model calls. If this module starts returning None often, the orchestrator's cost
model is broken — that is the signal to look at, not raw latency.

PURE MODULE — no I/O, no config access, no HTTP. Everything here is a pure
function or a dataclass mutation. The engine (bsl_orchestrator_engine) owns all
I/O. This split is deliberate: the gate logic is the part that must be provable
by unit test without a server.

DELIBERATE DIVERGENCES from the TypeScript reference (documented, not accidental):
  1. No ``human_gate`` phase. BSL Router answers a single HTTP request and cannot
     pause for user input mid-response. Reference phase templates include one;
     ours omit it. Attempting to port it would produce a phase that can only
     ever time out.
  2. No tool-execution phases. The router does not execute tools, so the
     reference's ``tools: "search" | "write"`` scopes collapse to a single
     reasoning scope. ``scout`` therefore reasons over supplied context instead
     of actually searching a codebase — a real fidelity reduction, named here so
     nobody later mistakes it for a working code search.
  3. Role names are BSL snake_case (``planner_architect``), not the reference's
     dotted PascalCase (``Planner.architect``), to match the existing
     ``agent_routes`` config keys and the benchmark sheet.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Literal, Optional

# ─── Score / verdict vocabulary ──────────────────────────────────────────────

DimensionScore = Literal["pass", "partial", "fail"]
VerdictStatus = Literal["success", "partial", "blocked"]
Confidence = Literal["high", "cautious"]
PhaseStatus = Literal["success", "partial", "blocked", "error"]
DecisionAction = Literal["continue", "revise", "skip", "insert", "done"]

# fail < partial < pass. Used by ``worst`` for the worst-score-wins merge.
_RANK: Dict[str, int] = {"fail": 0, "partial": 1, "pass": 2}

# Total reroute budget across the whole orchestration, independent of the
# per-role round budgets. Mirrors MAX_REROUTES in the reference.
MAX_REROUTES = 3

# Default per-team round budget. A team may re-fire this many times before the
# orchestration stops and surfaces the partial result.
DEFAULT_MAX_ROUNDS_PER_TEAM = 2


# ─── Core dataclasses ────────────────────────────────────────────────────────


@dataclass
class QualityVerdict:
    """A mechanically-derived quality judgement.

    ``status`` and ``confidence`` are NEVER supplied by a model — they are
    computed by ``derive_verdict`` from the dimension scores. A model reports
    per-dimension pass/partial/fail and nothing else. This is the whole point:
    a model cannot talk its way to a passing verdict, because it does not get to
    write the verdict.
    """

    dimensions: Dict[str, str] = field(default_factory=dict)
    status: VerdictStatus = "success"
    confidence: Confidence = "high"
    constraints: List[str] = field(default_factory=list)
    blockers: List[str] = field(default_factory=list)
    round: int = 1
    findings_summary: str = ""


@dataclass
class MemberVerdict:
    """One member's scores for its assigned subset of the role's rubric.

    A member that produced no parseable verdict MUST be represented as an
    all-``fail`` MemberVerdict, not as an absent one. Silent loss must not be
    indistinguishable from success — an unparseable member is a failure signal,
    and dropping it would silently upgrade the merged verdict.
    """

    slot: int
    dimensions: Dict[str, str] = field(default_factory=dict)
    evidence: Dict[str, str] = field(default_factory=dict)
    synthetic: Optional[str] = None


@dataclass
class PhaseReport:
    """Structured report from a completed phase."""

    phase: int
    agent: str
    sub_role: str
    status: PhaseStatus
    summary: str = ""
    # Full output, preserved untruncated. ``summary`` is a short preview for
    # logs/UI; any downstream routing decision must read full_output instead of
    # a sliced snippet, or it will route on truncated evidence.
    full_output: str = ""
    gaps: List[str] = field(default_factory=list)
    recommendation: Optional[str] = None
    tokens: Dict[str, int] = field(default_factory=lambda: {"input": 0, "output": 0})
    # Explicit auditor verdict. True = audit passed, False = failed,
    # None = not an auditor phase / no structured verdict found. The tri-state
    # matters: None must not be conflated with False.
    audit_passed: Optional[bool] = None
    quality_verdict: Optional[QualityVerdict] = None
    model: str = ""
    effort_tier: str = ""


@dataclass
class RoutingDecision:
    """The orchestrator's decision after receiving a phase report."""

    action: DecisionAction
    reason: str
    source: Literal["deterministic", "llm"] = "deterministic"
    next: Optional[int] = None
    inserted: List["PhaseConfig"] = field(default_factory=list)
    # Terminal decisions stop the loop and surface the partial result to the
    # user. Distinct from a plain "done": terminal means we ran out of budget or
    # rounds, not that the work completed.
    terminal: bool = False


@dataclass
class PhaseConfig:
    """Execution configuration for one phase."""

    sub_role: str
    description: str
    owner: str = ""
    # Model authority still comes from agent_routes / the benchmark sheet. This
    # tier is advisory metadata for prompt shaping and budget selection.
    model_tier: Literal["fast", "medium", "reasoning"] = "medium"
    thinking: Optional[Literal["always", "never", "scout-fast-retry"]] = None
    max_tokens: int = 8000
    timeout: float = 60.0
    blocking: bool = True
    # Force LLM reassessment after this phase even on clean success. Set for
    # decision-checkpoint roles (architect, auditor) whose successful output is a
    # routing inflection point rather than a signal to blindly continue.
    reassess_after: bool = False


@dataclass
class OrchestrationState:
    """Full orchestration state for one request's lifetime.

    Lives in a local variable inside a single request. NOT persisted: BSL Router
    is a stateless proxy, and persisting this would turn it into a stateful
    service (see the implementation plan's rejected Option 3).
    """

    id: str
    query: str
    source: Literal["template", "llm", "single"]
    phases: List[PhaseConfig] = field(default_factory=list)
    current: int = 0
    reports: List[PhaseReport] = field(default_factory=list)
    reroutes: int = 0
    # Per-role round counters. Deliberately NOT one shared counter: the reference
    # documents that multiple writers on a single budget was a bug, because one
    # team's retries silently consumed another team's allowance.
    challenge_rounds: int = 0
    review_rounds: int = 0
    audit_rounds: int = 0
    round2_attempts: int = 0
    max_rounds_per_team: int = DEFAULT_MAX_ROUNDS_PER_TEAM
    # Constraints carried forward from a CONDITIONAL (partial) verdict. Injected
    # into the next phase's prompt so a "proceed, but respect X" verdict actually
    # binds the next phase instead of being advisory text nobody reads.
    pending_constraints: List[str] = field(default_factory=list)
    cap_reached: bool = False
    done: bool = False
    started: float = 0.0
    template_id: str = ""
    effort_tier: str = "balanced"


# ─── Mechanical verdict derivation ───────────────────────────────────────────


def worst(a: str, b: str) -> str:
    """Return the lower of two scores. fail < partial < pass."""
    return a if _RANK.get(a, 0) <= _RANK.get(b, 0) else b


def merge_members(lead: Dict[str, str], members: List[MemberVerdict]) -> Dict[str, str]:
    """Worst-score-wins merge of member scores into the lead's.

    A member can only LOWER a dimension, never raise it. Two consequences, both
    intentional:

    - The lead cannot talk its way past a member's ``fail``.
    - A member scoring a dimension the lead OMITTED still lands. Otherwise a
      lead could launder a member's fail simply by not reporting that dimension.
    """
    out = dict(lead)
    for member in members:
        for dim, score in member.dimensions.items():
            out[dim] = worst(out[dim], score) if dim in out else score
    return out


def derive_verdict(dimensions: Dict[str, str]) -> Dict[str, str]:
    """Derive status and confidence from dimension scores. Pure.

    Any fail -> blocked. Any partial -> partial. All pass -> success.
    """
    scores = list(dimensions.values())
    if any(s == "fail" for s in scores):
        return {"status": "blocked", "confidence": "cautious"}
    if any(s == "partial" for s in scores):
        return {"status": "partial", "confidence": "cautious"}
    return {"status": "success", "confidence": "high"}


def build_verdict(
    lead_dimensions: Dict[str, str],
    members: Optional[List[MemberVerdict]] = None,
    *,
    constraints: Optional[List[str]] = None,
    blockers: Optional[List[str]] = None,
    round_num: int = 1,
    findings_summary: str = "",
) -> QualityVerdict:
    """Merge member scores, derive the verdict, and assemble it.

    The single entry point callers should use — it guarantees status/confidence
    are computed rather than passed in.
    """
    merged = merge_members(lead_dimensions, members or [])
    derived = derive_verdict(merged)
    return QualityVerdict(
        dimensions=merged,
        status=derived["status"],  # type: ignore[arg-type]
        confidence=derived["confidence"],  # type: ignore[arg-type]
        constraints=list(constraints or []),
        blockers=list(blockers or []),
        round=round_num,
        findings_summary=findings_summary,
    )


# ─── Layer 3: the deterministic feedback gate ────────────────────────────────


def _role_round_key(sub_role: str) -> str:
    """Map a sub-role to its per-role round counter attribute."""
    family = sub_role.split("_")[0] if "_" in sub_role else sub_role
    if family == "planner":
        return "challenge_rounds"
    if family == "auditor":
        return "audit_rounds"
    return "review_rounds"


def _find_phase(state: OrchestrationState, sub_role: str) -> int:
    for idx, phase in enumerate(state.phases):
        if phase.sub_role == sub_role:
            return idx
    return -1


def feedback_gate(
    report: PhaseReport, state: OrchestrationState
) -> Optional[RoutingDecision]:
    """Deterministic routing gate. Returns None when the LLM must be consulted.

    Precedence, highest first:
      1. Quality verdict (a structured verdict resolves routing with no LLM cost)
      2. Checkpoint phases (reassess_after) — with a deterministic audit escape
      3. Happy path (success, no gaps, no conflicting recommendation)
      4. Success-with-gaps / partial / blocked handling

    Returning None is the EXPENSIVE branch — it costs an extra model call. Every
    early return here is a saved call, which is why the ordering matters.
    """
    next_idx = state.current + 1
    has_next = next_idx < len(state.phases)
    phase = state.phases[state.current]

    # ── 1. Quality verdict ──
    # Takes priority over the reassess_after LLM path: when a structured verdict
    # exists, routing is fully determined and an LLM round-trip would be wasted.
    verdict = report.quality_verdict
    if verdict is not None:
        if verdict.status == "success":
            if has_next:
                return RoutingDecision(
                    action="continue", next=next_idx, reason="quality_gate_pass"
                )
            return RoutingDecision(action="done", reason="quality_gate_pass_complete")

        if verdict.status == "partial":
            # CONDITIONAL: proceed, but bind the next phase to the constraints.
            state.pending_constraints = list(verdict.constraints)
            if has_next:
                return RoutingDecision(
                    action="continue", next=next_idx, reason="quality_gate_conditional"
                )
            return RoutingDecision(
                action="done", reason="quality_gate_conditional_complete"
            )

        if verdict.status == "blocked":
            role_key = _role_round_key(phase.sub_role)
            rounds = getattr(state, role_key)
            if rounds >= state.max_rounds_per_team:
                # Budget exhausted — stop and surface. Terminal, not success.
                return RoutingDecision(
                    action="done",
                    reason="quality_gate_fail_rounds_exhausted",
                    terminal=True,
                )
            setattr(state, role_key, rounds + 1)
            # Reroute to whichever revision target exists.
            revise_idx = _find_phase(state, "planner_architect")
            if revise_idx < 0:
                revise_idx = _find_phase(state, "planner_planner")
            if revise_idx >= 0:
                return RoutingDecision(
                    action="revise",
                    next=revise_idx,
                    reason=f"quality_gate_fail_reroute_round_{rounds + 1}",
                )
            # No revision target — genuinely ambiguous, consult the LLM.
            return None

    # ── 2. Checkpoint phases ──
    # Decision roles produce routing inflections, not terminal answers. They
    # normally require LLM reassessment — EXCEPT when a deterministic audit
    # verdict is present, which resolves without cost.
    if phase.reassess_after:
        if report.audit_passed is not None:
            if report.audit_passed:
                # Approved: skip any remaining consecutive auditor phases.
                offset = -1
                for i in range(next_idx, len(state.phases)):
                    if not state.phases[i].sub_role.startswith("auditor"):
                        offset = i
                        break
                if offset >= 0:
                    return RoutingDecision(
                        action="continue",
                        next=offset,
                        reason="audit_passed_deterministic",
                    )
                return RoutingDecision(
                    action="done", reason="audit_passed_pipeline_complete"
                )
            # Rejected: route back to the planner for revision.
            planner_idx = _find_phase(state, "planner_planner")
            if planner_idx >= 0:
                return RoutingDecision(
                    action="revise",
                    next=planner_idx,
                    reason="audit_failed_deterministic_reroute_to_planner",
                )
            # No planner phase — let the LLM decide how to recover.
        return None

    # ── 3. Happy path ──
    if report.status == "success" and not report.gaps:
        if report.recommendation and has_next:
            planned = state.phases[next_idx].sub_role
            if report.recommendation != planned:
                # The agent disagrees with the plan — needs arbitration.
                return None
        if has_next:
            return RoutingDecision(
                action="continue", next=next_idx, reason="phase_success_no_gaps"
            )
        return RoutingDecision(action="done", reason="all_phases_complete")

    # ── 4a. Succeeded but reported gaps ──
    if report.status == "success" and report.gaps:
        # Insert remediation, or continue with gaps? Not mechanically decidable.
        return None

    # ── 4b. Partial ──
    if report.status == "partial":
        if not phase.blocking and has_next:
            return RoutingDecision(
                action="continue",
                next=next_idx,
                reason="partial_nonblocking_continue",
            )
        return None

    # ── 4c. Blocked or errored ──
    if report.status in ("blocked", "error"):
        if state.reroutes >= MAX_REROUTES:
            return RoutingDecision(
                action="done", reason="reroute_budget_exhausted", terminal=True
            )
        return None

    return None


# ─── State transitions ───────────────────────────────────────────────────────


def record_report(report: PhaseReport, state: OrchestrationState) -> None:
    """Append a report to the state's history."""
    state.reports.append(report)


def apply_decision(
    decision: RoutingDecision, state: OrchestrationState
) -> Optional[PhaseConfig]:
    """Apply a routing decision, returning the next phase to execute.

    Returns None when the orchestration is finished.
    """
    if decision.action == "done":
        state.done = True
        return None

    if decision.action == "insert" and decision.inserted:
        at = state.current + 1
        state.phases[at:at] = decision.inserted
        state.current = at
        return state.phases[at]

    if decision.action == "revise":
        state.reroutes += 1
        if decision.next is None or not (0 <= decision.next < len(state.phases)):
            state.done = True
            return None
        state.current = decision.next
        return state.phases[state.current]

    # continue / skip
    if decision.next is None or decision.next >= len(state.phases):
        state.done = True
        return None
    state.current = decision.next
    return state.phases[state.current]
