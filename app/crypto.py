"""Encryption layer for BSL Router config secrets.

Uses Fernet symmetric encryption. Key is generated ONCE, then wrapped
(stored encrypted) using Windows DPAPI (machine + user bound) on Windows,
or a restricted key file on other platforms.

Key design (P0-1 fix):
  - First call: generate random Fernet key → wrap with CryptProtectData →
    store the wrapped blob in .bsl_key.dpapi
  - Subsequent calls: read .bsl_key.dpapi → CryptUnprotectData → same key
  - This is deterministic across restarts (unlike hashing CryptProtectData
    output, which embeds a random session key per call)

Encrypted values are prefixed with 'enc:' in the config dict.
All existing code reads plaintext via cs_get_config() — the decrypt
layer in config_state.py handles this transparently.
"""
from __future__ import annotations

import os
import copy

from cryptography.fernet import Fernet

_ENC_PREFIX = "enc:"
_APP_SALT = b"BSL-Router-v1-encryption-salt"
_fernet: Fernet | None = None

# Key-wrapping file path (stores DPAPI-wrapped Fernet key)
_KEY_FILE_DPAPI = ".bsl_key.dpapi"
_KEY_FILE_FALLBACK = ".bsl_key"


def _project_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _get_or_create_key() -> bytes:
    """Retrieve or create the Fernet key (deterministic across restarts).

    PRIMARY (Windows with pywin32):
      1. If .bsl_key.dpapi exists: read it → CryptUnprotectData → return key
      2. Else: generate random Fernet key → CryptProtectData(key) → write blob
         to .bsl_key.dpapi → return key

    FALLBACK (non-Windows or no pywin32):
      1. If .bsl_key exists: read it → return key
      2. Else: generate random key → write to .bsl_key (chmod 600) → return key
    """
    root = _project_root()

    # --- Primary: DPAPI key-wrapping ---
    try:
        import win32crypt

        dpapi_path = os.path.join(root, _KEY_FILE_DPAPI)
        if os.path.exists(dpapi_path):
            with open(dpapi_path, "rb") as f:
                wrapped_key = f.read()
            # Unwrap: CryptUnprotectData may return (desc, bytes) or just bytes
            # depending on pywin32 build. Handle both.
            result = win32crypt.CryptUnprotectData(wrapped_key, None, None, None, 0)
            if isinstance(result, tuple):
                unwrapped = result[1]  # (description, raw_bytes)
            else:
                unwrapped = result     # raw_bytes directly
            if not unwrapped:
                # Silent DPAPI failure: empty blob (corrupt .bsl_key.dpapi,
                # or pywin32 bug). Log loudly and fall through to the file
                # fallback instead of letting Fernet() crash later with
                # "Fernet key must be 32 url-safe base64-encoded bytes".
                print(
                    "[CRYPTO] DPAPI unwrap returned an EMPTY key "
                    f"({os.path.getsize(dpapi_path)} byte blob) — using file fallback.",
                    flush=True,
                )
                raise ValueError("DPAPI unwrap returned empty key")
            return unwrapped

        # First run: generate Fernet key, wrap with DPAPI, persist
        fernet_key = Fernet.generate_key()  # 32 random bytes, urlsafe-b64 encoded
        # CryptProtectData may return (desc, bytes) or just bytes.
        result = win32crypt.CryptProtectData(fernet_key, "BSL-Router-key", None, None, None, 0)
        if isinstance(result, tuple):
            wrapped = result[1]
        else:
            wrapped = result
        if not wrapped:
            print("[CRYPTO] DPAPI wrap returned an EMPTY blob — using file fallback.", flush=True)
            raise ValueError("DPAPI wrap returned empty blob")
        with open(dpapi_path, "wb") as f:
            f.write(wrapped)
        # Restrict file permissions (Windows: rely on NTFS default ACL)
        try:
            os.chmod(dpapi_path, 0o600)
        except OSError:
            pass
        return fernet_key

    except ImportError:
        pass  # pywin32 not available
    except Exception as e:
        # DPAPI failed (non-Windows, or pywin32 issue) — fall through to file key
        print(f"[CRYPTO] DPAPI key-wrap unavailable ({e}), using file fallback.", flush=True)

    # --- Fallback: plain key file ---
    key_path = os.path.join(root, _KEY_FILE_FALLBACK)
    if os.path.exists(key_path):
        with open(key_path, "rb") as f:
            return f.read()
    key = Fernet.generate_key()
    with open(key_path, "wb") as f:
        f.write(key)
    try:
        os.chmod(key_path, 0o600)
    except OSError:
        pass
    return key


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_get_or_create_key())
    return _fernet


