"""Minimal Blacksand-compatible orchestration adapter for BSL-Agentic-Ultra.

No model selection here. Ultra router selects the lane; this module only owns
phase state and deterministic gate transitions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from uuid import uuid4

from app.middleware.bsl_orchestrator import (
    OrchestrationState,
    PhaseConfig,
    PhaseReport,
    RoutingDecision,
    apply_decision,
    feedback_gate,
    record_report,
)
from app.middleware.bsl_orchestrator_gates import validate_plan


_CATEGORY_TO_ROLE = {
    "fast_coder": "fast_coder",
    "power_coder": "power_coder",
    "ultra_coder": "ultra_coder",
    "frontend_coder": "frontend_coder",
    "refactor": "refactor",
}


@dataclass(frozen=True)
class BalancedPlan:
    state: OrchestrationState
    admission_reason: str = ""


def build_balanced_plan(query: str, category: str) -> BalancedPlan:
    """Create the one-member balanced plan selected by Scout."""
    role = _CATEGORY_TO_ROLE.get(category, "scout")
    phase = PhaseConfig(
        sub_role=role,
        description=f"Execute the classified request ({category})",
        owner=role,
        model_tier="medium",
        thinking="always",
        max_tokens=8000,
        timeout=60.0,
        blocking=True,
    )
    admission = validate_plan([phase], "balanced")
    if not admission.ok:
        raise ValueError(admission.reason)
    return BalancedPlan(
        OrchestrationState(
            id=f"bsl-ultra-{uuid4().hex}",
            query=query,
            source="single",
            phases=[phase],
            effort_tier="balanced",
        ),
        admission_reason=admission.reason,
    )


def finish_phase(
    state: OrchestrationState,
    *,
    status: str = "success",
    summary: str = "",
    full_output: str = "",
    gaps: Optional[list[str]] = None,
    recommendation: Optional[str] = None,
    model: str = "",
) -> RoutingDecision:
    """Record one phase, then apply the deterministic Blacksand gate.

    ``feedback_gate`` returns ``None`` only for genuine ambiguity. The caller
    must use Scout reassessment there; this adapter never invents a route.
    """
    phase = state.phases[state.current]
    report = PhaseReport(
        phase=state.current,
        agent=phase.owner or phase.sub_role,
        sub_role=phase.sub_role,
        status=status,  # type: ignore[arg-type]
        summary=summary,
        full_output=full_output,
        gaps=list(gaps or []),
        recommendation=recommendation,
        model=model,
        effort_tier=state.effort_tier,
    )
    record_report(report, state)
    decision = feedback_gate(report, state)
    if decision is None:
        raise AmbiguousPhase("Scout reassessment required")
    apply_decision(decision, state)
    return decision


class AmbiguousPhase(RuntimeError):
    """The deterministic gate cannot decide; Scout must reassess."""


def phase_headers(state: OrchestrationState) -> dict[str, str]:
    phase = state.phases[state.current]
    return {
        "X-BSL-Orchestration-Tier": state.effort_tier,
        "X-BSL-Orchestration-Phase": str(state.current),
        "X-BSL-Orchestration-Role": phase.sub_role,
        "X-BSL-Orchestration-Source": state.source,
    }
