"""spatial_curve v2 lane (2026-10-19) -- offline tests.

Covers: lane payload shape identical to capability_curve (so similarity_curve
is reused unchanged), programmatically computed ground truth for all three
tiers, the judge_rank weight rebalance (sum stays exactly 1.0), the
pairwise_separability tie-break on synthetic siblings, and the backfiller
LANES registration + skip-if-present semantics (runner monkeypatched, no
network).
"""
import asyncio
import json
import random
import re

import httpx

from app.authenticity import standard_v2 as v2
from app.authenticity.judge_rank import DECISIVE_LANES, FEATURE_WEIGHTS
from scripts.backfill_v2_lane import LANES


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ------------------------------------------------------------------ weights

def test_feature_weights_sum_to_one_after_spatial_rebalance():
    assert round(sum(FEATURE_WEIGHTS.values()), 6) == 1.0


def test_spatial_curve_weight_and_decisive_lane_registered():
    assert FEATURE_WEIGHTS["spatial_curve"] == 0.10
    assert "spatial_curve" in DECISIVE_LANES


# ------------------------------------------------------------- tier ground truth

def test_spatial_tiers_truth_is_computed_not_llm():
    """Each generator must return (prompt, answer) with the answer derivable
    from the prompt by an independent re-computation."""
    rng = random.Random(1234)
    for _ in range(40):
        q, a = v2._sp_point_in_box(rng)
        m = re.search(r"corner mins \[([^\]]+)\] and corner maxes \[([^\]]+)\]", q)
        lo = [int(x) for x in m.group(1).split(",")]
        hi = [int(x) for x in m.group(2).split(",")]
        p = [int(x) for x in re.search(r"point \[([^\]]+)\]", q).group(1).split(",")]
        want = "yes" if all(lo[i] <= p[i] <= hi[i] for i in range(3)) else "no"
        assert a == want, (q, a, want)

    for _ in range(40):
        q, a = v2._sp_box_volume(rng)
        boxes = re.findall(r"corner mins \[([^\]]+)\] and maxes \[([^\]]+)\]", q)
        assert len(boxes) == 2
        alo = [int(x) for x in boxes[0][0].split(",")]
        ahi = [int(x) for x in boxes[0][1].split(",")]
        blo = [int(x) for x in boxes[1][0].split(",")]
        bhi = [int(x) for x in boxes[1][1].split(",")]
        va = (ahi[0] - alo[0]) * (ahi[1] - alo[1]) * (ahi[2] - alo[2])
        vb = (bhi[0] - blo[0]) * (bhi[1] - blo[1]) * (bhi[2] - blo[2])
        inter = 1
        for i in range(3):
            inter *= max(0, min(ahi[i], bhi[i]) - max(alo[i], blo[i]))
        want = str(va + vb - inter) if "UNION" in q else str(va - inter)
        assert a == want, (q, a, want)

    for _ in range(60):
        q, a = v2._sp_ray_occlusion(rng)
        d = [float(x) for x in re.search(r"unit direction \[([^\]]+)\]", q)
             .group(1).split(",")]
        center = [float(x) for x in re.search(r"centered at \[([^\]]+)\]", q)
                  .group(1).split(",")]
        deg = float(re.search(r"rotated ([\d.]+) degrees", q).group(1))
        t1 = float(re.search(r"t=([\d.]+)", q).group(1))
        import math as _m
        th = _m.radians(deg)
        c, s = _m.cos(th), _m.sin(th)
        op = [-(c * center[0] + s * center[2]), -center[1],
              -(-s * center[0] + c * center[2])]
        dp = [c * d[0] + s * d[2], d[1], -s * d[0] + c * d[2]]
        tn, tf = -_m.inf, _m.inf
        for i in range(3):
            if abs(dp[i]) < 1e-9:
                if not (-0.5 <= op[i] <= 0.5):
                    tn, tf = 1.0, 0.0
                    break
                continue
            x, y = (-0.5 - op[i]) / dp[i], (0.5 - op[i]) / dp[i]
            lo2, hi2 = (x, y) if x <= y else (y, x)
            tn, tf = max(tn, lo2), min(tf, hi2)
        hit = tn <= tf and tf >= 0.0 and tn < t1
        want = "yes" if hit else "no"
        assert a == want, (q, a, want, tn, tf, t1)


def test_spatial_tiers_fresh_instances():
    p1, _ = v2.build_capability_prompt(v2._sp_point_in_box, random.Random(1))
    p2, _ = v2.build_capability_prompt(v2._sp_point_in_box, random.Random(2))
    assert p1 != p2


