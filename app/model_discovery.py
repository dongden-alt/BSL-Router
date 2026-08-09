"""
Dynamic model discovery: probes upstream /v1/models endpoints for providers
that support OpenAI-compatible model listing.
"""
import time
import logging
from typing import Any
import httpx

logger = logging.getLogger(__name__)

# Cache: provider_id -> (timestamp, models_list)
_discovered_cache: dict[str, tuple[float, list[dict]]] = {}
_CACHE_TTL = 300  # 5 minutes

# Providers that do NOT support upstream /v1/models probing
# Kiro uses CodeWhisperer protocol, no models endpoint
_STATIC_ONLY_PROVIDERS = {"kiro", "ollama", "cursor"}


def _get_probe_url(base_url: str, provider_format: str) -> str | None:
    """Build the models probe URL for a provider based on its format."""
    base = base_url.rstrip("/")
    provider_format = (provider_format or "").lower()
    
    if provider_format in ("openai", "openai-responses", ""):
        # Most providers use /v1/models
        if "/v1" in base:
            return f"{base}/models"
        return f"{base}/v1/models"
    elif provider_format == "anthropic":
        if "/v1" in base:
            return f"{base}/models"
        return f"{base}/v1/models"
    elif provider_format == "gemini":
        return f"{base}/v1beta/models"
    elif provider_format == "kiro":
        return None  # No models endpoint
    else:
        return None


def _get_auth_headers(provider_id: str, provider_config: dict) -> dict[str, str]:
    """Extract auth headers from provider config."""
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    
    # Try first enabled connection
    connections = provider_config.get("connections", [])
    for conn in connections:
        if not conn.get("enabled", True):
            continue
        api_key = conn.get("api_key", "")
        if api_key:
            # Check token_type to determine auth scheme
            token_type = conn.get("token_type", "")
            if provider_id in ("claude", "anthropic"):
                headers["x-api-key"] = api_key
                headers["anthropic-version"] = "2023-06-01"
            else:
                headers["Authorization"] = f"Bearer {api_key}"
        break
    
    return headers


def _get_stealth_ua(provider_id: str, main_module=None) -> str | None:
    """Get stealth User-Agent for a provider."""
    # Import from main to avoid duplication
    try:
        if main_module is None:
            from app.main import _STEALTH_USER_AGENTS
        else:
            from app.main import _STEALTH_USER_AGENTS
        return _STEALTH_USER_AGENTS.get(provider_id)
    except Exception:
        return None


def _get_provider_base_url(provider_id: str, provider_config: dict, main_module=None) -> str:
    """Resolve the base URL for a provider."""
    # Check connections for base_url
    for conn in provider_config.get("connections", []):
        if conn.get("enabled", True) and conn.get("base_url"):
            return conn["base_url"]
    
    # Check provider-level base_url
    if provider_config.get("base_url"):
        return provider_config["base_url"]
    
    # Fall back to PROVIDER_DEFAULT_URLS
    try:
        if main_module is None:
            from app.main import PROVIDER_DEFAULT_URLS
        else:
            from app.main import PROVIDER_DEFAULT_URLS
        return PROVIDER_DEFAULT_URLS.get(provider_id, "")
    except Exception:
        return ""


async def discover_models(
    provider_id: str,
    provider_config: dict,
    http_client: httpx.AsyncClient,
    main_module=None,
) -> dict[str, Any]:
    """
    Probe upstream /v1/models endpoint for a provider.
    
    Returns:
        {
            "provider": provider_id,
            "discovered": True/False,
            "models": [{"id": "...", "object": "model", "owned_by": provider_id}],
            "error": "error message" or None,
            "source": "upstream" or "static" or "cache"
        }
    """
    # Check cache first
    cached = _discovered_cache.get(provider_id)
    if cached and (time.time() - cached[0]) < _CACHE_TTL:
        return {
            "provider": provider_id,
            "discovered": True,
            "models": cached[1],
            "error": None,
            "source": "cache"
        }
    
    # Static-only providers (no upstream models endpoint)
    if provider_id in _STATIC_ONLY_PROVIDERS:
        # Return models from config
        static_models = []
        for m in provider_config.get("models", []):
            if m.get("enabled", True):
                static_models.append({"id": m.get("id"), "object": "model", "owned_by": provider_id})
        return {
            "provider": provider_id,
            "discovered": False,
            "models": static_models,
            "error": None,
            "source": "static"
        }
    
    # Resolve base URL
    base_url = _get_provider_base_url(provider_id, provider_config, main_module)
    if not base_url:
        return {
            "provider": provider_id,
            "discovered": False,
            "models": [],
            "error": f"No base URL configured for provider '{provider_id}'",
            "source": "error"
        }
    
    # Build probe URL
    provider_format = provider_config.get("format", "openai")
    probe_url = _get_probe_url(base_url, provider_format)
    if not probe_url:
        return {
            "provider": provider_id,
            "discovered": False,
            "models": [],
            "error": f"Provider format '{provider_format}' does not support model discovery",
            "source": "error"
        }
    
    # Build auth headers
    headers = _get_auth_headers(provider_id, provider_config)
    stealth_ua = _get_stealth_ua(provider_id, main_module)
    if stealth_ua:
        headers["User-Agent"] = stealth_ua
    
    try:
        resp = await http_client.get(probe_url, headers=headers, timeout=15.0)
        if resp.status_code != 200:
            return {
                "provider": provider_id,
                "discovered": False,
                "models": [],
                "error": f"Upstream returned {resp.status_code}",
                "source": "error"
            }
        
        data = resp.json()
        models = []
        
        # OpenAI format: {"data": [{"id": "..."}, ...]}
        if isinstance(data, dict) and "data" in data:
            for m in data["data"]:
                model_id = m.get("id") if isinstance(m, dict) else str(m)
                if model_id:
                    models.append({"id": model_id, "object": "model", "owned_by": provider_id})
        
        # Gemini format: {"models": [{"name": "models/gemini-pro"}, ...]}
        elif isinstance(data, dict) and "models" in data:
            for m in data["models"]:
                model_name = m.get("name", "") if isinstance(m, dict) else str(m)
                # Strip "models/" prefix
                if model_name.startswith("models/"):
                    model_name = model_name[7:]
                if model_name:
                    models.append({"id": model_name, "object": "model", "owned_by": provider_id})
        
        # Cache results
        _discovered_cache[provider_id] = (time.time(), models)
        
        return {
            "provider": provider_id,
            "discovered": True,
            "models": models,
            "error": None,
            "source": "upstream"
        }
    
    except Exception as e:
        logger.warning(f"Model discovery failed for {provider_id}: {e}")
        return {
            "provider": provider_id,
            "discovered": False,
            "models": [],
            "error": str(e),
            "source": "error"
        }


def clear_discovery_cache(provider_id: str | None = None):
    """Clear the discovery cache for a provider, or all providers if None."""
    if provider_id:
        _discovered_cache.pop(provider_id, None)
    else:
        _discovered_cache.clear()
