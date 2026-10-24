"""Tests for 7-Family Authenticity Tier-Splitter, Two-Stage Intra-Family
Matcher, Model Catalog Sync, and Probe Middleware Isolation (2026-10-24).
"""
import asyncio
import json
import random
from pathlib import Path

import httpx
import pytest

from app.authenticity import standard_v2 as v2
from app.authenticity.baseline import BaselineStore
from app.authenticity.fingerprint import FingerprintFeatures
from app.authenticity.judge_rank import DECISIVE_LANES, FEATURE_WEIGHTS
from app.authenticity.matcher import match_submodel
from app.authenticity.scope import (
    MODEL_TIER_EXPECTATIONS,
    TRUSTED_ANCHORS,
    VARIANT_NOTES,
    detect_family,
    get_model_tier_rank,
    in_scope,
)
from scripts.backfill_v2_lane import LANES

STANDARDS_DIR = (
    Path(__file__).resolve().parents[1] / "authenticity" / "reference" / "standards"
)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ------------------------------------------------------------------ Part 1: Scope & Tier Ranks


@pytest.mark.parametrize(
    "model_id,expected_family,expected_band",
    [
        # Claude
        ("claude-haiku-4-5", "claude", (0.28, 0.42)),
        ("claude-sonnet-4-6", "claude", (0.55, 0.75)),
        ("claude-sonnet-5-5", "claude", (0.55, 0.75)),
        ("claude-opus-5-5", "claude", (0.85, 0.99)),
        ("claude-fable-5-1", "claude", (0.85, 0.99)),
        # GPT
        ("gpt-5.6-luna", "gpt", (0.30, 0.45)),
        ("gpt-6-luna", "gpt", (0.30, 0.45)),
        ("gpt-5.6-terra", "gpt", (0.55, 0.76)),
        ("gpt-5.6-sol", "gpt", (0.55, 0.76)),
        ("gpt-6.1-sol", "gpt", (0.55, 0.76)),
        ("gpt-6-astra", "gpt", (0.85, 0.99)),
        # Qwen
        ("qwen3.7-flash", "qwen", (0.30, 0.45)),
        ("qwen3.7-plus", "qwen", (0.55, 0.75)),
        ("qwen3.7-max", "qwen", (0.80, 0.99)),
        ("qwen3.8-flash", "qwen", (0.30, 0.45)),
        ("qwen3.8-plus", "qwen", (0.55, 0.75)),
        ("qwen3.8-max", "qwen", (0.85, 0.99)),
        ("qwen3.8-max-0902", "qwen", (0.90, 0.99)),
        # GLM
        ("glm-5.3-flash", "glm", (0.30, 0.45)),
        ("glm-5.2", "glm", (0.55, 0.75)),
        ("glm-5.3", "glm", (0.84, 0.99)),
        # DeepSeek
        ("deepseek-v4-flash", "deepseek", (0.30, 0.45)),
        ("deepseek-v4.1-flash", "deepseek", (0.30, 0.45)),
        ("deepseek-v4-pro", "deepseek", (0.85, 0.99)),
        # Kimi
        ("kimi-k2.6", "kimi", (0.30, 0.45)),
        ("kimi-k2.7-code", "kimi", (0.55, 0.75)),
        ("kimi-k3", "kimi", (0.85, 0.99)),
        # Gemini
        ("gemini-3.6-flash", "gemini", (0.30, 0.45)),
        ("gemini-3.7-flash", "gemini", (0.30, 0.45)),
        ("gemini-3.8-flash", "gemini", (0.30, 0.45)),
        ("gemini-3.1-pro", "gemini", (0.55, 0.75)),
        ("gemini-4-argon", "gemini", (0.85, 0.99)),
    ],
)
def test_scope_and_tier_rank_all_7_families(model_id, expected_family, expected_band):
    assert detect_family(model_id) == expected_family
    sc = in_scope(model_id)
    assert sc["verdict_scope"] == "full"
    assert sc["family"] == expected_family
    rank = get_model_tier_rank(model_id)
    assert rank is not None, f"missing tier rank for {model_id}"
    lo, hi = expected_band
    assert lo <= rank <= hi, f"{model_id} rank {rank} outside {expected_band}"


def test_trusted_anchors_and_variant_notes_cover_7_families():
    seven = {"claude", "gemini", "gpt", "glm", "kimi", "deepseek", "qwen"}
    assert seven.issubset(set(TRUSTED_ANCHORS.keys()))
    assert seven.issubset(set(VARIANT_NOTES.keys()))
    assert "chat2api/zai-web1" in TRUSTED_ANCHORS["glm"]["reference"]
    assert "chat2api/workbuddy-web" in TRUSTED_ANCHORS["kimi"]["reference"]
    assert "chat2api/deepseek-web" in TRUSTED_ANCHORS["deepseek"]["reference"]
    assert "chat2api/qwen-web" in TRUSTED_ANCHORS["qwen"]["reference"]
    assert "chat2api/claude-web" in TRUSTED_ANCHORS["claude"]["reference"]
    assert "grok" not in TRUSTED_ANCHORS


