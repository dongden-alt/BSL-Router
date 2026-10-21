"""PR1 dead-leaf self-heal coverage.

Maps to self_heal_hardening_plan_v2.md §4:
  1  test_timeout_triggers_immediate_ban            — Part 2 zero-strike ban
  2  test_ephemeral_ban_writes_sidecar_not_config   — Part 2b sidecar isolation
  2b test_escalated_ban_writes_config_not_sidecar   — Part 2b policy write
  3  test_sidecar_reload_on_startup                 — Part 2b startup restore
  4  test_combo_skips_banned_leaf_and_writes_back   — Part 4 RC5 + C3
  5  test_retry_index_uses_snapshot_not_rebuilt     — Part 4 RC5 + C2
  6  test_all_banned_chain_returns_exhausted_502    — Part 4 C5
  8  test_nongemini_header_wait_fastfail            — Part 1b RC6
  9  test_gemini_connect_timeout_fastfail           — Part 1a RC4
"""

from __future__ import annotations

import asyncio
import builtins
import io
import json
import os
import tempfile
import time

import httpx
import pytest

import app.error_prevention as ep
from app.error_prevention import ErrorPreventionManager
import app.config_state as cs
import app.main as main


# ─────────────────────────────────────────────────────────────────────────────
# Part 2 / 2b — AEP unit tests (pure, no network)
# ─────────────────────────────────────────────────────────────────────────────

def _aep_config(**overrides):
    cfg = {
        "error_prevention": {
            "enabled": True,
            "consecutive_threshold": 3,
            "rate_limit_cooldown_seconds": 90,
            "auth_cooldown_seconds": 90,
            "dead_leaf_cooldown_seconds": 90,
            "not_found_cooldown_seconds": 120,
            "default_cooldown_seconds": 30,
        },
        "error_prevention_state": {},
        "providers": {},
    }
    cfg["error_prevention"].update(overrides)
    return cfg


def _guard_config_yaml(monkeypatch):
    """Redirect config.yaml persistence to memory and record that it happened.

    Protects the real repo config.yaml from test writes and lets us assert the
    ephemeral/escalated persistence split. Intercepts BOTH the legacy direct
    open("config.yaml","w") and the new atomic path (tempfile.mkstemp in the
    CWD + os.replace -> config.yaml) used by _persist_config_yaml /
    _persist_config_snapshot.
    """
    real_open = builtins.open
    real_replace = os.replace
    real_mkstemp = tempfile.mkstemp
    flag = {"config_written": False}

    def guarded_open(path, *args, **kwargs):
        p = str(path).replace("\\", "/")
        mode = kwargs.get("mode", args[0] if args else "r")
        if p.endswith("config.yaml"):
            if "w" in mode:
                flag["config_written"] = True
                return io.StringIO()
            elif "r" in mode:
                # Protection 2b reads config.yaml to count existing providers.
                # Return a minimal fake config (1 provider) so the test's
                # 1-provider snapshot doesn't trigger the regression block.
                fake_yaml = "providers:\n  test-prov:\n    models: []\n"
                return io.StringIO(fake_yaml)
        return real_open(path, *args, **kwargs)

    def guarded_mkstemp(*args, **kwargs):
        # Redirect the atomic temp file (config.yaml.*.tmp) to the OS temp dir so
        # the test CWD stays clean; content is irrelevant to the assertion.
        kwargs["dir"] = tempfile.gettempdir()
        return real_mkstemp(*args, **kwargs)

    def guarded_replace(src, dst, *args, **kwargs):
        d = str(dst).replace("\\", "/")
        if d.endswith("config.yaml"):
            flag["config_written"] = True
            try:
                os.unlink(src)
            except OSError:
                pass
            return
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(tempfile, "mkstemp", guarded_mkstemp)
    monkeypatch.setattr(os, "replace", guarded_replace)
    return flag


