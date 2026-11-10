"""Unified Proxy Hub Link & Per-Provider Collision-Free Lane Allocator.

Bridges BSL Router (:6969) to ``@studio/proxy-hub`` (``confs/lanes_state.json``
and ``confs/urnetwork_vault.json``) so a single unified proxy link
(``proxyhub://auto``) automatically allocates different API keys / OAuth
accounts within the same provider to distinct, orthogonal proxy lanes.

Orthogonality invariants (Sovereign Mesh Ops Rule A.1):
1. No two connections in the same provider share the same ``lane_id`` or
   concrete ``http_proxy_url`` (while distinct lanes remain available).
2. Pass 1 enforces strict orthogonality across:
   - ``lane_id``
   - ``account_id`` (URnetwork account isolation)
   - ``ipv4_slash24`` (no two sibling keys in the same ``a.b.c.0/24`` subnet)
   - ``asn`` (Autonomous System Number extracted from ``org``)
3. Blacklisted regions (``NL``) and low-quota lanes (``< 5.0%`` or
   ``quota_low=True``) are never allocated.
4. Sticky binding: once a connection is assigned a ``lane_id`` (via
   ``conn["proxy_lane_meta"]["lane_id"]`` or the runtime sticky table), it
   retains that lane across config saves and runtime calls unless the lane
   becomes unhealthy/low-quota or collides with an earlier sibling.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

UNIFIED_PROXY_HUB_URI = "proxyhub://auto"
BLACKLISTED_COUNTRIES = frozenset({"NL"})
MIN_QUOTA_REMAINING_PCT = 5.0

_REPO_ROOT = Path(__file__).resolve().parent.parent

_CACHE_LOCK = threading.Lock()
_LANES_CACHE: Dict[str, Any] = {
    "confs_dir": None,
    "lanes_mtime": -1.0,
    "vault_mtime": -1.0,
    "lanes": [],
}
# Runtime sticky assignments: (provider_name, conn_identity_str) -> lane_id
_STICKY_ASSIGNMENTS: Dict[Tuple[str, str], str] = {}

_ASN_RE = re.compile(r"\b(AS\d+)\b", re.IGNORECASE)


def is_unified_proxy_link(proxy_url: Optional[str]) -> bool:
    """Return True if ``proxy_url`` is a Unified Proxy Hub link."""
    if not isinstance(proxy_url, str):
        return False
    raw = proxy_url.strip().lower()
    if not raw:
        return False
    return (
        raw.startswith("proxyhub://")
        or raw.startswith("proxy-hub://")
        or raw.startswith("hub://")
        or raw in ("proxyhub", "proxy-hub", "proxyhub:auto")
    )


def normalize_unified_proxy_link(proxy_url: Optional[str]) -> str:
    """Canonicalize shorthand unified links to ``proxyhub://...``."""
    if not isinstance(proxy_url, str):
        return UNIFIED_PROXY_HUB_URI
    raw = proxy_url.strip()
    low = raw.lower()
    if low in ("", "proxyhub", "proxy-hub", "proxyhub:auto", "hub://auto", "proxy-hub://auto"):
        return UNIFIED_PROXY_HUB_URI
    if low.startswith("proxy-hub://"):
        return "proxyhub://" + raw[len("proxy-hub://"):]
    if low.startswith("hub://"):
        return "proxyhub://" + raw[len("hub://"):]
    return raw


def parse_unified_proxy_link(proxy_url: Optional[str]) -> Dict[str, Any]:
    """Parse ``proxyhub://<mode>[/<target>][?country=XX&protocol=http|socks5]``."""
    norm = normalize_unified_proxy_link(proxy_url)
    parsed = urlparse(norm)
    host = (parsed.netloc or "auto").strip().lower()
    path = (parsed.path or "").strip("/")
    qs = parse_qs(parsed.query or "")

    mode = host or "auto"
    pinned_lane_id = ""
    pinned_slot: Optional[int] = None

    if mode in ("lane", "id") and path:
        pinned_lane_id = path.strip()
        mode = "pinned"
    elif mode == "slot" and path.isdigit():
        pinned_slot = int(path)
        mode = "slot"
    elif mode.startswith("lane-"):
        pinned_lane_id = mode
        mode = "pinned"

    if "lane" in qs and qs["lane"]:
        pinned_lane_id = str(qs["lane"][0]).strip()
    if "slot" in qs and qs["slot"] and str(qs["slot"][0]).isdigit():
        pinned_slot = int(qs["slot"][0])

    country = ""
    if "country" in qs and qs["country"]:
        country = str(qs["country"][0]).strip().upper()

    protocol = "http"
    if mode == "local":
        protocol = "local_socks"
    elif "protocol" in qs and qs["protocol"]:
        proto_arg = str(qs["protocol"][0]).strip().lower()
        if proto_arg in ("socks", "socks5", "socks5h"):
            protocol = "socks5"
        elif proto_arg in ("https",):
            protocol = "https"
        elif proto_arg in ("local", "local_socks", "wireproxy"):
            protocol = "local_socks"

    return {
        "raw": norm,
        "mode": mode,
        "pinned_lane_id": pinned_lane_id,
        "pinned_slot": pinned_slot,
        "country": country,
        "protocol": protocol,
    }


