#!/usr/bin/env python3
"""Offline one-time migration: collapse duplicate provider connections in config.yaml.

Bug 3 (2026-09-29): the live config.yaml accumulated duplicate connection rows
(blank-key "dongden" beside a keyed "dongden", same name twice, etc.) because the
save path appended instead of merging and identity was key-dependent. The load-time
``_dedup_connection_rows`` now collapses these on every boot, but the running router
process owns config.yaml -- this script lets an operator clean the on-disk file in a
controlled, audited pass instead of waiting for the next restart to heal it.

There is exactly ONE dedup implementation: it imports ``_dedup_connection_rows`` from
``app.main`` so the migration and the live code path can never drift.

USAGE:
    python scripts/migrate_dedup_connections.py --dry-run --config config.yaml
    python scripts/migrate_dedup_connections.py --apply   --config config.yaml

--dry-run is the DEFAULT (safe). --apply writes a timestamped backup first, then
rewrites config.yaml. The final line of output is ``DRY_RUN ...`` or ``APPLIED ...``
so it is machine-checkable.

The script operates on the file as-is (config.yaml stores encrypted api_key values
under the ``enc:`` scheme). It never decrypts or re-encrypts: a non-empty api_key is
treated as "has a key" regardless of whether it is plaintext or ciphertext, which is
all the dedup logic needs. Re-dump uses the same style as the live save path
(``yaml.dump(..., default_flow_style=False, sort_keys=False)``).
"""
from __future__ import annotations

import argparse
import datetime
import io
import os
import shutil
import sys

# Make ``app.main`` importable when run as a standalone script from the repo root.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import yaml  # noqa: E402

# Single source of truth for dedup. Imported lazily so --help and arg errors do not
# pay the full app.main import cost, and so a broken import is reported clearly.
def _load_dedup():
    try:
        from app.main import _dedup_connection_rows  # type: ignore
    except Exception as exc:  # pragma: no cover - import-time diagnostics
        print(f"[migrate] FATAL: could not import _dedup_connection_rows from app.main: {exc}",
              file=sys.stderr)
        raise
    return _dedup_connection_rows


def _conn_label(c: dict) -> str:
    """Short human label for a connection in the report."""
    name = str(c.get("name") or "").strip()
    key = str(c.get("api_key") or "").strip()
    cid = str(c.get("id") or "").strip()
    if name:
        base = name
    elif cid:
        base = f"id={cid}"
    else:
        base = "<unnamed>"
    if key:
        return f"{base} (key={key[:10]}...)"
    return f"{base} (EMPTY key)"


def _classify_drops(provider_cfg_before: dict, provider_cfg_after: dict,
                    dropped_conns: list) -> list:
    """Classify why each dropped row was removed: dup-name / dup-key / dup-id / blank-sibling."""
    surv_conns = provider_cfg_after.get("connections") or []
    names_after = [str(c.get("name") or "").strip().lower() for c in surv_conns
                   if isinstance(c, dict)]
    keys_after = [str(c.get("api_key") or "").strip() for c in surv_conns
                  if isinstance(c, dict)]
    ids_after = [str(c.get("id") or "").strip() for c in surv_conns
                 if isinstance(c, dict)]
    reasons = []
    for c in dropped_conns:
        if not isinstance(c, dict):
            reasons.append("non-dict row")
            continue
        nm = str(c.get("name") or "").strip()
        key = str(c.get("api_key") or "").strip()
        cid = str(c.get("id") or "").strip()
        if not key and nm and nm.lower() in names_after:
            reasons.append("blank-key-sibling")
        elif not key and (not cid or cid not in ids_after):
            reasons.append("blank-key-orphan")
        elif key and key in keys_after:
            reasons.append("dup-key")
        elif cid and cid in ids_after:
            reasons.append("dup-id")
        elif nm and nm.lower() in names_after:
            reasons.append("dup-name")
        else:
            reasons.append("dup-name" if nm else "duplicate")
    return reasons


def _remap_table(provider_cfg_before: dict, provider_cfg_after: dict,
                 dropped_indices: list) -> list:
    """Build the old_index -> new_index remap table for connection_indexes."""
    surv_conns = provider_cfg_after.get("connections") or []
    # Map each survivor's identity -> its new index.
    from app.main import _connection_identity  # type: ignore
    surv_id_to_new = {}
    for new_i, c in enumerate(surv_conns):
        ident = _connection_identity(c)
        if ident is not None and ident not in surv_id_to_new:
            surv_id_to_new[ident] = new_i
    before_conns = provider_cfg_before.get("connections") or []
    table = []
    for old_i in dropped_indices:
        c = before_conns[old_i] if old_i < len(before_conns) else None
        if not isinstance(c, dict):
            table.append((old_i, None))
            continue
        ident = _connection_identity(c)
        new_i = surv_id_to_new.get(ident) if ident is not None else None
        table.append((old_i, new_i))
    return table