def test_timeout_triggers_immediate_ban():
    """Test 1: a single timeout benches the leaf immediately (no 3-strike wait)."""
    cfg = _aep_config()
    mgr = ErrorPreventionManager(cfg)

    action = mgr.record_error("vsllm-a", "MiniMax-M3", 504, "Read timed out")

    assert action is not None
    assert action["action"] == "softban"
    assert action["ephemeral"] is True
    assert action["error_type"] == "timeout"

    banned, state, remaining = mgr.is_banned("vsllm-a", "MiniMax-M3")
    assert banned is True
    assert state == "softban"
    assert 0 < remaining <= 90


def test_unknown_error_gets_default_cooldown():
    """Test 1b (C2): an unmatched/unknown error still gets the 30s default lock."""
    cfg = _aep_config()
    mgr = ErrorPreventionManager(cfg)

    # status 418 with an unrecognized body classifies as 'unknown'.
    action = mgr.record_error("someprov", "some-model", 418, "i am a teapot")

    assert action is not None
    assert action["action"] == "softban"
    assert action["error_type"] == "unknown"
    assert action["duration_seconds"] == 30


# ─────────────────────────────────────────────────────────────────────────────
# Anti-downgrade guard (2026-10-21): an immediate sub-threshold cooldown must
# never SHORTEN an active escalated ban nor touch a permanent 'disabled' state.
# ─────────────────────────────────────────────────────────────────────────────

def test_immediate_cooldown_does_not_shorten_active_softban():
    """Strike 3 (sub-threshold) must NOT overwrite the strike-2 ladder softban."""
    cfg = _aep_config(consecutive_threshold=2, softban_duration_minutes=7,
                      dead_leaf_cooldown_seconds=90)
    mgr = ErrorPreventionManager(cfg)

    mgr.record_error("prov", "model", 500, "internal server error")   # strike 1 -> 90s
    a2 = mgr.record_error("prov", "model", 500, "internal server error")  # strike 2 -> ladder 420s
    assert a2 is not None and a2.get("duration_minutes") == 7

    a3 = mgr.record_error("prov", "model", 500, "internal server error")  # strike 3 -> guarded
    assert a3 is None, "immediate cooldown must not fire over an active softban"
    banned, state, remaining = mgr.is_banned("prov", "model")
    assert banned is True and state == "softban"
    assert remaining > 400, f"420s softban truncated to {remaining}s"


def test_immediate_cooldown_does_not_shorten_active_longban():
    """Strike 5 (sub-threshold) must NOT overwrite the strike-4 ladder longban."""
    cfg = _aep_config(consecutive_threshold=2, softban_duration_minutes=7,
                      longban_duration_minutes=77, dead_leaf_cooldown_seconds=90)
    mgr = ErrorPreventionManager(cfg)

    for _ in range(3):
        mgr.record_error("prov", "model", 500, "internal server error")
    a4 = mgr.record_error("prov", "model", 500, "internal server error")  # strike 4 -> longban 4620s
    assert a4 is not None and a4.get("action") == "longban" and a4.get("duration_minutes") == 77

    a5 = mgr.record_error("prov", "model", 500, "internal server error")  # strike 5 -> guarded
    assert a5 is None, "immediate cooldown must not fire over an active longban"
    banned, state, remaining = mgr.is_banned("prov", "model")
    assert banned is True and state == "longban"
    assert remaining > 4000, f"4620s longban truncated to {remaining}s"


