"""Rebuild thin/defected canonical authenticity standards at full depth.

Runs the CURRENT on-disk harvest.py (with the lexical fix) IN-PROCESS, probing
the live router as a plain OpenAI-compatible client — the live router keeps
routing upstream, no restart needed. Each model is re-harvested from a trusted
anchor lane with a DIFFERENT-family judge (never the suspect's own family).

Usage:
  python scripts/rebuild_thin_standards.py --list
  python scripts/rebuild_thin_standards.py --only opus
  python scripts/rebuild_thin_standards.py --apply
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.authenticity import harvest as H  # noqa: E402
from app.authenticity import lanes as L  # noqa: E402
from app.authenticity import spend_guard as SG  # noqa: E402

ROUTER_EP = "http://127.0.0.1:6969"
NL = chr(10)

# HARD RULE (2026-10-08): canonical authenticity standards may be benchmarked ONLY
# off these trusted sources. Anything else is an untrusted relay/proxy and its data
# must never be frozen into a benchmark. vsllm is explicitly banned.
TRUSTED_SOURCES = ("codex", "antigravity", "mintrouter", "aibox")


def _trusted(anchor: str) -> bool:
    return anchor.split("/")[0].strip().lower() in TRUSTED_SOURCES


MODELS = {
    # claude -- mintrouter only (antigravity also carries opus-5-5/sonnet-5-5).
    # opus-4-8 and haiku-4-5 have NO trusted lane -> not benchmarks (see notes).
    "claude-fable-5":       ("mintrouter/claude-fable-5",       "codex/gpt-6-astra"),
    "claude-fable-5-1":     ("mintrouter/claude-fable-5-1",     "codex/gpt-6-astra"),
    "claude-opus-5":        ("mintrouter/claude-opus-5",        "codex/gpt-6-astra"),
    "claude-opus-5-5":      ("mintrouter/claude-opus-5-5",      "codex/gpt-6-astra"),
    "claude-sonnet-5":      ("mintrouter/claude-sonnet-5",      "codex/gpt-6-astra"),
    "claude-sonnet-5-5":    ("mintrouter/claude-sonnet-5-5",    "codex/gpt-6-astra"),
    # gpt -- codex first-party.
    "gpt-5.6-sol":          ("codex/gpt-5.6-sol",               "mintrouter/claude-opus-5-5"),
    "gpt-6-astra":          ("codex/gpt-6-astra",               "mintrouter/claude-opus-5-5"),
    "gpt-6-luna":           ("codex/gpt-6-luna",                "mintrouter/claude-opus-5-5"),
    "gpt-6.1-sol":          ("codex/gpt-6.1-sol",               "mintrouter/claude-opus-5-5"),
    # STANDARD LANES (user directive 2026-10-08) -- enforced by app/authenticity/lanes.py:
    #   glm -> aibox/ZHIPU/   kimi -> aibox/kimi/   deepseek -> aibox/ds/
    # mintrouter/glm-5.3 is actually glm-5.3-FLASH; aibox/glm-5.x and aibox/kimi-k3
    # are NOT the standard lanes. The router is case-sensitive (lists ZHIPU/GLM-5.3).
    "glm-5.2":              ("aibox/ZHIPU/GLM-5.2",             "mintrouter/claude-opus-5-5"),
    "glm-5.3":              ("aibox/ZHIPU/GLM-5.3",             "mintrouter/claude-opus-5-5"),
    # kimi -- moonshot lane aibox/kimi/ for BOTH kimi-k3 and k2.7-code.
    "kimi-k3":              ("aibox/kimi/kimi-k3",              "mintrouter/claude-opus-5-5"),
    "kimi-k2.7-code":       ("aibox/kimi/kimi-k2.7-code",       "mintrouter/claude-opus-5-5"),
    # qwen -- aibox/qwen3.8-max and aibox/qwen3.8-max-0902.
    "qwen3.8-max":          ("aibox/qwen3.8-max",               "mintrouter/claude-opus-5-5"),
    "qwen3.8-max-0902":     ("aibox/qwen3.8-max-0902",          "mintrouter/claude-opus-5-5"),
    # deepseek -- v4-pro on aibox/ds/; v4.1-flash on aibox/deepseek-v4.1-flash
    # (user directive 2026-10-09; exact-lane exception in lanes.CANONICAL_EXACT_LANES).
    "deepseek-v4-pro":      ("aibox/ds/deepseek-v4-pro",        "mintrouter/claude-opus-5-5"),
    "deepseek-v4.1-flash":  ("aibox/deepseek-v4.1-flash",       "mintrouter/claude-opus-5-5"),
    # gemini -- antigravity first-party. gemini-pro-agent == gemini-3.1-pro-high
    # (user 2026-10-09); gemini-3.8-flash is not routable anywhere -> not listed.
    "gemini-3.1-pro":       ("antigravity/gemini-pro-agent",     "codex/gpt-6-astra"),
}

# DOUBLE REFERENCES (--double): an independently-served second copy of a canonical,
# harvested with a source_tag so it never enters the canonical match pool.
# kimi-k3: aibox/kimi-k3 is Alibaba-served (canonical = Moonshot aibox/kimi/kimi-k3).
DOUBLE_TAG = "double-ref"
DOUBLES = {
    "kimi-k3": ("aibox/kimi-k3", "mintrouter/claude-opus-5-5"),
    "glm-5.2": ("aibox/glm-5.2", "mintrouter/claude-opus-5-5"),
    "glm-5.3": ("aibox/glm-5.3", "mintrouter/claude-opus-5-5"),
}

# SPEND GUARD (2026-10-08): aibox is a PAID lane. Every aibox response is cached to
# disk the instant it lands (resume = zero re-spend) and each model gets a hard
# network-call ceiling. A full harvest is ~115 calls; the ceiling leaves headroom
# for retries but cannot run away.
CACHE_DIR = Path(__file__).resolve().parent.parent / ".brain" / "scratch" / "harvest_cache"
DEFAULT_MAX_CALLS = 150


def _lane_problems(models, routable, double=False, skip_unroutable=False):
    """Validate each anchor against the lane policy and the router's live model
    list. Returns (problems, resolved) -- resolved maps canonical -> exact id.
    double=True validates DOUBLE-REFERENCE lanes instead of canonical lanes.
    skip_unroutable=True: unroutable lanes are dropped from `resolved` and NOT
    reported as problems (used when the driver must skip absent lanes)."""
    problems, resolved = [], {}
    for m, (anchor, _judge) in models.items():
        if double:
            if not L.is_double_reference(anchor) or L.DOUBLE_REFERENCE_LANES[anchor.lower()] != m:
                problems.append("%s: %s is not a registered double-reference lane" % (m, anchor))
                continue
            if routable is None:
                resolved[m] = anchor
                continue
            exact = L.resolve_routable(anchor, routable)
            if not exact:
                if skip_unroutable:
                    print("SKIP %-18s lane %s not routable" % (m, anchor))
                else:
                    problems.append("%s: lane %s is not routable on the router" % (m, anchor))
            else:
                resolved[m] = exact
            continue
        why = L.lane_violation(anchor)
        if why:
            problems.append("%s: %s" % (m, why))
            continue
        if L.CANONICAL_ANCHORS.get(m) and L.CANONICAL_ANCHORS[m].lower() != anchor.lower():
            problems.append("%s: anchor %s disagrees with lanes.CANONICAL_ANCHORS (%s)"
                            % (m, anchor, L.CANONICAL_ANCHORS[m]))
            continue
        if routable is None:
            resolved[m] = anchor
            continue
        exact = L.resolve_routable(anchor, routable)
        if not exact:
            if skip_unroutable:
                print("SKIP %-18s lane %s not routable" % (m, anchor))
            else:
                problems.append("%s: lane %s is not routable on the router" % (m, anchor))
        else:
            resolved[m] = exact
    return problems, resolved


def _routable_ids(key):
    try:
        import httpx
        r = httpx.get(ROUTER_EP + "/v1/models", timeout=30,
                      headers={"Authorization": "Bearer " + key})
        return [x.get("id", "") for x in (r.json().get("data") or [])]
    except Exception:
        return None


def _depth(tm):
    k = tm.knowledge
    nc = tm.noncoding
    axes = [(getattr(nc, a, {}) or {}).get("score", 0) for a in H.JUDGE_AXES]
    nc_ok = all(isinstance(s, (int, float)) and s > 0 for s in axes)
    # Lexical captured == the layer was computed over real outputs. vocab (TTR)
    # is always > 0 for real text; slop may legitimately be 0.0 for low-cliché
    # models (Claude) — that IS the signal, not a gap.
    lex_ok = tm.lexical.vocabulary_complexity > 0
    cutoff_known = bool(k.cutoff_year)
    cap_ok = (k.capability_boundary or {}).get("correct", 0) > 0
    ts = (tm.structural.tokenizer_signature or {})
    v2p = ts.get("v2") or {}
    v2_lanes = [ln for ln in ("tokenizer", "capability_curve", "knowledge_bank")
                if v2p.get(ln)]
    v2_ok = len(v2_lanes) == 3
    return {
        "cutoff": "%s-%s" % (k.cutoff_year, k.cutoff_month),
        "cap": (k.capability_boundary or {}).get("correct", 0),
        "nc": "/".join(str(s) for s in axes),
        "slop": tm.lexical.slop_score,
        "vocab": tm.lexical.vocabulary_complexity,
        "judge": tm.meta.judged_by or "-",
        "needles": len(k.known_facts or []) + len(k.post_cutoff_unknown or []),
        "v2": "+".join(v2_lanes) or "-",
        # Full depth = both decisive discriminators present AND all three
        # judge-free v2 lanes. cutoff/cap may be honest-unknown (a model
        # legitimately may not self-report them); the v2 lanes + noncoding +
        # lexical layers carry the version split.
        "full": bool(nc_ok and lex_ok and v2_ok),
        "cutoff_known": cutoff_known,
        "cap_ok": cap_ok,
    }


async def _one(model_id, anchor, judge, args, key):
    H.JUDGE_BACKOFF_GAPS = (4.0, 10.0, 25.0)
    throttled = anchor.split("/")[0].strip().lower() == "mintrouter"
    cfg = {"probe_timeout_s": 90, "total_timeout_s": 600 if throttled else 300,
           "max_tier": 2, "backoff_gaps": (4.0, 10.0, 25.0),
           "concurrency": 2 if throttled else 8,
           "v2": True, "v2_probe_timeout_s": 180, "v2_total_timeout_s": 2400,
           "v2_concurrency": 2 if throttled else 4}
    tm = await H.harvest_model(
        ROUTER_EP, key, anchor,
        judge_endpoint=ROUTER_EP, judge_key=key, judge_model=judge,
        include_noncoding=True, source_tag=args.source_tag, config=cfg)
    tm.meta.model_id = model_id
    return tm


async def _run(args, key):
    double = bool(getattr(args, "double", False))
    if double:
        args.source_tag = DOUBLE_TAG   # never enters the canonical pool
    src = DOUBLES if double else MODELS
    # Exact-name --only (case-insensitive, FULL canonical name match). No fuzzy
    # substring: '--only kimi' must not silently select kimi-k3 AND kimi-k2.7.
    if args.only:
        want = args.only.strip().lower()
        models = {m: v for m, v in src.items() if m.lower() == want}
        if not models:
            print("--only %r matched no canonical name. Available: %s"
                  % (args.only, ", ".join(sorted(src))))
            return
    else:
        models = dict(src)
    if args.list:
        for m, (a, j) in models.items():
            print("%-18s anchor=%-40s judge=%s" % (m, a, j))
        print(NL + "%d models" % len(models))
        return
    if not args.only and not args.all:
        print("Refusing a full run without --all (spend guard: accidental bulk "
              "harvest is how the last 800-call overrun happened).")
        return
    # Spend guard: disk cache + hard per-model network-call ceiling on aibox.
    aibox_models = any(a.split("/")[0].lower() == "aibox" for a, _ in models.values())
    if aibox_models and args.apply:
        SG.enable(CACHE_DIR, sources=("aibox",), max_calls=args.max_calls)
        print("spend guard: cache=%s ceiling=%d network-calls/model" % (CACHE_DIR, args.max_calls))
    problems, resolved = _lane_problems(models, _routable_ids(key), double=double,
                                        skip_unroutable=True)
    if problems:
        print("LANE POLICY VIOLATION -- refusing to harvest:")
        for p in problems:
            print("  - " + p)
        return
    if not resolved:
        print("nothing routable to harvest")
        return
    results = []
    for m, (anchor, judge) in models.items():
        if m not in resolved:
            continue
        SG.reset_counters()
        try:
            tm = await _one(m, resolved[m], judge, args, key)
            st = SG.stats(resolved[m])
            if st["exhausted"]:
                results.append((m, False))
                print("BUDGET %-15s HIT ceiling %d network calls -- NOT saved" % (m, st["limit"]))
                continue
            d = _depth(tm)
            ok = d["full"]
            action = "dry"
            if args.apply and (ok or args.force):
                path = H.save_standard(tm, H.STANDARDS_DIR)
                action = "saved %s" % path.name
            elif args.apply:
                action = "REFUSED (below depth floor; --force to override)"
            results.append((m, ok))
            print("%s %-18s cutoff=%-9s cap=%s nc=%-9s slop=%.3f vocab=%.3f needles=%d v2=%s judge=%s  %s"
                  % ("OK " if ok else "THIN", m, d["cutoff"], d["cap"], d["nc"],
                     d["slop"], d["vocab"], d["needles"], d["v2"], d["judge"], action))
            print("    spend: %d network + %d cache-hits (limit %d)" % (st["net"], st["hits"], st["limit"]))
        except Exception as exc:
            results.append((m, False))
            print("ERR %-18s %s" % (m, str(exc)[:160]))
    full = sum(1 for _, ok in results if ok)
    print(NL + "%d/%d full-depth" % (full, len(results)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--only", default="")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="save even below the depth floor")
    ap.add_argument("--double", action="store_true",
                    help="harvest the DOUBLE-REFERENCE lanes (e.g. Alibaba-served "
                         "aibox/kimi-k3) as source-tagged references")
    ap.add_argument("--source-tag", default="")
    ap.add_argument("--all", action="store_true",
                    help="run the full model list (required when --only is absent)")
    ap.add_argument("--max-calls", type=int, default=DEFAULT_MAX_CALLS,
                    help="hard network-call ceiling per model (default %(default)s)")
    args = ap.parse_args()
    key = ""
    kf = Path(__file__).resolve().parent.parent / ".bsl_key"
    if kf.exists():
        key = kf.read_text(encoding="utf-8").strip()
    asyncio.run(_run(args, key))
    return 0


if __name__ == "__main__":
    sys.exit(main())
