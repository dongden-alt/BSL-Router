"""Targeted v2-lane backfill for the 4 legacy Claude reference files, routed
through NON-mintrouter lanes because every mintrouter claude lane is hard-429
(Flex lifetime quota exhausted: $0.042 of $1000 remains).

Why this is safe / within policy:
- Targeted lane-only backfill (AGENTS.md rule 10 / KI targeted-backfill):
  scripts/backfill_v2_lane.py MERGES one lane into the existing standard,
  never re-harvests, never touches judge axes or other lanes, skips lanes
  already present.
- Zero mintrouter credit consumed: all probes go to vsllm-a / a6api pool
  accounts (probed live in .brain/scratch/probe_alt_lanes.txt).
- Cheap-first lane order; expensive hard_series runs LAST per file so a
  hard_series failure can never block cheaper lanes.
- Per-file consecutive-fail guard (3) so a flaky account cannot burn hours.

Lane mapping (alt lane -> file), chosen from probe results:
  haiku-4-5  -> vsllm-a/claude-fable-5     (matches anchor mintrouter/claude-fable-5)
  opus-4-6   -> vsllm-a/claude-opus-4-6    (EXACT legacy model, probed OK)
  opus-4-7   -> a6api/claude-opus-5-5      (matches anchor; fastest/most reliable probe)
  sonnet-4-6 -> vsllm-a/claude-sonnet-4-6  (EXACT legacy model, probed OK)

Log: .brain/scratch/claude4_backfill_log.txt (UTF-8, full output persisted).
"""
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STANDARDS = ROOT / "app" / "authenticity" / "reference" / "standards"
LOG = ROOT / ".brain" / "scratch" / "claude4_backfill_log.txt"
PY = ROOT / ".venv" / "Scripts" / "python.exe"
BACKFILL = ROOT / "scripts" / "backfill_v2_lane.py"

# cheap-first; hard_series (long reasoning outputs) always last
LANES = [
    "tokenizer",
    "capability_curve",
    "knowledge_bank",
    "spatial_curve",
    "token_jsd",
    "discriminator",
    "hard_series",
]

JOBS = [
    ("claude_claude-haiku-4-5_reference.json", "vsllm-a/claude-fable-5"),
    ("claude_claude-opus-4-6_reference.json", "vsllm-a/claude-opus-4-6"),
    ("claude_claude-opus-4-7_reference.json", "a6api/claude-opus-5-5"),
    ("claude_claude-sonnet-4-6_reference.json", "vsllm-a/claude-sonnet-4-6"),
]

MAX_CONSEC_FAIL = 3


def log(msg: str) -> None:
    line = "[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg)
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def lane_present(path: Path, lane: str) -> bool:
    try:
        d = json.load(path.open(encoding="utf-8"))
        v2 = d.get("v2", {}) or {}
        return v2.get(lane) not in (None, {}, [])
    except Exception:  # noqa: BLE001
        return False


def main() -> int:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    log("=== claude4 alt-lane backfill start (7 lanes x 4 files, cheap-first) ===")
    total_ok = total_fail = total_skip = 0
    for fname, model in JOBS:
        fpath = STANDARDS / fname
        if not fpath.exists():
            log("MISSING FILE %s -- skipped" % fname)
            continue
        log("--- %s via %s ---" % (fname, model))
        consec_fail = 0
        for lane in LANES:
            if lane_present(fpath, lane):
                log("SKIP %s :: %s already present" % (fname, lane))
                total_skip += 1
                continue
            t0 = time.time()
            proc = subprocess.run(
                [str(PY), "-X", "utf8", str(BACKFILL),
                 "--file", str(fpath), "--lane", model, "--lane-name", lane],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=1800,
            )
            dt = time.time() - t0
            tail = ""
            out = (proc.stdout or "") + (proc.stderr or "")
            for ln in out.strip().splitlines():
                if any(k in ln.lower() for k in ("merged", "skip", "error", "fail", "empty", "429")):
                    tail = ln.strip()[:150]
            if proc.returncode == 0:
                consec_fail = 0
                total_ok += 1
                log("OK   %s :: %-17s %6.0fs %s" % (fname, lane, dt, tail))
            else:
                consec_fail += 1
                total_fail += 1
                log("FAIL %s :: %-17s %6.0fs rc=%d %s" % (fname, lane, dt, proc.returncode, tail))
                if consec_fail >= MAX_CONSEC_FAIL:
                    log("ABORT %s -- %d consecutive failures, moving to next file" % (fname, consec_fail))
                    break
    log("=== done: ok=%d fail=%d skip=%d ===" % (total_ok, total_fail, total_skip))
    return 0


if __name__ == "__main__":
    sys.exit(main())
