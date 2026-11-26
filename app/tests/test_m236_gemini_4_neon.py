"""M236 Neon catalog and model-specific xhigh wire contracts."""
import ast
import copy
import json
from pathlib import Path
import re
import subprocess
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.compat.families import resolve_thinking
from app.compat.families._base import Provenance, ThinkingContext
from app.compat.families.gemini import CONTRACTS, _apply_gemini3
from app.compat.adapters.antigravity_upstream import (
    _EFFORT_TO_BUDGET,
    _PRO_TIER_ARGON_FALLBACK_MAP,
    openai_to_cloudcode_envelope,
)
from app.oauth import OAUTH_PROVIDER_MODELS

NEON = {"low": (534, 1), "medium": (533, 2), "high": (532, 3), "xhigh": (535, 100)}


@pytest.fixture(scope="module")
def backend():
    """Execute the actual pure helpers without importing the server lifecycle."""
    names = {
        "_AG_RESERVED_INDICES", "ANTIGRAVITY_INTEGRATION_SLOTS",
        "_ANTIGRAVITY_GEMINI_4_CARBON_CATALOG", "_DEFAULT_ANTIGRAVITY_QUOTA_SAVER",
        "_validate_app_custom_models", "_validate_antigravity_quota_saver_config",
        "_merge_antigravity_custom_models_catalog", "_is_known_antigravity_mapping_target",
    }
    tree = ast.parse((ROOT / "app/main.py").read_text(encoding="utf-8-sig"))
    selected = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            selected.append(node)
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Name) and t.id in names for t in targets):
                selected.append(node)
    namespace = {"re": re}
    exec(compile(ast.Module(body=selected, type_ignores=[]), "app/main.py", "exec"), namespace)
    assert names <= namespace.keys()
    return namespace


@pytest.mark.parametrize("family", ["neon", "hydrogen"])
@pytest.mark.parametrize("suffix", ["", "-xhigh", "-tiered-xhigh"])
@pytest.mark.parametrize("wire", ["gemini", "openai"])
@pytest.mark.parametrize("effort,expected", [("xhigh", "xhigh"), ("max", "xhigh"), ("ultra", "xhigh"), ("minimal", "low"), ("low", "low"), ("medium", "medium"), ("high", "high")])
def test_native_xhigh_vocabulary(family, suffix, wire, effort, expected):
    payload, _ = resolve_thinking({}, f"antigravity/gemini-4-{family}{suffix}", effort, wire_format=wire)
    if wire == "gemini":
        assert payload["generationConfig"]["thinkingConfig"]["thinkingLevel"] == expected
        assert "reasoning_effort" not in payload
    else:
        assert payload["reasoning_effort"] == expected
        assert "generationConfig" not in payload


@pytest.mark.parametrize("family", ["neon", "hydrogen"])
@pytest.mark.parametrize("effort", ["xhigh", "max", "ultra"])
@pytest.mark.parametrize("limit", [None, 4096, 200000])
def test_anthropic_xhigh_budget_and_output_headroom(family, effort, limit):
    payload = {} if limit is None else {"max_tokens": limit}
    result, _ = resolve_thinking(payload, f"gemini-4-{family}", effort, wire_format="anthropic")
    assert result["thinking"] == {"type": "enabled", "budget_tokens": 65536}
    assert result["max_tokens"] == max(limit or 0, 98304)


@pytest.mark.parametrize("model", ["gemini-4-argon", "gemini-4-carbon", "gemini-3.8-flash", "gemini-3.1-pro", "gemini-pro-agent"])
@pytest.mark.parametrize("wire", ["gemini", "openai"])
def test_legacy_xhigh_still_maps_to_max(model, wire):
    result, _ = resolve_thinking({}, model, "xhigh", wire_format=wire)
    level = result["generationConfig"]["thinkingConfig"]["thinkingLevel"] if wire == "gemini" else result["reasoning_effort"]
    assert level == "max"


def test_model_id_detection_and_native_sampling_preserved():
    ctx = ThinkingContext(f_val="provider/alias", model_id="GEMINI-4-NEON", effort="xhigh", wire_format="gemini")
    result = _apply_gemini3({"generationConfig": {"temperature": 0.2}}, ctx, Provenance(), CONTRACTS[0])
    assert result["generationConfig"] == {"temperature": 0.2, "thinkingConfig": {"thinkingLevel": "xhigh", "includeThoughts": True}}


@pytest.mark.parametrize("family", ["neon", "hydrogen"])
def test_off_does_not_enable_thinking(family):
    result, _ = resolve_thinking({}, f"gemini-4-{family}", "off", wire_format="gemini")
    assert result == {}


def test_reserved_indices_and_collision_reallocation(backend):
    reserved = backend["_AG_RESERVED_INDICES"]
    assert {531, 532, 533, 534, 535} <= reserved
    assert 536 not in reserved
    config = {"combos": [{"alias": "GPT-6-Astra"}, {"alias": "Other"}]}
    entries = [{"target": "GPT-6-Astra", "placeholder_index": 533}, {"target": "Other", "placeholder_index": 536}]
    result = backend["_validate_app_custom_models"](config, entries)
    assert result[0]["placeholder_index"] not in reserved | {536}
    assert result[1]["placeholder_index"] == 536
    assert entries[0]["placeholder_index"] == 533


