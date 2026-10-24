"""hard_series v2 lane (2026-10-20) -- offline tests.

Covers: ANSWER-line parsing (correct / wrong / missing / negative), generator
determinism + independent re-computation of the ground truth for all three
math tiers, lane payload shape (judged present only with a judge), the
similarity function's shared-component contract, the run_standard_v2 wiring,
the judge_rank weight rebalance (sum exactly 1.0) and the backfiller LANES
registration. No network: every runner is mocked or served by MockTransport.
"""
import asyncio
import json
import math
import random
import re

import httpx

from app.authenticity import standard_v2 as v2
from app.authenticity.judge_rank import DECISIVE_LANES, FEATURE_WEIGHTS
from scripts.backfill_v2_lane import LANES


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ------------------------------------------------------------- answer parsing

def test_parse_answer_correct_wrong_missing_negative():
    assert v2.parse_hard_answer("working...\nANSWER: 42") == 42
    assert v2.parse_hard_answer("ANSWER: -7") == -7
    # Only the integer immediately after the marker is taken (no comma
    # grouping, no arithmetic) -- documented contract, not a silent parse.
    assert v2.parse_hard_answer("ANSWER:  12,345") == 12
    assert v2.parse_hard_answer("ANSWER: twelve") is None
    assert v2.parse_hard_answer("the answer is 42") is None
    assert v2.parse_hard_answer("") is None
    assert v2.parse_hard_answer(None) is None


def test_parse_answer_last_line_wins():
    text = "first guess\nANSWER: 5\nrechecked\nANSWER: 9"
    assert v2.parse_hard_answer(text) == 9


def test_grade_hard_math_exact_match_only():
    assert v2.grade_hard_math(42, "blah\nANSWER: 42") is True
    assert v2.grade_hard_math(42, "ANSWER: 41") is False
    assert v2.grade_hard_math(42, "I think it is 42.") is False  # no ANSWER line
    assert v2.grade_hard_math(-3, "ANSWER: -3") is True


# ------------------------------------------------ generator truth + determinism

def _tier_fns():
    return {tier: fns for tier, _name, fns in v2.HARD_MATH_TIERS}


def test_generators_are_deterministic_per_seed():
    for tier, _name, fns in v2.HARD_MATH_TIERS:
        for fn in fns:
            a = fn(random.Random(99))
            b = fn(random.Random(99))
            assert a == b, (tier, fn.__name__)
            c = fn(random.Random(100))
            assert a != c, (tier, fn.__name__, "fresh instances expected")


def test_tier1_truth_recomputed_independently():
    rng = random.Random(4242)
    for _ in range(20):
        q, want = v2._hm_sumsq_mod(rng)
        n = int(re.search(r"sum_\{k=1\}\^\{(\d+)\}", q).group(1))
        m = int(re.search(r"mod (\d+)", q).group(1))
        assert want == sum(k * k for k in range(1, n + 1)) % m
    for _ in range(20):
        q, want = v2._hm_sumcube_mod(rng)
        n = int(re.search(r"sum_\{k=1\}\^\{(\d+)\}", q).group(1))
        m = int(re.search(r"mod (\d+)", q).group(1))
        assert want == sum(k ** 3 for k in range(1, n + 1)) % m


def test_tier2_truth_recomputed_independently():
    rng = random.Random(777)
    for _ in range(6):
        q, want = v2._hm_gcd_congruence(rng)
        n = int(re.search(r"1 <= x <= (\d+)", q).group(1))
        m = int(re.search(r"gcd\(x, (\d+)\)", q).group(1))
        r = int(re.search(r"x == (\d+) \(mod", q).group(1))
        qq = int(re.search(r"\(mod (\d+)\)", q).group(1))
        brute = sum(1 for x in range(1, n + 1) if x % qq == r and math.gcd(x, m) == 1)
        assert want == brute
    for _ in range(6):
        q, want = v2._hm_divisor_union(rng)
        n = int(re.search(r"1 <= x <= (\d+)", q).group(1))
        a = int(re.search(r"divisible by (\d+)", q).group(1))
        b = int(re.search(r"by (\d+) \(or both\)", q).group(1))
        brute = sum(1 for x in range(1, n + 1) if x % a == 0 or x % b == 0)
        assert want == brute


