"""
Tests for blacksand_orchestrator (balanced-mode orchestration loop).

Covers template matching per coding category, balanced expansion (group
splitting + fast brief), preflight blocked/degraded, run_balanced with a
mocked execute (phase order, cap=22, member only on commit boundaries,
substance-gate retry, synthesis last, token aggregation), member-missing
degrade, and the main.py dispatch wiring smoke.
"""

import asyncio

from app.models import ChatCompletionRequest, Message
from app.middleware.blacksand_orchestrator import (
    FAST_BRIEF,
    PHASE_TEMPLATES,
    SESSION_PHASE_CAP_BALANCED,
    SUBSTANCE_GATE_MIN_CHARS,
    expand_reasoning_phases,
    match_template,
    preflight_slots,
    run_balanced,
)


def _request(text: str, model: str = "blacksand-agentic-ultra") -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model=model,
        messages=[Message(role="user", content=text)],
    )


def _config(routes: bool = True, members: bool = True) -> dict:
    agent_routes = {
        "scout": {"primary": "scout-model", "fallback_1": "sf1", "fallback_2": "sf2"},
        "planner": {"primary": "planner-model", "fallback_1": "pf1", "fallback_2": "pf2"},
        "scout_external": {"primary": "scout-ext-model", "fallback_1": "sef1", "fallback_2": "sef2"},
        "planner_architect": {"primary": "arch-model", "fallback_1": "af1", "fallback_2": "af2"},
        "planner_challenger": {"primary": "challenger-model", "fallback_1": "cf1", "fallback_2": "cf2"},
        "planner_planner": {"primary": "plan-model", "fallback_1": "ppf1", "fallback_2": "ppf2"},
        "planner_brainstormer": {"primary": "brain-model", "fallback_1": "bf1", "fallback_2": "bf2"},
        "auditor": {"primary": "auditor-model", "fallback_1": "auf1", "fallback_2": "auf2"},
        "auditor_reviewer": {"primary": "reviewer-model", "fallback_1": "rf1", "fallback_2": "rf2"},
        "auditor_auditor": {"primary": "deep-audit-model", "fallback_1": "daf1", "fallback_2": "daf2"},
        "fast_coder": {"primary": "fast-model", "fallback_1": "ff1", "fallback_2": "ff2"},
        "power_coder": {"primary": "power-model", "fallback_1": "pwf1", "fallback_2": "pwf2"},
        "ultra_coder": {"primary": "ultra-model", "fallback_1": "uf1", "fallback_2": "uf2"},
        "frontend_coder": {"primary": "front-model", "fallback_1": "ftf1", "fallback_2": "ftf2"},
        "refactor": {"primary": "refactor-model", "fallback_1": "ref1", "fallback_2": "ref2"},
        "general": {"primary": "general-model", "fallback_1": "gf1", "fallback_2": "gf2"},
    } if routes else {}
    member_routes = {
        "planner_architect": {"primary": "member-arch"},
        "planner_challenger": {"primary": "member-challenger"},
        "auditor_reviewer": {"primary": "member-reviewer"},
        "auditor_auditor": {"primary": "member-auditor"},
        "planner_planner": {"primary": "member-planner"},
        "planner_brainstormer": {"primary": "member-brain"},
        "fast_coder": {"primary": "member-fast"},
        "power_coder": {"primary": "member-power"},
        "ultra_coder": {"primary": "member-ultra"},
        "frontend_coder": {"primary": "member-front"},
        "refactor": {"primary": "member-refactor"},
        "scout_external": {"primary": "member-scout-ext"},
    } if members else {}
    return {
        "bsl_models": {
            "bsl_agentic_ultra": {
                "enabled": True,
                "agent_routes": agent_routes,
                "member_routes": member_routes,
                "orchestration": {
                    "phase_cap": 22,
                    "rounds": 1,
                    "member_timeout_ms": 600000,
                    "total_budget_ms": 900000,
                },
                "global_last_fallback": "GLM-5.2",
            },
        },
    }


SUBSTANTIAL = "x" * 250  # passes the substance gate


