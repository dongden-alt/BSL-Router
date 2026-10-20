"""Standard v2 (judge-free, fake-resistant) -- offline tests.

Covers: weights invariant, tokenizer usage-delta fingerprint (overhead
cancellation + fabricated-usage flags), programmatic capability tiers (ground
truth computed, fresh instances), the truth-free knowledge bank, the matcher
wiring, and honest alias groups.
"""
import asyncio
import json
import random

import httpx

from app.authenticity import standard_v2 as v2
from app.authenticity.fingerprint import FingerprintFeatures
from app.authenticity.judge_rank import FEATURE_WEIGHTS
from app.authenticity.matcher import match_submodel


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ------------------------------------------------------------------ weights

def test_weights_invariants():
    assert abs(sum(FEATURE_WEIGHTS.values()) - 1.0) < 1e-9
    # 2026-10-20 hard_series rebalance funded the new lane partly from refusal
    # (0.05 -> 0.04); the family-corroborator role is unchanged.
    assert FEATURE_WEIGHTS["refusal"] == 0.04
    for d in ("tokenizer", "capability_curve", "knowledge_bank"):
        assert FEATURE_WEIGHTS[d] > 0


# --------------------------------------------------------------- tokenizer

def _mock_client(overhead: int, tok_per_char: float = 0.5, usage=True,
                 content="ok"):
    """Fake lane: prompt_tokens = overhead + ceil(len(prompt)*tok_per_char)."""
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        prompt = body["messages"][-1]["content"]
        data = {"choices": [{"message": {"content": content},
                             "finish_reason": "stop"}]}
        if usage:
            data["usage"] = {"prompt_tokens": overhead + int(len(prompt) * tok_per_char) + 1,
                             "completion_tokens": 1}
        return httpx.Response(200, json=data)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_tokenizer_delta_cancels_lane_overhead():
    async def go(overhead):
        async with _mock_client(overhead) as c:
            return await v2.run_tokenizer_fingerprint(c, "http://x", "k", "m")
    a = _run(go(236))
    b = _run(go(5239))
    assert a and b
    assert a["deltas"] == b["deltas"], "overhead must cancel in the delta"
    assert v2.similarity_tokenizer(a, b) == 1.0


def test_tokenizer_no_usage_returns_empty():
    async def go():
        async with _mock_client(0, usage=False) as c:
            return await v2.run_tokenizer_fingerprint(c, "http://x", "k", "m")
    assert _run(go()) == {}


def test_tokenizer_detects_naive_char_div4_usage():
    deltas = {n: v2.naive_token_estimate(s) - v2.naive_token_estimate(v2.TOKENIZER_BASELINE)
              for n, s in v2.TOKENIZER_STRINGS.items()}
    sig = v2.analyze_tokenizer(deltas, [10, 10])
    assert "usage_naive_estimator" in sig["flags"]


def test_tokenizer_detects_constant_usage():
    sig = v2.analyze_tokenizer({n: 7 for n in v2.TOKENIZER_STRINGS}, [10, 10])
    assert "usage_constant" in sig["flags"]


def test_tokenizer_different_tokenizers_separate():
    # tokenizer A: cheap CJK/emoji; tokenizer B: expensive CJK/emoji
    base = {"cjk": 12, "emoji": 14, "digits": 18, "digits_x4": 70, "spaces": 3,
            "newlines": 5, "rare_unicode": 40, "code": 22, "url": 14, "repeat": 20}
    other = dict(base, cjk=30, emoji=40, rare_unicode=75, digits=12, digits_x4=45)
    a = v2.analyze_tokenizer(base, [5, 5])
    b = v2.analyze_tokenizer(other, [5, 5])
    assert v2.similarity_tokenizer(a, a) == 1.0
    assert v2.similarity_tokenizer(a, b) < 0.75


def test_tokenizer_unstable_overhead_not_compared():
    a = v2.analyze_tokenizer({n: i for i, n in enumerate(v2.TOKENIZER_STRINGS)}, [5, 40])
    b = v2.analyze_tokenizer({n: i for i, n in enumerate(v2.TOKENIZER_STRINGS)}, [5, 5])
    assert "unstable_overhead" in a["flags"]
    assert v2.similarity_tokenizer(a, b) is None