def test_tier3_truth_recomputed_independently():
    rng = random.Random(31337)
    for _ in range(3):
        q, want = v2._hm_sigma_divisible(rng)
        n = int(re.search(r"1 <= n <= (\d+)", q).group(1))
        p = int(re.search(r"divisible by (\d+)", q).group(1))
        sig = [0] * (n + 1)
        for d in range(1, n + 1):
            for m in range(d, n + 1, d):
                sig[m] += d
        assert want == sum(1 for i in range(1, n + 1) if sig[i] % p == 0)
    for _ in range(3):
        q, want = v2._hm_tau_exact(rng)
        n = int(re.search(r"1 <= n <= (\d+)", q).group(1))
        k = int(re.search(r"exactly (\d+) positive divisors", q).group(1))
        # independent divisor count: prime factorisation (trial division),
        # tau = prod(e_i + 1) -- deliberately NOT the runner's sieve.
        def _tau(x):
            t, d = 1, 2
            while d * d <= x:
                e = 0
                while x % d == 0:
                    x //= d
                    e += 1
                t *= (e + 1)
                d += 1 if d == 2 else 2
            if x > 1:
                t *= 2
            return t
        brute = sum(1 for i in range(1, n + 1) if _tau(i) == k)
        assert want == brute


def test_every_generator_carries_the_answer_contract():
    rng = random.Random(5)
    for _tier, _name, fns in v2.HARD_MATH_TIERS:
        for fn in fns:
            q, want = fn(rng)
            assert 'ANSWER: <integer>' in q
            assert isinstance(want, int)


# ------------------------------------------------------------- runner payload

def _oracle_handler(expected_by_prompt):
    """Perfect-solver mock for the math half (prompts are opaque to it, so the
    expected values are supplied by the test through a prompt->answer map)."""
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        prompt = body["messages"][-1]["content"]
        ans = expected_by_prompt.get(prompt)
        content = f"ANSWER: {ans}" if ans is not None else "ANSWER: 0"
        return httpx.Response(200, json={"choices": [
            {"message": {"content": content}, "finish_reason": "stop"}]})
    return handler


def _capture_rng_prompts(seed, k):
    """Re-generate the exact prompts run_hard_series will build for `seed`."""
    rng = random.Random(seed)
    out = []
    for tier, _name, variants in v2.HARD_MATH_TIERS:
        for _ in range(k):
            fn = rng.choice(variants)
            out.append(fn(rng))
    return out


def test_run_hard_series_math_half_shape_without_judge(monkeypatch):
    monkeypatch.setattr(v2, "_MIN_INTERVAL", 0.0)
    expected = dict(_capture_rng_prompts(21, 2))

    async def go():
        async with httpx.AsyncClient(
                transport=httpx.MockTransport(_oracle_handler(expected))) as c:
            return await v2.run_hard_series(c, "http://x", "k", "m", k=2,
                                            rng=random.Random(21), gaps=())
    res = _run(go())
    assert set(res) == {"answers", "math", "legacy_features", "k"}
    assert "judged" not in res, "judge=None must omit the judged sub-key"
    assert res["k"] == 2
    assert [t["tier"] for t in res["math"]["tiers"]] == [1, 2, 3]
    assert all(t["total"] == 2 for t in res["math"]["tiers"])
    assert all(t["correct"] == 2 for t in res["math"]["tiers"])
    assert res["math"]["accuracy_by_tier"] == [1.0, 1.0, 1.0]
    assert res["legacy_features"]["hard_math_t1"] == 1.0
    # one raw answer per math instance
    assert len(res["answers"]) == 3 * 2


