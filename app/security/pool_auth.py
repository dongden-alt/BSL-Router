"""BSL Pool — Tier-2 inbound Ed25519 signature verifier.

Pure verification module matching the Shared Pool App (Rust / ed25519-dalek)
Tier-2 signing contract at commit ef3897f.  No FastAPI imports.

Signing contract (auth.rs, gateway.rs):
    Canonical message = f"{METHOD} {path}\\n{unix_ts}\\n{peer_host}"
    Headers on every Tier-2 POST:
        X-BSL-Pool-From   — peer_id  (SHA-256 of pubkey, hex, 64 chars)
        X-BSL-Pool-Pubkey — Ed25519 public key hex (64 chars / 32 bytes)
        X-BSL-Pool-Ts     — unix timestamp seconds (decimal string)
        X-BSL-Pool-Sig    — Ed25519 signature hex (128 chars / 64 bytes)
"""

from __future__ import annotations

import hashlib
import time
from typing import Sequence

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.exceptions import InvalidSignature

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class PoolAuthError(Exception):
    """Base exception for pool-auth failures."""


class MissingHeaders(PoolAuthError):
    """One or more required X-BSL-Pool-* headers are absent."""


class BadPubkey(PoolAuthError):
    """Public key hex is invalid (wrong length or non-hex)."""


class BadSignature(PoolAuthError):
    """Ed25519 signature verification failed."""


class ReplayWindow(PoolAuthError):
    """Timestamp falls outside the allowed replay window."""


class NotAllowlisted(PoolAuthError):
    """Public key not found in the allowlist."""


# ---------------------------------------------------------------------------
# Core primitives (mirror auth.rs byte-for-byte)
# ---------------------------------------------------------------------------


def signing_message(method: str, path: str, ts: int, peer_host: str) -> str:
    """Build the canonical UTF-8 message that was signed.

    Exact match to ``tier2_signing_message()`` in Rust auth.rs::

        "{METHOD} {path}\\n{timestamp_secs}\\n{peer_host}"
    """
    return f"{method} {path}\n{ts}\n{peer_host}"


def verify_signature(
    pubkey_hex: str,
    method: str,
    path: str,
    ts: int,
    peer_host: str,
    sig_hex: str,
    *,
    window: int = 300,
) -> None:
    """Verify an Ed25519 signature against the claimed public key.

    Raises ``BadPubkey`` on malformed public-key hex, ``BadSignature`` on
    crypto mismatch or bad signature hex, ``ReplayWindow`` when *ts* is
    outside ±*window* seconds of wall-clock time.
    """
    # replay window (auth.rs does NOT do this; we add it here for convenience)
    if not is_within_replay_window(ts, window):
        raise ReplayWindow(f"timestamp {ts} outside ±{window}s window")

    try:
        pub_bytes = bytearray.fromhex(pubkey_hex)
    except ValueError:
        raise BadPubkey("public key is not valid hex")

    if len(pub_bytes) != 32:
        raise BadPubkey(f"public key must be 32 bytes, got {len(pub_bytes)}")

    try:
        sig_bytes = bytearray.fromhex(sig_hex)
    except ValueError:
        raise BadSignature("signature is not valid hex")

    if len(sig_bytes) != 64:
        raise BadSignature(f"signature must be 64 bytes, got {len(sig_bytes)}")

    verifying_key = Ed25519PublicKey.from_public_bytes(bytes(pub_bytes))
    msg = signing_message(method, path, ts, peer_host).encode("utf-8")

    try:
        verifying_key.verify(sig_bytes, msg)
    except InvalidSignature:
        raise BadSignature("signature does not match the canonical message")


def is_within_replay_window(ts: int, window: int = 300) -> bool:
    """Return True when ``ts`` is within ±*window* seconds of now.

    Mirrors ``is_within_replay_window()`` from Rust auth.rs.
    """
    now = int(time.time())
    return abs(now - ts) <= window


# ---------------------------------------------------------------------------
# Header extraction & batch verification
# ---------------------------------------------------------------------------


_POOL_HEADER_KEYS = (
    "x-bsl-pool-from",
    "x-bsl-pool-pubkey",
    "x-bsl-pool-ts",
    "x-bsl-pool-sig",
)


def _header_get(headers, key_lower: str):
    """Case-insensitive header lookup; also accept plain dict."""
    val = headers.get(key_lower)
    if val is not None:
        return val
    # Fallback: try original-cased keys (Starlette Headers wraps CIMultiDict)
    for k, v in headers.items():
        if k.lower() == key_lower:
            return v
    return None


