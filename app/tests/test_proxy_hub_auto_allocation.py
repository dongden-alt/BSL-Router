"""Tests for Unified Proxy Hub Link (`proxyhub://auto`) & Per-Provider Auto-Allocation.

Verifies:
1. Unified proxy link detection, normalization, and query parsing.
2. Multi-key collision-free allocation within the same provider:
   - Distinct `lane_id` and concrete `http_proxy_url` per key.
   - Strict `/24` IPv4 subnet, ASN (`org`), and `account_id` orthogonality.
   - Blacklisted country (`NL`) and low-quota (`< 5%`) lanes are excluded.
3. Sticky preservation: adding a new key to a provider preserves existing keys'
   assigned lanes and allocates a fresh orthogonal lane to the new key.
4. Runtime `_effective_egress_proxy(active_conn, provider_name)` integration:
   - Resolves `proxyhub://auto` to each connection's distinct Proxy Hub URL.
   - Honors `proxy_bypass: True` (returns `None`).
   - Supports provider-level `auto_proxy_hub: True`.
5. `POST /api/config` and `POST /api/proxy-hub/allocate/{provider_id}` endpoints.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from starlette.requests import Request

from app import main
from app.proxy_hub_bridge import (
    UNIFIED_PROXY_HUB_URI,
    _LANES_CACHE,
    _STICKY_ASSIGNMENTS,
    allocate_provider_lanes,
    apply_proxy_hub_allocations_to_config,
    is_unified_proxy_link,
    load_proxy_hub_lanes,
    parse_unified_proxy_link,
    resolve_unified_proxy_url,
)


def _write_synthetic_proxy_hub(confs_dir: Path) -> Path:
    """Create a synthetic `lanes_state.json` with healthy, NL-blacklisted, low-quota, and same-/24 lanes."""
    confs_dir.mkdir(parents=True, exist_ok=True)
    state = {
        "updated_at": 1775544000,
        "lanes": [
            {
                "lane_id": "lane-001",
                "slot": 1,
                "http_proxy_url": "http://tok-slot1-c1:@crisp.bringyour.com:7160",
                "socks_proxy_url": "socks5h://tok-slot1-c1:@crisp.bringyour.com:7159",
                "exit_port": 10801,
                "exit_ip": "82.108.198.101",
                "country": "JP",
                "org": "AS2516 KDDI Corporation",
                "urnetwork_account_id": "urnetwork-acct-01",
                "quota_remaining_pct": 100.0,
                "quota_low": False,
                "healthy": True,
                "searxng_lane_id": "lane-016",
                "searxng_http_proxy_url": "http://tok-slot1-c2:@crisp.bringyour.com:7160",
                "searxng_socks_proxy_url": "socks5h://tok-slot1-c2:@crisp.bringyour.com:7159",
                "searxng_exit_port": 10816,
                "searxng_exit_ip": "82.108.198.202",  # same /24 and same account as lane-001
                "searxng_country": "JP",
                "searxng_org": "AS2516 KDDI Corporation",
            },
            {
                "lane_id": "lane-002",
                "slot": 2,
                "http_proxy_url": "http://tok-slot2-c1:@crisp.bringyour.com:7160",
                "socks_proxy_url": "socks5h://tok-slot2-c1:@crisp.bringyour.com:7159",
                "exit_port": 10802,
                "exit_ip": "118.45.165.142",
                "country": "KR",
                "org": "AS4766 Korea Telecom",
                "urnetwork_account_id": "urnetwork-acct-02",
                "quota_remaining_pct": 95.0,
                "quota_low": False,
                "healthy": True,
            },
            {
                "lane_id": "lane-003",
                "slot": 3,
                "http_proxy_url": "http://tok-slot3-c1:@crisp.bringyour.com:7160",
                "socks_proxy_url": "socks5h://tok-slot3-c1:@crisp.bringyour.com:7159",
                "exit_port": 10803,
                "exit_ip": "216.194.127.66",
                "country": "US",
                "org": "AS401116 Sharktech",
                "urnetwork_account_id": "urnetwork-acct-03",
                "quota_remaining_pct": 92.0,
                "quota_low": False,
                "healthy": True,
            },
            {
                "lane_id": "lane-004",
                "slot": 4,
                "http_proxy_url": "http://tok-slot4-c1:@crisp.bringyour.com:7160",
                "socks_proxy_url": "socks5h://tok-slot4-c1:@crisp.bringyour.com:7159",
                "exit_port": 10804,
                "exit_ip": "146.70.189.200",
                "country": "DE",
                "org": "AS9009 M247 Europe",
                "urnetwork_account_id": "urnetwork-acct-04",
                "quota_remaining_pct": 88.0,
                "quota_low": False,
                "healthy": True,
            },
            {
                "lane_id": "lane-nl-blocked",
                "slot": 5,
                "http_proxy_url": "http://tok-nl:@crisp.bringyour.com:7160",
                "exit_ip": "185.107.56.10",
                "country": "NL",  # Blacklisted country -> must be excluded
                "org": "AS60068 Datacamp",
                "urnetwork_account_id": "urnetwork-acct-05",
                "quota_remaining_pct": 100.0,
                "quota_low": False,
                "healthy": True,
            },
            {
                "lane_id": "lane-low-quota",
                "slot": 6,
                "http_proxy_url": "http://tok-low:@crisp.bringyour.com:7160",
                "exit_ip": "103.152.112.9",
                "country": "SG",
                "org": "AS138915 Kaopu Cloud",
                "urnetwork_account_id": "urnetwork-acct-06",
                "quota_remaining_pct": 2.5,  # < 5% -> must be excluded
                "quota_low": True,
                "healthy": True,
            },
        ],
        "hot_lanes_30": [],
    }
    (confs_dir / "lanes_state.json").write_text(json.dumps(state), encoding="utf-8")
    return confs_dir


@pytest.fixture(autouse=True)
def _reset_proxy_hub_cache():
    _LANES_CACHE["confs_dir"] = None
    _LANES_CACHE["lanes_mtime"] = -1.0
    _LANES_CACHE["vault_mtime"] = -1.0
    _LANES_CACHE["lanes"] = []
    _STICKY_ASSIGNMENTS.clear()
    yield
    _STICKY_ASSIGNMENTS.clear()


def test_unified_proxy_link_parsing():
    assert is_unified_proxy_link("proxyhub://auto")
    assert is_unified_proxy_link("proxyhub://hot?country=KR&protocol=socks5")
    assert is_unified_proxy_link("proxyhub://local")
    assert is_unified_proxy_link("hub://auto")
    assert not is_unified_proxy_link("http://127.0.0.1:8080")
    assert not is_unified_proxy_link(None)

    spec = parse_unified_proxy_link("proxyhub://hot?country=kr&protocol=socks5")
    assert spec["mode"] == "hot"
    assert spec["country"] == "KR"
    assert spec["protocol"] == "socks5"

    local_spec = parse_unified_proxy_link("proxyhub://local")
    assert local_spec["mode"] == "local"
    assert local_spec["protocol"] == "local_socks"


def test_filters_out_nl_and_low_quota_lanes(tmp_path):
    confs_dir = _write_synthetic_proxy_hub(tmp_path / "confs")
    lanes = load_proxy_hub_lanes(confs_dir=confs_dir, force_reload=True)
    lane_ids = {l["lane_id"] for l in lanes}

    assert "lane-nl-blocked" not in lane_ids
    assert "lane-low-quota" not in lane_ids
    assert {"lane-001", "lane-002", "lane-003", "lane-004", "lane-016"}.issubset(lane_ids)


def test_multi_key_same_provider_orthogonal_allocation(tmp_path):
    """4 keys in the same provider with `proxyhub://auto` get 4 distinct, /24 & ASN orthogonal lanes."""
    confs_dir = _write_synthetic_proxy_hub(tmp_path / "confs")
    connections = [
        {"id": "acct-1", "name": "Ultra 1", "proxy_url": UNIFIED_PROXY_HUB_URI},
        {"id": "acct-2", "name": "Ultra 2", "proxy_url": UNIFIED_PROXY_HUB_URI},
        {"id": "acct-3", "name": "Ultra 3", "proxy_url": UNIFIED_PROXY_HUB_URI},
        {"id": "acct-4", "name": "Ultra 4", "proxy_url": UNIFIED_PROXY_HUB_URI},
    ]

    assigned = allocate_provider_lanes("antigravity", connections, confs_dir=confs_dir)
    assert len(assigned) == 4

    assigned_lane_ids = [assigned[i]["lane_id"] for i in range(4)]
    assigned_urls = [assigned[i]["http_proxy_url"] for i in range(4)]
    assigned_slash24s = [assigned[i]["ipv4_slash24"] for i in range(4)]
    assigned_asns = [assigned[i]["asn"] for i in range(4)]
    assigned_accounts = [assigned[i]["account_id"] for i in range(4)]

    # All 4 keys must receive completely distinct lanes, URLs, /24 subnets, ASNs, and URnetwork accounts.
    # Notice lane-016 shares /24 and account_id with lane-001, so Pass 1 skips lane-016 and picks
    # lane-001, lane-002, lane-003, lane-004!
    assert len(set(assigned_lane_ids)) == 4
    assert set(assigned_lane_ids) == {"lane-001", "lane-002", "lane-003", "lane-004"}
    assert len(set(assigned_urls)) == 4
    assert len(set(assigned_slash24s)) == 4
    assert len(set(assigned_asns)) == 4
    assert len(set(assigned_accounts)) == 4

    # Verify each connection has proxy_lane_meta stamped
    for i, conn in enumerate(connections):
        assert conn["proxy_lane_meta"]["lane_id"] == assigned_lane_ids[i]


def test_adding_new_key_preserves_existing_sticky_lanes(tmp_path):
    """Adding a 3rd key to a provider must NOT shuffle the lanes already assigned to keys 1 & 2."""
    confs_dir = _write_synthetic_proxy_hub(tmp_path / "confs")
    connections = [
        {"id": "k1", "name": "Key 1", "proxy_url": UNIFIED_PROXY_HUB_URI},
        {"id": "k2", "name": "Key 2", "proxy_url": UNIFIED_PROXY_HUB_URI},
    ]
    first_alloc = allocate_provider_lanes("openai", connections, confs_dir=confs_dir)
    lane_k1 = first_alloc[0]["lane_id"]
    lane_k2 = first_alloc[1]["lane_id"]
    assert lane_k1 != lane_k2

    # Now add Key 3 at the end
    connections.append({"id": "k3", "name": "Key 3", "proxy_url": UNIFIED_PROXY_HUB_URI})
    second_alloc = allocate_provider_lanes("openai", connections, confs_dir=confs_dir)
    assert second_alloc[0]["lane_id"] == lane_k1
    assert second_alloc[1]["lane_id"] == lane_k2
    assert second_alloc[2]["lane_id"] not in {lane_k1, lane_k2}


def test_effective_egress_proxy_resolves_unified_link_and_honors_bypass(tmp_path, monkeypatch):
    confs_dir = _write_synthetic_proxy_hub(tmp_path / "confs")
    monkeypatch.setenv("BSL_PROXY_HUB_CONFS_DIR", str(confs_dir))

    c1 = {"id": "ag-1", "email": "u1@example.com", "proxy_url": "proxyhub://auto"}
    c2 = {"id": "ag-2", "email": "u2@example.com", "proxy_url": "proxyhub://auto"}
    c3_bypass = {
        "id": "ag-3",
        "email": "u3@example.com",
        "proxy_url": "proxyhub://auto",
        "proxy_bypass": True,
    }
    fake_cfg = {
        "providers": {
            "antigravity": {
                "type": "oauth",
                "connections": [c1, c2, c3_bypass],
            }
        }
    }
    monkeypatch.setattr(main, "cs_get_config", lambda: fake_cfg)

    url1 = main._effective_egress_proxy(c1, "antigravity")
    url2 = main._effective_egress_proxy(c2, "antigravity")
    url3 = main._effective_egress_proxy(c3_bypass, "antigravity")

    assert url1 == "http://tok-slot1-c1:@crisp.bringyour.com:7160"
    assert url2 == "http://tok-slot2-c1:@crisp.bringyour.com:7160"
    assert url1 != url2
    assert url3 is None


def test_provider_level_proxy_url_allocates_distinct_lanes_to_all_keys(tmp_path, monkeypatch):
    """Setting `proxy_url: 'proxyhub://auto'` at the provider level auto-allocates every key/account."""
    confs_dir = _write_synthetic_proxy_hub(tmp_path / "confs")
    monkeypatch.setenv("BSL_PROXY_HUB_CONFS_DIR", str(confs_dir))

    acc1 = {"id": "oauth-1", "email": "acc1@example.com", "enabled": True}
    acc2 = {"id": "oauth-2", "email": "acc2@example.com", "enabled": True}
    cfg = {
        "providers": {
            "antigravity": {
                "type": "oauth",
                "proxy_url": "proxyhub://auto",
                "connections": [acc1, acc2],
            }
        }
    }
    monkeypatch.setattr(main, "cs_get_config", lambda: cfg)

    # 1. Runtime resolution when connections only inherit provider-level proxy_url
    u1 = main._effective_egress_proxy(acc1, "antigravity")
    u2 = main._effective_egress_proxy(acc2, "antigravity")
    assert u1 == "http://tok-slot1-c1:@crisp.bringyour.com:7160"
    assert u2 == "http://tok-slot2-c1:@crisp.bringyour.com:7160"
    assert u1 != u2

    # 2. Config save materialization stamps proxy_url & proxy_lane_meta onto all connections
    total = apply_proxy_hub_allocations_to_config(cfg, confs_dir=confs_dir)
    assert total == 2
    assert acc1["proxy_lane_meta"]["lane_id"] != acc2["proxy_lane_meta"]["lane_id"]


def test_local_wireproxy_mode_resolves_to_local_socks(tmp_path):
    confs_dir = _write_synthetic_proxy_hub(tmp_path / "confs")
    c1 = {"id": "loc-1", "proxy_url": "proxyhub://local"}
    c2 = {"id": "loc-2", "proxy_url": "proxyhub://local"}
    cfg = {"providers": {"deepseek": {"connections": [c1, c2]}}}

    u1 = resolve_unified_proxy_url("proxyhub://local", c1, "deepseek", config=cfg, confs_dir=confs_dir)
    u2 = resolve_unified_proxy_url("proxyhub://local", c2, "deepseek", config=cfg, confs_dir=confs_dir)
    assert u1 == "socks5h://127.0.0.1:10801"
    assert u2 == "socks5h://127.0.0.1:10802"


def test_allocate_provider_endpoint_assigns_distinct_lanes(tmp_path, monkeypatch):
    import app.config_state as cs

    confs_dir = _write_synthetic_proxy_hub(tmp_path / "confs")
    monkeypatch.setenv("BSL_PROXY_HUB_CONFS_DIR", str(confs_dir))

    saved = cs.get_config()
    persisted = []
    monkeypatch.setattr(main, "_persist_config_snapshot", lambda cfg: persisted.append(cfg))

    try:
        seed_cfg = {
            "providers": {
                "gemini-cli": {
                    "type": "oauth",
                    "connections": [
                        {"id": "g1", "email": "g1@example.com", "api_key": "tok1", "enabled": True},
                        {"id": "g2", "email": "g2@example.com", "api_key": "tok2", "enabled": True},
                        {"id": "g3", "email": "g3@example.com", "api_key": "tok3", "enabled": True},
                    ],
                }
            }
        }
        cs.replace_config(seed_cfg)

        async def _receive():
            return {"type": "http.request", "body": b"{}", "more_body": False}

        req = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/proxy-hub/allocate/gemini-cli",
                "query_string": b"",
                "headers": [(b"content-type", b"application/json")],
                "scheme": "http",
                "server": ("127.0.0.1", 6969),
                "client": ("127.0.0.1", 50000),
            },
            _receive,
        )
        resp = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            main.proxy_hub_allocate_provider("gemini-cli", req)
        )
        assert resp.status_code == 200
        payload = json.loads(resp.body.decode("utf-8"))
        assert payload["status"] == "success"
        assert payload["allocated"] == 3
        lanes = [row["proxy_lane_meta"]["lane_id"] for row in payload["connections"]]
        assert len(set(lanes)) == 3
        assert all(row["proxy_url"] == "proxyhub://auto" for row in payload["connections"])
    finally:
        cs.replace_config(saved)
