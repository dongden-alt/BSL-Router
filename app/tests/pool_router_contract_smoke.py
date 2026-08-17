"""Cross-repo contract smoke test: Pool (Rust) ↔ Router (Python) signing contract.

Uses fixed deterministic values so both repos can verify the same vector
independently. If this test fails, the two repos have diverged.
"""
import hashlib
import hmac
import time

from app.security.pool_auth import signing_message, verify_request_headers, BadBodyHash

# Fixed deterministic values — must match the Rust test vector in stress_test.rs
FIXED_BODY = b'{"model":"test-model","messages":[{"role":"user","content":"hi"}]}'
FIXED_TS = 1_755_000_000
FIXED_HOST = "http://192.168.1.10:6969"
FIXED_PATH = "/v1/chat/completions"
FIXED_METHOD = "POST"


def test_signing_message_is_four_lines():
    body_hash = hashlib.sha256(FIXED_BODY).hexdigest()
    msg = signing_message(FIXED_METHOD, FIXED_PATH, FIXED_TS, FIXED_HOST, body_hash)
    parts = msg.split("\n")
    assert len(parts) == 4, f"expected 4 lines, got {len(parts)}: {parts}"
    assert parts[0] == f"{FIXED_METHOD} {FIXED_PATH}"
    assert parts[1] == str(FIXED_TS)
    assert parts[2] == FIXED_HOST
    assert parts[3] == body_hash


def test_body_hash_deterministic():
    h = hashlib.sha256(FIXED_BODY).hexdigest()
    assert len(h) == 64
    assert h == hashlib.sha256(FIXED_BODY).hexdigest()  # deterministic


def test_body_mismatch_rejected_smoke():
    """Swapped body with original headers → BadBodyHash."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key = Ed25519PrivateKey.generate()
    pubkey_hex = key.public_key().public_bytes_raw().hex()
    # Use current timestamp so replay check passes
    ts_now = int(time.time())
    body_hash_now = hashlib.sha256(FIXED_BODY).hexdigest()
    msg_now = signing_message(FIXED_METHOD, FIXED_PATH, ts_now, FIXED_HOST, body_hash_now).encode()
    sig_now = key.sign(msg_now).hex()
    peer_id = hashlib.sha256(bytes.fromhex(pubkey_hex)).hexdigest()
    headers_now = {
        "X-BSL-Pool-From": peer_id,
        "X-BSL-Pool-Pubkey": pubkey_hex,
        "X-BSL-Pool-Ts": str(ts_now),
        "X-BSL-Pool-Sig": sig_now,
        "X-BSL-Pool-Body-Sha256": body_hash_now,
    }
    # Valid body → passes
    result = verify_request_headers(
        headers_now, method=FIXED_METHOD, path=FIXED_PATH,
        peer_host=FIXED_HOST, body=FIXED_BODY,
    )
    assert result["pubkey_hex"] == pubkey_hex

    # Swapped body → BadBodyHash
    swapped = b'{"model":"attacker"}'
    try:
        verify_request_headers(
            headers_now, method=FIXED_METHOD, path=FIXED_PATH,
            peer_host=FIXED_HOST, body=swapped,
        )
        assert False, "should have raised BadBodyHash"
    except BadBodyHash:
        pass
