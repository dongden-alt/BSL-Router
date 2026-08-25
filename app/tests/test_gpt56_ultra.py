"""GPT-5.6-Sol 'ultra' handling (research 2026-08-25).

OpenAI's 'ultra' is Codex/ChatGPT-only multi-agent orchestration with NO REST
wire parameter. The deepest reasoning the API actually exposes for GPT-5.6 is
reasoning_effort='max'. BSL Router must therefore:

  - pass 'max' through verbatim (it is the real deepest tier, Sol-only), and
  - coerce any inbound 'ultra' to 'max' so an invalid reasoning_effort value is
    never emitted to the upstream.

The UI (app/static/app.js getThinkingSpec) intentionally does NOT offer 'ultra'
as a selectable chip, to avoid implying multi-agent behavior that the API cannot
perform. This module locks the backend contract instead.
"""
from app.compat.families import resolve_thinking

_GPT56_SOL = "openai/gpt-5.6-sol"


def _payload(**kw):
    return dict(kw)


def test_gpt56_sol_max_is_deepest_real_tier():
    out, _ = resolve_thinking(_payload(), _GPT56_SOL, "max")
    assert out.get("reasoning_effort") == "max"


def test_gpt56_sol_ultra_coerced_to_max():
    out, _ = resolve_thinking(_payload(), _GPT56_SOL, "ultra")
    assert out.get("reasoning_effort") == "max"


def test_gpt56_sol_ultra_reasoning_nested_coerced_to_max():
    out, _ = resolve_thinking(_payload(), _GPT56_SOL, "ultra")
    reasoning = out.get("reasoning", {})
    assert isinstance(reasoning, dict)
    assert reasoning.get("effort") == "max"


def test_gpt56_sol_xhigh_passthrough():
    out, _ = resolve_thinking(_payload(), _GPT56_SOL, "xhigh")
    assert out.get("reasoning_effort") == "xhigh"


def test_gpt56_sol_high_passthrough():
    out, _ = resolve_thinking(_payload(), _GPT56_SOL, "high")
    assert out.get("reasoning_effort") == "high"
