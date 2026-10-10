"""Lane policy: the standard lane for each family is pinned and cannot be missed.

glm -> aibox/ZHIPU/   kimi -> aibox/kimi/   deepseek -> aibox/ds/
(user directive 2026-10-08). Born from: mintrouter/glm-5.3 being glm-5.3-FLASH,
aibox/glm-5.x / aibox/kimi-k3 being non-standard lanes, and the router being
case-sensitive (aibox/zhipu/glm-5.3 -> 503; the listed id is aibox/ZHIPU/GLM-5.3).
"""
import importlib.util
import sys
from pathlib import Path

import pytest

from app.authenticity import lanes as L
from app.authenticity.scope import TRUSTED_ANCHORS

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("lane", [
    "aibox/ZHIPU/GLM-5.3", "aibox/ZHIPU/GLM-5.2", "aibox/zhipu/glm-5.3",
    "aibox/kimi/kimi-k3", "aibox/ds/deepseek-v4-pro", "aibox/qwen3.8-max",
    "codex/gpt-6-luna", "mintrouter/claude-opus-5-5", "antigravity/gemini-3.7-flash",
])
def test_standard_lanes_accepted(lane):
    assert L.lane_violation(lane) is None, L.lane_violation(lane)


@pytest.mark.parametrize("lane", [
    "mintrouter/glm-5.3",        # actually glm-5.3-flash
    "aibox/glm-5.3", "aibox/glm-5.2", "aibox/glm-5.3-prime",
    "aibox/kimi-k3", "mintrouter/kimi-k3",
    "aibox/deepseek-v4-pro", "mintrouter/deepseek-v4-pro",
    "vsllm-o/glm-5.3", "glm-5.3", "",
])
def test_non_standard_lanes_refused(lane):
    assert L.lane_violation(lane), f"{lane!r} must be refused"


def test_known_bad_reason_mentions_flash():
    assert "FLASH" in L.lane_violation("mintrouter/glm-5.3")


def test_resolve_routable_maps_case():
    routable = ["aibox/ZHIPU/GLM-5.3", "aibox/glm-5.3", "aibox/kimi/kimi-k3"]
    assert L.resolve_routable("aibox/zhipu/glm-5.3", routable) == "aibox/ZHIPU/GLM-5.3"
    assert L.resolve_routable("aibox/ds/deepseek-v4-pro", routable) is None


def test_canonical_anchors_all_legal():
    for name, lane in L.CANONICAL_ANCHORS.items():
        assert L.lane_violation(lane) is None, (name, lane)
    assert len(L.CANONICAL_ANCHORS) == 19


def test_deepseek_flash_exact_lane_only():
    """aibox/deepseek-v4.1-flash is canonical (user directive 2026-10-09); the
    exception is exact-id only and does not open aibox/deepseek-v4-pro."""
    assert L.lane_violation("aibox/deepseek-v4.1-flash") is None
    assert L.lane_violation("AIBOX/DeepSeek-V4.1-Flash") is None
    assert L.lane_violation("aibox/deepseek-v4-pro")
    assert L.CANONICAL_ANCHORS["deepseek-v4.1-flash"] == "aibox/deepseek-v4.1-flash"


def test_scope_gold_prefixes_agree_with_lane_policy():
    """scope.TRUSTED_ANCHORS gold prefixes and lanes.FAMILY_LANE_PREFIXES must
    never drift apart -- they describe the same fact."""
    for fam, spec in TRUSTED_ANCHORS.items():
        for gold in spec.get("gold", []):
            assert any((gold.lower() + "/").startswith(p) or gold.lower().startswith(p)
                       for p in L.FAMILY_LANE_PREFIXES[fam]), (fam, gold)