# ------------------------------------------------------------ runner payload

def _oracle_handler(req: httpx.Request) -> httpx.Response:
    """Perfect-solver mock: re-derives every answer from the prompt text."""
    import math as _m

    body = json.loads(req.content)
    prompt = body["messages"][-1]["content"]
    out = []
    for m in re.finditer(r"Q(\d+): (.+)", prompt):
        n, q = int(m.group(1)), m.group(2)
        if "axis-aligned box has corner mins" in q:
            boxes = re.findall(r"mins \[([^\]]+)\] and corner maxes \[([^\]]+)\]", q)
            lo = [int(x) for x in boxes[0][0].split(",")]
            hi = [int(x) for x in boxes[0][1].split(",")]
            p = [int(x) for x in re.search(r"point \[([^\]]+)\]", q).group(1).split(",")]
            ans = "yes" if all(lo[i] <= p[i] <= hi[i] for i in range(3)) else "no"
        elif "unit direction" in q:
            d = [float(x) for x in re.search(r"unit direction \[([^\]]+)\]", q)
                 .group(1).split(",")]
            center = [float(x) for x in re.search(r"centered at \[([^\]]+)\]", q)
                      .group(1).split(",")]
            deg = float(re.search(r"rotated ([\d.]+) degrees", q).group(1))
            t1 = float(re.search(r"t=([\d.]+)", q).group(1))
            th = _m.radians(deg)
            c, s = _m.cos(th), _m.sin(th)
            op = [-(c * center[0] + s * center[2]), -center[1],
                  -(-s * center[0] + c * center[2])]
            dp = [c * d[0] + s * d[2], d[1], -s * d[0] + c * d[2]]
            tn, tf = -_m.inf, _m.inf
            for i in range(3):
                if abs(dp[i]) < 1e-9:
                    if not (-0.5 <= op[i] <= 0.5):
                        tn, tf = 1.0, 0.0
                        break
                    continue
                x, y = (-0.5 - op[i]) / dp[i], (0.5 - op[i]) / dp[i]
                lo2, hi2 = (x, y) if x <= y else (y, x)
                tn, tf = max(tn, lo2), min(tf, hi2)
            ans = "yes" if (tn <= tf and tf >= 0.0 and tn < t1) else "no"
        else:
            boxes = re.findall(r"mins \[([^\]]+)\] and maxes \[([^\]]+)\]", q)
            alo = [int(x) for x in boxes[0][0].split(",")]
            ahi = [int(x) for x in boxes[0][1].split(",")]
            blo = [int(x) for x in boxes[1][0].split(",")]
            bhi = [int(x) for x in boxes[1][1].split(",")]
            va = (ahi[0] - alo[0]) * (ahi[1] - alo[1]) * (ahi[2] - alo[2])
            vb = (bhi[0] - blo[0]) * (bhi[1] - blo[1]) * (bhi[2] - blo[2])
            inter = 1
            for i in range(3):
                inter *= max(0, min(ahi[i], bhi[i]) - max(alo[i], blo[i]))
            ans = str(va + vb - inter) if "UNION" in q else str(va - inter)
        out.append(f"Q{n}: {ans}")
    return httpx.Response(200, json={"choices": [{"message": {"content": "\n".join(out)},
                                                  "finish_reason": "stop"}]})


def test_run_spatial_curve_payload_shape_matches_capability_curve(monkeypatch):
    monkeypatch.setattr(v2, "_MIN_INTERVAL", 0.0)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(_oracle_handler)) as c:
            return await v2.run_spatial_curve(c, "http://x", "k", "m", k=2,
                                              rng=random.Random(5), gaps=())
    res = _run(go())
    assert set(res) == {"tiers", "k"}
    assert res["k"] == 2
    assert set(res["tiers"]) == {n for n, _ in v2.SPATIAL_TIERS}
    for t in res["tiers"].values():
        assert set(t) == {"acc", "n"}
        assert t["acc"] == 1.0 and t["n"] == 2 * v2.QUESTIONS_PER_PROMPT
    # similarity_curve must accept it unchanged (identical shape); the
    # spatial lane has 3 tiers, so it uses min_shared=3.
    assert v2.similarity_curve(res, res, min_shared=3) == 1.0