def test_run_hard_series_grades_partial_and_missing_answers(monkeypatch):
    monkeypatch.setattr(v2, "_MIN_INTERVAL", 0.0)
    expected = dict(_capture_rng_prompts(22, 1))
    prompts = list(expected)
    # tier1 right, tier2 wrong integer, tier3 no ANSWER line at all
    replies = {prompts[0]: f"ANSWER: {expected[prompts[0]]}",
               prompts[1]: "ANSWER: 1" if expected[prompts[1]] != 1 else "ANSWER: 2",
               prompts[2]: "I am not sure."}

    def handler(req):
        body = json.loads(req.content)
        prompt = body["messages"][-1]["content"]
        return httpx.Response(200, json={"choices": [
            {"message": {"content": replies.get(prompt, "ANSWER: 0")},
             "finish_reason": "stop"}]})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await v2.run_hard_series(c, "http://x", "k", "m", k=1,
                                            rng=random.Random(22), gaps=())
    res = _run(go())
    assert res["math"]["accuracy_by_tier"] == [1.0, 0.0, 0.0]
    assert res["legacy_features"]["hard_math_t2"] == 0.0


def test_run_hard_series_judged_half_runs_only_with_judge(monkeypatch):
    monkeypatch.setattr(v2, "_MIN_INTERVAL", 0.0)
    expected = dict(_capture_rng_prompts(23, 1))
    judge_calls = []

    def handler(req):
        body = json.loads(req.content)
        prompt = body["messages"][-1]["content"]
        if body.get("model") == "judge-model":
            judge_calls.append(prompt)
            return httpx.Response(200, json={"choices": [
                {"message": {"content": '{"score": 8, "why": "solid"}'},
                 "finish_reason": "stop"}]})
        ans = expected.get(prompt)
        return httpx.Response(200, json={"choices": [
            {"message": {"content": f"ANSWER: {ans}"}, "finish_reason": "stop"}]})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await v2.run_hard_series(
                c, "http://x", "k", "m", k=1, rng=random.Random(23), gaps=(),
                judge=("http://judge", "jk", "judge-model"))
    res = _run(go())
    assert set(res) == {"answers", "math", "judged", "legacy_features", "k"}
    assert set(res["judged"]) == set(v2.HARD_JUDGED_PROBES)
    assert res["judged"]["hard_architecture"] == {"score": 8, "why": "solid"}
    assert res["legacy_features"]["hard_judged_architecture"] == 8
    assert len(judge_calls) == len(v2.HARD_JUDGED_PROBES)
    # every judged answer is retained in `answers` for offline inspection
    assert all(f"hj_{a}_0" in res["answers"] for a in v2.HARD_JUDGED_PROBES)


def test_run_hard_series_judge_failure_is_fail_open(monkeypatch):
    monkeypatch.setattr(v2, "_MIN_INTERVAL", 0.0)
    expected = dict(_capture_rng_prompts(24, 1))

    def handler(req):
        body = json.loads(req.content)
        prompt = body["messages"][-1]["content"]
        if body.get("model") == "judge-model":
            return httpx.Response(500, json={})
        ans = expected.get(prompt)
        return httpx.Response(200, json={"choices": [
            {"message": {"content": f"ANSWER: {ans}"}, "finish_reason": "stop"}]})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await v2.run_hard_series(
                c, "http://x", "k", "m", k=1, rng=random.Random(24), gaps=(),
                judge=("http://judge", "jk", "judge-model"))
    res = _run(go())
    assert res["judged"] == {}, "judge error must omit the axes, never raise"
    assert res["math"]["accuracy_by_tier"] == [1.0, 1.0, 1.0]


def test_run_hard_series_dead_transport_returns_empty(monkeypatch):
    monkeypatch.setattr(v2, "_MIN_INTERVAL", 0.0)

    def dead(req):
        return httpx.Response(500, json={})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(dead)) as c:
            return await v2.run_hard_series(c, "http://x", "k", "m", k=1,
                                            rng=random.Random(1), gaps=())
    assert _run(go()) == {}


def test_judge_json_extraction_accepts_prose_and_nesting():
    assert v2._extract_judge_score('```json\n{"score": 7, "why": "ok"}\n```') == (7, "ok")
    assert v2._extract_judge_score('Result: {"axis": {"score": 9, "why": "x"}}') == (9, "x")
    assert v2._extract_judge_score("no json here") is None
    assert v2._extract_judge_score('{"score": 99}')[0] == 10   # clamped