def _load_driver():
    spec = importlib.util.spec_from_file_location(
        "rebuild_thin_standards", ROOT / "scripts" / "rebuild_thin_standards.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_driver_anchors_match_registry_and_policy():
    drv = _load_driver()
    problems, resolved = drv._lane_problems(drv.MODELS, None)
    assert problems == []
    assert set(drv.MODELS) == set(L.CANONICAL_ANCHORS)
    assert drv.MODELS["glm-5.3"][0] == "aibox/ZHIPU/GLM-5.3"
    assert drv.MODELS["glm-5.2"][0] == "aibox/ZHIPU/GLM-5.2"
    assert drv.MODELS["kimi-k3"][0] == "aibox/kimi/kimi-k3"
    assert drv.MODELS["deepseek-v4-pro"][0] == "aibox/ds/deepseek-v4-pro"


def test_driver_refuses_unroutable_and_resolves_case():
    drv = _load_driver()
    only = {"glm-5.3": drv.MODELS["glm-5.3"]}
    problems, resolved = drv._lane_problems(only, ["aibox/ZHIPU/GLM-5.3"])
    assert problems == [] and resolved["glm-5.3"] == "aibox/ZHIPU/GLM-5.3"
    problems, _ = drv._lane_problems(only, ["aibox/glm-5.3"])
    assert problems and "not routable" in problems[0]


def test_harvest_endpoint_refuses_wrong_lane(tmp_path, monkeypatch):
    import app.main as main
    from fastapi.testclient import TestClient
    monkeypatch.setattr(main.auth_harvest, "STANDARDS_DIR", tmp_path)
    with TestClient(main.app) as client:
        for lane in ("mintrouter/glm-5.3", "aibox/glm-5.3", "aibox/kimi-k3",
                     "mintrouter/kimi-k3"):
            r = client.post("/api/authenticity/standards/harvest", json={
                "model_id": lane, "endpoint": "https://api.example.com",
                "api_key": "sk-test", "include_noncoding": False})
            assert r.status_code == 400, lane
            assert r.json().get("refused") is True


def test_anchor_lane_recorded_in_meta():
    from app.authenticity.traitmark import MetaLayer
    m = MetaLayer(anchor_lane="aibox/ZHIPU/GLM-5.3")
    assert m.anchor_lane == "aibox/ZHIPU/GLM-5.3"


def test_kimi_roles_moonshot_canonical_alibaba_double():
    """aibox/kimi/kimi-k3 = Moonshot canonical; aibox/kimi-k3 = Alibaba double
    reference. Never swapped, never mixed (user directive 2026-10-08)."""
    assert L.lane_violation("aibox/kimi/kimi-k3") is None
    why = L.lane_violation("aibox/kimi-k3")
    assert why and "DOUBLE-REFERENCE" in why and "aibox/kimi/" in why
    assert L.is_double_reference("aibox/kimi-k3")
    assert L.is_double_reference("AIBOX/Kimi-K3")
    assert not L.is_double_reference("aibox/kimi/kimi-k3")
    assert L.CANONICAL_ANCHORS["kimi-k3"] == "aibox/kimi/kimi-k3"


def test_driver_double_mode_validates_and_tags():
    drv = _load_driver()
    problems, resolved = drv._lane_problems(drv.DOUBLES, None, double=True)
    assert problems == [] and resolved["kimi-k3"] == "aibox/kimi-k3"
    # the canonical lane is NOT a valid double, and a double is not a valid canonical
    bad = {"kimi-k3": ("aibox/kimi/kimi-k3", "j")}
    assert drv._lane_problems(bad, None, double=True)[0]
    assert drv._lane_problems(drv.DOUBLES, None, double=False)[0]
    assert drv.DOUBLE_TAG


def test_cascade_lane_map_kimi_uses_moonshot_lane():
    spec = importlib.util.spec_from_file_location(
        "cascade_augment_standards", ROOT / "scripts" / "cascade_augment_standards.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    assert mod.LANE_MAP["kimi_kimi-k3.json"] == "aibox/kimi/kimi-k3"
    for fname, lane in mod.LANE_MAP.items():
        if fname.startswith(("kimi_", "glm_", "deepseek_")):
            assert L.lane_violation(lane) is None, (fname, lane)