# ------------------------------------------------------------------ Part 3: 3-Prong Scorers & Runner


def _perfect_svg_from_spec(spec: dict) -> str:
    fx, fy, fw, fh = spec["fx"], spec["fy"], spec["fw"], spec["fh"]
    cx, cy, hr = spec["cx"], spec["cy"], spec["hr"]
    tok = spec["token"]
    pts_l = f"{cx - 25},{cy - 10} {cx - 45},{cy} {cx - 25},{cy + 10}"
    pts_r = f"{cx + 25},{cy - 10} {cx + 45},{cy} {cx + 25},{cy + 10}"
    nx2, ny2 = cx + 12, cy - 12
    ty = cy + hr + 16
    return (
        f'<svg viewBox="0 0 240 240" xmlns="http://www.w3.org/2000/svg">'
        f'<rect id="frame" x="{fx}" y="{fy}" width="{fw}" height="{fh}"/>'
        f'<circle id="hub" cx="{cx}" cy="{cy}" r="{hr}"/>'
        f'<polygon id="fin_l" points="{pts_l}"/>'
        f'<polygon id="fin_r" points="{pts_r}"/>'
        f'<line id="needle" x1="{cx}" y1="{cy}" x2="{nx2}" y2="{ny2}"/>'
        f'<text id="tag" x="{cx}" y="{ty}">{tok}</text>'
        f"</svg>"
    )


def test_score_svg_compiler_deterministic():
    rng = random.Random(42)
    prompt, spec = v2._build_svg_compiler_prompt(rng)
    assert "viewBox" in prompt and spec["token"] in prompt
    perfect = _perfect_svg_from_spec(spec)
    assert v2.score_svg_compiler(perfect, spec) == 1.0
    # Break reflection symmetry + needle direction -> lower score
    broken = perfect.replace(f'x2="{spec["cx"] + 12}"', f'x2="{spec["cx"] - 12}"')
    assert 0.5 <= v2.score_svg_compiler(broken, spec) < 1.0
    assert v2.score_svg_compiler("not svg at all", spec) == 0.0


def test_score_constraint_sat_deterministic():
    rng = random.Random(99)
    prompt, spec = v2._build_constraint_sat_prompt(rng)
    assert "grid" in prompt
    witness_json = json.dumps({"grid": spec["witness"]})
    assert v2.score_constraint_sat(witness_json, spec) == 1.0
    # Break modulo and row sum on row 0
    bad_grid = [list(r) for r in spec["witness"]]
    bad_grid[0][1] += 1
    assert 0.5 <= v2.score_constraint_sat(json.dumps({"grid": bad_grid}), spec) < 1.0
    assert v2.score_constraint_sat("{}", spec) == 0.0


def test_score_state_trace_deterministic():
    rng = random.Random(123)
    prompt, spec = v2._build_state_trace_prompt(rng)
    assert "MID:" in prompt and "FINAL:" in prompt
    perfect = f"Step trace...\nMID: {spec['mid']}\nFINAL: {spec['final']}"
    assert v2.score_state_trace(perfect, spec) == 1.0
    half = f"MID: {spec['mid']}\nFINAL: [0, 0, 0, 0, 0]"
    assert v2.score_state_trace(half, spec) >= 0.4
    assert v2.score_state_trace("no trace", spec) == 0.0


