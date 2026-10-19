"""
Codex Responses-wire sanitize tests (2026-10-19).

Regression coverage for the codex/gpt-5.6-sol 502: codex_adapter
.openai_to_responses() builds a clean Responses body ({input, model,
store:false, stream:true, reasoning:{effort}}) BEFORE resolve_thinking
runs. The gpt-5 family contract must NOT then stamp top-level
`reasoning_effort` or `reasoning.mode="pro"` onto that already
Responses-shaped body — chatgpt.com/backend-api/codex/responses 400s on
both ("Unsupported parameter: reasoning_effort" / "reasoning.mode is
not supported with this model."). Probe6: context="all_turns" +
mode="standard" -> 200.

Run:
  .venv311\\Scripts\\python -m pytest app/tests/test_codex_wire_sanitize.py -q
"""
import os
import sys

sys.path.insert(
    0,
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
)

from app.compat.families._base import Provenance, ThinkingContext
from app.compat.families.openai import CONTRACTS, _apply_gpt5


def _ctx() -> ThinkingContext:
    return ThinkingContext(
        f_val="codex/gpt-5.6-sol",
        effort="max",
        reasoning_mode="pro",
        reasoning_context="all_turns",
        wire_format="openai",
    )


def test_responses_wire_sanitize_clean_adapter_body():
    """Clean adapter body: no reasoning_effort is added, no mode is added,
    adapter-set reasoning.effort survives, and the sanitize is attributed."""
    payload = {
        "input": [{"role": "user", "content": "hi"}],
        "model": "gpt-5.6-sol",
        "store": False,
        "stream": True,
        "reasoning": {"effort": "xhigh"},
    }
    prov = Provenance()
    out = _apply_gpt5(payload, _ctx(), prov, CONTRACTS[0])

    assert "reasoning_effort" not in out
    assert "mode" not in out["reasoning"]
    assert out["reasoning"]["effort"] == "xhigh"
    # The adapter body carried no context; the sanitize must not invent one
    # (and must never strip one either — see the next test).
    assert out["reasoning"].get("context") is None
    assert any(
        r.rule == "codex_responses_wire_sanitize" for r in prov.records
    ), f"expected codex_responses_wire_sanitize provenance, got {prov.summary()}"


def test_responses_wire_sanitize_strips_polluted_keys_keeps_context():
    """A Responses-shaped body that already carries the forbidden keys gets
    them removed; reasoning.context is preserved untouched."""
    payload = {
        "input": [{"role": "user", "content": "hi"}],
        "model": "gpt-5.6-sol",
        "store": False,
        "stream": True,
        "reasoning_effort": "max",
        "reasoning": {"effort": "xhigh", "mode": "pro", "context": "all_turns"},
    }
    prov = Provenance()
    out = _apply_gpt5(payload, _ctx(), prov, CONTRACTS[0])

    assert "reasoning_effort" not in out
    assert "mode" not in out["reasoning"]
    assert out["reasoning"]["effort"] == "xhigh"
    # context must NOT be stripped by the sanitize.
    assert out["reasoning"].get("context") == "all_turns"
    rec = next(
        (r for r in prov.records if r.rule == "codex_responses_wire_sanitize"),
        None,
    )
    assert rec is not None, f"expected sanitize provenance, got {prov.summary()}"
    assert "-reasoning_effort" in rec.fields
    assert "-reasoning.mode" in rec.fields


def test_chat_shaped_payload_openai_path_unchanged():
    """Negative control: a chat/completions-shaped payload (messages, no
    input) with the same ctx MUST still get reasoning_effort written."""
    payload = {
        "messages": [{"role": "user", "content": "hi"}],
        "model": "gpt-5.6-sol",
    }
    prov = Provenance()
    out = _apply_gpt5(payload, _ctx(), prov, CONTRACTS[0])

    assert out.get("reasoning_effort") == "max"
    assert out["reasoning"]["effort"] == "max"
    assert out["reasoning"]["mode"] == "pro"
    assert out["reasoning"]["context"] == "all_turns"
    assert not any(
        r.rule == "codex_responses_wire_sanitize" for r in prov.records
    )
