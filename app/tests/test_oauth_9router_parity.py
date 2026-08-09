"""Focused parity checks for the OAuth flows extracted from 9router."""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


@pytest.fixture()
def oauth_module():
    import importlib
    import app.oauth as oauth

    return importlib.reload(oauth)


def test_claude_auth_url_includes_code_true(oauth_module):
    data = asyncio.run(oauth_module.authorize("claude", "http://localhost:6969/callback"))

    query = parse_qs(urlsplit(data["authUrl"]).query)
    assert query["code"] == ["true"]


def test_codex_auth_url_includes_cli_simplified_parameters(oauth_module):
    data = asyncio.run(oauth_module.authorize("codex"))

    query = parse_qs(urlsplit(data["authUrl"]).query)
    assert query["codex_cli_simplified_flow"] == ["true"]
    assert query["originator"] == ["codex_cli_rs"]


def test_antigravity_auth_url_matches_google_oauth_parameters(oauth_module):
    data = asyncio.run(oauth_module.authorize("antigravity", "http://localhost:6969/callback"))

    query = parse_qs(urlsplit(data["authUrl"]).query)
    assert query["response_type"] == ["code"]
    assert query["access_type"] == ["offline"]
    assert query["prompt"] == ["consent"]
    assert query["scope"] == [" ".join(oauth_module.OAUTH_PROVIDERS["antigravity"]["config"]["scopes"])]


def test_antigravity_post_exchange_uses_exact_9router_user_agent(oauth_module):
    get_response = httpx.Response(200, json={"email": "user@example.com"})
    load_response = httpx.Response(200, json={"cloudaicompanionProject": {"id": "project-1"}})
    # Reset cached egress client so the mock takes effect
    oauth_module._google_egress_client = None
    with (
        patch.object(oauth_module._oauth_client, "get", new=AsyncMock(return_value=get_response)),
        patch.object(oauth_module, "_get_google_egress_client") as mock_egress_factory,
        patch.object(oauth_module, "_onboard_antigravity", new=AsyncMock()),
    ):
        mock_egress = AsyncMock()
        mock_egress.get = AsyncMock(return_value=get_response)
        mock_egress.post = AsyncMock(return_value=load_response)
        mock_egress_factory.return_value = mock_egress
        result = asyncio.run(oauth_module._post_exchange_antigravity({"access_token": "access-token"}))

    assert result["projectId"] == "project-1"
    assert mock_egress.post.await_args_list[0].kwargs["headers"]["User-Agent"] == "google-api-nodejs-client/9.15.1"
    assert mock_egress.post.await_args_list[0].kwargs["headers"]["X-Goog-Api-Client"] == "google-cloud-sdk vscode_cloudshelleditor/0.1"