def _ipv4_slash24(ip_str: Optional[str]) -> str:
    """Return ``a.b.c.0/24`` for a valid IPv4 address, else empty string."""
    if not isinstance(ip_str, str):
        return ""
    raw = ip_str.strip()
    if not raw or raw.lower() == "unknown":
        return ""
    try:
        addr = ipaddress.ip_address(raw)
        if isinstance(addr, ipaddress.IPv4Address):
            net = ipaddress.IPv4Network(f"{raw}/24", strict=False)
            return str(net)
    except ValueError:
        pass
    return ""


def _extract_asn(org_str: Optional[str]) -> str:
    """Extract uppercase ``AS<number>`` from an organization string if present."""
    if not isinstance(org_str, str) or not org_str.strip():
        return ""
    m = _ASN_RE.search(org_str)
    if m:
        return m.group(1).upper()
    return org_str.strip().upper()[:32]


def resolve_proxy_hub_confs_dir(config: Optional[dict] = None) -> Optional[Path]:
    """Dynamically locate ``@studio/proxy-hub/confs`` without hardcoded machine paths."""
    candidates: List[Path] = []

    env_confs = os.environ.get("BSL_PROXY_HUB_CONFS_DIR", "").strip()
    if env_confs:
        candidates.append(Path(env_confs))

    env_root = os.environ.get("BSL_PROXY_HUB_ROOT", "").strip()
    if env_root:
        root_p = Path(env_root)
        candidates.append(root_p / "confs")
        candidates.append(root_p / "packages" / "proxy-hub" / "confs")

    if isinstance(config, dict):
        ph_cfg = config.get("proxy_hub")
        if isinstance(ph_cfg, dict):
            cfg_dir = str(ph_cfg.get("confs_dir") or "").strip()
            if cfg_dir:
                candidates.append(Path(cfg_dir))

    # Relative sibling discovery from workspace parent(s)
    for base in (_REPO_ROOT.parent, _REPO_ROOT.parent.parent):
        if not base or not base.exists():
            continue
        candidates.append(base / "Studio Core" / "packages" / "proxy-hub" / "confs")
        candidates.append(base / "Studio Setup" / "packages" / "proxy-hub" / "confs")
        candidates.append(base / "proxy-hub" / "confs")

    seen = set()
    for cand in candidates:
        try:
            resolved = cand.resolve()
        except OSError:
            continue
        key = str(resolved).lower()
        if key in seen:
            continue
        seen.add(key)
        lanes_file = resolved / "lanes_state.json"
        if lanes_file.is_file():
            return resolved

    return None


