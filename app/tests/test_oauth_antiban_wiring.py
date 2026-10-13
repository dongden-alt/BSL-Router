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