def test_immediate_cooldown_extends_when_longer():
    """A LONGER immediate cooldown still applies: 90s softban + 120s 404 -> ban moves OUT.

    The state key embeds the error_type, so the extension must be observed on ONE
    key: the not_found key first takes a 90s immediate softban, then the same key
    takes a 120s not_found cooldown. The guard must let the longer one through.
    """
    cfg = _aep_config(consecutive_threshold=5, softban_duration_minutes=7,
                      dead_leaf_cooldown_seconds=90, not_found_cooldown_seconds=90)
    mgr = ErrorPreventionManager(cfg)

    a1 = mgr.record_error("prov", "model", 404, "model not found")  # 90s softban on not_found key
    assert a1 is not None and a1.get("duration_seconds") == 90

    mgr.settings["not_found_cooldown_seconds"] = 120
    a2 = mgr.record_error("prov", "model", 404, "model not found")  # 120s > remaining 90s
    assert a2 is not None and a2.get("duration_seconds") == 120
    banned, state, remaining = mgr.is_banned("prov", "model")
    assert banned is True and state == "softban"
    assert remaining > 110, f"ban should extend to ~120s, got {remaining}s"


def test_disabled_leaf_not_downgraded():
    """A permanent 'disabled' ban_state is never weakened by an immediate cooldown."""
    cfg = _aep_config(consecutive_threshold=5, dead_leaf_cooldown_seconds=90)
    mgr = ErrorPreventionManager(cfg)

    key = mgr.get_state_key("prov", "model", "server_error")
    mgr.state[key] = {
        "streak": 0, "last_error_time": time.time(),
        "ban_state": "disabled", "ban_until": None,
        "ban_escalation_count": 3, "error_type": "server_error",
        "provider": "prov", "model": "model",
    }

    action = mgr.record_error("prov", "model", 500, "internal server error")
    assert action is None, "disabled state must not be downgraded to a cooldown"
    assert mgr.state[key]["ban_state"] == "disabled"


@pytest.mark.parametrize("status_code", [400, 422, 499])
def test_excluded_status_codes_never_ban(status_code):
    """Test 1c: payload errors (400/422) and client aborts (499) never bench a leaf."""
    cfg = _aep_config()
    mgr = ErrorPreventionManager(cfg)

    actions = [
        mgr.record_error("prov", "model", status_code, "bad request or client abort")
        for _ in range(mgr.threshold + 2)
    ]

    assert actions == [None] * (mgr.threshold + 2)
    assert mgr.state == {}
    banned, _, _ = mgr.is_banned("prov", "model")
    assert banned is False


def test_ephemeral_ban_writes_sidecar_not_config(tmp_path, monkeypatch):
    """Test 2: an ephemeral cooldown writes the sidecar and does NOT touch config.yaml."""
    sidecar = tmp_path / "aep_runtime.json"
    monkeypatch.setattr(ep, "_AEP_SIDECAR_PATH", str(sidecar))
    flag = _guard_config_yaml(monkeypatch)

    cfg = _aep_config()
    ep.record_outcome(cfg, "vsllm-a", "MiniMax-M3", 504, "Read timed out")

    assert sidecar.exists(), "ephemeral ban should create the sidecar"
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert any(k.endswith("/timeout") for k in data), data
    assert flag["config_written"] is False, "ephemeral ban must NOT rewrite config.yaml"


def test_escalated_ban_writes_config_not_sidecar(tmp_path, monkeypatch):
    """Test 2b: a non-ephemeral policy ban (disabled) writes config.yaml, not the sidecar."""
    sidecar = tmp_path / "aep_runtime.json"
    monkeypatch.setattr(ep, "_AEP_SIDECAR_PATH", str(sidecar))
    flag = _guard_config_yaml(monkeypatch)

    # Force a non-ephemeral action shape (the zero-strike path only ever emits
    # ephemeral ones, so we synthesize an escalated 'disabled' to exercise the
    # policy-write branch of record_outcome).
    def fake_record_error(self, provider, model, status_code, error_msg):
        return {
            "action": "disabled",
            "model": model,
            "provider": provider,
            "error_type": "server_error",
            "duration_minutes": None,
            "notify": True,
        }

    monkeypatch.setattr(ep.ErrorPreventionManager, "record_error", fake_record_error)

    # Non-empty providers snapshot: the never-wipe guard only suppresses writes
    # of EMPTY provider sets. Supply a real provider so the escalated write is
    # permitted, matching production (a ban always targets an existing provider).
    cfg = _aep_config()
    cfg["providers"] = {"prov": {"models": [{"id": "model", "enabled": True}]}}
    ep.record_outcome(cfg, "prov", "model", 500, "internal server error")

    assert flag["config_written"] is True, "escalated ban must persist to config.yaml"
    assert not sidecar.exists(), "escalated ban must NOT use the ephemeral sidecar"


