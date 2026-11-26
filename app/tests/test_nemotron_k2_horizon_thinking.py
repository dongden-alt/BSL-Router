"""Nemotron 3/3.5 & K2-Horizon family contract & UI thinking-menu parity tests."""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.compat.families import resolve_thinking


NEMOTRON_IDS = [
    "crowllm/nemotron-3-ultra-550b-a55b-uncensored:free",
    "crowllm/nemotron-3-ultra-550b-a55b:free",
    "crowllm/nemotron-3.5-super-vl:free",
    "crowllm/nvidia/nemotron-3.5-super-vl-120b-a12b:free",
    "crowllm/nemotron-ultra",
    "kilocode/nvidia/nemotron-3-ultra-550b-a55b:free",
    "mintrouter/nemotron-3-ultra-free",
    "numesai/nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
]

NEMOTRON_NON_REASONING_IDS = [
    "crowllm/nvidia/nemotron-3.5-bolt-e4b:free",
    "kilocode/nvidia/nemotron-3.5-content-safety:free",
]

K2_HORIZON_IDS = [
    "crowllm/k2-horizon-375b-a23b-uncensored:free",
    "crowllm/k2-horizon-375b-a23b:free",
    "crowllm/k2-horizon",
]


@pytest.mark.parametrize("f_val", NEMOTRON_IDS)
@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh"])
def test_nemotron_openai_valid_efforts(f_val, effort):
    p, prov = resolve_thinking({}, f_val, effort, wire_format="openai")
    assert p["reasoning_effort"] == effort
    assert "thinking" not in p
    assert "output_config" not in p
    assert any(r.contract_id == "nemotron-3" for r in prov.records)


@pytest.mark.parametrize("f_val", NEMOTRON_IDS)
@pytest.mark.parametrize("clamped_effort", ["max", "ultra", "32k", "enable"])
def test_nemotron_openai_max_and_budget_clamp_to_high(f_val, clamped_effort):
    """Live probe (2026-11-26): vLLM does not map 'max' above 'high' and falls back to 63 tokens."""
    p, prov = resolve_thinking({}, f_val, clamped_effort, wire_format="openai")
    assert p["reasoning_effort"] == "high"
    assert any(r.contract_id == "nemotron-3" and r.rule == "reasoning_effort_normalized" for r in prov.records)


@pytest.mark.parametrize("f_val", NEMOTRON_IDS)
@pytest.mark.parametrize("off_word", ["off", "none", "disabled"])
def test_nemotron_openai_explicit_off_emits_none(f_val, off_word):
    p, prov = resolve_thinking(
        {"thinking": {"type": "enabled"}, "output_config": {"effort": "high"}},
        f_val,
        off_word,
        wire_format="openai",
    )
    assert p["reasoning_effort"] == "none"
    assert "thinking" not in p
    assert "output_config" not in p
    assert any(r.contract_id == "nemotron-3" for r in prov.records)


@pytest.mark.parametrize("f_val", NEMOTRON_NON_REASONING_IDS)
def test_nemotron_non_reasoning_skus_excluded(f_val):
    p, prov = resolve_thinking({}, f_val, "high", wire_format="openai")
    assert "reasoning_effort" not in p
    assert prov.records == []


@pytest.mark.parametrize("f_val", K2_HORIZON_IDS)
@pytest.mark.parametrize("effort", ["low", "high"])
def test_k2_horizon_openai_valid_efforts_and_preserves_sampling(f_val, effort):
    p, prov = resolve_thinking(
        {"temperature": 0.7, "top_p": 0.9},
        f_val,
        effort,
        wire_format="openai",
    )
    assert p["reasoning_effort"] == effort
    assert p["temperature"] == 0.7
    assert p["top_p"] == 0.9
    assert "enable_thinking" not in p
    assert any(r.contract_id == "k2-horizon" for r in prov.records)


@pytest.mark.parametrize("f_val", K2_HORIZON_IDS)
@pytest.mark.parametrize("coerced_to_high", ["auto", "", "medium", "xhigh", "max", "enable"])
def test_k2_horizon_coerces_auto_medium_xhigh_max_to_high(f_val, coerced_to_high):
    """Live probe (2026-11-26): omitting reasoning_effort (auto) or passing medium/xhigh/max
    causes CrowLLM's upstream parser to leak raw <ifm|think> monologue into content."""
    p, prov = resolve_thinking({}, f_val, coerced_to_high, wire_format="openai")
    assert p["reasoning_effort"] == "high"
    assert any(r.contract_id == "k2-horizon" for r in prov.records)


@pytest.mark.parametrize("f_val", K2_HORIZON_IDS)
@pytest.mark.parametrize("off_word", ["off", "none", "disabled"])
def test_k2_horizon_openai_explicit_off_emits_none(f_val, off_word):
    p, prov = resolve_thinking({}, f_val, off_word, wire_format="openai")
    assert p["reasoning_effort"] == "none"
    assert any(r.contract_id == "k2-horizon" for r in prov.records)


@pytest.mark.parametrize("f_val", NEMOTRON_IDS)
@pytest.mark.parametrize("unset_word", ["auto", ""])
def test_nemotron_auto_leaves_payload_untouched(f_val, unset_word):
    p, prov = resolve_thinking({}, f_val, unset_word, wire_format="openai")
    assert p == {}
    assert prov.records == []


def test_openrouter_still_outranks_nemotron():
    p, prov = resolve_thinking(
        {},
        "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free",
        "high",
        wire_format="openai",
    )
    assert p["reasoning"] == {"effort": "high", "exclude": False}
    assert any(r.contract_id == "openrouter" for r in prov.records)


def test_app_js_get_thinking_spec_exposes_nemotron_and_k2_horizon_menus():
    app_js = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "static",
        "app.js",
    )
    with open(app_js, "r", encoding="utf-8") as fh:
        src = fh.read()

    m_k2h = re.search(
        r"if\s*\(/\\bk2\[-_\.\]\?horizon\\b/\.test\(id\)\)\s*return\s*\{\s*effort:\s*\[([^\]]+)\]",
        src,
    )
    assert m_k2h is not None, "getThinkingSpec missing k2-horizon branch in app/static/app.js"
    for lvl in ("'off'", "'low'", "'high'"):
        assert lvl in m_k2h.group(1)
    for bad in ("'medium'", "'xhigh'", "'max'"):
        assert bad not in m_k2h.group(1)

    m_nem = re.search(
        r"if\s*\(/\\bnemotron\\b/\.test\(id\)\)\s*\{[^}]*return\s*\{\s*effort:\s*\[([^\]]+)\]",
        src,
    )
    assert m_nem is not None, "getThinkingSpec missing nemotron branch in app/static/app.js"
    for lvl in ("'off'", "'low'", "'medium'", "'high'", "'xhigh'"):
        assert lvl in m_nem.group(1)
    assert "'max'" not in m_nem.group(1)