def test_run_spatial_curve_never_raises_on_dead_transport(monkeypatch):
    monkeypatch.setattr(v2, "_MIN_INTERVAL", 0.0)

    def dead(req):
        return httpx.Response(500, json={})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(dead)) as c:
            return await v2.run_spatial_curve(c, "http://x", "k", "m", k=1,
                                              rng=random.Random(1), gaps=())
    assert _run(go()) == {}


# ------------------------------------------- separability tie-break (acceptance)

def _std(spatial_tiers):
    """Synthetic standard dict whose v2 block carries ONLY the given spatial
    tiers (plus identical filler lanes handled by the caller)."""
    return {"structural": {"tokenizer_signature": {"v2": {
        "spatial_curve": {"tiers": spatial_tiers, "k": 2}}}}}


def test_pairwise_separability_breaks_tie_via_spatial_curve():
    """Sibling pair identical on every existing lane but separated on
    spatial_curve tiers: distance must rise from <0.12 to >=0.12."""
    strong = {n: {"acc": 1.0, "n": 6} for n, _ in v2.SPATIAL_TIERS}
    weak = {n: {"acc": 0.0, "n": 6} for n, _ in v2.SPATIAL_TIERS}
    a = _std(strong)
    b = _std(weak)

    # Baseline: strip spatial_curve from both -> the pair must NOT separate.
    a_ns = json.loads(json.dumps(a))
    b_ns = json.loads(json.dumps(b))
    a_ns["structural"]["tokenizer_signature"]["v2"].pop("spatial_curve")
    b_ns["structural"]["tokenizer_signature"]["v2"].pop("spatial_curve")
    sep_none = v2.pairwise_separability(a_ns, b_ns)
    assert sep_none["distance"] is None or sep_none["distance"] < v2.SEPARABLE_DISTANCE

    sep = v2.pairwise_separability(a, b)
    assert sep["distance"] is not None and sep["distance"] >= v2.SEPARABLE_DISTANCE, sep
    assert sep["separable"] is True
    assert sep["sims"]["spatial_curve"] < 1.0


# ------------------------------------------------------- backfiller registration

def test_backfiller_lanes_contains_spatial_curve():
    assert "spatial_curve" in LANES
    assert LANES["spatial_curve"] is v2.run_spatial_curve


def test_backfiller_skip_if_present(monkeypatch, tmp_path):
    """--lane-name spatial_curve on a standard file that already carries the
    lane must SKIP without invoking the runner (monkeypatched, no network)."""
    import scripts.backfill_v2_lane as bf

    std = {"structural": {"tokenizer_signature": {"v2": {
        "spatial_curve": {"tiers": {"point_in_box": {"acc": 1.0, "n": 6}}, "k": 2}}}}}
    f = tmp_path / "std.json"
    f.write_text(json.dumps(std), encoding="utf-8")
    monkeypatch.setattr(bf, "STANDARDS_DIR", tmp_path)

    called = []

    async def fake_runner(*a, **k):
        called.append((a, k))
        return {"tiers": {"point_in_box": {"acc": 0.5, "n": 6}}, "k": 2}

    monkeypatch.setitem(bf.LANES, "spatial_curve", fake_runner)
    rc = _run(bf._run("std.json", "lane-id", "spatial_curve", "key"))
    assert rc == 0
    assert called == [], "skip-if-present must not invoke the runner"
    # File untouched
    after = json.loads(f.read_text(encoding="utf-8"))
    assert after == std


def test_backfiller_runs_spatial_lane_with_k2(monkeypatch, tmp_path):
    """On a standard file WITHOUT the lane, the backfiller must invoke the
    runner with k=2 (harvest special-case) and merge the result."""
    import scripts.backfill_v2_lane as bf

    std = {"structural": {"tokenizer_signature": {"v2": {}}}}
    f = tmp_path / "std.json"
    f.write_text(json.dumps(std), encoding="utf-8")
    monkeypatch.setattr(bf, "STANDARDS_DIR", tmp_path)
    monkeypatch.setattr(bf, "_make_client", lambda: None)

    seen = {}

    async def fake_runner(client, ep, key, lane, **kwargs):
        seen.update(kwargs)
        return {"tiers": {"point_in_box": {"acc": 0.5, "n": 6}}, "k": kwargs.get("k")}

    monkeypatch.setitem(bf.LANES, "spatial_curve", fake_runner)
    rc = _run(bf._run("std.json", "lane-id", "spatial_curve", "key"))
    assert rc == 0
    assert seen.get("k") == 2, seen
    after = json.loads(f.read_text(encoding="utf-8"))
    assert after["structural"]["tokenizer_signature"]["v2"]["spatial_curve"]["k"] == 2