def test_catalog_exact_metadata_and_idempotent_merge(backend):
    quota = {"remainingFraction": 0.75}
    upstream = {"models": {"gemini-4-argon-high": {"quotaInfo": quota}}, "agentModelSorts": [{"groups": [{"modelIds": ["gemini-4-argon-high"]}]}]}
    original = copy.deepcopy(upstream)
    merge = backend["_merge_antigravity_custom_models_catalog"]
    result = merge(upstream, [])
    assert upstream == original
    for level, (index, thinking_level) in NEON.items():
        entry = result["models"][f"gemini-4-neon-{level}"]
        assert entry["model"] == f"MODEL_PLACEHOLDER_M{index}"
        assert entry["thinkingLevel"] == thinking_level
        assert entry["minThinkingBudget"] == 32
        assert entry["maxTokens"] == 1048576
        assert entry["maxOutputTokens"] == 262144
        assert entry["tagTitle"] == "New"
        assert entry["quotaInfo"] == quota
        assert entry["displayName"] == f"Gemini 4 Neon ({'XHigh' if level == 'xhigh' else level.title()})"
    for level in ("low", "medium", "high"):
        assert result["models"][f"gemini-4-carbon-{level}"]["minThinkingBudget"] == 2048
    assert merge(result, []) == result


@pytest.mark.parametrize("level", list(NEON))
def test_integration_slots_and_quota_saver_accept_neon(backend, level):
    model = f"gemini-4-neon-{level}"
    assert model in backend["ANTIGRAVITY_INTEGRATION_SLOTS"]
    result = backend["_validate_antigravity_quota_saver_config"]({}, {"fast_native_model": model, "routine_native_model": model, "fallback_models": []})
    assert result["fast_native_model"] == result["routine_native_model"] == model


@pytest.mark.parametrize("effort", ["xhigh", "max"])
def test_upstream_adapter_top_budget(effort):
    assert _EFFORT_TO_BUDGET[effort] == 65536
    envelope = openai_to_cloudcode_envelope({"messages": [{"role": "user", "content": "hello"}], "reasoning_effort": effort}, "gemini-4-neon")
    assert envelope["request"]["generationConfig"]["thinkingConfig"]["thinkingBudget"] == 65536


@pytest.mark.parametrize("suffix,tier", [("-low", "low"), ("-medium", "medium"), ("-high", "high"), ("-xhigh", "high"), ("", "high")])
def test_neon_plan_fallback(suffix, tier):
    assert _PRO_TIER_ARGON_FALLBACK_MAP[f"gemini-4-neon{suffix}"] == f"gemini-3.8-flash-{tier}"


def test_ui_executable_catalog_and_thinking_parity():
    source = (ROOT / "app/static/app.js").read_text(encoding="utf-8-sig")
    catalog = re.search(r"const OAUTH_PROVIDER_MODELS = (\{.*?\n\});", source, re.S)
    thinking = re.search(r"function getThinkingSpec\(modelId\) \{.*?\n\}", source, re.S)
    assert catalog and thinking
    model_ids = [f"gemini-4-{family}" for family in ("neon", "hydrogen", "argon", "carbon")] + ["gemini-3.8-flash", "gemini-pro-agent"]
    script = "const OAUTH_PROVIDER_MODELS = " + catalog[1] + ";\n" + thinking[0] + "\nconsole.log(JSON.stringify({catalog: OAUTH_PROVIDER_MODELS.antigravity, specs: " + json.dumps(model_ids) + ".map(getThinkingSpec)}));"
    output = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
    data = json.loads(output.stdout)
    assert data["catalog"] == OAUTH_PROVIDER_MODELS["antigravity"]
    assert len({entry["id"] for entry in data["catalog"]}) == len(data["catalog"])
    for i, spec in enumerate(data["specs"]):
        assert spec["effort"] == ["off", "low", "medium", "high", "xhigh" if i < 2 else "max"]
    for family in ("neon", "hydrogen"):
        assert {f"gemini-4-{family}{suffix}" for suffix in ("", "-low", "-medium", "-high", "-xhigh")} <= {m["id"] for m in data["catalog"]}
    for options in ("fastOpts", "routineOpts"):
        block = re.search(rf"const {options} = \[(.*?)\]\.map", source, re.S)
        assert block
        for level in NEON:
            assert f"gemini-4-neon-{level}" in block[1]
    for level in NEON:
        assert "key:'gemini-4-neon-" + level + "'" in source


def test_persisted_neon_defaults_and_astra_index():
    cfg_path = ROOT / "config.yaml"
    if not cfg_path.is_file():
        pytest.skip("local config.yaml not present")
    config = yaml.safe_load(cfg_path.read_bytes()) or {}
    ag = (config.get("providers") or {}).get("antigravity")
    if not isinstance(ag, dict) or not ag.get("models"):
        pytest.skip("local antigravity provider config not present (fresh/CI checkout)")
    models = {m["id"]: m for m in ag["models"]}
    if "gemini-4-neon" not in models:
        pytest.skip("gemini-4-neon not configured in local config.yaml")
    assert models["gemini-4-neon"]["thinking"] == "high"
    for level in NEON:
        assert models[f"gemini-4-neon-{level}"]["thinking"] == level
    custom = (config.get("antigravity_integration") or {}).get("custom_models") or []
    astra = next((m for m in custom if m.get("slug") == "custom-gpt-6-astra"), None)
    if astra is None:
        pytest.skip("custom-gpt-6-astra not configured in local config.yaml")
    assert astra["placeholder_index"] == 536
    assert sum(m.get("placeholder_index") == 536 for m in custom) == 1
