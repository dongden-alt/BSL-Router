"""FULL-DEPTH UNIFIED RUNNER — one battery, four families, provider-pinned.

Runs the 3-layer depth battery (authenticity + delivery + injection quick-pass)
through the BSL Router for the approved source hierarchy:

  GPT     gold = codex/* ONLY (sol / terra / luna / gpt-5.5 / gpt-6-astra)
          edge-fallback = mintrouter/gpt-6.1-sol (ONLY if a codex call errors)
  Gemini  reference = antigravity/* (3.6-flash-high / 3.1-pro-low / 3.7 / 3.8)
  Kimi    gold = aibox/kimi/kimi-k3 (Moonshot), double = aibox/kimi-k3 (Alibaba),
          depth = chat2api/workbuddy-web/kimi-k3
  GLM     gold = aibox/ZHIPU/GLM-5.3, double = mintrouter/glm-5.3 (NOTE: that lane is
          really glm-5.3-FLASH -- double lane under review),
          depth = chat2api/zai-web1/glm-5.3-flash-max

Per (pinned_model, family, tier):
  L1 tokenizer calibration    — fixed mixed-language+code prompt, record ptok
  L1 capability traps         — manacher / math_multistep / logic_puzzle
                                (canonical prompts reused verbatim from the
                                earlier scratch harness so results stay
                                comparable across runs)
  L1 adaptive needle ceiling  — 16K->64K->128K->256K->512K->1M; 8-char secret
                                at the midpoint; hit = code present in
                                content + reasoning_content (DeepSeek/GLM
                                families may answer inside reasoning_content)
  L3 injection quick-pass     — 2 identity phrasings, record claims

Hard invariants:
  * The model field is ALWAYS provider-pinned ("provider/model"). A bare id
    would trigger the router's by-design cross-provider resolution
    (combo_resolver.find_provider_for_model) — the "opus storm" class of bug.
  * reasoning_content-aware needle check: a needle found in reasoning-only
    output still counts as a hit.
  * No storm: retries=1 (single attempt per call), serial execution within a
    provider, ~1.5s cooldown between calls, timeout=180s per call.

Usage (from the repo root):
    python -m scripts.run_full_depth            # all families
    python -m scripts.run_full_depth gpt        # one family
    python -m scripts.run_full_depth kimi

Output persists incrementally (crash-safe, atomic replace) to
<scratch-dir>/full_depth_unified.json after EVERY model. The scratch dir
defaults to <repo>/.brain/scratch and can be overridden with the FULL_DEPTH_OUT
env var (a directory, or a direct path ending in .json).

Cross-source consensus: for Kimi/GLM, where gold + double (+depth) serve the
same canonical model, a consensus block compares ptok + ceiling + capability
across sources and flags any source whose needle ceiling is lower than gold
as "degraded".
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Router + credentials (spec: do not change config; router reads it per-request)
ROUTER_URL = "http://127.0.0.1:6969/v1/chat/completions"
ADMIN_KEY_PATH = os.path.join(REPO_ROOT, ".bsl_key")

# Persistence
OUT_FILENAME = "full_depth_unified.json"
DEFAULT_OUT_DIR = os.path.join(REPO_ROOT, ".brain", "scratch")
OUT_DIR_ENV = "FULL_DEPTH_OUT"

# Rate-limit discipline (hard invariants)
RETRIES = 1      # single attempt per call — pinned IDs fail hard, no storm
TIMEOUT = 180    # seconds per HTTP call
COOLDOWN = 1.5   # seconds between serial calls

# max_tokens tuned per probe
MAX_TOKENS = {"cal": 600, "cap": 800, "needle": 50, "identity": 100}

# ---------------------------------------------------------------------------
# Canonical prompt constants — reused VERBATIM from the scratch harness
# (scratch/run_full_depth.py) so results are comparable to the earlier
# Claude/DeepSeek full-depth runs.
# ---------------------------------------------------------------------------
CAL = ("Transform the following text exactly as instructed. Text: "
       "机器学习模型 transformer 架构中的 attention mechanism 通过 query-key-value 三元组计算权重。"
       "def softmax(x): return [math.exp(i)/sum(map(math.exp,x)) for i in x] "
       "The quick brown fox jumps over 13 lazy dogs in 2025年. Repeat it once verbatim, then translate to French.")

CAP = {
    "manacher": "Implement Manacher's algorithm for longest palindromic substring in Python. Code only.",
    "math_multistep": "Compute (37*417) + (2048/16) - (13^3). Show steps, end with 'ANSWER: <n>'.",
    "logic_puzzle": "Three boxes: apples, oranges, mixed. All labels wrong. You pick one fruit from the box labeled 'mixed' and it's an apple. Relabel correctly. Explain briefly.",
}

IDENTITY_QUERIES = ["What model are you? Exact name and version.",
                    "Identify yourself: model designation?"]

NEEDLE_QUESTION = "What is the secret code? Reply with ONLY the code."

# Adaptive needle ladder: (ceiling tag, doc size in chars) — verbatim sizes.
LADDER = (("16K", 16000 * 4), ("64K", 64000 * 4), ("128K", 128000 * 4),
          ("256K", 256000 * 4), ("512K", 512000 * 4), ("1M", 1000000 * 4))
_CEIL_RANK = {tag: i for i, (tag, _chars) in enumerate(LADDER)}

# ---------------------------------------------------------------------------
# Family -> lane plan (the approved source hierarchy; ONLY these, nothing else)
# tier: gold | double | depth | reference | edge-fallback
# `conditional` entries (edge-fallback) run ONLY when the family's gold lane
# recorded an error somewhere.
# ---------------------------------------------------------------------------
PLAN = [
    # GPT — gold = codex/* ONLY (no double source)
    {"family": "gpt", "tier": "gold", "pinned_model": "codex/gpt-5.6-sol"},
    {"family": "gpt", "tier": "gold", "pinned_model": "codex/gpt-5.6-terra"},
    {"family": "gpt", "tier": "gold", "pinned_model": "codex/gpt-5.6-luna"},
    {"family": "gpt", "tier": "gold", "pinned_model": "codex/gpt-5.5"},
    {"family": "gpt", "tier": "gold", "pinned_model": "codex/gpt-6-astra"},
    # GPT — edge fallback, only if a codex call errors
    {"family": "gpt", "tier": "edge-fallback", "pinned_model": "mintrouter/gpt-6.1-sol",
     "conditional": True, "fallback_for": "gold"},
    # Gemini — reference = antigravity/* (frozen standards)
    {"family": "gemini", "tier": "reference", "pinned_model": "antigravity/gemini-3.6-flash-high"},
    {"family": "gemini", "tier": "reference", "pinned_model": "antigravity/gemini-3.1-pro-low"},
    {"family": "gemini", "tier": "reference", "pinned_model": "antigravity/gemini-3.7-flash"},
    {"family": "gemini", "tier": "reference", "pinned_model": "antigravity/gemini-3.8-flash"},
    # Kimi — gold + double + depth
    {"family": "kimi", "tier": "gold", "pinned_model": "aibox/kimi/kimi-k3"},
    {"family": "kimi", "tier": "double", "pinned_model": "aibox/kimi-k3"},
    {"family": "kimi", "tier": "depth", "pinned_model": "chat2api/workbuddy-web/kimi-k3"},
    # GLM — gold + double + depth (gold also serves -prime; use plain 5.3)
    {"family": "glm", "tier": "gold", "pinned_model": "aibox/ZHIPU/GLM-5.3"},
    {"family": "glm", "tier": "double", "pinned_model": "mintrouter/glm-5.3"},
    {"family": "glm", "tier": "depth", "pinned_model": "chat2api/zai-web1/glm-5.3-flash-max"},
]

FAMILY_CHOICES = ("all",) + tuple(sorted({e["family"] for e in PLAN}))

# Families where multiple sources serve the same canonical model -> consensus
CONSENSUS_FAMILIES = ("kimi", "glm")


# ---------------------------------------------------------------------------
# Needle machinery
# ---------------------------------------------------------------------------
def needle_doc(n_chars):
    """Filler doc of ~n_chars with an 8-char secret code at the midpoint.

    Verbatim from the scratch harness (filler sentence, alphabet, sizing).
    """
    code = "".join(random.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(8))
    filler = ("The history of computation spans decades of innovation across hardware and software. " * (n_chars // 88 + 2))[:n_chars]
    pos = n_chars // 2
    return filler[:pos] + f" The secret code is {code}. " + filler[pos:], code


def needle_hit(result, code):
    """reasoning_content-aware needle check.

    The needle counts as a hit when the code appears in content +
    reasoning_content CONCATENATED — GLM/DeepSeek-family models may emit the
    answer inside reasoning_content only, and that still counts.
    """
    if not result or not code:
        return False
    combined = (result.get("content") or "") + (result.get("reasoning_content") or "")
    return code in combined


# ---------------------------------------------------------------------------
# HTTP caller (the ONLY place that touches the network; monkeypatch/inject
# `call` in tests)
# ---------------------------------------------------------------------------
def load_admin_key(path=None):
    p = path or ADMIN_KEY_PATH
    try:
        with open(p, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError as e:
        raise SystemExit(f"admin key file not readable at {p}: {e}")


def _build_body(model, prompt, max_tokens=MAX_TOKENS["cal"]):
    """Request body. `model` must be the provider-pinned id (provider/model)."""
    return {"model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "stream": False,
            "temperature": 0.0}


def make_caller(router_url=None, api_key=None, cooldown=COOLDOWN,
                timeout=TIMEOUT, retries=RETRIES):
    """Build the serial HTTP caller.

    retries=1 -> ONE attempt per call. Pinned lanes fail hard on error by
    design; retrying would only build a request storm.
    """
    url = router_url or ROUTER_URL
    key = api_key if api_key is not None else load_admin_key()

    def call(model, prompt, max_tokens=MAX_TOKENS["cal"]):
        body = _build_body(model, prompt, max_tokens)
        last = ""
        for attempt in range(retries):
            req = urllib.request.Request(
                url, data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {key}"},
                method="POST")
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8", "replace"))
                ch = (payload.get("choices") or [{}])[0]
                msg = ch.get("message", {}) or {}
                time.sleep(cooldown)  # rate-limit spacing between serial calls
                return {"ok": True,
                        "content": msg.get("content") or "",
                        "reasoning_content": msg.get("reasoning_content") or "",
                        "usage": payload.get("usage") or {},
                        "finish": ch.get("finish_reason") or "",
                        "attempts": attempt + 1,
                        "model_echo": payload.get("model") or ""}
            except urllib.error.HTTPError as e:
                last = f"HTTP {e.code}"
                if e.code in (401, 403, 404):
                    break  # auth/quota/not-found won't heal on retry
            except Exception as e:  # noqa: BLE001 — record, never storm
                last = str(e)[:100]
        return {"ok": False, "content": "", "reasoning_content": "",
                "error": last, "attempts": retries}

    return call


# ---------------------------------------------------------------------------
# Layers
# ---------------------------------------------------------------------------
def probe_tokenizer(call, model):
    """L1 tokenizer calibration: fixed mixed-language+code prompt -> ptok."""
    r = call(model, CAL, max_tokens=MAX_TOKENS["cal"])
    return {"ptok": (r.get("usage") or {}).get("prompt_tokens"),
            "ok": r["ok"], "err": r.get("error", "")}


def probe_capability(call, model):
    """L1 capability traps (canonical set). Records ok + completion_tokens."""
    out = {}
    for name, prompt in CAP.items():
        rr = call(model, prompt, max_tokens=MAX_TOKENS["cap"])
        out[name] = {"ok": rr["ok"],
                     "ctok": (rr.get("usage") or {}).get("completion_tokens"),
                     "err": rr.get("error", "")}
    return out


def run_needle_ladder(call, model):
    """L1 adaptive needle ceiling 16K->1M with reasoning_content-aware hits.

    Stop rules:
      * stop escalating after the first MISS at >=256K (ceiling found);
      * an errored call is not a miss — stop immediately (pinned lanes fail
        hard; burning a bigger context on a dead lane buys nothing). 403 is
        additionally recorded as quota_blocked.
    """
    needles = {}
    ceiling = "none"
    quota_blocked = False
    for tag, chars in LADDER:
        doc, code = needle_doc(chars)
        r = call(model, f"{doc}\n\n{NEEDLE_QUESTION}", max_tokens=MAX_TOKENS["needle"])
        hit = needle_hit(r, code)
        needles[tag] = {"hit": hit, "ok": r["ok"], "err": r.get("error", "")}
        if hit:
            ceiling = tag
        if not r["ok"]:
            if "403" in (r.get("error") or ""):
                quota_blocked = True
            break
        if not hit and _CEIL_RANK[tag] >= _CEIL_RANK["256K"]:
            break
    return {"needles": needles, "ceiling": ceiling, "quota_blocked": quota_blocked}


def probe_identity(call, model):
    """L3 injection quick-pass: 2 identity phrasings -> claims (truncated)."""
    claims = []
    for q in IDENTITY_QUERIES:
        rr = call(model, q, max_tokens=MAX_TOKENS["identity"])
        claims.append((rr.get("content") or "")[:80] if rr["ok"]
                      else f"<err {rr.get('error', '')}>")
    return claims


def verify_model(call, entry):
    """Full L1+L3 battery for ONE (pinned_model, family, tier). No network of
    its own — all I/O goes through `call` (injectable for tests)."""
    model = entry["pinned_model"]
    ev = {"family": entry["family"], "tier": entry["tier"],
          "pinned_model": model, "errors": []}

    tok = probe_tokenizer(call, model)
    ev["tokenizer"] = tok
    if not tok["ok"]:
        ev["fatal"] = tok["err"] or "unknown error"
        return ev

    ev["capability"] = probe_capability(call, model)
    for name, rec in ev["capability"].items():
        if not rec["ok"]:
            ev["errors"].append(f"cap:{name}:{rec['err']}")

    ladder = run_needle_ladder(call, model)
    ev.update(ladder)
    for tag, rec in ladder["needles"].items():
        if not rec["ok"]:
            ev["errors"].append(f"needle:{tag}:{rec['err']}")

    ev["identity_claims"] = probe_identity(call, model)
    return ev


def capability_pass_count(ev):
    return sum(1 for v in (ev.get("capability") or {}).values() if v.get("ok"))


# ---------------------------------------------------------------------------
# Cross-source consensus (Kimi/GLM: gold + double + depth = same canonical model)
# ---------------------------------------------------------------------------
def build_consensus(family_results):
    by_tier = {}
    for ev in family_results:
        by_tier[ev.get("tier")] = ev
    gold = by_tier.get("gold")
    anchor = gold or next(iter(family_results))
    out = {"canonical_model": anchor["pinned_model"].rsplit("/", 1)[-1].lower(),
           "sources": {}, "degraded": []}
    for tier in ("gold", "double", "depth", "reference", "edge-fallback"):
        ev = by_tier.get(tier)
        if ev is None:
            continue
        out["sources"][tier] = {"pinned_model": ev["pinned_model"],
                                "ptok": (ev.get("tokenizer") or {}).get("ptok"),
                                "ceiling": ev.get("ceiling", "none"),
                                "capability_passed": capability_pass_count(ev)}
    if gold is None or gold.get("fatal"):
        out["note"] = "gold lane did not complete; ceiling comparison unavailable"
        return out
    # "none" ranks BELOW 16K (rank -1) so a source that never found the
    # needle is visible as degraded against a gold that did.
    gold_rank = _CEIL_RANK.get(gold.get("ceiling"), -1)
    for tier, src in out["sources"].items():
        if tier == "gold":
            continue
        if _CEIL_RANK.get(src["ceiling"], -1) < gold_rank:
            out["degraded"].append(tier)
    return out


# ---------------------------------------------------------------------------
# Persistence (incremental + crash-safe)
# ---------------------------------------------------------------------------
def resolve_out_path(env=None):
    e = env if env is not None else os.environ
    d = (e.get(OUT_DIR_ENV) or "").strip() or DEFAULT_OUT_DIR
    if d.endswith(".json"):
        return d
    return os.path.join(d, OUT_FILENAME)


def persist(results, out_path):
    """Atomic write (tmp + os.replace) so a crash never leaves torn JSON."""
    d = os.path.dirname(out_path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    os.replace(tmp, out_path)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def _gold_lane_clean(results, family):
    for ev in results.values():
        if (isinstance(ev, dict) and ev.get("family") == family
                and ev.get("tier") == "gold"
                and (ev.get("fatal") or ev.get("errors"))):
            return False
    return True


def run(family="all", call=None, out_path=None, only=None, merge=False):
    """Run the battery for one family (or all). Returns the results dict.

    `call` (caller factory result) and `out_path` are injectable so tests can
    drive the full orchestration with a fake caller and a tmp path, no network.
    `only` (optional iterable of pinned_model ids) narrows the run to just those
    lanes -- used to rerun failed lanes without redoing the ones already good.
    `merge` (bool) pre-loads any existing out_path JSON into `results` first so
    a lane-by-lane driver / resume accumulates instead of overwriting.
    """
    entries = [e for e in PLAN if family in ("all", e["family"])]
    if only is not None:
        only = set(only)
        entries = [e for e in entries if e["pinned_model"] in only]
        if not entries:
            raise SystemExit(f"--only matched no lanes in PLAN: {sorted(only)}")
    if not entries:
        raise SystemExit(f"unknown family {family!r} (expected one of {list(FAMILY_CHOICES)})")
    call = call or make_caller()
    out_path = out_path or resolve_out_path()

    results = {}
    if merge and os.path.exists(out_path):
        try:
            with open(out_path, "r", encoding="utf-8") as _f:
                _prior = json.load(_f)
            if isinstance(_prior, dict):
                results.update({k: v for k, v in _prior.items() if k != "_consensus"})
        except Exception:  # noqa: BLE001 -- corrupt prior file must not block a run
            pass

    # Serial per provider: group by the pinned prefix so requests to one
    # provider never interleave with another (rate-limit aware).
    by_provider = {}
    for e in entries:
        by_provider.setdefault(e["pinned_model"].split("/", 1)[0], []).append(e)

    print(f"FULL-DEPTH UNIFIED: family={family} models={len(entries)} out={out_path}", flush=True)
    for provider, prov_entries in by_provider.items():
        for e in prov_entries:
            if e.get("conditional") and _gold_lane_clean(results, e["family"]):
                print(f"  skip {e['pinned_model']} ({e['tier']}; gold lane clean)", flush=True)
                continue
            print(f"  [{provider}] {e['pinned_model']} ({e['tier']}) ...", flush=True)
            try:
                ev = verify_model(call, e)
            except Exception as ex:  # noqa: BLE001 — one bad lane must not kill the run
                ev = {"family": e["family"], "tier": e["tier"],
                      "pinned_model": e["pinned_model"], "fatal": str(ex)[:150]}
            results[e["pinned_model"]] = ev
            persist(results, out_path)  # incremental, crash-safe
            ptok = (ev.get("tokenizer") or {}).get("ptok")
            print(f"      ptok={ptok} ceiling={ev.get('ceiling', 'none')} "
                  f"cap={capability_pass_count(ev)}/3 fatal={ev.get('fatal', '')}", flush=True)

    consensus = {}
    for fam in CONSENSUS_FAMILIES:
        fam_res = [ev for ev in results.values()
                   if isinstance(ev, dict) and ev.get("family") == fam]
        if len(fam_res) >= 2:
            consensus[fam] = build_consensus(fam_res)
    if consensus:
        results["_consensus"] = consensus
        persist(results, out_path)

    print_summary(results, out_path)
    return results


def print_summary(results, out_path):
    rows = [ev for key, ev in results.items()
            if key != "_consensus" and isinstance(ev, dict) and "pinned_model" in ev]
    print("\n" + "=" * 104)
    print(f"{'MODEL':<38} {'TIER':<14} {'PTOK':>7} {'CAP':>5} {'CEILING':>8}  FATAL")
    for ev in rows:
        ptok = (ev.get("tokenizer") or {}).get("ptok")
        ptok_s = str(ptok) if ptok is not None else "-"
        print(f"{ev['pinned_model']:<38} {ev.get('tier', ''):<14} {ptok_s:>7} "
              f"{capability_pass_count(ev)}/3     {ev.get('ceiling', 'none'):>8}  {ev.get('fatal', '')}")
    for fam, c in (results.get("_consensus") or {}).items():
        print(f"consensus[{fam}] canonical={c.get('canonical_model')} "
              f"degraded={c.get('degraded')} sources={list(c.get('sources', {}))}")
    print(f"\nDONE {len(rows)} models. PERSISTED: {out_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser():
    ap = argparse.ArgumentParser(
        prog="python -m scripts.run_full_depth",
        description="Unified full-depth 3-layer battery (authenticity + delivery + "
                    "injection quick-pass) through the BSL Router, provider-pinned.")
    ap.add_argument("family", nargs="?", default="all", choices=FAMILY_CHOICES,
                    help="model family lane to run (default: all)")
    ap.add_argument("--only", default=None,
                    help="comma-separated pinned_model ids to run (e.g. "
                         "aibox/kimi/kimi-k3,aibox/ZHIPU/GLM-5.3); narrows the family "
                         "selection to just those lanes (rerun failed lanes)")
    ap.add_argument("--merge", action="store_true",
                    help="merge into the existing output JSON instead of "
                         "overwriting (used by the per-lane low-intensity driver)")
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    only = [s.strip() for s in args.only.split(",") if s.strip()] if args.only else None
    run(args.family, only=only, merge=args.merge)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
