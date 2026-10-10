# cascade_augment_standards.py — patch existing frozen standards with V6/V7
# cascade profiles (boundary_signature / stability_profile / timing_profile).
#
# These standards were harvested BEFORE the cascade lanes existed, so their
# structural.tokenizer_signature lacks the three new keys. The gate's matcher
# reads them from that location; without them the cascade dims are always None
# and the cascade silently no-ops. This script runs ONLY the 3 new probes
# (6 API calls/lane, no judge, no T1/T2 re-burn) and merges the profiles into
# each standard in place. Idempotent: re-running refreshes the same keys.
#
# ROUTER-ONLY: probes go through the router endpoint key, never upstream.
# Run this AFTER the router is restarted with the new cascade code.
#
# Usage:
#   python scripts/cascade_augment_standards.py            # dry-run (no writes)
#   python scripts/cascade_augment_standards.py --apply    # write changes
#   python scripts/cascade_augment_standards.py --apply --only glm_vsllm-o-glm-5-3_glm-5-3.json
import argparse
import asyncio
import json
import sys
from pathlib import Path

# Reuse the router's own cascade probe helpers (single source of truth).
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import httpx  # noqa: E402
from app.authenticity.probes import (  # noqa: E402
    run_boundary_probes, run_stability_probe, run_timing_probe,
)

STANDARDS_DIR = REPO / "app" / "authenticity" / "reference" / "standards"


def _router_self() -> tuple[str, str]:
    """Router endpoint + admin key (router-only invariant, same as the gate)."""
    key_path = REPO / ".bsl_key"
    key = key_path.read_text(encoding="utf-8").strip() if key_path.exists() else ""
    return "http://127.0.0.1:6969/v1", key


# Explicit standard -> trusted lane map (curated, exact /v1/models ids).
# The router broadcasts any non-registered/bare model across ALL providers for
# that model (the quota-burn bug), so we NEVER infer a lane -- we only probe the
# exact trusted ids listed here. meta.model_id is the upstream's self-reported
# (often bare/aliased) id and is NEVER used to probe.
# Trusted lanes (user-designated 2026-10-06):
#   claude->mintrouter/claude-*, glm->aibox/ZHIPU/*, deepseek->aibox/ds/*,
#   kimi->aibox/kimi*, qwen->aibox/qwen3.8-max*, gpt->codex/gpt-* +
#   mintrouter/gpt-* (double-check), gemini->antigravity/gemini-*.
LANE_MAP: dict[str, str] = {
    # claude
    "claude_claude-fable-5.json":       "mintrouter/claude-fable-5",
    "claude_claude-fable-5-1.json":     "mintrouter/claude-fable-5-1",
    "claude_claude-haiku-4-5.json":     "mintrouter/claude-haiku-4-5",
    "claude_claude-opus-4-8.json":      "mintrouter/claude-opus-4-8",
    "claude_claude-opus-5.json":        "mintrouter/claude-opus-5",
    "claude_claude-opus-5-5.json":      "mintrouter/claude-opus-5-5",
    "claude_claude-sonnet-5.json":      "mintrouter/claude-sonnet-5",
    "claude_claude-sonnet-5-5.json":    "mintrouter/claude-sonnet-5-5",
    "claude_mintrouter-claude-fable-5-1_claude-fable-5-1.json":   "mintrouter/claude-fable-5-1",
    "claude_mintrouter-claude-sonnet-5-5_claude-sonnet-5-5.json": "mintrouter/claude-sonnet-5-5",
    # glm
    "glm_glm-5-2.json":  "aibox/ZHIPU/GLM-5.2",
    "glm_glm-5-3.json":  "aibox/ZHIPU/GLM-5.3",
    # deepseek
    "deepseek_ds-deepseek-v4-pro.json": "aibox/ds/deepseek-v4-pro",
    # kimi -- canonical = Moonshot lane aibox/kimi/ ONLY. aibox/kimi-k3 is the
    # Alibaba-served DOUBLE reference and must never feed the canonical's dims.
    "kimi_kimi-k3.json":      "aibox/kimi/kimi-k3",
    # qwen
    "qwen_qwen3-8-max.json":  "aibox/qwen3.8-max",
    # gpt (primary codex + mintrouter double-check)
    "gpt_codex-gpt-5-5.json":      "codex/gpt-5.5",
    "gpt_codex-gpt-5-6-luna.json": "codex/gpt-5.6-luna",
    "gpt_codex-gpt-5-6-sol.json":  "codex/gpt-5.6-sol",
    "gpt_codex-gpt-6-astra.json":  "codex/gpt-6-astra",
    "gpt_mintrouter-gpt-6-1-sol_gpt-6-1-sol.json": "mintrouter/gpt-6.1-sol",
    # gemini
    "gemini_antigravity-gemini-3-1-pro-low.json":   "antigravity/gemini-3.1-pro-low",
    "gemini_antigravity-gemini-3-6-flash-high.json": "antigravity/gemini-3.6-flash-high",
    "gemini_gemini-3-8-flash.json":                 "antigravity/gemini-3.8-flash",
}

