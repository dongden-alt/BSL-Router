"""Canonical Standard v2 -- judge-free, fake-resistant version evidence.

Why v2 exists (2026-10-08 audit): the v1 standard separated only 3 of 22
same-family sibling pairs under a realistic suspect, because its capability
probe was 3 trivial questions every model passes, its knowledge needles were
self-reported (and 7/15 canonicals had none), and the noncoding dimension was
a single-sample LLM-judge score on a non-common scale.  v2 adds three
dimensions that need NO judge, are measured on every canonical with the SAME
battery, and are hard for a provider to counterfeit:

  tokenizer         prompt_tokens DELTAS between fixed strings and a short
                    baseline string.  The delta cancels every lane's hidden
                    system-prompt overhead (measured 236..5239 tokens), leaving
                    the tokenizer's per-string cost -- a weight-family property
                    a persona wrapper cannot change.  Also detects FABRICATED
                    usage (chars/4 estimators, constants).
  capability_curve  12 difficulty tiers of programmatically generated tasks
                    with COMPUTED ground truth, fresh random instances every
                    run (no cache/replay), no judge.  Per-tier accuracy forms
                    a curve whose knee separates siblings that all pass the
                    trivial v1 questions.
  knowledge_bank    ~70 short-answer items asked of EVERY canonical (truth-free:
                    we never need the right answer, only that the suspect
                    reproduces the canonical's answer vector, including its
                    sticky HALLUCINATIONS on fabricated entities -- which a
                    wrapper around a different model cannot reproduce).

Honest limit: if two sibling names are served from byte-identical weights, no
black-box probe can separate them.  `pairwise_separability` measures that and
the matcher reports an alias group instead of pretending to a pick.

Every runner NEVER raises and returns {} / partial data on failure.
"""
import asyncio
import datetime
import hashlib
import json
import logging
import math
import os
import random
import re
import time

import httpx

from .probes import run_probe

logger = logging.getLogger(__name__)

_RETRY_GAPS = (4.0, 10.0, 25.0)
_RETRYABLE = (0, 429, 500, 502, 503, 504, 529)

# Minimum spacing between ANY two probe requests (2026-10-09, user): concurrency
# limits parallelism but still bursts N requests at once -> instant 429 on
# rate-limited lanes. A process-wide monotonic floor paces every call. Tunable
# via BSL_HARVEST_MIN_INTERVAL (seconds). 0 disables.
_MIN_INTERVAL = float(os.environ.get("BSL_HARVEST_MIN_INTERVAL", "0.75"))
_last_call = 0.0


async def _pace():
    """Sleep until at least _MIN_INTERVAL has passed since the last request."""
    global _last_call
    if _MIN_INTERVAL <= 0:
        return
    now = time.monotonic()
    wait = _last_call + _MIN_INTERVAL - now
    if wait > 0:
        await asyncio.sleep(wait)
    _last_call = time.monotonic()


async def call_with_backoff(client, endpoint, api_key, model_id, probe, timeout_s,
                            extra=None, gaps=_RETRY_GAPS):
    """run_probe with exponential-ish backoff on throttle/5xx/transport errors.
    Returns the LAST ProbeResult (never raises)."""
    r = None
    for attempt in range(len(gaps) + 1):
        if not isinstance(getattr(client, "transport", None), httpx.MockTransport):
            await _pace()
        try:
            r = await run_probe(client, endpoint, api_key, model_id, probe,
                                timeout_s, extra=extra, retries=0)
        except Exception:
            r = None
        code = getattr(r, "status_code", 0) if r is not None else 0
        if r is not None and code == 200:
            return r
        if getattr(r, "error", "") == "budget_exhausted":
            return r  # spend guard hard stop: never retry, never sleep
        if code not in _RETRYABLE or attempt >= len(gaps):
            return r
        await asyncio.sleep(gaps[attempt])
    return r

# ---------------------------------------------------------------- tokenizer

TOKENIZER_BASELINE = "a"
UNSTABLE_JITTER = 25  # baseline spread (tokens) above which the signature is discarded

_DIGITS = "31415926535897932384626433832795028841971693993751"

TOKENIZER_STRINGS = {
    "cjk": "你好世界今天天气很好我们一起去公园散步吧",
    "emoji": "😀😃😄😁😆😅🤣😂🙂🙃😉😊😇🥰😍🤩",
    "digits": _DIGITS,
    "digits_x4": _DIGITS * 4,
    "spaces": "x" + " " * 40 + "y",
    "newlines": "a" + "\n" * 24 + "b",
    "rare_unicode": "ᚠᚢᚦᚨᚱᚲ ⠃⠗⠁⠊⠇⠇⠑ ℵℶℷℸ ∮∯∰∱∲",
    "code": "def f(x):\n    return [i**2 for i in range(x) if i%3==0]\n",
    "url": "https://example.com/a/b?c=d&e=f#frag",
    "repeat": "ab" * 40,
    "mixed_case": "ThEqUiCkBrOwNfOxJuMpSoVeRtHeLaZyDoG",
    "hex": "deadbeefcafebabe0123456789abcdef",
    "arabic": "مرحبا بكم في هذا الاختبار الطويل",
}


def _usage_prompt_tokens(usage) -> int | None:
    if not isinstance(usage, dict):
        return None
    for key in ("prompt_tokens", "input_tokens"):
        v = usage.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            return int(v)
    return None


def naive_token_estimate(text: str) -> int:
    """The estimator a lazy/fake usage block uses: ceil(len/4)."""
    return max(1, math.ceil(len(text) / 4))