def _reset_fernet_cache() -> None:
    """Reset the cached Fernet instance (for testing)."""
    global _fernet
    _fernet = None


def encrypt_value(plaintext: str) -> str:
    """Encrypt a string. Returns 'enc:gAAAAA...'.

    Already-encrypted values are returned unchanged (idempotent).
    Empty strings are returned as-is.
    """
    if not plaintext or is_encrypted(plaintext):
        return plaintext
    token = _get_fernet().encrypt(plaintext.encode("utf-8"))
    return _ENC_PREFIX + token.decode("ascii")


# Placeholder ciphertexts shipped in config.example.yaml. These are NOT real
# Fernet tokens, so attempting to decrypt them always fails. Recognising them
# lets a first-run user boot without alarming crypto errors.
_PLACEHOLDER_MARKERS = ("YOUR_", "_HERE", "CHANGEME", "CHANGE_ME", "EXAMPLE")


def _looks_like_placeholder(ciphertext: str) -> bool:
    """True when the value is an example template stand-in, not a real token."""
    upper = ciphertext.upper()
    return any(marker in upper for marker in _PLACEHOLDER_MARKERS)


def decrypt_value(value: str) -> str:
    """Decrypt an 'enc:...' string. Plaintext values pass through unchanged.

    On decryption failure (wrong machine, corrupted key, or an unfilled example
    placeholder), returns an empty string rather than crashing the server.
    """
    if not value or not is_encrypted(value):
        return value
    ciphertext = value[len(_ENC_PREFIX):]

    # Unfilled config.example.yaml placeholders are expected on first run.
    # Stay quiet: the admin UI already shows these connections as unconfigured.
    if _looks_like_placeholder(ciphertext):
        return ""

    token = ciphertext.encode("ascii")
    try:
        return _get_fernet().decrypt(token).decode("utf-8")
    except Exception as e:
        # InvalidToken carries no message, so str(e) is often empty. Report the
        # exception TYPE plus remediation guidance instead of a bare blank line.
        reason = str(e) or type(e).__name__
        print(
            f"[CRYPTO] Could not decrypt a stored credential ({reason}). "
            "This usually means config.yaml was copied from another machine: "
            "the encryption key is machine-bound. Re-enter the affected API key "
            "in the admin UI to re-encrypt it locally.",
            flush=True,
        )
        return ""


def is_encrypted(value: str) -> bool:
    """Check if a value is encrypted (starts with 'enc:')."""
    return isinstance(value, str) and value.startswith(_ENC_PREFIX)


# ── Config-level operations ────────────────────────────────────────────────────

_SENSITIVE_FIELDS = ("api_key", "refresh_token", "access_token")


def _encrypt_connection(conn: dict) -> None:
    """Encrypt sensitive fields in a single connection dict (in-place)."""
    for field in _SENSITIVE_FIELDS:
        val = conn.get(field)
        if val and isinstance(val, str) and not is_encrypted(val):
            conn[field] = encrypt_value(val)


def _decrypt_connection(conn: dict) -> None:
    """Decrypt sensitive fields in a single connection dict (in-place)."""
    for field in _SENSITIVE_FIELDS:
        val = conn.get(field)
        if val and isinstance(val, str) and is_encrypted(val):
            conn[field] = decrypt_value(val)


