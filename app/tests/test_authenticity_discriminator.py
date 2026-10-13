"""Discriminator lane (v3 behavioural-schema axis) -- offline tests.

Covers: schema_features canonicalization (JSON vs prose branches), the
runner (mock lane, k runs, per-probe answer vectors), similarity_discriminator
(agreement fraction, <5-shared guard), run_standard_v2 wiring (discriminator
lands in `out`), and pairwise_separability inclusion (None-safe: old files
without the lane keep scoring on the other 3 axes).
"""
import asyncio
import json

import httpx

from app.authenticity import standard_v2 as v2


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------- schema_features

def test_features_json_branch():
    text = '```json\n[{"tool": "get_weather", "arguments": {"location": "Hanoi"}}]\n```'
    f = v2.schema_features(text)
    assert f.startswith("f=1|")
    assert "arguments" in f and "location" in f and "tool" in f
    assert "|v=get|" in f
    assert "a=a" in f  # arguments -> 'a'


def test_features_json_no_fence_args_key():
    text = '[{"name": "get_weather", "params": {"city": "Hanoi"}}]'
    f = v2.schema_features(text)
    assert f.startswith("f=0|")
    assert "|v=get|" in f and "a=p" in f


def test_features_prose_branch():
    text = "There is no established result called the Kessler-Voronin lemma."
    f = v2.schema_features(text)
    # first 6 normalized words (by design); refusal phrasing opener is the signal
    assert f == "f=0|w=there is no established result called"


def test_features_empty():
    assert v2.schema_features("") == ""
    assert v2.schema_features("   ") == ""


def test_features_deterministic():
    text = '[{"tool": "fetch_x", "args": {"q": 1}}]'
    assert v2.schema_features(text) == v2.schema_features(text)


# --------------------------------------------------------------- similarity

def _bank(**answers):
    return {"answers": answers, "k": 2}


def test_similarity_identical_is_one():
    a = _bank(p1=["x"], p2=["y"], p3=["z"], p4=["w"], p5=["v"])
    assert v2.similarity_discriminator(a, a) == 1.0


def test_similarity_disjoint_is_zero():
    a = _bank(p1=["x"], p2=["y"], p3=["z"], p4=["w"], p5=["v"])
    b = _bank(p1=["q"], p2=["r"], p3=["s"], p4=["t"], p5=["u"])
    assert v2.similarity_discriminator(a, b) == 0.0


def test_similarity_guard_needs_five_shared():
    a = _bank(p1=["x"], p2=["y"], p3=["z"], p4=["w"])
    b = _bank(p1=["x"], p2=["y"], p3=["z"], p4=["w"])
    assert v2.similarity_discriminator(a, b) is None


def test_similarity_partial_agreement():
    a = _bank(p1=["x"], p2=["y"], p3=["z"], p4=["w"], p5=["v"])
    b = _bank(p1=["x"], p2=["y"], p3=["z"], p4=["t"], p5=["u"])
    assert v2.similarity_discriminator(a, b) == 0.6


# ------------------------------------------------------------------- runner

