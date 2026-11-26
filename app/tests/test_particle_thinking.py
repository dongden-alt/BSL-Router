"""Particle-1 family contract & UI thinking-menu parity tests."""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.compat.families import resolve_thinking


PARTICLE_IDS = [
    "crowllm/particle-1-uncensored:free",
    "crowllm/particle-1:free",
    "kilocode-free/particle-ai/particle-1:free",
    "freepi-free/particle",
]


@pytest.mark.parametrize("f_val", PARTICLE_IDS)
@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
def test_particle_openai_valid_efforts(f_val, effort):
    p, prov = resolve_thinking({}, f_val, effort, wire_format="openai")
    assert p["reasoning_effort"] == effort
    assert "thinking" not in p
    assert "output_config" not in p
    assert any(r.contract_id == "particle-1" for r in prov.records)


@pytest.mark.parametrize("f_val", PARTICLE_IDS)
@pytest.mark.parametrize("off_word", ["off", "none", "disabled"])
def test_particle_openai_explicit_off_emits_none(f_val, off_word):
    p, prov = resolve_thinking(
        {"thinking": {"type": "enabled"}, "output_config": {"effort": "high"}},
        f_val,
        off_word,
        wire_format="openai",
    )
    assert p["reasoning_effort"] == "none"
    assert "thinking" not in p
    assert "output_config" not in p
    assert any(r.contract_id == "particle-1" for r in prov.records)


@pytest.mark.parametrize("f_val", PARTICLE_IDS)
@pytest.mark.parametrize("unset_word", ["auto", ""])
def test_particle_auto_leaves_payload_untouched(f_val, unset_word):
    p, prov = resolve_thinking({}, f_val, unset_word, wire_format="openai")
    assert p == {}
    assert prov.records == []


def test_particle_minimal_and_unknown_normalize():
    p_min, _ = resolve_thinking({}, "crowllm/particle-1-uncensored:free", "minimal", wire_format="openai")
    assert p_min["reasoning_effort"] == "low"

    p_unk, _ = resolve_thinking({}, "crowllm/particle-1-uncensored:free", "32k", wire_format="openai")
    assert p_unk["reasoning_effort"] == "high"


def test_particle_anthropic_wire():
    p_on, _ = resolve_thinking(
        {"reasoning_effort": "stale"},
        "crowllm/particle-1-uncensored:free",
        "high",
        wire_format="anthropic",
    )
    assert p_on["thinking"] == {"type": "enabled"}
    assert p_on["output_config"] == {"effort": "high"}
    assert "reasoning_effort" not in p_on

    p_off, _ = resolve_thinking(
        {"reasoning_effort": "high"},
        "crowllm/particle-1-uncensored:free",
        "off",
        wire_format="anthropic",
    )
    assert p_off["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in p_off
    assert "output_config" not in p_off


def test_openrouter_still_outranks_particle():
    p, prov = resolve_thinking(
        {},
        "openrouter/particle-ai/particle-1:free",
        "high",
        wire_format="openai",
    )
    assert p["reasoning"] == {"effort": "high", "exclude": False}
    assert any(r.contract_id == "openrouter" for r in prov.records)


def test_app_js_get_thinking_spec_exposes_particle_menu():
    app_js = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "static",
        "app.js",
    )
    with open(app_js, "r", encoding="utf-8") as fh:
        src = fh.read()
    m = re.search(
        r"if\s*\(/\\bparticle\(\?:\[-_\.\]\?\\d\+\)\?\\b/\.test\(id\)\)\s*return\s*\{\s*effort:\s*\[([^\]]+)\]",
        src,
    )
    assert m is not None, "getThinkingSpec missing particle-1 branch in app/static/app.js"
    for lvl in ("'off'", "'low'", "'medium'", "'high'", "'xhigh'", "'max'"):
        assert lvl in m.group(1)