def _parse_lanes_from_confs(confs_dir: Path) -> List[Dict[str, Any]]:
    """Read and normalize all healthy lanes from ``lanes_state.json`` + ``urnetwork_vault.json``."""
    lanes_file = confs_dir / "lanes_state.json"
    vault_file = confs_dir / "urnetwork_vault.json"

    state: Dict[str, Any] = {}
    if lanes_file.is_file():
        try:
            state = json.loads(lanes_file.read_text(encoding="utf-8"))
        except Exception:
            state = {}

    # Index slot metadata from `lanes` (15 slots with c1 + c2 searxng fields)
    slot_meta: Dict[Tuple[int, str], Dict[str, Any]] = {}
    for row in state.get("lanes") or []:
        if not isinstance(row, dict):
            continue
        slot_num = int(row.get("slot") or 0)
        if slot_num <= 0:
            continue
        slot_meta[(slot_num, "c1")] = {
            "lane_id": str(row.get("lane_id") or f"lane-{slot_num:03d}"),
            "slot": slot_num,
            "client_slot": "c1",
            "http_proxy_url": str(row.get("http_proxy_url") or "").strip(),
            "https_proxy_url": str(row.get("https_proxy_url") or "").strip(),
            "socks_proxy_url": str(row.get("socks_proxy_url") or "").strip(),
            "exit_port": row.get("exit_port"),
            "exit_ip": str(row.get("exit_ip") or "").strip(),
            "country": str(row.get("country") or "").strip().upper(),
            "org": str(row.get("org") or "").strip(),
            "account_id": str(row.get("urnetwork_account_id") or "").strip(),
            "client_id": str(row.get("urnetwork_client_id") or "").strip(),
            "quota_remaining_pct": float(row.get("quota_remaining_pct") or 100.0),
            "quota_low": bool(row.get("quota_low", False)),
            "healthy": bool(row.get("healthy", True)),
        }
        c2_http = str(row.get("searxng_http_proxy_url") or "").strip()
        if c2_http:
            slot_meta[(slot_num, "c2")] = {
                "lane_id": str(row.get("searxng_lane_id") or f"lane-{slot_num + 15:03d}"),
                "slot": slot_num,
                "client_slot": "c2",
                "http_proxy_url": c2_http,
                "https_proxy_url": str(row.get("searxng_https_proxy_url") or "").strip(),
                "socks_proxy_url": str(row.get("searxng_socks_proxy_url") or "").strip(),
                "exit_port": row.get("searxng_exit_port"),
                "exit_ip": str(row.get("searxng_exit_ip") or "").strip(),
                "country": str(row.get("searxng_country") or "").strip().upper(),
                "org": str(row.get("searxng_org") or "").strip(),
                "account_id": str(row.get("urnetwork_account_id") or "").strip(),
                "client_id": str(row.get("searxng_client_id") or "").strip(),
                "quota_remaining_pct": float(row.get("quota_remaining_pct") or 100.0),
                "quota_low": bool(row.get("quota_low", False)),
                "healthy": bool(row.get("healthy", True)),
            }

    normalized: List[Dict[str, Any]] = []
    seen_urls = set()

    def _add_lane(entry: Dict[str, Any], tier: str, priority_order: int) -> None:
        http_url = str(entry.get("http_proxy_url") or "").strip()
        socks_url = str(entry.get("socks_proxy_url") or "").strip()
        if not http_url and not socks_url:
            return
        country = str(entry.get("country") or "").strip().upper()
        if country in BLACKLISTED_COUNTRIES:
            return
        if not entry.get("healthy", True):
            return
        quota_pct = float(entry.get("quota_remaining_pct") or 100.0)
        if entry.get("quota_low", False) or quota_pct < MIN_QUOTA_REMAINING_PCT:
            return
        dedup_key = http_url or socks_url
        if dedup_key in seen_urls:
            return
        seen_urls.add(dedup_key)

        exit_ip = str(entry.get("exit_ip") or "").strip()
        if exit_ip.lower() == "unknown":
            exit_ip = ""
        org = str(entry.get("org") or "").strip()
        if org.lower() == "unknown":
            org = ""
        exit_port = entry.get("exit_port")
        local_socks_url = (
            f"socks5h://127.0.0.1:{int(exit_port)}"
            if isinstance(exit_port, int) and exit_port > 0
            else ""
        )

        normalized.append(
            {
                "lane_id": str(entry.get("lane_id") or f"lane-{len(normalized) + 1:03d}"),
                "slot": int(entry.get("slot") or 0),
                "client_slot": str(entry.get("client_slot") or "c1"),
                "tier": tier,
                "priority_order": priority_order,
                "account_id": str(entry.get("account_id") or "").strip(),
                "client_id": str(entry.get("client_id") or "").strip(),
                "http_proxy_url": http_url,
                "https_proxy_url": str(entry.get("https_proxy_url") or "").strip(),
                "socks_proxy_url": socks_url,
                "local_socks_url": local_socks_url,
                "exit_ip": exit_ip,
                "ipv4_slash24": _ipv4_slash24(exit_ip),
                "country": country or "XX",
                "org": org,
                "asn": _extract_asn(org),
                "quota_remaining_pct": quota_pct,
            }
        )

    # 1. Merge hot_lanes_30 with slot_meta (match by exact http_proxy_url first, then (slot, client_slot))
    slot_meta_by_url: Dict[str, Dict[str, Any]] = {
        m["http_proxy_url"]: m
        for m in slot_meta.values()
        if m.get("http_proxy_url")
    }
    hot_list = state.get("hot_lanes_30") or []
    if isinstance(hot_list, list) and hot_list:
        for h in hot_list:
            if not isinstance(h, dict):
                continue
            slot_num = int(h.get("slot") or 0)
            c_slot = str(h.get("client_slot") or "c1").strip().lower()
            h_url = str(h.get("http_proxy_url") or "").strip()
            base = dict(
                slot_meta_by_url.get(h_url)
                or slot_meta.get((slot_num, c_slot))
                or {}
            )
            hot_idx = int(h.get("hot_index") or 0)
            lane_id = f"lane-{hot_idx:03d}" if hot_idx > 0 else (
                base.get("lane_id") or f"lane-{slot_num:03d}"
            )
            merged = {
                **base,
                "lane_id": lane_id,
                "slot": slot_num,
                "client_slot": c_slot,
                "account_id": str(h.get("account_id") or base.get("account_id") or "").strip(),
                "client_id": str(h.get("client_id") or base.get("client_id") or "").strip(),
                "http_proxy_url": str(h.get("http_proxy_url") or base.get("http_proxy_url") or "").strip(),
                "https_proxy_url": str(h.get("https_proxy_url") or base.get("https_proxy_url") or "").strip(),
                "socks_proxy_url": str(h.get("socks_proxy_url") or base.get("socks_proxy_url") or "").strip(),
                "exit_ip": str(h.get("exit_ip") or base.get("exit_ip") or "").strip(),
                "country": str(h.get("exit_country") or h.get("country") or base.get("country") or "").strip().upper(),
                "org": str(base.get("org") or h.get("org") or "").strip(),
                "quota_remaining_pct": float(
                    h.get("quota_remaining_pct")
                    if h.get("quota_remaining_pct") is not None
                    else base.get("quota_remaining_pct", 100.0)
                ),
                "quota_low": bool(base.get("quota_low", False)),
                "healthy": bool(base.get("healthy", True)),
            }
            # Prefer c1 (LLM dedicated) before c2 (SearXNG shared)
            prio = 1 if c_slot == "c1" else 2
            _add_lane(merged, tier="hot", priority_order=prio)
    else:
        # Fallback if hot_lanes_30 is absent: use slot_meta directly
        for (slot_num, c_slot), meta in sorted(slot_meta.items()):
            prio = 1 if c_slot == "c1" else 2
            _add_lane(meta, tier="hot", priority_order=prio)

    # 2. Standby lanes (Tier 2 warm pool)
    standby_list = state.get("standby_lanes_10") or state.get("standby_queue") or []
    if isinstance(standby_list, list):
        for idx, sb in enumerate(standby_list, start=1):
            if not isinstance(sb, dict):
                continue
            sb_idx = int(sb.get("standby_index") or idx)
            _add_lane(
                {
                    "lane_id": f"standby-{sb_idx:03d}",
                    "slot": 0,
                    "client_slot": str(sb.get("client_slot") or "c1"),
                    "account_id": str(sb.get("account_id") or "").strip(),
                    "client_id": str(sb.get("client_id") or "").strip(),
                    "http_proxy_url": str(sb.get("http_proxy_url") or "").strip(),
                    "https_proxy_url": str(sb.get("https_proxy_url") or "").strip(),
                    "socks_proxy_url": str(sb.get("socks_proxy_url") or "").strip(),
                    "exit_ip": str(sb.get("exit_ip") or "").strip(),
                    "country": str(sb.get("exit_country") or sb.get("country") or "").strip().upper(),
                    "org": str(sb.get("org") or "").strip(),
                    "quota_remaining_pct": float(sb.get("quota_remaining_pct") or 100.0),
                    "quota_low": bool(sb.get("quota_low", False)),
                    "healthy": True,
                },
                tier="standby",
                priority_order=3,
            )

    # 3. Vault lanes (Tier 3 deep pool)
    if vault_file.is_file():
        try:
            vault = json.loads(vault_file.read_text(encoding="utf-8"))
            v_idx = 0
            for acct in vault.get("accounts") or []:
                if not isinstance(acct, dict):
                    continue
                acct_id = str(acct.get("account_id") or acct.get("id") or "").strip()
                q_pct = float(acct.get("quota_remaining_pct") or 100.0)
                for c_slot in ("c1", "c2"):
                    c_obj = acct.get(c_slot)
                    if not isinstance(c_obj, dict):
                        continue
                    v_idx += 1
                    _add_lane(
                        {
                            "lane_id": f"vault-{v_idx:03d}",
                            "slot": 0,
                            "client_slot": c_slot,
                            "account_id": acct_id,
                            "client_id": str(c_obj.get("client_id") or "").strip(),
                            "http_proxy_url": str(c_obj.get("http_proxy_url") or "").strip(),
                            "https_proxy_url": str(c_obj.get("https_proxy_url") or "").strip(),
                            "socks_proxy_url": str(c_obj.get("socks_proxy_url") or "").strip(),
                            "exit_ip": str(c_obj.get("exit_ip") or "").strip(),
                            "country": str(c_obj.get("exit_country") or c_obj.get("country") or "").strip().upper(),
                            "org": str(c_obj.get("org") or "").strip(),
                            "quota_remaining_pct": q_pct,
                            "healthy": True,
                        },
                        tier="vault",
                        priority_order=4,
                    )
        except Exception:
            pass

    normalized.sort(key=lambda item: (item["priority_order"], item["lane_id"]))
    return normalized


