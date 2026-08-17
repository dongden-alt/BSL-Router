"""Tests for app/security/pool_auth.py — BSL Pool Tier-2 inbound Ed25519 verifier.

Uses cryptography.hazmat.primitives.asymmetric.ed25519 to generate fixtures.
No FastAPI dependency unless explicitly testing middleware integration.
"""

import time
import hashlib

import pytest
from fastapi import FastAPI, Request
from starlette.testclient import TestClient

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

# ---------------------------------------------------------------------------
# Local imports
# ---------------------------------------------------------------------------

from app.security.pool_auth import (
    PoolAuthError,
    MissingHeaders,
    BadPubkey,
    BadSignature,
    ReplayWindow,
    NotAllowlisted,
    signing_message,
    verify_signature,
    is_within_replay_window,
    verify_request_headers,
    load_pool_auth_config,
    make_pool_auth_middleware,
    POOL_AUTH_PATH,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def keypair():
    """Generate an Ed25519 keypair for signing/verification tests."""
    return Ed25519PrivateKey.generate()


@pytest.fixture()
def attacker_keypair():
    """A second, unrelated keypair (the 'attacker')."""
    return Ed25519PrivateKey.generate()


@pytest.fixture()
def pubkey_hex(keypair):
    """Hex-encoded public key (64 hex chars = 32 bytes)."""
    return keypair.public_key().public_bytes_raw().hex()


@pytest.fixture()
def attacker_pubkey_hex(attacker_keypair):
    return attacker_keypair.public_key().public_bytes_raw().hex()


@pytest.fixture()
def peer_host():
    return "http://192.168.1.10:6969"


@pytest.fixture()
def method():
    return "POST"


@pytest.fixture()
def path():
    return "/v1/chat/completions"


# ---------------------------------------------------------------------------
# Helper: sign like the Pool does (mirror auth.rs)
# ---------------------------------------------------------------------------


def _sign_tier2(private_key, method, path, ts, peer_host) -> str:
    msg = signing_message(method, path, ts, peer_host).encode("utf-8")
    return private_key.sign(msg).hex()


def _make_headers(pubkey_hex_val: str, sig_hex: str, ts: int) -> dict:
    """Build all four X-BSL-Pool-* headers from a pubkey + signature + ts."""
    peer_id = hashlib.sha256(bytes.fromhex(pubkey_hex_val)).hexdigest()
    return {
        "X-BSL-Pool-From": peer_id,
        "X-BSL-Pool-Pubkey": pubkey_hex_val,
        "X-BSL-Pool-Ts": str(ts),
        "X-BSL-Pool-Sig": sig_hex,
    }


def _make_headers_partial(**kwargs) -> dict:
    """Build a partial header dict for missing-header tests.

    kwargs keys map to header names minus the leading 'X-BSL-Pool-':
      e.g. {'pubkey': '<hex>'} → X-BSL-Pool-Pubkey
    """
    prefix = "X-BSL-Pool-"
    mapping = {
        "from": "From",
        "pubkey": "Pubkey",
        "ts": "Ts",
        "sig": "Sig",
    }
    result = {}
    for short_val, full_name in mapping.items():
        header_name = prefix + full_name
        val = kwargs.get(short_val)
        if val is not None:
            if short_val == "from" and len(val) < 64:
                val = hashlib.sha256(b"x" * 32).hexdigest()
            result[header_name] = val
    return result


# ===========================================================================
# Middleware builder helper
# ===========================================================================


def _build_test_app(pool_cfg: dict) -> tuple[FastAPI, dict]:
    """Build a minimal FastAPI app using the REAL shared middleware factory.

    Critical: tests must exercise ``make_pool_auth_middleware`` from
    ``app.security.pool_auth`` — NOT a reimplemented copy. A prior audit
    caught production bugs that tests missed because they duplicated the
    middleware inline with the correct logic while main.py had the wrong one.
    """
    app = FastAPI()
    # pool_cfg is the FULL root config dict (may contain pool_auth key).
    app.middleware("http")(make_pool_auth_middleware(lambda: pool_cfg))

    @app.post("/v1/chat/completions")
    async def stub_chat(req: Request):
        return {"id": "stub", "choices": [{"message": {"role": "assistant", "content": "ok"}}]}

    @app.get("/health")
    async def stub_health():
        return {"status": "ok"}

    return app, pool_cfg


# ===========================================================================
# Unit tests — no FastAPI
# ===========================================================================


class TestSigningMessage:
    def test_exact_pool_format(self, method, path, peer_host):
        """MUST produce exactly f"{method} {path}\\n{ts}\\n{peer_host}"."""
        ts_now = 1_755_000_000
        got = signing_message(method, path, ts_now, peer_host)
        expected = f"{method} {path}\n{ts_now}\n{peer_host}"
        assert got == expected

    def test_newlines_are_literal(self, method, path, peer_host):
        """Ensure newlines are actual \\n characters, not escaped."""
        ts_now = 1_755_000_000
        msg = signing_message(method, path, ts_now, peer_host)
        assert "\n" in msg
        parts = msg.split("\n")
        assert len(parts) == 3
        assert parts[0] == f"{method} {path}"
        assert parts[1] == str(ts_now)
        assert parts[2] == peer_host


class TestValidSignatureVerifies:
    def test_valid_signature_passes(self, keypair, pubkey_hex, method, path, peer_host):
        ts = int(time.time())
        sig = _sign_tier2(keypair, method, path, ts, peer_host)
        verify_signature(pubkey_hex, method, path, ts, peer_host, sig)

    def test_verify_returns_none_on_success(self, keypair, pubkey_hex, method, path, peer_host):
        ts = int(time.time())
        sig = _sign_tier2(keypair, method, path, ts, peer_host)
        result = verify_signature(pubkey_hex, method, path, ts, peer_host, sig)
        assert result is None


class TestWrongPathFails:
    def test_different_path_fails(self, keypair, pubkey_hex, method, path, peer_host):
        ts = int(time.time())
        correct_sig = _sign_tier2(keypair, method, path, ts, peer_host)
        with pytest.raises(BadSignature):
            verify_signature(pubkey_hex, method, "/v1/other", ts, peer_host, correct_sig)


class TestWrongHostFails:
    def test_different_host_fails(self, keypair, pubkey_hex, method, path, peer_host):
        ts = int(time.time())
        correct_sig = _sign_tier2(keypair, method, path, ts, peer_host)
        with pytest.raises(BadSignature):
            verify_signature(pubkey_hex, method, path, ts, "http://evil.local:9999", correct_sig)


class TestAttackerPubkeyFails:
    def test_foreign_pubkey_rejects_signatures_signed_by_other_key(
        self, keypair, attacker_keypair, pubkey_hex, peer_host
    ):
        """Signature produced by attacker must never verify under victim's pubkey."""
        # Use current time so replay-window check passes and BadSignature fires
        ts = int(time.time())
        attacker_sig = _sign_tier2(
            attacker_keypair, "POST", "/v1/chat/completions", ts, peer_host,
        )
        with pytest.raises(BadSignature):
            verify_signature(
                pubkey_hex,
                "POST",
                "/v1/chat/completions",
                ts,
                peer_host,
                attacker_sig,
            )


class TestReplayWindow:
    def test_replay_outside_window_fails(self, keypair, pubkey_hex, method, path, peer_host):
        ts = int(time.time()) - 400  # 400 seconds ago (> 300s window)
        sig = _sign_tier2(keypair, method, path, ts, peer_host)
        with pytest.raises(ReplayWindow):
            verify_signature(pubkey_hex, method, path, ts, peer_host, sig)

    def test_replay_inside_window_passes(self, keypair, pubkey_hex, method, path, peer_host):
        ts = int(time.time()) - 200  # within ±300s
        sig = _sign_tier2(keypair, method, path, ts, peer_host)
        verify_signature(pubkey_hex, method, path, ts, peer_host, sig)

    def test_future_timestamp_inside_window_passes(self, keypair, pubkey_hex, method, path, peer_host):
        ts = int(time.time()) + 100  # slightly in the future
        sig = _sign_tier2(keypair, method, path, ts, peer_host)
        verify_signature(pubkey_hex, method, path, ts, peer_host, sig)

    def test_is_within_replay_window_boundary(self):
        now = int(time.time())
        assert is_within_replay_window(now, 300) is True
        assert is_within_replay_window(now - 300, 300) is True
        assert is_within_replay_window(now + 300, 300) is True
        assert is_within_replay_window(now - 301, 300) is False
        assert is_within_replay_window(now + 301, 300) is False


class TestAllowlist:
    def test_allowlist_empty_accepts(self, keypair, peer_host):
        ts = int(time.time())
        pubkey = keypair.public_key().public_bytes_raw().hex()
        sig = _sign_tier2(keypair, "POST", "/v1/chat/completions", ts, peer_host)
        headers = _make_headers(pubkey, sig, ts)
        result = verify_request_headers(
            headers, method="POST", path="/v1/chat/completions",
            peer_host=peer_host, allowlist=[],
        )
        assert result["pubkey_hex"] == pubkey

    def test_allowlist_rejects_unknown(self, keypair, attacker_pubkey_hex, peer_host):
        ts = int(time.time())
        pubkey = keypair.public_key().public_bytes_raw().hex()
        sig = _sign_tier2(keypair, "POST", "/v1/chat/completions", ts, peer_host)
        headers = _make_headers(pubkey, sig, ts)
        with pytest.raises(NotAllowlisted):
            verify_request_headers(
                headers, method="POST", path="/v1/chat/completions",
                peer_host=peer_host, allowlist=[attacker_pubkey_hex],
            )

    def test_allowlist_accepts_known(self, keypair, peer_host):
        ts = int(time.time())
        pubkey = keypair.public_key().public_bytes_raw().hex()
        sig = _sign_tier2(keypair, "POST", "/v1/chat/completions", ts, peer_host)
        headers = _make_headers(pubkey, sig, ts)
        result = verify_request_headers(
            headers, method="POST", path="/v1/chat/completions",
            peer_host=peer_host, allowlist=[pubkey],
        )
        assert result["pubkey_hex"] == pubkey


class TestMissingHeaders:
    def test_all_missing_raises(self):
        with pytest.raises(MissingHeaders):
            verify_request_headers(
                {}, method="POST", path="/v1/chat/completions",
                peer_host="http://example.com:6969",
            )

    def test_only_pubkey_present_raises(self, keypair):
        headers = {"X-BSL-Pool-Pubkey": keypair.public_key().public_bytes_raw().hex()}
        with pytest.raises(MissingHeaders):
            verify_request_headers(
                headers, method="POST", path="/v1/chat/completions",
                peer_host="http://example.com:6969",
            )

    def test_three_of_four_present_still_raises(self, keypair, peer_host):
        ts = int(time.time())
        pubkey = keypair.public_key().public_bytes_raw().hex()
        headers = _make_headers_partial(pubkey=pubkey, ts=str(ts))
        # From/Pubkey/Ts present but Sig absent → MissingHeaders
        with pytest.raises(MissingHeaders):
            verify_request_headers(
                headers, method="POST", path="/v1/chat/completions",
                peer_host=peer_host,
            )


class TestBadPubkey:
    def test_non_hex_pubkey(self, peer_host):
        ts = int(time.time())
        # Directly call verify_signature with a bad pubkey; bypass _make_headers
        # because it internally hashes the pubkey (needs valid hex).
        with pytest.raises(BadPubkey):
            verify_signature(
                "zzzznotvalidhex",
                "POST", "/v1/chat/completions",
                ts, peer_host,
                "ff" * 64,  # dummy sig
            )

    def test_wrong_length_pubkey(self, peer_host):
        ts = int(time.time())
        bad_pubkey = "00" * 16  # 32 hex = 16 bytes
        headers = _make_headers(bad_pubkey, "ff" * 64, ts)
        with pytest.raises(BadPubkey):
            verify_request_headers(
                headers, method="POST", path="/v1/chat/completions",
                peer_host=peer_host,
            )


class TestLoadPoolAuthConfig:
    def test_defaults_when_empty_dict(self):
        cfg = load_pool_auth_config({})
        assert cfg["enabled"] is False
        assert cfg["mode"] == "optional"
        assert cfg["replay_window_secs"] == 300
        assert cfg["router_address"] == ""
        assert cfg["allowlist"] == []

    def test_custom_values_passed_through(self):
        raw = {
            "enabled": True,
            "mode": "required",
            "replay_window_secs": 600,
            "router_address": "http://10.0.0.1:8080/",
            "allowlist": ["abc123"],
        }
        cfg = load_pool_auth_config({"pool_auth": raw})
        assert cfg["enabled"] is True
        assert cfg["mode"] == "required"
        assert cfg["replay_window_secs"] == 600
        assert cfg["router_address"] == "http://10.0.0.1:8080"
        assert cfg["allowlist"] == ["abc123"]

    def test_missing_section_returns_defaults(self):
        cfg = load_pool_auth_config({"other_section": True})
        assert cfg["enabled"] is False

    def test_null_section_returns_defaults(self):
        cfg = load_pool_auth_config({"pool_auth": None})
        assert cfg["enabled"] is False
    def test_double_unwrap_does_not_force_defaults(self):
        """Regression: callers must pass FULL root config, not pre-unwrapped pool_auth.

        A production bug called::

            load_pool_auth_config(cs_get_config().get("pool_auth") or {})

        which always forced defaults and made enabled=True unreachable.
        """
        # Correct: full root
        cfg = load_pool_auth_config({"pool_auth": {"enabled": True, "mode": "required"}})
        assert cfg["enabled"] is True
        assert cfg["mode"] == "required"
        # Wrong shape (pre-unwrapped) must NOT silently look enabled
        wrong = load_pool_auth_config({"enabled": True, "mode": "required"})
        assert wrong["enabled"] is False, "pre-unwrapped dict must not enable auth"





class TestFromMatchesPeerId:
    def test_from_mismatch_rejected(self, keypair, pubkey_hex, peer_host):
        ts = int(time.time())
        sig = _sign_tier2(keypair, "POST", "/v1/chat/completions", ts, peer_host)
        headers = _make_headers(pubkey_hex, sig, ts)
        headers["X-BSL-Pool-From"] = "00" * 32  # spoofed peer_id
        with pytest.raises(BadSignature):
            verify_request_headers(
                headers, method="POST", path="/v1/chat/completions",
                peer_host=peer_host,
            )

    def test_from_matches_derived_peer_id(self, keypair, pubkey_hex, peer_host):
        ts = int(time.time())
        sig = _sign_tier2(keypair, "POST", "/v1/chat/completions", ts, peer_host)
        headers = _make_headers(pubkey_hex, sig, ts)
        result = verify_request_headers(
            headers, method="POST", path="/v1/chat/completions",
            peer_host=peer_host,
        )
        assert result["peer_id"] == headers["X-BSL-Pool-From"]


# ===========================================================================
# Middleware integration tests (FastAPI TestClient)
# ===========================================================================


class TestMiddlewareDisabledPassesUnsigned:
    def test_unsigned_post_without_pool_auth_enabled(self):
        """When pool_auth.enabled=False, unsigned POST /v1/chat/completions → 200."""
        app, _ = _build_test_app({"pool_auth": {"enabled": False}})
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/v1/chat/completions", json={"model": "gpt-4o", "messages": []})
        assert resp.status_code == 200


class TestMiddlewareRequiredRejectsUnsigned:
    def test_unsigned_post_with_required_mode_returns_401(self):
        """required mode without headers → 401."""
        app, _ = _build_test_app({
            "pool_auth": {"enabled": True, "mode": "required"},
        })
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/v1/chat/completions", json={"model": "gpt-4o", "messages": []})
        assert resp.status_code == 401
        body = resp.json()
        assert body["error"]["type"] == "pool_auth"


class TestMiddlewareRequiredAcceptsValidSignature:
    def test_valid_signature_succeeds(self, keypair, pubkey_hex, peer_host):
        """required mode with valid signature → 200."""
        ts = int(time.time())
        sig = _sign_tier2(keypair, "POST", "/v1/chat/completions", ts, peer_host)
        headers = _make_headers(pubkey_hex, sig, ts)
        app, _ = _build_test_app({
            "pool_auth": {"enabled": True, "mode": "required", "router_address": peer_host},
        })
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o", "messages": []},
            headers=headers,
        )
        assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"