def _fake_execute(log: list, text: str = None):
    """Execute stub: records every call; returns substantial text."""
    async def execute(role_model, messages, max_tokens, timeout):
        log.append({
            "model": role_model,
            "max_tokens": max_tokens,
            "timeout": timeout,
            "system": messages[0]["content"] if messages else "",
        })
        return {"text": text or SUBSTANTIAL, "tokens_in": 10, "tokens_out": 20}
    return execute


# ─── Template matching ───────────────────────────────────────────────────────


def test_template_match_per_category() -> None:
    assert [p.sub_role for p in match_template("planner", "architect the new auth subsystem")][:1] == ["scout_internal"]
    assert match_template("planner", "architect the new auth subsystem")[1].sub_role == "planner_architect"
    assert match_template("auditor", "check for security issues")[1].sub_role == "auditor_auditor"
    assert match_template("refactor", "extract the helper")[1].sub_role == "planner_architect"
    # bug-fix: coder lane substituted from the classified category
    bug = match_template("power_coder", "fix the crash in parser")
    assert bug[1].sub_role == "power_coder"
    assert bug[1].model == "medium"
    # feature-build for build verbs
    feat = match_template("ultra_coder", "build a new feature module")
    assert feat[1].sub_role == "planner_architect"
    # explain: read-only, never reaches arch-plan's write phase
    exp = match_template("planner", "explain how the router works")
    assert exp[1].sub_role == "planner_planner"
    assert exp[1].tools == "read-only"
    # research: scout.external lane
    res = match_template("scout", "research the best vector database")
    assert res[0].sub_role == "scout_external"


def test_arch_plan_seven_phases() -> None:
    phases = match_template("planner", "architect the new subsystem")
    assert len(phases) == 7
    assert phases[5].sub_role == "plan_exit"
    assert phases[5].tools == "human-gate"
    assert phases[6].sub_role == "planner_planner"
    assert phases[6].tools == "write"


def test_template_metadata_matches_reference() -> None:
    """subRole/modelTier/maxTokens/timeout ports must match phase-templates.ts."""
    phases = PHASE_TEMPLATES["arch-plan"]
    assert (phases[0].sub_role, phases[0].model, phases[0].max_tokens, phases[0].timeout) == (
        "scout_internal", "fast", 2000, 15.0)
    assert (phases[1].sub_role, phases[1].model, phases[1].max_tokens, phases[1].timeout) == (
        "planner_architect", "reasoning", 16000, 60.0)
    assert phases[1].reassess_after is True
    assert (phases[2].sub_role, phases[2].max_tokens) == ("planner_challenger", 12000)
    assert (phases[4].sub_role, phases[4].max_tokens, phases[4].timeout) == (
        "auditor_reviewer", 10000, 45.0)
    assert (phases[5].max_tokens, phases[5].timeout) == (1000, 60.0)
    research = PHASE_TEMPLATES["research"]
    assert (research[0].sub_role, research[0].max_tokens, research[0].timeout) == (
        "scout_external", 3000, 20.0)
    assert research[1].sub_role == "planner_brainstormer"


# ─── Expansion ───────────────────────────────────────────────────────────────


def test_balanced_expansion_splits_reasoning_roles() -> None:
    phases = match_template("planner", "architect the new subsystem")
    expanded = expand_reasoning_phases(phases, tier="balanced")
    # 7 template phases: scout(no split) + 5 reasoning roles x3 groups +
    # plan_exit(no mode) = 17
    assert len(expanded) == 17
    grouped = [p for p in expanded if p.phase_group is not None]
    assert len(grouped) == 15
    # Every group boundary is one of the three behavior boundaries
    assert all(p.phase_group.boundary in ("commit", "critique", "synthesize") for p in grouped)
    # Group briefs are appended to the description
    architect_first = next(
        p for p in expanded if p.sub_role == "planner_architect" and p.phase_group.idx == 0
    )
    assert architect_first.phase_group.boundary == "commit"
    assert "Map the terrain" in architect_first.description
    # Intermediate group phases lose reassess_after (TS Bug #2 fix)
    assert architect_first.reassess_after is False
    architect_last = next(
        p for p in expanded if p.sub_role == "planner_architect"
        and p.phase_group.idx == 2
    )
    assert architect_last.reassess_after is True