def load_proxy_hub_lanes(
    config: Optional[dict] = None,
    confs_dir: Optional[Path] = None,
    force_reload: bool = False,
) -> List[Dict[str, Any]]:
    """Return cached healthy Proxy Hub lanes, reloading only when file mtimes change."""
    target_dir = confs_dir or resolve_proxy_hub_confs_dir(config)
    if target_dir is None or not target_dir.is_dir():
        return []

    lanes_file = target_dir / "lanes_state.json"
    vault_file = target_dir / "urnetwork_vault.json"
    try:
        l_mtime = lanes_file.stat().st_mtime if lanes_file.is_file() else -1.0
    except OSError:
        l_mtime = -1.0
    try:
        v_mtime = vault_file.stat().st_mtime if vault_file.is_file() else -1.0
    except OSError:
        v_mtime = -1.0

    with _CACHE_LOCK:
        if (
            not force_reload
            and _LANES_CACHE["confs_dir"] == str(target_dir)
            and _LANES_CACHE["lanes_mtime"] == l_mtime
            and _LANES_CACHE["vault_mtime"] == v_mtime
            and _LANES_CACHE["lanes"]
        ):
            return list(_LANES_CACHE["lanes"])

        lanes = _parse_lanes_from_confs(target_dir)
        _LANES_CACHE["confs_dir"] = str(target_dir)
        _LANES_CACHE["lanes_mtime"] = l_mtime
        _LANES_CACHE["vault_mtime"] = v_mtime
        _LANES_CACHE["lanes"] = lanes
        return list(lanes)


