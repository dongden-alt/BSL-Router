"""Batch driver: backfill ALL missing v2 lanes across canonical standards (2026-10-21).

Wraps scripts/backfill_v2_lane.py (the sanctioned targeted-backfill, which
MERGES one lane into an existing standard without touching the rest) in a
sequential loop. Design:

- Only files with meta.anchor_lane (canonical references). anchor=None files
  are alias/candidate captures -- skipped, nothing to backfill into.
- Recomputes the inventory at start; backfill_v2_lane.py itself SKIPs lanes
  already present, so re-running this driver is idempotent.
- Judge family separation: gpt_* suspects are judged by
  mintrouter/claude-sonnet-5-5 (the default codex/gpt-6-astra would be
  same-family judging itself). All other families use the script default.
- Sequential subprocess-per-lane: no read-modify-write races on the same
  JSON file, and one lane crash/timeout can't kill the batch.
- Aborts up front if router :6969 is unreachable.
"""
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STANDARDS = ROOT / "app" / "authenticity" / "reference" / "standards"
BACKFILL = ROOT / "scripts" / "backfill_v2_lane.py"
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
LANES = ["tokenizer", "capability_curve", "knowledge_bank",
         "discriminator", "spatial_curve", "hard_series"]
ROUTER = "http://127.0.0.1:6969"
GPT_JUDGE = "mintrouter/claude-sonnet-5-5"
PER_LANE_TIMEOUT_S = 1500  # 25 min hard cap per lane-run


def router_up() -> bool:
    try:
        urllib.request.urlopen(ROUTER + "/v1/models", timeout=5)
        return True
    except urllib.error.HTTPError:
        return True  # any HTTP response (even 401) = server alive
    except Exception:
        return False


def inventory():
    for p in sorted(STANDARDS.glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        anchor = (d.get("meta") or {}).get("anchor_lane") or ""
        if not anchor:
            continue
        v2 = ((d.get("structural") or {}).get("tokenizer_signature") or {}).get("v2") or {}
        missing = [l for l in LANES if not v2.get(l)]
        if missing:
            yield p.name, anchor, missing


def main() -> int:
    if not router_up():
        print("ABORT: router :6969 unreachable")
        return 2
    todo = list(inventory())
    total = sum(len(m) for _, _, m in todo)
    print(f"plan: {len(todo)} files, {total} lane-runs", flush=True)
    done = fail = 0
    i = 0
    for fname, anchor, missing in todo:
        judge = GPT_JUDGE if fname.startswith("gpt_") else None
        for lane in missing:
            i += 1
            cmd = [PY, "-X", "utf8", str(BACKFILL),
                   "--file", fname, "--lane", anchor, "--lane-name", lane]
            if judge:
                cmd += ["--judge", judge]
            print(f"[{i}/{total}] {fname} :: {lane} via {anchor}"
                  + (f" (judge={judge})" if judge else ""), flush=True)
            try:
                r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True,
                                   text=True, timeout=PER_LANE_TIMEOUT_S,
                                   encoding="utf-8", errors="replace")
                lines = (r.stdout or "").strip().splitlines()
                tail = lines[-1] if lines else (r.stderr or "").strip()[:200]
                print(f"    rc={r.returncode} {tail}", flush=True)
                if r.returncode == 0 and not tail.startswith("FAIL"):
                    done += 1
                else:
                    fail += 1
            except subprocess.TimeoutExpired:
                fail += 1
                print(f"    TIMEOUT after {PER_LANE_TIMEOUT_S}s", flush=True)
    print(f"DONE: {done} ok, {fail} failed, {total} planned", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
