"""Config state accessors - thin indirection over the global config dict.

Provides get/replace/init operations so mutations route through
_persist_config_snapshot instead of bare dict assignment.

Secrets are stored ENCRYPTED in _config (using Fernet + DPAPI).
get_config() returns DECRYPTED values transparently.
replace_config() ENCRYPTS values before storing.
"""
from __future__ import annotations

import yaml

_config: dict = {}
_decrypted_cache: dict | None = None


def get_config() -> dict:
    """Return the live config dict with secrets DECRYPTED.

    All existing code that reads api_key/password gets plaintext.
    The internal _config stores encrypted values.

    PERFORMANCE: The decrypted result is cached in _decrypted_cache.
    Invalidation happens in replace_config() and init_config().
    With 50+ providers and 100+ connections, re-decrypting on every
    request (50+ call sites in main.py) adds significant latency.
    """
    global _decrypted_cache
    if _decrypted_cache is not None:
        return _decrypted_cache
    from app.crypto import decrypt_config_secrets
    _decrypted_cache = decrypt_config_secrets(_config)
    return _decrypted_cache


def replace_config(new_config: dict) -> None:
    """Replace the entire config dict. Secrets are ENCRYPTED before storage."""
    global _config, _decrypted_cache
    _decrypted_cache = None  # Invalidate cache — next get_config() re-decrypts.
    from app.crypto import encrypt_config_secrets
    _config = encrypt_config_secrets(new_config)


def get_mutable_config() -> dict:
    """Return a fresh, fully-decrypted DEEP copy of the live config.

    get_config() returns a shallow copy whose `providers`/`admin` are deep-copied
    but whose other top-level sections are shared references. That is correct for
    READS, but a mutation to `providers` on it silently hits a throwaway clone and
    never reaches the live master (the self-heal enable/disable no-op).

    Use this when you intend to MUTATE the config: mutate the returned dict, then
    commit it via main._replace_runtime_config(mutated) — the single sanctioned
    runtime swap path (persist -> replace_config -> reconfigure breaker). This
    never hands out the raw master, preserving the encrypt-on-read contract.
    """
    import copy
    return copy.deepcopy(get_config())


def init_config(path: str = "config.yaml") -> None:
    """Load config from YAML, set defaults, migrate plaintext secrets.

    Raises a FileNotFoundError with first-run setup guidance when the config is
    absent. config.yaml is intentionally gitignored (it holds live credentials),
    so a fresh clone has no config until the user copies the example template.
    """
    global _config
    import os

    if not os.path.exists(path):
        example = "config.example.yaml"
        hint = (
            f"Configuration file not found: {path}\n\n"
            "BSL Router needs a config.yaml to start. It is not included in the\n"
            "repository because it holds live API credentials.\n\n"
            "To create one:\n"
            f"    copy {example} {path}        (Windows)\n"
            f"    cp {example} {path}          (macOS/Linux)\n\n"
            f"Then edit {path} and add your provider credentials.\n"
            "See docs/ARCHITECTURE.md for the full configuration reference."
        )
        if not os.path.exists(example):
            hint += (
                f"\n\nWARNING: {example} is also missing. Your checkout may be "
                "incomplete - try re-cloning the repository."
            )
        raise FileNotFoundError(hint)

    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    # An empty or comment-only YAML file parses to None, which would crash every
    # downstream .get() call with a confusing AttributeError.
    if raw is None:
        raise ValueError(
            f"Configuration file is empty: {path}\n\n"
            "Copy config.example.yaml over it to get a working starting point."
        )
    if not isinstance(raw, dict):
        raise ValueError(
            f"Configuration file must contain a YAML mapping at the top level: {path}\n"
            f"Found {type(raw).__name__} instead."
        )

    # Ensure admin config defaults exist
    if "admin" not in raw:
        raw["admin"] = {}
    raw["admin"].setdefault("password_enabled", False)
    raw["admin"].setdefault("password", "123456")

    # Update config defaults
    raw.setdefault("update", {})
    raw["update"].setdefault("github_repo", "")
    raw["update"].setdefault("check_enabled", True)
    raw["update"].setdefault("auto_check_interval", 300)

    # Antigravity integration defaults
    integration = raw.get("antigravity_integration")
    if not isinstance(integration, dict):
        integration = {}
        raw["antigravity_integration"] = integration
    integration.setdefault("enabled", False)
    integration.setdefault("mappings", {})

    # One-time migration: detect plaintext secrets and encrypt them
    from app.crypto import migrate_plaintext_config
    raw, migrated = migrate_plaintext_config(raw)
    if migrated:
        import shutil
        shutil.copy2(path, path + ".pre-encryption.bak")
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(raw, f, default_flow_style=False, sort_keys=False)
        print("[CRYPTO] Migrated plaintext secrets to encrypted format. "
              "Backup saved as config.yaml.pre-encryption.bak", flush=True)

    # Store encrypted internally
    from app.crypto import encrypt_config_secrets
    global _decrypted_cache
    _decrypted_cache = None  # Invalidate cache after config reload.
    _config = encrypt_config_secrets(raw)