def test_run_tier_splitter_and_similarity(monkeypatch):
    monkeypatch.setattr(v2, "_MIN_INTERVAL", 0.0)
    rng = random.Random(7)
    specs_by_id = {}

    # Capture generated specs by wrapping the builders
    orig_prongs = v2.TIER_SPLITTER_PRONGS
    wrapped = []
    for name, builder, scorer in orig_prongs:
        def _make_wrap(nm, bfn):
            def _w(r):
                p, sp = bfn(r)
                specs_by_id[nm] = sp
                return p, sp
            return _w
        wrapped.append((name, _make_wrap(name, builder), scorer))
    monkeypatch.setattr(v2, "TIER_SPLITTER_PRONGS", tuple(wrapped))

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        prompt = body["messages"][-1]["content"]
        if "<svg" in prompt:
            txt = _perfect_svg_from_spec(specs_by_id["svg_compiler"])
        elif '"grid"' in prompt:
            txt = json.dumps({"grid": specs_by_id["constraint_sat"]["witness"]})
        else:
            sp = specs_by_id["state_trace"]
            txt = f"MID: {sp['mid']}\nFINAL: {sp['final']}"
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": txt}, "finish_reason": "stop"}]},
        )

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await v2.run_tier_splitter(c, "http://x", "k", "m", k=1, rng=rng, gaps=())

    res = _run(go())
    assert res["composite"] == 1.0
    assert set(res["tiers"]) == {"svg_compiler", "constraint_sat", "state_trace"}
    assert v2.similarity_tier_splitter(res, res) == 1.0

    # Compare flagship (1.0) against budget (0.35) -> sharp separation
    weak = {
        "tiers": {
            "svg_compiler": {"acc": 0.35, "n": 2},
            "constraint_sat": {"acc": 0.35, "n": 2},
            "state_trace": {"acc": 0.35, "n": 2},
        },
        "composite": 0.35,
        "k": 2,
    }
    sim = v2.similarity_tier_splitter(res, weak)
    assert sim is not None and sim < 0.25

    # Fallback when candidate standard lacks tier_splitter block
    sim_flagship = v2.similarity_tier_splitter(res, None, candidate_model_id="qwen3.8-max")
    sim_flash = v2.similarity_tier_splitter(res, None, candidate_model_id="qwen3.8-flash")
    assert sim_flagship > 0.85
    assert sim_flash < 0.45


def test_tier_splitter_registered_in_weights_and_backfiller():
    assert FEATURE_WEIGHTS["tier_splitter"] == 0.12
    assert "tier_splitter" in DECISIVE_LANES
    assert LANES["tier_splitter"] is v2.run_tier_splitter


# ------------------------------------------------------------------ Part 4: Stage-2 Intra-Family Disambiguation


@pytest.mark.parametrize(
    "family,cheap_id,flagship_id",
    [
        ("claude", "claude-sonnet-5-5", "claude-opus-5-5"),
        ("gpt", "gpt-6-luna", "gpt-6-astra"),
        ("qwen", "qwen3-8-flash", "qwen3-8-max"),
        ("glm", "glm-5-3-flash", "glm-5-3"),
        ("deepseek", "deepseek-v4-flash", "deepseek-v4-pro"),
        ("kimi", "kimi-k2-6", "kimi-k3"),
        ("gemini", "gemini-3-8-flash", "gemini-4-argon"),
    ],
)
def test_two_stage_matcher_disambiguates_all_7_families(family, cheap_id, flagship_id):
    store = BaselineStore(STANDARDS_DIR.parent / "baselines")
    pool = store.load_family_standards(STANDARDS_DIR, family)
    assert cheap_id in pool and flagship_id in pool

    # 1. Suspect shaped like flagship -> matches flagship decisively (abstain=False)
    flag_std = pool[flagship_id]
    f_flag = FingerprintFeatures(
        claimed_model=flagship_id,
        cutoff_year=(flag_std.get("knowledge") or {}).get("cutoff_year") or 0,
        cutoff_month=(flag_std.get("knowledge") or {}).get("cutoff_month") or 0,
        capability_correct=3,
    )
    f_flag.v2_profile = json.loads(json.dumps(v2.v2_of(flag_std)))
    mr_flag = match_submodel(f_flag, {cheap_id: pool[cheap_id], flagship_id: flag_std})
    assert mr_flag.best_model == flagship_id
    assert mr_flag.abstain is False
    assert mr_flag.matched_model == flagship_id
    assert (mr_flag.match_score - mr_flag.runner_up_score) >= 0.05

    # 2. Suspect shaped like cheap tier -> matches cheap tier decisively (detects downgrade!)
    cheap_std = pool[cheap_id]
    f_cheap = FingerprintFeatures(
        claimed_model=flagship_id,
        cutoff_year=(cheap_std.get("knowledge") or {}).get("cutoff_year") or 0,
        cutoff_month=(cheap_std.get("knowledge") or {}).get("cutoff_month") or 0,
        capability_correct=3,
    )
    f_cheap.v2_profile = json.loads(json.dumps(v2.v2_of(cheap_std)))
    mr_cheap = match_submodel(f_cheap, {cheap_id: cheap_std, flagship_id: flag_std})
    assert mr_cheap.best_model == cheap_id
    assert mr_cheap.abstain is False
    assert mr_cheap.matched_model == cheap_id