def migrate(config_path: str, apply: bool) -> int:
    dedup = _load_dedup()
    if not os.path.exists(config_path):
        print(f"[migrate] FATAL: config not found: {config_path}", file=sys.stderr)
        return 2

    with io.open(config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    providers = cfg.get("providers") or {}
    if not isinstance(providers, dict):
        print("[migrate] FATAL: providers is not a mapping; refusing.", file=sys.stderr)
        return 2

    total_before = 0
    total_after = 0
    total_dropped = 0
    per_provider_reports = []

    for pid, pcfg in list(providers.items()):
        if not isinstance(pcfg, dict):
            continue
        conns = pcfg.get("connections")
        if not isinstance(conns, list) or len(conns) < 2:
            total_before += len(conns) if isinstance(conns, list) else 0
            total_after += len(conns) if isinstance(conns, list) else 0
            continue
        before = [c for c in conns if isinstance(c, dict)]
        before_n = len(conns)
        total_before += before_n
        # Snapshot identities for dropped-row classification BEFORE mutating.
        before_snapshot = {
            "connections": list(conns),
            "models": [dict(m) if isinstance(m, dict) else m
                       for m in (pcfg.get("models") or [])],
        }
        # Run the single source of truth.
        removed = dedup(pcfg)
        after_conns = pcfg.get("connections") or []
        after_n = len(after_conns)
        total_after += after_n
        total_dropped += removed
        if removed == 0:
            per_provider_reports.append((pid, before_n, after_n, [], []))
            continue
        # Recover dropped original indices by object identity: a row survived
        # iff the very same dict object is still present in after_conns. This
        # is exact for both union-find collapses (survivor kept, copy dropped)
        # and id-less blank-row drops (no survivor, row just gone).
        from app.main import _connection_identity  # type: ignore
        survivor_objs = {id(ac) for ac in after_conns if isinstance(ac, dict)}
        dropped_indices = []
        dropped_conns = []
        for i, c in enumerate(conns):
            if not isinstance(c, dict):
                continue
            if id(c) in survivor_objs:
                continue  # this exact row survived
            dropped_indices.append(i)
            dropped_conns.append(c)
        reasons = _classify_drops(before_snapshot, pcfg, dropped_conns)
        remap = _remap_table(before_snapshot, pcfg, dropped_indices)
        per_provider_reports.append((pid, before_n, after_n, list(zip(
            [_conn_label(c) for c in dropped_conns], reasons)), remap))

    # ── Report ────────────────────────────────────────────────────────────
    print("=" * 72)
    print(f"dedup migration report for: {config_path}")
    print(f"mode: {'APPLY' if apply else 'DRY-RUN (no write)'}")
    print(f"totals: connections {total_before} -> {total_after} ({total_dropped} removed)")
    print("=" * 72)
    collapsing = [r for r in per_provider_reports if r[3]]
    if not collapsing:
        print("No duplicate connection rows found. Nothing to do.")
    for pid, before_n, after_n, drops, remap in per_provider_reports:
        if not drops:
            continue
        print(f"\nprovider={pid}: rows {before_n} -> {after_n}")
        for label, reason in drops:
            print(f"  dropped {label}  reason={reason}")
        if remap:
            print(f"  connection_indexes remap (old_idx -> new_idx):")
            for old_i, new_i in remap:
                tgt = f"new {new_i}" if new_i is not None else "dropped (dangling)"
                print(f"    [{old_i}] -> {tgt}")

    # ── Write (only on --apply) ───────────────────────────────────────────
    if not apply:
        print("\nDRY_RUN no file written. Re-run with --apply to write a backup "
              "and the cleaned config.")
        print("DRY_RUN ok")
        return 0

    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup_path = f"{config_path}.bak-dedup-{ts}"
    shutil.copy2(config_path, backup_path)
    print(f"\n[apply] wrote backup: {backup_path}")
    # Re-dump matching the live save path style.
    enc_dump = yaml.dump(cfg, default_flow_style=False, sort_keys=False)
    with io.open(config_path, "w", encoding="utf-8") as f:
        f.write(enc_dump)
    print(f"[apply] wrote cleaned config: {config_path}")
    print("APPLIED ok")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Collapse duplicate provider connections in config.yaml "
                    "(Bug 3, 2026-09-29). Imports _dedup_connection_rows from app.main.")
    p.add_argument("--config", default="config.yaml",
                   help="path to config.yaml (default: config.yaml)")
    p.add_argument("--dry-run", action="store_true", default=True,
                   help="report only, write nothing (DEFAULT)")
    p.add_argument("--apply", action="store_true", default=False,
                   help="write a timestamped backup then rewrite the cleaned config")
    args = p.parse_args(argv)
    if args.apply and args.dry_run:
        # --apply explicitly overrides the default --dry-run.
        apply = True
    else:
        apply = args.apply
    return migrate(args.config, apply=apply)


if __name__ == "__main__":
    sys.exit(main())
