"""Regression tests for BSL Router forensic audit bugfixes and hot-path optimizations."""
from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import time
from types import SimpleNamespace

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.testclient import TestClient

import app.config_state as cs
import app.main as main
import app.middleware.normalizer_shadow as ns
import app.mitm as mitm_mod
import app.normalizer as normalizer_v1
import app.normalizer_v2 as normalizer_v2
import app.observability as obs
from app.security import pool_auth


def test_normalizer_v2_tool_result_before_tool_use_no_unbound_local():
    """_egress_responses with tool_result before tool_use must not raise UnboundLocalError."""
    canonical = normalizer_v2.CanonicalRequest(
        model="gpt-5",
        messages=[
            {
                "role": "user",
                "parts": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "call_before_use_1",
                        "content": "ok",
                    }
                ],
            },
            {
                "role": "assistant",
                "parts": [
                    {
                        "type": "tool_use",
                        "id": "call_after_result_2",
                        "name": "read_file",
                        "arguments": {"path": "a.py"},
                    }
                ],
            },
        ],
    )
    out = normalizer_v2._egress_responses(canonical)
    items = out["input"]
    assert items[0]["type"] == "function_call_output"
    assert items[0]["call_id"] == "call_before_use_1"
    assert items[1]["type"] == "function_call"
    assert items[1]["call_id"] == "call_after_result_2"


def test_normalizer_v1_messages_only_no_unbound_local():
    """normalize_to_openai without tools and >64-char tool_call_id must not raise UnboundLocalError."""
    long_id = "call_" + ("x" * 80)
    payload = {
        "model": "claude-sonnet-4-5",
        "messages": [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": long_id,
                        "type": "function",
                        "function": {"name": "run_cmd", "arguments": '{"cmd":"ls"}'},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": long_id,
                "content": "done",
            },
        ],
    }
    out = normalizer_v1.UniversalNormalizer.normalize_to_openai(payload)
    assert out.model == "claude-sonnet-4-5"
    assert len(out.messages[1].tool_call_id) <= 64
    assert out.messages[0].tool_calls[0].id == out.messages[1].tool_call_id


def test_normalizer_shadow_writer_starts_real_asyncio_task():
    """_ensure_shadow_writer must start a live asyncio.Task without name collision."""
    async def _run():
        ns._shadow_writer_task = None
        ns._shadow_queue = None
        ns._ensure_shadow_writer()
        task = ns._shadow_writer_task
        assert isinstance(task, asyncio.Task)
        assert not task.done()
        # Idempotent second call returns the same live task.
        ns._ensure_shadow_writer()
        assert ns._shadow_writer_task is task
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        ns._shadow_writer_task = None
        ns._shadow_queue = None

    asyncio.run(_run())


def test_pool_auth_asgi_middleware_streaming_and_verify():
    """PoolAuthASGIMiddleware must stream without buffering when off and verify Ed25519 when on."""
    priv = Ed25519PrivateKey.generate()
    pub_hex = priv.public_key().public_bytes_raw().hex()
    peer_id = hashlib.sha256(bytes.fromhex(pub_hex)).hexdigest()
    cfg_holder = {"cfg": {"pool_auth": {"enabled": False}}}

    mini = FastAPI()
    mini.add_middleware(pool_auth.PoolAuthASGIMiddleware, get_config=lambda: cfg_holder["cfg"])

    @mini.post("/v1/chat/completions")
    async def _chat(request: Request):
        body = await request.json()
        got_peer = getattr(request.state, "pool_peer_id", None)
        if body.get("stream"):
            async def _gen():
                yield b'data: {"ok":true}\n\n'
                yield b"data: [DONE]\n\n"
            return StreamingResponse(_gen(), media_type="text/event-stream")
        return JSONResponse({"peer_id": got_peer, "echo": body.get("model")})

    client = TestClient(mini)
    # Disabled: streaming passes straight through.
    r_stream = client.post("/v1/chat/completions", json={"model": "m", "stream": True})
    assert r_stream.status_code == 200
    assert "[DONE]" in r_stream.text

    # Enabled + required: unsigned request rejected with 401.
    cfg_holder["cfg"] = {
        "pool_auth": {
            "enabled": True,
            "mode": "required",
            "router_address": "http://testserver",
            "allowlist": [pub_hex],
        }
    }
    r_unauth = client.post("/v1/chat/completions", json={"model": "m"})
    assert r_unauth.status_code == 401

    # Signed request replays body cleanly to route and sets request.state.pool_peer_id.
    raw = json.dumps({"model": "signed-m"}).encode("utf-8")
    body_sha = hashlib.sha256(raw).hexdigest()
    ts = int(time.time())
    msg = pool_auth.signing_message("POST", "/v1/chat/completions", ts, "http://testserver", body_sha)
    sig_hex = priv.sign(msg.encode("utf-8")).hex()
    signed_headers = {
        "Content-Type": "application/json",
        "X-BSL-Pool-From": peer_id,
        "X-BSL-Pool-Pubkey": pub_hex,
        "X-BSL-Pool-Ts": str(ts),
        "X-BSL-Pool-Sig": sig_hex,
        "X-BSL-Pool-Body-Sha256": body_sha,
    }
    r_ok = client.post("/v1/chat/completions", content=raw, headers=signed_headers)
    assert r_ok.status_code == 200
    data = r_ok.json()
    assert data["echo"] == "signed-m"
    assert data["peer_id"] == peer_id


def test_observability_cached_conn_reuses_and_reopens(tmp_path):
    """Cached SQLite connection reuses handle for same DB and reopens on path swap."""
    db1 = str(tmp_path / "one.sqlite3")
    db2 = str(tmp_path / "two.sqlite3")
    old_db = obs.usage_db_path
    old_override = obs._USAGE_CONN_CACHE_OVERRIDE
    try:
        obs._USAGE_CONN_CACHE_OVERRIDE = True
        obs.close_usage_conn()
        obs.usage_db_path = db1
        obs.init_usage_store()

        with obs._usage_conn() as c1:
            id1 = id(c1)
        with obs._usage_conn() as c2:
            id2 = id(c2)
        assert id1 == id2  # same connection reused

        obs.usage_db_path = db2
        obs.init_usage_store()
        with obs._usage_conn() as c3:
            id3 = id(c3)
        assert id3 != id1  # new path -> cleanly reopened
    finally:
        obs.close_usage_conn()
        obs._USAGE_CONN_CACHE_OVERRIDE = old_override
        obs.usage_db_path = old_db


def test_config_state_uses_c_yaml_when_available():
    """config_state selects CSafeLoader/CSafeDumper when PyYAML C bindings exist."""
    import yaml

    if hasattr(yaml, "CSafeLoader"):
        assert cs._YAML_LOADER is yaml.CSafeLoader
    if hasattr(yaml, "CSafeDumper"):
        assert cs._YAML_DUMPER is yaml.CSafeDumper


def test_mitm_tls_start_server_restores_client_sni():
    """tls_start_server must bind server = tls_start.conn and copy client SNI."""
    addon = mitm_mod.BSLRouterMitm()
    server = SimpleNamespace(sni="142.250.190.46")
    client = SimpleNamespace(sni="cloudcode-pa.googleapis.com")
    tls_start = SimpleNamespace(conn=server, context=SimpleNamespace(client=client))
    addon.tls_start_server(tls_start)
    assert server.sni == "cloudcode-pa.googleapis.com"
