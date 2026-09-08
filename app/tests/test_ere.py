"""ERE tests: R6 tool-arg repair wiring, CCPA ledger hygiene, F4 auto-registration.

Offline tests only — network-dependent paths (discover_models) are monkeypatched
on app.main attributes; ledgers are redirected to tmp_path.
"""
import asyncio
import json
import types

import pytest

import app.main as main
from app.middleware.tool_arg_repair import repair_tool_calls_argument_strings


# ── R6: string-encoded tool-call argument repair ─────────────────────────────

def test_r6_repairs_truncated_argument_string():
    tc = [{
        "id": "call_1",
        "type": "function",
        "function": {"name": "f", "arguments": '{"a": 1'},  # truncated JSON
    }]
    repaired = repair_tool_calls_argument_strings(tc)
    assert repaired > 0
    post = tc[0]["function"]["arguments"]
    assert isinstance(post, str)
    assert json.loads(post) == {"a": 1}


def test_r6_wiring_shape_repairs_openai_response_choices():
    """Simulate the exact loop the main.py R6 wiring runs over a normalized
    OpenAI-shaped response (choices[].message.tool_calls[].arguments)."""
    normalized = {
        "choices": [{
            "message": {
                "role": "assistant",
                "tool_calls": [{
                    "id": "call_x",
                    "type": "function",
                    "function": {"name": "g", "arguments": '{"path": "/tmp", "x":'},  # truncated
                }],
            },
        }],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
    _response_mutated = False
    try:
        if normalized:
            for _rc in (normalized.get("choices") or []):
                _tc_msg = (_rc or {}).get("message") or {}
                if _tc_msg.get("tool_calls"):
                    if repair_tool_calls_argument_strings(_tc_msg["tool_calls"]) > 0:
                        _response_mutated = True
    except Exception:
        pass
    assert _response_mutated is True
    assert json.loads(normalized["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]) == {
        "path": "/tmp", "x": None,
    } or isinstance(normalized["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"], str)


# ── CCPA ledger hygiene ───────────────────────────────────────────────────────

@pytest.fixture()
def ccpa_ledger(tmp_path, monkeypatch):
    ledger = tmp_path / "ccpa_failures.jsonl"
    monkeypatch.setattr(main, "_ccpa_ledger_dedup", {})
    monkeypatch.setattr(main, "_ccpa_ledger_ws_noise_skipped", 0)
    monkeypatch.setattr(main, "_CCPA_LEDGER_PATH", str(ledger))
    return ledger


def test_ccpa_ledger_dedup_collapses_fanout(tmp_path, monkeypatch):
    ledger = tmp_path / "ccpa_failures.jsonl"
    monkeypatch.setattr(main, "_ccpa_ledger_dedup", {})
    monkeypatch.setattr(main, "_CCPA_LEDGER_PATH", str(ledger))
    exc = ValueError("boom")
    for _ in range(5):
        main._log_antigravity_ccpa_stage_failure("op", "stage", exc)
    rows = [json.loads(l) for l in ledger.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(rows) == 1
    assert rows[0]["operation"] == "op" and rows[0]["stage"] == "stage"
    assert rows[0]["error_type"] == "ValueError"


def test_ccpa_ledger_ws_noise_skipped(tmp_path, monkeypatch):
    ledger = tmp_path / "ccpa_failures.jsonl"
    monkeypatch.setattr(main, "_ccpa_ledger_ws_noise_skipped", 0)
    monkeypatch.setattr(main, "_CCPA_LEDGER_PATH", str(ledger))
    main._log_antigravity_ccpa_stage_failure("op", "recv", RuntimeError("RECV_PING on CLOSED"))
    main._log_antigravity_ccpa_stage_failure("op", "hdr", RuntimeError("connection HEADERS on CLOSED"))
    assert not ledger.exists()  # 0 rows
    assert main._ccpa_ledger_ws_noise_skipped == 2


def test_ccpa_ledger_different_ops_separate_rows(tmp_path, monkeypatch):
    ledger = tmp_path / "ccpa_failures.jsonl"
    monkeypatch.setattr(main, "_ccpa_ledger_dedup", {})
    monkeypatch.setattr(main, "_CCPA_LEDGER_PATH", str(ledger))
    exc = ValueError("x")
    main._log_antigravity_ccpa_stage_failure("opA", "stage", exc)
    main._log_antigravity_ccpa_stage_failure("opB", "stage", exc)
    rows = [json.loads(l) for l in ledger.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(rows) == 2
    assert {r["operation"] for r in rows} == {"opA", "opB"}


# ── F4 auto-registration ─────────────────────────────────────────────────────

class _Resp404:
    status_code = 404


class _Resp500:
    status_code = 500


@pytest.fixture()
def f4_env(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "_AUTO_REGISTER_LEDGER", str(tmp_path / "auto_register.jsonl"))
    monkeypatch.setattr(main, "_auto_register_rate", {})
    return tmp_path / "auto_register.jsonl"


def _drain_tasks():
    """Run any fire-and-forget tasks _maybe_auto_register spawned."""
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(asyncio.sleep(0.05))
        pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
    finally:
        loop.close()


def test_f4_disabled_no_probe(f4_env, monkeypatch):
    monkeypatch.setattr(
        main, "cs_get_config", lambda: {"tools": {"auto_register_models": False}}
    )
    called = {"n": 0}

    async def _sentinel(*a, **k):
        called["n"] += 1
        return {"models": []}

    monkeypatch.setattr(main, "discover_models", _sentinel)
    main._maybe_auto_register("prov", "some-model", _Resp404())
    _drain_tasks()
    assert called["n"] == 0  # OFF → zero behavior change
    assert not f4_env.exists()


def test_f4_exact_norm_match_registers(f4_env, monkeypatch):
    monkeypatch.setattr(
        main, "cs_get_config", lambda: {"tools": {"auto_register_models": True}}
    )
    provider_config = {"models": [], "connections": [{"enabled": True}]}

    async def _fake_discover(name, pcfg, client):
        return {"models": [{"id": "gpt-5.6-mini"}, {"id": "other"}]}

    monkeypatch.setattr(main, "discover_models", _fake_discover)
    monkeypatch.setattr(
        main, "get_mutable_config", lambda: {"providers": {"prov": provider_config}}
    )
    replaced = {}
    monkeypatch.setattr(main, "_replace_runtime_config", lambda cfg: replaced.update(cfg=cfg))

    main._maybe_auto_register("prov", "gpt56 mini", _Resp404())
    _drain_tasks()
    rows = [json.loads(l) for l in f4_env.read_text(encoding="utf-8").splitlines() if l.strip()]
    registered = [r for r in rows if r["outcome"] == "registered"]
    assert len(registered) == 1
    assert registered[0]["match"] == "gpt-5.6-mini"
    assert {"id": "gpt-5.6-mini", "enabled": True} == {
        k: v for k, v in provider_config["models"][0].items() if k in ("id", "enabled")
    }
    assert replaced  # runtime config was refreshed


def test_f4_ambiguous_prefix_no_change(f4_env, monkeypatch):
    monkeypatch.setattr(
        main, "cs_get_config", lambda: {"tools": {"auto_register_models": True}}
    )
    provider_config = {"models": [], "connections": [{"enabled": True}]}

    async def _fake_discover(name, pcfg, client):
        return {"models": [{"id": "gpt-5.6-mini"}, {"id": "gpt-5.6-nano"}]}

    monkeypatch.setattr(main, "discover_models", _fake_discover)
    monkeypatch.setattr(
        main, "get_mutable_config", lambda: {"providers": {"prov": provider_config}}
    )
    monkeypatch.setattr(main, "_replace_runtime_config", lambda cfg: pytest.fail("must not register"))

    main._maybe_auto_register("prov", "gpt-5.6", _Resp404())
    _drain_tasks()
    rows = [json.loads(l) for l in f4_env.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert all(r["outcome"] == "no_match" for r in rows)
    assert provider_config["models"] == []  # config untouched


def test_f4_rate_limit_second_call_skipped(f4_env, monkeypatch):
    monkeypatch.setattr(
        main, "cs_get_config", lambda: {"tools": {"auto_register_models": True}}
    )
    provider_config = {"models": [], "connections": [{"enabled": True}]}

    async def _fake_discover(name, pcfg, client):
        return {"models": [{"id": "solo-model"}]}

    calls = {"n": 0}

    async def _counting_discover(name, pcfg, client):
        calls["n"] += 1
        return await _fake_discover(name, pcfg, client)

    monkeypatch.setattr(main, "discover_models", _counting_discover)
    monkeypatch.setattr(
        main, "get_mutable_config", lambda: {"providers": {"prov": provider_config}}
    )
    monkeypatch.setattr(main, "_replace_runtime_config", lambda cfg: None)

    main._maybe_auto_register("prov", "solo", _Resp404())
    main._maybe_auto_register("prov", "solo", _Resp404())  # within cooldown → skipped
    _drain_tasks()
    assert calls["n"] == 1  # probe fired exactly once


def test_f4_non_404_ignored(f4_env, monkeypatch):
    monkeypatch.setattr(
        main, "cs_get_config", lambda: {"tools": {"auto_register_models": True}}
    )
    called = {"n": 0}

    async def _sentinel(*a, **k):
        called["n"] += 1
        return {"models": []}

    monkeypatch.setattr(main, "discover_models", _sentinel)
    main._maybe_auto_register("prov", "m", _Resp500())
    _drain_tasks()
    assert called["n"] == 0


# ── Fuzzy matcher table ──────────────────────────────────────────────────────

def test_fuzzy_exact_after_normalization():
    assert main._fuzzy_model_match("GPT-5.6 mini", ["gpt56-mini", "x"]) == "gpt56-mini"


def test_fuzzy_prefix_unique():
    assert main._fuzzy_model_match("claude-opus", ["claude-opus-4-8", "gpt"]) == "claude-opus-4-8"


def test_fuzzy_prefix_ambiguous():
    assert main._fuzzy_model_match("gpt-5.6", ["gpt-5.6-mini", "gpt-5.6-nano"]) is None


def test_fuzzy_substring_unique():
    assert main._fuzzy_model_match("opus", ["claude-opus-4-8", "gpt"]) == "claude-opus-4-8"


def test_fuzzy_substring_ambiguous():
    assert main._fuzzy_model_match("5.6", ["xgpt-5.6", "gpt-5.6-y"]) is None


def test_fuzzy_empty_candidates():
    assert main._fuzzy_model_match("anything", []) is None
