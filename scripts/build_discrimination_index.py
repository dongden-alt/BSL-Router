"""Build the offline discrimination index over harvested authenticity standards.

Phase 0/1 of the adaptive authenticity protocol. Zero API calls.

Usage:
    .venv312\\Scripts\\python.exe scripts\\build_discrimination_index.py
    .venv312\\Scripts\\python.exe scripts\\build_discrimination_index.py --full-digest
    .venv312\\Scripts\\python.exe scripts\\build_discrimination_index.py --out <path>

Full index JSON is persisted (default: .brain/scratch/discrimination_index.json);
stdout carries only a bounded digest per batch-output discipline.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.authenticity import diffmatrix as dm


def main() -> int:
    ap = argparse.ArgumentParser(description="Offline discrimination index over harvested standards")
    ap.add_argument("--out", default=str(dm.DEFAULT_INDEX_PATH), help="index JSON output path")
    ap.add_argument("--full-digest", action="store_true", help="print extended digest (still bounded)")
    args = ap.parse_args()

    index = dm.build_index()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"PERSISTED: {out}")

    print(dm.digest(
        index,
        max_pairs=80 if args.full_digest else 10,
        max_flagged=60 if args.full_digest else 8,
    ))

    # Exit code doubles as the Phase 1 gate signal for scripting:
    #   0 = all canonical models separable from every other standard (offline)
    #   2 = at least one canonical model has inseparable pairs -> Phase 2 finding
    unresolved = [r for r in index["canonical_validation"] if r.get("resolved") is None]
    inseparable = [r for r in index["canonical_validation"] if r.get("resolved") and not r["separable_from_all"]]
    if unresolved or inseparable:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