def test_sidecar_reload_on_startup(tmp_path, monkeypatch):
    """Test 3: startup loader restores live cooldowns, drops expired, self-prunes."""
    sidecar = tmp_path / "aep_runtime.json"
    monkeypatch.setattr(ep, "_AEP_SIDECAR_PATH", str(sidecar))

    now = time.time()
    live_key = "prov/live-model/timeout"
    expired_key = "prov/expired-model/timeout"
    sidecar.write_text(
        json.dumps(
            {
                live_key: {
                    "streak": 0, "ban_state": "softban", "ban_until": now + 100,
                    "ban_escalation_count": 1, "error_type": "timeout",
                    "provider": "prov", "model": "live-model",
                },
                expired_key: {
                    "streak": 0, "ban_state": "softban", "ban_until": now - 100,
                    "ban_escalation_count": 1, "error_type": "timeout",
                    "provider": "prov", "model": "expired-model",
                },
            }
        ),
        encoding="utf-8",
    )

    cfg = _aep_config()
    restored = ep.load_runtime_bans(cfg)

    assert restored == 1
    state = cfg["error_prevention_state"]
    assert live_key in state
    assert expired_key not in state

    # Sidecar self-pruned to survivors only.
    remaining = json.loads(sidecar.read_text(encoding="utf-8"))
    assert live_key in remaining
    assert expired_key not in remaining


# ─────────────────────────────────────────────────────────────────────────────
# Toggle gate — disabled path must not ban, persist, restore, notify, or expose
# ─────────────────────────────────────────────────────────────────────────────

def _seeded_ban_state(provider="prov", model="model", error_type="timeout", remaining=120.0):
    """Build a still-live softban entry for disabled-path projection tests."""
    key = f"{provider}/{model}/{error_type}"
    entry = {
        "streak": 0,
        "ban_state": "softban",
        "ban_until": time.time() + remaining,
        "ban_escalation_count": 1,
        "error_type": error_type,
        "provider": provider,
        "model": model,
        "last_error_time": time.time(),
    }
    return key, entry


@pytest.mark.parametrize(
    "status_code,error_msg",
    [
        (429, "rate limit exceeded"),
        (504, "Read timed out"),
        (500, "internal server error"),
    ],
    ids=["rate_limit", "timeout", "server_error"],
)
def test_disabled_record_outcome_no_side_effects(tmp_path, monkeypatch, status_code, error_msg):
    """When error_prevention.enabled is false, outcomes must not mutate or notify."""
    sidecar = tmp_path / "aep_runtime.json"
    monkeypatch.setattr(ep, "_AEP_SIDECAR_PATH", str(sidecar))
    flag = _guard_config_yaml(monkeypatch)

    cfg = _aep_config(enabled=False)
    notif_before = list(ep.notifications)

    ep.record_outcome(cfg, "vsllm-a", "MiniMax-M3", status_code, error_msg)

    assert cfg["error_prevention_state"] == {}
    assert not sidecar.exists(), "disabled path must not create the sidecar"
    assert flag["config_written"] is False, "disabled path must not persist config.yaml"
    assert ep.notifications == notif_before, "disabled path must not emit notifications"
    banned, _, _ = ep.check_ban(cfg, "vsllm-a", "MiniMax-M3")
    assert banned is False


