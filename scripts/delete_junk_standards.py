"""Remove provably-garbage frozen authenticity standards before re-harvest.

A standard is junk when it carries NO usable forensic signal:
  - a hallucinated knowledge cutoff (year < 2019 or year > current+1)
  - OR a zero/never-captured capability boundary AND an all-default non-coding
    battery AND no knowledge needles AND no boundary/stability/refusal profile
    (an empty shell that can only ever score 0.5 neutral)

Dry-run by default; pass --apply to delete. Re-harvest regenerates good ones.
"""
import argparse, datetime, json, sys
from pathlib import Path

STANDARDS_DIR = Path(__file__).resolve().parent.parent / "app" / "authenticity" / "reference" / "standards"


def _is_junk(std):
    know = std.get("knowledge") or {}
    cutoff_y = int(know.get("cutoff_year", 0) or 0)
    now_y = datetime.datetime.now().year
    if cutoff_y and (cutoff_y < 2019 or cutoff_y > now_y + 1):
        return True, "hallucinated_cutoff=%s" % cutoff_y
    cap = (know.get("capability_boundary") or {}).get("correct", 0)
    nc = std.get("noncoding") or {}
    nc_ok = any(isinstance((nc.get(a) or {}).get("score", 0), (int, float)) and (nc.get(a) or {}).get("score", 0) > 0 for a in ("creative", "eq", "humor", "design"))
    needles = bool(know.get("known_facts") or know.get("post_cutoff_unknown"))
    struct = (std.get("structural") or {}).get("tokenizer_signature") or {}
    cascade = bool(struct.get("boundary_signature") or struct.get("stability_profile") or struct.get("logprob_profile"))
    refusal = bool((std.get("refusal") or {}).get("axes"))
    if not cap and not nc_ok and not needles and not cascade and not refusal:
        return True, "no_usable_signal"
    return False, ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    junk, kept = [], 0
    for path in sorted(STANDARDS_DIR.glob("*.json")):
        try:
            std = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            print("SKIP(unparseable) %s: %s" % (path.name, exc))
            continue
        bad, why = _is_junk(std)
        if bad:
            junk.append((path, why))
        else:
            kept += 1
    for path, why in junk:
        print("%s %s  [%s]" % ("DELETE" if args.apply else "JUNK  ", path.name, why))
        if args.apply:
            path.unlink()
    print("\n%s %d; kept %d" % ("deleted" if args.apply else "would delete", len(junk), kept))
    return 0


if __name__ == "__main__":
    sys.exit(main())
