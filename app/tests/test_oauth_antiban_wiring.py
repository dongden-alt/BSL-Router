"""TDD wiring tests for app/oauth_antiban.py + resolver/main integration.

Covers:
  1. Resolver affinity hook collapses the pool to the session's bound account
     (round_robin provider pinned across repeated resolves).
  2. No session key -> pool untouched, nothing bound (scouts/background paths).
  3. Non-OAuth providers never get affined.
  4. Hook exceptions are fail-open (selection still succeeds).
  5. Sticky rebind when the bound account is breaker-OPEN (tuple-shaped
     is_open, matching the real CircuitBreaker API).
  6. _is_healthy tolerates tuple-returning and bool-returning breakers.
  7. Account-keyed client cache: per-account isolation, stable identity, and
     identity never derived from the raw token.
  8. Integration: importing app.main registers the resolver affinity hook.
"""

import pytest

from app import oauth_antiban as oab
from app.utils import model_resolver as mr


def _provider(n_conns=3):
    return {
        "round_robin": True,
        "connections": [
            {"enabled": True, "oauth_email": f"acct{i}@example.com", "api_key": f"tok-{i}"}
            for i in range(n_conns)
        ],
        "models": [{"id": "m1", "connection_indexes": list(range(n_conns))}],
    }


@pytest.fixture(autouse=True)
def _clean_state():
    prev_hook = mr._AFFINITY_HOOK
    oab.reset_store()
    oab.reset_clients()
    yield
    mr.set_affinity_hook(prev_hook)
    oab.reset_store()
    oab.reset_clients()


class _TupleBreaker:
    """Mimics app.circuit_breaker.CircuitBreaker.is_open -> (bool, remaining)."""

    enabled = True

    def __init__(self, open_indexes=()):
        self._open = set(open_indexes)

    def is_open(self, provider, model, idx):
        return (idx in self._open, 30.0 if idx in self._open else None)


def test_hook_pins_session_to_one_account_under_round_robin():
    prov = _provider()
    cfg = {"providers": {"antigravity": prov}}
    mr.set_affinity_hook(oab.resolver_affinity_hook)
    tok = oab.set_request_session_key("f:metadata.session_id:sess-A")
    try:
        picks = {mr.resolve_active_connection(cfg, "antigravity", "m1")[1] for _ in range(5)}
    finally:
        oab.reset_request_session_key(tok)
    # Round-robin would otherwise rotate across 3 accounts; affinity pins one.
    assert len(picks) == 1
    idx = picks.pop()
    assert idx is not None
    assert oab.get_store().lookup("f:metadata.session_id:sess-A", "antigravity") == idx


def test_no_session_key_leaves_pool_and_store_untouched():
    prov = _provider()
    cfg = {"providers": {"antigravity": prov}}
    mr.set_affinity_hook(oab.resolver_affinity_hook)
    # No contextvar published -> hook must no-op and bind nothing.
    conn, idx = mr.resolve_active_connection(cfg, "antigravity", "m1")
    assert conn is not None and idx is not None
    assert len(oab.get_store()) == 0


def test_non_oauth_provider_not_affined():
    prov = {
        "round_robin": True,
        "connections": [{"enabled": True, "api_key": f"k{i}"} for i in range(3)],
        "models": [{"id": "m1"}],
    }
    cfg = {"providers": {"vsllm": prov}}
    mr.set_affinity_hook(oab.resolver_affinity_hook)
    tok = oab.set_request_session_key("sess-X")
    try:
        conn, idx = mr.resolve_active_connection(cfg, "vsllm", "m1")
    finally:
        oab.reset_request_session_key(tok)
    assert conn is not None
    assert len(oab.get_store()) == 0


def test_hook_exception_is_fail_open():
    prov = _provider()
    cfg = {"providers": {"antigravity": prov}}

    def boom(*a, **k):
        raise RuntimeError("boom")

    mr.set_affinity_hook(boom)
    conn, idx = mr.resolve_active_connection(cfg, "antigravity", "m1")
    assert conn is not None and idx is not None


def test_sticky_rebinds_when_bound_account_is_breaker_open():
    prov = _provider()
    cfg = {"providers": {"antigravity": prov}}
    store = oab.get_store()
    store.bind("sess-B", "antigravity", 0)
    eligible = [{"index": i, "conn": c} for i, c in enumerate(prov["connections"])]
    out = oab.apply_affinity(
        "antigravity", prov, eligible, "sess-B", model_id="m1",
        breaker=_TupleBreaker(open_indexes={0}), config=cfg,
    )
    assert len(out) == 1
    assert out[0]["index"] == 1  # first healthy peer, deterministically
    assert store.lookup("sess-B", "antigravity") == 1