def test_scout_never_expands() -> None:
    phases = match_template("planner", "architect the subsystem")
    expanded = expand_reasoning_phases(phases, tier="balanced")
    scouts = [p for p in expanded if p.sub_role == "scout_internal"]
    assert len(scouts) == 1
    assert scouts[0].phase_group is None


def test_fast_tier_injects_brief_no_split() -> None:
    phases = match_template("planner", "architect the subsystem")
    fast = expand_reasoning_phases(phases, tier="fast")
    assert len(fast) == len(phases)  # no splitting
    assert all(p.phase_group is None for p in fast)
    arch = fast[1]
    assert "[FAST]" in arch.description
    assert FAST_BRIEF["planner_architect"] in arch.description


# ─── Preflight ───────────────────────────────────────────────────────────────


def test_preflight_ready() -> None:
    phases = expand_reasoning_phases(match_template("planner", "arch it"))
    cfg = _config()
    pre = preflight_slots(phases, cfg["bsl_models"]["bsl_agentic_ultra"]["agent_routes"],
                          cfg["bsl_models"]["bsl_agentic_ultra"]["member_routes"])
    assert pre.status == "ready"
    assert pre.missing == []


def test_preflight_blocked_missing_member() -> None:
    phases = expand_reasoning_phases(match_template("planner", "arch it"))
    cfg = _config(members=False)
    pre = preflight_slots(phases, cfg["bsl_models"]["bsl_agentic_ultra"]["agent_routes"], {})
    assert pre.status == "blocked"
    member_slots = [m.role for m in pre.missing if ":" in m.role]
    assert "planner_architect:1" in member_slots


def test_preflight_degraded_missing_fallback() -> None:
    phases = expand_reasoning_phases(match_template("planner", "arch it"))
    routes = {"scout": {"primary": "s"}, "planner": {"primary": "p"},
              "planner_architect": {"primary": "a"}, "planner_challenger": {"primary": "c"},
              "planner_planner": {"primary": "pp"}, "auditor_reviewer": {"primary": "r"}}
    pre = preflight_slots(phases, routes, {"planner_architect": {"primary": "ma"},
                                           "planner_challenger": {"primary": "mc"},
                                           "planner_planner": {"primary": "mp"},
                                           "auditor_reviewer": {"primary": "mr"}})
    assert pre.status == "degraded"
    assert pre.missing == []


# ─── run_balanced with mocked execute ────────────────────────────────────────


def test_run_balanced_phase_order_and_synthesis_last() -> None:
    log = []
    result = asyncio.run(run_balanced(_request("architect the new auth subsystem"), _config(),
                                      _fake_execute(log)))
    roles = [t.sub_role for t in result.trace if not t.member]
    assert roles[0] == "scout_internal"
    assert roles[-1] == "planner_planner"  # synthesis/write phase is final
    assert result.phases_total == 17  # expanded arch-plan
    assert result.capped is False
    assert result.final_text == SUBSTANTIAL


def test_run_balanced_token_aggregation() -> None:
    result = asyncio.run(run_balanced(_request("audit this module for vulnerabilities"),
                                      _config(), _fake_execute([])))
    expected_calls = len(result.trace)
    assert expected_calls > 2
    assert result.tokens_in == expected_calls * 10
    assert result.tokens_out == expected_calls * 20


def test_member_only_on_commit_boundary() -> None:
    log = []
    result = asyncio.run(run_balanced(_request("architect the new auth subsystem"), _config(),
                                      _fake_execute(log)))
    member_calls = [t for t in result.trace if t.member]
    assert member_calls, "commit-boundary phases must spawn members"
    assert all(t.boundary == "commit" for t in member_calls)
    # Every commit-boundary phase in the expanded plan spawned exactly 1 member
    commit_phases = set()
    for t in result.trace:
        if t.boundary == "commit":
            commit_phases.add(t.phase_idx)
    assert len(member_calls) == len(commit_phases)
    assert all(t.model.startswith("member-") for t in member_calls)