def test_tokenizer_decisive_abstains_on_thin_cross_baseline_margin():
    """The jitter tilt must not decide a sibling pick.

    Both sibling standards carry IDENTICAL deltas and differ only in their own
    harvest-time jitter (opus 0, sonnet 3).  Under the old per-pair slack the
    suspect scored 0.0 against the clean sibling (slack=1, diff 4 > 1) but 1.0
    against the jittery one (slack=4, diff 4 <= 4) -- a full 1.0 margin of pure
    jitter artifact, far too wide for any decisive-gap guard to catch.  With
    matched slack both comparisons are identical, so the lane abstains.
    """
    names = ["cjk", "emoji", "digits", "digits_x4", "spaces",
             "newlines", "rare_unicode", "code", "url", "repeat"]
    sus = v2.analyze_tokenizer({n: 10 for n in names}, [100, 100])
    opus = v2.analyze_tokenizer({n: 14 for n in names}, [100, 100])      # jitter 0
    sonnet = v2.analyze_tokenizer({n: 14 for n in names}, [100, 103])    # jitter 3

    # The old per-pair behaviour, pinned so the tilt can never silently return:
    assert v2.similarity_tokenizer(sus, opus) == 0.0
    assert v2.similarity_tokenizer(sus, sonnet) == 1.0

    # Matched slack removes it -> identical deltas score identically -> abstain.
    assert v2.tokenizer_decisive(sus, opus, sonnet) is None


def test_tokenizer_decisive_picks_on_clear_margin():
    sus = v2.analyze_tokenizer(
        {"cjk": 17, "emoji": 36, "digits": 13, "digits_x4": 63, "spaces": 4,
         "newlines": -4, "rare_unicode": 54, "code": 26, "url": 16, "repeat": 20},
        [100, 100])
    close = v2.analyze_tokenizer(
        {"cjk": 17, "emoji": 36, "digits": 13, "digits_x4": 63, "spaces": 4,
         "newlines": -4, "rare_unicode": 54, "code": 26, "url": 16, "repeat": 20},
        [100, 100])
    far = v2.analyze_tokenizer(
        {"cjk": 60, "emoji": 90, "digits": 40, "digits_x4": 150, "spaces": 9,
         "newlines": 30, "rare_unicode": 120, "code": 70, "url": 45, "repeat": 55},
        [100, 100])
    res = v2.tokenizer_decisive(sus, close, far)
    assert res is not None
    assert res[0] >= res[1]            # winner-first ordering
    assert res[0] - res[1] >= v2.TOKENIZER_DECISIVE_GAP


def test_tokenizer_decisive_none_when_side_not_comparable():
    sus = v2.analyze_tokenizer({n: i for i, n in enumerate(v2.TOKENIZER_STRINGS)}, [5, 5])
    unstable = v2.analyze_tokenizer({n: i for i, n in enumerate(v2.TOKENIZER_STRINGS)}, [5, 40])
    ok = v2.analyze_tokenizer({n: i for i, n in enumerate(v2.TOKENIZER_STRINGS)}, [5, 5])
    assert v2.tokenizer_decisive(sus, unstable, ok) is None


# -------------------------------------------------------- capability curve

def test_capability_truth_is_computed_and_graded():
    rng = random.Random(7)
    for name, fn in v2.CAPABILITY_TIERS:
        q, ans = fn(rng)
        assert q and ans, name
    prompt, expected = v2.build_capability_prompt(v2.CAPABILITY_TIERS[0][1], rng)
    perfect = "\n".join(f"Q{i}: {a}" for i, a in enumerate(expected, 1))
    assert v2.grade_capability(expected, perfect)[:2] == (3, 3)
    wrong = "\n".join(f"Q{i}: 1" for i in range(1, 4))
    assert v2.grade_capability(expected, wrong)[0] == 0
    # garbled/missing lines count wrong, never crash
    assert v2.grade_capability(expected, "no idea")[0] == 0


def test_capability_grader_handles_formatting_noise():
    exp = ["12345", "monday", "abcd"]
    txt = "**Q1:** 12,345\nQ2: `Monday`\nq3 = abcd"
    c, t, parsed = v2.grade_capability(exp, txt)
    assert (c, t) == (3, 3)


def test_capability_instances_are_fresh():
    fn = v2.CAPABILITY_TIERS[4][1]
    p1, _ = v2.build_capability_prompt(fn, random.Random(1))
    p2, _ = v2.build_capability_prompt(fn, random.Random(2))
    assert p1 != p2


