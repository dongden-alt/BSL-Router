import re
from pathlib import Path

from app.oauth import OAUTH_PROVIDERS

APP_JS = Path(__file__).resolve().parents[1] / "static" / "app.js"
CONSOLIDATED_PROVIDERS = {"mistral", "kiro-import"}


def test_frontend_oauth_registry_matches_backend() -> None:
    """Every OAUTH_PROVIDERS entry must have a matching FLOW_TYPE_MAP entry and KNOWN_PROVIDERS card in app.js."""
    source = APP_JS.read_text(encoding="utf-8")
    for provider, entry in OAUTH_PROVIDERS.items():
        flow = entry["flowType"]
        # FLOW_TYPE_MAP uses single quotes
        assert f"'{provider}': '{flow}'" in source, (
            f"Provider '{provider}' (flow={flow}) missing from FLOW_TYPE_MAP in app.js"
        )
        # KNOWN_PROVIDERS.oauth entry — kiro-import is consolidated into the
        # kiro tile (submode selector), so skip the separate card check for it.
        if provider not in CONSOLIDATED_PROVIDERS:
            assert f"id: '{provider}'" in source, (
                f"Provider '{provider}' missing from KNOWN_PROVIDERS.oauth in app.js"
            )


def test_provider_cards_count_enabled_models() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    assert "model.enabled !== false" in source
    assert "active model${activeModelCount === 1 ? '' : 's'}" in source


def test_no_removed_providers_linger() -> None:
    """Ensure removed providers don't linger in the registry."""
    removed = {
        "gitlab", "gemini-cli", "iflow", "cline", "clinepass",
        "kimi-coding", "kilocode", "codebuddy-cn", "kimchi", "qoder",
    }
    overlap = removed & set(OAUTH_PROVIDERS.keys())
    assert not overlap, f"Removed providers still in OAUTH_PROVIDERS: {overlap}"


def test_import_token_providers_have_importer() -> None:
    """Every import_token flow provider must have a callable importTokens."""
    for pid, entry in OAUTH_PROVIDERS.items():
        if entry["flowType"] == "import_token":
            assert "importTokens" in entry, f"Provider '{pid}' is import_token but has no importTokens"
            assert callable(entry["importTokens"]), f"Provider '{pid}' importTokens is not callable"

