"""9router-compatible OAuth login flows for BSL Router."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import errno
import hashlib
import html
import json
import os
import secrets
import sqlite3
from time import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, quote_plus, urlencode, urlparse, urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Request

from app.utils.google_cloudcode_egress import build_google_egress_client, ALLOWED_GOOGLE_HOSTS


oauth_router = APIRouter(prefix="/api/oauth", tags=["oauth"])

# OAuth exchanges are deliberately isolated from proxy transport configuration.
_oauth_client = httpx.AsyncClient(
    timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)
)

# Lazy singleton: hosts-file-bypassing egress client for Google Cloud Code calls.
# This bypasses the Windows hosts file (127.0.0.1 → mitmdump/9Router) and connects
# directly to the real Google IP via external DNS. Required for loadCodeAssist/onboardUser.
_google_egress_client: httpx.AsyncClient | None = None


def _get_google_egress_client() -> httpx.AsyncClient:
    """Return (and cache) the hosts-bypassing egress client for Google Cloud Code."""
    global _google_egress_client
    if _google_egress_client is not None:
        return _google_egress_client
    _google_egress_client = build_google_egress_client()
    return _google_egress_client


def _get_oauth_client_for_url(url: str) -> httpx.AsyncClient:
    """Return egress client for allowlisted Google hosts; plain client otherwise."""
    try:
        parsed = urlsplit(url)
        if parsed.hostname in ALLOWED_GOOGLE_HOSTS:
            return _get_google_egress_client()
    except Exception:
        pass
    return _oauth_client

_STATE_TTL_SECONDS = 600
_LOOPBACK_TIMEOUT_SECONDS = 300
_KIRO_AUTH_BASE = "https://prod.us-east-1.auth.desktop.kiro.dev"
_KIRO_REDIRECT_URI = "kiro://kiro.kiroAgent/authenticate-success"
_GOOGLE_CODE_ASSIST_URL = "https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist"
_GOOGLE_ONBOARD_USER_URL = "https://cloudcode-pa.googleapis.com/v1internal:onboardUser"
_ANTIGRAVITY_LOAD_CODE_ASSIST_USER_AGENT = "google-api-nodejs-client/9.15.1"
_ANTIGRAVITY_LOAD_CODE_ASSIST_API_CLIENT = "google-cloud-sdk vscode_cloudshelleditor/0.1"
_GROK_CLI_USER_AGENT = "grok-pager/0.2.99 grok-shell/0.2.99 (linux; x86_64)"

_oauth_states: dict[str, dict[str, Any]] = {}
_oauth_completions: dict[str, dict[str, Any]] = {}


# ──────────────────────────────────────────────────────────────────────────────
# Google (Antigravity) client credentials — resolved at call time, never inlined.
#
# These belong to Google's Antigravity desktop client, not to this project, so
# they are deliberately NOT committed. A hardcoded "GOCSPX-" literal is also
# rejected outright by GitHub push protection. Resolution order:
#   1. env: BSL_ANTIGRAVITY_CLIENT_ID / BSL_ANTIGRAVITY_CLIENT_SECRET
#   2. config.yaml: antigravity_oauth.client_id / .client_secret
#   3. "" — the Antigravity login flow is simply unavailable until configured
# ──────────────────────────────────────────────────────────────────────────────
_ANTIGRAVITY_ENV_CLIENT_ID = "BSL_ANTIGRAVITY_CLIENT_ID"
_ANTIGRAVITY_ENV_CLIENT_SECRET = "BSL_ANTIGRAVITY_CLIENT_SECRET"
_ANTIGRAVITY_CONFIG_SECTION = "antigravity_oauth"


def _antigravity_credential(env_var: str, config_key: str) -> str:
    """Resolve one Antigravity OAuth credential from env, then config.yaml."""
    from_env = os.environ.get(env_var)
    if from_env:
        return from_env.strip()
    try:
        from app.config_state import get_config

        section = get_config().get(_ANTIGRAVITY_CONFIG_SECTION) or {}
        value = section.get(config_key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    except Exception:
        # Config not loaded yet (e.g. import-time access) — fall through.
        pass
    return ""


def get_antigravity_client_id() -> str:
    return _antigravity_credential(_ANTIGRAVITY_ENV_CLIENT_ID, "client_id")


def get_antigravity_client_secret() -> str:
    return _antigravity_credential(_ANTIGRAVITY_ENV_CLIENT_SECRET, "client_secret")


class _LazyCredentialConfig(dict):
    """Provider config whose clientId/clientSecret resolve on every read.

    Subclassing dict keeps every existing ``config[\"clientId\"]`` /
    ``config[\"clientSecret\"]`` call site working unchanged, including code that
    grabs ``OAUTH_PROVIDERS[\"antigravity\"][\"config\"]`` directly and the
    ``\"clientId\" in config`` style membership checks.
    """

    _RESOLVERS = {
        "clientId": get_antigravity_client_id,
        "clientSecret": get_antigravity_client_secret,
    }

    def __getitem__(self, key: str) -> Any:
        resolver = self._RESOLVERS.get(key)
        return resolver() if resolver else super().__getitem__(key)

    def get(self, key: str, default: Any = None) -> Any:
        resolver = self._RESOLVERS.get(key)
        if resolver:
            return resolver() or default
        return super().get(key, default)

    def __contains__(self, key: object) -> bool:
        if key in self._RESOLVERS:
            return True
        return super().__contains__(key)

import collections
_loopback_sessions: dict[str, dict[str, Any]] = collections.defaultdict(dict)
_loopback_servers: dict[str, Any] = collections.defaultdict(lambda: None)
_loopback_timeout_tasks: dict[str, Any] = collections.defaultdict(lambda: None)
_loopback_app_ports: dict[str, int] = {}



def generate_pkce(verifier_bytes: int = 32) -> dict[str, str]:
    """Generate the URL-safe S256 PKCE pair used by 9router."""
    verifier = base64.urlsafe_b64encode(os.urandom(verifier_bytes)).rstrip(b"=").decode("ascii")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    return {"codeVerifier": verifier, "codeChallenge": challenge}


def generate_state() -> str:
    """Return an opaque CSRF state value."""
    return secrets.token_urlsafe(32)


def decode_jwt_payload(token: str) -> dict[str, Any]:
    """Decode an unverified JWT payload, as 9router does for account metadata."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        value = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError, UnicodeDecodeError):
        return {}


def extract_email_from_token(token: str) -> str | None:
    payload = decode_jwt_payload(token)
    for key in ("email", "preferred_username"):
        value = payload.get(key)
        if isinstance(value, str) and "@" in value:
            return value
    return None


def _scope_value(config: dict[str, Any]) -> str | None:
    scopes = config.get("scopes", config.get("scope"))
    if isinstance(scopes, list):
        return " ".join(scopes)
    return scopes if isinstance(scopes, str) and scopes else None


def _validate_redirect_uri(redirect_uri: str) -> str:
    parsed = urlparse(redirect_uri)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=400, detail="redirect_uri must be an absolute http(s) URL")
    return redirect_uri


def _remember_state(state: str, provider: str, redirect_uri: str, code_verifier: str | None) -> None:
    now = datetime.now(timezone.utc).timestamp()
    for stale_state, entry in list(_oauth_states.items()):
        if entry["expires"] <= now:
            _oauth_states.pop(stale_state, None)
    _oauth_states[state] = {
        "provider": provider,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
        "expires": now + _STATE_TTL_SECONDS,
    }


def _consume_state(provider: str, state: Any, redirect_uri: str, code_verifier: Any) -> None:
    if not isinstance(state, str) or not state:
        raise HTTPException(status_code=400, detail="state is required")
    entry = _oauth_states.pop(state, None)
    now = datetime.now(timezone.utc).timestamp()
    if entry is None or entry["expires"] <= now:
        raise HTTPException(status_code=400, detail="OAuth state is invalid or expired")
    if entry["provider"] != provider or entry["redirect_uri"] != redirect_uri:
        raise HTTPException(status_code=400, detail="OAuth state does not match this request")
    if entry["code_verifier"] is not None and entry["code_verifier"] != code_verifier:
        raise HTTPException(status_code=400, detail="PKCE code_verifier does not match the authorization request")


def _error_message(payload: Any, fallback: str) -> str:
    if isinstance(payload, dict):
        error = payload.get("error")
        description = payload.get("error_description") or payload.get("errorDescription") or payload.get("message")
        if isinstance(error, dict):
            description = error.get("message") or error.get("code") or description
        if description:
            return str(description)
        if error:
            return str(error)
    return fallback


async def _response_data(response: httpx.Response, operation: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if not response.is_success:
        detail = _error_message(payload, f"{operation} failed with HTTP {response.status_code}")
        print(f"[OAuth] {operation}: {detail}", flush=True)
        raise HTTPException(status_code=502, detail=detail)
    if not isinstance(payload, dict):
        raise HTTPException(status_code=502, detail=f"{operation} returned an invalid JSON response")
    return payload


async def _post_form(url: str, data: dict[str, str], operation: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
    request_headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        **(headers or {}),
    }
    client = _get_oauth_client_for_url(url)
    try:
        response = await client.post(url, data=data, headers=request_headers)
    except httpx.HTTPError as exc:
        print(f"[OAuth] {operation} request failed: {exc}", flush=True)
        raise HTTPException(status_code=502, detail=f"{operation} request failed") from exc
    return await _response_data(response, operation)


async def _post_json(url: str, data: dict[str, Any], operation: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
    request_headers = {"Content-Type": "application/json", "Accept": "application/json", **(headers or {})}
    client = _get_oauth_client_for_url(url)
    try:
        response = await client.post(url, json=data, headers=request_headers)
    except httpx.HTTPError as exc:
        print(f"[OAuth] {operation} request failed: {exc}", flush=True)
        raise HTTPException(status_code=502, detail=f"{operation} request failed") from exc
    return await _response_data(response, operation)


def _url_with_params(url: str, params: dict[str, str], quote_spaces: bool = False) -> str:
    return f"{url}?{urlencode(params, quote_via=quote if quote_spaces else quote_plus)}"






def _build_claude_auth_url(config: dict[str, Any], redirect_uri: str, state: str, code_challenge: str, _: Any = None) -> str:
    return _url_with_params(config["authorizeUrl"], {
        "code": "true",
        "client_id": config["clientId"],
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": " ".join(config["scopes"]),
        "code_challenge": code_challenge,
        "code_challenge_method": config["codeChallengeMethod"],
        "state": state,
    })


def _build_codex_auth_url(config: dict[str, Any], redirect_uri: str, state: str, code_challenge: str, _: Any = None) -> str:
    params = {
        "response_type": "code",
        "client_id": config["clientId"],
        "redirect_uri": redirect_uri,
        "scope": config["scope"],
        "code_challenge": code_challenge,
        "code_challenge_method": config["codeChallengeMethod"],
        **config["extraParams"],
        "state": state,
    }
    # OpenAI's hydra server requires proper percent-encoding of all parameter values.
    # Using quote_via=quote (instead of quote_plus) encodes spaces as %20, which hydra expects.
    url = _url_with_params(config["authorizeUrl"], params, quote_spaces=True)
    print(f"[OAuth] Codex auth URL: {url[:200]}...", flush=True)
    return url


def _build_google_auth_url(config: dict[str, Any], redirect_uri: str, state: str, _: Any = None, __: Any = None) -> str:
    return _url_with_params(config["authorizeUrl"], {
        "client_id": config["clientId"],
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": " ".join(config["scopes"]),
        "state": state,
        "access_type": "offline",
        "prompt": "consent",
    })








async def _exchange_claude(config: dict[str, Any], code: str, redirect_uri: str, code_verifier: str | None, state: str | None = None) -> dict[str, Any]:
    parsed_code, separator, code_state = code.partition("#")
    return await _post_json(config["tokenUrl"], {
        "code": parsed_code,
        "state": code_state if separator else state or "",
        "grant_type": "authorization_code",
        "client_id": config["clientId"],
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier or "",
    }, "Claude token exchange")


async def _exchange_standard_pkce(config: dict[str, Any], code: str, redirect_uri: str, code_verifier: str | None, _: str | None = None) -> dict[str, Any]:
    return await _post_form(config["tokenUrl"], {
        "grant_type": "authorization_code",
        "client_id": config["clientId"],
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier or "",
    }, "OAuth token exchange")


async def _exchange_google(config: dict[str, Any], code: str, redirect_uri: str, _: str | None, __: str | None = None) -> dict[str, Any]:
    return await _post_form(config["tokenUrl"], {
        "grant_type": "authorization_code",
        "client_id": config["clientId"],
        "client_secret": config["clientSecret"],
        "code": code,
        "redirect_uri": redirect_uri,
    }, "Google token exchange")








def _google_metadata() -> dict[str, int]:
    # 9router maps Windows to platform code 5.
    return {"ideType": 9, "platform": 5, "pluginType": 2}


async def _get_json(url: str, headers: dict[str, str]) -> dict[str, Any]:
    try:
        client = _get_oauth_client_for_url(url)
        response = await client.get(url, headers=headers)
        payload = response.json() if response.is_success else {}
        return payload if isinstance(payload, dict) else {}
    except (httpx.HTTPError, ValueError):
        return {}


def _google_project_id(value: Any) -> str:
    if isinstance(value, dict):
        project = value.get("cloudaicompanionProject")
        if isinstance(project, dict) and isinstance(project.get("id"), str):
            return project["id"]
        if isinstance(project, str):
            return project
    return ""


async def _onboard_antigravity(access_token: str, headers: dict[str, str], tier_id: str) -> None:
    for _ in range(10):
        try:
            client = _get_oauth_client_for_url(_GOOGLE_ONBOARD_USER_URL)
            response = await client.post(
                _GOOGLE_ONBOARD_USER_URL,
                headers=headers,
                json={"tierId": tier_id, "metadata": _google_metadata()},
            )
            if response.is_success and isinstance(response.json(), dict) and response.json().get("done") is True:
                return
        except (httpx.HTTPError, ValueError):
            return
        await asyncio.sleep(5)


async def _post_exchange_antigravity(tokens: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config or OAUTH_PROVIDERS["antigravity"]["config"]
    access_token = str(tokens.get("access_token") or "")
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "User-Agent": config["loadCodeAssistUserAgent"],
        "X-Goog-Api-Client": config["loadCodeAssistApiClient"],
        "Client-Metadata": config["loadCodeAssistClientMetadata"],
        "x-request-source": "local",
    }
    user_info = await _get_json(
        f"{config['userInfoUrl']}?alt=json",
        {"Authorization": f"Bearer {access_token}", "x-request-source": "local"},
    )
    project_id = ""
    tier_id = "legacy-tier"
    try:
        client = _get_oauth_client_for_url(config["loadCodeAssistEndpoint"])
        response = await client.post(
            config["loadCodeAssistEndpoint"], headers=headers, json={"metadata": _google_metadata()},
        )
        if response.is_success and isinstance(response.json(), dict):
            payload = response.json()
            project_id = _google_project_id(payload)
            for tier in payload.get("allowedTiers", []):
                if isinstance(tier, dict) and tier.get("isDefault") and isinstance(tier.get("id"), str):
                    tier_id = tier["id"].strip()
                    break
    except (httpx.HTTPError, ValueError) as exc:
        print(f"[OAuth] Antigravity loadCodeAssist failed: {exc}", flush=True)
    if project_id:
        asyncio.create_task(_onboard_antigravity(access_token, headers, tier_id))
    return {
        "userInfo": user_info,
        "projectId": project_id,
    }
async def _post_exchange_github(tokens: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config or OAUTH_PROVIDERS["github"]["config"]
    headers = {
        "Authorization": f"Bearer {tokens.get('access_token', '')}",
        "Accept": "application/json",
        "X-GitHub-Api-Version": config["apiVersion"],
        "User-Agent": config["userAgent"],
    }
    return {
        "copilotToken": await _get_json(config["copilotTokenUrl"], headers),
        "userInfo": await _get_json(config["userInfoUrl"], headers),
    }


async def _post_exchange_grok(tokens: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config or OAUTH_PROVIDERS["grok-cli"]["config"]
    user = await _get_json(config["userUrl"], {
        "Authorization": f"Bearer {tokens.get('access_token', '')}",
        "Accept": "application/json",
        "User-Agent": _GROK_CLI_USER_AGENT,
        "x-xai-token-auth": "xai-grok-cli",
        "x-grok-client-version": "0.2.99",
    })
    return {"user": user or None}


def _token_value(tokens: dict[str, Any], snake_case: str, camel_case: str) -> Any:
    return tokens.get(snake_case) or tokens.get(camel_case)


def _map_default(tokens: dict[str, Any], _: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "accessToken": _token_value(tokens, "access_token", "accessToken"),
        "refreshToken": _token_value(tokens, "refresh_token", "refreshToken"),
        "expiresIn": _token_value(tokens, "expires_in", "expiresIn"),
        "scope": tokens.get("scope"),
    }


def _map_codex(tokens: dict[str, Any], _: dict[str, Any] | None = None) -> dict[str, Any]:
    id_token = tokens.get("id_token")
    account = decode_jwt_payload(id_token) if isinstance(id_token, str) else {}
    email = account.get("email") or extract_email_from_token(str(tokens.get("access_token") or ""))
    data: dict[str, Any] = {
        "accessToken": tokens.get("access_token"),
        "refreshToken": tokens.get("refresh_token"),
        "idToken": id_token,
        "expiresIn": tokens.get("expires_in"),
        "lastRefreshAt": datetime.now(timezone.utc).isoformat(),
    }
    if isinstance(email, str) and email:
        data["email"] = email
    account_data = {
        key: value for key, value in {
            "chatgptAccountId": account.get("https://api.openai.com/auth", {}).get("chatgpt_account_id") if isinstance(account.get("https://api.openai.com/auth"), dict) else account.get("account_id"),
            "chatgptPlanType": account.get("https://api.openai.com/auth", {}).get("chatgpt_plan_type") if isinstance(account.get("https://api.openai.com/auth"), dict) else account.get("plan_type"),
        }.items() if value
    }
    if account_data:
        data["providerSpecificData"] = account_data
    return data


def _map_google(tokens: dict[str, Any], post: dict[str, Any] | None = None) -> dict[str, Any]:
    data = _map_default(tokens)
    if isinstance(post, dict):
        user = post.get("userInfo")
        if isinstance(user, dict) and isinstance(user.get("email"), str):
            data["email"] = user["email"]
        if isinstance(post.get("projectId"), str) and post["projectId"]:
            data["projectId"] = post["projectId"]
    return data


def _map_github(tokens: dict[str, Any], post: dict[str, Any] | None = None) -> dict[str, Any]:
    data = _map_default(tokens)
    user = post.get("userInfo") if isinstance(post, dict) and isinstance(post.get("userInfo"), dict) else {}
    copilot = post.get("copilotToken") if isinstance(post, dict) and isinstance(post.get("copilotToken"), dict) else {}
    data.update({
        "email": user.get("email"),
        "displayName": user.get("name") or user.get("login"),
        "providerSpecificData": {
            "copilotToken": copilot.get("token"),
            "copilotTokenExpiresAt": copilot.get("expires_at"),
            "githubUserId": user.get("id"),
            "githubLogin": user.get("login"),
            "githubName": user.get("name"),
            "githubEmail": user.get("email"),
        },
    })
    return data


def _map_qwen(tokens: dict[str, Any], _: dict[str, Any] | None = None) -> dict[str, Any]:
    # Qwen's token response includes an id_token (scope: "openid profile email model.completion").
    # Extract the user's email from it so the connection shows the actual account identity.
    id_token = tokens.get("id_token")
    email = None
    if isinstance(id_token, str):
        email = extract_email_from_token(id_token)
    if not email:
        email = extract_email_from_token(str(tokens.get("access_token") or ""))
    data = _map_default(tokens)
    if email:
        data["email"] = email
        data["displayName"] = email
    data["providerSpecificData"] = {"resourceUrl": tokens.get("resource_url")}
    return data


def _map_kiro(tokens: dict[str, Any], _: dict[str, Any] | None = None) -> dict[str, Any]:
    data = _map_default(tokens)
    # Try extracting email from id_token first (AWS SSO id_tokens often contain email),
    # then fall back to access_token, then to the 'sub' claim if it looks like an email.
    id_token = tokens.get("id_token")
    email = None
    if isinstance(id_token, str):
        email = extract_email_from_token(id_token)
    if not email:
        email = extract_email_from_token(str(tokens.get("access_token") or ""))
    # If still no email, try the 'name' or 'username' claim from the JWT payload
    if not email:
        for token_key in ("id_token", "access_token"):
            raw = tokens.get(token_key)
            if isinstance(raw, str):
                payload = decode_jwt_payload(raw)
                for claim_key in ("email", "preferred_username", "name", "username"):
                    val = payload.get(claim_key)
                    if isinstance(val, str) and "@" in val:
                        email = val
                        break
            if email:
                break
    display_name = email
    data.update({
        "email": email,
        "displayName": display_name,
        "providerSpecificData": {
            "profileArn": tokens.get("profile_arn"),
            "clientId": tokens.get("_clientId"),
            "clientSecret": tokens.get("_clientSecret"),
            "region": tokens.get("_region") or "us-east-1",
            "authMethod": tokens.get("_authMethod") or "builder-id",
            "startUrl": tokens.get("_startUrl"),
        },
    })
    return data


def _map_grok(tokens: dict[str, Any], post: dict[str, Any] | None = None) -> dict[str, Any]:
    user = post.get("user") if isinstance(post, dict) and isinstance(post.get("user"), dict) else {}
    email = extract_email_from_token(str(tokens.get("id_token") or "")) or extract_email_from_token(str(tokens.get("access_token") or "")) or user.get("email")
    display_name = " ".join(filter(None, [user.get("firstName"), user.get("lastName")])) or None
    return {
        "accessToken": tokens.get("access_token"),
        "refreshToken": tokens.get("refresh_token") or None,
        "expiresIn": tokens.get("expires_in"),
        "scope": tokens.get("scope"),
        "email": email,
        "displayName": display_name,
        "providerSpecificData": {
            "authMethod": "device_code",
            "idToken": tokens.get("id_token") or None,
            "email": email or None,
            "userId": user.get("userId") or user.get("principalId") or None,
            "hasGrokCodeAccess": user.get("hasGrokCodeAccess"),
            "subscriptionTier": user.get("subscriptionTier"),
        },
    }


def _map_cursor(tokens: dict[str, Any], _: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "accessToken": tokens.get("accessToken"),
        "refreshToken": None,
        "expiresIn": tokens.get("expiresIn") or 86400,
        "providerSpecificData": {"machineId": tokens.get("machineId"), "authMethod": "imported"},
    }


async def _request_github_device_code(config: dict[str, Any], _: str | None = None, __: dict[str, Any] | None = None) -> dict[str, Any]:
    return await _post_form(config["deviceCodeUrl"], {"client_id": config["clientId"], "scope": config["scopes"]}, "GitHub device authorization")


async def _request_qwen_device_code(config: dict[str, Any], code_challenge: str | None, __: dict[str, Any] | None = None) -> dict[str, Any]:
    if not code_challenge:
        raise HTTPException(status_code=400, detail="Qwen device authorization requires a PKCE challenge")
    # Qwen's device-code endpoint requires form-encoded data, not JSON.
    raw = await _post_form(config["deviceCodeUrl"], {
        "client_id": config["clientId"],
        "scope": config["scope"],
        "code_challenge": code_challenge,
        "code_challenge_method": config["codeChallengeMethod"],
    }, "Qwen device authorization")
    print(f"[OAuth] Qwen device-code raw response keys: {list(raw.keys()) if isinstance(raw, dict) else type(raw)}", flush=True)
    return raw


async def _request_grok_device_code(config: dict[str, Any], _: str | None = None, __: dict[str, Any] | None = None) -> dict[str, Any]:
    data = {"client_id": config["clientId"], "scope": config["scope"]}
    if config.get("referrer"):
        data["referrer"] = config["referrer"]
    return await _post_form(config["deviceCodeUrl"], data, "Grok CLI device authorization", {"User-Agent": _GROK_CLI_USER_AGENT})


async def _request_kiro_device_code(config: dict[str, Any], _: str | None = None, options: dict[str, Any] | None = None) -> dict[str, Any]:
    options = options or {}
    region = str(options.get("region") or "us-east-1").strip()
    if not region or not all(part.isalnum() for part in region.split("-")):
        raise HTTPException(status_code=400, detail="Invalid Kiro region")
    start_url = str(options.get("startUrl") or config["startUrl"]).strip()
    auth_method = "idc" if options.get("authMethod") == "idc" else "builder-id"
    endpoint = f"https://oidc.{region}.amazonaws.com"
    registered = await _post_json(endpoint + "/client/register", {
        "clientName": config["clientName"],
        "clientType": config["clientType"],
        "scopes": config["scopes"],
        "grantTypes": config["grantTypes"],
        "issuerUrl": config["issuerUrl"],
    }, "Kiro client registration")
    client_id = _token_value(registered, "client_id", "clientId")
    client_secret = _token_value(registered, "client_secret", "clientSecret")
    if not isinstance(client_id, str) or not isinstance(client_secret, str):
        raise HTTPException(status_code=502, detail="Kiro client registration did not return client credentials")
    device = await _post_json(endpoint + "/device_authorization", {
        "clientId": client_id,
        "clientSecret": client_secret,
        "startUrl": start_url,
    }, "Kiro device authorization")
    return {
        **device,
        "_clientId": client_id,
        "_clientSecret": client_secret,
        "_region": region,
        "_authMethod": auth_method,
        "_startUrl": start_url,
    }


async def _poll_github(config: dict[str, Any], device_code: str, _: str | None, __: dict[str, Any] | None = None) -> tuple[bool, dict[str, Any]]:
    try:
        response = await _oauth_client.post(config["tokenUrl"], data={
            "client_id": config["clientId"], "device_code": device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        }, headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
        data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        return False, {"error": "invalid_response", "error_description": str(exc)}
    return response.is_success, data if isinstance(data, dict) else {"error": "invalid_response"}


async def _poll_qwen(config: dict[str, Any], device_code: str, code_verifier: str | None, __: dict[str, Any] | None = None) -> tuple[bool, dict[str, Any]]:
    if not code_verifier:
        raise HTTPException(status_code=400, detail="Qwen polling requires codeVerifier")
    try:
        response = await _oauth_client.post(config["tokenUrl"], data={
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "client_id": config["clientId"], "device_code": device_code, "code_verifier": code_verifier,
        }, headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
        data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        return False, {"error": "invalid_response", "error_description": str(exc)}
    return response.is_success, data if isinstance(data, dict) else {"error": "invalid_response"}


async def _poll_grok(config: dict[str, Any], device_code: str, _: str | None, __: dict[str, Any] | None = None) -> tuple[bool, dict[str, Any]]:
    headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json", "User-Agent": _GROK_CLI_USER_AGENT}
    try:
        response = await _oauth_client.post(config["tokenUrl"], data={
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": device_code,
            "client_id": config["clientId"],
        }, headers=headers)
        data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        return False, {"error": "invalid_response", "error_description": str(exc)}
    if not isinstance(data, dict):
        data = {"error": "invalid_response"}
    return response.is_success or data.get("error") in {"authorization_pending", "slow_down"}, data


async def _poll_kiro(config: dict[str, Any], device_code: str, _: str | None, extra: dict[str, Any] | None = None) -> tuple[bool, dict[str, Any]]:
    extra = extra or {}
    client_id = extra.get("_clientId") or extra.get("clientId")
    client_secret = extra.get("_clientSecret") or extra.get("clientSecret")
    region = str(extra.get("_region") or extra.get("region") or "us-east-1")
    if not isinstance(client_id, str) or not isinstance(client_secret, str):
        raise HTTPException(status_code=400, detail="Kiro polling requires registered client credentials")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    try:
        response = await _oauth_client.post(f"https://oidc.{region}.amazonaws.com/token", headers=headers, json={
            "clientId": client_id, "clientSecret": client_secret, "deviceCode": device_code,
            "grantType": "urn:ietf:params:oauth:grant-type:device_code",
        })
        data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        return False, {"error": "invalid_response", "error_description": str(exc)}
    if not isinstance(data, dict):
        data = {"error": "invalid_response"}
    if data.get("accessToken"):
        return True, {
            "access_token": data.get("accessToken"), "refresh_token": data.get("refreshToken"),
            "expires_in": data.get("expiresIn"), "profile_arn": data.get("profileArn"),
            "_clientId": client_id, "_clientSecret": client_secret, "_region": region,
            "_authMethod": extra.get("_authMethod") or extra.get("authMethod"),
            "_startUrl": extra.get("_startUrl") or extra.get("startUrl") or config["startUrl"],
            # AWS SSO OIDC returns idToken when openid scope is included in the auth request.
            # This is needed by _map_kiro to extract the user's email from the JWT.
            "id_token": data.get("idToken"),
        }
    return False, {"error": data.get("error") or "authorization_pending", "error_description": data.get("error_description") or data.get("message")}


# Every entry mirrors 9router's provider object: config plus flow-specific
# buildAuthUrl/exchangeToken/mapTokens hooks, and optional post/device/loopback hooks.
OAUTH_PROVIDERS: dict[str, dict[str, Any]] = {
    "claude": {
        "config": {
            "clientId": "9d1c250a-e61b-44d9-88ed-5944d1962f5e", "authorizeUrl": "https://claude.ai/oauth/authorize",
            "tokenUrl": "https://api.anthropic.com/v1/oauth/token", "scopes": ["org:create_api_key", "user:profile", "user:inference"], "codeChallengeMethod": "S256",
        }, "flowType": "authorization_code_pkce", "buildAuthUrl": _build_claude_auth_url, "exchangeToken": _exchange_claude, "mapTokens": _map_default,
    },
    "codex": {
        "config": {
            "clientId": "app_EMoamEEZ73f0CkXaXp7hrann", "authorizeUrl": "https://auth.openai.com/oauth/authorize", "tokenUrl": "https://auth.openai.com/oauth/token",
            "scope": "openid profile email offline_access", "codeChallengeMethod": "S256", "fixedPort": 1455, "callbackPath": "/auth/callback",
            "extraParams": {"id_token_add_organizations": "true", "codex_cli_simplified_flow": "true", "originator": "codex_cli_rs"},
        }, "flowType": "authorization_code_pkce", "fixedPort": 1455, "callbackPath": "/auth/callback", "buildAuthUrl": _build_codex_auth_url, "exchangeToken": _exchange_standard_pkce, "mapTokens": _map_codex,
    },
    "antigravity": {
        # clientId/clientSecret resolve at read time from env or config.yaml —
        # see _LazyCredentialConfig. They are intentionally absent from source.
        "config": _LazyCredentialConfig({
            "authorizeUrl": "https://accounts.google.com/o/oauth2/v2/auth", "tokenUrl": "https://oauth2.googleapis.com/token", "userInfoUrl": "https://www.googleapis.com/oauth2/v1/userinfo",
            "scopes": ["https://www.googleapis.com/auth/cloud-platform", "https://www.googleapis.com/auth/userinfo.email", "https://www.googleapis.com/auth/userinfo.profile", "https://www.googleapis.com/auth/cclog", "https://www.googleapis.com/auth/experimentsandconfigs"],
            "loadCodeAssistEndpoint": _GOOGLE_CODE_ASSIST_URL, "onboardUserEndpoint": _GOOGLE_ONBOARD_USER_URL,
            "loadCodeAssistUserAgent": _ANTIGRAVITY_LOAD_CODE_ASSIST_USER_AGENT, "loadCodeAssistApiClient": _ANTIGRAVITY_LOAD_CODE_ASSIST_API_CLIENT,
            "loadCodeAssistClientMetadata": json.dumps(_google_metadata(), separators=(",", ":")),
        }), "flowType": "authorization_code", "buildAuthUrl": _build_google_auth_url, "exchangeToken": _exchange_google, "postExchange": _post_exchange_antigravity, "mapTokens": _map_google,
    },
    "github": {
        "config": {"clientId": "Iv1.b507a08c87ecfe98", "deviceCodeUrl": "https://github.com/login/device/code", "tokenUrl": "https://github.com/login/oauth/access_token", "scopes": "read:user", "userInfoUrl": "https://api.github.com/user", "copilotTokenUrl": "https://api.github.com/copilot_internal/v2/token", "apiVersion": "2022-11-28", "userAgent": "GitHubCopilotChat/0.26.7"},
        "flowType": "device_code", "requestDeviceCode": _request_github_device_code, "pollToken": _poll_github, "postExchange": _post_exchange_github, "mapTokens": _map_github,
    },
    "kiro": {
        "config": {"startUrl": "https://view.awsapps.com/start", "clientName": "AWS Toolkit for VS Code", "clientType": "public", "scopes": ["codewhisperer:completions", "codewhisperer:analysis", "codewhisperer:conversations"], "grantTypes": ["urn:ietf:params:oauth:grant-type:device_code", "refresh_token"], "issuerUrl": "https://identitycenter.amazonaws.com/ssoins-722374e8c3c8e6c6"},
        "flowType": "device_code", "requestDeviceCode": _request_kiro_device_code, "pollToken": _poll_kiro, "mapTokens": _map_kiro,
    },
    "grok-cli": {
        "config": {"clientId": "b1a00492-073a-47ea-816f-4c329264a828", "deviceCodeUrl": "https://auth.x.ai/oauth2/device/code", "tokenUrl": "https://auth.x.ai/oauth2/token", "scope": "openid profile email offline_access grok-cli:access api:access conversations:read conversations:write", "referrer": "grok-build", "userUrl": "https://cli-chat-proxy.grok.com/v1/user?include=subscription"},
        "flowType": "device_code", "requestDeviceCode": _request_grok_device_code, "pollToken": _poll_grok, "postExchange": _post_exchange_grok, "mapTokens": _map_grok,
    },
    "cursor": {
        "config": {"tokenStoragePath": r"%APPDATA%\Cursor\User\globalStorage\state.vscdb"},
        "flowType": "import_token", "importTokens": lambda cfg: _cursor_token_from_state_db(cfg), "mapTokens": _map_cursor,
    },
    "kiro-import": {
        "config": {"region": "us-east-1", "startUrl": "https://view.awsapps.com/start"},
        "flowType": "import_token", "importTokens": lambda cfg: _kiro_token_from_sso_cache(cfg), "mapTokens": _map_kiro,
    },
}


# Predefined model lists for OAuth providers — mirrors 9Router's AI_PROVIDERS
# (extracted from 9router/app/.next-cli-build/server/chunks/2573.js).
# OAuth tokens cannot fetch /v1/models, so these must be static.
OAUTH_PROVIDER_MODELS: dict[str, list[dict[str, Any]]] = {
    "claude": [
        {"id": "claude-opus-5"},
        {"id": "claude-fable-5"},
        {"id": "claude-sonnet-5"},
        {"id": "claude-haiku-4-5-20251001"},
    ],
    "codex": [
        {"id": "gpt-5.6-sol"},
        {"id": "gpt-5.6-sol-review"},
        {"id": "gpt-5.6-terra"},
        {"id": "gpt-5.6-terra-review"},
        {"id": "gpt-5.6-luna"},
        {"id": "gpt-5.6-luna-review"},
        {"id": "gpt-5.5"},
        {"id": "gpt-5.5-review"},
        {"id": "gpt-5.4"},
        {"id": "gpt-5.4-review"},
        {"id": "gpt-5.4-mini"},
        {"id": "gpt-5.4-mini-review"},
        {"id": "gpt-5.3-codex-spark"},
        {"id": "gpt-5.3-codex-spark-review"},
        {"id": "gpt-5.5-image", "kind": "image"},
        {"id": "gpt-5.4-image", "kind": "image"},
        {"id": "gpt-5.3-image", "kind": "image"},
    ],
    "antigravity": [
        {"id": "gemini-3.6-flash-high"},
        {"id": "gemini-3.6-flash-medium"},
        {"id": "gemini-3.6-flash-low"},
        {"id": "gemini-3.5-flash-high"},
        {"id": "gemini-3-flash-agent"},
        {"id": "gemini-3.5-flash-low"},
        {"id": "gemini-3.5-flash-extra-low"},
        {"id": "gemini-pro-agent"},
        {"id": "gemini-3.1-pro-low"},
        {"id": "claude-sonnet-4-6"},
        {"id": "claude-opus-4-6-thinking"},
        {"id": "gpt-oss-120b-medium"},
        {"id": "gemini-3-flash"},
        {"id": "gemini-3.1-flash-image", "kind": "image"},
    ],
    "github": [
        {"id": "gpt-5.2"},
        {"id": "gpt-5.2-codex"},
        {"id": "gpt-5.3-codex"},
        {"id": "gpt-5.4"},
        {"id": "gpt-5.4-mini"},
        {"id": "claude-haiku-4.5"},
        {"id": "claude-opus-4.5"},
        {"id": "claude-sonnet-4.5"},
        {"id": "claude-sonnet-4.6"},
        {"id": "claude-opus-4.6"},
        {"id": "claude-opus-4.7"},
        {"id": "gemini-2.5-pro"},
        {"id": "gemini-3-flash-preview"},
        {"id": "gemini-3.1-pro-preview"},
        {"id": "grok-code-fast-1"},
        {"id": "oswe-vscode-prime"},
        {"id": "goldeneye-free-auto"},
        {"id": "text-embedding-3-small", "kind": "embedding"},
        {"id": "text-embedding-3-large", "kind": "embedding"},
    ],
    "grok-cli": [
        {"id": "grok-build"},
        {"id": "grok-4.5"},
        {"id": "grok-4.5-high"},
        {"id": "grok-4.5-medium"},
        {"id": "grok-4.5-low"},
    ],
    "kiro": [
        {"id": "claude-opus-5"},
        {"id": "claude-opus-5-thinking"},
        {"id": "claude-opus-5-agentic"},
        {"id": "claude-opus-5-thinking-agentic"},
        {"id": "claude-opus-4.8"},
        {"id": "claude-opus-4.8-thinking"},
        {"id": "claude-opus-4.8-agentic"},
        {"id": "claude-opus-4.8-thinking-agentic"},
        {"id": "claude-opus-4.7"},
        {"id": "claude-opus-4.7-thinking"},
        {"id": "claude-opus-4.7-agentic"},
        {"id": "claude-opus-4.7-thinking-agentic"},
        {"id": "claude-opus-4.5"},
        {"id": "claude-opus-4.5-thinking"},
        {"id": "claude-opus-4.5-agentic"},
        {"id": "claude-opus-4.5-thinking-agentic"},
        {"id": "claude-sonnet-5"},
        {"id": "claude-sonnet-4.5"},
        {"id": "claude-haiku-4.5"},
        {"id": "deepseek-3.2"},
        {"id": "qwen3-coder-next"},
        {"id": "glm-5"},
        {"id": "MiniMax-M2.5"},
        {"id": "gpt-5.6-sol"},
        {"id": "gpt-5.6-terra"},
        {"id": "gpt-5.6-luna"},
        {"id": "claude-sonnet-5-thinking"},
        {"id": "claude-sonnet-4.5-thinking"},
        {"id": "claude-haiku-4.5-thinking"},
        {"id": "gpt-5.6-sol-thinking"},
        {"id": "gpt-5.6-terra-thinking"},
        {"id": "gpt-5.6-luna-thinking"},
        {"id": "claude-sonnet-5-agentic"},
        {"id": "claude-sonnet-4.5-agentic"},
        {"id": "claude-haiku-4.5-agentic"},
        {"id": "gpt-5.6-sol-agentic"},
        {"id": "gpt-5.6-terra-agentic"},
        {"id": "gpt-5.6-luna-agentic"},
        {"id": "claude-sonnet-5-thinking-agentic"},
        {"id": "claude-sonnet-4.5-thinking-agentic"},
        {"id": "claude-haiku-4.5-thinking-agentic"},
        {"id": "gpt-5.6-sol-thinking-agentic"},
        {"id": "gpt-5.6-terra-thinking-agentic"},
        {"id": "gpt-5.6-luna-thinking-agentic"},
    ],
    "cursor": [
        {"id": "default"},
        {"id": "claude-4.5-opus-high-thinking"},
        {"id": "claude-4.5-opus-high"},
        {"id": "claude-4.5-sonnet-thinking"},
        {"id": "claude-4.5-sonnet"},
        {"id": "claude-4.5-haiku"},
        {"id": "claude-4.5-opus"},
        {"id": "gpt-5.2-codex"},
        {"id": "claude-4.6-opus-max"},
        {"id": "claude-4.6-sonnet-medium-thinking"},
        {"id": "kimi-k2.5"},
        {"id": "gemini-3-flash-preview"},
        {"id": "gpt-5.2"},
        {"id": "gpt-5.3-codex"},
    ],
}


@oauth_router.get("/providers/models")
async def get_oauth_provider_models():
    """Return predefined model lists for all OAuth providers."""
    return OAUTH_PROVIDER_MODELS


def _provider_or_404(provider: str) -> tuple[str, dict[str, Any]]:
    normalized = provider.lower()
    entry = OAUTH_PROVIDERS.get(normalized)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Unsupported OAuth provider: {provider}")
    return normalized, entry


async def _prepare_provider_config(entry: dict[str, Any]) -> dict[str, Any]:
    config = entry["config"]
    prepare = entry.get("prepareConfig")
    return await prepare(config) if prepare else config


async def _exchange_authorization_code(provider: str, entry: dict[str, Any], code: str, redirect_uri: str, code_verifier: str | None, state: str | None = None) -> dict[str, Any]:
    config = await _prepare_provider_config(entry)
    raw_tokens = await entry["exchangeToken"](config, code, redirect_uri, code_verifier, state)
    post_exchange = entry.get("postExchange")
    post_data = await post_exchange(raw_tokens, config) if post_exchange else None
    return entry["mapTokens"](raw_tokens, post_data)


def _expires_at(tokens: dict[str, Any]) -> str | None:
    expires_at = _token_value(tokens, "expires_at", "expiresAt")
    if isinstance(expires_at, str) and expires_at:
        return expires_at
    if isinstance(expires_at, (int, float)) and expires_at > 0:
        timestamp = expires_at / 1000 if expires_at > 10_000_000_000 else expires_at
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()
    expires_in = _token_value(tokens, "expires_in", "expiresIn")
    try:
        if expires_in is not None:
            return (datetime.now(timezone.utc) + timedelta(seconds=float(expires_in))).isoformat()
    except (TypeError, ValueError, OverflowError):
        pass
    return None


def _save_connection(provider: str, flow_type: str, access_token: str, refresh_token: str | None, expires_at: str | None, email: str | None, display_name: str | None, provider_data: dict[str, Any]) -> dict[str, Any]:
    """Persist BSL's normalized connection through main.py's YAML writer."""
    from app import main as main_app

    if not isinstance(main_app.config, dict):
        raise HTTPException(status_code=503, detail="Router configuration is not loaded")
    providers = main_app.config.setdefault("providers", {})
    provider_config = providers.setdefault(provider, {"type": "oauth", "connections": []})
    connections = provider_config.setdefault("connections", [])
    if not isinstance(connections, list):
        raise HTTPException(status_code=500, detail=f"Provider {provider} has an invalid connections configuration")
    connection_id = secrets.token_urlsafe(12)
    connection = {
        "id": connection_id, "name": display_name or email or f"{provider} Account", "api_key": access_token,
        "refresh_token": refresh_token, "expires_at": expires_at, "email": email, "token_type": "oauth",
        "auth_method": flow_type, "provider_data": provider_data, "enabled": True,
        "imported_at": datetime.now(timezone.utc).isoformat(),
    }
    connections.append(connection)
    try:
        main_app._persist_config_snapshot(main_app.config)
    except OSError as exc:
        connections.pop()
        print(f"[OAuth] failed to persist {provider} connection: {exc}", flush=True)
        raise HTTPException(status_code=500, detail="Could not save OAuth connection to config.yaml") from exc
    return {"id": connection_id, "provider": provider, "email": email, "displayName": display_name or email or f"{provider} Account"}


async def _complete_connection(provider: str, entry: dict[str, Any], tokens: dict[str, Any]) -> dict[str, Any]:
    access_token = _token_value(tokens, "access_token", "accessToken")
    if not isinstance(access_token, str) or not access_token:
        raise HTTPException(status_code=502, detail=f"{provider} did not return an access token")
    refresh_token = _token_value(tokens, "refresh_token", "refreshToken")
    id_token = _token_value(tokens, "id_token", "idToken")
    email = tokens.get("email") or (extract_email_from_token(id_token) if isinstance(id_token, str) else None)
    display_name = tokens.get("displayName") or email
    provider_data = dict(tokens.get("providerSpecificData") or {}) if isinstance(tokens.get("providerSpecificData"), dict) else {}
    if isinstance(id_token, str):
        provider_data.setdefault("id_token", id_token)
    if isinstance(tokens.get("projectId"), str) and tokens["projectId"]:
        provider_data["project_id"] = tokens["projectId"]
    if isinstance(tokens.get("apiKey"), str) and tokens["apiKey"]:
        provider_data["api_key"] = tokens["apiKey"]
    return _save_connection(
        provider, entry["flowType"], access_token,
        refresh_token if isinstance(refresh_token, str) else None, _expires_at(tokens),
        email if isinstance(email, str) else None, display_name if isinstance(display_name, str) else None, provider_data,
    )


def _loopback_callback_page(success: bool, message: str) -> str:
    title = "Authentication Successful" if success else "Authentication Failed"
    color = "#22c55e" if success else "#ef4444"
    icon = "&#10003;" if success else "&#10007;"
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{title}</title>
<style>body{{font-family:system-ui;display:flex;justify-content:center;align-items:center;height:100vh;margin:0;background:#f5f5f5}}.c{{text-align:center;padding:2rem;background:#fff;border-radius:8px;box-shadow:0 2px 10px rgba(0,0,0,.1)}}.i{{color:{color};font-size:3rem}}h1{{margin:1rem 0}}p{{color:#666}}</style>
</head><body><div class="c"><div class="i">{icon}</div><h1>{title}</h1><p>{html.escape(message)}</p><p>Closing in <span id="cd">3</span>s...</p>
<script>let n=3;const c=document.getElementById("cd");const t=setInterval(()=>{{n--;c.textContent=n;if(n<=0){{clearInterval(t);window.close();}}}},1000);</script>
</div></body></html>"""


async def _send_loopback_response(writer: asyncio.StreamWriter, status: int, success: bool, message: str) -> None:
    body = _loopback_callback_page(success, message).encode("utf-8")
    reason = "OK" if status == 200 else "Bad Request" if status == 400 else "Not Found"
    writer.write(f"HTTP/1.1 {status} {reason}\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode("ascii") + body)
    await writer.drain()


async def _send_loopback_redirect(writer: asyncio.StreamWriter, location: str) -> None:
    writer.write(f"HTTP/1.1 302 Found\r\nLocation: {location}\r\nConnection: close\r\n\r\n".encode("ascii"))
    await writer.drain()


async def _close_loopback_listener(provider: str) -> None:
    timeout_task = _loopback_timeout_tasks.get(provider)
    _loopback_timeout_tasks[provider] = None
    if timeout_task is not None and timeout_task is not asyncio.current_task():
        timeout_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await timeout_task
    server = _loopback_servers.get(provider)
    _loopback_servers[provider] = None
    if server is not None:
        server.close()
        await server.wait_closed()


async def _expire_loopback_session(provider: str, state: str) -> None:
    try:
        await asyncio.sleep(_LOOPBACK_TIMEOUT_SECONDS)
        session = _loopback_sessions[provider].get(state)
        if session is not None and session["status"] in {"pending", "manual", "completing"}:
            session.update(status="error", error=f"{provider} OAuth callback timed out. Start the sign-in flow again.")
            await _close_loopback_listener(provider)
    except asyncio.CancelledError:
        raise


async def _finish_loopback_callback(provider: str, state: str, code: str | None, provider_error: str | None, error_description: str | None) -> tuple[dict[str, Any] | None, str | None]:
    session = _loopback_sessions[provider].get(state)
    if session is None or session["status"] not in {"pending", "manual"}:
        return None, f"This {provider} OAuth session is no longer active."
    if provider_error:
        message = error_description or provider_error
        session.update(status="error", error=message)
        return None, message
    if not code:
        message = f"No authorization code received from {provider}."
        session.update(status="error", error=message)
        return None, message
    session["status"] = "completing"
    try:
        entry = OAUTH_PROVIDERS[provider]
        tokens = await _exchange_authorization_code(provider, entry, code, session["redirectUri"], session["codeVerifier"])
        connection = await _complete_connection(provider, entry, tokens)
    except HTTPException as exc:
        message = str(exc.detail)
        session.update(status="error", error=message)
        return None, message
    except Exception as exc:
        message = f"{provider} OAuth completion failed"
        print(f"[OAuth] {message}: {exc}", flush=True)
        session.update(status="error", error=message)
        return None, message
    session.update(status="done", connection=connection)
    return connection, None


def _make_loopback_handler(provider: str):
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = (await asyncio.wait_for(reader.readline(), timeout=10)).decode("latin-1").strip()
            parts = request_line.split(" ", 2)
            if len(parts) < 2 or parts[0] != "GET":
                await _send_loopback_response(writer, 400, False, "Invalid OAuth callback request.")
                return
            callback = urlsplit(parts[1])
            if callback.path not in {"/callback", "/auth/callback"}:
                await _send_loopback_response(writer, 404, False, "Not found.")
                return
            query = parse_qs(callback.query, keep_blank_values=True)
            state = query.get("state", [None])[0]
            session = _loopback_sessions[provider].get(state) if isinstance(state, str) else None
            if session is None:
                search = f"?{callback.query}" if callback.query else ""
                await _send_loopback_redirect(writer, f"http://localhost:{_loopback_app_ports[provider]}/callback{search}")
                return
            connection, error = await _finish_loopback_callback(provider, state, query.get("code", [None])[0], query.get("error", [None])[0], query.get("error_description", [None])[0])
            await _send_loopback_response(writer, 200, connection is not None, "You can close this window." if connection else error or f"{provider} OAuth failed.")
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, UnicodeDecodeError, ValueError):
            with contextlib.suppress(ConnectionError):
                await _send_loopback_response(writer, 400, False, "Invalid OAuth callback request.")
        finally:
            with contextlib.suppress(Exception):
                await _close_loopback_listener(provider)
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()
    return handler


async def _start_loopback_listener(provider: str, app_port: int, state: str, code_verifier: str, redirect_uri: str) -> dict[str, Any]:
    entry = OAUTH_PROVIDERS[provider]
    _loopback_app_ports[provider] = app_port
    _loopback_sessions[provider][state] = {"codeVerifier": code_verifier, "redirectUri": redirect_uri, "status": "pending", "createdAt": datetime.now(timezone.utc).timestamp()}
    if _loopback_timeout_tasks[provider] is None or _loopback_timeout_tasks[provider].done():
        _loopback_timeout_tasks[provider] = asyncio.create_task(_expire_loopback_session(provider, state))
    if _loopback_servers[provider] is not None:
        return {"success": True, "serverSide": True}
    try:
        _loopback_servers[provider] = await asyncio.start_server(_make_loopback_handler(provider), "127.0.0.1", entry["fixedPort"])
    except OSError as exc:
        _loopback_servers[provider] = None
        if exc.errno in {errno.EADDRINUSE, 10048}:
            _loopback_sessions[provider][state].update(status="manual")
            return {"success": False, "serverSide": False, "reason": "port_busy"}
        _loopback_sessions[provider][state].update(status="manual", error=str(exc))
        return {"success": False, "serverSide": False, "reason": str(exc)}
    return {"success": True, "serverSide": True}


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Request body must be valid JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object")
    return body


def _required_string(body: dict[str, Any], field: str) -> str:
    value = body.get(field)
    if not isinstance(value, str) or not value:
        raise HTTPException(status_code=400, detail=f"{field} is required")
    return value


@oauth_router.get("/{provider}/authorize")
async def authorize(provider: str, redirect_uri: str = "http://localhost:6969/callback"):
    """Build the exact provider-specific authorization URL from the registry."""
    provider, entry = _provider_or_404(provider)
    if entry["flowType"] in {"device_code", "import_token"}:
        raise HTTPException(status_code=400, detail="This provider does not use authorization-code OAuth")
    config = await _prepare_provider_config(entry)
    fixed_port = entry.get("fixedPort")
    if fixed_port:
        callback_path = entry.get("callbackPath", "/callback")
        redirect_uri = config.get("redirectUri") or f"http://localhost:{fixed_port}{callback_path}"
    else:
        redirect_uri = _validate_redirect_uri(redirect_uri)
    state = generate_state()
    pkce = generate_pkce(entry.get("pkceVerifierBytes", config.get("pkceVerifierBytes", 32))) if entry["flowType"] == "authorization_code_pkce" else None
    if entry.get("stateRequired", True) and not fixed_port:
        _remember_state(state, provider, redirect_uri, pkce["codeVerifier"] if pkce else None)
    return {
        "authUrl": entry["buildAuthUrl"](config, redirect_uri, state, pkce["codeChallenge"] if pkce else None),
        "state": state, "codeVerifier": pkce["codeVerifier"] if pkce else None,
        "codeChallenge": pkce["codeChallenge"] if pkce else None, "redirectUri": redirect_uri,
        "flowType": entry["flowType"], "fixedPort": fixed_port, "callbackPath": entry.get("callbackPath", "/callback"),
    }


@oauth_router.get("/{provider}/start-proxy")
async def start_proxy(provider: str, app_port: int, state: str, code_verifier: str, redirect_uri: str):
    """Start the provider's 9router-style fixed-port loopback listener."""
    provider, entry = _provider_or_404(provider)
    if not entry.get("fixedPort"):
        raise HTTPException(status_code=400, detail="Loopback proxy is only supported for fixed-port OAuth providers")
    return await _start_loopback_listener(provider, app_port, state, code_verifier, redirect_uri)


@oauth_router.get("/{provider}/poll-status")
async def poll_status(provider: str, state: str):
    provider, entry = _provider_or_404(provider)
    if not entry.get("fixedPort"):
        comp = _oauth_completions.get(state)
        if comp is None:
            if state in _oauth_states:
                return {"status": "pending"}
            return {"status": "unknown"}
        response = dict(comp)
        # Pop it if done or error so we release memory
        if comp.get("status") in {"done", "error"}:
            _oauth_completions.pop(state, None)
        return response
    session = _loopback_sessions[provider].get(state)
    if session is None:
        return {"status": "unknown"}
    if session["status"] in {"done", "error"}:
        response = dict(session)
        _loopback_sessions[provider].pop(state, None)
        return response
    return {"status": session["status"]}


@oauth_router.post("/{provider}/stop-proxy")
async def stop_proxy(provider: str):
    provider, entry = _provider_or_404(provider)
    if not entry.get("fixedPort"):
        raise HTTPException(status_code=400, detail="Loopback proxy is only supported for fixed-port OAuth providers")
    _loopback_sessions[provider].clear()
    await _close_loopback_listener(provider)
    return {"success": True}


@oauth_router.post("/{provider}/manual-code")
async def manual_code(provider: str, request: Request):
    provider, entry = _provider_or_404(provider)
    if not entry.get("fixedPort"):
        raise HTTPException(status_code=400, detail="Manual code is only supported for fixed-port OAuth providers")
    body = await _json_body(request)
    code, state = _required_string(body, "code"), _required_string(body, "state")
    if state not in _loopback_sessions[provider]:
        raise HTTPException(status_code=400, detail=f"{provider} OAuth session not found; restart the login flow and paste the code again")
    connection, error = await _finish_loopback_callback(provider, state, code, None, None)
    _loopback_sessions[provider].pop(state, None)
    await _close_loopback_listener(provider)
    if connection is None:
        raise HTTPException(status_code=400, detail=error or f"{provider} OAuth could not be completed")
    return {"success": True, "connection": connection}


def _codex_jwt_connection(code: str) -> tuple[str | None, dict[str, Any]]:
    claims = decode_jwt_payload(code)
    account = claims.get("https://api.openai.com/auth") if isinstance(claims.get("https://api.openai.com/auth"), dict) else {}
    data = {"authMethod": "access_token"}
    account_id = account.get("chatgpt_account_id") or claims.get("account_id")
    plan_type = account.get("chatgpt_plan_type") or claims.get("plan_type")
    if account_id:
        data["chatgptAccountId"] = account_id
    if plan_type:
        data["chatgptPlanType"] = plan_type
    email = claims.get("email")
    return email if isinstance(email, str) else None, data


@oauth_router.post("/{provider}/exchange")
async def exchange(provider: str, request: Request):
    """Exchange an authorization code, including Codex simplified-flow JWT tokens."""
    provider, entry = _provider_or_404(provider)
    if entry["flowType"] in {"device_code", "import_token"}:
        raise HTTPException(status_code=400, detail="This provider does not use authorization-code exchange")
    body = await _json_body(request)
    code = _required_string(body, "code")
    if provider == "codex" and code.startswith("eyJ") and "." in code:
        email, provider_data = _codex_jwt_connection(code)
        connection = _save_connection("codex", "access_token", code, None, None, email, email, provider_data)
        return {"success": True, "connection": connection}
    config = await _prepare_provider_config(entry)
    if entry.get("fixedPort"):
        redirect_uri = config.get("redirectUri") or f"http://localhost:{entry['fixedPort']}{entry.get('callbackPath', '/callback')}"
    else:
        redirect_uri = _validate_redirect_uri(body.get("redirectUri", "http://localhost:6969/callback"))
    code_verifier = body.get("codeVerifier")
    if code_verifier is not None and not isinstance(code_verifier, str):
        raise HTTPException(status_code=400, detail="codeVerifier must be a string")
    if entry.get("stateRequired", True) and not entry.get("fixedPort"):
        _consume_state(provider, body.get("state"), redirect_uri, code_verifier)
    tokens = await _exchange_authorization_code(provider, entry, code, redirect_uri, code_verifier, body.get("state"))
    connection = await _complete_connection(provider, entry, tokens)
    return {"success": True, "connection": connection}


def _cursor_db_value(connection: sqlite3.Connection, key: str) -> Any:
    try:
        row = connection.execute("SELECT value FROM Item WHERE key = ?", (key,)).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    value = row[0]
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="ignore")
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _kiro_token_from_sso_cache(config: dict[str, Any]) -> dict[str, Any]:
    """Import Kiro auth from the local AWS SSO token cache (kirogo-style).

    Reads ~/.aws/sso/cache/kiro-auth-token.json (written by the Kiro app after a
    Builder-ID / social (GitHub/Google) / Identity-Center login) and any sibling
    *.json registration blob holding clientId/clientSecret (needed for refresh).
    Returns keys matching _map_kiro's expectations. Fail-open: raises 404/502 with
    a clear message; never crashes the route on a missing optional field.
    """
    cache_dir = Path.home() / ".aws" / "sso" / "cache"
    token_path = cache_dir / "kiro-auth-token.json"
    if not token_path.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"Kiro auth token not found at {token_path}. Log in with the Kiro app first (Builder ID, social, or Identity Center).",
        )
    try:
        data = json.loads(token_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=502, detail=f"Could not read Kiro auth token cache: {exc}") from exc
    access_token = data.get("accessToken")
    if not isinstance(access_token, str) or not access_token:
        raise HTTPException(status_code=404, detail="Kiro auth token cache has no accessToken")

    # expires_in from ISO8601 expiresAt (e.g. 2026-07-22T07:19:04.440Z)
    expires_in = 3600
    expires_at_raw = data.get("expiresAt")
    if isinstance(expires_at_raw, str):
        try:
            exp_dt = datetime.fromisoformat(expires_at_raw.replace("Z", "+00:00"))
            expires_in = max(1, int(exp_dt.timestamp() - datetime.now(timezone.utc).timestamp()))
        except ValueError:
            pass

    # Scan sibling registration blobs for clientId/clientSecret (needed for refresh).
    client_id = client_secret = None
    try:
        for sibling in cache_dir.glob("*.json"):
            if sibling.name == token_path.name:
                continue
            try:
                blob = json.loads(sibling.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(blob.get("clientId"), str) and isinstance(blob.get("clientSecret"), str):
                client_id, client_secret = blob["clientId"], blob["clientSecret"]
                break
    except OSError:
        pass

    region = str(config.get("region") or "us-east-1")
    start_url = str(config.get("startUrl") or "https://view.awsapps.com/start")
    auth_method = data.get("authMethod") or data.get("provider") or "social"
    result = {
        "access_token": access_token,
        "refresh_token": data.get("refreshToken"),
        "expires_in": expires_in,
        "profile_arn": data.get("profileArn"),
        "_clientId": client_id,
        "_clientSecret": client_secret,
        "_region": region,
        "_authMethod": auth_method if isinstance(auth_method, str) else "social",
        "_startUrl": start_url,
    }
    if not client_id or not client_secret:
        # Import succeeds but refresh will not be possible without the registration blob.
        print("[OAuth] Kiro import: no clientId/clientSecret registration blob found in SSO cache; token refresh unavailable", flush=True)
    return result


def _cursor_token_from_state_db(config: dict[str, Any]) -> dict[str, Any]:
    db_path = Path(os.path.expandvars(config["tokenStoragePath"])).expanduser()
    if not db_path.is_file():
        raise HTTPException(
            status_code=404,
            detail=(
                f"Cursor state database was not found at {db_path}. "
                "Make sure Cursor IDE is installed and you have logged in at least once. "
                "Download from https://cursor.com"
            ),
        )
    try:
        connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
        with connection:
            access_token = _cursor_db_value(connection, "cursorAuth/accessToken")
            # 9Router uses 'storage.serviceMachineId' key for the machine ID.
            machine_id = _cursor_db_value(connection, "storage.serviceMachineId")
            if not machine_id:
                # Fallback to older key used in some Cursor versions
                machine_id = _cursor_db_value(connection, "cursorAuth/machineId")
    except sqlite3.Error as exc:
        raise HTTPException(status_code=502, detail=f"Could not read Cursor state database: {exc}") from exc
    if isinstance(access_token, dict):
        machine_id = access_token.get("machineId") or machine_id
        access_token = access_token.get("accessToken") or access_token.get("token")
    if not isinstance(access_token, str) or not access_token:
        raise HTTPException(status_code=404, detail="Cursor access token was not found in its state database")
    expires_in = 86400
    claims = decode_jwt_payload(access_token)
    if isinstance(claims.get("exp"), (int, float)):
        expires_in = max(1, int(claims["exp"] - datetime.now(timezone.utc).timestamp()))
    return {"accessToken": access_token, "machineId": machine_id if isinstance(machine_id, str) else None, "expiresIn": expires_in}


@oauth_router.post("/{provider}/import")
async def import_token(provider: str):
    provider, entry = _provider_or_404(provider)
    if entry["flowType"] != "import_token":
        raise HTTPException(status_code=400, detail="This provider does not support native token import")
    importer = entry.get("importTokens")
    if importer is None:
        raise HTTPException(status_code=400, detail=f"Provider '{provider}' has no token importer configured")
    raw_tokens = importer(entry["config"])
    tokens = entry["mapTokens"](raw_tokens)
    connection = await _complete_connection(provider, entry, tokens)
    return {"success": True, "connection": connection}


@oauth_router.get("/{provider}/device-code")
async def device_code(provider: str, region: str | None = None, start_url: str | None = None, auth_method: str | None = None):
    provider, entry = _provider_or_404(provider)
    if entry["flowType"] != "device_code":
        raise HTTPException(status_code=400, detail="This provider does not use device code")
    pkce = generate_pkce() if provider == "qwen" else None
    options = {key: value for key, value in {"region": region, "startUrl": start_url, "authMethod": auth_method}.items() if value}
    raw = await entry["requestDeviceCode"](entry["config"], pkce["codeChallenge"] if pkce else None, options)
    device_value = _token_value(raw, "device_code", "deviceCode")
    if not device_value:
        # Some providers (e.g. Qwen) may return the device code under alternative field names.
        device_value = raw.get("code") or raw.get("deviceId") or raw.get("device_id")
    if not isinstance(device_value, str) or not device_value:
        print(f"[OAuth] {provider} device-code response (full): {raw}", flush=True)
        raise HTTPException(status_code=502, detail=f"{provider} did not return a device code")
    extra_data = {key: value for key, value in raw.items() if key.startswith("_")}
    return {
        "device_code": device_value, "user_code": _token_value(raw, "user_code", "userCode"),
        "verification_uri": _token_value(raw, "verification_uri", "verificationUri"),
        "verification_uri_complete": _token_value(raw, "verification_uri_complete", "verificationUriComplete"),
        "expires_in": _token_value(raw, "expires_in", "expiresIn"), "interval": raw.get("interval"),
        "codeVerifier": pkce["codeVerifier"] if pkce else None, "extraData": extra_data,
    }


@oauth_router.post("/{provider}/poll")
async def poll(provider: str, request: Request):
    provider, entry = _provider_or_404(provider)
    if entry["flowType"] != "device_code":
        raise HTTPException(status_code=400, detail="This provider does not use device code")
    body = await _json_body(request)
    device_code_value = _required_string(body, "deviceCode")
    code_verifier = body.get("codeVerifier")
    extra_data = body.get("extraData") or {}
    if code_verifier is not None and not isinstance(code_verifier, str):
        raise HTTPException(status_code=400, detail="codeVerifier must be a string")
    if not isinstance(extra_data, dict):
        raise HTTPException(status_code=400, detail="extraData must be an object")
    ok, raw_tokens = await entry["pollToken"](entry["config"], device_code_value, code_verifier, extra_data)
    error = raw_tokens.get("error") if isinstance(raw_tokens, dict) else "invalid_response"
    if error in {"authorization_pending", "slow_down"}:
        return {"pending": True}
    if not ok or not isinstance(raw_tokens.get("access_token"), str):
        return {"error": str(error or "device_code_exchange_failed"), "errorDescription": _error_message(raw_tokens, f"{provider} device token exchange failed")}
    post_exchange = entry.get("postExchange")
    post_data = await post_exchange(raw_tokens, entry["config"]) if post_exchange else None
    tokens = entry["mapTokens"](raw_tokens, post_data)
    connection = await _complete_connection(provider, entry, tokens)
    return {"success": True, "connection": connection}


async def _kiro_refresh_token(refresh_token: str, provider_data: dict[str, Any] | None = None) -> dict[str, Any]:
    provider_data = provider_data or {}
    client_id, client_secret = provider_data.get("clientId"), provider_data.get("clientSecret")
    region = str(provider_data.get("region") or "us-east-1")
    if client_id and client_secret:
        url = f"https://oidc.{region}.amazonaws.com/token"
        payload = {"clientId": client_id, "clientSecret": client_secret, "refreshToken": refresh_token, "grantType": "refresh_token"}
    else:
        url = "https://prod.us-east-1.auth.desktop.kiro.dev/refreshToken"
        payload = {"refreshToken": refresh_token}
    raw = await _post_json(url, payload, "Kiro token refresh")
    access_token = _token_value(raw, "access_token", "accessToken")
    if not isinstance(access_token, str) or not access_token:
        raise HTTPException(status_code=502, detail="Kiro token refresh returned no access token")
    return {
        "access_token": access_token,
        "refresh_token": _token_value(raw, "refresh_token", "refreshToken") or refresh_token,
        "expires_in": _token_value(raw, "expires_in", "expiresIn") or 3600,
        "profile_arn": _token_value(raw, "profile_arn", "profileArn"),
        "_clientId": client_id, "_clientSecret": client_secret, "_region": region,
        "_authMethod": provider_data.get("authMethod") or "imported",
        "_startUrl": provider_data.get("startUrl"),
    }


async def _save_kiro_tokens(raw: dict[str, Any], auth_method: str, source: str) -> dict[str, Any]:
    raw["_authMethod"] = auth_method
    mapped = _map_kiro(raw)
    mapped["providerSpecificData"]["provider"] = source
    return await _complete_connection("kiro", OAUTH_PROVIDERS["kiro"], mapped)


@oauth_router.get("/kiro/social-authorize")
async def kiro_social_authorize(provider: str = "google", request: Request = None):
    if provider not in ("google", "github"):
        raise HTTPException(status_code=400, detail="Provider must be 'google' or 'github'")
    pkce = generate_pkce(32)
    state = generate_state()
    idp = "Google" if provider == "google" else "Github"
    auth_url = (
        f"{_KIRO_AUTH_BASE}/login"
        f"?idp={idp}"
        f"&redirect_uri={quote(_KIRO_REDIRECT_URI)}"
        f"&code_challenge={pkce['codeChallenge']}"
        f"&code_challenge_method=S256"
        f"&state={state}"
        f"&prompt=select_account"
    )
    key = f"kiro_social_{state}"
    _oauth_states[key] = {
        "provider": provider,
        "codeVerifier": pkce["codeVerifier"],
        "state": state,
        "expires_at": time() + 600,
    }
    return {"authUrl": auth_url, "codeVerifier": pkce["codeVerifier"], "state": state}


@oauth_router.post("/kiro/social-exchange")
async def kiro_social_exchange(body: dict):
    code = (body.get("code") or "").strip()
    code_verifier = (body.get("codeVerifier") or "").strip()
    provider = body.get("provider", "google")
    if not code or not code_verifier:
        raise HTTPException(status_code=400, detail="Missing code or codeVerifier")
    resp = await _oauth_client.post(
        f"{_KIRO_AUTH_BASE}/oauth/token",
        json={
            "code": code,
            "code_verifier": code_verifier,
            "redirect_uri": _KIRO_REDIRECT_URI,
        },
    )
    if not resp.is_success:
        raise HTTPException(status_code=502, detail=f"Kiro token exchange failed: {resp.text}")
    data = resp.json()
    raw = {
        "access_token": _token_value(data, "access_token", "accessToken"),
        "refresh_token": _token_value(data, "refresh_token", "refreshToken"),
        "expires_in": _token_value(data, "expires_in", "expiresIn") or 3600,
        "profile_arn": _token_value(data, "profile_arn", "profileArn"),
        "_region": "us-east-1",
    }
    raw["_authMethod"] = "oauth"
    mapped = _map_kiro(raw)
    mapped["providerSpecificData"]["provider"] = f"AWS Builder ID ({provider.capitalize()})"
    return {"success": True, "connection": await _complete_connection("kiro", OAUTH_PROVIDERS["kiro"], mapped)}


@oauth_router.post("/kiro/api-key")
async def kiro_api_key(request: Request):
    body = await _json_body(request)
    api_key = _required_string(body, "apiKey").strip()
    region = str(body.get("region") or "us-east-1").strip()
    if not all(part.isalnum() for part in region.split("-")):
        raise HTTPException(status_code=400, detail="Invalid region")
    response = await _oauth_client.post(
        f"https://codewhisperer.{region}.amazonaws.com",
        headers={"Content-Type": "application/x-amz-json-1.0", "x-amz-target": "AmazonCodeWhispererService.ListAvailableProfiles", "Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        json={"maxResults": 10},
    )
    if not response.is_success:
        raise HTTPException(status_code=400, detail="API key validation failed")
    data = response.json()
    profiles = data.get("profiles") if isinstance(data, dict) else []
    profile = next((p for p in profiles if isinstance(p, dict) and str(p.get("arn") or p.get("profileArn") or "").split(":")[3:4] == [region]), profiles[0] if profiles else {})
    raw = {"access_token": api_key, "expires_in": 31536000, "profile_arn": profile.get("arn") or profile.get("profileArn"), "_region": region}
    return {"success": True, "connection": await _save_kiro_tokens(raw, "api_key", "API Key")}


@oauth_router.post("/kiro/import")
async def kiro_import_refresh_token(request: Request):
    body = await _json_body(request)
    refresh_token = _required_string(body, "refreshToken").strip()
    if not refresh_token.startswith("aorAAAAAG"):
        raise HTTPException(status_code=400, detail="Invalid token format. Token should start with aorAAAAAG...")
    raw = await _kiro_refresh_token(refresh_token)
    return {"success": True, "connection": await _save_kiro_tokens(raw, "imported", "Imported")}


@oauth_router.get("/kiro/auto-import")
async def kiro_auto_import():
    cache_dir = Path.home() / ".aws" / "sso" / "cache"
    if not cache_dir.is_dir():
        return {"found": False, "error": "AWS SSO cache not found. Please login to Kiro IDE first."}
    paths = [cache_dir / "kiro-auth-token.json", *cache_dir.glob("*.json")]
    seen: set[Path] = set()
    for path in paths:
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        token = data.get("refreshToken") if isinstance(data, dict) else None
        if isinstance(token, str) and token.startswith("aorAAAAAG"):
            return {"found": True, "refreshToken": token, "source": path.name}
    return {"found": False, "error": "Kiro token not found in AWS SSO cache. Please login to Kiro IDE first."}


@oauth_router.post("/kiro/cliproxy-import")
async def kiro_cliproxy_import(request: Request):
    body = await _json_body(request)
    value = body.get("json")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="CLIProxyAPI value must be valid JSON") from None
    if not isinstance(value, dict):
        raise HTTPException(status_code=400, detail="CLIProxyAPI JSON is required")
    refresh_token = _token_value(value, "refresh_token", "refreshToken")
    access_token = _token_value(value, "access_token", "accessToken")
    provider_data = {
        "clientId": _token_value(value, "client_id", "clientId"),
        "clientSecret": _token_value(value, "client_secret", "clientSecret"),
        "region": value.get("region") or "us-east-1",
        "startUrl": _token_value(value, "start_url", "startUrl"),
        "authMethod": "cliproxyapi",
    }
    if isinstance(refresh_token, str) and refresh_token:
        raw = await _kiro_refresh_token(refresh_token, provider_data)
    elif isinstance(access_token, str) and access_token:
        raw = {
            "access_token": access_token, "refresh_token": None,
            "expires_in": _token_value(value, "expires_in", "expiresIn") or 3600,
            "profile_arn": _token_value(value, "profile_arn", "profileArn"),
            "_clientId": provider_data["clientId"], "_clientSecret": provider_data["clientSecret"],
            "_region": provider_data["region"], "_startUrl": provider_data["startUrl"],
        }
    else:
        raise HTTPException(status_code=400, detail="CLIProxyAPI JSON must contain access_token or refresh_token")
    return {"success": True, "connection": await _save_kiro_tokens(raw, "cliproxyapi", "CLIProxyAPI")}


# ──────────────────────────────────────────────────────────────────────────────
# Token refresh - ALL OAuth providers
# ──────────────────────────────────────────────────────────────────────────────
# Root cause of the one-week recurring 401 loop: only _kiro_refresh_token existed.
# Every other OAuth provider's access_token silently expired (~1h) with no refresh
# path, causing upstream 401s that BSL had no recovery for.
#
# Each refresh function returns a normalized dict:
#   {"access_token": str, "refresh_token": str|None, "expires_in": int}
#
# All use _post_form / _post_json which route through the egress client for
# Google hosts via _get_oauth_client_for_url + ALLOWED_GOOGLE_HOSTS.
# ──────────────────────────────────────────────────────────────────────────────


async def _google_refresh_token(
    refresh_token: str, config: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Refresh a Google (Antigravity) OAuth2 access token."""
    config = config or OAUTH_PROVIDERS["antigravity"]["config"]
    raw = await _post_form(
        config["tokenUrl"],
        {
            "grant_type": "refresh_token",
            "client_id": config["clientId"],
            "client_secret": config["clientSecret"],
            "refresh_token": refresh_token,
        },
        "Google token refresh",
    )
    access_token = _token_value(raw, "access_token", "accessToken")
    if not isinstance(access_token, str) or not access_token:
        raise HTTPException(
            status_code=502, detail="Google token refresh returned no access token"
        )
    return {
        "access_token": access_token,
        "refresh_token": _token_value(raw, "refresh_token", "refreshToken")
        or refresh_token,
        "expires_in": _token_value(raw, "expires_in", "expiresIn") or 3600,
    }


async def _claude_refresh_token(
    refresh_token: str, config: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Refresh a Claude (Anthropic) OAuth2 access token."""
    config = config or OAUTH_PROVIDERS["claude"]["config"]
    raw = await _post_json(
        config["tokenUrl"],
        {
            "grant_type": "refresh_token",
            "client_id": config["clientId"],
            "refresh_token": refresh_token,
        },
        "Claude token refresh",
    )
    access_token = _token_value(raw, "access_token", "accessToken")
    if not isinstance(access_token, str) or not access_token:
        raise HTTPException(
            status_code=502, detail="Claude token refresh returned no access token"
        )
    return {
        "access_token": access_token,
        "refresh_token": _token_value(raw, "refresh_token", "refreshToken")
        or refresh_token,
        "expires_in": _token_value(raw, "expires_in", "expiresIn") or 3600,
    }


async def _codex_refresh_token(
    refresh_token: str, config: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Refresh a Codex (OpenAI) OAuth2 access token."""
    config = config or OAUTH_PROVIDERS["codex"]["config"]
    raw = await _post_form(
        config["tokenUrl"],
        {
            "grant_type": "refresh_token",
            "client_id": config["clientId"],
            "refresh_token": refresh_token,
        },
        "Codex token refresh",
    )
    access_token = _token_value(raw, "access_token", "accessToken")
    if not isinstance(access_token, str) or not access_token:
        raise HTTPException(
            status_code=502, detail="Codex token refresh returned no access token"
        )
    return {
        "access_token": access_token,
        "refresh_token": _token_value(raw, "refresh_token", "refreshToken")
        or refresh_token,
        "expires_in": _token_value(raw, "expires_in", "expiresIn") or 3600,
    }


async def _github_refresh_token(
    refresh_token: str, config: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Refresh a GitHub OAuth2 access token and re-fetch the Copilot token.

    GitHub has a two-layer token model:
    1. OAuth refresh_token -> GitHub access_token (via tokenUrl)
    2. GitHub access_token -> Copilot token (via copilotTokenUrl)

    Both must be refreshed; the Copilot token carries its own expiry.
    """
    config = config or OAUTH_PROVIDERS["github"]["config"]
    # Layer 1: refresh the GitHub OAuth token
    raw = await _post_form(
        config["tokenUrl"],
        {
            "grant_type": "refresh_token",
            "client_id": config["clientId"],
            "refresh_token": refresh_token,
        },
        "GitHub token refresh",
    )
    access_token = _token_value(raw, "access_token", "accessToken")
    if not isinstance(access_token, str) or not access_token:
        raise HTTPException(
            status_code=502, detail="GitHub token refresh returned no access token"
        )
    new_refresh = _token_value(raw, "refresh_token", "refreshToken") or refresh_token

    # Layer 2: re-fetch the Copilot token using the new GitHub access_token
    gh_headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "X-GitHub-Api-Version": config["apiVersion"],
        "User-Agent": config["userAgent"],
    }
    copilot_token = await _get_json(config["copilotTokenUrl"], gh_headers)

    return {
        "access_token": access_token,
        "refresh_token": new_refresh,
        "expires_in": _token_value(raw, "expires_in", "expiresIn") or 28800,
        "copilot_token": copilot_token,
    }


async def _grok_refresh_token(
    refresh_token: str, config: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Refresh a Grok CLI OAuth2 access token."""
    config = config or OAUTH_PROVIDERS["grok-cli"]["config"]
    raw = await _post_form(
        config["tokenUrl"],
        {
            "grant_type": "refresh_token",
            "client_id": config["clientId"],
            "refresh_token": refresh_token,
        },
        "Grok token refresh",
    )
    access_token = _token_value(raw, "access_token", "accessToken")
    if not isinstance(access_token, str) or not access_token:
        raise HTTPException(
            status_code=502, detail="Grok token refresh returned no access token"
        )
    return {
        "access_token": access_token,
        "refresh_token": _token_value(raw, "refresh_token", "refreshToken")
        or refresh_token,
        "expires_in": _token_value(raw, "expires_in", "expiresIn") or 3600,
    }


# Dispatch table: provider name -> refresh coroutine
_REFRESH_DISPATCH: dict[str, Any] = {
    "antigravity": _google_refresh_token,
    "claude": _claude_refresh_token,
    "codex": _codex_refresh_token,
    "github": _github_refresh_token,
    "grok-cli": _grok_refresh_token,
    "kiro": _kiro_refresh_token,
}


def _is_token_expired(expires_at: str | None, skew_seconds: int = 60) -> bool:
    """Check if a token's expires_at timestamp has passed (with skew)."""
    if not expires_at or not isinstance(expires_at, str):
        return True  # No expiry info - assume expired (safe default)
    try:
        exp_dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return True
    now = datetime.now(timezone.utc)
    return now >= (exp_dt - timedelta(seconds=skew_seconds))


def _update_connection_token(
    provider: str,
    connection_id: str,
    new_access_token: str,
    new_refresh_token: str | None,
    new_expires_at: str | None,
    extra_provider_data: dict[str, Any] | None = None,
) -> None:
    """Update an existing connection's tokens in config.yaml in-place.

    This is the missing counterpart to _save_connection: it mutates the
    connection dict inside main_app.config and persists the snapshot, rather
    than creating a new connection entry.
    """
    from app import main as main_app

    if not isinstance(main_app.config, dict):
        return
    provider_config = main_app.config.get("providers", {}).get(provider, {})
    connections = provider_config.get("connections", [])
    for conn in connections:
        if isinstance(conn, dict) and conn.get("id") == connection_id:
            conn["api_key"] = new_access_token
            if new_refresh_token:
                conn["refresh_token"] = new_refresh_token
            if new_expires_at:
                conn["expires_at"] = new_expires_at
            if extra_provider_data:
                pd = conn.get("provider_data")
                if isinstance(pd, dict):
                    pd.update(extra_provider_data)
                else:
                    conn["provider_data"] = extra_provider_data
            break
    try:
        main_app._persist_config_snapshot(main_app.config)
    except OSError as exc:
        print(
            f"[OAuth] Failed to persist refreshed token for {provider}"
            f" connection {connection_id}: {exc}",
            flush=True,
        )


# Anti-ban: serialize token refreshes per provider AND pace them.
# Concurrent requests on expired tokens each fire their own refresh
# (thundering herd — a classic abuse signal) and race refresh-token
# rotation. Serializing per CONNECTION stops same-conn herds, but a
# provider still sees N refreshes back-to-back across its connections —
# burst patterns (N refreshes in <T seconds) are flagged as account
# theft. So: one refresh per provider at a time, with a minimum gap
# between them. All waiters coalesce on the leader's result.
# ponytail: registry never pruned; entry count == provider count, fine.
_refresh_locks: dict[str, asyncio.Lock] = {}
_refresh_locks_guard = asyncio.Lock()
_last_refresh_at: dict[str, float] = {}
_REFRESH_MIN_INTERVAL_SECONDS = 30.0


async def _provider_refresh_lock(provider_name: str) -> asyncio.Lock:
    """Return (creating if needed) the global per-provider refresh lock."""
    async with _refresh_locks_guard:
        lock = _refresh_locks.get(provider_name)
        if lock is None:
            lock = asyncio.Lock()
            _refresh_locks[provider_name] = lock
        return lock


async def _pace_refresh(provider_name: str) -> None:
    """Enforce the minimum inter-refresh gap for a provider.

    Caller holds the provider lock. First refresh never waits; later
    refreshes wait out the remainder of the gap.
    """
    import time

    now = time.monotonic()
    last = _last_refresh_at.get(provider_name)
    if last is not None:
        remaining = _REFRESH_MIN_INTERVAL_SECONDS - (now - last)
        if remaining > 0:
            await asyncio.sleep(remaining)
    _last_refresh_at[provider_name] = time.monotonic()


async def ensure_fresh_token(
    provider_name: str,
    connection: dict[str, Any],
    provider_config: dict[str, Any],
    force: bool = False,
) -> str:
    """Unified entry point: return a valid access token for the connection.

    If the stored token is still fresh (within 60s skew), return it as-is.
    If expired (or force=True), dispatch to the provider's refresh function,
    persist the new token via _update_connection_token, and return it.

    Fail-open: on refresh failure, log the error and return the stored token
    (which may still work if the upstream has grace). The caller's 401-retry
    will handle genuine failures.
    """
    stored_token = connection.get("api_key", "")
    expires_at = connection.get("expires_at")

    if not force and not _is_token_expired(expires_at):
        return stored_token

    refresh_token = connection.get("refresh_token")
    if not refresh_token:
        # No refresh token (e.g. Cursor import) - can't refresh.
        return stored_token

    refresh_fn = _REFRESH_DISPATCH.get(provider_name)
    if refresh_fn is None:
        # Provider has no refresh function - return stored token.
        return stored_token

    # Serialize refresh per PROVIDER (not per connection): a provider sees
    # N refreshes back-to-back across its connections, and burst patterns
    # (N refreshes in <T seconds) are flagged as account theft. One refresh
    # per provider at a time, paced, with waiters coalescing on the leader.
    # force=True still respects the lock (401-retry bursts become one refresh).
    async with await _provider_refresh_lock(provider_name):
        # Coalesce: the leader ahead of us may have already refreshed while we
        # waited on the lock. Detect via api_key mutation.
        current_token = connection.get("api_key", "")
        if current_token and current_token != stored_token:
            return current_token
        # Double-check expiry against the CURRENT expires_at (leader may have
        # refreshed) and re-read refresh_token (rotation providers may have
        # consumed ours).
        refresh_token = connection.get("refresh_token") or refresh_token
        if not force and not _is_token_expired(connection.get("expires_at")):
            return current_token or stored_token

        # We're the next actual refresher — pace before calling the provider
        # so the upstream never sees burst patterns.
        await _pace_refresh(provider_name)

        # Build the config dict for the refresh function.
        # For most providers, OAUTH_PROVIDERS[provider]["config"] has everything.
        # For Kiro, the connection's provider_data has clientId/clientSecret/region.
        if provider_name == "kiro":
            provider_data = connection.get("provider_data") or {}
            try:
                raw = await refresh_fn(refresh_token, provider_data)
            except Exception as exc:
                print(f"[OAuth] Kiro token refresh failed: {exc}", flush=True)
                return stored_token
        else:
            oauth_entry = OAUTH_PROVIDERS.get(provider_name, {})
            refresh_config = oauth_entry.get("config", provider_config)
            try:
                raw = await refresh_fn(refresh_token, refresh_config)
            except Exception as exc:
                print(f"[OAuth] {provider_name} token refresh failed: {exc}", flush=True)
                return stored_token

        new_access_token = raw.get("access_token", "")
        if not new_access_token:
            return stored_token

        new_refresh = raw.get("refresh_token") or refresh_token
        new_expires_in = raw.get("expires_in") or 3600
        new_expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=int(new_expires_in))
        ).isoformat()

        # For GitHub, also update the copilot token in provider_data.
        extra_pd = None
        if provider_name == "github" and raw.get("copilot_token"):
            ct = raw["copilot_token"]
            extra_pd = {
                "copilotToken": ct.get("token") if isinstance(ct, dict) else None,
                "copilotTokenExpiresAt": ct.get("expires_at") if isinstance(ct, dict) else None,
            }

        _update_connection_token(
            provider_name,
            connection.get("id", ""),
            new_access_token,
            new_refresh,
            new_expires_at,
            extra_pd,
        )

        print(
            f"[OAuth] Token refreshed for {provider_name} connection"
            f" {connection.get('id', '?')}, expires_at={new_expires_at}",
            flush=True,
        )
        return new_access_token