# ---------------------------------------------------------------- similarity

def _hs(math_tiers, judged=None):
    tiers = [{"tier": t, "correct": c, "total": n} for t, (c, n) in math_tiers.items()]
    out = {"math": {"accuracy_by_tier": [c / n for c, n in math_tiers.values()],
                    "tiers": tiers},
           "answers": {}, "legacy_features": {}}
    if judged is not None:
        out["judged"] = {k: {"score": s, "why": ""} for k, s in judged.items()}
    return out


def test_similarity_identical_is_one():
    a = _hs({1: (2, 2), 2: (1, 2), 3: (0, 2)},
            {"hard_architecture": 8, "hard_visual": 6, "hard_domain": 9})
    assert v2.similarity_hard_series(a, a) == 1.0


def test_similarity_none_on_missing_or_thin():
    a = _hs({1: (2, 2), 2: (1, 2), 3: (0, 2)})
    assert v2.similarity_hard_series(None, a) is None
    assert v2.similarity_hard_series(a, {}) is None
    assert v2.similarity_hard_series({}, {}) is None
    # only 2 shared components -> below min_shared=3
    thin = _hs({1: (2, 2), 2: (1, 2)})
    assert v2.similarity_hard_series(thin, thin) is None


def test_similarity_partial_shared_and_math_distance():
    a = _hs({1: (2, 2), 2: (2, 2), 3: (2, 2)})
    b = _hs({1: (2, 2), 2: (0, 2), 3: (1, 2)})
    # math half only: mean |acc diff| = (0 + 1 + 0.5) / 3
    assert v2.similarity_hard_series(a, b) == round(1 - 0.5, 3)


def test_similarity_judged_axes_scale_by_ten():
    a = _hs({1: (2, 2), 2: (2, 2), 3: (2, 2)},
            {"hard_architecture": 10, "hard_visual": 10, "hard_domain": 10})
    b = _hs({1: (2, 2), 2: (2, 2), 3: (2, 2)},
            {"hard_architecture": 5, "hard_visual": 10, "hard_domain": 10})
    # math half 1.0, judged half 1 - (0.5 + 0 + 0)/3
    assert v2.similarity_hard_series(a, b) == round((1.0 + (1 - 0.5 / 3)) / 2, 3)


# ------------------------------------------------------------------- wiring

def test_run_standard_v2_includes_hard_series_without_judge(monkeypatch):
    monkeypatch.setattr(v2, "_MIN_INTERVAL", 0.0)

    class _Ok:
        status_code = 200
        content = "ok"

    async def fake_canary(*a, **k):
        return _Ok()

    async def empty_lane(*a, **k):
        return {}

    seen = {}

    async def fake_hard(*a, **k):
        seen.update(k)
        return {"answers": {"hm_t1_0": "ANSWER: 1"},
                "math": {"accuracy_by_tier": [1.0], "tiers": [
                    {"tier": 1, "correct": 1, "total": 1}]},
                "legacy_features": {"hard_math_t1": 1.0}, "k": k.get("k")}

    monkeypatch.setattr(v2, "call_with_backoff", fake_canary)
    for name in ("run_tokenizer_fingerprint", "run_capability_curve",
                 "run_knowledge_bank", "run_discriminator", "run_spatial_curve",
                 "run_tier_splitter"):
        monkeypatch.setattr(v2, name, empty_lane)
    monkeypatch.setattr(v2, "run_hard_series", fake_hard)

    async def go():
        return await v2.run_standard_v2(None, "http://x", "k", "m", timeout_s=5,
                                        mode="harvest", rng=random.Random(3),
                                        gaps=())
    res = _run(go())
    assert "hard_series" in res
    assert "judged" not in res["hard_series"]
    assert seen.get("judge") is None, "live/harvest call without judge stays judge-free"
    assert seen.get("k") == 2, "harvest depth for hard_series is k=2"