def _select_lane_url(lane: Dict[str, Any], protocol: str = "http") -> str:
    """Return the concrete proxy URL from a normalized lane dict for ``protocol``."""
    if protocol == "local_socks":
        return (
            lane.get("local_socks_url")
            or lane.get("socks_proxy_url")
            or lane.get("http_proxy_url")
            or ""
        )
    if protocol == "socks5":
        return (
            lane.get("socks_proxy_url")
            or lane.get("local_socks_url")
            or lane.get("http_proxy_url")
            or ""
        )
    if protocol == "https":
        return lane.get("https_proxy_url") or lane.get("http_proxy_url") or ""
    return lane.get("http_proxy_url") or lane.get("socks_proxy_url") or ""


def _conn_identity_key(conn: Dict[str, Any], fallback_idx: int = 0) -> str:
    """Return a stable string key identifying a connection within a provider."""
    for field in ("id", "email", "name"):
        val = str(conn.get(field) or "").strip()
        if val:
            return f"{field}:{val}"
    for secret_field in ("api_key", "access_token", "refresh_token"):
        sec = str(conn.get(secret_field) or "").strip()
        if sec:
            digest = hashlib.sha256(sec.encode("utf-8")).hexdigest()[:16]
            return f"{secret_field}:{digest}"
    return f"idx:{fallback_idx}"