def encrypt_config_secrets(config: dict) -> dict:
    """Walk config dict, encrypt all sensitive fields in-place.

    Encrypts:
    - providers.*.connections[].api_key
    - providers.*.connections[].refresh_token
    - providers.*.connections[].access_token
    - providers.*.api_key (legacy single-key)
    - admin.password

    Already-encrypted values are left unchanged (idempotent).
    """
    if not isinstance(config, dict):
        return config

    providers = config.get("providers", {})
    if isinstance(providers, dict):
        for prov_data in providers.values():
            if not isinstance(prov_data, dict):
                continue
            connections = prov_data.get("connections", [])
            if isinstance(connections, list):
                for conn in connections:
                    if isinstance(conn, dict):
                        _encrypt_connection(conn)
            # Legacy single-key
            val = prov_data.get("api_key")
            if val and isinstance(val, str) and not is_encrypted(val):
                prov_data["api_key"] = encrypt_value(val)

    admin = config.get("admin", {})
    if isinstance(admin, dict):
        val = admin.get("password")
        if val and isinstance(val, str) and not is_encrypted(val):
            admin["password"] = encrypt_value(val)

    return config


def decrypt_config_secrets(config: dict) -> dict:
    """Walk config dict, decrypt all sensitive fields.

    Returns a NEW dict (shallow copy with deep-copied sensitive sections).
    Non-sensitive sections are shared references to avoid copying 7,700
    lines of config on every get_config() call.
    """
    if not isinstance(config, dict):
        return config

    # Shallow copy the top-level dict
    result = dict(config)

    # Deep-copy only the providers section (where sensitive fields live)
    providers = result.get("providers")
    if isinstance(providers, dict):
        providers_copy = copy.deepcopy(providers)
        result["providers"] = providers_copy
        for prov_data in providers_copy.values():
            if not isinstance(prov_data, dict):
                continue
            connections = prov_data.get("connections", [])
            if isinstance(connections, list):
                for conn in connections:
                    if isinstance(conn, dict):
                        _decrypt_connection(conn)
            val = prov_data.get("api_key")
            if val and isinstance(val, str) and is_encrypted(val):
                prov_data["api_key"] = decrypt_value(val)

    # Deep-copy admin section (has password)
    admin = result.get("admin")
    if isinstance(admin, dict):
        admin_copy = copy.deepcopy(admin)
        result["admin"] = admin_copy
        val = admin_copy.get("password")
        if val and isinstance(val, str) and is_encrypted(val):
            admin_copy["password"] = decrypt_value(val)

    return result


def migrate_plaintext_config(config: dict) -> tuple[dict, bool]:
    """Detect and encrypt plaintext secrets.

    Returns (config, migrated_bool).
    - If no plaintext secrets found, returns (config, False).
    - If plaintext secrets found, encrypts them and returns (config, True).
    """
    if not isinstance(config, dict):
        return config, False

    migrated = False

    providers = config.get("providers", {})
    if isinstance(providers, dict):
        for prov_data in providers.values():
            if not isinstance(prov_data, dict):
                continue
            connections = prov_data.get("connections", [])
            if isinstance(connections, list):
                for conn in connections:
                    if not isinstance(conn, dict):
                        continue
                    for field in _SENSITIVE_FIELDS:
                        val = conn.get(field)
                        if val and isinstance(val, str) and not is_encrypted(val):
                            migrated = True
            # Legacy single-key
            val = prov_data.get("api_key")
            if val and isinstance(val, str) and not is_encrypted(val):
                migrated = True

    admin = config.get("admin", {})
    if isinstance(admin, dict):
        val = admin.get("password")
        if val and isinstance(val, str) and not is_encrypted(val):
            migrated = True

    if migrated:
        config = encrypt_config_secrets(config)

    return config, migrated