def test_is_healthy_handles_tuple_and_bool_breakers():
    assert oab._is_healthy(_TupleBreaker({1}), "p", "m", 0) is True
    assert oab._is_healthy(_TupleBreaker({1}), "p", "m", 1) is False

    class _BoolBreaker:
        enabled = True

        def is_open(self, p, m, i):
            return i == 2

    assert oab._is_healthy(_BoolBreaker(), "p", "m", 2) is False
    assert oab._is_healthy(_BoolBreaker(), "p", "m", 0) is True
    assert oab._is_healthy(None, "p", "m", 0) is True


def test_account_client_cache_isolates_accounts():
    made = []

    def builder(proxy_url=None, verify=True):
        c = object()
        made.append((proxy_url, verify, c))
        return c

    a1 = oab.get_client_for_account("acct-a", "http://p1", build_client=builder)
    a2 = oab.get_client_for_account("acct-b", "http://p1", build_client=builder)
    a1_again = oab.get_client_for_account("acct-a", "http://p1", build_client=builder)
    a1_other_proxy = oab.get_client_for_account("acct-a", "http://p2", build_client=builder)
    assert a1 is not a2
    assert a1 is a1_again
    assert a1 is not a1_other_proxy
    assert len(made) == 3


def test_account_identity_never_uses_token():
    conn = {"api_key": "secret-token", "oauth_email": "me@example.com"}
    assert oab.account_identity(conn, 3) == "me@example.com"
    assert oab.account_identity({"api_key": "secret-token"}, 3) == "idx:3"
    assert "secret-token" not in str(oab.account_identity(conn, 3))


def test_main_import_registers_affinity_hook():
    import app.main  # noqa: F401  (conftest isolates config.yaml before this import)

    assert mr._AFFINITY_HOOK is oab.resolver_affinity_hook


def test_all_oauth_providers_covered_in_affinity_and_profiles():
    expected = {
        "antigravity", "gemini-cli", "codex", "claude", "claude_code",
        "claude-code", "claudecode", "kiro", "cursor", "github",
        "copilot", "github-copilot", "grok-cli", "qwen", "qwen-code",
        "commandcode", "iflow", "cline", "kimi-coding",
    }
    assert expected.issubset(oab.OAUTH_AFFINITY_PROVIDERS)
    for prov in ("antigravity", "codex", "claude", "kiro", "cursor", "github", "copilot", "grok-cli", "qwen"):
        assert oab.is_oauth_provider(prov) is True
        min_iv, jitter = oab.provider_cadence_params(prov, oab.resolve_antiban_config(None))
        assert min_iv >= 0.6
        assert 0.2 <= jitter <= 0.5
    # Kiro has stricter 1.2s min_interval_seconds per single-stream guard
    kiro_iv, _ = oab.provider_cadence_params("kiro", oab.resolve_antiban_config(None))
    assert kiro_iv == 1.2


def test_is_oauth_connection_recognizes_provider_data_and_token_type_markers():
    assert oab.is_oauth_connection({"enabled": True, "api_key": "plain-key"}) is False
    assert oab.is_oauth_connection({"enabled": True, "token_type": "oauth", "api_key": "tok"}) is True
    assert oab.is_oauth_connection({"enabled": True, "provider_data": {"chatgptAccountId": "acct-1"}}) is True
    assert oab.is_oauth_connection({"enabled": True, "provider_data": {"profileArn": "arn:aws:codewhisperer:1"}}) is True
    assert oab.is_oauth_connection({"enabled": True, "provider_data": {"copilotToken": "ghu_1"}}) is True


