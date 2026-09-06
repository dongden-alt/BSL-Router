"""
Claude Fable 5.1 (+ Mythos 5.1) — official thinking-contract locks.

docs.anthropic.com (fetched 2026-09-06): 5.1 is ADAPTIVE-ONLY — a
`thinking {type: "enabled"}` payload 400s upstream — and effort runs
low/medium/high/xhigh/max (xhigh is NEW vs Fable 5) with NO 'off'
(thinking is always-on; the documented default is 'high'). Fable 5
(no .1) keeps the older adaptive|enabled contract unchanged.

Provenance contract id: claude-next-51 (families/anthropic.py).

Run:
  python -m pytest app/tests/test_fable_51_thinking.py -v
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.compat.families import resolve_thinking

_CONTRACT = "claude-next-51"

# Dotted, dashed, provider-prefixed, and the mythos skin — all must land on
# the SAME contract (dash->dot normalization happens in ThinkingContext,
# the [.-] in the pattern is belt-and-suspenders).
_F51_IDS = [
    "claude-fable-5.1",
    "claude-fable-5-1",
    "anthropic/claude-fable-5.1",
    "claude-mythos-5-1",
]


def _payload(**extra):
    body = {"model": "x", "messages": [], "max_tokens": 8192}
    body.update(extra)
    return body


# ─────────────────────────────────────────────────────────────────────────
# Resolution: every 5.1 spelling hits claude-next-51, never next/modern
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("f_val", _F51_IDS)
def test_51_ids_resolve_to_claude_next_51(f_val):
    out, prov = resolve_thinking(_payload(), f_val, "high")
    assert out.get("thinking") == {"type": "adaptive"}
    assert out.get("output_config") == {"effort": "high"}
    assert prov.records, f"no provenance for {f_val}: {prov.summary()}"
    assert all(r.contract_id == _CONTRACT for r in prov.records), prov.summary()
    contract_ids = {r.contract_id for r in prov.records}
    assert "claude-next" not in contract_ids
    assert "claude-modern" not in contract_ids


# ─────────────────────────────────────────────────────────────────────────
# Provenance fields populated (contract id, source, rule, fields)
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("f_val", _F51_IDS)
def test_51_provenance_fields_populated(f_val):
    _, prov = resolve_thinking(_payload(), f_val, "max")
    assert len(prov.records) == 1, prov.summary()
    rec = prov.records[0]
    assert rec.contract_id == _CONTRACT
    assert rec.source == "families/anthropic.py"
    assert rec.rule == "adaptive_only"
    assert "thinking" in rec.fields
    assert "output_config" in rec.fields


# ─────────────────────────────────────────────────────────────────────────
# Forced adaptive: reasoning_mode="enabled" must NOT leak onto the wire
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("f_val", ["claude-fable-5.1", "claude-fable-5-1"])
@pytest.mark.parametrize("effort", ["high", "auto"])
def test_enabled_mode_forced_to_adaptive(f_val, effort):
    out, _ = resolve_thinking(_payload(), f_val, effort, reasoning_mode="enabled")
    assert out.get("thinking") == {"type": "adaptive"}
    assert "enabled" not in str(out.get("thinking"))


# ─────────────────────────────────────────────────────────────────────────
# Effort clamp matrix: always-on means no off — every non-vocabulary value
# (auto/off/none/garbage) pins to the documented default 'high'; budget
# styles coerce by size via coerce_effort (32k->max, 16k->medium).
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("effort,expected", [
    ("auto", "high"),
    ("off", "high"),
    ("none", "high"),
    ("", "high"),
    ("garbage", "high"),
    ("banana", "high"),
    ("32k", "max"),
    ("16k", "medium"),
    ("low", "low"),
    ("medium", "medium"),
    ("high", "high"),
    ("xhigh", "xhigh"),
    ("max", "max"),
])
def test_51_effort_clamp_matrix(effort, expected):
    out, _ = resolve_thinking(_payload(), "claude-fable-5.1", effort)
    assert out.get("thinking") == {"type": "adaptive"}
    assert out.get("output_config") == {"effort": expected}


def test_51_dash_dotted_effort_parity_xhigh():
    """Dashed and dotted spellings clamp identically at the new xhigh tier."""
    out_d, prov_d = resolve_thinking(_payload(), "vsllm-a/fable-5-1", "xhigh")
    out_c, prov_c = resolve_thinking(_payload(), "vsllm-a/fable-5.1", "xhigh")
    assert out_d == out_c
    assert out_d.get("output_config") == {"effort": "xhigh"}
    assert prov_d.summary() == prov_c.summary()


# ─────────────────────────────────────────────────────────────────────────
# Boundary: Fable/Mythos 5 (no .1) keeps the older adaptive|enabled contract
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("f_val", ["claude-fable-5", "claude-mythos-5", "pix4k/fable-5"])
def test_fable_5_still_resolves_claude_next(f_val):
    out, prov = resolve_thinking(_payload(), f_val, "high")
    assert all(r.contract_id == "claude-next" for r in prov.records), prov.summary()
    assert out.get("thinking") == {"type": "adaptive"}  # default mode is adaptive


@pytest.mark.parametrize("f_val", ["claude-fable-5", "claude-mythos-5"])
def test_fable_5_still_allows_enabled_mode(f_val):
    """The old contract's signature behavior — opt-in enabled mode — survives."""
    out, prov = resolve_thinking(
        _payload(), f_val, "high", reasoning_mode="enabled"
    )
    assert out.get("thinking") == {"type": "enabled"}
    assert all(r.contract_id == "claude-next" for r in prov.records), prov.summary()