class TestMiddlewareOptionalPassesUnsigned:
    def test_optional_mode_no_headers_passes(self):
        """optional mode, no pool headers → pass through."""
        app, _ = _build_test_app({
            "pool_auth": {"enabled": True, "mode": "optional"},
        })
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/v1/chat/completions", json={"model": "gpt-4o", "messages": []})
        assert resp.status_code == 200


class TestMiddlewareOptionalRejectsBadSignature:
    def test_partial_headers_fail(self, keypair, pubkey_hex, peer_host):
        """optional mode: one header present but others missing → 401."""
        headers = {"X-BSL-Pool-Pubkey": pubkey_hex}
        app, _ = _build_test_app({
            "pool_auth": {"enabled": True, "mode": "optional", "router_address": peer_host},
        })
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o", "messages": []},
            headers=headers,
        )
        assert resp.status_code == 401

    def test_bad_signature_rejected(self, keypair, pubkey_hex, peer_host):
        """Optional mode: all headers present but bad sig → 401."""
        ts = int(time.time())
        fake_sig = "ff" * 64
        headers = _make_headers(pubkey_hex, fake_sig, ts)
        app, _ = _build_test_app({
            "pool_auth": {"enabled": True, "mode": "optional", "router_address": peer_host},
        })
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o", "messages": []},
            headers=headers,
        )
        assert resp.status_code == 401


class TestHealthNeverGated:
    def test_health_get_not_gated_even_when_required(self):
        """/health GET must never be gated by pool_auth middleware."""
        app, _ = _build_test_app({
            "pool_auth": {"enabled": True, "mode": "required"},
        })
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/health")
        assert resp.status_code == 200