def _filter_candidates_for_spec(
    all_lanes: List[Dict[str, Any]], spec: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """Filter candidate lanes according to the parsed unified link spec."""
    mode = spec.get("mode") or "auto"
    pinned_lane_id = (spec.get("pinned_lane_id") or "").strip().lower()
    pinned_slot = spec.get("pinned_slot")
    country = (spec.get("country") or "").strip().upper()

    if pinned_lane_id:
        exact = [l for l in all_lanes if l["lane_id"].lower() == pinned_lane_id]
        if exact:
            return exact

    if pinned_slot is not None:
        slot_matches = [l for l in all_lanes if l.get("slot") == pinned_slot]
        if slot_matches:
            return slot_matches

    candidates = list(all_lanes)
    if mode == "hot":
        candidates = [l for l in candidates if l.get("tier") == "hot"] or candidates
    elif mode == "c1":
        candidates = [l for l in candidates if l.get("client_slot") == "c1" and l.get("tier") == "hot"] or candidates
    elif mode == "c2":
        candidates = [l for l in candidates if l.get("client_slot") == "c2" and l.get("tier") == "hot"] or candidates
    elif mode == "standby":
        candidates = [l for l in candidates if l.get("tier") == "standby"] or candidates
    elif mode == "local":
        candidates = [l for l in candidates if l.get("local_socks_url")] or candidates

    if country:
        country_matches = [l for l in candidates if l.get("country") == country]
        if country_matches:
            candidates = country_matches

    return candidates


def _build_lane_meta(lane: Dict[str, Any], unified_uri: str = UNIFIED_PROXY_HUB_URI) -> Dict[str, Any]:
    """Build the clean metadata dict stored on ``conn['proxy_lane_meta']``."""
    return {
        "lane_id": lane["lane_id"],
        "slot": lane.get("slot", 0),
        "client_slot": lane.get("client_slot", "c1"),
        "tier": lane.get("tier", "hot"),
        "account_id": lane.get("account_id", ""),
        "country": lane.get("country", "XX"),
        "exit_ip": lane.get("exit_ip", ""),
        "ipv4_slash24": lane.get("ipv4_slash24", ""),
        "org": lane.get("org", ""),
        "asn": lane.get("asn", ""),
        "unified_uri": unified_uri,
    }


def allocate_provider_lanes(
    provider_name: str,
    connections: List[Dict[str, Any]],
    config: Optional[dict] = None,
    confs_dir: Optional[Path] = None,
    auto_assign_unconfigured: bool = False,
) -> Dict[int, Dict[str, Any]]:
    """Allocate collision-free, orthogonal Proxy Hub lanes across ``connections`` in one provider.

    Returns a mapping ``{conn_index: chosen_lane_dict}`` and updates
    ``conn["proxy_lane_meta"]`` in-place for every connection using a Unified
    Proxy Hub link (or all connections when ``auto_assign_unconfigured=True``).
    """
    if not isinstance(connections, list) or not connections:
        return {}

    all_lanes = load_proxy_hub_lanes(config=config, confs_dir=confs_dir)
    if not all_lanes:
        return {}

    lanes_by_id = {l["lane_id"]: l for l in all_lanes}
    lanes_by_http = {l["http_proxy_url"]: l for l in all_lanes if l.get("http_proxy_url")}
    lanes_by_socks = {l["socks_proxy_url"]: l for l in all_lanes if l.get("socks_proxy_url")}

    used_lane_ids: set = set()
    used_urls: set = set()
    used_accounts: set = set()
    used_slash24s: set = set()
    used_asns: set = set()

    assignments: Dict[int, Dict[str, Any]] = {}

    def _mark_lane_used(lane: Dict[str, Any]) -> None:
        used_lane_ids.add(lane["lane_id"])
        if lane.get("http_proxy_url"):
            used_urls.add(lane["http_proxy_url"])
        if lane.get("socks_proxy_url"):
            used_urls.add(lane["socks_proxy_url"])
        if lane.get("account_id"):
            used_accounts.add(lane["account_id"])
        if lane.get("ipv4_slash24"):
            used_slash24s.add(lane["ipv4_slash24"])
        if lane.get("asn"):
            used_asns.add(lane["asn"])

    # Pre-pass A: record concrete (non-unified) proxy URLs that match known lanes
    for idx, conn in enumerate(connections):
        if not isinstance(conn, dict):
            continue
        raw_url = str(conn.get("proxy_url") or "").strip()
        if raw_url and not is_unified_proxy_link(raw_url):
            matched = lanes_by_http.get(raw_url) or lanes_by_socks.get(raw_url)
            if matched and matched["lane_id"] not in used_lane_ids:
                _mark_lane_used(matched)

    # Pre-pass B: preserve existing sticky / metadata lane assignments if still healthy & non-colliding
    pending_indices: List[int] = []
    for idx, conn in enumerate(connections):
        if not isinstance(conn, dict):
            continue
        raw_url = str(conn.get("proxy_url") or "").strip()
        if auto_assign_unconfigured and not raw_url:
            raw_url = UNIFIED_PROXY_HUB_URI
            conn["proxy_url"] = UNIFIED_PROXY_HUB_URI
        if not is_unified_proxy_link(raw_url):
            continue

        spec = parse_unified_proxy_link(raw_url)
        conn_key = _conn_identity_key(conn, idx)
        sticky_id = ""
        if spec.get("pinned_lane_id"):
            sticky_id = spec["pinned_lane_id"]
        else:
            meta = conn.get("proxy_lane_meta")
            if isinstance(meta, dict) and meta.get("lane_id"):
                sticky_id = str(meta["lane_id"]).strip()
            if not sticky_id:
                sticky_id = _STICKY_ASSIGNMENTS.get((provider_name, conn_key), "")

        if sticky_id and sticky_id in lanes_by_id and sticky_id not in used_lane_ids:
            lane = lanes_by_id[sticky_id]
            if not spec.get("country") or lane.get("country") == spec["country"]:
                _mark_lane_used(lane)
                assignments[idx] = lane
                conn["proxy_lane_meta"] = _build_lane_meta(lane, spec["raw"])
                _STICKY_ASSIGNMENTS[(provider_name, conn_key)] = lane["lane_id"]
                continue

        pending_indices.append(idx)

    # Multi-pass orthogonality allocation for remaining connections
    for idx in pending_indices:
        conn = connections[idx]
        raw_url = str(conn.get("proxy_url") or UNIFIED_PROXY_HUB_URI).strip()
        spec = parse_unified_proxy_link(raw_url)
        candidates = _filter_candidates_for_spec(all_lanes, spec)
        if not candidates:
            candidates = all_lanes

        conn_key = _conn_identity_key(conn, idx)
        chosen: Optional[Dict[str, Any]] = None

        # Pass 1: Strict Orthogonality (distinct lane + account + /24 subnet + ASN)
        for cand in candidates:
            if cand["lane_id"] in used_lane_ids:
                continue
            if cand.get("account_id") and cand["account_id"] in used_accounts:
                continue
            if cand.get("ipv4_slash24") and cand["ipv4_slash24"] in used_slash24s:
                continue
            if cand.get("asn") and cand["asn"] in used_asns:
                continue
            chosen = cand
            break

        # Pass 2: Account + /24 Subnet Orthogonality
        if chosen is None:
            for cand in candidates:
                if cand["lane_id"] in used_lane_ids:
                    continue
                if cand.get("account_id") and cand["account_id"] in used_accounts:
                    continue
                if cand.get("ipv4_slash24") and cand["ipv4_slash24"] in used_slash24s:
                    continue
                chosen = cand
                break

        # Pass 3: Distinct Lane / Proxy URL within the provider
        if chosen is None:
            for cand in candidates:
                if cand["lane_id"] in used_lane_ids:
                    continue
                if cand.get("http_proxy_url") and cand["http_proxy_url"] in used_urls:
                    continue
                chosen = cand
                break

        # Pass 4: Wrap-around deterministic fallback if connections > total lanes
        if chosen is None:
            chosen = candidates[idx % len(candidates)]

        _mark_lane_used(chosen)
        assignments[idx] = chosen
        conn["proxy_lane_meta"] = _build_lane_meta(chosen, spec["raw"])
        _STICKY_ASSIGNMENTS[(provider_name, conn_key)] = chosen["lane_id"]

    return assignments


def resolve_unified_proxy_url(
    proxy_url: Optional[str],
    active_conn: Optional[Dict[str, Any]],
    provider_name: str = "",
    config: Optional[dict] = None,
    confs_dir: Optional[Path] = None,
) -> Optional[str]:
    """Resolve a Unified Proxy Hub link (``proxyhub://auto``) to a concrete proxy URL at runtime.

    If ``proxy_url`` is already a standard ``http://`` / ``socks5://`` URL, returns
    it unchanged. If ``proxy_url`` is a unified link, allocates a collision-free
    lane across all sibling connections of ``provider_name`` and returns the
    concrete proxy URL for ``active_conn``.
    """
    if not isinstance(proxy_url, str) or not proxy_url.strip():
        # Check provider-level proxy_url or auto_proxy_hub
        if isinstance(config, dict) and provider_name:
            prov_cfg = (config.get("providers") or {}).get(provider_name)
            if isinstance(prov_cfg, dict):
                prov_proxy = str(prov_cfg.get("proxy_url") or "").strip()
                if prov_proxy:
                    proxy_url = prov_proxy
                elif prov_cfg.get("auto_proxy_hub") is True:
                    proxy_url = UNIFIED_PROXY_HUB_URI
                else:
                    return None
            else:
                return None
        else:
            return None

    if not is_unified_proxy_link(proxy_url):
        return proxy_url

    spec = parse_unified_proxy_link(proxy_url)
    all_lanes = load_proxy_hub_lanes(config=config, confs_dir=confs_dir)
    if not all_lanes:
        return None

    lanes_by_id = {l["lane_id"]: l for l in all_lanes}

    # Gather sibling connections from provider_config if available
    sibling_conns: List[Dict[str, Any]] = []
    if isinstance(config, dict) and provider_name:
        prov_cfg = (config.get("providers") or {}).get(provider_name)
        if isinstance(prov_cfg, dict) and isinstance(prov_cfg.get("connections"), list):
            sibling_conns = prov_cfg["connections"]

    target_idx = -1
    if isinstance(active_conn, dict) and sibling_conns:
        target_id = _conn_identity_key(active_conn, -1)
        for idx, c in enumerate(sibling_conns):
            if c is active_conn or (
                isinstance(c, dict) and _conn_identity_key(c, idx) == target_id
            ):
                target_idx = idx
                break

    if sibling_conns and target_idx >= 0:
        prov_proxy_cfg = ""
        if isinstance(config, dict) and provider_name:
            _pc = (config.get("providers") or {}).get(provider_name)
            if isinstance(_pc, dict):
                prov_proxy_cfg = str(_pc.get("proxy_url") or "").strip()
        alloc = allocate_provider_lanes(
            provider_name,
            sibling_conns,
            config=config,
            confs_dir=confs_dir,
            auto_assign_unconfigured=is_unified_proxy_link(prov_proxy_cfg) or is_unified_proxy_link(proxy_url),
        )
        chosen = alloc.get(target_idx)
        if chosen is not None:
            if isinstance(active_conn, dict):
                active_conn["proxy_lane_meta"] = _build_lane_meta(chosen, spec["raw"])
            return _select_lane_url(chosen, spec["protocol"])

    # Standalone single-connection resolution (when active_conn is not in config snapshot)
    if isinstance(active_conn, dict):
        meta = active_conn.get("proxy_lane_meta")
        if isinstance(meta, dict):
            lid = str(meta.get("lane_id") or "").strip()
            if lid in lanes_by_id:
                return _select_lane_url(lanes_by_id[lid], spec["protocol"])

        conn_key = _conn_identity_key(active_conn, 0)
        sticky_lid = _STICKY_ASSIGNMENTS.get((provider_name, conn_key), "")
        if sticky_lid and sticky_lid in lanes_by_id:
            chosen = lanes_by_id[sticky_lid]
            active_conn["proxy_lane_meta"] = _build_lane_meta(chosen, spec["raw"])
            return _select_lane_url(chosen, spec["protocol"])

        candidates = _filter_candidates_for_spec(all_lanes, spec) or all_lanes
        digest_int = int(hashlib.sha256(conn_key.encode("utf-8")).hexdigest()[:8], 16)
        chosen = candidates[digest_int % len(candidates)]
        _STICKY_ASSIGNMENTS[(provider_name, conn_key)] = chosen["lane_id"]
        active_conn["proxy_lane_meta"] = _build_lane_meta(chosen, spec["raw"])
        return _select_lane_url(chosen, spec["protocol"])

    candidates = _filter_candidates_for_spec(all_lanes, spec) or all_lanes
    return _select_lane_url(candidates[0], spec["protocol"])


def apply_proxy_hub_allocations_to_config(
    config: Optional[dict], confs_dir: Optional[Path] = None
) -> int:
    """Materialize ``proxy_lane_meta`` for all providers in ``config`` that use unified links.

    Returns the total number of connections assigned a Proxy Hub lane.
    """
    if not isinstance(config, dict):
        return 0
    providers = config.get("providers")
    if not isinstance(providers, dict):
        return 0

    total_assigned = 0
    for prov_id, prov_cfg in providers.items():
        if not isinstance(prov_cfg, dict):
            continue
        conns = prov_cfg.get("connections")
        if not isinstance(conns, list) or not conns:
            continue
        prov_proxy = str(prov_cfg.get("proxy_url") or "").strip()
        prov_is_unified = is_unified_proxy_link(prov_proxy)
        auto_all = bool(prov_cfg.get("auto_proxy_hub", False)) or prov_is_unified
        if prov_is_unified:
            for c in conns:
                if isinstance(c, dict) and not str(c.get("proxy_url") or "").strip():
                    c["proxy_url"] = prov_proxy
        has_unified = auto_all or any(
            isinstance(c, dict) and is_unified_proxy_link(c.get("proxy_url"))
            for c in conns
        )
        if not has_unified:
            continue
        assigned = allocate_provider_lanes(
            str(prov_id),
            conns,
            config=config,
            confs_dir=confs_dir,
            auto_assign_unconfigured=auto_all,
        )
        total_assigned += len(assigned)
    return total_assigned


def get_proxy_hub_status_summary(
    config: Optional[dict] = None, confs_dir: Optional[Path] = None
) -> Dict[str, Any]:
    """Return a status summary of Proxy Hub lanes and current per-provider allocations."""
    resolved_dir = confs_dir or resolve_proxy_hub_confs_dir(config)
    all_lanes = load_proxy_hub_lanes(config=config, confs_dir=resolved_dir)
    hot_lanes = [l for l in all_lanes if l.get("tier") == "hot"]
    standby_lanes = [l for l in all_lanes if l.get("tier") == "standby"]
    vault_lanes = [l for l in all_lanes if l.get("tier") == "vault"]

    provider_allocations: Dict[str, List[Dict[str, Any]]] = {}
    if isinstance(config, dict):
        for prov_id, prov_cfg in (config.get("providers") or {}).items():
            if not isinstance(prov_cfg, dict):
                continue
            conns = prov_cfg.get("connections") or []
            if not isinstance(conns, list):
                continue
            prov_rows = []
            for idx, c in enumerate(conns):
                if not isinstance(c, dict):
                    continue
                purl = str(c.get("proxy_url") or "").strip()
                meta = c.get("proxy_lane_meta")
                if is_unified_proxy_link(purl) or isinstance(meta, dict):
                    prov_rows.append(
                        {
                            "index": idx,
                            "name": c.get("name") or c.get("email") or f"Connection #{idx + 1}",
                            "proxy_url": purl,
                            "lane_meta": meta if isinstance(meta, dict) else {},
                        }
                    )
            if prov_rows:
                provider_allocations[str(prov_id)] = prov_rows

    return {
        "available": bool(all_lanes),
        "confs_dir": str(resolved_dir) if resolved_dir else None,
        "unified_uri": UNIFIED_PROXY_HUB_URI,
        "counts": {
            "total_healthy": len(all_lanes),
            "hot": len(hot_lanes),
            "standby": len(standby_lanes),
            "vault": len(vault_lanes),
        },
        "hot_lanes_preview": [
            {
                "lane_id": l["lane_id"],
                "slot": l["slot"],
                "client_slot": l["client_slot"],
                "country": l["country"],
                "exit_ip": l["exit_ip"],
                "ipv4_slash24": l["ipv4_slash24"],
                "org": l["org"],
                "asn": l["asn"],
                "account_id": l["account_id"],
                "quota_remaining_pct": l["quota_remaining_pct"],
            }
            for l in hot_lanes[:30]
        ],
        "provider_allocations": provider_allocations,
    }