def test_disabled_load_runtime_bans_leaves_sidecar_untouched(tmp_path, monkeypatch):
    """Disabled startup restore must not merge state or rewrite the sidecar."""
    sidecar = tmp_path / "aep_runtime.json"
    monkeypatch.setattr(ep, "_AEP_SIDECAR_PATH", str(sidecar))

    now = time.time()
    live_key = "prov/live-model/timeout"
    payload = {
        live_key: {
            "streak": 0,
            "ban_state": "softban",
            "ban_until": now + 100,
            "ban_escalation_count": 1,
            "error_type": "timeout",
            "provider": "prov",
            "model": "live-model",
        }
    }
    raw = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    sidecar.write_text(raw, encoding="utf-8")

    cfg = _aep_config(enabled=False)
    restored = ep.load_runtime_bans(cfg)

    assert restored == 0
    assert cfg["error_prevention_state"] == {}
    assert sidecar.read_text(encoding="utf-8") == raw


def test_disabled_get_active_bans_and_check_ban_empty():
    """Disabled projection hides seeded bans; check_ban stays false."""
    cfg = _aep_config(enabled=False)
    key, entry = _seeded_ban_state()
    cfg["error_prevention_state"][key] = entry

    mgr = ErrorPreventionManager(cfg)
    assert mgr.get_active_bans() == []
    banned, ban_type, remaining = mgr.is_banned("prov", "model")
    assert banned is False
    assert ban_type is None
    assert remaining is None
    banned2, _, _ = ep.check_ban(cfg, "prov", "model")
    assert banned2 is False
    # Seeded in-memory state must remain intact for a later re-enable.
    assert key in cfg["error_prevention_state"]
    assert cfg["error_prevention_state"][key]["ban_state"] == "softban"


def test_disabled_handle_action_no_notify_or_disable(monkeypatch):
    """Defensive _handle_action guard: no notifications or live config mutation."""
    cfg = _aep_config(enabled=False)
    cfg["providers"] = {"prov": {"models": [{"id": "model", "enabled": True}]}}
    notif_before = list(ep.notifications)
    swaps = {"count": 0}

    def _fake_swap(_config):
        swaps["count"] += 1

    monkeypatch.setattr(main, "_replace_runtime_config", _fake_swap, raising=False)

    ep._handle_action(
        {
            "action": "disabled",
            "model": "model",
            "provider": "prov",
            "error_type": "server_error",
            "duration_minutes": None,
            "notify": True,
        },
        cfg,
    )

    assert ep.notifications == notif_before
    assert swaps["count"] == 0
    assert cfg["providers"]["prov"]["models"][0]["enabled"] is True


# ─────────────────────────────────────────────────────────────────────────────
# Part 1 / 4 — integration harness (scripted upstream client)
# Mirrors app/tests/test_mapped_gemini_combo_fallback.py.
# ─────────────────────────────────────────────────────────────────────────────

class _ChunkStream(httpx.AsyncByteStream):
    def __init__(self, *chunks: bytes):
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


class _ScriptedClient:
    def __init__(self, behaviors):
        self.behaviors = behaviors
        self.models = []

    def build_request(self, method, url, **kwargs):
        return httpx.Request(method, url, **kwargs)

    async def send(self, request, stream=False):
        payload = json.loads(request.content)
        model = payload["model"]
        self.models.append(model)
        behavior = self.behaviors[model]
        if callable(behavior):
            return await behavior(request)
        return httpx.Response(200, request=request, stream=behavior)


def _openai_chunk(model: str, text: str = "", finish_reason=None) -> bytes:
    payload = {
        "id": f"chatcmpl-{model}",
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [{"index": 0, "delta": {"content": text} if text else {}, "finish_reason": finish_reason}],
    }
    return f"data: {json.dumps(payload)}\n\ndata: [DONE]\n\n".encode()


def _success_stream(model: str, text: str = "ok") -> _ChunkStream:
    return _ChunkStream(_openai_chunk(model, text, "stop"))


