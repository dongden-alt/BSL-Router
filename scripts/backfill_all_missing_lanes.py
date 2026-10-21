"""Backfill ALL missing v2 lanes across canonical standards (user-directed 2026-10-21).

Scope rule: only files carrying meta.anchor_lane (canonical standards). The
anchor=None files are alias/candidate captures (matcher inputs), not references,
and are skipped. Per targeted-backfill policy this is lane-by-lane MERGE through
the sanctioned scripts/backfill_v2_lane.py CLI -- never a full re-harvest.

Design:
- subprocess per (file, lane): one crash cannot kill the batch.
- sequential: avoids read-modify-write races on the same standard JSON and keeps
  router :6969 load sane.
- judge family separation: gpt_* suspects are judged by mintrouter/claude-sonnet-5-5
  (default codex/gpt-6-astra would be same-family self-judging); everything else
  uses the script default.
- self-aborts if router :6969 is not healthy.
- full progress log: .brain/scratch/backfill_all_log.txt

Budget guards (added 2026-10-21 after the 62-job run was killed for credit):
- CHEAP-FIRST ordering: spatial_curve/capability_curve/knowledge_bank/discriminator
  run before any hard_series (the expensive long-generation lane).
- Dead-lane blocklist (.brain/scratch/backfill_dead_lanes.txt): a lane with 2+
  consecutive failures is skipped on every future run; a success resets it to 0.
- --fail-streak N: abort the whole batch after N consecutive FAILs.
- --skip-hard-series / --only-lane X / --only-files REGEX for tiered runs.
- --dry-run: print the ordered job list without firing anything.
"""
import argparse
import json
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STANDARDS = ROOT / "app" / "authenticity" / "reference" / "standards"
LOG = ROOT / ".brain" / "scratch" / "backfill_all_log.txt"
DEAD = ROOT / ".brain" / "scratch" / "backfill_dead_lanes.txt"
PY = ROOT / ".venv" / "Scripts" / "python.exe"
CLI = ROOT / "scripts" / "backfill_v2_lane.py"

LANES = ["tokenizer", "capability_curve", "knowledge_bank", "discriminator",
         "spatial_curve", "hard_series"]
# Cheap-first: hard_series (long generations, real token cost) goes last.
LANE_ORDER = {"spatial_curve": 0, "capability_curve": 1, "knowledge_bank": 2,
              "discriminator": 3, "tokenizer": 4, "hard_series": 5}
GPT_JUDGE = "mintrouter/claude-sonnet-5-5"   # different family than gpt_* suspects
DEFAULT_JUDGE = "codex/gpt-6-astra"          # matches backfill_v2_lane.py default
SUBPROCESS_TIMEOUT_S = 1500                  # one lane run must finish within 25 min
DEAD_THRESHOLD = 2                           # consecutive fails before a lane is skipped


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def router_alive() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:6969/health", timeout=5) as r:
            return r.status == 200
    except Exception:
        return False


def load_dead() -> dict:
    dead = {}
    if DEAD.exists():
        for raw in DEAD.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            try:
                dead[parts[0]] = int(parts[1]) if len(parts) > 1 else 1
            except ValueError:
                dead[parts[0]] = 1
    return dead