def test_multi_provider_session_rhythm_headers_and_cursor_checksum():
    import asyncio
    rhythm = oab.get_claude_rhythm()

    claude_h = rhythm.headers_for_provider("claude", "sess-1", "acct-claude")
    assert "x-claude-code-session-id" in claude_h
    assert claude_h["X-Stainless-Runtime"] == "node"

    codex_h1 = rhythm.headers_for_provider("codex", "sess-1", "acct-codex")
    codex_h2 = rhythm.headers_for_provider("codex", "sess-1", "acct-codex")
    assert codex_h1["session_id"] == codex_h2["session_id"]
    assert codex_h1["Conversation-Id"] == codex_h1["session_id"]

    kiro_h = rhythm.headers_for_provider("kiro", "sess-1", "acct-kiro")
    assert len(kiro_h["amz-sdk-invocation-id"]) == 36
    assert kiro_h["x-amzn-codewhisperer-optout"] == "true"

    cursor_h1 = rhythm.headers_for_provider("cursor", "sess-1", "acct-cursor-A")
    cursor_h2 = rhythm.headers_for_provider("cursor", "sess-1", "acct-cursor-B")
    assert "/" in cursor_h1["x-cursor-checksum"]
    # Different Cursor accounts must have distinct hardware fingerprints
    assert cursor_h1["x-cursor-checksum"][8:] != cursor_h2["x-cursor-checksum"][8:]
    assert cursor_h1["x-ghost-mode"] == "true"

    copilot_h = rhythm.headers_for_provider("copilot", "sess-1", "acct-gh")
    assert copilot_h["Copilot-Integration-Id"] == "vscode-chat"
    assert copilot_h["Openai-Intent"] == "conversation-panel"

    ag_h = rhythm.headers_for_provider("antigravity", "sess-1", "acct-ag")
    assert ag_h == {}
    gcli_h = rhythm.headers_for_provider("gemini-cli", "sess-1", "acct-gcli")
    assert gcli_h["X-Goog-Api-Client"].startswith("gl-node/")

    # govern_pre_send scores human (>=0.75) for real OAuth connections
    decision = asyncio.run(
        oab.govern_pre_send(
            "codex",
            {"oauth_email": "u@example.com", "refresh_token": "r1"},
            0,
            "sess-codex-1",
            {"tools": {"oauth_antiban": {"min_interval_seconds": 0.0}}},
        )
    )
    assert decision["proceed"] is True
    assert decision["grade"] == "human"
    assert decision["score"] >= 0.85
    assert "session_id" in decision["headers"]


def test_antigravity_envelope_and_terminal_tos_ban_isolation():
    from app.compat.adapters.antigravity_upstream import (
        AntigravityUpstreamClient,
        openai_to_cloudcode_envelope,
    )
    from app.circuit_breaker import CircuitBreaker

    payload = {"messages": [{"role": "user", "content": "hello world"}]}
    env_a = openai_to_cloudcode_envelope(payload, "gemini-4-argon", account_id="id:acct-A")
    env_b = openai_to_cloudcode_envelope(payload, "gemini-4-argon", account_id="id:acct-B")

    # Never emit literal "bsl-" watermarks on the wire
    assert not env_a["requestId"].startswith("bsl-")
    assert not env_a["request"]["sessionId"].startswith("bsl-")
    # Different accounts must never share fallback project or sessionId
    assert env_a["project"] != env_b["project"]
    assert env_a["request"]["sessionId"] != env_b["request"]["sessionId"]

    # AntigravityUpstreamClient strips leaked CLI/proxy headers
    class _DummyInner:
        def build_request(self, method, url, headers=None, content=None, **kw):
            return {"headers": headers, "content": content}

    wrapped = AntigravityUpstreamClient(
        _DummyInner(), "gemini-4-argon", project="real-proj-123", account_id="id:acct-A"
    )
    req = wrapped.build_request(
        "POST",
        "https://daily-cloudcode-pa.googleapis.com/v1/chat/completions",
        headers={
            "Authorization": "Bearer tok",
            "X-Goog-Api-Client": "gl-node/22.14.0",
            "Client-Metadata": "ideType=IDE_UNSPECIFIED",
            "x-request-source": "local",
        },
        content=payload,
    )
    lower_keys = {k.lower() for k in req["headers"]}
    assert "x-goog-api-client" not in lower_keys
    assert "client-metadata" not in lower_keys
    assert "x-request-source" not in lower_keys

    # Terminal ToS 403 ban on one model quarantines the connection across ALL models
    cb = CircuitBreaker({"circuit_breaker": {"enabled": True, "recovery_timeout": 30}})
    cb.record_outcome(
        "antigravity",
        "gemini-4-argon-low",
        1,
        403,
        error_msg="This service has been disabled in this account for violation of Terms of Service.",
    )
    is_open_same, rem_same = cb.is_open("antigravity", "gemini-4-argon-low", 1)
    is_open_other, rem_other = cb.is_open("antigravity", "gemini-4-argon", 1)
    is_open_healthy, _ = cb.is_open("antigravity", "gemini-4-argon", 0)
    assert is_open_same is True and rem_same > 3600
    assert is_open_other is True and rem_other > 3600
    assert is_open_healthy is False


