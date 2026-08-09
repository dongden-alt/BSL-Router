"""
Qwen thinking-level contract tests.

Qwen reasoning is TWO axes, and both are version-dependent. Official source:
https://docs.qwencloud.com/developer-guides/text-generation/thinking

    qwen3.8-max             enable_thinking + reasoning_effort {low, medium, xhigh}
                            default xhigh. NO 'high', NO 'max'.
    qwen3.7-max and older   hybrid; enable_thinking bool ONLY (no published enum)
    *-thinking SKUs         thinking-only; cannot be disabled

These tests exist because the previous contract shipped two real defects that
would each produce an upstream 400 or silently wrong reasoning depth:

  DEFECT 1 — `_EFFORT_WORDS = (low, medium, high, max)` contained two words
    Qwen rejects and OMITTED `xhigh`. So `xhigh` — the model's only valid
    ceiling AND its own default — was coerced away to the invalid `max`.

  DEFECT 2 — a `require_effort` rule in `sanitize` injected an effort even
    when the operator selected `off`, so `off` behaved as `max` and thinking
    was undisableable on a hybrid model.

Run:
  .venv\\Scripts\\python -m pytest app/tests/test_qwen_thinking_levels.py -q
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.compat.families import resolve_thinking

# Values Alibaba publishes for qwen3.8-max. Anything else is out-of-contract.
V38_VALID = {"low", "medium", "xhigh"}

# Real ids drawn from config.yaml so these tests track shipped routes.
V38_ROUTES = [
    "qwencoder/qwen3.8-max",
    "vsllm-gpt/qwen3.8-max",
    "ltn-ai/ltnai/alibaba/qwen3.8-max",
]
V37_ROUTES = [
    "qwencoder/qwen3.7-max",
    "vsllm-gpt/qwen3.7-max",
    "ltn-ai/ltnai/alibaba/qwen3.7-max",
    "opencode-zen/qwen3.7-max",
]


def _payload(**extra):
    p = {"model": "x", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 8192}
    p.update(extra)
    return p


# ─────────────────────────────────────────────────────────────────────
# DEFECT 1 — the valid ceiling must survive.
# ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("f_val", V38_ROUTES)
def test_v38_xhigh_survives(f_val):
    """`xhigh` is qwen3.8-max's documented default AND ceiling.

    The old contract had no 'xhigh' in its vocabulary, so it coerced this
    valid value to 'max' — a word the enum does not contain.
    """
    out, _ = resolve_thinking(_payload(), f_val, "xhigh")
    assert out["reasoning_effort"] == "xhigh", (
        f"{f_val}: xhigh was rewritten to {out.get('reasoning_effort')!r}. "
        "It is the model's own default and must pass through untouched."
    )


@pytest.mark.parametrize("f_val", V38_ROUTES)
@pytest.mark.parametrize("effort", ["low", "medium", "xhigh"])
def test_v38_published_values_pass_through(f_val, effort):
    """Every value in the published enum survives verbatim."""
    out, _ = resolve_thinking(_payload(), f_val, effort)
    assert out["reasoning_effort"] == effort
    assert out["enable_thinking"] is True


@pytest.mark.parametrize("f_val", V38_ROUTES)
@pytest.mark.parametrize("effort", ["high", "max", "minimal", "medium", "low", "xhigh", "enable", "adaptive"])
def test_v38_never_emits_an_unpublished_value(f_val, effort):
    """The single invariant that prevents a 400: whatever we send is in the enum.

    This is the generalized guard. Individual clamp directions are asserted
    below, but this one fails for ANY future value that escapes the table.
    """
    out, _ = resolve_thinking(_payload(), f_val, effort)
    sent = out.get("reasoning_effort")
    assert sent is None or sent in V38_VALID, (
        f"{f_val} effort={effort!r}: sent reasoning_effort={sent!r}, which is "
        f"not in the published enum {sorted(V38_VALID)} — upstream 400."
    )


@pytest.mark.parametrize("f_val", V38_ROUTES)
def test_v38_high_and_max_clamp_up_to_xhigh(f_val):
    """`high`/`max` round UP to the ceiling, not down.

    Direction matters. xhigh is the model's DEFAULT, so clamping a request
    for MORE reasoning down to 'medium' would deliver LESS than an
    unconfigured request — the opposite of the operator's intent.
    """
    for effort in ("high", "max"):
        out, _ = resolve_thinking(_payload(), f_val, effort)
        assert out["reasoning_effort"] == "xhigh", (
            f"{f_val} effort={effort!r}: expected clamp UP to xhigh, got "
            f"{out.get('reasoning_effort')!r}"
        )


@pytest.mark.parametrize("f_val", V38_ROUTES)
def test_v38_minimal_clamps_down_to_low(f_val):
    """`minimal` has no wire equivalent; the floor is `low`."""
    out, _ = resolve_thinking(_payload(), f_val, "minimal")
    assert out["reasoning_effort"] == "low"


@pytest.mark.parametrize("f_val", V38_ROUTES)
@pytest.mark.parametrize("effort", ["enable", "adaptive"])
def test_v38_enable_omits_effort_for_model_default(f_val, effort):
    """'thinking on, no depth specified' => omit effort, model applies xhigh.

    Qwen has no wire `adaptive`, so the honest translation of "let the model
    decide" is to send the boolean and no effort at all.
    """
    out, _ = resolve_thinking(_payload(), f_val, effort)
    assert out["enable_thinking"] is True
    assert "reasoning_effort" not in out, (
        f"{f_val} effort={effort!r}: fabricated a depth "
        f"({out.get('reasoning_effort')!r}) the operator did not request"
    )


# ─────────────────────────────────────────────────────────────────────
# Qwen3.7 and older — hybrid boolean, no effort enum.
# ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("f_val", V37_ROUTES)
@pytest.mark.parametrize("effort", ["enable", "adaptive", "max", "high", "low", "medium", "xhigh"])
def test_v37_is_boolean_only(f_val, effort):
    """No effort enum is published for 3.7-Max, so we must not send one.

    Regardless of which tier the operator picks, the only reasoning axis is
    the boolean. Previously every one of these sent reasoning_effort=max.
    """
    out, _ = resolve_thinking(_payload(), f_val, effort)
    assert out["enable_thinking"] is True
    assert "reasoning_effort" not in out, (
        f"{f_val} effort={effort!r}: sent reasoning_effort="
        f"{out.get('reasoning_effort')!r} to a boolean-only hybrid model"
    )


# ─────────────────────────────────────────────────────────────────────
# DEFECT 2 — `off` must actually turn thinking off.
# ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("f_val", V38_ROUTES + V37_ROUTES)
def test_off_disables_thinking(f_val):
    """`off` previously injected reasoning_effort=max — it meant MAX, not off."""
    out, _ = resolve_thinking(_payload(), f_val, "off")
    assert "reasoning_effort" not in out, (
        f"{f_val} effort=off: still sent reasoning_effort="
        f"{out.get('reasoning_effort')!r} — 'off' silently meant 'on'"
    )
    assert out.get("enable_thinking") is False, (
        f"{f_val} effort=off: expected enable_thinking=False, got "
        f"{out.get('enable_thinking')!r}"
    )


@pytest.mark.parametrize("effort", ["off", "none", "false", "disable"])
def test_off_synonyms_all_disable(effort):
    """Config and clients spell 'off' several ways; all must disable."""
    out, _ = resolve_thinking(_payload(), "qwencoder/qwen3.8-max", effort)
    assert out.get("enable_thinking") is False
    assert "reasoning_effort" not in out


def test_off_omits_flag_on_thinking_only_sku():
    """A `*-thinking` SKU cannot disable reasoning.

    Sending enable_thinking=false would be a request the model cannot honor.
    Omitting the field is the honest payload.
    """
    out, _ = resolve_thinking(
        _payload(), "openrouter/qwen/qwen3-max-thinking", "off"
    )
    assert "enable_thinking" not in out, (
        "sent enable_thinking=false to a thinking-only SKU that cannot honor it"
    )
    assert "reasoning_effort" not in out


# ─────────────────────────────────────────────────────────────────────
# Unset ('auto') — express no preference.
# ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("f_val", V38_ROUTES + V37_ROUTES)
@pytest.mark.parametrize("effort", ["auto", ""])
def test_unset_sends_no_reasoning_fields(f_val, effort):
    """An unconfigured model must not have a thinking preference invented."""
    out, _ = resolve_thinking(_payload(), f_val, effort)
    assert "enable_thinking" not in out
    assert "reasoning_effort" not in out


# ─────────────────────────────────────────────────────────────────────
# Client-supplied values (BSL is a proxy — clients inject their own).
# ─────────────────────────────────────────────────────────────────────

def test_client_supplied_invalid_effort_is_clamped_when_unset():
    """A client's own `reasoning_effort` must be policed even at effort=auto.

    Claude Code / Codex forward their own reasoning_effort. With the operator
    on 'auto' the apply path is a no-op, so sanitize is the ONLY thing
    standing between a client's 'high' and an upstream 400.
    """
    out, _ = resolve_thinking(
        _payload(reasoning_effort="high"), "qwencoder/qwen3.8-max", "auto"
    )
    assert out["reasoning_effort"] == "xhigh"


def test_client_supplied_effort_stripped_for_hybrid_model():
    """3.7-Max publishes no enum, so a client-supplied effort must go."""
    out, _ = resolve_thinking(
        _payload(reasoning_effort="high"), "qwencoder/qwen3.7-max", "auto"
    )
    assert "reasoning_effort" not in out


def test_client_supplied_valid_effort_survives():
    """A client value already in the enum is left alone."""
    out, _ = resolve_thinking(
        _payload(reasoning_effort="medium"), "qwencoder/qwen3.8-max", "auto"
    )
    assert out["reasoning_effort"] == "medium"


# ─────────────────────────────────────────────────────────────────────
# Unconditional hygiene — must not regress while adding the axes above.
# ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("f_val", V38_ROUTES + V37_ROUTES)
@pytest.mark.parametrize("effort", ["off", "auto", "enable", "xhigh"])
def test_sampling_params_always_stripped(f_val, effort):
    """Qwen rejects these outright, at every thinking setting."""
    payload = _payload(
        temperature=0.7, top_p=0.9, presence_penalty=0.0, frequency_penalty=0.0, n=1
    )
    out, _ = resolve_thinking(payload, f_val, effort)
    for banned in ("temperature", "top_p", "presence_penalty", "frequency_penalty", "n"):
        assert banned not in out, f"{f_val} effort={effort!r}: leaked {banned}"


@pytest.mark.parametrize("effort", ["off", "auto", "enable", "xhigh"])
def test_reasoning_containers_always_stripped(effort):
    """`thinking`/`output_config`/`reasoning` => "Request body format invalid"."""
    payload = _payload(
        thinking={"type": "enabled"},
        output_config={"effort": "high"},
        reasoning={"effort": "high"},
    )
    out, _ = resolve_thinking(payload, "qwencoder/qwen3.8-max", effort)
    for banned in ("thinking", "output_config", "reasoning"):
        assert banned not in out, f"effort={effort!r}: leaked {banned}"


def test_max_tokens_cap_lookup_still_resolves():
    """main.py:4884 does matches_contract(f_val, "qwen") for the 65535 cap.

    The contract id MUST stay "qwen". Splitting it into qwen-3.8/qwen-hybrid
    (the Kimi K2/K3 pattern) would make this lookup silently return False and
    start 400-ing long requests. This test is why the version branch lives
    inside one contract instead of two.
    """
    from app.compat.families import matches_contract

    for f_val in V38_ROUTES + V37_ROUTES:
        assert matches_contract(f_val, "qwen"), (
            f"{f_val} no longer resolves to contract id 'qwen' — the "
            "max_tokens 65535 cap in main.py just went dead"
        )


def test_qwencoder_provider_still_excluded():
    """`qwencoder` is a PROVIDER name; it must not match the Qwen contract.

    Guards the pre-existing negative lookahead while the pattern is nearby.
    """
    payload = _payload(temperature=0.7, top_p=0.9)
    out, prov = resolve_thinking(payload, "qwencoder/claude-opus-4.8", "high")
    assert "temperature" in out
    assert "qwen" not in {r.contract_id for r in prov.records}


def test_tool_availability_reminder_preserved():
    """Qwen3.8-Max claims tools don't exist; the reminder must still inject."""
    payload = _payload(
        messages=[{"role": "system", "content": "You are helpful."}],
        tools=[{"type": "function", "function": {"name": "read_file"}}],
    )
    out, _ = resolve_thinking(payload, "vsllm-gpt/qwen3.8-max", "xhigh")
    assert "[BSL]" in out["messages"][0]["content"]


def test_provenance_recorded_for_every_setting():
    """Every write must be attributable — the point of the contract registry."""
    for effort in ("off", "enable", "xhigh", "high"):
        _out, prov = resolve_thinking(_payload(), "qwencoder/qwen3.8-max", effort)
        assert prov.records, f"effort={effort!r}: no provenance recorded"
        assert all(r.source == "families/qwen.py" for r in prov.records)
