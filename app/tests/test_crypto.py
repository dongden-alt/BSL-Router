"""Tests for the encryption layer."""
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.crypto import (
    encrypt_value, decrypt_value, is_encrypted,
    encrypt_config_secrets, decrypt_config_secrets,
    migrate_plaintext_config,
)


def _make_config():
    return {
        "providers": {
            "openai": {
                "connections": [
                    {"name": "Primary", "base_url": "https://api.openai.com/v1",
                     "api_key": "sk-openai-test", "enabled": True}
                ],
            },
            "google": {
                "connections": [
                    {"name": "OAuth", "base_url": "https://generativelanguage.googleapis.com",
                     "api_key": "ya29.test-token", "refresh_token": "1//refresh-test",
                     "token_type": "oauth", "enabled": True}
                ],
            },
        },
        "admin": {"password_enabled": True, "password": "mypassword123"},
    }


class TestEncryptDecrypt:
    def test_round_trip(self):
        plaintext = "sk-test-key-12345"
        encrypted = encrypt_value(plaintext)
        assert encrypted != plaintext
        assert is_encrypted(encrypted)
        assert decrypt_value(encrypted) == plaintext

    def test_empty_string(self):
        assert encrypt_value("") == ""
        assert decrypt_value("") == ""

    def test_plaintext_passthrough(self):
        assert decrypt_value("sk-plain-key") == "sk-plain-key"

    def test_is_encrypted(self):
        assert is_encrypted("enc:gAAAAA...")
        assert not is_encrypted("sk-plain-key")
        assert not is_encrypted("")
        assert not is_encrypted(None)

    def test_unicode(self):
        plaintext = "sk-tëst-ünïcödé-🔑"
        encrypted = encrypt_value(plaintext)
        assert decrypt_value(encrypted) == plaintext

    def test_idempotent_encrypt(self):
        """Encrypting an already-encrypted value should not double-encrypt."""
        plaintext = "sk-test"
        encrypted = encrypt_value(plaintext)
        double_encrypted = encrypt_value(encrypted)
        assert double_encrypted == encrypted


class TestConfigEncryption:
    def test_encrypt_config(self):
        config = _make_config()
        encrypted = encrypt_config_secrets(config)
        assert is_encrypted(encrypted["providers"]["openai"]["connections"][0]["api_key"])
        assert is_encrypted(encrypted["providers"]["google"]["connections"][0]["api_key"])
        assert is_encrypted(encrypted["providers"]["google"]["connections"][0]["refresh_token"])
        assert is_encrypted(encrypted["admin"]["password"])

    def test_decrypt_config(self):
        config = _make_config()
        encrypted = encrypt_config_secrets(config)
        decrypted = decrypt_config_secrets(encrypted)
        assert decrypted["providers"]["openai"]["connections"][0]["api_key"] == "sk-openai-test"
        assert decrypted["providers"]["google"]["connections"][0]["api_key"] == "ya29.test-token"
        assert decrypted["providers"]["google"]["connections"][0]["refresh_token"] == "1//refresh-test"
        assert decrypted["admin"]["password"] == "mypassword123"

    def test_encrypt_idempotent(self):
        config = _make_config()
        encrypted_once = encrypt_config_secrets(config)
        encrypted_twice = encrypt_config_secrets(encrypted_once)
        k1 = encrypted_once["providers"]["openai"]["connections"][0]["api_key"]
        k2 = encrypted_twice["providers"]["openai"]["connections"][0]["api_key"]
        assert k1 == k2

    def test_decrypt_does_not_modify_original(self):
        config = _make_config()
        encrypted = encrypt_config_secrets(config)
        original_key = encrypted["providers"]["openai"]["connections"][0]["api_key"]
        _ = decrypt_config_secrets(encrypted)
        assert encrypted["providers"]["openai"]["connections"][0]["api_key"] == original_key
        assert is_encrypted(encrypted["providers"]["openai"]["connections"][0]["api_key"])

    def test_non_sensitive_fields_untouched(self):
        config = _make_config()
        encrypted = encrypt_config_secrets(config)
        assert encrypted["providers"]["openai"]["connections"][0]["base_url"] == "https://api.openai.com/v1"
        assert encrypted["providers"]["openai"]["connections"][0]["name"] == "Primary"


class TestMigration:
    def test_migrate_plaintext(self):
        config = _make_config()
        migrated_config, migrated = migrate_plaintext_config(config)
        assert migrated is True
        assert is_encrypted(migrated_config["providers"]["openai"]["connections"][0]["api_key"])

    def test_migrate_already_encrypted(self):
        config = _make_config()
        encrypted = encrypt_config_secrets(config)
        _, migrated = migrate_plaintext_config(encrypted)
        assert migrated is False

    def test_migrate_empty_config(self):
        config, migrated = migrate_plaintext_config({})
        assert migrated is False

    def test_migrate_no_secrets(self):
        config = {"providers": {"custom": {"connections": [{"name": "test", "base_url": "https://test.com"}]}}}
        _, migrated = migrate_plaintext_config(config)
        assert migrated is False
