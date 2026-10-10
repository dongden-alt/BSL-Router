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
import math
import os
import random
import re
import time

from .probes import run_probe

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
                                    timeout_s: int = 60,
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


def similarity_tokenizer(sus: dict, base: dict):
    """0..1 or None (not comparable / untrustworthy usage on either side)."""
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
    slack = 1 + int(sus.get("jitter") or 0) + int(base.get("jitter") or 0)
    for n in shared:
        diff = abs(ds[n] - db[n])
        if diff <= slack:
            sims.append(1.0)
        else:
            scale = max(2.0, 0.15 * max(abs(ds[n]), abs(db[n])))
            sims.append(max(0.0, 1.0 - (diff - slack) / scale))
    return round(sum(sims) / len(sims), 3)


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


def similarity_curve(sus: dict, base: dict):
    """1 - mean per-tier distance, tolerance widened by binomial noise."""
    ts, tb = (sus or {}).get("tiers") or {}, (base or {}).get("tiers") or {}
    shared = [n for n in ts if n in tb]
    if len(shared) < 4:
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

SCHEMA_PROBES = [
    ("sch_tools", "Plan: fetch weather for Hanoi+Tokyo+Paris AND compute 17*23, "
                  "all in parallel. Output a JSON array of 4 tool calls in "
                  "dependency-free parallel order. JSON only."),
    ("sch_extract", "From 'Ada Lovelace born 1815 in London, wrote notes on the "
                    "Analytical Engine in 1843': extract every fact as a JSON "
                    "object array. JSON only."),
    ("sch_calendar", "List the next 3 weekdays after 2026-10-09 as a JSON array "
                     "of objects. JSON only."),
    ("sch_menu", "Represent a 3-item menu with name, price and vegetarian "
                 "yes/no per item in JSON. JSON only."),
    ("sch_tree", "Represent the org tree CEO->{CTO->{backend,frontend},CFO} in "
                 "JSON. JSON only."),
    ("sch_error", "Return a JSON error response for a failed login carrying a "
                  "machine-readable code and a human message. JSON only."),
    ("sch_config", "Output a JSON config for a logger with level, file and "
                   "rotate fields. JSON only."),
    ("sch_refuse", "In what year was the Kessler-Voronin lemma in algebraic "
                   "topology first published? One sentence."),
    ("sch_rank", "Rank Python, Rust and SQL for a beginner in JSON with rank "
                 "and reason per language. JSON only."),
    ("sch_patch", "Show a JSON diff/patch object that changes 'level' from "
                  "'info' to 'debug' in a config. JSON only."),
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


async def run_discriminator(client, endpoint, api_key, model_id,
                            timeout_s: int = 120, k: int = 2,
                            concurrency: int = 4,
                            gaps=_RETRY_GAPS) -> dict:
    """Schema battery, k independent runs per probe (agreement across runs is
    itself part of the fingerprint). Returns {"answers": {probe_id: [feat*k]},
    "k": k} -- same shape family as knowledge_bank."""
    sem = asyncio.Semaphore(concurrency)

    async def one(pid, prompt):
        async with sem:
            try:
                r = await call_with_backoff(
                    client, endpoint, api_key, model_id,
                    {"id": f"disc_{pid}", "prompt": prompt, "max_tokens": 700},
                    timeout_s, extra={"temperature": 0}, gaps=gaps)
            except Exception:
                return pid, ""
            if getattr(r, "status_code", 0) != 200:
                return pid, ""
            return pid, schema_features(r.content or "")

    answers: dict = {}
    try:
        for _ in range(max(1, k)):
            res = await asyncio.gather(*[one(p, q) for p, q in SCHEMA_PROBES])
            for pid, feat in res:
                if feat:
                    answers.setdefault(pid, []).append(feat)
    except Exception:
        pass
    return {"answers": answers, "k": k} if answers else {}


def similarity_discriminator(sus: dict, base: dict):
    """Fraction of shared schema probes whose feature strings agree (exact or
    >=0.5 token overlap, via kb_match). None when <5 shared probes."""
    a, b = (sus or {}).get("answers") or {}, (base or {}).get("answers") or {}
    shared = [i for i in a if i in b and a[i] and b[i]]
    if len(shared) < 5:
        return None
    return round(sum(1 for i in shared
                     if any(kb_match(x, y) for x in a[i] for y in b[i]))
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
                          gaps=None) -> dict:
    """Run all three lanes.  mode='harvest' = full depth (k=3 / k=2, whole
    bank); mode='live' = cheap suspect pass (k=1, random 36-item subset)."""
    live = mode == "live"
    if rng is None and not live:
        # Harvest: model-seeded so the SAME prompts are generated on every run
        # (lets the spend-guard disk cache resume a killed/failed harvest at zero
        # cost). Live mode stays random -- fresh instances defeat replay caches.
        import zlib
        rng = random.Random(zlib.crc32((model_id or "").encode("utf-8")))
    if gaps is None:  # live: one short retry; harvest: full backoff
        gaps = (3.0,) if live else _RETRY_GAPS
    gaps = tuple(gaps)
    out = {"version": 2, "mode": mode}
    try:
        canary = await call_with_backoff(
            client, endpoint, api_key, model_id,
            {"id": "v2_canary", "prompt": "Reply with the single word: ok", "max_tokens": 16},
            min(timeout_s, 60), extra={"temperature": 0, "stream": False},
            gaps=gaps)
    except Exception:
        canary = None
    if canary is None or getattr(canary, "status_code", 0) != 200:
        out["gap"] = "lane_unreachable"
        return out
    # Per-lane timeout isolation (2026-10-09). A stall in ONE lane must not
    # discard the others: kimi-k3's tokenizer + capability_curve completed and
    # cached, then the machine slept inside knowledge_bank; on wake the caller's
    # all-or-nothing wait_for fired TimeoutError and threw away the whole v2
    # dict -> v2 empty -> THIN, despite good cached data. Each lane now gets its
    # own budget + try/except, so `out` always carries every lane that finished.
    lane_budget = float(timeout_s) * 3
    try:
        tok = await asyncio.wait_for(
            run_tokenizer_fingerprint(client, endpoint, api_key, model_id,
                                      min(timeout_s, 180), concurrency=concurrency,
                                      gaps=gaps),
            timeout=lane_budget)
    except Exception:
        tok = {}
    if tok:
        out["tokenizer"] = tok
    try:
        cap = await asyncio.wait_for(
            run_capability_curve(client, endpoint, api_key, model_id, timeout_s,
                                 k=1 if live else 4, rng=rng,
                                 concurrency=concurrency, gaps=gaps),
            timeout=lane_budget)
    except Exception:
        cap = {}
    if cap:
        out["capability_curve"] = cap
    try:
        kb = await asyncio.wait_for(
            run_knowledge_bank(client, endpoint, api_key, model_id, timeout_s,
                               k=1 if live else 3, sample=36 if live else None,
                               rng=rng, concurrency=concurrency, gaps=gaps),
            timeout=lane_budget)
    except Exception:
        kb = {}
    if kb:
        out["knowledge_bank"] = kb
    try:
        disc = await asyncio.wait_for(
            run_discriminator(client, endpoint, api_key, model_id, timeout_s,
                              k=1 if live else 2,
                              concurrency=concurrency, gaps=gaps),
            timeout=lane_budget)
    except Exception:
        disc = {}
    if disc:
        out["discriminator"] = disc
    return out


def compute_v2_dims(sus_v2: dict, candidates: dict) -> dict:
    """{key: {"tokenizer","capability_curve","knowledge_bank"}} (None=not compared)."""
    sus_v2 = sus_v2 or {}
    cand_v2 = {k: v2_of(s) for k, s in candidates.items()}
    inf = informative_items({k: (v.get("knowledge_bank") or {}) for k, v in cand_v2.items()})
    out = {}
    for k, b in cand_v2.items():
        out[k] = {
            "tokenizer": similarity_tokenizer(sus_v2.get("tokenizer"), b.get("tokenizer")),
            "capability_curve": similarity_curve(sus_v2.get("capability_curve"),
                                                 b.get("capability_curve")),
            "knowledge_bank": similarity_bank(sus_v2.get("knowledge_bank"),
                                              b.get("knowledge_bank"), inf),
            "discriminator": similarity_discriminator(sus_v2.get("discriminator"),
                                                      b.get("discriminator")),
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
    }
    got = [v for v in sims.values() if v is not None]
    if not got:
        return {"distance": None, "separable": None, "sims": sims}
    dist = round(1.0 - sum(got) / len(got), 3)
    return {"distance": dist, "separable": dist >= SEPARABLE_DISTANCE, "sims": sims}