def save_dead(dead: dict) -> None:
    DEAD.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# lane_anchor<TAB>consecutive_fail_count -- count>=%d => skipped"
             % DEAD_THRESHOLD]
    for k in sorted(dead):
        if dead[k] > 0:
            lines.append(f"{k}\t{dead[k]}")
    DEAD.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_jobs():
    jobs = []
    for p in sorted(STANDARDS.glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            log(f"SKIP {p.name}: unparseable ({e})")
            continue
        anchor = (d.get("meta") or {}).get("anchor_lane") or ""
        if not anchor:
            continue  # alias/candidate capture, not a canonical reference
        v2 = ((d.get("structural") or {}).get("tokenizer_signature") or {}).get("v2") or {}
        for lane in LANES:
            if not v2.get(lane):
                jobs.append((p.name, anchor, lane))
    return jobs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--skip-hard-series", action="store_true",
                    help="Tier A: run every missing lane EXCEPT hard_series")
    ap.add_argument("--only-lane", default="", help="run only this lane name")
    ap.add_argument("--only-files", default="", help="regex filter on standard filename")
    ap.add_argument("--fail-streak", type=int, default=4,
                    help="abort batch after N consecutive FAILs (budget guard)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the ordered, filtered job list and exit")
    args = ap.parse_args()

    log("=== backfill_all_missing_lanes start "
        f"(skip_hard={args.skip_hard_series} only_lane={args.only_lane or '-'} "
        f"only_files={args.only_files or '-'} fail_streak={args.fail_streak}) ===")
    if not router_alive() and not args.dry_run:
        log("ABORT: router :6969 /health not OK -- nothing fired")
        return 2

    dead = load_dead()
    jobs = build_jobs()
    if args.skip_hard_series:
        jobs = [j for j in jobs if j[2] != "hard_series"]
    if args.only_lane:
        jobs = [j for j in jobs if j[2] == args.only_lane]
    if args.only_files:
        rx = re.compile(args.only_files)
        jobs = [j for j in jobs if rx.search(j[0])]
    skipped_dead = [j for j in jobs if dead.get(j[1], 0) >= DEAD_THRESHOLD]
    jobs = [j for j in jobs if dead.get(j[1], 0) < DEAD_THRESHOLD]
    jobs.sort(key=lambda j: (LANE_ORDER.get(j[2], 9), j[0]))

    log(f"JOBS={len(jobs)} skipped_dead_lane={len(skipped_dead)}")
    for fn, anchor, lane in skipped_dead:
        log(f"  SKIP-DEAD {fn} :: {lane} via {anchor}")
    if args.dry_run:
        for fn, anchor, lane in jobs:
            log(f"  DRY {fn} :: {lane} via {anchor}")
        return 0

    ok = fail = 0
    streak = 0
    failures = []
    for i, (fn, anchor, lane) in enumerate(jobs, 1):
        judge = GPT_JUDGE if fn.startswith("gpt_") else DEFAULT_JUDGE
        cmd = [str(PY), "-X", "utf8", str(CLI),
               "--file", fn, "--lane", anchor,
               "--lane-name", lane, "--judge", judge]
        log(f"[{i}/{len(jobs)}] {fn} :: {lane} via {anchor} (judge={judge})")
        t0 = time.time()
        try:
            r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                               encoding="utf-8", errors="replace",
                               timeout=SUBPROCESS_TIMEOUT_S)
            out = (r.stdout or "").strip().splitlines()
            tail = out[-1] if out else (r.stderr or "").strip()[-200:]
            log(f"  -> rc={r.returncode} {time.time() - t0:.0f}s | {tail}")
            if r.returncode == 0 and not tail.startswith("FAIL"):
                ok += 1
                streak = 0
                if dead.get(anchor):
                    dead[anchor] = 0
                    save_dead(dead)
            else:
                fail += 1
                streak += 1
                failures.append((fn, lane, tail[:120]))
                dead[anchor] = dead.get(anchor, 0) + 1
                save_dead(dead)
        except subprocess.TimeoutExpired:
            fail += 1
            streak += 1
            failures.append((fn, lane, "subprocess_timeout"))
            dead[anchor] = dead.get(anchor, 0) + 1
            save_dead(dead)
            log(f"  -> TIMEOUT after {SUBPROCESS_TIMEOUT_S}s")
        if streak >= args.fail_streak:
            log(f"ABORT: {streak} consecutive FAILs -- stopping to protect budget")
            break
    log(f"DONE ok={ok} fail={fail} skipped_dead_lane={len(skipped_dead)}")
    for fn, lane, why in failures:
        log(f"  FAILED: {fn} :: {lane} :: {why}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