_REGISTERED_IDS: frozenset | None = None


def _registered_ids() -> frozenset:
    """Exact set of lane ids from /v1/models (loaded once). Used only as a
    sanity check that a LANE_MAP entry is live-registered before probing."""
    global _REGISTERED_IDS
    if _REGISTERED_IDS is None:
        endpoint, key = _router_self()
        try:
            r = httpx.get(f"{endpoint}/models",
                          headers={"Authorization": f"Bearer {key}"}, timeout=20)
            ids = [m.get("id", "") for m in r.json().get("data", [])]
        except Exception:
            ids = []
        _REGISTERED_IDS = frozenset(i for i in ids if i)
    return _REGISTERED_IDS


def _resolve_lane(std: dict, filename: str) -> tuple[str | None, str]:
    """Return (lane, reason) from the explicit LANE_MAP. A standard not in the
    map is skipped (its cascade dims abstain -- safe), it is never probed."""
    lane = LANE_MAP.get(filename)
    if not lane:
        return None, "skip:not_in_lane_map"
    if lane not in _registered_ids():
        return None, f"skip:not_registered:{lane}"
    return lane, "ok"


async def augment_standard(client, endpoint, api_key, path: Path,
                           apply: bool, timeout: int = 60) -> dict:
    std = json.loads(path.read_text(encoding="utf-8"))
    lane, reason = _resolve_lane(std, path.name)
    if not lane:
        return {"file": path.name, "status": reason}
    model_id = lane

    # Run the 3 cascade probe sets (all, since this is offline augmentation).
    try:
        boundary = await run_boundary_probes(client, endpoint, api_key, model_id, timeout)
    except Exception as exc:
        boundary = {}
        print(f"  [boundary err] {str(exc)[:80]}")
    try:
        stability = await run_stability_probe(client, endpoint, api_key, model_id, timeout)
    except Exception as exc:
        stability = {}
        print(f"  [stability err] {str(exc)[:80]}")
    try:
        timing = await run_timing_probe(client, endpoint, api_key, model_id, timeout)
    except Exception as exc:
        timing = {}
        print(f"  [timing err] {str(exc)[:80]}")

    struct = (std.setdefault("structural", {}).setdefault("tokenizer_signature", {}))
    added = []
    if isinstance(boundary, dict) and boundary:
        struct["boundary_signature"] = boundary
        added.append("boundary")
    if isinstance(stability, dict) and stability.get("mode_hash"):
        struct["stability_profile"] = stability
        added.append("stability")
    if isinstance(timing, dict) and timing.get("chunk_count"):
        struct["timing_profile"] = timing
        added.append("timing")

    status = "ok" if len(added) == 3 else ("partial" if added else "no_signal")
    if apply and added:
        path.write_text(json.dumps(std, indent=2, ensure_ascii=False),
                        encoding="utf-8")
    return {"file": path.name, "model_id": model_id, "status": status,
            "added": added, "wrote": bool(apply and added)}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    ap.add_argument("--only", help="only this standard filename")
    ap.add_argument("--timeout", type=int, default=60)
    args = ap.parse_args()

    endpoint, api_key = _router_self()
    if not api_key:
        print("ERROR: .bsl_key not found"); return 1
    print(f"endpoint={endpoint}  apply={args.apply}  dir={STANDARDS_DIR}")

    files = sorted(STANDARDS_DIR.glob("*.json"))
    if args.only:
        files = [f for f in files if f.name == args.only]
        if not files:
            print(f"ERROR: no standard named {args.only}"); return 1
    print(f"standards: {len(files)}\n")

    results = []
    async with httpx.AsyncClient() as client:
        for f in files:
            print(f">> {f.name}")
            r = await augment_standard(client, endpoint, api_key, f,
                                       args.apply, args.timeout)
            print(f"   {r['status']}  added={r.get('added', [])}  wrote={r.get('wrote', False)}")
            results.append(r)

    ok = sum(1 for r in results if r["status"] == "ok")
    partial = sum(1 for r in results if r["status"] == "partial")
    print(f"\nDONE: {ok} ok / {partial} partial / "
          f"{len(results) - ok - partial} no-signal of {len(results)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