def verify_request_headers(
    headers,
    *,
    method: str,
    path: str,
    peer_host: str,
    window: int = 300,
    allowlist=None,
) -> dict:
    """Read the four ``X-BSL-Pool-*`` headers and run the full verification.

    Returns ``{"peer_id": ..., "pubkey_hex": ..., "ts": ...}`` on success.

    Raises:
        MissingHeaders — any of the four headers absent
        ReplayWindow   — timestamp outside ±window
        BadPubkey      — public key hex invalid
        BadSignature   — signature crypto check fails
        NotAllowlisted — allowlist set but pubkey not listed
    """
    raw_from = _header_get(headers, "x-bsl-pool-from")
    raw_pubkey = _header_get(headers, "x-bsl-pool-pubkey")
    raw_ts = _header_get(headers, "x-bsl-pool-ts")
    raw_sig = _header_get(headers, "x-bsl-pool-sig")

    # --- presence check (ALL four headers MUST be present and truthy) ---
    missing = []
    if not raw_from:
        missing.append("X-BSL-Pool-From")
    if not raw_pubkey:
        missing.append("X-BSL-Pool-Pubkey")
    if not raw_ts:
        missing.append("X-BSL-Pool-Ts")
    if not raw_sig:
        missing.append("X-BSL-Pool-Sig")
    if missing:
        raise MissingHeaders(f"missing {', '.join(missing)}")

    # --- parse timestamp ---
    try:
        ts = int(raw_ts)
    except (ValueError, TypeError):
        raise MissingHeaders(f"X-BSL-Pool-Ts is not a valid integer: {raw_ts!r}")

    pubkey_hex = raw_pubkey.strip()
    sig_hex = raw_sig.strip()

    # --- crypto + replay window (combined for this entry-point) ---
    verify_signature(pubkey_hex, method, path, ts, peer_host, sig_hex, window=window)

    # --- allowlist gate (if configured) ---
    if allowlist and len(allowlist) > 0:
        if pubkey_hex not in allowlist:
            short = pubkey_hex[:16] + "…"
            raise NotAllowlisted(f"public key {short} not in allowlist")

    # Derive peer_id = SHA-256(pubkey), hex-encoded (auth.rs Identity::peer_id)
    peer_id = hashlib.sha256(bytearray.fromhex(pubkey_hex)).hexdigest()

    # X-BSL-Pool-From must match the derived peer_id (anti-spoof attribution).
    claimed = str(raw_from).strip().lower()
    if claimed != peer_id.lower():
        raise BadSignature(
            "X-BSL-Pool-From does not match pubkey-derived peer_id"
        )

    return {
        "peer_id": peer_id,
        "pubkey_hex": pubkey_hex,
        "ts": ts,
    }


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def load_pool_auth_config(config: dict) -> dict:
    """Normalize pool_auth config from the FULL YAML/root config dict.

    Expects the top-level app config (the object that contains a ``pool_auth``
    key). Do NOT pre-unwrap ``config["pool_auth"]`` before calling this —
    that double-unwrap bug silently forced defaults forever.

    Default state: ``enabled=False``, ``mode="optional"`` — zero behaviour
    change for existing callers with no config section.
    """
    if not isinstance(config, dict):
        config = {}
    raw = config.get("pool_auth") or {}
    if not isinstance(raw, dict):
        raw = {}
    return {
        "enabled": bool(raw.get("enabled", False)),
        "mode": str(raw.get("mode", "optional")).lower(),
        "replay_window_secs": int(raw.get("replay_window_secs", 300)),
        "router_address": str(raw.get("router_address", "") or "").rstrip("/"),
        "allowlist": list(raw.get("allowlist") or []),
    }


# ---------------------------------------------------------------------------
# HTTP middleware (shared by app/main.py and tests — single source of truth)
# ---------------------------------------------------------------------------

POOL_AUTH_PATH = "/v1/chat/completions"
_POOL_HEADER_NAMES = (
    "x-bsl-pool-from",
    "x-bsl-pool-pubkey",
    "x-bsl-pool-ts",
    "x-bsl-pool-sig",
)


def _headers_have_any_pool_auth(headers) -> bool:
    """True if ANY of the four X-BSL-Pool-* headers is present."""
    for hkey in _POOL_HEADER_NAMES:
        if _header_get(headers, hkey) is not None:
            return True
    return False


def make_pool_auth_middleware(get_config):
    """Build the FastAPI HTTP middleware that gates Tier-2 chat completions.

    ``get_config`` must return the FULL app config dict (same shape as
    ``cs_get_config()``). Injected so unit tests can supply a fixed dict
    without patching global state.

    Behaviour (locked):
    - Default OFF via ``pool_auth.enabled``
    - Only POST ``/v1/chat/completions``
    - ``optional``: no headers → pass; any/partial headers → full verify
    - ``required``: always full verify
    - Auth failures → 401 JSON, never 500
    """

    async def pool_auth_middleware(request, call_next):
        # Local imports keep this module FastAPI-free at import time.
        from fastapi.responses import JSONResponse
        import logging

        log = logging.getLogger("pool_auth")

        try:
            root_cfg = get_config() or {}
            cfg = load_pool_auth_config(root_cfg)
        except Exception:
            # Config parse failure → fail-open (never 500 a chat request).
            return await call_next(request)

        if not cfg["enabled"]:
            return await call_next(request)

        if request.url.path != POOL_AUTH_PATH or request.method != "POST":
            return await call_next(request)

        peer_host = cfg["router_address"] or (
            f"{request.url.scheme}://{request.url.netloc}"
        )
        mode = cfg.get("mode", "optional")
        window = cfg.get("replay_window_secs", 300)
        allowlist = cfg.get("allowlist") or []

        # optional: no pool headers at all → mixed-traffic pass-through.
        # If ANY header is present, fall through to full verify (partial = 401).
        if mode == "optional" and not _headers_have_any_pool_auth(request.headers):
            return await call_next(request)

        try:
            result = verify_request_headers(
                request.headers,
                method=request.method,
                path=request.url.path,
                peer_host=peer_host,
                window=window,
                allowlist=allowlist,
            )
            # Attach for downstream logging/telemetry if anything cares.
            try:
                request.state.pool_peer_id = result["peer_id"]
            except Exception:
                pass
            pid = result["peer_id"]
            log.info("pool_auth OK peer_id=%s", pid[:16] + "…" if len(pid) > 16 else pid)
        except PoolAuthError as exc:
            log.warning("pool_auth reject: %s", exc)
            return JSONResponse(
                {"error": {"type": "pool_auth", "message": str(exc)}},
                status_code=401,
            )

        return await call_next(request)

    return pool_auth_middleware