def test_stage2_tiebreak_activates_when_stage1_saturated():
    """Even when two siblings agree on every family-level axis so Stage-1's
    total gap is < 0.05, Stage-2 breaks the tie via tier_splitter."""
    base_common = json.loads((STANDARDS_DIR / "qwen_qwen3-8-max.json").read_text(encoding="utf-8"))
    sib_plus = json.loads(json.dumps(base_common))
    sib_max = json.loads(json.dumps(base_common))
    sib_plus["structural"]["tokenizer_signature"]["v2"]["tier_splitter"] = {
        "tiers": {"svg_compiler": {"acc": 0.83, "n": 2}, "constraint_sat": {"acc": 0.83, "n": 2}, "state_trace": {"acc": 0.83, "n": 2}},
        "composite": 0.83, "k": 2,
    }
    sib_max["structural"]["tokenizer_signature"]["v2"]["tier_splitter"] = {
        "tiers": {"svg_compiler": {"acc": 0.94, "n": 2}, "constraint_sat": {"acc": 0.94, "n": 2}, "state_trace": {"acc": 0.94, "n": 2}},
        "composite": 0.94, "k": 2,
    }

    f = FingerprintFeatures(cutoff_year=2025, cutoff_month=6, capability_correct=3)
    f.noncoding = {"judge": {"creative": {"score": 8}, "eq": {"score": 8}, "humor": {"score": 8}, "design": {"score": 8}}}
    f.refusal_signature = {"delta": (base_common.get("refusal") or {}).get("delta_signature") or {}}
    f.boundary_signature = dict(((base_common.get("structural") or {}).get("tokenizer_signature") or {}).get("boundary_signature") or {})
    f.v2_profile = json.loads(json.dumps(sib_max["structural"]["tokenizer_signature"]["v2"]))

    mr = match_submodel(f, {"qwen3-8-plus": sib_plus, "qwen3-8-max": sib_max})
    assert mr.best_model == "qwen3-8-max"
    assert mr.abstain is False
    assert "_stage2_tiebreak" in mr.dimensions
    assert mr.dimensions["_stage2_tiebreak"]["winner"] == "qwen3-8-max"


# ------------------------------------------------------------------ Part 5: Standards & Pairwise Separability


def test_gemini_4_argon_standard_repaired():
    argon = json.loads((STANDARDS_DIR / "gemini_gemini-4-argon.json").read_text(encoding="utf-8"))
    v2_block = v2.v2_of(argon)
    assert v2_block["hard_series"]["math"]["accuracy_by_tier"] == [1.0, 1.0, 1.0]
    assert v2_block["tier_splitter"]["composite"] >= 0.90
    kb_vals = [vals[0] for vals in v2_block["knowledge_bank"]["answers"].values() if vals]
    assert any(v != "unknown" for v in kb_vals)


def test_list_testable_standards_includes_reference_and_canonical():
    from app.authenticity.harvest import list_testable_standards
    res = list_testable_standards(STANDARDS_DIR)
    assert res["count_full"] >= 40
    all_models = {
        m["model"]
        for entries in res["vendors"].values()
        for m in entries
    }
    for expected in ("qwen3.7-flash", "k2.7-code", "5.3-flash", "4-argon"):
        assert expected in all_models, (expected, all_models)


@pytest.mark.parametrize(
    "family,tier_a,tier_b",
    [
        ("claude", "claude-haiku-4-5", "claude-sonnet-5-5"),
        ("claude", "claude-sonnet-5-5", "claude-opus-5-5"),
        ("gpt", "gpt-6-luna", "gpt-6-1-sol"),
        ("gpt", "gpt-6-1-sol", "gpt-6-astra"),
        ("qwen", "qwen3-7-flash", "qwen3-7-plus"),
        ("qwen", "qwen3-7-plus", "qwen3-7-max"),
        ("qwen", "qwen3-8-flash", "qwen3-8-plus"),
        ("qwen", "qwen3-8-plus", "qwen3-8-max"),
        ("glm", "glm-5-3-flash", "glm-5-2"),
        ("glm", "glm-5-2", "glm-5-3"),
        ("deepseek", "deepseek-v4-flash", "deepseek-v4-pro"),
        ("deepseek", "deepseek-v4-1-flash", "deepseek-v4-pro"),
        ("kimi", "kimi-k2-6", "kimi-k2-7-code"),
        ("kimi", "kimi-k2-7-code", "kimi-k3"),
        ("gemini", "gemini-3-8-flash", "gemini-3-1-pro"),
        ("gemini", "gemini-3-1-pro", "gemini-4-argon"),
    ],
)
def test_pairwise_separability_across_all_7_families(family, tier_a, tier_b):
    store = BaselineStore(STANDARDS_DIR.parent / "baselines")
    pool = store.load_family_standards(STANDARDS_DIR, family)
    sep = v2.pairwise_separability(pool[tier_a], pool[tier_b])
    assert sep["separable"] is True
    assert sep["distance"] >= 0.15