def _combo_config(chain, *, banned=None):
    """Build a minimal config resolving alias TESTCOMBO to `chain`.

    chain: list of (model, provider) tuples.
    banned: iterable of (provider, model) to pre-bench in AEP state.
    """
    providers = {}
    for model, provider in chain:
        providers.setdefault(provider, {
            "type": "custom",
            "format": "openai",
            "connections": [{"enabled": True, "api_key": "t", "base_url": f"https://{provider}.invalid"}],
            "models": [],
        })
        providers[provider]["models"].append({"id": model, "enabled": True, "thinking": "off"})

    now = time.time()
    state = {}
    for provider, model in (banned or []):
        state[f"{provider}/{model}/timeout"] = {
            "streak": 0, "ban_state": "softban", "ban_until": now + 300,
            "ban_escalation_count": 1, "error_type": "timeout",
            "provider": provider, "model": model,
        }

    return {
        "tools": {"output_thinking_squeeze": False},
        "providers": providers,
        "combos": [{
            "alias": "TESTCOMBO",
            "strategy": "fallback",
            "chain": [{"provider": p, "model": m} for m, p in chain],
        }],
        "aliases": {},
        "error_prevention": {"enabled": True},
        "error_prevention_state": state,
    }


def _install(monkeypatch, client, config, *, breaker=None):
    real_open = builtins.open

    def open_without_forensics(path, *args, **kwargs):
        if str(path).replace("\\", "/").endswith(".brain/logs/outbound_upstream.jsonl"):
            return io.StringIO()
        return real_open(path, *args, **kwargs)

    starts, ends = [], []
    monkeypatch.setattr(builtins, "open", open_without_forensics)
    cs.replace_config(config)
    monkeypatch.setattr(main, "_get_client_for_proxy", lambda *_a: client)
    monkeypatch.setattr(main, "get_breaker", lambda: breaker)
    monkeypatch.setattr(main.obs, "log_request_start", lambda **kw: starts.append(kw) or f"req-{len(starts)}")
    monkeypatch.setattr(main.obs, "log_request", lambda **kw: ends.append(kw))
    monkeypatch.setattr(main, "GEMINI_EGRESS_KEEPALIVE_INTERVAL", 0.005)
    monkeypatch.setattr(main, "GEMINI_EGRESS_CONNECT_KEEPALIVE_INTERVAL", 0.005)
    monkeypatch.setattr(main, "GEMINI_EGRESS_CONNECT_TIMEOUT", 0.05)
    monkeypatch.setattr(main, "GEMINI_EGRESS_BODY_STALL_TIMEOUT", 0.05)
    # Preserve production ordering: HEADER_WAIT_TIMEOUT > GEMINI_EGRESS_CONNECT_TIMEOUT
    # so the Gemini path's own connect deadline wins first (the non-Gemini path
    # still gets a bound). 0.12 vs 0.05 keeps the suite fast while realistic.
    monkeypatch.setattr(main, "HEADER_WAIT_TIMEOUT", 0.12)
    return starts, ends


async def _dispatch(rs=None, gemini=False):
    body = {
        "model": "TESTCOMBO",
        "_bsl_original_model": "test-agent",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
    }
    response = await main._process_chat_completion(
        body, client_wants_gemini=gemini, _retry_state=rs,
    )
    if hasattr(response, "body_iterator"):
        chunks = [c async for c in response.body_iterator]
        return response, b"".join(chunks)
    return response, getattr(response, "body", b"")


def _chain(*entries):
    """entries: (model, provider) → (model, provider, thinking=None) snapshot tuples."""
    return [(m, p, None) for m, p in entries]


def test_combo_skips_banned_leaf_and_writes_back(monkeypatch):
    """Test 4 (RC5 + C3): retry idx points at a banned leaf → skip to next, write back idx."""
    chain = [("model-a", "prova"), ("model-b", "provb"), ("model-c", "provc")]
    config = _combo_config(chain, banned=[("provb", "model-b")])
    client = _ScriptedClient({
        "model-a": _success_stream("model-a"),
        "model-b": _success_stream("model-b"),
        "model-c": _success_stream("model-c"),
    })
    _install(monkeypatch, client, config)

    rs = {"chain": _chain(*chain), "idx": 1, "cache_bp": None, "original_model": "test-agent"}
    response, output = asyncio.run(_dispatch(rs=rs))

    # idx 1 (banned model-b) skipped → model-c dialed.
    assert client.models[0] == "model-c"
    assert rs["idx"] == 2, "advanced index must be written back for downstream _next_idx"
    assert b"ok" in output


