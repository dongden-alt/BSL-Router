"""Targeted V5 logprob-tail backfill (one file, one lane, 3 probes).

Re-running the FULL harvest just to fill structural.logprob_profile would
re-bill every tier-1/tier-2 probe and risk clobbering freshly captured v2
lanes. This script runs ONLY the LOGPROB_PROBES battery against ONE standard
file and MERGES the result into structural.logprob_profile -- v2 lanes,
judge axes, tokenizer signature and needles are preserved untouched.

Preflight is retries=0 on purpose (harvest.py:479-492): lanes that reject
logprobs surface the rejection as a retryable 502, which backoff classifies
as retryable, so any retry just burns paid calls on a guaranteed failure.
The remaining probes run with retries=2 because the lane's logprob support
is proven by then.

Usage:
  python scripts/backfill_logprob_lane.py --file claude_claude-sonnet-5.json \
      --lane aibox/kimi-k2.7-code
  python scripts/backfill_logprob_lane.py --file X.json --lane Y --replace
"""
import argparse, asyncio, json, os, sys, tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.authenticity.harvest import STANDARDS_DIR, _make_client  # noqa: E402
from app.authenticity.probes import LOGPROB_PROBES, run_probe  # noqa: E402

ROUTER_EP = "http://127.0.0.1:6969"

LP_EXTRA = {"logprobs": True, "top_logprobs": 5, "temperature": 0}


def _print_detail(r):
    """One line per probe: id, HTTP status, error, size of the top-k tail."""
    if r is None:
        print("  <none>: no result (probe returned None)")
        return
    top_n = 0
    if r.logprobs:
        first = (r.logprobs.get("content") or [{}])[0]
        top_n = len(first.get("top_logprobs") or [])
    print(f"  {r.probe_id}: status={r.status_code} "
          f"error={r.error or '-'} top={top_n}")


async def _run(file, lane, key, replace=False):
    path = STANDARDS_DIR / file
    d = json.loads(path.read_text(encoding="utf-8"))
    struct = d.setdefault("structural", {})
    existing = struct.get("logprob_profile") or {}
    if existing and not replace:
        print(f"SKIP {file}: logprob_profile already present "
              f"({len(existing)} probes)")
        return 0

    async with _make_client() as client:
        # Preflight, one shot, NO retries: an unsupported lane rejects
        # logprobs with a retryable 502 every time, so retries would only
        # multiply paid calls on a guaranteed failure.
        pf = await run_probe(client, ROUTER_EP, key, lane, LOGPROB_PROBES[0],
                             120, extra=LP_EXTRA, retries=0)
        _print_detail(pf)
        if pf is None or pf.error or pf.status_code != 200 \
                or pf.logprobs is None:
            st = pf.status_code if pf is not None else None
            err = (pf.error if pf is not None else None) or "-"
            print(f"FAIL {file}: lane does not support logprobs "
                  f"(status={st}, error={err}); file untouched")
            return 2
        results = [pf]
        # Lane support is proven; transient failures may be retried safely.
        for probe in LOGPROB_PROBES[1:]:
            r = await run_probe(client, ROUTER_EP, key, lane, probe,
                                120, extra=LP_EXTRA, retries=2)
            _print_detail(r)
            results.append(r)

    # Profile shape must match harvest.py:525-531 exactly --
    # standard_v2._logprob_profile_of / similarity_token_jsd consume it.
    profile = {}
    for r in results:
        if not r.error and r.logprobs:
            first = (r.logprobs.get("content") or [{}])[0]
            profile[r.probe_id] = {
                "logprob": first.get("logprob"),
                "top": {t.get("token"): t.get("logprob")
                        for t in (first.get("top_logprobs") or [])},
            }
    if not profile:
        print(f"FAIL {file}: no usable logprob payloads; file untouched")
        return 2

    struct["logprob_profile"] = profile
    # Atomic write on the same filesystem: temp file in the standards dir,
    # then os.replace -- a crash can never leave a half-written standard.
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(d, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

    ids = ", ".join(sorted(profile))
    print(f"OK {file}: logprob_profile merged "
          f"({len(profile)} probes: {ids})")
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="Backfill structural.logprob_profile for one standard "
                    "file (LOGPROB_PROBES battery, preflight-gated).")
    ap.add_argument("--file", required=True,
                    help="standard file name inside the standards dir")
    ap.add_argument("--lane", required=True,
                    help="router model lane to probe "
                         "(e.g. aibox/kimi-k2.7-code)")
    ap.add_argument("--replace", action="store_true",
                    help="re-capture even if logprob_profile is present "
                         "(wholesale replace, no merge)")
    a = ap.parse_args()
    key = (Path(__file__).resolve().parent.parent / ".bsl_key").read_text(encoding="utf-8").strip()
    return asyncio.run(_run(a.file, a.lane, key, replace=a.replace))


if __name__ == "__main__":
    sys.exit(main())
