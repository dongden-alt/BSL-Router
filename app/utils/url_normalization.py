"""URL normalization for custom text-provider endpoints."""
from copy import deepcopy
from urllib.parse import urlsplit, urlunsplit


_TEXT_FORMATS = {"openai", "openai-responses", "anthropic", "gemini"}
_OPERATION_SUFFIXES = (
    "/v1/chat/completions",
    "/chat/completions",
    "/v1/messages",
    "/messages",
    "/v1/responses",
    "/responses",
)


def canonicalize_custom_text_base_url(value: str) -> str:
    """Return a validated custom text base URL without a terminal API suffix."""
    raw = str(value or "").strip().rstrip("/")
    parsed = urlsplit(raw)
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.netloc
        or parsed.hostname is None
        or parsed.username
        or parsed.password
    ):
        raise ValueError("Base URL must use http:// or https:// and include a host")
    if parsed.query or parsed.fragment:
        raise ValueError("Base URL must not include a query string or fragment")

    path = parsed.path.rstrip("/")
    lowered_path = path.lower()
    for suffix in _OPERATION_SUFFIXES:
        if lowered_path.endswith(suffix):
            path = path[:-len(suffix)].rstrip("/")
            break
    if path.lower().endswith("/v1"):
        path = path[:-3].rstrip("/")

    return urlunsplit((parsed.scheme.lower(), parsed.netloc, path, "", ""))


def normalize_custom_text_provider_urls(config: dict) -> dict:
    """Return a copy with supported custom text-provider base URLs canonicalized."""
    normalized = deepcopy(config)
    providers = normalized.get("providers", {})
    if not isinstance(providers, dict):
        return normalized

    for provider_id, provider in providers.items():
        if (
            not isinstance(provider, dict)
            or provider.get("type") != "custom"
            or str(provider.get("format", "")).lower() not in _TEXT_FORMATS
        ):
            continue

        base_url = provider.get("base_url")
        if isinstance(base_url, str) and base_url.strip():
            try:
                provider["base_url"] = canonicalize_custom_text_base_url(base_url)
            except ValueError as exc:
                raise ValueError(f"Provider '{provider_id}' base_url: {exc}") from exc

        connections = provider.get("connections", [])
        if not isinstance(connections, list):
            continue
        for index, connection in enumerate(connections):
            if not isinstance(connection, dict):
                continue
            base_url = connection.get("base_url")
            if not isinstance(base_url, str) or not base_url.strip():
                continue
            try:
                connection["base_url"] = canonicalize_custom_text_base_url(base_url)
            except ValueError as exc:
                connection_name = connection.get("name") or connection.get("id") or index
                raise ValueError(
                    f"Provider '{provider_id}' connection '{connection_name}' base_url: {exc}"
                ) from exc

    return normalized


def build_custom_text_upstream_url(base_url: str, provider_format: str, model_id: str = None, is_stream: bool = False) -> str:
    """Build the chat dispatch URL for a supported custom text format."""
    provider_format = str(provider_format or "").lower()
    if provider_format not in _TEXT_FORMATS:
        raise ValueError(f"Unsupported custom text provider format: {provider_format}")
    base = canonicalize_custom_text_base_url(base_url)
    
    if provider_format == "anthropic":
        suffix = "/v1/messages"
    elif provider_format == "gemini":
        action = ":streamGenerateContent" if is_stream else ":generateContent"
        if model_id:
            suffix = f"/v1beta/models/{model_id}{action}"
        else:
            # Fallback if model is not provided (should not happen in normal routing)
            suffix = f"/v1beta/models/gemini-pro{action}"
    else:
        suffix = "/v1/chat/completions"
        
    return f"{base}{suffix}"


def build_custom_models_probe_url(base_url: str) -> str:
    """Build the OpenAI-compatible models probe URL for a custom text base."""
    return f"{canonicalize_custom_text_base_url(base_url)}/v1/models"