def test_retry_index_uses_snapshot_not_rebuilt(monkeypatch):
    """Test 5 (RC5 + C2): snapshot idx is authoritative — no reset to entry 0."""
    chain = [("model-a", "prova"), ("model-b", "provb")]
    config = _combo_config(chain)  # nothing banned
    client = _ScriptedClient({
        "model-a": _success_stream("model-a"),
        "model-b": _success_stream("model-b"),
    })
    _install(monkeypatch, client, config)

    rs = {"chain": _chain(*chain), "idx": 1, "cache_bp": None, "original_model": "test-agent"}
    _response, output = asyncio.run(_dispatch(rs=rs))

    # idx 1 honored → model-b dialed first, NOT model-a (which a rebuilt-chain
    # scan-from-start would have re-selected).
    assert client.models[0] == "model-b"
    assert rs["idx"] == 1
    assert b"ok" in output


def test_all_banned_chain_returns_exhausted_502(monkeypatch):
    """Test 6 (C5): every remaining leaf banned → exhausted 502, no dispatch.

    Never-stop retry defaults ON, so this test must pin
    settings.combo_infinite_retry=false to observe the terminal 502 contract.
    """
    chain = [("model-a", "prova"), ("model-b", "provb")]
    config = _combo_config(chain, banned=[("prova", "model-a"), ("provb", "model-b")])
    config["settings"] = {"combo_infinite_retry": False}
    client = _ScriptedClient({
        "model-a": _success_stream("model-a"),
        "model-b": _success_stream("model-b"),
    })
    _install(monkeypatch, client, config)

    rs = {"chain": _chain(*chain), "idx": 0, "cache_bp": None, "original_model": "test-agent"}
    response, _output = asyncio.run(_dispatch(rs=rs))

    assert getattr(response, "status_code", None) == 502
    assert client.models == [], "no leaf should be dialed when all are banned"


def test_nongemini_header_wait_fastfail(monkeypatch):
    """Test 8 (RC6): a non-Gemini stream whose headers never arrive fails fast and advances."""
    chain = [("model-a", "prova"), ("model-b", "provb")]
    config = _combo_config(chain)

    async def hung_headers(_request):
        await asyncio.sleep(10)  # never returns headers
        return httpx.Response(200, request=_request, stream=_success_stream("model-a"))

    client = _ScriptedClient({
        "model-a": hung_headers,
        "model-b": _success_stream("model-b", "fallback-ok"),
    })
    _install(monkeypatch, client, config)  # HEADER_WAIT_TIMEOUT patched to 0.05

    _response, output = asyncio.run(_dispatch(gemini=False))

    assert client.models == ["model-a", "model-b"]
    assert b"fallback-ok" in output


def test_gemini_connect_timeout_fastfail(monkeypatch):
    """Test 9 (RC4): Gemini connect deadline trips on a hung leaf and advances the combo."""
    chain = [("model-a", "prova"), ("model-b", "provb")]
    config = _combo_config(chain)
    cancelled = asyncio.Event()

    async def hung_connection(_request):
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    client = _ScriptedClient({
        "model-a": hung_connection,
        "model-b": _success_stream("model-b", "fallback-ok"),
    })
    _install(monkeypatch, client, config)  # GEMINI_EGRESS_CONNECT_TIMEOUT patched to 0.05

    _response, output = asyncio.run(_dispatch(gemini=True))

    assert cancelled.is_set()
    assert client.models == ["model-a", "model-b"]
    assert b"fallback-ok" in output