def test_compute_v2_dims_carries_hard_series():
    std = {"structural": {"tokenizer_signature": {"v2": {
        "hard_series": _hs({1: (2, 2), 2: (2, 2), 3: (2, 2)})}}}}
    dims = v2.compute_v2_dims(std["structural"]["tokenizer_signature"]["v2"],
                              {"sib": std})
    assert dims["sib"]["hard_series"] == 1.0


def test_pairwise_separability_carries_hard_series():
    a = {"structural": {"tokenizer_signature": {"v2": {
        "hard_series": _hs({1: (2, 2), 2: (2, 2), 3: (2, 2)})}}}}
    b = {"structural": {"tokenizer_signature": {"v2": {
        "hard_series": _hs({1: (2, 2), 2: (0, 2), 3: (0, 2)})}}}}
    sep = v2.pairwise_separability(a, b)
    assert sep["sims"]["hard_series"] < 1.0
    assert sep["separable"] is True


# ------------------------------------------------------------- weights/lanes

def test_feature_weights_sum_to_one_and_hard_series_weighted():
    assert abs(sum(FEATURE_WEIGHTS.values()) - 1.0) < 1e-9
    assert FEATURE_WEIGHTS["hard_series"] == 0.11
    assert FEATURE_WEIGHTS["tier_splitter"] == 0.12
    assert FEATURE_WEIGHTS["cutoff"] == 0.08
    assert FEATURE_WEIGHTS["spatial_curve"] == 0.09   # pinned by its own test


def test_hard_series_is_a_decisive_lane():
    assert "hard_series" in DECISIVE_LANES


# ------------------------------------------------------- backfiller registration

def test_backfiller_lanes_contains_hard_series():
    assert "hard_series" in LANES
    assert LANES["hard_series"] is v2.run_hard_series


def test_backfiller_runs_hard_series_with_k2_and_judge(monkeypatch, tmp_path):
    import scripts.backfill_v2_lane as bf

    std = {"structural": {"tokenizer_signature": {"v2": {}}}}
    f = tmp_path / "std.json"
    f.write_text(json.dumps(std), encoding="utf-8")
    monkeypatch.setattr(bf, "STANDARDS_DIR", tmp_path)
    monkeypatch.setattr(bf, "_make_client", lambda: None)

    seen = {}

    async def fake_runner(client, ep, key, lane, **kwargs):
        seen.update(kwargs)
        seen["endpoint"] = ep
        return {"answers": {"hm_t1_0": "ANSWER: 1"},
                "math": {"accuracy_by_tier": [1.0], "tiers": []},
                "judged": {}, "legacy_features": {}, "k": kwargs.get("k")}

    monkeypatch.setitem(bf.LANES, "hard_series", fake_runner)
    rc = _run(bf._run("std.json", "lane-id", "hard_series", "secret"))
    assert rc == 0
    assert seen.get("k") == 2
    assert seen["judge"] == (bf.ROUTER_EP, "secret", bf.DEFAULT_JUDGE_MODEL)
    after = json.loads(f.read_text(encoding="utf-8"))
    assert after["structural"]["tokenizer_signature"]["v2"]["hard_series"]["k"] == 2


def test_backfiller_skip_if_present_hard_series(monkeypatch, tmp_path):
    import scripts.backfill_v2_lane as bf

    std = {"structural": {"tokenizer_signature": {"v2": {
        "hard_series": {"math": {"accuracy_by_tier": [1.0], "tiers": []},
                        "answers": {}, "legacy_features": {}, "k": 2}}}}}
    f = tmp_path / "std.json"
    f.write_text(json.dumps(std), encoding="utf-8")
    monkeypatch.setattr(bf, "STANDARDS_DIR", tmp_path)

    called = []

    async def fake_runner(*a, **k):
        called.append((a, k))
        return {"answers": {"x": "y"}, "k": 2}

    monkeypatch.setitem(bf.LANES, "hard_series", fake_runner)
    rc = _run(bf._run("std.json", "lane-id", "hard_series", "key"))
    assert rc == 0
    assert called == []
    assert json.loads(f.read_text(encoding="utf-8")) == std