def test_member_missing_degrades_to_lead_only() -> None:
    cfg = _config(members=False)
    result = asyncio.run(run_balanced(_request("architect the new auth subsystem"), cfg,
                                      _fake_execute([])))
    assert not any(t.member for t in result.trace)
    assert any("missing_member_primary" in d for d in result.degraded)
    assert result.phases_total > 0  # still ran lead-only


def test_substance_gate_retries_once() -> None:
    calls = {"n": 0}

    async def execute(role_model, messages, max_tokens, timeout):
        calls["n"] += 1
        # First lead call thin, everything after substantial.
        text = "thin" if calls["n"] == 1 else SUBSTANTIAL
        return {"text": text, "tokens_in": 1, "tokens_out": 1}

    result = asyncio.run(run_balanced(_request("audit this code"), _config(), execute))
    assert calls["n"] > 3  # retries happened beyond base phases
    assert result.final_text == SUBSTANTIAL


def test_phase_cap_enforced() -> None:
    cfg = _config()
    cfg["bsl_models"]["bsl_agentic_ultra"]["orchestration"]["phase_cap"] = 3
    log = []
    result = asyncio.run(run_balanced(_request("architect the new auth subsystem"), cfg,
                                      _fake_execute(log)))
    assert result.phases_total == 3
    assert result.capped is True
    assert len([t for t in result.trace if not t.member]) == 3


def test_default_cap_is_22() -> None:
    assert SESSION_PHASE_CAP_BALANCED == 22
    cfg = _config()
    cfg["bsl_models"]["bsl_agentic_ultra"]["orchestration"] = {}
    result = asyncio.run(run_balanced(_request("architect the new auth subsystem"), cfg,
                                      _fake_execute([])))
    assert result.phases_total == 17
    assert result.capped is False


def test_phase_budgets_flow_to_execute() -> None:
    log = []
    asyncio.run(run_balanced(_request("audit this code for injection"), _config(),
                             _fake_execute(log)))
    assert log[0]["max_tokens"] == 2000
    assert log[1]["max_tokens"] == 16000  # auditor template: 16000 on phase 2


def test_substance_threshold_is_200() -> None:
    assert SUBSTANCE_GATE_MIN_CHARS == 200


# ─── main.py wiring smoke ────────────────────────────────────────────────────


def test_main_dispatch_wires_run_balanced() -> None:
    """main.py's ultra dispatch invokes the balanced orchestration loop."""
    import inspect
    import app.main as main_mod

    src = inspect.getsource(main_mod._bsl_agentic_ultra_dispatch)
    assert "run_balanced" in src, (
        "_bsl_agentic_ultra_dispatch must invoke the balanced orchestration loop"
    )
    assert "execute_upstream" in src


def test_main_ultra_post_returns_single_response(monkeypatch) -> None:
    """POST /v1/chat/completions model=blacksand-agentic-ultra returns 200 with
    one final response (mocked upstream executor)."""
    import app.config_state as cs
    import app.main as main_mod

    monkeypatch.setattr(cs, "get_config", lambda: _config())
    monkeypatch.setattr(main_mod, "cs_get_config", lambda: _config())

    async def fake_internal(body, client_wants_anthropic=False,
                            client_wants_gemini=False, _retry_state=None, request=None):
        from fastapi.responses import JSONResponse

        return JSONResponse({
            "id": "chatcmpl-internal",
            "object": "chat.completion",
            "created": 0,
            "model": body.get("model", "x"),
            "choices": [{"index": 0, "message": {"role": "assistant",
                                                  "content": SUBSTANTIAL},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12},
        }, status_code=200)

    monkeypatch.setattr(main_mod, "_process_chat_completion", fake_internal)

    from fastapi.testclient import TestClient

    with TestClient(main_mod.app) as client:
        resp = client.post("/v1/chat/completions", json={
            "model": "blacksand-agentic-ultra",
            "messages": [{"role": "user", "content": "architect the new auth subsystem"}],
            "stream": False,
        })
    assert resp.status_code == 200, resp.text[:500]
    data = resp.json()
    assert data["object"] == "chat.completion"
    assert data["choices"][0]["message"]["content"]
    assert data["usage"]["total_tokens"] > 0