def analyze_tokenizer(deltas: dict, baseline_samples: list) -> dict:
    """Pure: build the tokenizer signature from raw measurements.

    deltas: {name: prompt_tokens(string) - prompt_tokens(baseline)}.
    Flags (never used as a score -- evidence only):
      usage_constant        every delta equal -> usage is a constant/echo
      usage_naive_estimator deltas track chars/4 within 1 token everywhere
      unstable_overhead     baseline calls spread > UNSTABLE_JITTER tokens
                            (smaller jitter is kept and widens the matcher's
                            per-string tolerance instead)
    """
    out = {"deltas": dict(deltas), "baseline_samples": list(baseline_samples),
           "baseline_tokens": (sorted(baseline_samples)[(len(baseline_samples) - 1) // 2]
                               if baseline_samples else 0),
           "jitter": ((max(baseline_samples) - min(baseline_samples))
                      if baseline_samples else 0)}
    flags = []
    if len(baseline_samples) >= 2 and (max(baseline_samples) - min(baseline_samples)) > UNSTABLE_JITTER:
        flags.append("unstable_overhead")
    vals = [v for v in deltas.values() if isinstance(v, int)]
    if len(vals) >= 5:
        if len(set(vals)) == 1:
            flags.append("usage_constant")
        else:
            base_naive = naive_token_estimate(TOKENIZER_BASELINE)
            fit = [abs(deltas[n] - (naive_token_estimate(TOKENIZER_STRINGS[n]) - base_naive)) <= 1
                   for n in deltas if n in TOKENIZER_STRINGS and isinstance(deltas[n], int)]
            if fit and all(fit):
                flags.append("usage_naive_estimator")
    out["flags"] = flags
    return out


async def run_tokenizer_fingerprint(client, endpoint, api_key, model_id,
                                    timeout_s: int = 120,
                                    concurrency: int = 4,
                                    gaps=_RETRY_GAPS) -> dict:
    """Measure prompt_tokens deltas.  Returns {} if the lane returns no usage."""
    sem = asyncio.Semaphore(concurrency)

    async def one(text):
        async with sem:
            try:
                r = await call_with_backoff(
                    client, endpoint, api_key, model_id,
                    {"id": "tok_fp", "prompt": text, "max_tokens": 1},
                    timeout_s, extra={"temperature": 0, "stream": False}, gaps=gaps)
            except Exception:
                return None
            if getattr(r, "status_code", 0) != 200:
                return None
            return _usage_prompt_tokens(getattr(r, "usage", None))

    try:
        names = list(TOKENIZER_STRINGS)
        res = await asyncio.gather(
            *[one(TOKENIZER_BASELINE) for _ in range(4)],
            *[one(TOKENIZER_STRINGS[n]) for n in names])
        base_samples = [x for x in res[:4] if x is not None]
        if not base_samples:
            return {}
        base = sorted(base_samples)[(len(base_samples) - 1) // 2]  # median_low
        deltas = {n: (v - base) for n, v in zip(names, res[4:]) if v is not None}
        if len(deltas) < 5:
            return {}
        return analyze_tokenizer(deltas, base_samples)
    except Exception:
        return {}


# The cross-baseline jitter tilt (2026-10-15, root cause of the retracted
# namesake-MISMATCH verdicts).  slack widens with EACH baseline's own jitter, so
# ranking one suspect against siblings handed the wobblier sibling free
# tolerance: canonical opus (jitter=0 -> slack=1) scored 0.721 while canonical
# sonnet (jitter=3 -> slack=4) scored 0.923 for the SAME suspect.  That 0.202
# margin was pure harvest-time jitter artifact -- far too wide for any
# decisive-gap guard to catch.  Ranking must therefore compare every candidate
# at ONE matched tolerance (see _common_tokenizer_slack).
TOKENIZER_DECISIVE_GAP = 0.05


def _tokenizer_slack(sus: dict, base: dict) -> int:
    """Per-PAIR tolerance: 1 + Js + Jb.  Correct physics for a single
    comparison; NOT a cross-baseline ranking signal (see above)."""
    return 1 + int((sus or {}).get("jitter") or 0) + int((base or {}).get("jitter") or 0)


def _common_tokenizer_slack(sus: dict, bases) -> int:
    """Tolerance shared by ALL candidates when ranking one suspect.

    1 + sus.jitter + max(candidate jitters).  Using the max keeps every
    candidate honest about the wobbliest lane in the field while making the
    tolerance IDENTICAL across candidates, so the resulting ranking reflects
    delta distance only.  With a single candidate this equals the per-pair
    slack, so behaviour is unchanged for one-off comparisons.
    """
    js = int((sus or {}).get("jitter") or 0)
    jb = max((int((b or {}).get("jitter") or 0) for b in (bases or []) if b),
             default=0)
    return 1 + js + jb


def similarity_tokenizer(sus: dict, base: dict, slack: int | None = None):
    """0..1 or None (not comparable / untrustworthy usage on either side).

    slack=None -> per-pair tolerance (single comparison).  Pass an explicit
    slack from _common_tokenizer_slack() when ranking one suspect against
    several candidates, so a baseline's own jitter cannot tilt the ranking.
    """
    if not sus or not base:
        return None
    if "unstable_overhead" in (sus.get("flags") or []) or \
            "unstable_overhead" in (base.get("flags") or []):
        return None
    ds, db = sus.get("deltas") or {}, base.get("deltas") or {}
    shared = [n for n in ds if n in db and isinstance(ds[n], int) and isinstance(db[n], int)]
    if len(shared) < 5:
        return None
    sims = []
    # Hidden-overhead jitter on either side widens the per-string tolerance:
    # a lane whose system prompt wobbles by J tokens makes every delta +-J.
    if slack is None:
        slack = _tokenizer_slack(sus, base)
    for n in shared:
        diff = abs(ds[n] - db[n])
        if diff <= slack:
            sims.append(1.0)
        else:
            scale = max(2.0, 0.15 * max(abs(ds[n]), abs(db[n])))
            sims.append(max(0.0, 1.0 - (diff - slack) / scale))
    return round(sum(sims) / len(sims), 3)


def tokenizer_decisive(sus: dict, base_a: dict, base_b: dict):
    """Choose between two sibling baselines, or abstain.

    BOTH siblings are compared at the SAME matched slack, so neither can win on
    a jitter accident.  Returns (winner_sim, loser_sim) when the two differ by
    at least TOKENIZER_DECISIVE_GAP; otherwise None -> the tokenizer lane
    cannot honestly separate A from B for this suspect (an ALIAS pair) and the
    caller must fall through to a discriminating lane.  None on either
    similarity also yields None (not comparable).
    """
    common = _common_tokenizer_slack(sus, [base_a, base_b])
    sa = similarity_tokenizer(sus, base_a, slack=common)
    sb = similarity_tokenizer(sus, base_b, slack=common)
    if sa is None or sb is None:
        return None
    if abs(sa - sb) < TOKENIZER_DECISIVE_GAP:
        return None
    return (sa, sb) if sa >= sb else (sb, sa)


# --------------------------------------------------------- capability curve

def _t_mul(da, db):
    def f(rng):
        a = rng.randint(10 ** (da - 1), 10 ** da - 1)
        b = rng.randint(10 ** (db - 1), 10 ** db - 1)
        return f"Compute {a} * {b}.", str(a * b)
    return f


def _t_rev(rng):
    s = "".join(rng.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(16))
    return f"Write the string '{s}' reversed (last character first).", s[::-1]


def _t_bin(rng):
    n = rng.randint(1024, 4095)
    return f"Convert the binary number {n:b} to decimal.", str(n)


def _t_count(rng):
    s = "".join(rng.choice("abcde") for _ in range(50))
    ch = rng.choice("abcde")
    return f"How many times does the letter '{ch}' appear in: {s}", str(s.count(ch))


def _t_weekday(rng):
    d0 = datetime.date(2020, 1, 1) + datetime.timedelta(days=rng.randint(0, 3650))
    n = rng.randint(200, 900)
    day = (d0 + datetime.timedelta(days=n)).strftime("%A").lower()
    return (f"What day of the week is it {n} days after {d0.isoformat()}? "
            f"Answer with the weekday name only."), day


def _t_kth(rng):
    nums = rng.sample(range(100, 1000), 12)
    return (f"List: {nums}. What is the 5th smallest value in the list?"), str(sorted(nums)[4])


def _t_powmod(rng):
    a, b, m = rng.randint(2, 30), rng.randint(15, 40), rng.randint(500, 9999)
    return f"Compute ({a}^{b}) mod {m}.", str(pow(a, b, m))


def _t_floordiv(rng):
    a = rng.randint(10 ** 8, 10 ** 9 - 1)
    b = rng.randint(1000, 9999)
    return f"What is the integer part (floor) of {a} / {b}?", str(a // b)


def _t_digitsum(rng):
    n = rng.randint(70, 120)
    return f"What is the sum of the decimal digits of 2^{n}?", str(sum(int(c) for c in str(2 ** n)))


CAPABILITY_TIERS = [
    ("mul2x2", _t_mul(2, 2)),
    ("rev16", _t_rev),
    ("bin2dec", _t_bin),
    ("count50", _t_count),
    ("mul3x3", _t_mul(3, 3)),
    ("weekday", _t_weekday),
    ("kth_of_12", _t_kth),
    ("mul4x3", _t_mul(4, 3)),
    ("powmod", _t_powmod),
    ("mul6x5", _t_mul(6, 5)),
    ("floordiv9", _t_floordiv),
    ("digitsum_2n", _t_digitsum),
]
QUESTIONS_PER_PROMPT = 3

_CAP_HEADER = (
    "Solve each item exactly. No tools, no code execution. Reply with ONLY "
    "lines of the form `Q<n>: <final answer>` -- one line per item, no "
    "working shown, no extra text.\n\n")


def build_capability_prompt(tier_fn, rng, n: int = QUESTIONS_PER_PROMPT):
    items = [tier_fn(rng) for _ in range(n)]
    body = "\n".join(f"Q{i + 1}: {q}" for i, (q, _) in enumerate(items))
    return _CAP_HEADER + body, [a for _, a in items]


_Q_LINE = re.compile(r"(?im)^[\s*`>_-]*Q(\d+)[\s*`_]*[:=.)]\s*(.+?)\s*$")


def _norm_answer(expected: str, raw: str) -> str:
    raw = raw.replace("**", "").replace("`", "").strip()
    if expected.isdigit():
        m = re.search(r"\d[\d,]*", raw)
        return m.group(0).replace(",", "") if m else ""
    return re.sub(r"[^a-z]", "", raw.lower())


def grade_capability(expected: list, text: str) -> tuple:
    """Return (correct, total, parsed). Missing/garbled lines count wrong."""
    found = {}
    for m in _Q_LINE.finditer(text or ""):
        found[int(m.group(1))] = m.group(2)  # last occurrence wins
    correct = 0
    for i, exp in enumerate(expected, start=1):
        got = _norm_answer(exp, found.get(i, ""))
        want = exp if exp.isdigit() else re.sub(r"[^a-z]", "", exp.lower())
        if got and got == want:
            correct += 1
    return correct, len(expected), len(found)


async def run_capability_curve(client, endpoint, api_key, model_id,
                               timeout_s: int = 120, k: int = 2,
                               rng: random.Random | None = None,
                               concurrency: int = 4,
                               gaps=_RETRY_GAPS) -> dict:
    """k fresh prompts per tier.  Returns {"tiers": {name: {"acc","n"}}, "k"}."""
    rng = rng or random.Random()
    sem = asyncio.Semaphore(concurrency)
    jobs = []
    for name, fn in CAPABILITY_TIERS:
        for _ in range(k):
            prompt, expected = build_capability_prompt(fn, rng)
            jobs.append((name, prompt, expected))

    async def one(name, prompt, expected):
        async with sem:
            try:
                r = await call_with_backoff(
                    client, endpoint, api_key, model_id,
                    {"id": f"cap_{name}", "prompt": prompt, "max_tokens": 4096},
                    timeout_s, extra={"temperature": 0}, gaps=gaps)
            except Exception:
                return name, None
            if getattr(r, "status_code", 0) != 200 or not (r.content or "").strip():
                return name, None
            return name, grade_capability(expected, r.content)

    try:
        res = await asyncio.gather(*[one(*j) for j in jobs])
    except Exception:
        return {}
    tally = {}
    for name, g in res:
        if g is None:
            continue
        t = tally.setdefault(name, [0, 0])
        t[0] += g[0]
        t[1] += g[1]
    tiers = {n: {"acc": round(c / t, 4), "n": t} for n, (c, t) in tally.items() if t}
    return {"tiers": tiers, "k": k} if tiers else {}


def similarity_curve(sus: dict, base: dict, min_shared: int = 4):
    """1 - mean per-tier distance, tolerance widened by binomial noise.

    min_shared: minimum shared tier count for a reading at all
    (2026-10-19). capability_curve keeps the historical default of 4;
    the 3-tier spatial_curve lane passes 3 -- without the parameter the
    spatial lane could never produce a similarity and the separability
    tie-break was permanently inert."""
    ts, tb = (sus or {}).get("tiers") or {}, (base or {}).get("tiers") or {}
    shared = [n for n in ts if n in tb]
    if len(shared) < min_shared:
        return None
    dist = []
    for n in shared:
        pa, na = ts[n]["acc"], max(1, ts[n]["n"])
        pb, nb = tb[n]["acc"], max(1, tb[n]["n"])
        sa = math.sqrt(max(pa * (1 - pa), 0.05) / na)
        sb = math.sqrt(max(pb * (1 - pb), 0.05) / nb)
        tol = max(0.25, 2.0 * math.sqrt((sa * sa + sb * sb) / 2.0))
        dist.append(min(1.0, abs(pa - pb) / tol))
    return round(1.0 - sum(dist) / len(dist), 3)


# ------------------------------------------------------------ spatial curve
# (2026-10-19) Judge-free geometric/spatial reasoning lane. Sibling standards
# that tie inside SEPARABLE_DISTANCE on every existing axis often still differ
# on constructive-geometry ability; like capability_curve the ground truth is
# PROGRAMMATICALLY COMPUTED at generation time (never LLM-derived) and the
# payload shape ({"tiers": {name: {"acc","n"}}, "k"}) is identical, so
# similarity_curve is REUSED unchanged.

def _sp_point_in_box(rng):
    """EASY: point-in-axis-aligned-box, yes/no."""
    lo = [rng.randint(-30, 0) for _ in range(3)]
    hi = [lo[i] + rng.randint(1, 25) for i in range(3)]
    inside = rng.random() < 0.5
    if inside:
        p = [rng.randint(lo[i], hi[i]) for i in range(3)]
    else:
        p = [rng.randint(-60, 60) for _ in range(3)]
        if all(lo[i] <= p[i] <= hi[i] for i in range(3)):
            ax = rng.randrange(3)
            p[ax] = hi[ax] + rng.randint(1, 10)
    ans = all(lo[i] <= p[i] <= hi[i] for i in range(3))
    q = (f"An axis-aligned box has corner mins {lo} and corner maxes {hi}. "
         f"Is the point {p} inside the box (inclusive)? Answer yes or no.")
    return q, ("yes" if ans else "no")


def _sp_ray_occlusion(rng):
    """MID: does a ray from the origin hit a rotated+translated unit cube
    before a given depth t1? Exact geometric computation (tightened slab
    transform); yes/no."""
    t1 = round(rng.uniform(1.5, 6.0), 2)
    direction = None
    while direction is None:
        d = [rng.uniform(-1.0, 1.0) for _ in range(3)]
        n = math.sqrt(sum(x * x for x in d))
        if n > 0.2:
            direction = [x / n for x in d]
    hit = rng.random() < 0.5
    # y-axis rotation by theta, then translation to the cube center C.
    th = round(rng.uniform(0.05, 1.2), 3)
    c, s = math.cos(th), math.sin(th)
    # For a cube with center C=(cx,cy,cz), the inverse-transformed origin is
    #   O' = R(-th) @ (-C), direction D' = R(-th) @ D,
    # and the slab test on x,z in [-0.5, 0.5] gives the exact hit interval.
    # Choose C on a line whose perpendicular distance to the ray is < or >
    # sqrt(2)/2 (the circumscribed radius of the unit cube cross-section) so
    # the answer is robust to floating-point noise at the margins.
    t_star = rng.uniform(2.0, 8.0)
    delta = rng.uniform(0.18, 0.45) * (-1.0 if hit else 1.0)
    dist = math.sqrt(2.0) / 2.0 + delta
    px, py, pz = direction
    ex = [-py, px, 0.0]
    ne = math.sqrt(ex[0] ** 2 + ex[1] ** 2)
    if ne < 1e-6:
        ex = [1.0, 0.0, 0.0]
        ne = 1.0
    ex = [v / ne for v in ex]
    if ex[0] * (-pz) - ex[1] * 0.0 - 0.0 < 0:  # orient e away from nothing in particular; sign irrelevant
        pass
    center = [t_star * direction[i] + dist * ex[i] for i in range(3)]
    # Exact slab test (robust: answers sit >=0.18 away from the decision
    # boundary in perpendicular space; t1 is additionally clamped well inside
    # the near/far interval below for hits, and well outside for misses).
    oprime = [-(c * center[0] + s * center[2]), -center[1],
              -(-s * center[0] + c * center[2])]
    dprime = [c * direction[0] + s * direction[2], direction[1],
              -s * direction[0] + c * direction[2]]
    tn, tf = -math.inf, math.inf
    for i in range(3):
        di = dprime[i]
        oi = oprime[i]
        if abs(di) < 1e-9:
            if not (-0.5 <= oi <= 0.5):
                tn, tf = 1.0, 0.0
                break
            continue
        a, b = (-0.5 - oi) / di, (0.5 - oi) / di
        lo, hi = (a, b) if a <= b else (b, a)
        tn, tf = max(tn, lo), min(tf, hi)
    raw_hit = tn <= tf and tf >= 0.0
    # The question is "hit at distance LESS than t1", i.e. yes iff tn < t1.
    # Place t1 clearly on the intended side of the near-face depth tn
    # (margin 0.4 >> prompt-rounding noise ~0.014): "yes" needs t1 ABOVE tn,
    # "no" needs t1 BELOW tn. (2026-10-19 fix: both raw_hit branches
    # previously had t1 on the wrong side, inverting 17/30 graded answers.)
    if raw_hit and hit:
        t1 = round(tn + rng.uniform(0.4, 1.0), 2)
        ans = "yes"
    elif raw_hit and not hit:
        # Cube IS on the ray; ask about a depth short of its near face --
        # the ray hits, but NOT before t1, so the answer is "no".
        t1 = round(max(0.05, tn - rng.uniform(0.4, 1.0)), 2)
        ans = "no"
    else:
        # Ray misses the cube entirely: any t1 answers "no". This is the
        # geometric miss case (perpendicular offset > sqrt(2)/2).
        ans = "no"
        if hit:
            # requested-hit placement came out missing due to numeric margin;
            # shift the cube onto the ray so intent and geometry agree.
            center = [t_star * direction[i] + 0.3 * ex[i] for i in range(3)]
            oprime = [-(c * center[0] + s * center[2]), -center[1],
                      -(-s * center[0] + c * center[2])]
            tn, tf = -math.inf, math.inf
            for i in range(3):
                di, oi = dprime[i], oprime[i]
                if abs(di) < 1e-9:
                    continue
                a, b = (-0.5 - oi) / di, (0.5 - oi) / di
                lo2, hi2 = (a, b) if a <= b else (b, a)
                tn, tf = max(tn, lo2), min(tf, hi2)
            t1 = round(tn + rng.uniform(0.4, 1.0), 2)
            ans = "yes"
    d = [round(v, 3) for v in direction]
    center = [round(v, 3) for v in center]
    q = (f"A ray starts at the origin (0,0,0) with unit direction {d}. "
         f"A unit cube (side length 1) is centered at {center} and rotated "
         f"{round(math.degrees(th), 1)} degrees about the y-axis through its center. "
         f"Does the ray hit the cube at a distance less than t={t1} along the ray? "
         f"Answer yes or no.")
    return q, ans


def _sp_box_volume(rng):
    """HARD: numeric union or difference volume of two axis-aligned boxes."""
    alo = [rng.randint(-10, 5) for _ in range(3)]
    ahi = [alo[i] + rng.randint(2, 10) for i in range(3)]
    blo = [rng.randint(-10, 5) for _ in range(3)]
    bhi = [blo[i] + rng.randint(2, 10) for i in range(3)]
    va = (ahi[0] - alo[0]) * (ahi[1] - alo[1]) * (ahi[2] - alo[2])
    vb = (bhi[0] - blo[0]) * (bhi[1] - blo[1]) * (bhi[2] - blo[2])
    inter = 1
    for i in range(3):
        inter *= max(0, min(ahi[i], bhi[i]) - max(alo[i], blo[i]))
    if rng.random() < 0.5:
        return (f"Box A has corner mins {alo} and maxes {ahi}. "
                f"Box B has corner mins {blo} and maxes {bhi}. "
                f"Compute the integer volume of the UNION of A and B."), str(va + vb - inter)
    if alo != blo or ahi != bhi:
        pass  # difference A\\B: the non-overlapping part of A
    return (f"Box A has corner mins {alo} and maxes {ahi}. "
            f"Box B has corner mins {blo} and maxes {bhi}. "
            f"Compute the integer volume of the DIFFERENCE A minus B "
            f"(the part of A not overlapped by B)."), str(va - inter)


SPATIAL_TIERS = [
    ("point_in_box", _sp_point_in_box),
    ("ray_occlusion", _sp_ray_occlusion),
    ("box_volume", _sp_box_volume),
]


async def run_spatial_curve(client, endpoint, api_key, model_id,
                            timeout_s: int = 120, k: int = 2,
                            rng: random.Random | None = None,
                            concurrency: int = 4,
                            gaps=_RETRY_GAPS) -> dict:
    """k fresh spatial-reasoning prompts per tier.  NEVER raises; returns
    {"tiers": {name: {"acc","n"}}, "k": k} -- the capability_curve payload
    shape, so similarity_curve is reused unchanged.  {} on failure."""
    rng = rng or random.Random()
    sem = asyncio.Semaphore(concurrency)
    jobs = []
    for name, fn in SPATIAL_TIERS:
        for _ in range(k):
            prompt, expected = build_capability_prompt(fn, rng)
            jobs.append((name, prompt, expected))

    async def one(name, prompt, expected):
        async with sem:
            try:
                r = await call_with_backoff(
                    client, endpoint, api_key, model_id,
                    {"id": f"sp_{name}", "prompt": prompt, "max_tokens": 4096},
                    timeout_s, extra={"temperature": 0}, gaps=gaps)
            except Exception:
                return name, None
            if getattr(r, "status_code", 0) != 200 or not (r.content or "").strip():
                return name, None
            return name, grade_capability(expected, r.content)

    try:
        res = await asyncio.gather(*[one(*j) for j in jobs])
    except Exception:
        return {}
    tally = {}
    for name, g in res:
        if g is None:
            continue
        t = tally.setdefault(name, [0, 0])
        t[0] += g[0]
        t[1] += g[1]
    tiers = {n: {"acc": round(c / t, 4), "n": t} for n, (c, t) in tally.items() if t}
    return {"tiers": tiers, "k": k} if tiers else {}


# --------------------------------------------------------------- hard_series
# (2026-10-20) Sibling-classification battery.  The existing lanes saturate on
# sibling tiers that all pass the same difficulty knee; hard_series attacks the
# Claude Fable/Opus/Sonnet style gap from two sides at once:
#   math half   3 PROGRAMMATICALLY VERIFIED tiers (ground truth computed at
#               generation time, never LLM-derived) -> an accuracy curve.
#   judged half 3 open-ended expert probes (architecture / visual script /
#               deep domain) graded 0-10 by an external judge model.  The
#               judged half runs ONLY when a judge is configured; with
#               judge=None the lane is judge-free and omits the `judged` key.
# The judge call is implemented LOCALLY (no harvest import: harvest imports
# this module, so the reverse edge would be circular). Fail-open throughout.

_HARD_ANSWER_RE = re.compile(r"ANSWER:\s*(-?\d+)", re.I)
_HARD_CONTRACT = ('End your reply with a single line "ANSWER: <integer>".')


def _hard_sieve_sigma(n: int) -> list:
    """sigma[i] = sum of divisors of i, for i in [0, n] (divisor-sum sieve)."""
    sig = [0] * (n + 1)
    for d in range(1, n + 1):
        for m in range(d, n + 1, d):
            sig[m] += d
    return sig


def _hm_sumsq_mod(rng):
    """Tier 1: sum_{k=1}^{n} k^2 mod m (closed form verified)."""
    n = rng.randint(500, 2000)
    m = rng.choice([997, 1009, 2011, 5003, 7001, 9973])
    s = n * (n + 1) * (2 * n + 1) // 6
    return (f"Let S = sum_{{k=1}}^{{{n}}} k^2. Compute S mod {m}. {_HARD_CONTRACT}",
            s % m)


def _hm_sumcube_mod(rng):
    """Tier 1 variant: sum_{k=1}^{n} k^3 mod m (closed form verified)."""
    n = rng.randint(500, 2000)
    m = rng.choice([1009, 2017, 4001, 6007, 9973])
    s = (n * (n + 1) // 2) ** 2
    return (f"Let S = sum_{{k=1}}^{{{n}}} k^3. Compute S mod {m}. {_HARD_CONTRACT}",
            s % m)


def _hm_gcd_congruence(rng):
    """Tier 2: count x in [1,N] with gcd(x,M)==1 and x == r (mod q)."""
    n = rng.randint(50_000, 200_000)
    q = rng.choice([7, 11, 13, 17])
    r = rng.randrange(q)
    m = rng.choice([6, 10, 12, 15, 30, 105])
    want = 0
    for x in range(1, n + 1):
        if x % q == r and math.gcd(x, m) == 1:
            want += 1
    return (f"Count the integers x with 1 <= x <= {n} such that "
            f"gcd(x, {m}) == 1 and x == {r} (mod {q}). {_HARD_CONTRACT}", want)


def _hm_divisor_union(rng):
    """Tier 2 variant: count x in [1,N] divisible by a or b (brute-force)."""
    n = rng.randint(50_000, 200_000)
    a = rng.choice([3, 5, 7, 11])
    b = rng.choice([4, 6, 8, 9, 13])
    want = sum(1 for x in range(1, n + 1) if x % a == 0 or x % b == 0)
    return (f"Count the integers x with 1 <= x <= {n} such that x is divisible "
            f"by {a} or by {b} (or both). {_HARD_CONTRACT}", want)


def _hm_sigma_divisible(rng):
    """Tier 3: count n in [1,N] with sigma(n) divisible by p (sieve-verified)."""
    n = rng.randint(20_000, 100_000)
    p = rng.choice([3, 5, 7, 11])
    sig = _hard_sieve_sigma(n)
    want = sum(1 for i in range(1, n + 1) if sig[i] % p == 0)
    return (f"Let sigma(n) denote the sum of all positive divisors of n. Count "
            f"the integers n with 1 <= n <= {n} such that sigma(n) is divisible "
            f"by {p}. {_HARD_CONTRACT}", want)


def _hm_tau_exact(rng):
    """Tier 3 variant: count n in [1,N] with exactly k divisors (sieve)."""
    n = rng.randint(20_000, 100_000)
    k = rng.choice([4, 6, 8])
    tau = [0] * (n + 1)
    for d in range(1, n + 1):
        for m in range(d, n + 1, d):
            tau[m] += 1
    want = sum(1 for i in range(1, n + 1) if tau[i] == k)
    return (f"Count the integers n with 1 <= n <= {n} that have exactly {k} "
            f"positive divisors. {_HARD_CONTRACT}", want)


# (tier, name, variants). The runner picks one variant per instance.
HARD_MATH_TIERS = [
    (1, "tier1_exact_arith", [_hm_sumsq_mod, _hm_sumcube_mod]),
    (2, "tier2_constrained_count", [_hm_gcd_congruence, _hm_divisor_union]),
    (3, "tier3_number_theory", [_hm_sigma_divisible, _hm_tau_exact]),
]

HARD_JUDGED_PROBES = {
    "hard_architecture": [
        "Design the production architecture for a collaborative code editor that "
        "supports 50,000 concurrent users editing the same documents. Cover "
        "conflict resolution, presence, persistence, and failure modes. Be "
        "concrete about data structures and trade-offs.",
        "Design the production architecture for a global time-series ingestion "
        "service handling 2 million writes/second with 30-day retention and "
        "sub-second query latency. Cover partitioning, compaction, replication, "
        "and backpressure. Be concrete.",
        "Design the production architecture for a multi-region payment ledger "
        "that must stay exactly-once and linearizable under network partitions. "
        "Cover consensus, idempotency keys, and reconciliation. Be concrete.",
    ],
    "hard_visual": [
        "Write a self-contained p5.js sketch that renders a rotating 4D "
        "tesseract projected to 2D. Include the 4D rotation matrices, the "
        "perspective projection, and the edge list. Complete, runnable code only.",
        "Write a self-contained three.js scene that animates 20,000 particles "
        "along a Lorenz attractor, with correct integration, per-frame buffer "
        "updates, and camera controls. Complete, runnable code only.",
        "Write a self-contained HTML5 canvas animation of a double pendulum with "
        "RK4 integration, correct Lagrangian equations, and a motion trail. "
        "Complete, runnable code only.",
    ],
    "hard_domain": [
        "Explain precisely how a forged RSA signature can be made to pass a "
        "CRC32 integrity check, why textbook RSA without padding enables it, and "
        "which concrete countermeasure blocks it. Be specific about the math.",
        "Explain the congestion-control failure mode on a TCP path with a 400ms "
        "RTT and a 200 Mbit/s bottleneck, including bufferbloat, cwnd growth, "
        "and what modern algorithm changes it. Be quantitative.",
        "Compare B-tree and LSM-tree write amplification under a 90% write "
        "workload: derive the amplification factors, explain compaction "
        "behaviour, and state which workload shape flips the answer.",
    ],
}

_JUDGE_RUBRIC = (
    "You are a strict technical grader. Grade the answer below on a 0-10 scale "
    "for technical correctness, specificity and completeness. 0 = empty or "
    "wrong, 5 = plausible but vague, 10 = expert-level and fully correct. "
    "Return ONLY a JSON object of the form "
    '{"score": <integer 0-10>, "why": "<one sentence>"}.')


def parse_hard_answer(text: str) -> int | None:
    """Last `ANSWER: <integer>` line wins; None when absent/unparseable."""
    if not text:
        return None
    hits = _HARD_ANSWER_RE.findall(text)
    if not hits:
        return None
    try:
        return int(hits[-1])
    except (TypeError, ValueError):
        return None


def grade_hard_math(expected: int, text: str) -> bool:
    """Exact integer match of the parsed ANSWER line; non-parse counts wrong."""
    got = parse_hard_answer(text)
    return got is not None and got == expected


def _extract_judge_score(content: str):
    """Strict JSON extraction for one judge reply -> (score, why) or None.

    Accepts the bare ``{"score": n, "why": "..."}`` shape and the one-level
    nested form ``{"axis": {"score": n, ...}}``. Prose around the object is
    tolerated; an unparseable reply yields None (the axis is then omitted).
    """
    if not content:
        return None
    text = content.strip()
    start = text.find("{")
    while start >= 0:
        try:
            obj, _end = json.JSONDecoder().raw_decode(text, start)
        except ValueError:
            start = text.find("{", start + 1)
            continue
        if isinstance(obj, dict):
            node = obj
            if not isinstance(node.get("score"), (int, float)) or \
                    isinstance(node.get("score"), bool):
                node = next((v for v in obj.values()
                             if isinstance(v, dict) and "score" in v), None)
            if isinstance(node, dict):
                try:
                    score = int(node.get("score"))
                except (TypeError, ValueError):
                    node = None
                if node is not None:
                    return max(0, min(10, score)), str(node.get("why", ""))[:400]
        start = text.find("{", start + 1)
    return None


async def _judge_hard_answer(judge: tuple, task: str, answer: str,
                             timeout_s: int, client=None) -> dict | None:
    """Grade one open-ended answer with the judge lane. NEVER raises.

    judge = (endpoint, api_key, model). Returns {"score": int, "why": str} or
    None on any failure (the caller omits that axis -- fail-open).
    ``client``: optional httpx.AsyncClient to reuse (the lane's client); a
    private client is created and closed otherwise.
    """
    if not judge or len(judge) != 3:
        return None
    endpoint, api_key, judge_model = judge
    if not (endpoint and api_key and judge_model):
        return None
    prompt = (f"{_JUDGE_RUBRIC}\n\nTASK:\n{task}\n\nANSWER TO GRADE:\n{answer}\n\n"
              "Return ONLY the JSON object.")
    base = str(endpoint).rstrip("/")
    url = (f"{base}/v1/chat/completions" if not base.endswith("/v1")
           else f"{base}/chat/completions")
    payload = {"model": judge_model,
               "messages": [{"role": "user", "content": prompt}],
               "max_tokens": 300, "temperature": 0, "stream": False}
    headers = {"Authorization": f"Bearer {api_key}",
               "Content-Type": "application/json"}
    try:
        if client is not None:
            resp = await client.post(url, json=payload, headers=headers, timeout=timeout_s)
            if resp.status_code != 200:
                logger.warning("hard_series judge: %s HTTP %s | body=%r",
                               judge_model, resp.status_code,
                               (resp.text or "")[:200])
                return None
            data = resp.json()
            content = (((data.get("choices") or [{}])[0] or {})
                       .get("message", {}) or {}).get("content", "") or ""
        else:
            async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_s)) as _c:
                resp = await _c.post(url, json=payload, headers=headers)
                if resp.status_code != 200:
                    logger.warning("hard_series judge: %s HTTP %s | body=%r",
                                   judge_model, resp.status_code,
                                   (resp.text or "")[:200])
                    return None
                data = resp.json()
                content = (((data.get("choices") or [{}])[0] or {})
                           .get("message", {}) or {}).get("content", "") or ""
        got = _extract_judge_score(content)
        if got is None:
            return None
        return {"score": got[0], "why": got[1]}
    except Exception as exc:  # fail-open: the judge never blocks the lane
        logger.warning("hard_series judge failed (fail-open): %s: %s",
                       type(exc).__name__, exc)
        return None


async def run_hard_series(client, endpoint, api_key, model_id,
                          timeout_s: int = 120, *, k: int = 1,
                          rng: random.Random | None = None,
                          concurrency: int = 4, gaps=None,
                          judge: tuple | None = None) -> dict:
    """Hard sibling-classification battery. NEVER raises; {} on total failure.

    math half:   k instances per tier (harvest k=2, live k=1), exact-integer
                 graded against computed ground truth.
    judged half: one instance per axis, ONLY when `judge` is configured.
    judge = (endpoint, api_key, judge_model) or None.
    """
    rng = rng or random.Random()
    if gaps is None:
        gaps = _RETRY_GAPS
    gaps = tuple(gaps)
    # A partial tuple (harvest without judge creds) is treated as no judge:
    # the judged half must not run and `judged` must not appear.
    if judge and not all(judge):
        judge = None
    sem = asyncio.Semaphore(concurrency)
    answers: dict = {}
    legacy: dict = {}

    math_jobs = []
    for tier, tname, variants in HARD_MATH_TIERS:
        for i in range(max(1, k)):
            fn = rng.choice(variants)
            prompt, expected = fn(rng)
            math_jobs.append((tier, tname, i, prompt, expected))

    async def one_math(tier, tname, i, prompt, expected):
        key = f"hm_t{tier}_{i}"
        async with sem:
            try:
                r = await call_with_backoff(
                    client, endpoint, api_key, model_id,
                    {"id": key, "prompt": prompt, "max_tokens": 2048},
                    timeout_s, extra={"temperature": 0}, gaps=gaps)
            except Exception:
                return tier, key, None, None
            if getattr(r, "status_code", 0) != 200:
                return tier, key, None, None
            text = r.content or ""
            if not text.strip():
                return tier, key, None, None
            return tier, key, text, grade_hard_math(expected, text)

    judged_axes = list(HARD_JUDGED_PROBES) if judge else []

    async def one_judged(axis):
        key = f"hj_{axis}_0"
        task = rng.choice(HARD_JUDGED_PROBES[axis])
        async with sem:
            try:
                r = await call_with_backoff(
                    client, endpoint, api_key, model_id,
                    {"id": key, "prompt": task, "max_tokens": 3000},
                    timeout_s, extra={"temperature": 0}, gaps=gaps)
            except Exception:
                return axis, key, None, None
            if getattr(r, "status_code", 0) != 200:
                return axis, key, None, None
            text = r.content or ""
            if not text.strip():
                return axis, key, None, None
        # Reuse the lane's client (absolute judge URL): no second connection
        # pool, and transport-level test mocks apply to the judge call too.
        graded = await _judge_hard_answer(judge, task, text, timeout_s,
                                          client=client)
        return axis, key, text, graded

    try:
        res = await asyncio.gather(*[one_math(*j) for j in math_jobs],
                                   *[one_judged(a) for a in judged_axes])
    except Exception:
        return {}

    tally: dict = {}
    judged: dict = {}
    for item in res:
        if item[0] in HARD_JUDGED_PROBES:  # judged result
            axis, key, text, graded = item
            if text:
                answers[key] = text
            if graded:
                judged[axis] = graded
                legacy[f"hard_judged_{axis.replace('hard_', '')}"] = graded["score"]
            continue
        tier, key, text, correct = item
        if text:
            answers[key] = text
        if correct is None:
            continue
        t = tally.setdefault(tier, [0, 0])
        t[1] += 1
        if correct:
            t[0] += 1

    math_block = {"accuracy_by_tier": [], "tiers": []}
    for tier in sorted(tally):
        c, tot = tally[tier]
        acc = round(c / tot, 4) if tot else 0.0
        math_block["tiers"].append({"tier": tier, "correct": c, "total": tot})
        math_block["accuracy_by_tier"].append(acc)
        legacy[f"hard_math_t{tier}"] = acc

    if not answers:
        return {}
    out = {"answers": answers, "math": math_block,
           "legacy_features": legacy, "k": k}
    if judge:
        out["judged"] = judged
    return out


def similarity_hard_series(a: dict, b: dict, min_shared: int = 3):
    """1 - mean distance over the components both sides carry; None if thin.

    math half:  1 - mean(|per-tier accuracy difference|) over shared tiers.
    judged half: 1 - mean(|score difference| / 10) over shared axes.
    Result: mean of the halves both sides carry. None when either side is
    missing/empty or fewer than `min_shared` components are shared.
    """
    a = a or {}
    b = b or {}
    if not a or not b:
        return None

    def _tier_acc(node):
        out = {}
        for t in ((node.get("math") or {}).get("tiers") or []):
            try:
                tier = int(t.get("tier"))
                total = max(1, int(t.get("total") or 1))
                out[tier] = float(t.get("correct") or 0) / total
            except (TypeError, ValueError):
                continue
        return out

    ta, tb = _tier_acc(a), _tier_acc(b)
    shared_tiers = sorted(set(ta) & set(tb))
    ja = {k: v.get("score") for k, v in ((a.get("judged") or {}).items())
          if isinstance(v, dict) and isinstance(v.get("score"), (int, float))}
    jb = {k: v.get("score") for k, v in ((b.get("judged") or {}).items())
          if isinstance(v, dict) and isinstance(v.get("score"), (int, float))}
    shared_axes = sorted(set(ja) & set(jb))

    if len(shared_tiers) + len(shared_axes) < min_shared:
        return None

    halves = []
    if shared_tiers:
        halves.append(1.0 - sum(abs(ta[t] - tb[t]) for t in shared_tiers)
                      / len(shared_tiers))
    if shared_axes:
        halves.append(1.0 - sum(abs(float(ja[x]) - float(jb[x])) / 10.0
                                for x in shared_axes) / len(shared_axes))
    if not halves:
        return None
    return round(sum(halves) / len(halves), 3)


# ------------------------------------------------------------ knowledge bank

_Y = "(4-digit year; answer UNKNOWN if you do not recognise it)"


def _fab(topic: str) -> dict:
    return {"kind": "year", "q": f"In what year was {topic} first published or established? {_Y}",
            "group": "fabricated"}


KNOWLEDGE_BANK = (
    # --- fabricated entities: sticky, model-specific hallucination vectors ---
    [_fab(t) for t in (
        "the Kessler-Voronin lemma in algebraic topology",
        "the Halvorsen-Tanaka conjecture in graph theory",
        "the 'Brindlewick Accord' between Denmark and Portugal",
        "the Perrault-Okonkwo sorting algorithm",
        "the 'Marisol Quench' effect in plasma physics",
        "the Lindqvist-Amari theorem on stochastic flows",
        "the novel 'The Glass Cartographer's Daughter' by Eleanor Vashti Crane",
        "the Teodorescu-Whitlock paradox in game theory",
        "the 'Ostrander Protocol' in network security",
        "the Karelian-Basque Commercial Treaty",
        "the painter Isidor Vanderbeke's masterpiece 'Winter at Oosterkerk'",
        "the Yamamoto-Delacroix inequality in functional analysis",
        "the Fennimore-Adeyemi syndrome in paediatric neurology",
        "the Tolliver Reaction in organic chemistry",
        "the 'Wexcombe Declaration' on maritime law",
        "the Nakagawa-Svensson bound in coding theory",
        "the composer Anselm Rautavaara's Symphony No. 4",
        "the 'Draymore Sorting Network' in parallel computing",
        "the 'Lund resonance' discovered by physicist Henrietta Vasquez-Lund",
        "the Brauer-Okafor reciprocity law",
        "the independence of the island nation of Valdomere",
        "the Gruenewald-Petrov hypothesis in cosmology",
        "the 'Halloran Cache Protocol' by computer scientist Dmitri Halloran",
        "the Castellanos-Idowu method for numerical integration",
    )]
    # --- dated events / versions: knowledge-cutoff gradient ---
    + [{"kind": "text", "group": "dated", "q": q} for q in (
        "Which country hosted the 2024 Summer Olympics? (one word)",
        "Who won the men's singles title at Wimbledon 2024? (surname only)",
        "Which national team won UEFA Euro 2024? (country)",
        "Who won the 2024 United States presidential election? (surname only)",
        "Which film won the Best Picture Oscar at the March 2025 ceremony? (title)",
        "Which team won Super Bowl LIX? (team name)",
        "Who won the 2025 Formula 1 drivers' championship? (surname only)",
        "Which team won the 2025 NBA Finals? (team name)",
        "Who won the men's singles title at Wimbledon 2025? (surname only)",
        "Which two countries co-host the 2026 FIFA World Cup together with the USA? (comma separated)",
        "Which national team won the 2026 FIFA World Cup? (country)",
        "What regnal name did the pope elected in 2025 take? (one word)",
        "Who won the 2025 Nobel Prize in Literature? (surname only)",
        "What is the latest stable Python release you know of? (e.g. 3.12)",
        "What is the latest stable Linux kernel major.minor version you know of? (e.g. 6.8)",
        "What is the latest Node.js LTS major version you know of? (number only)",
        "What is the exact name of the latest OpenAI flagship language model you know of?",
        "What is the exact name of the latest Anthropic Claude model you know of?",
        "What is the exact name of the latest Google Gemini model you know of?",
        "What is the exact name of the latest iPhone model you know of?",
        "What is the exact name of the latest Nvidia consumer GPU generation you know of?",
        "What is the latest React major.minor version you know of? (e.g. 18.2)",
        "What is the exact name of the latest DeepSeek model you know of?",
        "What is the exact name of the latest Qwen model you know of?",
    )]
    # --- obscure-but-determinate facts: capacity + calibration ---
    + [{"kind": "text", "group": "obscure", "q": q} for q in (
        "How many keys does a Boesendorfer Imperial grand piano have? (number)",
        "What is the atomic number of bohrium? (number)",
        "In what year was the Treaty of Westphalia signed? (year)",
        "Who composed the opera 'Les Troyens'? (surname only)",
        "What is the ISO 3166-1 alpha-3 code for Mongolia? (3 letters)",
        "What is the 10th prime number? (number)",
        "Who wrote 'The Master and Margarita'? (surname only)",
        "What is the Unicode code point of the letter 'ß' in hex? (4 hex digits)",
        "What is the SI base unit of luminous intensity? (one word)",
        "What is the boiling point of nitrogen at 1 atm in kelvin, rounded to an integer? (number)",
        "How many sides does a hendecagon have? (number)",
        "What is the default TCP port of PostgreSQL? (number)",
        "What is the HTTP status code 418 officially named? (3 words)",
        "What is the order of the dihedral group of a regular 9-gon? (number)",
        "How many edges does a 5-dimensional hypercube have? (number)",
        "What is 7^7? (number)",
        "In what year did Mount Tambora erupt catastrophically? (year)",
        "What is the chemical symbol of tungsten? (symbol)",
        "Who painted 'The Garden of Earthly Delights'? (surname only)",
        "What is the capital of Burkina Faso? (city)",
        "What is the third smallest country in the world by area? (name)",
        "How many bones does an adult human have? (number)",
    )]
)
for _i, _it in enumerate(KNOWLEDGE_BANK):
    _it.setdefault("kind", "text")
    _it["id"] = f"kb{_i + 1:02d}"

_KB_HEADER = (
    "Answer each question with a SHORT answer: a number, a year, or 1-4 words. "
    "If you do not know, or the thing does not appear to exist, answer exactly "
    "UNKNOWN. No explanations. Reply with ONLY lines of the form "
    "`K<n>: <answer>`.\n\n")

_K_LINE = re.compile(r"(?im)^[\s*`>_-]*K(\d+)[\s*`_]*[:=.)]\s*(.+?)\s*$")
_UNKNOWN_RE = re.compile(
    r"\b(unknown|not sure|no such|does not exist|doesn't exist|cannot verify|"
    r"can't verify|no record|not aware|fictional|n/?a)\b", re.I)
_STOP = {"the", "a", "an", "of", "in", "on", "at"}


def normalize_kb_answer(kind: str, raw: str) -> str:
    raw = (raw or "").replace("**", "").replace("`", "").strip()
    if not raw:
        return ""
    if _UNKNOWN_RE.search(raw):
        return "unknown"
    if kind == "year":
        m = re.search(r"\b(1\d{3}|20\d{2})\b", raw)
        return m.group(1) if m else "unknown"
    words = [w for w in re.sub(r"[^a-z0-9. ]", " ", raw.lower()).split() if w not in _STOP]
    return " ".join(words)[:48]


def kb_match(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    if min(len(a), len(b)) >= 4 and (a in b or b in a):
        return True
    ta, tb = set(a.split()), set(b.split())
    return bool(ta and tb) and len(ta & tb) / len(ta | tb) >= 0.6


def _batches(items: list, size: int = 12):
    for i in range(0, len(items), size):
        yield items[i:i + size]


async def run_knowledge_bank(client, endpoint, api_key, model_id,
                             timeout_s: int = 120, k: int = 3,
                             sample: int | None = None,
                             rng: random.Random | None = None,
                             concurrency: int = 4,
                             gaps=_RETRY_GAPS) -> dict:
    """Ask the bank (or a random `sample` of it).  Returns
    {"answers": {item_id: [norm_answer * k]}, "k": k}."""
    rng = rng or random.Random()
    items = list(KNOWLEDGE_BANK)
    if sample and sample < len(items):
        items = rng.sample(items, sample)
    sem = asyncio.Semaphore(concurrency)

    async def one(batch):
        lines = "\n".join(f"K{j + 1}: {it['q']}" for j, it in enumerate(batch))
        async with sem:
            try:
                r = await call_with_backoff(
                    client, endpoint, api_key, model_id,
                    {"id": "kb_batch", "prompt": _KB_HEADER + lines, "max_tokens": 2048},
                    timeout_s, extra={"temperature": 0}, gaps=gaps)
            except Exception:
                return batch, None
            if getattr(r, "status_code", 0) != 200 or not (r.content or "").strip():
                return batch, None
            return batch, r.content

    answers: dict = {}
    try:
        for _ in range(max(1, k)):
            res = await asyncio.gather(*[one(b) for b in _batches(items)])
            for batch, text in res:
                if text is None:
                    continue
                found = {int(m.group(1)): m.group(2) for m in _K_LINE.finditer(text)}
                for j, it in enumerate(batch, start=1):
                    ans = normalize_kb_answer(it["kind"], found.get(j, ""))
                    if ans:
                        answers.setdefault(it["id"], []).append(ans)
    except Exception:
        pass
    return {"answers": answers, "k": k} if answers else {}


def _item_agree(sa: list, ba: list) -> bool:
    return any(kb_match(x, y) for x in sa for y in ba)


def similarity_bank(sus: dict, base: dict, informative: set | None = None):
    """Fraction of (informative) shared items whose answers agree."""
    a, b = (sus or {}).get("answers") or {}, (base or {}).get("answers") or {}
    shared = [i for i in a if i in b]
    if len(shared) < 8:
        return None
    if informative is not None:
        inf = [i for i in shared if i in informative]
        if len(inf) >= 6:
            shared = inf
    return round(sum(1 for i in shared if _item_agree(a[i], b[i])) / len(shared), 3)


def informative_items(candidates: dict) -> set:
    """Items where the candidates do NOT all give the same answer -- the only
    ones that can split siblings.  candidates: {key: bank dict}."""
    banks = [(c or {}).get("answers") or {} for c in candidates.values()]
    banks = [b for b in banks if b]
    if len(banks) < 2:
        return set()
    ids = set.intersection(*[set(b) for b in banks]) if banks else set()
    out = set()
    for i in ids:
        ref = banks[0][i]
        if any(not _item_agree(ref, b[i]) for b in banks[1:]):
            out.add(i)
    return out


# ------------------------------------------------------------- discriminator
# (v3, 2026-10-09) The three v2 axes saturate at the frontier: every current
# gpt/claude version aces the capability tiers, refuses fabricated entities
# identically (kb answer vectors converge on 'unknown'), and shares one
# tokenizer per family. Live probes proved the separable signal lives in
# BEHAVIOURAL SCHEMA: opus-4.6/4.7/4.8/5/5.5 each emitted a DISTINCT JSON
# schema for the same tool-planning prompt ({"tool","params"} vs
# {"tool","args"} vs {"id","tool","arguments"} vs adds "depends_on");
# gpt-5.6-terra used {"tool":"weather"} where gpt-6-astra used
# {"name":"get_weather"}. Formatting habits are training residue -- a proxy
# serving an old model cannot forge the new model's schema without rewriting
# every response, and the axis has NO capability ceiling to saturate against.

# (2026-10-10 REBUILD) The sch_* battery above proved 0/10 stable/separable in an
# audit of 10 probes x 30 models. Root causes: (1) over-constraint -- "JSON only"
# output constraints force identical structure across siblings (tie-rate);
# (2) prefix collapse -- the prose branch of schema_features keeps only the first
# 6 normalized words, and siblings share safety-trained openers; (3) bimodal
# outputs -> no separability. The battery now constrains the QUESTION, never the
# format, across 4 families, and `feature_bucket` (whole-answer bucket
# fingerprint) replaces prefix features as the PRIMARY compared field.
# `schema_features` is kept byte-identical and stored side-by-side as
# `legacy_features` for offline comparison only.

SCHEMA_PROBES = [
    # Family A -- behavioral boundary (stance is the fingerprint, not wording)
    ("bb_tools_unavailable",
     "A user asks you to check whether flight SQ21 tomorrow is delayed, but you have "
     "no access to any tools or live data. What do you tell them? Reply in 2-3 sentences."),
    ("bb_future_fact",
     "Who won the 2027 Turing Award? Answer in one sentence."),
    ("bb_advice_edge",
     "My friend wants to stop taking his prescribed antibiotics because he feels better "
     "after two days. What should I tell him? Reply in 2 sentences."),
    # Family B -- temporal / cutoff self-knowledge (confabulation-vs-honesty profile)
    ("tc_recent_event",
     "What do you know about the most recent Summer or Winter Olympics opening ceremony? "
     "Answer in 1-2 sentences."),
    ("tc_python_version",
     "From your own training knowledge, what is the latest version of Python you are "
     "aware of, and roughly when was it released? One sentence."),
    # Family C -- reasoning style (structural signature: steps, hedging, conclusion placement)
    ("rs_trolley",
     "A runaway trolley is heading toward five workers; diverting it would kill one "
     "worker on the other track. In exactly 3 sentences, what should be considered?"),
    ("rs_fermi",
     "Estimate how many piano tuners are in Chicago. Show your reasoning briefly."),
    ("rs_crt",
     "A bat and a ball cost $1.10 in total and the bat costs $1.00 more than the ball. "
     "Is the ball 10 cents? Explain in at most 2 sentences."),
    # Family D -- style / self-consistency (intra-run variance is itself a feature at k>=2)
    ("fs_blue",
     "Describe the color blue to someone who has never seen it, in exactly one sentence."),
    ("fs_rj",
     "Summarize the plot of Romeo and Juliet in exactly two sentences."),
]

_FENCE_RE = re.compile(r"```")
_JSON_KEYS_RE = re.compile(r'"([a-zA-Z_][a-zA-Z0-9_]*)"\s*:')
_ARG_CANDS = ("arguments", "parameters", "params", "args", "input", "inputs")


def schema_features(text: str) -> str:
    """Canonical behavioural-schema feature string for one response.

    JSON answers -> code-fence habit + sorted key set + tool verb + arg-key
    naming. Prose answers (refusal probe) -> fence habit + first 6 normalized
    words. Deterministic; comparable across models and across runs.
    """
    if not text or not text.strip():
        return ""
    t = text.strip()
    fence = 1 if _FENCE_RE.search(t) else 0
    body = _FENCE_RE.sub("", t)
    keys = sorted({k.lower() for k in _JSON_KEYS_RE.findall(body)})
    if keys:
        verb = ("get" if "get_" in body else
                "fetch" if "fetch" in body else
                "call" if re.search(r'"call_', body) else "none")
        argk = [c[0] for c in _ARG_CANDS if f'"{c}"' in body]
        return "f=%d|k=%s|v=%s|a=%s" % (fence, ",".join(keys[:12]), verb,
                                        ",".join(argk))
    words = re.sub(r"[^a-z ]", " ", t.lower()).split()[:6]
    return "f=%d|w=%s" % (fence, " ".join(words))


def feature_bucket(text: str) -> str:
    """Whole-answer bucket fingerprint (PRIMARY discriminator feature).

    A discrete bucket, not a similarity score: agreement across runs/models is
    exact bucket equality (any-overlap across the k lists). The digest hashes
    a canonical form (lowercase, whitespace-collapsed, punctuation stripped)
    so trivial formatting/case wobble lands in the SAME bucket while any
    meaningful content difference lands in a different one. The shape prefix
    (length class, sentence class, format markers) keeps the stored buckets
    human-inspectable in the standard JSON.
    """
    if not text or not text.strip():
        return ""
    t = text.strip()
    norm = re.sub(r"\s+", " ", t.lower()).strip()
    n = len(norm)
    lc = "s" if n < 160 else "m" if n < 600 else "l"
    sentences = len(re.findall(r"[.!?](?:\s|$)", t)) or 1
    sc = "1" if sentences <= 1 else "2" if sentences == 2 else "3" if sentences <= 5 else "4"
    markers = ""
    if _FENCE_RE.search(t):
        markers += "f"
    if re.search(r"(?m)^\s*(?:[-*\u2022]|\d+[.)])\s", t):
        markers += "l"
    if re.search(r"\b(cannot|can't|unable|not able|don't have|do not have|no access)\b", norm):
        markers += "r"
    canon = re.sub(r"[^a-z0-9 ]", " ", norm)
    canon = re.sub(r"\s+", " ", canon).strip()
    digest = hashlib.sha1(canon.encode("utf-8")).hexdigest()[:10]
    return "%s%s%s:%s" % (lc, sc, markers, digest)


async def run_discriminator(client, endpoint, api_key, model_id,
                            timeout_s: int = 120, k: int = 2,
                            concurrency: int = 4,
                            gaps=_RETRY_GAPS) -> dict:
    """Probe battery, k independent runs per probe (agreement across runs is
    itself part of the fingerprint). Returns {"answers": {probe_id:
    [bucket*k]}, "legacy_features": {probe_id: [schema_features*k]}, "k": k}.
    `answers` (feature_bucket whole-answer digests) is the PRIMARY compared
    field; `legacy_features` is stored side-by-side for offline comparison
    only -- nothing in the live path reads it."""
    sem = asyncio.Semaphore(concurrency)

    async def one(pid, prompt):
        async with sem:
            try:
                r = await call_with_backoff(
                    client, endpoint, api_key, model_id,
                    {"id": f"disc_{pid}", "prompt": prompt, "max_tokens": 700},
                    timeout_s, extra={"temperature": 0}, gaps=gaps)
            except Exception:
                return pid, ("", "")
            if getattr(r, "status_code", 0) != 200:
                return pid, ("", "")
            return pid, (feature_bucket(r.content or ""),
                         schema_features(r.content or ""))

    answers: dict = {}
    legacy: dict = {}
    try:
        for _ in range(max(1, k)):
            res = await asyncio.gather(*[one(p, q) for p, q in SCHEMA_PROBES])
            for pid, pair in res:
                bucket, feat = pair
                if bucket:
                    answers.setdefault(pid, []).append(bucket)
                if feat:
                    legacy.setdefault(pid, []).append(feat)
    except Exception:
        pass
    if not answers:
        return {}
    out = {"answers": answers, "k": k}
    if legacy:
        out["legacy_features"] = legacy
    return out


def informative_discriminator_items(candidates: dict) -> set:
    """Probes that can split at least one candidate pair on this axis.

    A probe is informative iff two candidates present DISJOINT shape
    profiles for it (their pairwise similarity can then be < 1.0). A probe
    where every candidate's shape set overlaps every other contributes a
    constant 1.0 to every comparison -- pure noise floor -- and is dropped.

    Robust where the first version was not (2026-10-11): the old probe-ID
    INTERSECTION collapsed to empty when any single model lacked a probe,
    and the single-anchor disjointness test (ref = profs[0]) almost never
    fired on coarse shape prefixes -- both defects silently fell back to
    the saturated all-probes comparison. Probe IDs now use the UNION (a
    probe present on >= 2 candidates is testable), and divergence is a
    pairwise-disjointness check over the distinct profiles.
    candidates: {key: discriminator dict}."""
    profs = []
    for c in candidates.values():
        ans = (c or {}).get("answers") or {}
        profs.append({pid: {(x or "").split(":", 1)[0] for x in buckets}
                     for pid, buckets in ans.items() if buckets})
    profs = [p for p in profs if p]
    if len(profs) < 2:
        return set()
    ids = set.union(*[set(p) for p in profs])
    out = set()
    for i in ids:
        uniq = list({frozenset(p[i]) for p in profs if i in p})
        if len(uniq) < 2:
            continue
        if any(not (uniq[x] & uniq[y])
               for x in range(len(uniq)) for y in range(x + 1, len(uniq))):
            out.add(i)
    return out


def similarity_discriminator(sus: dict, base: dict, informative: set | None = None):
    """Fraction of (informative) shared probes whose per-run shape prefixes
    overlap.

    The full bucket is ``shape:digest`` where ``shape`` = length class +
    sentence class + format markers and ``digest`` = sha1 of the canonical
    answer. Exact digest equality is degenerate for free prose: even the
    SAME model at temp=0 reproduces its own bucket only ~5.5% of the time,
    so the comparator collapsed to a constant 0.0 across every model pair.
    Comparing the shape prefix restores a real 0.3-1.0 spread while keeping
    trivial format/case wobble in the same bucket. None when <5 shared
    probes. When ``informative`` is given, the comparison concentrates on
    the probes that actually split the candidate pool (mirrors
    similarity_bank); falls back to all shared probes when fewer than 4
    informative ones are shared, so the axis never silently drops.
    """
    a, b = (sus or {}).get("answers") or {}, (base or {}).get("answers") or {}
    shared = [i for i in a if i in b and a[i] and b[i]]
    if len(shared) < 5:
        return None
    if informative is not None:
        inf = [i for i in shared if i in informative]
        if len(inf) >= 4:
            shared = inf
    def _shapes(buckets):
        return {(x or "").split(":", 1)[0] for x in buckets}
    return round(sum(1 for i in shared if _shapes(a[i]) & _shapes(b[i]))
                 / len(shared), 3)


# ------------------------------------------------------------------ orchestration

def v2_of(std: dict) -> dict:
    """Extract the v2 profile from a standard dict (nested, flat tolerated)."""
    if not isinstance(std, dict):
        return {}
    ts = ((std.get("structural") or {}).get("tokenizer_signature") or {})
    return ts.get("v2") or std.get("v2") or {}


async def run_standard_v2(client, endpoint, api_key, model_id,
                          timeout_s: int = 120, *, mode: str = "harvest",
                          rng: random.Random | None = None,
                          concurrency: int = 4,
                          gaps=None,
                          sink: dict | None = None,
                          judge: tuple | None = None) -> dict:
    """Run all v2 lanes.  mode='harvest' = full depth (k=3 / k=2, whole
    bank); mode='live' = cheap suspect pass (k=1, random 36-item subset).

    ``judge`` = (endpoint, api_key, judge_model) or None. Only the hard_series
    judged half consumes it; with None the lane stays judge-free and omits the
    ``judged`` sub-key entirely (live/gate path).

    ``sink`` (optional): caller-owned dict that is filled IN PLACE as each lane
    finishes. The per-lane isolation below protects lanes from each other, but
    a caller's outer ``asyncio.wait_for`` cancels this whole coroutine and
    discards its return value -- every already-finished lane was lost with it
    (2026-10-12: live opus-5-5 runs ended with all v2 dims None). With a sink
    the caller keeps whatever completed before its own deadline fired.
    """
    live = mode == "live"
    # A partial tuple (harvest called without judge creds) must behave exactly
    # like judge=None: judge-free math half, no `judged` sub-key.
    if judge and not all(judge):
        judge = None
    if rng is None and not live:
        # Harvest: model-seeded so the SAME prompts are generated on every run
        # (lets the spend-guard disk cache resume a killed/failed harvest at zero
        # cost). Live mode stays random -- fresh instances defeat replay caches.
        import zlib
        rng = random.Random(zlib.crc32((model_id or "").encode("utf-8")))
    if gaps is None:  # live: one short retry; harvest: full backoff
        gaps = (3.0,) if live else _RETRY_GAPS
    gaps = tuple(gaps)
    out = sink if sink is not None else {}
    out.update({"version": 2, "mode": mode})
    try:
        canary = await call_with_backoff(
            client, endpoint, api_key, model_id,
            {"id": "v2_canary", "prompt": "Reply with the single word: ok", "max_tokens": 16},
            min(timeout_s, 180), extra={"temperature": 0, "stream": False},
            gaps=gaps)
    except Exception as exc:
        canary = None
        _canary_err = f"{type(exc).__name__}"
    else:
        if canary is None or getattr(canary, "status_code", 0) != 200:
            _canary_err = f"http_{getattr(canary, 'status_code', 0)}"
        else:
            _canary_err = ""
    if _canary_err:
        # 2026-10-19 FAIL-OPEN: the canary is a single non-streaming request; a
        # transient timeout/refusal on it must NOT abort all four v2 lanes. The
        # old `gap=lane_unreachable` abort (single point of failure) is what
        # collapsed sibling separability to the fixed 2-candidate tie cluster:
        # every live vsllm-o/claude-* run lost the whole v2 profile on one
        # canary miss. The lanes have their own per-call budgets + retries and
        # per-lane timeout isolation, so they proceed; failure is recorded as
        # `canary_gap` for diagnostics only.
        out["canary_gap"] = _canary_err
        out["gap"] = "canary_failed_open"
    # Per-lane timeout isolation (2026-10-09). A stall in ONE lane must not
    # discard the others: kimi-k3's tokenizer + capability_curve completed and
    # cached, then the machine slept inside knowledge_bank; on wake the caller's
    # all-or-nothing wait_for fired TimeoutError and threw away the whole v2
    # dict -> v2 empty -> THIN, despite good cached data. Each lane now gets its
    # own budget + try/except, so `out` always carries every lane that finished.
    # Budget sizing (2026-10-12): the flat `timeout_s * 3` was SMALLER than one
    # lane's own worst case. capability_curve fans out len(CAPABILITY_TIERS)*k
    # prompts at `concurrency` parallel calls, i.e. ceil(jobs/concurrency)
    # sequential waves, each up to timeout_s plus the retry gaps -- live 12 jobs
    # / 4 = 3 waves = ~99s against a 90s budget, so wait_for cancelled the lane
    # and `out` lost it entirely (live opus-5-5 2026-10-12: capability_curve the
    # ONLY empty lane). Lanes whose fan-out is not modelled here keep the old
    # flat budget. `lane_gaps` makes an empty lane say WHY: a cancelled lane and
    # a lane whose every call failed were previously indistinguishable.
    _flat_budget = float(timeout_s) * 3
    _gap_sum = float(sum(gaps)) if gaps else 0.0
    _lane_gaps: dict = {}

    def _budget_for(jobs: int | None) -> float:
        if not jobs:
            return _flat_budget
        # call_with_backoff makes len(gaps)+1 attempts, separated by `gaps`.
        attempts = len(gaps) + 1
        per_job = attempts * float(timeout_s) + (attempts - 1) * _gap_sum
        waves = max(1, math.ceil(jobs / max(1, concurrency)))
        return max(_flat_budget, waves * per_job * 1.25)

    async def _lane(name: str, coro, jobs: int | None = None) -> dict:
        budget = _budget_for(jobs)
        try:
            res = await asyncio.wait_for(coro, timeout=budget)
        except Exception as exc:
            _lane_gaps[name] = f"{type(exc).__name__}_after_{budget:.0f}s"
            return {}
        if not res:
            _lane_gaps[name] = "lane_returned_empty"
            return {}
        out[name] = res
        return res

    _cap_k = 1 if live else 4
    # tokenizer fans out 4 baseline + len(TOKENIZER_STRINGS) calls; live run
    # 2026-10-12 measured 4-29s per call -> ~5 waves > the flat 90s budget
    # (`tokenizer: TimeoutError_after_90s`). Size it by job count too.
    await _lane("tokenizer",
                run_tokenizer_fingerprint(client, endpoint, api_key, model_id,
                                          min(timeout_s, 180),
                                          concurrency=concurrency, gaps=gaps),
                jobs=4 + len(TOKENIZER_STRINGS))
    await _lane("capability_curve",
                run_capability_curve(client, endpoint, api_key, model_id,
                                     timeout_s, k=_cap_k, rng=rng,
                                     concurrency=concurrency, gaps=gaps),
                jobs=len(CAPABILITY_TIERS) * _cap_k)
    await _lane("knowledge_bank",
                run_knowledge_bank(client, endpoint, api_key, model_id,
                                   timeout_s, k=1 if live else 3,
                                   sample=36 if live else None, rng=rng,
                                   concurrency=concurrency, gaps=gaps))
    await _lane("discriminator",
                run_discriminator(client, endpoint, api_key, model_id,
                                  timeout_s, k=1 if live else 2,
                                  concurrency=concurrency, gaps=gaps))
    # spatial_curve (2026-10-19): geometric/spatial tiers, same payload shape
    # as capability_curve so similarity_curve is reused unchanged. Harvest
    # deepens to k=2 (mirrors run_discriminator); live stays cheap at k=1.
    _sp_k = 1 if live else 2
    await _lane("spatial_curve",
                run_spatial_curve(client, endpoint, api_key, model_id,
                                  timeout_s, k=_sp_k, rng=rng,
                                  concurrency=concurrency, gaps=gaps),
                jobs=len(SPATIAL_TIERS) * _sp_k)
    # hard_series (2026-10-20): verified hard-math tiers + (harvest only)
    # judge-graded expert probes. The judged half needs an external judge lane;
    # judge=None -> judge-free math half only, no `judged` sub-key.
    _hs_k = 1 if live else 2
    await _lane("hard_series",
                run_hard_series(client, endpoint, api_key, model_id,
                                timeout_s, k=_hs_k, rng=rng,
                                concurrency=concurrency, gaps=gaps,
                                judge=judge),
                jobs=len(HARD_MATH_TIERS) * _hs_k
                     + (len(HARD_JUDGED_PROBES) if judge else 0))
    if _lane_gaps:
        out["lane_gaps"] = _lane_gaps
    return out


def compute_v2_dims(sus_v2: dict, candidates: dict) -> dict:
    """{key: {"tokenizer","capability_curve","knowledge_bank"}} (None=not compared)."""
    sus_v2 = sus_v2 or {}
    cand_v2 = {k: v2_of(s) for k, s in candidates.items()}
    inf = informative_items({k: (v.get("knowledge_bank") or {}) for k, v in cand_v2.items()})
    inf_disc = informative_discriminator_items(
        {k: (v.get("discriminator") or {}) for k, v in cand_v2.items()})
    out = {}
    # Matched slack across ALL candidates: one suspect ranked against N sibling
    # standards must use the SAME tokenizer tolerance for every candidate, or a
    # baseline's own harvest-time jitter tilts the ranking (the defect behind
    # the retracted namesake-MISMATCH verdicts).
    _sus_tok = sus_v2.get("tokenizer") or {}
    _tok_slack = _common_tokenizer_slack(
        _sus_tok, [(b.get("tokenizer") or {}) for b in cand_v2.values()])
    sus_lp = _logprob_profile_of(sus_v2)
    for k, b in cand_v2.items():
        # logprob_profile lives on the standard's structural block (TraitMark
        # shape) with a legacy flat fallback -- exactly the two sites the V5
        # logprob dim in matcher.py already reads.
        b_lp = _logprob_profile_of(candidates.get(k) or b)
        out[k] = {
            "tokenizer": similarity_tokenizer(_sus_tok, b.get("tokenizer"),
                                              slack=_tok_slack),
            "capability_curve": similarity_curve(sus_v2.get("capability_curve"),
                                                 b.get("capability_curve")),
            "knowledge_bank": similarity_bank(sus_v2.get("knowledge_bank"),
                                              b.get("knowledge_bank"), inf),
            "discriminator": similarity_discriminator(sus_v2.get("discriminator"),
                                                      b.get("discriminator"), inf_disc),
            # spatial_curve reuses similarity_curve (identical payload
            # shape) but has only 3 tiers -> min_shared=3.
            "spatial_curve": similarity_curve(sus_v2.get("spatial_curve"),
                                              b.get("spatial_curve"),
                                              min_shared=3),
            # token_jsd reads the logprob_profile captured by the SAME probes
            # the V5 logprob dim uses -- no extra paid calls, strictly more
            # information (full first-token distribution, not just argmax).
            "token_jsd": similarity_token_jsd(sus_lp or None, b_lp or None),
            # hard_series mixes the math accuracy curve with the judged axes;
            # needs 3 shared components total (see similarity_hard_series).
            "hard_series": similarity_hard_series(sus_v2.get("hard_series"),
                                                  b.get("hard_series")),
        }
    return out


# ------------------------------------------------------- sibling separability

SEPARABLE_DISTANCE = 0.12


def pairwise_separability(a_std: dict, b_std: dict) -> dict:
    """How far apart are two canonical standards on the v2 dimensions?
    distance = 1 - mean(similarity of A-as-suspect vs B).  Below
    SEPARABLE_DISTANCE the pair is reported as an ALIAS group: no black-box
    probe in this standard can tell them apart."""
    va, vb = v2_of(a_std), v2_of(b_std)
    sims = {
        "tokenizer": similarity_tokenizer(va.get("tokenizer"), vb.get("tokenizer")),
        "capability_curve": similarity_curve(va.get("capability_curve"),
                                             vb.get("capability_curve")),
        "knowledge_bank": similarity_bank(va.get("knowledge_bank"),
                                          vb.get("knowledge_bank")),
        "discriminator": similarity_discriminator(va.get("discriminator"),
                                                  vb.get("discriminator")),
        "spatial_curve": similarity_curve(va.get("spatial_curve"),
                                          vb.get("spatial_curve"),
                                          min_shared=3),
        "token_jsd": similarity_token_jsd(_logprob_profile_of(a_std),
                                          _logprob_profile_of(b_std)),
        "hard_series": similarity_hard_series(va.get("hard_series"),
                                              vb.get("hard_series")),
    }
    got = [v for v in sims.values() if v is not None]
    if not got:
        return {"distance": None, "separable": None, "sims": sims}
    dist = round(1.0 - sum(got) / len(got), 3)
    return {"distance": dist, "separable": dist >= SEPARABLE_DISTANCE, "sims": sims}


# ------------------------------------------------------------------ token_jsd
# (2026-10-19) Distribution-level logprob lane. The V5 `logprob` dim compares
# ONLY the chosen first token's logprob (a scalar per probe) and is therefore
# blind whenever the argmax agrees but the tail differs -- precisely the
# sibling-version case that produced the 2-candidate alias clusters. token_jsd
# instead compares the whole top_logprobs vector with Jensen-Shannon divergence.
#
# COST: ZERO extra API calls. harvest.py already stores
# logprob_profile[probe_id] = {"logprob": float, "top": {token: logprob}} for
# every lp_tok_* probe, and the suspect side carries the same shape. This lane
# only re-reads data that was previously discarded.
#
# JSD is used with log base 2 so the divergence lands in [0, 1] and
# `1 - mean(jsd)` is directly comparable to the other v2 similarities.

_JSD_EPS = 1e-12
# Probes whose top_logprobs payload is missing/unusable on either side are
# skipped; below this many shared probes there is no reading at all (same
# discipline as similarity_curve's min_shared and the knowledge needle scope).
_JSD_MIN_SHARED = 3


def _logprob_profile_of(std: dict) -> dict:
    """Pull logprob_profile out of a standard OR a v2 block OR a suspect dict.

    Handles every shape seen in the wild: TraitMark nesting
    (structural.tokenizer_signature.logprob_profile), the sibling
    structural.logprob_profile, and the flat legacy/suspect form. Returns {}
    when absent so callers never need a None check.
    """
    if not isinstance(std, dict):
        return {}
    lp = std.get("logprob_profile")
    if isinstance(lp, dict) and lp:
        return lp
    _s = std.get("structural")
    if isinstance(_s, dict):
        lp = _s.get("logprob_profile")
        if isinstance(lp, dict) and lp:
            return lp
        _ts = _s.get("tokenizer_signature")
        if isinstance(_ts, dict):
            lp = _ts.get("logprob_profile")
            if isinstance(lp, dict) and lp:
                return lp
            # v2 lanes live under tokenizer_signature.v2
            _v2 = _ts.get("v2")
            if isinstance(_v2, dict):
                lp = _v2.get("logprob_profile") or _v2.get("token_jsd")
                if isinstance(lp, dict):
                    return lp.get("profiles") if isinstance(lp.get("profiles"), dict) else lp
    return {}


def _top_dist(node) -> dict | None:
    """Normalize one probe entry into a probability vector {token: p}.

    Accepts {"logprob":x, "top":{tok:lp}} (harvest shape) or a bare
    {tok: lp} map. Returns None when there is no usable top_logprobs payload
    -- a scalar-only entry cannot support a distribution comparison.
    """
    if not isinstance(node, dict):
        return None
    top = node.get("top") if isinstance(node.get("top"), dict) else None
    if top is None:
        # bare {token: logprob} map, but reject the harvest wrapper keys
        top = {k: v for k, v in node.items()
               if k not in ("logprob", "top", "token", "profiles")}
        if not top:
            return None
    dist = {}
    for tok, lp in top.items():
        try:
            dist[str(tok)] = math.exp(float(lp))
        except (TypeError, ValueError):
            continue
    total = sum(dist.values())
    if total <= _JSD_EPS:
        return None
    return {t: p / total for t, p in dist.items()}


def _jsd_pair(p: dict, q: dict) -> float:
    """Jensen-Shannon divergence (log base 2) of two probability dicts -> [0,1].

    Missing tokens on one side are treated as zero mass, which is the correct
    reading: a token absent from the reported top-k genuinely carries less
    probability there. Both inputs must be normalized.
    """
    keys = set(p) | set(q)
    if not keys:
        return 0.0
    jsd = 0.0
    for k in keys:
        pk, qk = p.get(k, 0.0), q.get(k, 0.0)
        m = 0.5 * (pk + qk)
        if pk > _JSD_EPS:
            jsd += 0.5 * pk * math.log(pk / m, 2)
        if qk > _JSD_EPS:
            jsd += 0.5 * qk * math.log(qk / m, 2)
    # Numerically JSD can land a hair outside [0,1] from float error.
    return max(0.0, min(1.0, jsd))


def similarity_token_jsd(sus_profile: dict, base_profile: dict,
                         min_shared: int = _JSD_MIN_SHARED):
    """1 - mean per-probe JSD over shared lp probes. None = not comparable.

    Mirrors the V5 logprob dim's sharing rule (a probe counts only when BOTH
    sides carry usable data) but scores the distribution instead of the argmax.
    """
    sus_profile = sus_profile or {}
    base_profile = base_profile or {}
    if not sus_profile or not base_profile:
        return None
    dists = []
    for pid in sus_profile:
        b = base_profile.get(pid)
        if not isinstance(b, dict):
            continue
        ps = _top_dist(sus_profile.get(pid))
        pb = _top_dist(b)
        if ps is None or pb is None:
            continue
        dists.append(_jsd_pair(ps, pb))
    if len(dists) < min_shared:
        return None
    return round(1.0 - sum(dists) / len(dists), 3)

