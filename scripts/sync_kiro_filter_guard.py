#!/usr/bin/env python3
"""
sync_kiro_filter_guard.py - Auto-detect and register all Kiro-relevant providers in kiro_filter_guard.

Scans config.yaml for any provider matching Kiro characteristics:
  - Provider key / name contains 'kiro' or 'pix4k'
  - Protocol format or auth_type is 'kiro'
  - Connection base_url matches 'kiro.', 'pix4k', 'codewhisperer', or AWS Q markers
  - Individual model definitions with owned_by / backend containing 'kiro'

Updates `tools.kiro_filter_guard` in config.yaml:
  - tools.kiro_filter_guard.enabled = true
  - tools.kiro_filter_guard.providers = [all detected kiro providers]
  - tools.kiro_filter_guard.max_input_tokens_by_provider = {...}

Usage:
  python scripts/sync_kiro_filter_guard.py --status
  python scripts/sync_kiro_filter_guard.py --dry-run
  python scripts/sync_kiro_filter_guard.py --apply
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

import yaml

KIRO_HOST_MARKERS = (
    "kiro.",
    "kiro.dev",
    "kiro.pix4k.com",
    "pix4k",
    "codewhisperer.",
    "q.us-east-1.amazonaws.com",
)

DEFAULT_TOKEN_LIMITS = {
    "nguyenha": 550000,
    "kiro": 550000,
    "pix4k": 160000,
}


def find_config_path() -> Path:
    candidates = [
        Path(os.getcwd()) / "config.yaml",
        Path(__file__).resolve().parent.parent / "config.yaml",
    ]
    for c in candidates:
        if c.is_file():
            return c
    raise FileNotFoundError("Could not find config.yaml in workspace.")


def is_kiro_provider(pid: str, pcfg: Dict[str, Any]) -> Tuple[bool, List[str]]:
    reasons: List[str] = []
    pid_lower = pid.lower()

    if "kiro" in pid_lower or "pix4k" in pid_lower:
        reasons.append(f"Provider ID '{pid}' matches kiro/pix4k keyword")

    fmt = str(pcfg.get("format") or "").strip().lower()
    if fmt == "kiro":
        reasons.append("format == 'kiro'")

    auth = str(pcfg.get("auth_type") or "").strip().lower()
    if auth == "kiro":
        reasons.append("auth_type == 'kiro'")

    urls: List[str] = []
    base_url = pcfg.get("base_url")
    if isinstance(base_url, str) and base_url.strip():
        urls.append(base_url.strip())

    for conn in pcfg.get("connections") or []:
        if isinstance(conn, dict):
            c_url = conn.get("base_url")
            if isinstance(c_url, str) and c_url.strip():
                urls.append(c_url.strip())

    for u in urls:
        ul = u.lower()
        for marker in KIRO_HOST_MARKERS:
            if marker in ul:
                reasons.append(f"URL '{u}' matches host marker '{marker}'")
                break

    for m in pcfg.get("models") or []:
        if isinstance(m, dict):
            mid = str(m.get("id") or m.get("name") or "")
            for key in ("backend", "owned_by", "upstream_owned_by"):
                val = str(m.get(key) or "").strip().lower()
                if "kiro" in val or "pix4k" in val:
                    reasons.append(f"Model '{mid}' {key} == '{val}'")

    return bool(reasons), reasons


def scan_kiro_providers(cfg: Dict[str, Any]) -> Dict[str, List[str]]:
    providers = cfg.get("providers") or {}
    detected: Dict[str, List[str]] = {}
    if not isinstance(providers, dict):
        return detected

    for pid, pdata in providers.items():
        if not isinstance(pdata, dict):
            continue
        is_kiro, reasons = is_kiro_provider(pid, pdata)
        if is_kiro:
            detected[pid] = reasons

    return detected


def sync_guard_config(
    cfg: Dict[str, Any], detected: Dict[str, List[str]]
) -> Tuple[Dict[str, Any], bool, Dict[str, Any]]:
    tools = cfg.setdefault("tools", {})
    if not isinstance(tools, dict):
        tools = {}
        cfg["tools"] = tools

    guard = tools.setdefault("kiro_filter_guard", {})
    if not isinstance(guard, dict):
        guard = {}
        tools["kiro_filter_guard"] = guard

    changed = False
    diffs: Dict[str, Any] = {}

    # 1. enabled
    if guard.get("enabled") is not True:
        diffs["enabled"] = {"before": guard.get("enabled"), "after": True}
        guard["enabled"] = True
        changed = True

    # 2. providers list
    existing_providers = list(guard.get("providers") or [])
    new_providers_set = set(str(p).strip().lower() for p in existing_providers)
    for p in detected.keys():
        new_providers_set.add(p.strip().lower())
    # Ensure standard baselines
    new_providers_set.add("nguyenha")
    new_providers_set.add("kiro")

    sorted_providers = sorted(new_providers_set)
    if sorted_providers != existing_providers:
        diffs["providers"] = {"before": existing_providers, "after": sorted_providers}
        guard["providers"] = sorted_providers
        changed = True

    # 3. max_input_tokens_by_provider
    token_limits = guard.setdefault("max_input_tokens_by_provider", {})
    if not isinstance(token_limits, dict):
        token_limits = {}
        guard["max_input_tokens_by_provider"] = token_limits

    limits_diff: Dict[str, Any] = {}
    for p in sorted_providers:
        default_budget = DEFAULT_TOKEN_LIMITS.get(p, 550000)
        current_budget = token_limits.get(p)
        if current_budget is None:
            token_limits[p] = default_budget
            limits_diff[p] = {"before": None, "after": default_budget}
            changed = True

    if limits_diff:
        diffs["max_input_tokens_by_provider"] = limits_diff

    return cfg, changed, diffs


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Auto-detect Kiro providers and sync kiro_filter_guard config."
    )
    parser.add_argument("--status", action="store_true", help="Print current detection status.")
    parser.add_argument("--dry-run", action="store_true", help="Show changes without writing.")
    parser.add_argument("--apply", action="store_true", help="Apply updates to config.yaml.")
    parser.add_argument("--json", action="store_true", help="Output machine-readable JSON.")
    args = parser.parse_args()

    config_path = find_config_path()
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    detected = scan_kiro_providers(cfg)
    updated_cfg, changed, diffs = sync_guard_config(copy.deepcopy(cfg), detected)

    if args.json:
        result = {
            "config_path": str(config_path),
            "detected_providers": detected,
            "changed": changed,
            "diffs": diffs,
        }
        print(json.dumps(result, indent=2))
        return 0

    print(f"=== Kiro Provider Scanner ({config_path.name}) ===")
    print(f"Detected {len(detected)} Kiro-relevant provider(s):")
    for pid, reasons in detected.items():
        print(f"  * {pid}:")
        for r in reasons:
            print(f"      - {r}")

    if not changed:
        print("\n[OK] kiro_filter_guard in config.yaml is already 100% synchronized.")
        return 0

    print("\nProposed Changes to tools.kiro_filter_guard:")
    for k, v in diffs.items():
        print(f"  {k}:")
        print(f"    before: {v.get('before')}")
        print(f"    after:  {v.get('after')}")

    if args.apply:
        backup_path = config_path.with_name(f"{config_path.stem}.bak.{int(os.path.getmtime(config_path))}.yaml")
        shutil.copy2(config_path, backup_path)
        print(f"\n[Backup] Saved copy to {backup_path.name}")

        with open(config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(updated_cfg, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

        print(f"[SUCCESS] Updated {config_path} with synchronized Kiro providers.")
    else:
        print("\nRun with --apply to commit these changes to config.yaml.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