def _mock_lane():
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        prompt = body["messages"][-1]["content"]
        calls.append(prompt)
        if "Reply with the single word" in prompt:
            content = "ok"
        elif "Kessler-Voronin" in prompt:
            content = "There is no established result called that lemma."
        else:
            content = '[{"tool": "get_weather", "arguments": {"city": "X"}}]'
        return httpx.Response(200, json={
            "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10 + len(prompt) // 4, "completion_tokens": 5}})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), calls


def test_runner_populates_all_probes(monkeypatch):
    monkeypatch.setenv("BSL_HARVEST_MIN_INTERVAL", "0")
    monkeypatch.setattr(v2, "_MIN_INTERVAL", 0.0)
    client, calls = _mock_lane()
    out = _run(v2.run_discriminator(client, "http://x", "k", "m",
                                    timeout_s=30, k=2, concurrency=2))
    assert set(out["answers"]) == {p for p, _ in v2.SCHEMA_PROBES}
    assert all(len(v) == 2 for v in out["answers"].values())  # k=2 runs
    assert out["k"] == 2


def test_runner_returns_empty_on_all_fail(monkeypatch):
    monkeypatch.setattr(v2, "_MIN_INTERVAL", 0.0)

    def handler(req):
        return httpx.Response(500, json={"error": "boom"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    out = _run(v2.run_discriminator(client, "http://x", "k", "m",
                                    timeout_s=5, k=1, gaps=()))
    assert out == {}


# ------------------------------------------------------------------- wiring

def _std_with_v2(**v2lanes):
    return {"structural": {"tokenizer_signature": {"v2": v2lanes}}}


def test_run_standard_v2_includes_discriminator(monkeypatch):
    monkeypatch.setattr(v2, "_MIN_INTERVAL", 0.0)
    client, _ = _mock_lane()

    async def fake_tok(*a, **k):
        return {"deltas": {}}

    async def fake_cap(*a, **k):
        return {}

    async def fake_kb(*a, **k):
        return {}

    monkeypatch.setattr(v2, "run_tokenizer_fingerprint", fake_tok)
    monkeypatch.setattr(v2, "run_capability_curve", fake_cap)
    monkeypatch.setattr(v2, "run_knowledge_bank", fake_kb)
    out = _run(v2.run_standard_v2(client, "http://x", "k", "m", timeout_s=30))
    assert "discriminator" in out
    assert out["discriminator"]["answers"]


def test_pairwise_none_safe_without_discriminator():
    """Old files (no discriminator lane) must still score on the other axes."""
    tok = {"deltas": {n: i for i, n in enumerate("abcdefgh")}, "flags": []}
    a = _std_with_v2(tokenizer=tok)
    b = _std_with_v2(tokenizer=tok)
    r = v2.pairwise_separability(a, b)
    assert r["sims"]["discriminator"] is None
    assert r["distance"] is not None  # tokenizer still compared


def test_pairwise_uses_discriminator():
    tok = {"deltas": {n: i for i, n in enumerate("abcdefgh")}, "flags": []}
    da = _bank(p1=["x"], p2=["y"], p3=["z"], p4=["w"], p5=["v"])
    db = _bank(p1=["q"], p2=["r"], p3=["s"], p4=["t"], p5=["u"])
    a = _std_with_v2(tokenizer=tok, discriminator=da)
    b = _std_with_v2(tokenizer=tok, discriminator=db)
    r = v2.pairwise_separability(a, b)
    assert r["sims"]["discriminator"] == 0.0
    # identical tokenizer (sim 1.0) + zero discriminator (sim 0.0) -> dist 0.5
    assert r["distance"] == 0.5
    assert r["separable"] is True


# ---------------------------------------------------------- feature_bucket

def test_feature_bucket_empty():
    assert v2.feature_bucket("") == ""
    assert v2.feature_bucket("   ") == ""


def test_feature_bucket_deterministic():
    t = ("A runaway trolley should be diverted. One life against five. "
         "That is the calculus.")
    assert v2.feature_bucket(t) == v2.feature_bucket(t)


def test_feature_bucket_trivial_wobble_same_bucket():
    base = "The ball is 5 cents."
    for wob in ("The ball is 5 cents. ", "the ball is 5 cents", "The  ball   is 5 cents."):
        assert v2.feature_bucket(wob) == v2.feature_bucket(base)


def test_feature_bucket_distinct_content_distinct_bucket():
    a = v2.feature_bucket("The ball costs five cents, not ten.")
    b = v2.feature_bucket("Blue is the feeling of a cold ocean breeze.")
    assert a and b and a != b


def test_similarity_bucket_any_overlap():
    a = _bank(p1=["h1", "h1b"], p2=["x"], p3=["z"], p4=["w"], p5=["v"])
    b = _bank(p1=["h1b"], p2=["x"], p3=["q"], p4=["t"], p5=["u"])
    # p1 overlaps via h1b, p2 exact -> 2/5
    assert v2.similarity_discriminator(a, b) == 0.4


# ------------------------------------------------- informative item filter

def test_informative_discriminator_items_pairwise_disjoint():
    # p1: A and C present disjoint shapes -> informative (can split that pair).
    # p2: every candidate's shape overlaps every other -> noise floor, dropped.
    cands = {
        "A": _bank(p1=["s1:a"], p2=["m2:x"]),
        "B": _bank(p1=["s1:b"], p2=["m2:y"]),
        "C": _bank(p1=["l4f:c"], p2=["m2:z"]),
    }
    inf = v2.informative_discriminator_items(cands)
    assert "p1" in inf
    assert "p2" not in inf


def test_informative_discriminator_items_tolerates_missing_probe():
    # Regression (2026-10-11): the old probe-ID intersection collapsed to
    # empty when ANY candidate lacked a probe. B lacks p1; p1 must still be
    # evaluated on the candidates that carry it.
    cands = {
        "A": _bank(p1=["s1:a"], p2=["m2:x"]),
        "B": _bank(p2=["m2:y"]),
        "C": _bank(p1=["l4f:c"], p2=["m2:z"]),
    }
    assert "p1" in v2.informative_discriminator_items(cands)


def test_informative_discriminator_items_needs_two_profiles():
    assert v2.informative_discriminator_items({"A": _bank(p1=["s1:a"])}) == set()
    assert v2.informative_discriminator_items({}) == set()


def test_similarity_discriminator_informative_concentrates():
    # 4 saturated probes (identical shapes everywhere) + 4 splitting probes.
    # All-probes sim = 4/8 = 0.5; restricted to the 4 informative -> 0.0.
    sat = {f"s{n}": ["m2:same"] for n in range(4)}
    a = _bank(**sat, **{f"i{n}": [f"s1:a{n}"] for n in range(4)})
    b = _bank(**sat, **{f"i{n}": [f"l4f:b{n}"] for n in range(4)})
    informative = {f"i{n}" for n in range(4)}
    assert v2.similarity_discriminator(a, b) == 0.5
    assert v2.similarity_discriminator(a, b, informative) == 0.0