def test_capability_known_answers():
    rng = random.Random(3)
    q, a = v2._t_powmod(rng)
    # recompute independently from the question text
    import re
    m = re.search(r"\((\d+)\^(\d+)\) mod (\d+)", q)
    assert str(pow(int(m.group(1)), int(m.group(2)), int(m.group(3)))) == a
    q, a = v2._t_digitsum(rng)
    n = int(re.search(r"2\^(\d+)", q).group(1))
    assert str(sum(map(int, str(2 ** n)))) == a


def test_capability_curve_end_to_end_with_oracle_model():
    """A perfect-solver mock scores 1.0 on every tier."""
    import re
    state = {}

    def handler(req):
        body = json.loads(req.content)
        prompt = body["messages"][-1]["content"]
        lines = []
        for m in re.finditer(r"Q(\d+): (.+)", prompt):
            lines.append((int(m.group(1)), m.group(2)))
        out = []
        for n, q in lines:
            out.append(f"Q{n}: {_oracle(q)}")
        return httpx.Response(200, json={"choices": [{"message": {"content": "\n".join(out)},
                                                      "finish_reason": "stop"}]})

    def _oracle(q):
        import datetime
        if m := re.match(r"Compute (\d+) \* (\d+)\.", q):
            return str(int(m.group(1)) * int(m.group(2)))
        if m := re.match(r"Write the string '(\w+)' reversed", q):
            return m.group(1)[::-1]
        if m := re.match(r"Convert the binary number (\d+) to decimal", q):
            return str(int(m.group(1), 2))
        if m := re.match(r"How many times does the letter '(\w)' appear in: (\w+)", q):
            return str(m.group(2).count(m.group(1)))
        if m := re.match(r"What day of the week is it (\d+) days after ([\d-]+)\?", q):
            d = datetime.date.fromisoformat(m.group(2)) + datetime.timedelta(days=int(m.group(1)))
            return d.strftime("%A")
        if m := re.match(r"List: \[([\d, ]+)\]\. What is the 5th smallest", q):
            return str(sorted(int(x) for x in m.group(1).split(","))[4])
        if m := re.match(r"Compute \((\d+)\^(\d+)\) mod (\d+)\.", q):
            return str(pow(int(m.group(1)), int(m.group(2)), int(m.group(3))))
        if m := re.match(r"What is the integer part \(floor\) of (\d+) / (\d+)\?", q):
            return str(int(m.group(1)) // int(m.group(2)))
        if m := re.match(r"What is the sum of the decimal digits of 2\^(\d+)\?", q):
            return str(sum(map(int, str(2 ** int(m.group(1))))))
        return "?"

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await v2.run_capability_curve(c, "http://x", "k", "m", k=1,
                                                 rng=random.Random(11))
    res = _run(go())
    assert set(res["tiers"]) == {n for n, _ in v2.CAPABILITY_TIERS}
    assert all(t["acc"] == 1.0 for t in res["tiers"].values()), res


def test_curve_similarity_separates_a_weaker_model():
    strong = {"tiers": {n: {"acc": 1.0, "n": 6} for n, _ in v2.CAPABILITY_TIERS}}
    weak_tiers = {n: {"acc": (1.0 if i < 5 else 0.0), "n": 6}
                  for i, (n, _) in enumerate(v2.CAPABILITY_TIERS)}
    weak = {"tiers": weak_tiers}
    assert v2.similarity_curve(strong, strong) == 1.0
    assert v2.similarity_curve(strong, weak) < 0.6


# ---------------------------------------------------------- knowledge bank

def test_bank_shape():
    ids = [i["id"] for i in v2.KNOWLEDGE_BANK]
    assert len(ids) == len(set(ids)) >= 60
    groups = {i["group"] for i in v2.KNOWLEDGE_BANK}
    assert groups == {"fabricated", "dated", "obscure"}


def test_bank_normalization_and_matching():
    assert v2.normalize_kb_answer("year", "Probably 1987, I think") == "1987"
    assert v2.normalize_kb_answer("year", "UNKNOWN") == "unknown"
    assert v2.normalize_kb_answer("year", "I cannot verify that exists") == "unknown"
    assert v2.normalize_kb_answer("text", "**The Paris**") == "paris"
    assert v2.kb_match("paris", "paris france")
    assert not v2.kb_match("paris", "london")
    assert v2.kb_match("unknown", "unknown")


def _bank(answers):
    return {"answers": {k: [v] for k, v in answers.items()}, "k": 1}


def test_bank_similarity_and_informative_items():
    ids = [i["id"] for i in v2.KNOWLEDGE_BANK][:20]
    A = _bank({i: f"a{n}" for n, i in enumerate(ids)})
    B = _bank({i: (f"a{n}" if n < 14 else f"b{n}") for n, i in enumerate(ids)})
    assert v2.similarity_bank(A, A) == 1.0
    assert v2.similarity_bank(A, B) == 0.7
    inf = v2.informative_items({"A": A, "B": B})
    assert inf == set(ids[14:])
    # restricted to the 6 informative items the contrast sharpens to 0.0
    assert v2.similarity_bank(A, B, inf) == 0.0


def test_bank_runner_end_to_end_parses_batches():
    def handler(req):
        body = json.loads(req.content)
        prompt = body["messages"][-1]["content"]
        import re
        out = [f"K{m.group(1)}: unknown" for m in re.finditer(r"K(\d+): ", prompt)]
        return httpx.Response(200, json={"choices": [{"message": {"content": "\n".join(out)},
                                                      "finish_reason": "stop"}]})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await v2.run_knowledge_bank(c, "http://x", "k", "m", k=2)
    res = _run(go())
    assert len(res["answers"]) == len(v2.KNOWLEDGE_BANK)
    assert all(v == ["unknown", "unknown"] for v in res["answers"].values())


# ------------------------------------------------- matcher wiring + aliases

def _std_v2(tok, kb_shift, curve_knee):
    ids = [i["id"] for i in v2.KNOWLEDGE_BANK][:24]
    answers = {i: [(f"a{n}" if n >= kb_shift else f"z{n}")] for n, i in enumerate(ids)}
    tiers = {n: {"acc": (1.0 if k < curve_knee else 0.0), "n": 6}
             for k, (n, _) in enumerate(v2.CAPABILITY_TIERS)}
    deltas = {n: tok.get(n, 5) for n in v2.TOKENIZER_STRINGS}
    return {"structural": {"tokenizer_signature": {"v2": {
        "tokenizer": v2.analyze_tokenizer(deltas, [5, 5]),
        "capability_curve": {"tiers": tiers, "k": 1},
        "knowledge_bank": {"answers": answers, "k": 1}}}}}


def _feats_from(std):
    f = FingerprintFeatures(capability_correct=3)
    f.v2_profile = std["structural"]["tokenizer_signature"]["v2"]
    return f


def test_matcher_uses_v2_to_pick_the_right_sibling():
    A = _std_v2({}, kb_shift=0, curve_knee=12)
    B = _std_v2({}, kb_shift=10, curve_knee=7)
    res = match_submodel(_feats_from(A), {"sib-a": A, "sib-b": B})
    assert res.best_model == "sib-a"
    assert res.dimensions["knowledge_bank"] == 1.0
    assert res.dimensions["capability_curve"] == 1.0
    assert not res.alias_group


def test_matcher_reports_alias_group_for_identical_siblings():
    A = _std_v2({}, kb_shift=0, curve_knee=12)
    A2 = json.loads(json.dumps(A))
    res = match_submodel(_feats_from(A), {"sib-a": A, "sib-b": A2})
    assert res.abstain, "tie must abstain"
    assert set(res.alias_group) == {"sib-a", "sib-b"}
    assert res.dimensions["_alias_detail"]["sib-b"]["separable"] is False


def test_matcher_without_v2_data_is_unchanged_shape():
    A = {"knowledge": {"cutoff_year": 2025, "capability_boundary": {"correct": 3}}}
    f = FingerprintFeatures(capability_correct=3, cutoff_year=2025)
    res = match_submodel(f, {"only": A})
    assert res.dimensions["knowledge_bank"] is None
    assert res.dimensions["tokenizer"] is None


def test_pairwise_separability_flags_distinct_standards():
    A = _std_v2({}, kb_shift=0, curve_knee=12)
    B = _std_v2({"cjk": 40, "emoji": 60}, kb_shift=12, curve_knee=6)
    assert v2.pairwise_separability(A, B)["separable"] is True
    assert v2.pairwise_separability(A, A)["separable"] is False


def test_v2_lane_failure_modes_are_failopen(monkeypatch):
    monkeypatch.setattr(v2, "_MIN_INTERVAL", 0.0)  # offline battery: no pace sleeps
    def handler(req):
        return httpx.Response(500, json={})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await v2.run_standard_v2(c, "http://x", "k", "m", mode="live",
                                            rng=random.Random(1), gaps=())
    res = _run(go())
    # 2026-10-19 fail-open: a dead transport must NOT abort at the canary (old
    # behavior: gap=lane_unreachable with zero lanes attempted). The canary
    # miss is recorded as diagnostics and every lane still runs; all five fail
    # individually and say why via lane_gaps (spatial_curve joined the lane set
    # 2026-10-19). gaps=() keeps this fast -- the
    # default live gaps=(3.0,) would sleep 3s between retry attempts now that
    # the lanes actually execute under failure. 2026-10-19 pace: _pace() fires
    # per CALL (not per retry), so fail-open's full call battery at the
    # 0.75s import-time _MIN_INTERVAL stacks 10-27s of sleep against the 10s
    # pytest timeout. monkeypatch.setattr reaches the constant; setenv cannot
    # (it is bound at import time).
    assert res["version"] == 2 and res["mode"] == "live"
    assert res["gap"] == "canary_failed_open"
    assert res["canary_gap"] == "http_500"
    assert res["lane_gaps"] == {
        "tokenizer": "lane_returned_empty",
        "capability_curve": "lane_returned_empty",
        "knowledge_bank": "lane_returned_empty",
        "discriminator": "lane_returned_empty",
        "spatial_curve": "lane_returned_empty",
        "hard_series": "lane_returned_empty",   # 2026-10-20 lane
    }


def test_outer_timeout_keeps_finished_lanes_via_sink(monkeypatch):
    """2026-10-12 regression: the gate's outer wait_for cancelled the whole v2
    coroutine and discarded every lane that had already finished -> all v2
    dims None on live runs. With a sink, finished lanes must survive."""
    class _Ok:
        status_code = 200

    async def fake_canary(*a, **k):
        return _Ok()

    async def fake_tok(*a, **k):
        return {"deltas": {"a": 1}}

    async def fake_cap(*a, **k):
        return {"tiers": {"1": 1.0}}

    async def stall(*a, **k):
        await asyncio.sleep(3600)

    monkeypatch.setattr(v2, "call_with_backoff", fake_canary)
    monkeypatch.setattr(v2, "run_tokenizer_fingerprint", fake_tok)
    monkeypatch.setattr(v2, "run_capability_curve", fake_cap)
    monkeypatch.setattr(v2, "run_knowledge_bank", stall)
    monkeypatch.setattr(v2, "run_discriminator", stall)

    sink: dict = {}

    async def go():
        try:
            await asyncio.wait_for(
                v2.run_standard_v2(None, "http://x", "k", "m", timeout_s=30,
                                   mode="live", rng=random.Random(1), sink=sink),
                timeout=0.3)
        except asyncio.TimeoutError:
            return "timed_out"
        return "finished"

    assert _run(go()) == "timed_out"
    assert sink["tokenizer"] == {"deltas": {"a": 1}}
    assert sink["capability_curve"] == {"tiers": {"1": 1.0}}
    assert "knowledge_bank" not in sink and "discriminator" not in sink


def test_sink_is_optional_and_return_value_unchanged(monkeypatch):
    """Callers that pass no sink (harvest path) get the same dict shape back."""
    monkeypatch.setattr(v2, "_MIN_INTERVAL", 0.0)  # offline battery: no pace sleeps
    def handler(req):
        return httpx.Response(500, json={})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            s: dict = {}
            r = await v2.run_standard_v2(c, "http://x", "k", "m", mode="live",
                                         rng=random.Random(1), gaps=(), sink=s)
            return r, s
    r, s = _run(go())
    assert r is s
    # Same fail-open contract as above: sink identity preserved, canary miss
    # recorded as diagnostics (not an abort), all five lanes attempted and
    # individually reported. gaps=() + _MIN_INTERVAL=0 keep the failure path
    # fast: _pace() sleeps per CALL, not per retry, and the live 0.75s
    # constant over the full call battery would blow the 10s pytest timeout.
    assert r["version"] == 2 and r["mode"] == "live"
    assert r["gap"] == "canary_failed_open"
    assert r["canary_gap"] == "http_500"
    assert set(r["lane_gaps"]) == {"tokenizer", "capability_curve",
                                   "knowledge_bank", "discriminator",
                                   "spatial_curve", "hard_series"}
