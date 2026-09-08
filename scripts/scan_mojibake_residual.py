#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Residual mojibake scanner for BSL Router (CP1252-in-UTF8 double-encoding guard).

Detects double-encoded text: a substring that is the CP1252 *view* of UTF-8
bytes. Heuristic: for a candidate run, s.encode('cp1252') must succeed and the
result must decode as valid UTF-8 to a string DIFFERENT from s. Legitimate
accented text (e.g. "Sao") fails the round-trip (a lone 0xE3 is not a valid
UTF-8 lead byte) and is correctly NOT flagged.

Usage:
  python scan_mojibake_residual.py                 # scan whole repo (gitignored skipped)
  python scan_mojibake_residual.py --staged       # scan git staged (to-be-committed) files
  python scan_mojibake_residual.py file1 file2    # scan specific files

Exit codes: 0 = clean, 1 = residual(s) found, 2 = usage error.

NOTE: source is written with ASCII-only escape sequences for the lead-codepoint
table so this scanner can never itself carry mojibake.
"""
import io
import os
import re
import sys
import subprocess

# High Latin-1 / punctuation codepoints that, when mis-decoded as CP1252, mark
# a likely double-encoding. Built from codepoints (ASCII source) on purpose.
_LEAD_CODEPOINTS = [
    0x00c3, 0x00c2, 0x00e2, 0x20ac, 0x00f0, 0x0178, 0x00c5, 0x00ce, 0x00d4, 0x00d5,
    0x00d9, 0x00dc, 0x00de, 0x00ff, 0x00a5, 0x00a3, 0x00a2, 0x00a9, 0x00ae, 0x00b0,
    0x00c4, 0x00d6, 0x00df, 0x00f1, 0x00f3, 0x00fa, 0x00ec, 0x00ed, 0x00ee, 0x00ef,
    0x00f2, 0x00f4, 0x00f5, 0x00f9, 0x00fb, 0x00e7, 0x00eb, 0x00e6, 0x0153, 0x02c6,
    0x02dc, 0x2021, 0x2022, 0x2014, 0x2019, 0x201c, 0x201d,
]
LEAD = "(?:" + "|".join(re.escape(chr(c)) for c in _LEAD_CODEPOINTS) + ")"
PAT = re.compile(LEAD + r".{0,4}")

TEXT_EXT = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".md", ".txt", ".ps1", ".bat",
    ".cfg", ".ini", ".toml", ".yaml", ".yml", ".html", ".css", ".csv", ".rst",
    ".log", ".sh", ".pyi",
}
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "dist", "build", ".brain"}


def is_text_ext(path):
    return os.path.splitext(path)[1].lower() in TEXT_EXT


def is_gitignored(path, root):
    try:
        r = subprocess.run(
            ["git", "check-ignore", "-q", "--", path],
            cwd=root, capture_output=True,
        )
        return r.returncode == 0
    except Exception:
        return False


def try_roundtrip(s):
    try:
        b = s.encode("cp1252")
    except (UnicodeEncodeError, ValueError):
        return None
    try:
        d = b.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if d == s:
        return None
    return d


def scan_text(text):
    hits = []
    for m in PAT.finditer(text):
        seg = m.group(0)
        d = try_roundtrip(seg)
        if d is not None:
            ln = text.count("\n", 0, m.start()) + 1
            hits.append((ln, seg, d))
    seen = set()
    uniq = []
    for h in hits:
        if (h[0], h[1]) in seen:
            continue
        seen.add((h[0], h[1]))
        uniq.append(h)
    return uniq


def scan_file(path):
    try:
        with io.open(path, "r", encoding="utf-8", errors="strict") as f:
            text = f.read()
    except (UnicodeDecodeError, OSError):
        return []
    return scan_text(text)


def git_root():
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True,
        )
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return os.getcwd()


def scan_repo(root):
    file_hits = {}
    total = 0
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in SKIP_DIRS]
        for fn in fns:
            fp = os.path.join(dp, fn)
            rel = os.path.relpath(fp, root)
            if not is_text_ext(fp):
                continue
            if is_gitignored(fp, root):
                continue
            hits = scan_file(fp)
            if hits:
                file_hits[rel] = hits
                total += len(hits)
    return file_hits, total


def scan_staged(root):
    out = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
        cwd=root, capture_output=True, text=True,
    ).stdout.splitlines()
    file_hits = {}
    total = 0
    for rel in out:
        fp = os.path.join(root, rel)
        if not os.path.isfile(fp) or not is_text_ext(fp):
            continue
        if is_gitignored(fp, root):
            continue
        hits = scan_file(fp)
        if hits:
            file_hits[rel] = hits
            total += len(hits)
    return file_hits, total


def report(file_hits, total, scope_label):
    print("=== MOJIBAKE RESIDUAL SCAN (%s) ===" % scope_label)
    if not file_hits:
        print("RESULT: ZERO residual mojibake detected.")
        return 0
    print("files with residual mojibake: %d" % len(file_hits))
    print("total residual segments: %d" % total)
    for rel, hits in sorted(file_hits.items()):
        print("\n--- %s (%d seg) ---" % (rel, len(hits)))
        for ln, seg, d in hits[:40]:
            seg_s = seg.encode("unicode_escape").decode("ascii")
            d_s = d.encode("unicode_escape").decode("ascii")
            print("  L%d: '%s' -> '%s'" % (ln, seg_s, d_s))
    return 1


def main(argv):
    root = git_root()
    if "--staged" in argv:
        file_hits, total = scan_staged(root)
        rc = report(file_hits, total, "staged files")
        if file_hits:
            print("\nMOJIBAKE GUARD: %d residual segment(s) in staged files "
                  "-- fix before commit." % total)
        return rc
    files = [a for a in argv if not a.startswith("--")]
    if files:
        file_hits = {}
        total = 0
        for rel in files:
            fp = rel if os.path.isabs(rel) else os.path.join(root, rel)
            if not os.path.isfile(fp) or not is_text_ext(fp):
                continue
            hits = scan_file(fp)
            if hits:
                file_hits[os.path.relpath(fp, root)] = hits
                total += len(hits)
        return report(file_hits, total, "specified files")
    file_hits, total = scan_repo(root)
    return report(file_hits, total, "repo (gitignored skipped)")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
