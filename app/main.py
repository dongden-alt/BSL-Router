import sys as _sys
import os as _os_module
import os  # bare alias required by _git_version() (os.path)
# Force UTF-8 stdout/stderr on Windows (default cp1252 chokes on non-ASCII
# in upstream error messages, user code snippets, Chinese chars, etc.)
if hasattr(_sys.stdout, "reconfigure"):
    _sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(_sys.stderr, "reconfigure"):
    _sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Anchor project root to __file__ so the script works from ANY working directory.
# Without this, running `python /path/to/bsl-router/app/main.py` from a CWD
# outside the project fails with ModuleNotFoundError: No module named 'app'.
_project_root = _os_module.path.dirname(_os_module.path.abspath(__file__))
if _project_root not in _sys.path:
    _sys.path.insert(0, _project_root)

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse, Response, HTMLResponse
from fastapi.staticfiles import StaticFiles
import httpx
from contextlib import asynccontextmanager
from typing import Dict, Optional
from starlette.background import BackgroundTask
import asyncio
import copy
import re
import yaml
import json
import time
from datetime import datetime
import secrets as _secrets
import socket as _socket
import threading as _threading
from app.models import MitmRuntimeStatus
from app.oauth import (
    oauth_router,
    _oauth_states,
    _exchange_authorization_code,
    _complete_connection,
    _loopback_callback_page,
    OAUTH_PROVIDERS,
    ensure_fresh_token,
)
from app.utils.google_cloudcode_egress import build_google_egress_client
from app.normalizer import UniversalNormalizer
from app.middleware.caching import PromptCachingAdapter
from app.scouts.vision import polyfill_vision, VisionPolyfillFailed
from app.scouts.docs_parser import parse_documents
from app.middleware.compaction import apply_compaction
from app.middleware.efficiency import inject_turn_consolidation, inject_tool_batching
from app.middleware.bsl_chat_router import _get_bsl_cfg, route_bsl_chat, _extract_route
from app.middleware.bsl_lite_router import route_bsl_lite
from app.middleware.bsl_agentic_router import route_bsl_agentic
from app.middleware.bsl_agentic_ultra_router import route_bsl_agentic_ultra
from app.middleware.bsl_agentic_max_router import route_bsl_agentic_max
from app.middleware.bsl_orchestrator_engine import (
    AmbiguousPhase,
    build_balanced_plan,
    finish_phase,
    phase_headers,
)
from app.middleware.stream_guard import StreamEmissionState
from app.middleware.thinking_fallback import (
    is_thinking_param_rejection,
    payload_has_thinking,
    strip_thinking,
)
import app.observability as obs
from app.utils.model_resolver import resolve_active_connection
from app.circuit_breaker import init_breaker, reconfigure_breaker, get_breaker
from app.antifreeze import (
    next_stream_id,
    register_stream,
    unregister_stream,
    force_stop_all,
    active_stream_count,
    bench_leaf,
    stream_deadline,
    afz_guard,
    STREAM_HARD_DEADLINE_SECONDS,
)

# â”€â”€ Agent Compatibility Layer (Phase 2-7) â”€â”€
from app.compat import get_profile, is_anthropic_compatible, ToolLedger
from app.middleware.response_format_guard import inject_json_instruction, has_response_format
from app.middleware.glm_tools import normalize_glm_tool_calls, inject_glm_language_forcing
from app.middleware.quality import (
    is_length_truncated,
    build_continuation_payload,
    build_continuation_stream_payload,
    merge_continuation_response,
    should_retry_with_higher_budget,
    StreamTruncationDetector,
    _extract_assistant_text,
    _extract_usage,
    _extract_finish_reason,
)
from app.compat.reasoning_policy import (
    get_policy as get_reasoning_policy,
    apply_thinking_to_anthropic_payload,
    strip_thinking_from_messages,
)
# Single reasoning writer. Replaces the former in-line regex cascade AND
# the parallel apply_thinking_to_anthropic_payload path, which the cascade
# silently overwrote on every request.
from app.compat.families import resolve_thinking, matches_contract
from app.compat.stream_normalizer import StreamNormalizer
from app.compat.responses_api import ResponsesConverter
from app.utils.url_normalization import (
    build_custom_models_probe_url,
    build_custom_text_upstream_url,
    normalize_custom_text_provider_urls,
)
from app.compat.adapters import (
    unwrap_request as gemini_unwrap_request,
    is_antigravity as is_antigravity_request,
    normalize_model as normalize_gemini_model,
    gemini_request_to_openai,
    openai_chunk_to_gemini,
    openai_response_to_gemini,
    sse_data as gemini_sse_data,
    SSE_DONE as GEMINI_SSE_DONE,
    build_response_headers as gemini_response_headers,
)
from app import kiro_adapter
from app.model_discovery import discover_models, clear_discovery_cache
import fnmatch

# ── Anti-Detection: Per-provider User-Agent spoofing ─────────────────────────
# Strict providers (Claude, Antigravity, Codex, Kiro) may fingerprint BSL Router
# by its default httpx User-Agent. These authentic UAs match the real IDE/CLI
# clients so upstream providers see traffic from the expected application.
_STEALTH_USER_AGENTS: dict[str, str] = {
    "claude": "claude-cli/1.0.0 (cli; node:v22.16.0)",
    "anthropic": "claude-cli/1.0.0 (cli; node:v22.16.0)",
    "antigravity": "google-api-nodejs-client/9.15.1",
    "gemini-cli": "google-api-nodejs-client/9.15.1",
    "gemini": "google-api-nodejs-client/9.15.1",
    "codex": "codex/1.0.0",
    "kiro": "aws-toolkit-vscode/3.0.0",
    "github": "GitHubCopilotChat/0.26.7",
    "grok-cli": "grok-pager/0.2.99 grok-shell/0.2.99 (linux; x86_64)",
    "qwen": "qwen-cli/1.0.0",
    "cursor": "cursor/0.42.0",
    "openai": "codex/1.0.0",
    # Image providers — map to the authentic UA of their app family
    "openai-image": "codex/1.0.0",
    "gemini-image": "google-api-nodejs-client/9.15.1",
    "grok-image": "grok-pager/0.2.99 grok-shell/0.2.99 (linux; x86_64)",
    "qwen-image": "qwen-cli/1.0.0",
    # Video providers
    "openai-video": "codex/1.0.0",
    "google-veo": "google-api-nodejs-client/9.15.1",
    "grok-video": "grok-pager/0.2.99 grok-shell/0.2.99 (linux; x86_64)",
    "runway-video": "RunwayML-API/1.0",
    # OpenCode gateways
    "opencode-zen": "opencode/1.0.0",
    "opencode_go": "opencode/1.0.0",
    "opencode-go": "opencode/1.0.0",
}


def _inject_provider_headers(headers: dict, provider_name: str, active_conn: dict) -> None:
    """Inject provider-specific headers for upstream requests.

    Called at three sites:
    1. Initial proxy request (after User-Agent spoofing)
    2. Streaming 401-retry (after token refresh)
    3. Non-streaming 401-retry (after token refresh)

    Without re-injection on retry, Codex loses ChatGPT-Account-ID (→ HTML login
    page) and Grok CLI loses x-grok-client-version (→ "version outdated" error).
    """
    # Kiro Enterprise: TokenType header for Microsoft SSO / Azure AD tokens.
    if provider_name == 'kiro':
        headers["TokenType"] = "EXTERNAL_IDP"

    # Grok CLI: cli-chat-proxy.grok.com requires version headers.
    # Mirrors 9Router chunk 1882.js. Without x-grok-client-version, API returns:
    # "Your Grok CLI version (none) is outdated."
    if provider_name == 'grok-cli':
        headers["x-xai-token-auth"] = "xai-grok-cli"
        headers["x-grok-client-identifier"] = "grok-pager"
        # ponytail: pin to 9Router-tested version; bump when 9Router updates
        headers["x-grok-client-version"] = "0.2.99"
        headers["x-grok-client-mode"] = "headless"

    # Codex (OpenAI): chatgpt.com/backend-api/codex requires ChatGPT-Account-ID.
    # Without this header, upstream returns an HTML login page instead of JSON.
    # Mirrors 9Router chunk 318.js. Source: provider_data.chatgptAccountId.
    if provider_name == 'codex':
        _psd = active_conn.get('provider_data') or {}
        if isinstance(_psd, dict):
            _acct_id = _psd.get('chatgptAccountId') or _psd.get('accountId') or _psd.get('workspaceId')
            if _acct_id:
                headers["ChatGPT-Account-ID"] = str(_acct_id)
        headers["OpenAI-Beta"] = "codex-1"
        headers["originator"] = "codex"


def _strip_bsl_identity_headers(headers: dict) -> dict:
    """Remove all x-bsl-* and X-BSL-* headers before forwarding upstream.

    These are internal MITM→BSL routing headers and must never leak to
    upstream providers. Fail-open: returns a copy, never raises.
    """
    if not isinstance(headers, dict):
        return headers
    return {k: v for k, v in headers.items() if not k.lower().startswith("x-bsl-")}

# Gemini egress emits SSE comments during legitimate reasoning gaps, but comments
# must not turn a dead upstream into an indefinitely healthy-looking IDE stream.
# These explicit budgets remain active when the general circuit breaker is off.
# Tests monkeypatch them down to avoid production-length sleeps.
GEMINI_EGRESS_KEEPALIVE_INTERVAL = 2.0
GEMINI_EGRESS_CONNECT_KEEPALIVE_INTERVAL = 2.0
# RC4 fail-fast: the connect budget covers only the pre-header wait (TCP/TLS +
# response status line + headers). A healthy leaf returns headers in <5s; 90s
# just delayed the combo-fallback advance on a dead leaf. Body/reasoning gaps
# after headers are owned by the stall watchdog below, NOT this constant.
GEMINI_EGRESS_CONNECT_TIMEOUT = 15.0
# 9ROUTER PARITY (2026-08-04): 0.0 = DISABLED, matching undici `bodyTimeout: 0`.
#
# WHY. A 60s body-stall cap killed HEALTHY streams. Evidence (live log):
#   ttft=17015ms total=89221ms error=stream_stall  -- while that same provider
#   returned 200 OK on 15 other requests in the same session.
# The upstream was alive and thinking; Opus at thinking=max on a 114K-token
# prompt goes quiet for minutes between token bursts. We interpreted silence as
# death, killed a working stream, then the emission gate refused fallback and
# the IDE hung. 9router forwards through such gaps and does not freeze.
#
# Silence is NOT death. Only the upstream may declare a stream finished.
GEMINI_EGRESS_BODY_STALL_TIMEOUT = 0.0
# Antigravity IDE (Gemini/Cloud-Code) wedges on long comment-only preambles. Its
# TTFT budget must be FAR shorter than the 180s thinking ceiling used for
# OpenAI/Anthropic clients: fail fast so the IDE gets a terminal frame promptly
# and the dead leaf is benched (next request starts on the fallback combo leaf)
# instead of splicing a replacement into a 180s-stale stream the IDE abandoned.
# 9ROUTER PARITY (2026-08-04): 0.0 = DISABLED, matching undici `headersTimeout: 0`.
# Long thinking prompts legitimately exceed any fixed first-token budget; the
# repeated raising of this ceiling (60 -> 120 -> 180s for thinking models) was
# chasing a limit that should not exist.
GEMINI_EGRESS_TTFT_TIMEOUT = 0.0
# RC6 fail-fast: non-Gemini streams have no per-connection deadline of their own
# (the Gemini egress owns _conn_deadline; the OpenAI/Anthropic probe + single-
# model send do not). This bounds ONLY their pre-header wait so a TCP-connected-
# but-header-silent leaf advances the combo instead of hanging forever under
# httpx read=None.
#
# PRODUCTION FINDING (2026-07-22): GLM-5.2 (all Zhipu variants via Chinese
# aggregators) consistently takes 25-50s to return HTTP headers, then streams
# fine. At 20s this produced a 502 at the probe gate, killing the connection
# before the TTFT watchdog (60s) could ever apply. The TTFT watchdog is the
# correct kill mechanism for silent upstreams — the header probe must be looser.
# High-latency providers + thinking models need 90s.
HEADER_WAIT_TIMEOUT = 90.0

# â”€â”€ Unified Mode-Split Timeout Policy â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Derived from production log analysis (console_logs.jsonl, n=6749 end events).
# Stream: progress-based (TTFT + stall). Non-stream: generation budget only.
# "Fail on no-progress, not on long-thinking."
# 9ROUTER PARITY (2026-08-04): both DISABLED (0.0). See the rationale on
# GEMINI_EGRESS_BODY_STALL_TIMEOUT above. The upstream decides when a stream is
# over; the router only forwards. Real upstream failures (502/503, connection
# reset) still surface immediately -- they raise, they do not go quiet.
# 9ROUTER PARITY (2026-08-04): both DISABLED (0.0). The upstream decides when a
# stream is over; the router only forwards. Real upstream failures (502/503,
# connection reset) still surface immediately -- they raise, they do not go quiet.
# This matches 9router's undici approach: headersTimeout: 0, bodyTimeout: 0.
STREAM_STALL_TIMEOUT_DEFAULT = 0.0        # Body stall: disabled (0 = wait forever)
STREAM_TTFT_TIMEOUT_DEFAULT = 0.0         # First token cap: disabled (0 = wait forever)
NONSTREAM_TOTAL_BUDGET = 120.0            # Total generation budget for non-stream (seconds); fails inside AEP cooldown window
# Total wall-clock cap across the ENTIRE combo/fallback chain (all recursive
# hops combined). Per-leaf budgets above are per-attempt; without this cap a
# 4-leaf dead chain holds the client connection for 4x120s = 8 minutes, which
# exhausts the IDE's per-host connection pool and freezes every window.
CHAIN_TOTAL_BUDGET = 150.0
_RECOVERABLE = {403, 404, 408, 429, 500, 502, 503, 504, 524, 525, 526}  # HTTP status codes that trigger combo/chain advance

_BLACKSAND_MODEL_ALIASES = {
    "blacksand-chat": "blacksand-chat",
    "bsl-chat": "blacksand-chat",
    "BSL-Chat": "blacksand-chat",
    "blacksand-lite": "blacksand-lite",
    "bsl-lite": "blacksand-lite",
    "BSL-Lite": "blacksand-lite",
    "blacksand-agentic": "blacksand-agentic",
    "bsl-agentic": "blacksand-agentic",
    "BSL-Agentic": "blacksand-agentic",
    "blacksand-agentic-ultra": "blacksand-agentic-ultra",
    "bsl-agentic-ultra": "blacksand-agentic-ultra",
    "BSL-Agentic-Ultra": "blacksand-agentic-ultra",
    "blacksand-agentic-max": "blacksand-agentic-max",
    "bsl-agentic-max": "blacksand-agentic-max",
    "BSL-Agentic-Max": "blacksand-agentic-max",
}


def _normalize_blacksand_model_id(model: str) -> str:
    return _BLACKSAND_MODEL_ALIASES.get(model, model)


def _response_has_model_output(data: dict, out_tokens: int = 0) -> bool:
    """True when an OpenAI-compatible response contains usable model output."""
    if out_tokens > 0:
        return True
    for choice in data.get("choices") or []:
        message = choice.get("message") or {}
        for key in ("content", "reasoning_content", "reasoning"):
            value = message.get(key)
            if isinstance(value, str) and value.strip():
                return True
            if isinstance(value, list) and value:
                return True
        if message.get("tool_calls") or message.get("function_call"):
            return True
    return False


# NOTE: _resolve_combo_chain_segment used to be defined here. It now lives in
# app.routing.combo_resolver and is imported below under the same private name
# (see the combo_resolver import block). The old definition was left in place
# during the R2 extraction but was dead: the import further down rebinds the
# name, so all 5 call sites already resolved to the extracted version.

# Reasoning effort/budget helpers now live with the family contracts so
# the contract modules can use them without importing main (circular).
# Re-exported here under their original private names because existing
# tests and call sites import them from app.main.
from app.compat.families._effort import (  # noqa: E402
    apply_gpt5_reasoning_controls as _apply_gpt5_reasoning_controls,
    coerce_effort as _coerce_effort,
    budget_tokens as _budget_tokens,
    claude_modern_thinking as _claude_modern_thinking,
)


from app.config_state import get_config as cs_get_config, replace_config, init_config, get_mutable_config  # noqa: E402
from app.routing.combo_resolver import (  # noqa: E402
    resolve_combo_alias_redirect,
    resolve_combo,
    advance_combo_retry,
    resolve_alias,
    find_provider_for_model,
    build_not_found_error,
    resolve_combo_chain_segment as _resolve_combo_chain_segment,
)


# Global state
# NOTE: `config` is NOT a stored global. Each function that needs it takes a
# local snapshot: `config = cs_get_config()`. This makes the stale-mirror bug
# class structurally impossible - there is no storage to go stale.
http_client = None
ROUND_ROBIN_STATE = {}  # In-memory index counter for round_robin combos


def _replace_runtime_config(new_config: dict) -> None:
    """The ONLY sanctioned runtime config-swap path.

    Atomically: persist -> swap canonical -> reconfigure breaker.
    No mirror to forget - there is no mirror. The module-level `config` global
    is deleted; every reader takes a fresh snapshot via `config = cs_get_config()`.
    """
    _persist_config_snapshot(new_config)
    replace_config(new_config)
    reconfigure_breaker(cs_get_config())

# Default base URLs per provider â€” base only, NO path suffix.
# Backend URL builder appends /messages (anthropic fmt) or /chat/completions (openai fmt).
# Special providers (kiro, ollama) have their path handled explicitly in the URL builder.
PROVIDER_DEFAULT_URLS = {
    # OAuth
    'claude':               'https://api.anthropic.com/v1',
    'antigravity':          'https://daily-cloudcode-pa.googleapis.com',
    'codex':                'https://chatgpt.com/backend-api/codex',
    'github':               'https://api.githubcopilot.com',
    'cursor':               'https://api2.cursor.sh',
    'kilocode':             'https://api.kilo.ai/api/openrouter',
    'cline':                'https://api.cline.bot/api/v1',
    'xai':                  'https://api.x.ai/v1',
    'grok-cli':             'https://cli-chat-proxy.grok.com/v1',
    'kiro':                 'https://runtime.us-east-1.kiro.dev',
    # Free / FreeTier
    'mimo-free':            'https://api.xiaomimimo.com/api/free-ai/openai',
    'qoder':                'https://api3.qoder.sh/algo/api/v2/service/pro/sse',
    'openrouter':           'https://openrouter.ai/api/v1',
    'nvidia':               'https://integrate.api.nvidia.com/v1',
    'ollama':               'https://ollama.com/api',
    'vertex':               'https://aiplatform.googleapis.com',
    'gemini':               'https://generativelanguage.googleapis.com/v1beta/openai',
    'cloudflare-ai':        'https://api.cloudflare.com/client/v4/accounts/{accountId}/ai/v1',
    'byteplus':             'https://ark.ap-southeast.bytepluses.com/api/coding/v3',
    # API key
    'alicode':              'https://coding.dashscope.aliyuncs.com/v1',
    'alicode-intl':         'https://coding-intl.dashscope.aliyuncs.com/v1',
    'anthropic':            'https://api.anthropic.com/v1',
    'blackbox':             'https://api.blackbox.ai',
    'cerebras':             'https://api.cerebras.ai/v1',
    'chutes':               'https://llm.chutes.ai/v1',
    'cohere':               'https://api.cohere.ai/v1',
    'commandcode':          'https://api.commandcode.ai/alpha',
    'deepseek':             'https://api.deepseek.com',
    'fireworks':            'https://api.fireworks.ai/inference/v1',
    'glm':                  'https://api.z.ai/api/anthropic/v1',
    'glm-cn':               'https://open.bigmodel.cn/api/coding/paas/v4',
    'groq':                 'https://api.groq.com/openai/v1',
    'hyperbolic':           'https://api.hyperbolic.xyz/v1',
    'kimi':                 'https://api.kimi.com/coding/v1',
    'minimax':              'https://api.minimax.io/anthropic/v1',
    'minimax-cn':           'https://api.minimaxi.com/anthropic/v1',
    'mistral':              'https://api.mistral.ai/v1',
    'nebius':               'https://api.studio.nebius.ai/v1',
    'ollama-local':         'http://localhost:11434/api',
    'openai':               'https://api.openai.com/v1',
    'opencode-go':          'https://opencode.ai/zen/go/v1',
    'perplexity':           'https://api.perplexity.ai',
    'siliconflow':          'https://api.siliconflow.com/v1',
    'together':             'https://api.together.xyz/v1',
    'vercel-ai-gateway':    'https://ai-gateway.vercel.sh/v1',
    'vertex-partner':       'https://aiplatform.googleapis.com',
    'volcengine-ark':       'https://ark.cn-beijing.volces.com/api/coding/v3',
    'xiaomi-mimo':          'https://api.xiaomimimo.com/v1',
    'xiaomi-tokenplan':     'https://token-plan-sgp.xiaomimimo.com/v1',
}

def load_config():
    init_config()
    config = cs_get_config()
    # Initialize the connection-level circuit breaker from the loaded config.
    init_breaker(config)
    config = _validate_antigravity_integration_config(config)
    replace_config(config)


# Direct Antigravity integration deliberately owns its own mapping namespace.
# A missing entry is meaningful: it selects Google's native Cloud Code inference
# path instead of falling through BSL's global aliases or compatibility normalizer.
# This order mirrors the Antigravity IDE 2.1.1 model menu exactly.
ANTIGRAVITY_INTEGRATION_SLOTS = (
    "gemini-3.5-flash-medium",
    "gemini-3.5-flash-high",
    "gemini-3.5-flash-low",
    "gemini-3.1-pro-low",
    "gemini-3.1-pro-high",
    "claude-sonnet-4-6",
    "claude-opus-4-6-thinking",
    "gpt-oss-120b-medium",
)
_ANTIGRAVITY_INTEGRATION_SLOT_SET = frozenset(ANTIGRAVITY_INTEGRATION_SLOTS)
_ANTIGRAVITY_LEGACY_SLOT_MIGRATIONS = {
    "gemini-3-flash-agent": "gemini-3.5-flash-high",
}
_ANTIGRAVITY_OBSOLETE_SLOTS = frozenset({
    "gemini-default",
    "gemini-3.5-flash-extra-low",
    "gemini-3.1-pro-request-antigravity",
    "gemini-3-flash",
})
_ANTIGRAVITY_NATIVE_BASE_URL = "https://daily-cloudcode-pa.googleapis.com"
_ANTIGRAVITY_NATIVE_HOSTS = frozenset({
    "daily-cloudcode-pa.googleapis.com",
    "cloudcode-pa.googleapis.com",
})
_ANTIGRAVITY_INTEGRATION_LOCK = asyncio.Lock()
# Lazy singleton â€” built on first use so startup import order doesn't matter.
_ANTIGRAVITY_EGRESS_CLIENT: Optional[httpx.AsyncClient] = None
google_egress_client: Optional[httpx.AsyncClient] = None


def _get_antigravity_egress_client() -> httpx.AsyncClient:
    """Return the hosts-file-bypassing httpx client for native Google fallback.

    Uses build_google_egress_client() which resolves daily-cloudcode-pa.googleapis.com
    via 8.8.8.8 (not the OS hosts file), so it reaches the real Google servers
    even when the hosts file redirects the domain to 127.0.0.1 for MITM.
    """
    global google_egress_client
    if google_egress_client is not None:
        return google_egress_client
    global _ANTIGRAVITY_EGRESS_CLIENT
    if _ANTIGRAVITY_EGRESS_CLIENT is None:
        _ANTIGRAVITY_EGRESS_CLIENT = build_google_egress_client()
    return _ANTIGRAVITY_EGRESS_CLIENT


def _antigravity_integration_settings(config_data=None) -> dict:
    config = cs_get_config()
    source = config if config_data is None else config_data
    integration = source.get("antigravity_integration", {}) if isinstance(source, dict) else {}
    if not isinstance(integration, dict):
        integration = {}
    mappings = integration.get("mappings", {})
    return {
        "enabled": bool(integration.get("enabled", False)),
        "mappings": mappings if isinstance(mappings, dict) else {},
    }


def _is_known_antigravity_mapping_target(config_data: dict, target: str) -> bool:
    combo_ids = {
        combo.get("alias")
        for combo in config_data.get("combos", [])
        if isinstance(combo, dict) and isinstance(combo.get("alias"), str)
    }
    if target in combo_ids:
        return True

    provider_id, separator, model_id = target.partition("/")
    if not separator or not provider_id or not model_id:
        return False
    provider = config_data.get("providers", {}).get(provider_id)
    return bool(
        isinstance(provider, dict)
        and any(
            isinstance(model, dict) and model.get("id") == model_id
            for model in provider.get("models", [])
        )
    )


def _migrate_antigravity_integration_mappings(mappings: dict) -> dict:
    """Migrate the one proven legacy alias and discard uncertain retired slots."""
    current_mappings = {
        source_model: target
        for source_model, target in mappings.items()
        if source_model in _ANTIGRAVITY_INTEGRATION_SLOT_SET
    }
    migrated_mappings = dict(current_mappings)

    for source_model, destination_model in _ANTIGRAVITY_LEGACY_SLOT_MIGRATIONS.items():
        if source_model not in mappings:
            continue
        if destination_model in current_mappings:
            print(
                f"[AntigravityIntegration] retained current {destination_model!r} mapping "
                f"over legacy {source_model!r}.",
                flush=True,
            )
            continue
        migrated_mappings[destination_model] = mappings[source_model]
        print(
            f"[AntigravityIntegration] migrated legacy {source_model!r} mapping "
            f"to {destination_model!r}.",
            flush=True,
        )

    dropped_slots = [
        source_model
        for source_model in mappings
        if source_model in _ANTIGRAVITY_OBSOLETE_SLOTS
    ]
    if dropped_slots:
        print(
            "[AntigravityIntegration] dropped obsolete mapping slots: "
            + ", ".join(repr(source_model) for source_model in dropped_slots),
            flush=True,
        )

    # Preserve arbitrary submitted keys so the allowlist below can reject them.
    # Only explicitly retired keys are fail-open drops.
    for source_model, target in mappings.items():
        if (
            source_model not in _ANTIGRAVITY_INTEGRATION_SLOT_SET
            and source_model not in _ANTIGRAVITY_LEGACY_SLOT_MIGRATIONS
            and source_model not in _ANTIGRAVITY_OBSOLETE_SLOTS
        ):
            migrated_mappings[source_model] = target
    return migrated_mappings


def _validate_antigravity_integration_config(config_data: dict) -> dict:
    """Validate and migrate the direct-integration configuration in place."""
    if not isinstance(config_data, dict):
        raise ValueError("Configuration must be an object.")

    integration = config_data.get("antigravity_integration", {})
    if integration is None:
        integration = {}
    if not isinstance(integration, dict):
        raise ValueError("antigravity_integration must be an object.")

    enabled = integration.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("antigravity_integration.enabled must be a boolean.")

    mappings = integration.get("mappings", {})
    if mappings is None:
        mappings = {}
    if not isinstance(mappings, dict):
        raise ValueError("antigravity_integration.mappings must be an object.")

    normalized_mappings = {}
    for source_model, target in _migrate_antigravity_integration_mappings(mappings).items():
        if source_model not in _ANTIGRAVITY_INTEGRATION_SLOT_SET:
            print(f"[AntigravityIntegration] Dropping unknown source slot: {source_model!r}")
            continue
        if target in (None, ""):
            continue
        if not isinstance(target, str):
            print(f"[AntigravityIntegration] Dropping non-string mapping target for {source_model!r}")
            continue
        target = target.strip()
        if not target:
            continue
        if not _is_known_antigravity_mapping_target(config_data, target):
            print(
                f"[AntigravityIntegration] Dropping dead mapping target {target!r} "
                f"for {source_model!r}: not a configured combo alias or provider/model."
            )
            continue
        normalized_mappings[source_model] = target

    config_data["antigravity_integration"] = {
        "enabled": enabled,
        "mappings": normalized_mappings,
    }
    return config_data


def _persist_config_snapshot(updated_config: dict) -> None:
    """Write config.yaml atomically, refusing writes that would destroy it.

    THE BUG THIS FIXES (config wipe, 2026-08-03)
    This used a bare open("config.yaml", "w"), which truncates the file the
    INSTANT it opens — before a single byte is written. A kill or crash in that
    window left config.yaml as an empty `{}`. The running server kept serving
    from memory so nothing looked wrong, but on the next restart the antigravity
    provider had vanished and its models 404'd. That was misdiagnosed upstream
    as an expired OAuth token, which sent debugging in the wrong direction.

    Two independent protections, because they fail differently:

    1. ATOMIC WRITE — write to a temp file in the same directory, fsync, then
       os.replace(). os.replace is atomic on POSIX and on Windows, so a reader
       sees either the entire old file or the entire new one, never a partial
       or truncated one. Same-directory matters: os.replace is only atomic
       within a filesystem.

    2. SANITY GATE — refuse to persist a snapshot that has no providers when a
       non-empty config already exists on disk. Atomicity alone would not have
       prevented the wipe: an empty-but-VALID snapshot would be written
       perfectly atomically and still destroy the file. This guards the content;
       the gate above only guards the write.

    Failures here are logged and swallowed rather than raised: persistence is a
    side effect of request handling, and a disk problem must not take down live
    routing.
    """
    import tempfile

    target = "config.yaml"

    # --- Protection 2: refuse degenerate snapshots -------------------------
    providers = (updated_config or {}).get("providers") or {}
    if not providers:
        try:
            existing_size = os.path.getsize(target) if os.path.exists(target) else 0
        except OSError:
            existing_size = 0
        # An empty snapshot is only legitimate when there is nothing to lose
        # (genuine first run). "{}\n" is 3 bytes, so 8 clears an empty-ish file
        # without risking a real one.
        if existing_size > 8:
            print(
                "[CONFIG-GUARD] refused to persist a snapshot with no providers "
                f"over an existing {existing_size}-byte config.yaml — "
                "this is the 2026-08-03 wipe signature",
                flush=True,
            )
            return

    # --- Protection 2b: refuse massive provider-count regression -----------
    # The zero-providers check above only catches TOTAL wipes. A 1-provider
    # snapshot (from a corrupted in-memory config) passes that gate and
    # atomically destroys a 119-provider file. Refuses any write where the
    # new provider count is less than 50% of the existing on-disk count,
    # with a floor of 10 so small legitimate configs are never blocked.
    # This is the 2026-08-10 wipe signature.
    if providers:
        try:
            if os.path.exists(target):
                import yaml as _yaml_guard
                with open(target, 'r', encoding='utf-8') as _f:
                    _existing = _yaml_guard.safe_load(_f) or {}
                _existing_provs = _existing.get('providers') or {}
                _existing_count = len(_existing_provs)
                _new_count = len(providers)
                if _existing_count >= 10 and _new_count < _existing_count * 0.5:
                    print(
                        '[CONFIG-GUARD] refused to persist a snapshot with '
                        f'{_new_count} providers over an existing '
                        f'{_existing_count}-provider config.yaml - '
                        'this is the 2026-08-10 wipe signature '
                        '(in-memory config regression)',
                        flush=True,
                    )
                    return
        except Exception:
            pass  # Best-effort gate; never block writes on a read failure.

    # --- Protection 1: atomic write ----------------------------------------
    directory = os.path.dirname(os.path.abspath(target)) or "."
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(prefix=".config.", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
                # P0-2 FIX: Encrypt sensitive fields BEFORE writing to disk.
                # The caller passes a decrypted config (from cs_get_config),
                # so we must encrypt a copy before yaml.dump writes it.
                from app.crypto import encrypt_config_secrets
                import copy as _copy
                enc_config = encrypt_config_secrets(_copy.deepcopy(updated_config))
                yaml.dump(enc_config, tmp_file, default_flow_style=False, sort_keys=False)
                tmp_file.flush()
                # Durability: without fsync the rename can land before the data
                # on a power loss, producing an atomically-renamed empty file.
                os.fsync(tmp_file.fileno())
        except Exception:
            # fd is closed by the with-block even on failure.
            raise

        # Keep one backup of the last known-good config before replacing it.
        if os.path.exists(target):
            try:
                import shutil
                shutil.copy2(target, target + ".bak")
            except Exception:
                pass  # Backup is best-effort; never block the real write.

        # os.replace is atomic, but on WINDOWS it raises PermissionError
        # (WinError 5/32) if ANY other handle holds the target open — antivirus,
        # Search indexing, a config reader, the UI. Measured: 21 of 200 rapid
        # writes failed this way on this machine.
        #
        # Without this retry the fix would trade a rare config WIPE for frequent
        # SILENT config-save LOSS, which is arguably worse: the user changes a
        # setting, sees no error, and the change evaporates on restart. Retry
        # briefly, then report loudly.
        _replaced = False
        _last_exc = None
        for _attempt in range(10):
            try:
                os.replace(tmp_path, target)
                _replaced = True
                break
            except PermissionError as _pe:
                _last_exc = _pe
                time.sleep(0.05 * (_attempt + 1))  # 50ms..500ms, ~2.75s total
            except OSError as _oe:
                _last_exc = _oe
                break  # Not a transient sharing violation; stop.

        if _replaced:
            tmp_path = None  # consumed by replace; nothing to clean up
        else:
            # Loud: a dropped config write is a data-loss event, not a warning.
            print(
                f"[CONFIG-GUARD] FAILED to persist config.yaml after 10 attempts: "
                f"{type(_last_exc).__name__}: {_last_exc} — "
                f"in-memory config is UNSAVED and will be lost on restart",
                flush=True,
            )
            # MUST raise: 4 of 5 callers depend on an exception to stay correct.
            #   oauth.py:948  pops the new connection and returns HTTP 500
            #   oauth.py:1844 logs the failed token refresh
            #   main.py:1441/1599 set `config = new_config` AFTER this returns,
            #                 so swallowing would leave memory and disk diverged
            #   main.py:1704  returns HTTP 500
            # Swallowing here would make the UI report success while nothing was
            # saved — silent data loss, which is what this function exists to
            # prevent. Callers catch OSError specifically.
            raise OSError(
                f"could not replace config.yaml after 10 attempts: {_last_exc}"
            ) from _last_exc
    except Exception as exc:
        print(f"[CONFIG-GUARD] atomic persist failed: {type(exc).__name__}: {exc}", flush=True)
        raise  # Preserve the caller contract (see above).
    finally:
        # Never leave temp files behind (the round-trip test asserts this).
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


def _integration_endpoint_url() -> str:
    config = cs_get_config()
    port = config.get("server", {}).get("port", 6969) if isinstance(config, dict) else 6969
    return f"http://127.0.0.1:{port}"


def _antigravity_integration_status_payload(config_data=None) -> dict:
    integration = _antigravity_integration_settings(config_data)
    enabled = integration["enabled"]
    mapping_count = len(integration["mappings"])
    diagnostics = (
        "Mapped inference slots route through BSL; unmapped slots use native Google inference only when Antigravity forwards Google credentials."
        if enabled
        else "Integration is stopped. Requests sent to BSL require forwarded Google credentials for native Google inference."
    )
    return {
        "ok": True,
        "enabled": enabled,
        "state": "running" if enabled else "stopped",
        "endpoint": _integration_endpoint_url(),
        "mapping_count": mapping_count,
        "native_fallback": "Google Cloud Code",
        "diagnostics": diagnostics,
    }


def _validated_antigravity_native_base_url(base_url: str = _ANTIGRAVITY_NATIVE_BASE_URL) -> str:
    """Return the approved Google origin; reject loopback and arbitrary proxy targets."""
    parsed = httpx.URL(base_url)
    host = (parsed.host or "").lower()
    if (
        parsed.scheme != "https"
        or host not in _ANTIGRAVITY_NATIVE_HOSTS
        or parsed.port not in (None, 443)
        or parsed.path not in ("", "/")
    ):
        raise ValueError("Native Antigravity fallback must target an approved Google Cloud Code HTTPS origin.")
    return f"https://{host}"


def _build_antigravity_native_url(request: Request, base_url: str = _ANTIGRAVITY_NATIVE_BASE_URL) -> str:
    base = _validated_antigravity_native_base_url(base_url)
    path = request.url.path
    query = str(request.url.query)
    return f"{base}{path}{'?' + query if query else ''}"


def _antigravity_native_request_headers(request: Request) -> dict:
    """Copy Google inference-safe headers without recording authorization values."""
    explicit_headers = {
        "authorization", "x-goog-api-key", "content-type", "accept",
        "accept-encoding", "accept-language", "user-agent", "x-client-data",
        "x-cloud-trace-context", "x-request-id",
    }
    headers = {}
    for name, value in request.headers.items():
        normalized = name.lower()
        if normalized in explicit_headers or normalized.startswith(("x-goog-", "x-client-")):
            headers[name] = value
    return headers


def _antigravity_native_credentials_present(request: Request) -> bool:
    """Native Google inference needs a credential forwarded by Antigravity."""
    return any(
        request.headers.get(header, "").strip()
        for header in ("authorization", "x-goog-api-key")
    )


def _antigravity_native_credentials_error(source_model: str, is_stream: bool, fallback_reason: str):
    """Return a Gemini-shaped response instead of attempting unauthenticated native inference."""
    source_model = source_model if isinstance(source_model, str) and source_model else "<unknown>"
    if fallback_reason == "unmapped":
        message = (
            f"Antigravity source model {source_model!r} is unmapped, and native Google credentials "
            "were not forwarded. Map this slot to a BSL target or configure Antigravity to forward "
            "Authorization or x-goog-api-key."
        )
    else:
        message = (
            f"Antigravity source model {source_model!r} requires native Google fallback because "
            f"{fallback_reason}, but native credentials were not forwarded. Configure Antigravity "
            "to forward Authorization or x-goog-api-key."
        )
    error = {"error": {"code": 401, "message": message, "status": "UNAUTHENTICATED"}}
    if is_stream:
        # FREEZE FIX (2026-08-07): emit the SOLE terminal contract. The previous
        # shape yielded a bare top-level {"error":...} before the finishReason
        # candidate — that poisons the Antigravity Gemini parser into an error
        # state where it stops consuming the later candidate, so the IDE hangs.
        from app.compat.adapters.gemini import terminal_error_frame as _g_term
        return Response(
            content=(
                gemini_sse_data(_g_term(401, message, source_model))
                + GEMINI_SSE_DONE
            ),
            status_code=200,
            media_type="text/event-stream",
        )
    return JSONResponse(error, status_code=401)


async def _forward_antigravity_native_or_error(
    request: Request,
    is_stream: bool,
    raw_body: bytes,
    source_model: str,
    fallback_reason: str,
):
    """Preserve credentialed native fallback; fail explicitly when it cannot authenticate."""
    if not _antigravity_native_credentials_present(request):
        return _antigravity_native_credentials_error(source_model, is_stream, fallback_reason)
    return await _forward_antigravity_native(request, is_stream, raw_body)


def _antigravity_native_response_headers(response: httpx.Response) -> dict:
    """Forward end-to-end-safe metadata without transfer framing fields."""
    excluded = {
        "connection", "content-length", "keep-alive", "proxy-authenticate",
        "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade",
    }
    return {name: value for name, value in response.headers.items() if name.lower() not in excluded}


async def _afz_registry_only_guard(raw_agen, stream_id):
    """Kill-registry visibility WITHOUT deadline or frame injection.

    Mirrors afz_guard's register/unregister contract but omits the
    stream_deadline() wrapper. stream_deadline() ALWAYS emits an SSE terminal
    frame (error + [DONE]) on timeout; for a RAW passthrough that forwards
    upstream bytes verbatim, injecting those frames would CORRUPT a non-SSE
    body (control-plane JSON, or a non-event-stream inference response).

    So: register the current task so POST /api/antifreeze/force-stop can
    cancel it, pass bytes through untouched, and on exit aclose the upstream
    generator (httpx Response.aclose() is idempotent, so the BackgroundTask's
    aclose and this one do not conflict — see the 2026-08-02 leak fix).
    """
    _task = asyncio.current_task()
    await register_stream(stream_id, _task)
    try:
        async for chunk in raw_agen:
            yield chunk
    finally:
        try:
            await raw_agen.aclose()
        except BaseException:
            pass
        await unregister_stream(stream_id)


def _afz_passthrough_guard(raw_agen, content_type, *, protocol="openai", allow_deadline_frames=True):
    """Wrap a RAW upstream passthrough body for force-stop visibility.

    Returns (stream_id, body_generator). Unlike the SSE egress sites, raw
    passthroughs forward upstream status, headers, and content-type verbatim
    and may carry NON-SSE bodies. stream_deadline() injects SSE terminal frames
    on timeout, which would corrupt a non-SSE response, so the wrapper is
    chosen from the upstream content-type:

      - SSE (text/event-stream) AND allow_deadline_frames: full afz_guard with
        the site's `protocol` (kill-registry + hard deadline + protocol-correct
        terminal frames). This is the normal case for the Gemini inference
        passthrough, whose client is the Antigravity Gemini parser.
      - otherwise: _afz_registry_only_guard — kill-registry only, no deadline,
        no frames. Used for control-plane RPCs (always non-SSE) and for any
        non-event-stream inference body.

    `allow_deadline_frames=False` forces the registry-only path regardless of
    content-type, for sites where frame injection must never happen.
    """
    _afz_sid = next_stream_id()
    _is_sse = isinstance(content_type, str) and "text/event-stream" in content_type.lower()
    if allow_deadline_frames and _is_sse:
        return _afz_sid, afz_guard(raw_agen, _afz_sid, protocol=protocol)
    return _afz_sid, _afz_registry_only_guard(raw_agen, _afz_sid)


async def _forward_antigravity_native(request: Request, is_stream: bool, raw_body: bytes):
    """Pass inference requests to Google without re-entering local BSL."""
    try:
        # Use the hosts-file-bypassing egress client so the native fallback reaches
        # the REAL Google servers even when daily-cloudcode-pa.googleapis.com is
        # redirected to 127.0.0.1 in the hosts file for MITM interception.
        client = _get_antigravity_egress_client()
        upstream_request = client.build_request(
            "POST",
            _build_antigravity_native_url(request),
            headers=_antigravity_native_request_headers(request),
            content=raw_body,
        )
        if is_stream:
            upstream_response = await client.send(upstream_request, stream=True)
            if upstream_response.status_code != 200:
                _err_body = await upstream_response.aread()
                await upstream_response.aclose()
                try:
                    _err_json = json.loads(_err_body)
                    error_payload = {"error": _err_json.get("error", _err_json) if isinstance(_err_json, dict) else _err_json}
                except Exception:
                    error_payload = {"error": {"code": upstream_response.status_code, "message": _err_body.decode("utf-8", errors="replace"), "status": "UNKNOWN"}}
                # FREEZE FIX (2026-08-07): emit the SOLE terminal contract. A
                # preceding bare top-level {"error":...} poisons the Antigravity
                # Gemini parser (it stops consuming the later finishReason
                # candidate), reproducing the IDE freeze.
                from app.compat.adapters.gemini import terminal_error_frame as _g_term
                _err_msg = ""
                try:
                    _e = error_payload.get("error") if isinstance(error_payload, dict) else error_payload
                    _err_msg = _e.get("message", "") if isinstance(_e, dict) else str(_e)
                except Exception:
                    _err_msg = "upstream non-200 response"
                return Response(
                    content=(
                        gemini_sse_data(_g_term(upstream_response.status_code, _err_msg or "upstream non-200 response", ""))
                        + GEMINI_SSE_DONE
                    ),
                    status_code=200,
                    media_type="text/event-stream",
                )
            try:
                headers = _antigravity_native_response_headers(upstream_response)
                # ANTI-FREEZE: register this raw Gemini passthrough in the
                # kill-registry so POST /api/antifreeze/force-stop can cancel
                # it, and bound it with a hard deadline when (the normal case)
                # Google returned text/event-stream. Non-SSE bodies skip the
                # deadline wrapper: stream_deadline injects SSE terminal frames
                # on timeout, which would corrupt a non-event-stream response.
                _afz_sid, _afz_body = _afz_passthrough_guard(
                    upstream_response.aiter_raw(),
                    upstream_response.headers.get("content-type", ""),
                    protocol="gemini",
                )
                return StreamingResponse(
                    _afz_body,
                    status_code=upstream_response.status_code,
                    headers=headers,
                    background=BackgroundTask(upstream_response.aclose),
                )
            except Exception:
                await upstream_response.aclose()
                raise

        upstream_response = await client.send(upstream_request)
        try:
            return Response(
                content=upstream_response.content,
                status_code=upstream_response.status_code,
                headers=_antigravity_native_response_headers(upstream_response),
            )
        finally:
            await upstream_response.aclose()
    except Exception as exc:
        # Do not include request headers, authorization, or raw payload data in logs.
        import traceback
        traceback.print_exc()
        print(f"[AntigravityIntegration] native fallback unavailable: {type(exc).__name__}", flush=True)
        if is_stream:
            # FREEZE FIX (2026-08-07): SOLE terminal contract; no bare error prefix.
            from app.compat.adapters.gemini import terminal_error_frame as _g_term
            return Response(
                content=(
                    gemini_sse_data(_g_term(502, "Native Google inference fallback is unavailable.", ""))
                    + GEMINI_SSE_DONE
                ),
                status_code=200,
                media_type="text/event-stream",
            )
        return JSONResponse(
            {"error": "Native Google inference fallback is unavailable."},
            status_code=502,
        )


def _provider_namespace_info(prov_id: str):
    """Return (namespace, is_explicit) for a provider.

    Every provider is namespaced so published catalog IDs are globally unique
    by construction. The namespace defaults to the provider KEY (which is unique
    across config), guaranteeing no cross-provider collision for current OR
    future providers. An optional `namespace:` field in config overrides the
    default with a prettier public label (e.g. ckey -> 'ckey.vn').
    """
    config = cs_get_config()
    prov = config.get("providers", {}).get(prov_id, {})
    ns = prov.get("namespace")
    if isinstance(ns, str) and ns.strip():
        return ns.strip(), True
    return prov_id, False


def _provider_namespace(prov_id: str) -> str:
    """Return the effective public namespace for a provider (key by default)."""
    return _provider_namespace_info(prov_id)[0]


def _detect_capabilities_lite(
    model_entry: dict, model_id_lower: str
) -> tuple[bool, bool, bool]:
    """Lightweight capability detection for /v1/models publication.

    Mirrors route_registry._detect_capabilities so the IDE sees consistent
    flags with the router's internal classification. Extended with prefixes
    for modern multimodal models that BSL Router routes but the IDE wouldn't
    recognise by its built-in defaults.
    """
    vision = (
        bool(model_entry.get("vision", False))
        if isinstance(model_entry, dict)
        else False
    )
    tools = (
        bool(model_entry.get("tools", True))
        if isinstance(model_entry, dict)
        else True
    )
    reasoning = "-non-reasoning" not in model_id_lower and "-minimal" not in model_id_lower
    if not vision:
        for prefix in (
            "claude-opus",
            "claude-sonnet",
            "gpt-5",
            "gpt-4o",
            "gemini",
            "grok",
            "mimo-v2-omni",
            "kimi",
            "glm-5",
            "deepseek-v4",
            "minimax-m3",
            "qwen3",
            "seed-2-0",
        ):
            if model_id_lower.startswith(prefix):
                vision = True
                break
    return vision, tools, reasoning


def _public_model_id(prov_id: str, raw_id: str) -> str:
    """Compute the published (globally unique) catalog ID for a provider model.

    Published ID is '<namespace>/<raw_id>' (e.g. 'ckey.vn/tanynguyen97/glm-5.2'
    or 'vsllm-a/glm-5.2-anthropic'). The raw upstream ID is preserved unchanged
    inside the prefix so the upstream still receives its native model id.
    """
    ns = _provider_namespace(prov_id)
    if not raw_id:
        return raw_id
    if raw_id == ns or raw_id.startswith(f"{ns}/"):
        return raw_id
    return f"{ns}/{raw_id}"


def _resolve_namespaced_model(model: str):
    """Resolve a published namespaced ID to (provider_key, raw_model_id).

    Uses EXACT published-ID matching first (unambiguous even when raw ids
    contain slashes that collide with other provider keys, e.g. banana's
    'deepseek/deepseek-v4-flash' vs the empty 'deepseek' provider). Falls back
    to explicit-namespace prefix stripping only for providers with a configured
    `namespace:` (lets newly-added upstream models route before a catalog
    refresh). Returns (None, None) so callers can fall through to legacy
    provider/model and bare-ID resolution.
    """
    config = cs_get_config()
    if not isinstance(model, str) or "/" not in model:
        return None, None
    # Pass 1: exact match against published IDs â€” globally unambiguous.
    for prov_id, prov_data in config.get("providers", {}).items():
        # Hidden providers are admin-only (visible via /api/config); their
        # models must not resolve through the public namespaced catalog.
        if prov_data.get("hidden"):
            continue
        for m in prov_data.get("models", []):
            raw_id = m.get("id")
            if raw_id and _public_model_id(prov_id, raw_id) == model:
                return prov_id, raw_id
    # Pass 2: explicit-namespace prefix (not for implicit provider-key
    # namespaces, to avoid misrouting bare slashed ids to same-named providers).
    for prov_id, prov_data in config.get("providers", {}).items():
        ns, explicit = _provider_namespace_info(prov_id)
        if explicit and model.startswith(f"{ns}/"):
            return prov_id, model[len(ns) + 1:]
    return None, None


_proxy_clients: Dict[str, httpx.AsyncClient] = {}

# 9router undici Agent parity:
#   connectTimeout=120, headersTimeout=0 (infinite), bodyTimeout=0 (infinite)
#
# read=None (2026-08-04) — TRANSPORT-LAYER PARITY.
# The previous value (read=300.0) was documented as a "fail-safe fence" on the
# grounds that `_stall_watchdog` was the PRIMARY stall detector. That watchdog
# has now been deleted (it killed healthy thinking streams), so the rationale is
# void: httpx would have become the primary killer, silently re-creating the bug
# one layer lower, where no amount of application-level fixing could reach it.
#
# ARCHITECTURAL NOTE (httpx vs 9router's h2/undici).
# We use httpx/HTTP-1.1; 9router uses undici (and http2 for passthrough). The
# equivalences are:
#   undici headersTimeout: 0  ->  httpx read=None during the header wait
#   undici bodyTimeout:    0  ->  httpx read=None during body reads
#   undici connectTimeout     ->  httpx connect
# httpx exposes ONE `read` value covering both phases, so `None` is the only
# setting that expresses "infinite for both". A finite value cannot distinguish
# "still thinking" from "dead", which is precisely the judgment call that caused
# every freeze in this project.
#
# WHAT STILL PROTECTS US. Removing read timeouts does NOT remove failure
# detection. A dead upstream does not go quiet — it RAISES:
#   - TCP reset / FIN        -> httpx.RemoteProtocolError / ReadError
#   - TLS or DNS failure     -> httpx.ConnectError (bounded by connect=120)
#   - HTTP 4xx/5xx           -> a real response, handled by combo fallback
#   - client hits Stop       -> disconnect propagates, stream unwinds
# Only the pathological "socket open, zero bytes, forever" case is now unbounded,
# which is exactly 9router's behavior — and 9router does not freeze the IDE.
_UPSTREAM_TIMEOUT = httpx.Timeout(connect=120.0, read=None, write=None, pool=30.0)


# — Connection-pool hardening (parity with 9Router/undici resilience) ——————
# Documented 9Router failure class: keep-alive pool corruption forced restarts.
# httpx never recycled its singleton client, so a half-dead keepalive socket
# could be reused indefinitely. Two levers close the gap:
#   - keepalive_expiry: cap how long an idle socket lingers before eviction. (Set to 30.0 for drain parity)
#   - AsyncHTTPTransport(retries): transparently retry CONNECTION-LEVEL failures
#     (ConnectError/ConnectTimeout/broken-socket-on-connect) against a fresh
#     socket, mirroring undici's auto-evict-and-retry.
# NOTE: retries does NOT replay completed HTTP error responses (401/400/5xx) —
# those remain owned by the combo-fallback + softban logic.
# Keep idle connections indefinitely for 9Router parity. Active-stream silence
# is bounded by stall watchdog/breaker config, not the short old 30s read timer.
_KEEPALIVE_EXPIRY = 30.0
_POOL_RETRIES = 2


async def _conn_trace_hook(response: "httpx.Response") -> None:
    """Opt-in instrumentation (tools.conn_trace). Logs the client-side socket
    (local addr:port) per upstream response so a burst of failures can be checked
    for SAME-socket reuse â€” the signature of keep-alive pool poisoning."""
    config = cs_get_config()
    try:
        if not (config or {}).get("tools", {}).get("conn_trace", False):
            return
        sock_id = "?"
        stream = response.extensions.get("network_stream") if response.extensions else None
        if stream is not None:
            raw = stream.get_extra_info("socket")
            if raw is not None:
                try:
                    sock_id = str(raw.getsockname())
                except Exception:
                    sock_id = "?"
        print(
            f"[ConnTrace] status={response.status_code} sock={sock_id} "
            f"url={response.request.url}",
            flush=True,
        )
    except Exception:
        pass


def _tcp_keepalive_options():
    """Return conservative TCP keepalive options supported by this OS."""
    options = []
    if hasattr(_socket, "SO_KEEPALIVE"):
        options.append((_socket.SOL_SOCKET, _socket.SO_KEEPALIVE, 1))
    for name, value in (("TCP_KEEPIDLE", 60), ("TCP_KEEPINTVL", 15), ("TCP_KEEPCNT", 4)):
        option = getattr(_socket, name, None)
        if option is not None:
            options.append((_socket.IPPROTO_TCP, option, value))
    return options


def _build_hardened_client(proxy_url: Optional[str] = None, verify: bool = True) -> httpx.AsyncClient:
    """Construct an AsyncClient whose transport recycles stale sockets and retries
    connection-level faults. Limits/proxy/retries MUST live on the transport —
    when a custom transport is supplied, client-level limits/proxy are ignored.

    When verify=False, TLS certificate validation is disabled. This is ONLY
    for providers with self-signed certs (configured via ssl_verify: false
    in config.yaml). The default remains verify=True (certifi).
    """
    limits = httpx.Limits(
        max_keepalive_connections=100,
        max_connections=200,
        keepalive_expiry=_KEEPALIVE_EXPIRY,
    )
    transport = httpx.AsyncHTTPTransport(
        retries=_POOL_RETRIES,
        limits=limits,
        proxy=proxy_url,
        socket_options=_tcp_keepalive_options(),
    )
    return httpx.AsyncClient(
        timeout=_UPSTREAM_TIMEOUT,
        transport=transport,
        event_hooks={"response": [_conn_trace_hook]},
        verify=verify,
    )


def _dump_upstream_failure(provider, model, url, headers, payload, status, err_text):
    """Diagnostic (gated by tools.conn_trace): on an upstream 4xx, dump the EXACT
    outbound request (auth redacted) to .brain/logs/upstream_failures.jsonl so a
    real Antigravity payload can be diffed byte-for-byte against a known-good
    probe. Fail-open â€” never breaks the request path."""
    config = cs_get_config()
    try:
        if not (config or {}).get("tools", {}).get("conn_trace", False):
            return
        if not (400 <= int(status) < 500):
            return
        import os as _os
        _redact = ("authorization", "x-goog-api-key", "x-api-key")
        safe_headers = {
            k: ("<redacted>" if k.lower() in _redact else v)
            for k, v in dict(headers or {}).items()
        }
        rec = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "provider": provider,
            "model": model,
            "url": url,
            "status": status,
            "headers": safe_headers,
            "payload_keys": sorted(payload.keys()) if isinstance(payload, dict) else None,
            "payload": payload,
            "error": (err_text or "")[:600],
        }
        _os.makedirs(".brain/logs", exist_ok=True)
        with open(".brain/logs/upstream_failures.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
        print(
            f"[UpstreamDump] wrote failing {status} for {provider}/{model} "
            f"-> .brain/logs/upstream_failures.jsonl",
            flush=True,
        )
    except Exception as _e:
        print(f"[UpstreamDump] dump failed (non-blocking): {_e}", flush=True)


def _get_client_for_proxy(proxy_url: Optional[str] = None) -> httpx.AsyncClient:
    if not proxy_url:
        return http_client
    if proxy_url not in _proxy_clients:
        _proxy_clients[proxy_url] = _build_hardened_client(proxy_url)
    return _proxy_clients[proxy_url]


# Cache of clients with TLS verification disabled (for self-signed providers).
# Keyed by proxy_url (or "" for no proxy) so the cache stays small.
_ssl_disabled_clients: Dict[str, httpx.AsyncClient] = {}


def _get_ssl_disabled_client(proxy_url: Optional[str] = None) -> httpx.AsyncClient:
    """Return a cached httpx client with TLS verification disabled.

    ONLY for providers with ssl_verify: false in config.yaml.
    """
    _key = proxy_url or ""
    if _key not in _ssl_disabled_clients:
        _ssl_disabled_clients[_key] = _build_hardened_client(
            proxy_url=proxy_url, verify=False
        )
    return _ssl_disabled_clients[_key]

async def _mitm_watchdog_loop():
    """Background task: restart BSL's OWN mitmdump if it dies.

    HARD RULE: the watchdog NEVER takes the MITM port away from another
    process. If a foreign proxy (e.g. 9Router's node.exe) owns the port, the
    watchdog stands down and clears ``desired_running`` so it stops contending
    every 5 seconds. Only an explicit user action (the Start Integration
    button, which calls ``/api/mitm/start``) may evict a foreign owner.

    Previously this loop called the authoritative start path unconditionally,
    which force-killed whatever held :443. After a sleep/wake cycle 9Router
    would reclaim the port and BSL would silently steal it back within 5s --
    with no user action and no log of consent.
    """
    import asyncio as _asyncio
    _WATCHDOG_INTERVAL = 5  # seconds between checks
    print("[MitmWatchdog] started", flush=True)
    while True:
        try:
            await _asyncio.sleep(_WATCHDOG_INTERVAL)
            with _MITM_SUPERVISOR_LOCK:
                desired = bool(_MITM_SUPERVISOR.get("desired_running", False))
                tracked_pid = _MITM_SUPERVISOR.get("tracked_pid")
            if not desired:
                continue
            # Quick liveness check: is the tracked PID still alive?
            runtime = await asyncio.to_thread(_mitm_runtime_status)
            if runtime.get("inspection_error"):
                continue  # unknown state: never act on a guess
            if runtime.get("server") and not runtime.get("conflict"):
                continue  # still healthy

            # CONSENT GATE: a foreign owner means we stand down, permanently.
            # Clearing desired_running stops the 5s retry loop from fighting
            # another proxy for the port behind the user's back.
            foreign = [
                o for o in runtime.get("owners") or [] if not o.get("is_bsl_mitm")
            ]
            if foreign:
                names = ", ".join(
                    f"{o.get('name')}({o.get('pid')})" for o in foreign
                )
                print(
                    f"[MitmWatchdog] port {runtime.get('port')} is now owned by "
                    f"another process ({names}) — standing down. BSL will NOT "
                    f"reclaim it. Press Start Integration to take over.",
                    flush=True,
                )
                with _MITM_SUPERVISOR_LOCK:
                    _MITM_SUPERVISOR["desired_running"] = False
                    _MITM_SUPERVISOR["tracked_pid"] = None
                continue

            # Port is free (or holds only a dead BSL tree): safe to restart ours.
            print("[MitmWatchdog] BSL MITM process died — restarting...", flush=True)
            async with _MITM_LIFECYCLE_LOCK:
                await _start_mitm_locked(evict_foreign=False)
        except asyncio.CancelledError:
            print("[MitmWatchdog] cancelled", flush=True)
            break
        except Exception as exc:
            print(f"[MitmWatchdog] error: {exc}", flush=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = cs_get_config()
    global http_client
    load_config()
    # â”€â”€ AEP ephemeral ban restore (Part 2b) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # Re-hydrate still-live short cooldowns from the sidecar so a router restart
    # does not immediately re-select a leaf that was benched seconds earlier.
    # Expired entries self-prune inside the loader.
    try:
        import app.error_prevention as _ep
        _restored = _ep.load_runtime_bans(config)
        if _restored:
            print(f"[BSL Startup] Restored {_restored} live AEP cooldown(s) from sidecar.", flush=True)
    except Exception as _aep_err:
        print(f"[BSL Startup] AEP sidecar restore skipped (non-blocking): {_aep_err}", flush=True)
    # â”€â”€ Hosts file auto-restore â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # If antigravity_integration is enabled, ensure MITM intercept domains are
    # present in the hosts file on every startup. This survives Stopâ†’Start cycles
    # without requiring the user to manually click "Start Integration" again.
    _ag_cfg = config.get("antigravity_integration", {}) if isinstance(config, dict) else {}
    if _ag_cfg.get("enabled", False):
        _ag_domains = ["daily-cloudcode-pa.googleapis.com"]  # auth domain cloudcode-pa MUST NOT be intercepted â€” breaks login
        _BSL_TAG = "# bsl-router"
        try:
            with open(HOSTS_PATH, "r") as _hf:
                _h_lines = _hf.readlines()
            _existing = {
                ln.split()[1]
                for ln in _h_lines
                if ln.strip() and not ln.startswith("#") and len(ln.split()) >= 2
            }
            _to_add = [d for d in _ag_domains if d not in _existing]
            if _to_add:
                for _d in _to_add:
                    _h_lines.append(f"127.0.0.1 {_d} {_BSL_TAG}\n")
                with open(HOSTS_PATH, "w") as _hf:
                    _hf.writelines(_h_lines)
                print(f"[BSL Startup] Restored hosts entries: {_to_add}", flush=True)
            else:
                print("[BSL Startup] Hosts entries already present â€” no restore needed.", flush=True)
        except PermissionError:
            print("[BSL Startup] WARNING: Cannot auto-restore hosts file â€” run BSL Router as Administrator.", flush=True)
        except Exception as _he:
            print(f"[BSL Startup] WARNING: Hosts auto-restore failed: {_he}", flush=True)
    # High-performance, self-recycling connection pool (parity with 9Router).
    # Solves both the Node.js 5-minute timeout drops AND keep-alive pool
    # corruption via transport-level retries + bounded keepalive expiry.
    http_client = _build_hardened_client()
    # Start MITM watchdog â€” auto-restarts mitmdump if desired_running=True and it crashes.
    watchdog_task = asyncio.create_task(_mitm_watchdog_loop())

    # ── Periodic log rotation ───────────────────────────────────────────────
    async def _periodic_log_rotation():
        while True:
            await asyncio.sleep(300)  # 5 minutes
            try:
                for _p in (obs._CONSOLE_LOG_PATH, obs._USAGE_LOG_PATH):
                    try:
                        if os.path.exists(_p) and os.path.getsize(_p) > obs._MAX_LOG_FILE_SIZE:
                            obs._rotate_log_file(_p)
                    except Exception:
                        pass
            except Exception as _e:
                print(f"[BSL Router] Log rotation check failed: {_e}", flush=True)

    log_rotation_task = asyncio.create_task(_periodic_log_rotation())

    print("BSL Router Initialized. Connection pool active.")
    yield
    watchdog_task.cancel()
    log_rotation_task.cancel()
    try:
        await watchdog_task
    except asyncio.CancelledError:
        pass
    await http_client.aclose()
    for pc in _proxy_clients.values():
        await pc.aclose()
    print("BSL Router Shutting Down.")

app = FastAPI(title="BSL Router", lifespan=lifespan)
app.include_router(oauth_router)

# â”€â”€ Admin Auth (Session Store) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# In-memory session store: token -> {"created": float, "expires": float}
# Service restart clears all sessions, forcing re-authentication.
_admin_sessions: Dict[str, dict] = {}
_ADMIN_SESSION_TTL = 86400  # 24 hours

def _is_admin_auth_enabled() -> bool:
    """Check if password protection is enabled."""
    config = cs_get_config()
    admin_cfg = config.get("admin", {}) if isinstance(config, dict) else {}
    return bool(admin_cfg.get("password_enabled", False))

def _validate_admin_password(password: str) -> bool:
    """Validate admin password against config using constant-time comparison."""
    config = cs_get_config()
    admin_cfg = config.get("admin", {}) if isinstance(config, dict) else {}
    expected = admin_cfg.get("password", "123456")
    # P2-3 FIX: YAML may parse a numeric password as int/float.
    # secrets.compare_digest requires both args to be the same type.
    if not isinstance(expected, str):
        expected = str(expected)
    return _secrets.compare_digest(password, expected)

def _create_admin_session() -> str:
    """Create a new admin session token."""
    token = _secrets.token_urlsafe(32)
    now = time.time()
    _admin_sessions[token] = {
        "created": now,
        "expires": now + _ADMIN_SESSION_TTL,
    }
    return token

def _is_valid_admin_session(token: Optional[str]) -> bool:
    """Check if a session token is valid and not expired."""
    if not token:
        return False
    session = _admin_sessions.get(token)
    if not session:
        return False
    if time.time() > session["expires"]:
        del _admin_sessions[token]
        return False
    return True

def _invalidate_admin_session(token: str):
    """Remove a session token from the store."""
    _admin_sessions.pop(token, None)

# Paths that ALWAYS bypass admin auth:
#  - /health, /favicon.ico: infrastructure
#  - /admin: static files (HTML/CSS/JS must load to render login overlay)
#  - /v1/, /anthropic/, /gemini/, /v1internal:: proxy traffic (protected by API key)
#  - /api/auth/: login/logout/status must be reachable without session
_PUBLIC_PATHS = {"/health", "/favicon.ico", "/callback"}
_PUBLIC_PREFIXES = (
    "/admin",
    "/v1/", "/v1beta/", "/v1alpha/", "/anthropic/", "/gemini/", "/v1internal:",
    "/api/auth/",
)

def _is_public_path(path: str) -> bool:
    if path in _PUBLIC_PATHS:
        return True
    for prefix in _PUBLIC_PREFIXES:
        if path.startswith(prefix):
            return True
    return False

@app.middleware("http")
async def admin_auth_middleware(request: Request, call_next):
    """Gate management API routes behind password auth when enabled.
    
    When admin.password_enabled is True in config:
    - Proxy routes (/v1/*, /anthropic/*, /gemini/*, /v1internal:*) always pass (API-key protected)
    - Static files (/admin/*) always pass (needed to render login UI)
    - /api/auth/* always pass (login/logout/status)
    - All other /api/* routes require valid session cookie
    """
    if not _is_admin_auth_enabled():
        return await call_next(request)
    
    path = request.url.path
    if _is_public_path(path):
        return await call_next(request)
    
    # Gate management API routes
    if path.startswith("/api/"):
        session_token = request.cookies.get("bsl_admin_session")
        if _is_valid_admin_session(session_token):
            return await call_next(request)
        return JSONResponse(
            {"error": "Unauthorized", "auth_required": True},
            status_code=401,
        )
    
    return await call_next(request)



@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(content=b"", media_type="image/x-icon", status_code=204)

@app.get("/health")
async def health():
    return {"status": "ok"}

def _git_version() -> str:
    """Return git describe version, or 'unknown'."""
    try:
        import subprocess
        result = subprocess.run(
            ["git", "describe", "--tags", "--always"],
            capture_output=True, text=True, timeout=3,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        )
        return result.stdout.strip() or "unknown"
    except Exception:
        return "unknown"

# ── Auto-Update State (module-level, polled by frontend) ─────────────────────
import sys as _sys

_update_state: dict = {
    "phase": "idle",          # idle | downloading | extracting | writing | finalizing | done | error
    "progress": 0,
    "files_updated": 0,
    "files_total": 0,
    "files_skipped": 0,
    "current_file": None,
    "error": None,
}

def _read_version_file() -> str | None:
    """Read the VERSION file at project root. Returns None if missing."""
    try:
        _version_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "VERSION",
        )
        with open(_version_path, "r") as f:
            return f.read().strip() or None
    except Exception:
        return None

@app.get("/api/version/check")
async def version_check():
    """Frontend auto-update probe. Returns current version + update status."""
    current = _read_version_file() or _git_version()
    return {
        "currentVersion": current,
        "hasUpdate": False,       # No update source configured yet
        "latestVersion": None,    # Will be populated when an update source is wired
    }

@app.post("/api/version/update")
async def trigger_update():
    """Trigger a self-update (download + extract + overwrite).

    Currently a stub — no update source is configured (no git remote, no
    GitHub Releases repo). Returns a clear message so the frontend can
    surface it to the user instead of silently failing.
    """
    if _update_state["phase"] not in ("idle", "done", "error"):
        return {"error": "Update already in progress"}

    _update_state.update(
        phase="error",
        progress=0,
        error="No update source configured. Add a git remote or set a release URL in config.yaml to enable self-update.",
    )
    return {"error": _update_state["error"]}

@app.get("/api/version/update/status")
async def update_status():
    """Poll update progress. Called every 1s by the frontend modal."""
    return _update_state

@app.post("/api/version/restart")
async def restart_server():
    """Restart the BSL Router server process.

    Returns immediately; a 1s delayed callback kills the current process
    and relaunches uvicorn via subprocess. The frontend polls
    /api/version/check until the server comes back, then reloads.
    """
    import subprocess as _sp
    import threading as _th

    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _venv_python = os.path.join(_root, ".venv", "Scripts", "python.exe")
    _main_module = "app.main:app"
    _port = 6969  # canonical; config-derived port is read at startup

    def _delayed_restart():
        import time as _time
        _time.sleep(1.0)
        # Launch new uvicorn process in detached mode
        _sp.Popen(
            [_venv_python, "-m", "uvicorn", _main_module, "--host", "0.0.0.0", "--port", str(_port)],
            cwd=_root,
            creationflags=_sp.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            close_fds=True,
        )
        # Kill current process tree
        os._exit(0)

    _th.Thread(target=_delayed_restart, daemon=True).start()
    return {"ok": True}


# ── Antigravity OAuth Callback (root-level, for Google redirect) ──────────
_COOP_HEADERS = {"Cross-Origin-Opener-Policy": "unsafe-none"}

def _callback_page(success: bool, message: str) -> HTMLResponse:
    """Return HTMLResponse with COOP header to allow popup.close()."""
    return HTMLResponse(_loopback_callback_page(success, message), headers=_COOP_HEADERS)


@app.get("/callback")
async def antigravity_callback(code: str | None = None, state: str | None = None,
                                error: str | None = None, error_description: str | None = None):
    """Handle Google/Standard OAuth redirects at root level.

    Exchanges the code automatically and registers completion in _oauth_completions.
    """
    if error:
        if state:
            from app.oauth import _oauth_completions
            _oauth_completions[state] = {"status": "error", "error": error_description or error}
        return _callback_page(False, error_description or error)
    if not code:
        return _callback_page(False, "No authorization code received.")
    entry = _oauth_states.pop(state, None) if state else None
    if not entry:
        return _callback_page(False, "OAuth state is invalid or expired.")
    provider = entry.get("provider")
    try:
        from app.oauth import OAUTH_PROVIDERS, _oauth_completions
        provider_entry = OAUTH_PROVIDERS[provider]
        tokens = await _exchange_authorization_code(
            provider, provider_entry, code,
            entry["redirect_uri"], entry.get("code_verifier"),
        )
        connection = await _complete_connection(provider, provider_entry, tokens)
        _oauth_completions[state] = {"status": "done", "connection": connection}
        return _callback_page(True, f"Connected as {connection.get('email', connection.get('displayName', ''))}")
    except Exception as exc:
        if state:
            from app.oauth import _oauth_completions
            _oauth_completions[state] = {"status": "error", "error": str(exc)}
        return _callback_page(False, str(exc))


@app.get("/api/config")
async def get_config():
    # WARNING: Do NOT filter out builtin/virtual providers here. The admin UI
    # uses GET→modify→POST round-trip. Any provider filtered out of the GET
    # response will be absent from the POST body and permanently removed from
    # config.yaml by _persist_config_snapshot. The /v1/models endpoint has its
    # own filter (line ~6658) that prevents builtin model-id duplication.
    config = cs_get_config()
    return JSONResponse(config)

@app.post("/api/config")
async def update_config(request: Request):
    try:
        config = cs_get_config()
        new_config = await request.json()

        # Preserve server-managed provider metadata that the admin UI does not
        # round-trip (e.g. the public `namespace` override). Without this, a
        # normal Save from the dashboard would silently wipe these fields and
        # reintroduce model-id collisions across providers.
        _preserve_keys = ("namespace",)
        old_providers = config.get("providers", {}) if isinstance(config, dict) else {}
        new_providers = new_config.get("providers", {}) if isinstance(new_config, dict) else {}
        for prov_id, old_prov in old_providers.items():
            if not isinstance(old_prov, dict):
                continue
            new_prov = new_providers.get(prov_id)
            if not isinstance(new_prov, dict):
                continue
            for k in _preserve_keys:
                if k not in new_prov and k in old_prov:
                    new_prov[k] = old_prov[k]

        # Preserve live ErrorPrevention state so frontend auto-saves don't wipe it
        if "error_prevention_state" in config:
            new_config["error_prevention_state"] = config["error_prevention_state"]

        new_config = normalize_custom_text_provider_urls(new_config)
        new_config = _validate_antigravity_integration_config(new_config)

        _replace_runtime_config(new_config)

        # Clear round-robin state so edited/reordered combos start fresh.
        # Without this, RR counters leak stale indices after combo add/remove/reorder.
        ROUND_ROBIN_STATE.clear()

        return JSONResponse({"status": "success"})
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# â”€â”€ BSL Matrix UI API â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@app.get("/api/bsl-matrix/state")
async def bsl_matrix_state():
    """Return current BSL model family config + available route families."""
    config = cs_get_config()
    from app.middleware.bsl_benchmark_sheet import ALL_CATEGORIES, ALL_TIERS, GLOBAL_LAST_FALLBACK_FAMILY
    from app.middleware.route_registry import build_route_registry, list_available_families
    bsl_cfg = {}
    if isinstance(config, dict):
        bsl_models = config.get("bsl_models", {})
        if isinstance(bsl_models, dict):
            bsl_cfg = bsl_models
    registry = build_route_registry(config)
    available = list_available_families(registry, enabled_only=True)
    return JSONResponse({
        "bsl_models": bsl_cfg,
        "available_families": available,
        "all_categories": list(ALL_CATEGORIES),
        "all_tiers": list(ALL_TIERS),
        "global_last_fallback": GLOBAL_LAST_FALLBACK_FAMILY,
    })

@app.post("/api/bsl-matrix/auto-select-preview")
async def bsl_matrix_auto_select_preview(request: Request):
    """Run auto-select for a cell (or full matrix) without applying changes."""
    config = cs_get_config()
    from app.middleware.bsl_auto_select import auto_select_cell, auto_select_full_matrix
    try:
        body = await request.json()
        category = body.get("category")
        tier = body.get("tier")
        if category and tier:
            result = auto_select_cell(config, category, tier)
            return JSONResponse({
                "primary": _scored_to_dict(result.primary),
                "fallback_1": _scored_to_dict(result.fallback_1),
                "fallback_2": _scored_to_dict(result.fallback_2),
                "global_last_fallback": result.global_last_fallback,
                "explanation": result.explanation,
                "warnings": result.warnings,
            })
        else:
            matrix = auto_select_full_matrix(config)
            return JSONResponse({
                cat: {tier: _result_to_dict(matrix[cat][tier]) for tier in matrix[cat]}
                for cat in matrix
            })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/api/bsl-lite-matrix/auto-select-preview")
async def bsl_lite_matrix_auto_select_preview(request: Request):
    """Run auto-select for a BSL-Lite cell (or full matrix) without applying changes.

    Uses the coding-agent benchmark sheet (bsl_lite_benchmark_sheet) instead of
    the chat benchmark sheet.
    """
    config = cs_get_config()
    from app.middleware.bsl_auto_select import auto_select_cell, auto_select_full_matrix
    try:
        body = await request.json()
        category = body.get("category")
        tier = body.get("tier")
        if category and tier:
            result = auto_select_cell(config, category, tier, target="bsl_lite")
            return JSONResponse({
                "primary": _scored_to_dict(result.primary),
                "fallback_1": _scored_to_dict(result.fallback_1),
                "fallback_2": _scored_to_dict(result.fallback_2),
                "global_last_fallback": result.global_last_fallback,
                "explanation": result.explanation,
                "warnings": result.warnings,
            })
        else:
            matrix = auto_select_full_matrix(config, target="bsl_lite")
            return JSONResponse({
                cat: {tier: _result_to_dict(matrix[cat][tier]) for tier in matrix[cat]}
                for cat in matrix
            })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/api/bsl-agentic-matrix/auto-select-preview")
async def bsl_agentic_matrix_auto_select_preview(request: Request):
    """Run auto-select for a BSL-Agentic agent cell (or full matrix).

    Uses the agent-role benchmark sheet (bsl_agentic_benchmark_sheet) — agents
    have no complexity tier in the UI, so all cells resolve at "standard".
    """
    config = cs_get_config()
    from app.middleware.bsl_auto_select import auto_select_cell, auto_select_full_matrix
    try:
        body = await request.json()
        category = body.get("category")
        tier = body.get("tier")
        if category and tier:
            result = auto_select_cell(config, category, tier, target="bsl_agentic")
            return JSONResponse({
                "primary": _scored_to_dict(result.primary),
                "fallback_1": _scored_to_dict(result.fallback_1),
                "fallback_2": _scored_to_dict(result.fallback_2),
                "global_last_fallback": result.global_last_fallback,
                "explanation": result.explanation,
                "warnings": result.warnings,
            })
        else:
            matrix = auto_select_full_matrix(config, target="bsl_agentic")
            return JSONResponse({
                cat: {tier: _result_to_dict(matrix[cat][tier]) for tier in matrix[cat]}
                for cat in matrix
            })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/api/bsl-matrix/apply")
async def bsl_matrix_apply(request: Request):
    """Apply BSL model config changes (bsl_models section)."""
    try:
        config = cs_get_config()
        body = await request.json()
        bsl_models = body.get("bsl_models", {})
        new_config = dict(config)
        new_config["bsl_models"] = bsl_models
        if "tools" not in new_config:
            new_config["tools"] = {}
        tools = body.get("tools", {})
        new_config["tools"].update(tools)

        # Routing is ALWAYS ON (2026-08-06 directive). The bsl_chat_router
        # tools flag has been removed — bsl_models.bsl_chat.enabled controls
        # ONLY catalog visibility (/v1/models), never routing behavior.

        # Keep each model's enable flag in sync with its tools.* router flag
        # for backward compatibility. NOTE (2026-08-06): these flags gate ONLY
        # catalog visibility (/v1/models), never routing behavior (always on).
        for _bsl_key in ("bsl_lite", "bsl_agentic", "bsl_agentic_ultra"):
            _bsl_sec = bsl_models.get(_bsl_key) if isinstance(bsl_models, dict) else None
            _flag_key = f"{_bsl_key}_router"
            if isinstance(_bsl_sec, dict) and "enabled" in _bsl_sec:
                new_config["tools"][_flag_key] = bool(_bsl_sec["enabled"])
            elif _flag_key in new_config["tools"]:
                if isinstance(bsl_models.get(_bsl_key), dict):
                    bsl_models[_bsl_key]["enabled"] = bool(new_config["tools"][_flag_key])

        _replace_runtime_config(new_config)
        return JSONResponse({"status": "success"})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


def _combo_primary_canonical(
    combo_alias: str,
    combos_by_alias: dict,
    visiting: Optional[set] = None,
) -> str:
    """Resolve a combo's first chain entry to its canonical model family."""
    if not combo_alias or combo_alias not in combos_by_alias:
        return ""
    visiting = set(visiting or ())
    if combo_alias in visiting:
        return ""
    visiting.add(combo_alias)

    combo = combos_by_alias[combo_alias]
    chain = combo.get("chain", []) if isinstance(combo, dict) else []
    if not isinstance(chain, list) or not chain:
        return ""
    first = chain[0]

    from app.middleware.route_registry import normalize_canonical

    if isinstance(first, dict):
        return normalize_canonical(str(first.get("model", "")))
    if not isinstance(first, str) or not first:
        return ""
    if first in combos_by_alias:
        return _combo_primary_canonical(first, combos_by_alias, visiting)
    model_id = first.split("/", 1)[1] if "/" in first else first
    return normalize_canonical(model_id)


def _preferred_combo_alias(config_data: dict, canonical_id: str) -> Optional[str]:
    """Return the most specific configured combo for a canonical family."""
    from app.middleware.route_registry import normalize_canonical

    combos = config_data.get("combos", []) if isinstance(config_data, dict) else []
    if not isinstance(combos, list) or not canonical_id:
        return None
    combos_by_alias = {
        combo["alias"]: combo
        for combo in combos
        if isinstance(combo, dict) and isinstance(combo.get("alias"), str)
    }

    # A family-named alias is more precise than a broad meta-combo such as coder-3.
    for alias in combos_by_alias:
        if normalize_canonical(alias) == canonical_id:
            return alias
    for alias in combos_by_alias:
        if _combo_primary_canonical(alias, combos_by_alias) == canonical_id:
            return alias
    return None


def _scored_to_dict(scored) -> dict:
    """Serialize a ScoredRoute, preferring combo aliases for matrix storage."""
    config = cs_get_config()
    if not scored or not scored.route:
        return None
    leaf_route_id = scored.route.route_id
    route_id = _preferred_combo_alias(config, scored.route.canonical_id) or leaf_route_id
    return {
        "route_id": route_id,
        "leaf_route_id": leaf_route_id,
        "canonical_id": scored.route.canonical_id,
        "provider_id": scored.route.provider_id,
        "model_id": scored.route.model_id,
        "quality_score": scored.quality_score,
        "total_score": round(scored.total_score, 2),
        "reason": scored.reason,
    }


def _result_to_dict(result) -> dict:
    """Serialize an AutoSelectResult to JSON-safe dict."""
    return {
        "primary": _scored_to_dict(result.primary),
        "fallback_1": _scored_to_dict(result.fallback_1),
        "fallback_2": _scored_to_dict(result.fallback_2),
        "global_last_fallback": result.global_last_fallback,
        "explanation": result.explanation,
        "warnings": result.warnings,
    }


@app.get("/api/antigravity-integration/status")
async def antigravity_integration_status():
    return JSONResponse(_antigravity_integration_status_payload())


async def _set_antigravity_integration_enabled(enabled: bool):
    async with _ANTIGRAVITY_INTEGRATION_LOCK:
        config = cs_get_config()
        updated_config = copy.deepcopy(config)
        integration = updated_config.setdefault("antigravity_integration", {})
        integration["enabled"] = enabled
        integration.setdefault("mappings", {})
        try:
            updated_config = _validate_antigravity_integration_config(updated_config)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        try:
            # Single sanctioned swap path: persist -> swap canonical -> breaker.
            # A persist failure raises before replace_config, so the running
            # config stays untouched (same contract as the old explicit calls).
            _replace_runtime_config(updated_config)
        except Exception:
            return JSONResponse(
                {"ok": False, "error": "Could not persist Antigravity integration state."},
                status_code=500,
            )

        action = "started" if enabled else "stopped"
        return JSONResponse({
            "ok": True,
            "message": f"Antigravity direct integration {action}.",
            **_antigravity_integration_status_payload(),
        })


@app.post("/api/antigravity-integration/start")
async def antigravity_integration_start():
    return await _set_antigravity_integration_enabled(True)


@app.post("/api/antigravity-integration/stop")
async def antigravity_integration_stop():
    return await _set_antigravity_integration_enabled(False)


@app.post("/api/antigravity-integration/start-full")
async def antigravity_integration_start_full():
    """Combined: set enabled=True + restore hosts entries + start MITM (kills port 443 conflicts)."""
    # Step 1: Enable integration flag
    flag_result = await _set_antigravity_integration_enabled(True)
    if hasattr(flag_result, "status_code") and flag_result.status_code >= 400:
        return flag_result

    # Step 2: Restore hosts entries (same logic as startup auto-restore)
    _ag_domains = ["daily-cloudcode-pa.googleapis.com"]  # auth domain cloudcode-pa MUST NOT be intercepted â€” breaks login
    _BSL_TAG = "# bsl-router"
    hosts_note = "hosts_skipped"
    try:
        with open(HOSTS_PATH, "r") as _hf:
            _h_lines = _hf.readlines()
        _existing = {
            ln.split()[1]
            for ln in _h_lines
            if ln.strip() and not ln.startswith("#") and len(ln.split()) >= 2
        }
        _to_add = [d for d in _ag_domains if d not in _existing]
        if _to_add:
            for _d in _to_add:
                _h_lines.append(f"127.0.0.1 {_d} {_BSL_TAG}\n")
            with open(HOSTS_PATH, "w") as _hf:
                _hf.writelines(_h_lines)
            hosts_note = f"restored:{','.join(_to_add)}"
        else:
            hosts_note = "already_present"
    except PermissionError:
        hosts_note = "permission_denied"
    except Exception as _he:
        hosts_note = f"error:{_he}"

    # Step 3: Start MITM â€” delegate to existing endpoint (handles lock + kill + verify)
    mitm_resp = await mitm_start()
    mitm_payload: dict = {}
    try:
        mitm_payload = json.loads(mitm_resp.body) if isinstance(mitm_resp.body, bytes) else {}
    except Exception:
        pass

    if not mitm_payload.get("ok", False):
        # Rollback the integration flag if MITM failed
        await _set_antigravity_integration_enabled(False)
        return JSONResponse({
            "ok": False,
            "error": f"Failed to start MITM server: {mitm_payload.get('error', 'unknown error')}. Hosts file status: {hosts_note}",
            "mitm": mitm_payload
        }, status_code=mitm_resp.status_code if hasattr(mitm_resp, "status_code") else 500)

    return JSONResponse({
        "ok": True,
        "enabled": True,
        "message": "Antigravity integration fully started.",
        "hosts": hosts_note,
        "mitm": mitm_payload,
        **_antigravity_integration_status_payload(),
    })


@app.post("/api/antigravity-integration/stop-full")
async def antigravity_integration_stop_full():
    """Combined: stop MITM + set enabled=False."""
    # Step 1: Stop MITM â€” delegate to existing endpoint (handles lock + verify)
    mitm_resp = await mitm_stop(force=True)
    mitm_note: str
    try:
        mitm_body = json.loads(mitm_resp.body) if isinstance(mitm_resp.body, bytes) else {}
        mitm_note = "mitm_stopped" if mitm_body.get("ok") else f"mitm_error:{mitm_body.get('code', 'unknown')}"
    except Exception:
        mitm_note = "mitm_response_parse_error"

    # Step 2: Disable integration flag (best-effort even if MITM stop had issues)
    await _set_antigravity_integration_enabled(False)

    return JSONResponse({
        "ok": True,
        "enabled": False,
        "message": "Antigravity integration stopped.",
        "mitm": mitm_note,
        **_antigravity_integration_status_payload(),
    })


@app.post("/api/antifreeze/force-stop")
async def antifreeze_force_stop():
    """Absolute anti-freeze: cancel every active SSE stream task.

    Each cancelled wrapper emits a terminal error + [DONE] frame while
    unwinding, so the Antigravity IDE client unblocks immediately instead of
    waiting forever on a stalled 400/503/504 stream. Safe to call repeatedly;
    returns the number of streams cancelled.
    """
    cancelled = await force_stop_all()
    return JSONResponse({
        "ok": True,
        "cancelled": cancelled,
        "message": f"Cancelled {cancelled} active stream(s).",
    })


@app.get("/api/antifreeze/status")
async def antifreeze_status():
    return JSONResponse({
        "ok": True,
        "active_streams": active_stream_count(),
    })


@app.get("/api/observability/usage")
async def get_usage():
    # Retroactively recalculate costs using the current pricing registry
    # so historical entries logged with cost=0 get correct values.
    config = cs_get_config()
    obs.recompute_usage_costs(config)
    return JSONResponse(obs.usage_stats)

@app.get("/api/observability/logs")
async def get_logs():
    return JSONResponse(obs.console_logs)

@app.get("/api/observability/artifacts")
async def get_artifacts():
    return JSONResponse(obs.error_reports)

@app.get("/api/observability/artifact/{filename}")
async def get_artifact(filename: str):
    import os
    safe_dir = os.path.realpath("artifacts/error_reports")
    filepath = os.path.realpath(os.path.join(safe_dir, filename))
    # Path traversal guard: resolved path must stay inside safe_dir
    if not filepath.startswith(safe_dir + os.sep) and filepath != safe_dir:
        return JSONResponse({"error": "Invalid filename"}, status_code=400)
    if not os.path.exists(filepath):
        return JSONResponse({"error": "Artifact not found"}, status_code=404)
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()
    return Response(content=content, media_type="text/markdown")

@app.post("/api/observability/analyze_errors")
async def trigger_error_analysis():
    # Run in background to avoid blocking the UI
    config = cs_get_config()
    asyncio.create_task(obs.run_error_analysis(http_client, config))
    return JSONResponse({"status": "Analysis started"})

@app.post("/api/observability/clear_logs")
async def clear_logs():
    _cleared_console = len(obs.console_logs)
    obs.console_logs.clear()
    # Only truncate the console log file — NEVER usage_stats (usage/cost data
    # must survive log clears so the Usage & Costs page keeps its history).
    try:
        with open(obs._CONSOLE_LOG_PATH, "w", encoding="utf-8") as f:
            pass  # truncate
    except Exception as _e:
        print(f"[Observability] clear_logs disk truncate failed: {_e}", flush=True)
    return JSONResponse({"status": "Logs cleared", "cleared": {"console": _cleared_console}})

# â”€â”€â”€ Auto Error Prevention â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

import app.error_prevention as ep

@app.get("/api/error-prevention/notifications")
async def get_notifications():
    """Active (non-dismissed) dashboard notifications, newest first."""
    active = [n for n in ep.notifications if not n.get("dismissed")]
    return JSONResponse(list(reversed(active)))

@app.post("/api/error-prevention/notifications/{notif_id}/dismiss")
async def dismiss_notification(notif_id: int):
    for n in ep.notifications:
        if n["id"] == notif_id:
            n["dismissed"] = True
    return JSONResponse({"status": "dismissed"})

@app.post("/api/error-prevention/notifications/dismiss-all")
async def dismiss_all_notifications():
    for n in ep.notifications:
        n["dismissed"] = True
    return JSONResponse({"status": "all dismissed"})

@app.get("/api/error-prevention/bans")
async def get_active_bans():
    """List models currently under soft/long-ban or disabled by self-heal."""
    config = cs_get_config()
    mgr = ep.ErrorPreventionManager(config)
    return JSONResponse(mgr.get_active_bans())

@app.post("/api/error-prevention/clear-bans")
async def clear_bans():
    """Lift active soft/long bans ONLY (does NOT re-enable disabled models)."""
    config = cs_get_config()
    mgr = ep.ErrorPreventionManager(config)
    lifted = mgr.clear_temp_bans_with_count()
    return JSONResponse({
        "status": "temporary bans lifted",
        "lifted_count": lifted,
        "disabled_models_unchanged": True,
        "note": "Disabled models are NOT re-enabled by this endpoint. Use /api/error-prevention/lift-all-bans to also re-enable disabled models.",
    })


@app.post("/api/error-prevention/lift-all-bans")
async def lift_all_bans():
    """
    Full ban lift: clear temp/soft/long bans and re-enable models that were
    disabled by Error Prevention/self-heal. Manually disabled catalog models
    stay disabled. Does NOT alter provider connections or API keys.
    Persists config after re-enabling self-heal-disabled models.
    """
    config = get_mutable_config()
    try:
        mgr = ep.ErrorPreventionManager(config)

        # 1. Clear temp bans (softban / longban) and get count.
        temp_lifted = mgr.clear_temp_bans_with_count()

        # 2. Capture self-heal-disabled provider/model pairs BEFORE clearing
        # disabled state. This avoids re-enabling intentionally disabled
        # catalog models that were not disabled by Error Prevention.
        disabled_pairs = set()
        for entry in mgr.state.values():
            if entry.get("ban_state") == "disabled":
                provider = entry.get("provider")
                model = entry.get("model")
                if provider and model:
                    disabled_pairs.add((provider, model))

        # 3. Clear disabled ban-state entries in the error-prevention state.
        disabled_state_cleared = 0
        for entry in mgr.state.values():
            if entry.get("ban_state") == "disabled":
                entry["ban_state"] = None
                entry["ban_until"] = None
                entry["streak"] = 0
                entry["ban_escalation_count"] = 0
                disabled_state_cleared += 1

        # 4. Re-enable only self-heal-disabled models in the live config
        # (never provider connections; never unrelated manual model disables).
        models_reenabled = 0
        for provider_id, prov_data in config.get("providers", {}).items():
            if not isinstance(prov_data, dict):
                continue
            for m in prov_data.get("models", []):
                if not isinstance(m, dict):
                    continue
                model_id = m.get("id")
                if (provider_id, model_id) in disabled_pairs and m.get("enabled") is False:
                    m["enabled"] = True
                    models_reenabled += 1

        # 5. Count only self-heal-disabled models that remain disabled.
        disabled_remaining = 0
        for provider_id, model_id in disabled_pairs:
            prov_data = config.get("providers", {}).get(provider_id, {})
            if not isinstance(prov_data, dict):
                disabled_remaining += 1
                continue
            match = next(
                (m for m in prov_data.get("models", []) if isinstance(m, dict) and m.get("id") == model_id),
                None,
            )
            if not match or match.get("enabled") is False:
                disabled_remaining += 1

        # 6. Persist + commit to the live master if anything was re-enabled.
        # _replace_runtime_config persists to disk AND swaps the running config AND
        # reconfigures the breaker — a bare _persist_config_snapshot would leave the
        # live router still treating the re-enabled models as disabled until restart.
        if models_reenabled > 0:
            try:
                _replace_runtime_config(config)
            except Exception as save_err:
                print(f"[ErrorPrevention] lift-all-bans config save failed: {save_err}")

        return JSONResponse({
            "ok": True,
            "temp_bans_lifted": temp_lifted,
            "disabled_state_cleared": disabled_state_cleared,
            "models_reenabled": models_reenabled,
            "disabled_remaining": disabled_remaining,
        })
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

@app.post("/api/error-prevention/enable-model")
async def enable_model(request: Request):
    """Re-enable a disabled model and persist the config change."""
    config = get_mutable_config()
    try:
        body = await request.json()
        model = body.get("model")
        provider = body.get("provider")
        if not model or not provider:
            return JSONResponse({"error": "model and provider required"}, status_code=400)
        mgr = ep.ErrorPreventionManager(config)
        # manually_enable_model now commits the enable to the live master via
        # _replace_runtime_config (persist + swap + breaker) and returns True.
        mgr.manually_enable_model(provider, model)
        return JSONResponse({"status": "enabled", "model": model})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/circuit-breaker/health")
async def get_breaker_health():
    """List all tracked connections with their current breaker state."""
    _breaker = get_breaker()
    if not _breaker:
        return JSONResponse({"enabled": False, "connections": []})
    return JSONResponse({
        "enabled": _breaker.enabled,
        "settings": {
            "failure_threshold": _breaker.failure_threshold,
            "recovery_timeout": _breaker.recovery_timeout,
            "stream_stall_timeout": _breaker.stream_stall_timeout,
        },
        "connections": _breaker.get_health_summary(),
    })

@app.post("/api/circuit-breaker/reset")
async def reset_breaker():
    """Reset all connections to CLOSED state (admin action)."""
    _breaker = get_breaker()
    if not _breaker:
        return JSONResponse({"ok": False, "error": "breaker not initialized"})
    _breaker.reset_all()
    return JSONResponse({"ok": True, "status": "all connections reset to CLOSED"})



# â”€â”€â”€ Canonical Model Pricing â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# File-backed canonical pricing registry + offline detector. The detector maps
# every configured provider model to a single canonical family (collapsing
# variants like gpt-5.5 / gpt-5.5-pro20x / gpt-5.5-pro20x-openai-compact into
# one row) and is fully offline + deterministic.

import importlib.util as _importlib_util

_PRICING_REGISTRY_PATH = "data/model_pricing_registry.json"
_PRICING_DETECTED_PATH = "data/model_pricing_detected.json"


def _load_pricing_detector():
    """Load scripts/detect_model_pricing.py as a module (scripts/ is not a
    package). Cached on first use."""
    mod = getattr(_load_pricing_detector, "_mod", None)
    if mod is not None:
        return mod
    script = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "scripts", "detect_model_pricing.py")
    spec = _importlib_util.spec_from_file_location("bsl_detect_model_pricing", script)
    mod = _importlib_util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _load_pricing_detector._mod = mod
    return mod


def _merge_pricing_payload() -> dict:
    """Layer detected config variants over the seeded official registry.

    Detected entries win on actual variant/provider data; the seeded registry
    fills in any canonical families that have a first-party official price even
    when not currently in config."""
    detector = _load_pricing_detector()
    registry = {}
    if _os.path.exists(_PRICING_REGISTRY_PATH):
        try:
            with open(_PRICING_REGISTRY_PATH, "r", encoding="utf-8") as f:
                registry = (json.load(f) or {}).get("canonical_models", {})
        except Exception as _e:
            print(f"[pricing] failed to read registry: {_e}")
    detected = {}
    if _os.path.exists(_PRICING_DETECTED_PATH):
        try:
            with open(_PRICING_DETECTED_PATH, "r", encoding="utf-8") as f:
                detected = (json.load(f) or {}).get("canonical_models", {})
        except Exception as _e:
            print(f"[pricing] failed to read detected: {_e}")

    merged = {}
    # Seeded official entries first (so they always appear).
    for key, entry in registry.items():
        merged[key] = dict(entry)
    # Detected entries: they carry real variant/provider data; keep their
    # detected status/price but preserve any official price from the registry
    # when the detector itself flagged alias_unverified with null prices.
    for key, entry in detected.items():
        cur = dict(entry)
        reg_entry = registry.get(key)
        if reg_entry:
            if cur.get("input_per_1m") is None and reg_entry.get("input_per_1m") is not None:
                for fld in ("input_per_1m", "output_per_1m", "cache_hit_per_1m", "cache_write_per_1m", "source_url"):
                    if cur.get(fld) is None:
                        cur[fld] = reg_entry.get(fld)
            # If the registry says this family is genuinely official, honor it.
            if reg_entry.get("source_status") == "official" and cur.get("source_status") != "official":
                cur["source_status"] = "official"
        merged[key] = cur

    return {
        "canonical_models": merged,
        "detected_path": _PRICING_DETECTED_PATH if _os.path.exists(_PRICING_DETECTED_PATH) else None,
        "registry_path": _PRICING_REGISTRY_PATH if _os.path.exists(_PRICING_REGISTRY_PATH) else None,
    }


@app.get("/api/pricing/registry")
async def pricing_registry():
    """Return the merged canonical pricing registry (seeded + detected)."""
    return JSONResponse(_merge_pricing_payload())


@app.post("/api/pricing/detect")
async def pricing_detect():
    """Re-run the offline detector over config.yaml and persist the result."""
    try:
        detector = _load_pricing_detector()
        payload = detector.run_detection("config.yaml", _PRICING_DETECTED_PATH)
        merged = _merge_pricing_payload()
        merged["generated_at"] = payload.get("generated_at")
        merged["detected_count"] = len(payload.get("canonical_models", {}))
        return JSONResponse(merged)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# â”€â”€ Admin Auth Endpoints â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@app.post("/api/auth/login")
async def auth_login(request: Request):
    """Validate password and create admin session."""
    try:
        body = await request.json()
        password = body.get("password", "")
    except Exception:
        return JSONResponse({"error": "Invalid request body"}, status_code=400)
    
    if not _is_admin_auth_enabled():
        # Protection not enabled â€” consider authenticated
        return JSONResponse({"authenticated": True, "auth_required": False})
    
    if not _validate_admin_password(password):
        return JSONResponse({"error": "Invalid password"}, status_code=401)
    
    token = _create_admin_session()
    response = JSONResponse({"authenticated": True, "auth_required": True})
    response.set_cookie(
        "bsl_admin_session",
        token,
        httponly=True,
        samesite="lax",
        max_age=_ADMIN_SESSION_TTL,
        path="/",
    )
    return response


@app.post("/api/auth/logout")
async def auth_logout(request: Request):
    """Invalidate admin session and clear cookie."""
    session_token = request.cookies.get("bsl_admin_session")
    if session_token:
        _invalidate_admin_session(session_token)
    response = JSONResponse({"authenticated": False})
    response.delete_cookie("bsl_admin_session", path="/")
    return response


@app.get("/api/auth/status")
async def auth_status(request: Request):
    """Check if admin auth is required and if current session is valid."""
    if not _is_admin_auth_enabled():
        return JSONResponse({"auth_required": False, "authenticated": True})
    session_token = request.cookies.get("bsl_admin_session")
    authenticated = _is_valid_admin_session(session_token)
    return JSONResponse({"auth_required": True, "authenticated": authenticated})


# â”€â”€ System Shutdown Endpoint â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@app.post("/api/system/shutdown")
async def system_shutdown(request: Request):
    """Shutdown the BSL Router backend process safely.
    
    Only kills the current Python process (uvicorn worker). Does NOT
    touch sibling applications, MITM proxy, or any other processes.
    """
    # Verify admin session if auth is enabled
    if _is_admin_auth_enabled():
        session_token = request.cookies.get("bsl_admin_session")
        if not _is_valid_admin_session(session_token):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    def _delayed_shutdown():
        """Wait for response flush, collect the process-tree PIDs by walking the
        PARENT chain (CommandLine is empty when launched via start-all.bat, so we
        must rely on ParentProcessId + Name, which are always readable), then
        spawn a detached taskkill batch that survives our own death, and exit.
        """
        time.sleep(1.0)
        import subprocess as _sp
        import tempfile as _tempfile

        my_pid = _os_module.getpid()

        # 1. Build a full {pid: (ppid, name)} map via WMIC (Name/PPID always readable)
        pid_info = {}
        try:
            wmic = _sp.run(
                ['wmic', 'process', 'get', 'ProcessId,ParentProcessId,Name', '/format:csv'],
                capture_output=True, text=True, timeout=8
            )
            for row in wmic.stdout.splitlines():
                cols = row.strip().split(',')
                # CSV columns: Node,Name,ParentProcessId,ProcessId
                if len(cols) >= 4 and cols[3].strip().isdigit() and cols[2].strip().isdigit():
                    pid_v = int(cols[3].strip())
                    ppid_v = int(cols[2].strip())
                    name_v = cols[1].strip().lower()
                    pid_info[pid_v] = (ppid_v, name_v)
        except Exception:
            pid_info = {}

        # 2. Walk UP from our own PID, collecting every python/uvicorn ancestor.
        #    Stop when the parent is no longer python/uvicorn (i.e. the cmd/terminal).
        kill_pids = [my_pid]
        cur = my_pid
        for _ in range(10):
            entry = pid_info.get(cur)
            if not entry:
                break
            ppid, _name = entry
            parent = pid_info.get(ppid)
            if parent and ('python' in parent[1] or 'uvicorn' in parent[1]):
                kill_pids.append(ppid)
                cur = ppid
            else:
                break

        # 3. Write a detached killer .bat: taskkill the topmost ancestor with /T
        #    (kills the whole subtree) plus each collected PID as belt-and-suspenders.
        bat_path = _os_module.path.join(_tempfile.gettempdir(), 'bsl_kill.bat')
        try:
            with open(bat_path, 'w') as f:
                f.write('@echo off\n')
                f.write('timeout /t 1 /nobreak >nul\n')
                # Kill topmost ancestor first WITH tree flag, then each PID explicitly.
                f.write(f'taskkill /F /T /PID {kill_pids[-1]} 2>nul\n')
                for pid in kill_pids:
                    f.write(f'taskkill /F /PID {pid} 2>nul\n')
                f.write('del "%~f0"\n')
            _sp.Popen(
                ['cmd', '/c', bat_path],
                creationflags=0x08000000,  # CREATE_NO_WINDOW
                stdout=_sp.DEVNULL,
                stderr=_sp.DEVNULL,
                stdin=_sp.DEVNULL,
            )
        except Exception:
            pass

        _os_module._exit(0)
    
    _threading.Thread(target=_delayed_shutdown, daemon=True).start()
    return JSONResponse({"status": "shutting_down", "message": "BSL Router is shutting down."})


_STATIC_DIR = _os_module.path.join(_os_module.path.dirname(_os_module.path.abspath(__file__)), "static")
app.mount("/admin", StaticFiles(directory=_STATIC_DIR, html=True), name="admin")

@app.post("/api/verify-key")
async def verify_key(request: Request):
    """Probe a provider's models endpoint to verify an API key is valid."""
    config = cs_get_config()
    try:
        body = await request.json()
        provider_id = (body.get("provider_id") or "").strip()
        provider_format = (body.get("format") or "").strip().lower()
        api_key = body.get("api_key", "")
        base_url = (body.get("base_url") or "").rstrip("/")
        if not api_key:
            return JSONResponse({"ok": False, "error": "No API key provided"})
        if not base_url:
            return JSONResponse({"ok": False, "error": "No base URL provided"})

        # P0-3 FIX: resolve provider config BEFORE scanning
        provider_cfg = config.get("providers", {}).get(provider_id, {})
        effective_format = provider_format or str(provider_cfg.get("format", "")).lower()

        # Security pre-scan (block before probing suspicious URLs)
        try:
            from app.security.key_scanner import scan_single_key
            scan_findings = scan_single_key(api_key, base_url, effective_format)
            blocks = [f for f in scan_findings if f.severity == "block"]
            if blocks:
                return JSONResponse({
                    "ok": False,
                    "error": "Security scan failed",
                    "scan_findings": [f.__dict__ for f in scan_findings],
                    "blocked": True,
                })
        except Exception:
            pass  # Fail-open: don't block verification if scanner has a bug

        is_custom = provider_cfg.get("type") in ("custom", "image_custom", "video_custom") or not provider_cfg
        is_custom_text = (
            effective_format in {"openai", "openai-responses", "anthropic", "gemini"}
            and is_custom
        )
        is_custom_image_video = (
            effective_format in {"openai-image", "openai-video"}
            and is_custom
        )
        probe_url = build_custom_models_probe_url(base_url) if (is_custom_text or is_custom_image_video) else f"{base_url}/models"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        try:
            resp = await http_client.get(probe_url, headers=headers, timeout=30.0)
            ok = resp.status_code in (200, 206)
            data = None
            if ok:
                try:
                    data = resp.json()
                except (ValueError, KeyError):
                    pass
            return JSONResponse({"ok": ok, "status": resp.status_code, "data": data})
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

@app.post("/api/test-model")
async def test_model(request: Request):
    """Smoke-test a configured provider/model through BSL's normal routing path."""
    config = cs_get_config()
    try:
        body = await request.json()
        provider = (body.get("provider") or "").strip()
        model = (body.get("model") or "").strip()
        if not provider or not model:
            return JSONResponse({"ok": False, "error": "provider and model required"}, status_code=400)
        if provider not in config.get("providers", {}):
            return JSONResponse({"ok": False, "error": f"Unknown provider: {provider}"}, status_code=404)

        # Antigravity uses Gemini-format API (:generateContent), NOT OpenAI /chat/completions.
        # The standard _process_chat_completion builds the wrong upstream URL → 404.
        if provider == "antigravity":
            return await _test_antigravity_model(provider, model)

        probe_body = {
            "model": f"{provider}/{model}",
            "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
            "max_tokens": 8,
            "temperature": 0,
            "stream": False,
        }
        resp = await _process_chat_completion(probe_body)
        status = getattr(resp, "status_code", 200)
        if status >= 400:
            raw = getattr(resp, "body", b"")
            try:
                detail = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
            except Exception:
                detail = f"HTTP {status}"
            return JSONResponse({"ok": False, "status": status, "error": detail[:800]}, status_code=status)
        return JSONResponse({"ok": True, "status": status})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

async def _test_antigravity_model(provider: str, model: str):
    """Two-phase test: (1) OAuth auth, (2) real inference through upstream provider.

    Phase 1 — validate the stored OAuth token (no upstream call).
    Phase 2 — route a probe through _process_chat_completion using the
    antigravity integration mapping's combo alias, proving the full
    Gemini→OpenAI→upstream chain works and returns real In/Out tokens.
    """
    config = cs_get_config()
    _req_id = obs.log_request_start(provider, model, config, stream=False, client="antigravity-test")
    _t0 = time.time()
    try:
        prov = config.get("providers", {}).get(provider)
        if not prov:
            err = "Antigravity provider not configured"
            obs.log_request(provider, model, 404, 0, 0, 0, 0, config, error_msg=err, request_id=_req_id)
            return JSONResponse({"ok": False, "error": err}, status_code=404)

        active_conn, _ = resolve_active_connection(config, provider, model)
        if not active_conn:
            err = "Antigravity has no active connections"
            obs.log_request(provider, model, 500, 0, 0, 0, 0, config, error_msg=err, request_id=_req_id)
            return JSONResponse({"ok": False, "error": err}, status_code=500)

        from app.oauth import ensure_fresh_token
        token = await ensure_fresh_token(provider, active_conn, prov)
        if not token:
            err = "Antigravity has no stored OAuth token"
            obs.log_request(provider, model, 500, 0, 0, 0, 0, config, error_msg=err, request_id=_req_id)
            return JSONResponse({"ok": False, "error": err}, status_code=500)

        # Phase 1: OAuth token validated (ensure_fresh_token succeeded).
        # Phase 2: Route probe through _process_chat_completion using the
        # integration mapping's combo alias.
        integration = _antigravity_integration_settings()
        combo_alias = integration.get("mappings", {}).get(model)
        if not combo_alias:
            combo_alias = next(iter(integration.get("mappings", {}).values())) if integration.get("mappings") else None

        if not combo_alias:
            err = f"No integration mapping for '{model}' and no fallback"
            obs.log_request(provider, model, 500, time.time() - _t0, 0, 0, 0, config, error_msg=err, request_id=_req_id)
            return JSONResponse({"ok": False, "error": err}, status_code=500)

        # Build the same OpenAI-format probe body non-antigravity providers use.
        probe_body = {
            "model": combo_alias,
            "messages": [{"role": "user", "content": "OK"}],
            "max_tokens": 8,
            "temperature": 0,
            "stream": False,
        }
        resp = await _process_chat_completion(probe_body)
        status = getattr(resp, "status_code", 200)
        _t1 = time.time()

        if status >= 400:
            raw = getattr(resp, "body", b"")
            try:
                detail = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
            except Exception:
                detail = f"HTTP {status}"
            obs.log_request(provider, model, status, _t1 - _t0, 0, 0, 0, config,
                            error_msg=detail[:800], request_id=_req_id)
            return JSONResponse({"ok": False, "status": status, "error": detail[:800]}, status_code=status)

        # Extract token counts
        in_tokens = out_tokens = 0
        reply = ""
        try:
            raw = getattr(resp, "body", b"")
            if raw:
                j = json.loads(raw)
                usage = j.get("usage", {}) or j.get("usageMetadata", {})
                in_tokens = (usage.get("prompt_tokens") or usage.get("promptTokenCount")
                             or usage.get("input_tokens") or 0)
                out_tokens = (usage.get("completion_tokens") or usage.get("candidatesTokenCount")
                              or usage.get("output_tokens") or 0)
                if isinstance(in_tokens, str):
                    try: in_tokens = int(in_tokens)
                    except ValueError: in_tokens = 0
                if isinstance(out_tokens, str):
                    try: out_tokens = int(out_tokens)
                    except ValueError: out_tokens = 0
                choices = j.get("choices", [])
                if choices and isinstance(choices, list):
                    reply = choices[0].get("message", {}).get("content", "") or ""
        except Exception:
            pass

        obs.log_request(provider, model, status, _t1 - _t0,
                        in_tokens, out_tokens, in_tokens + out_tokens,
                        config, request_id=_req_id)
        return JSONResponse({
            "ok": True,
            "status": status,
            "combo_alias": combo_alias,
            "in_tokens": in_tokens,
            "out_tokens": out_tokens,
            "total_tokens": in_tokens + out_tokens,
            "reply": reply[:100],
        })
    except Exception as e:
        obs.log_request(provider, model, 500, time.time() - _t0, 0, 0, 0, config, error_msg=str(e), request_id=_req_id)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)



# â”€â”€â”€ MITM Status â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

import os as _os
import subprocess as _subprocess
import platform as _platform

def _mitmproxy_cert_path() -> str:
    home = _os.path.expanduser("~")
    return _os.path.join(home, ".mitmproxy", "mitmproxy-ca-cert.pem")


def _project_root() -> str:
    return _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))


def _classify_mitm_owner(process: dict) -> dict:
    """Return safe listener diagnostics with fail-closed BSL classification."""
    pid = int(process.get("ProcessId") or process.get("pid") or 0)
    name = str(process.get("Name") or process.get("name") or "unknown")
    lowered_name = name.lower()
    command_line = str(process.get("CommandLine") or "").lower()
    executable_path = str(process.get("ExecutablePath") or "").lower()
    evidence = f"{command_line} {executable_path}"
    root = _project_root().lower()

    is_node = lowered_name in {"node", "node.exe"}
    is_mitmdump = lowered_name in {"mitmdump", "mitmdump.exe"}
    is_python = lowered_name in {"python", "python.exe", "pythonw", "pythonw.exe"}
    root_variants = {root, root.replace("\\", "/")}
    attributable = any(candidate in evidence for candidate in root_variants) and (
        "mitmdump" in evidence or "app\\mitm.py" in evidence or "app/mitm.py" in evidence
    )

    # Elevated process metadata can be unreadable. The dedicated mitmdump process
    # name is then accepted as the narrow fallback; generic Python remains
    # unverified and therefore non-BSL.
    metadata_available = bool(command_line.strip() or executable_path.strip())
    is_bsl_mitm = bool(
        not is_node and (
            (is_mitmdump and (attributable or not metadata_available))
            or (is_python and attributable)
        )
    )
    parent_pid = int(process.get("ParentProcessId") or process.get("parent_pid") or 0) or None
    parent_chain = process.get("ParentChain") or process.get("parent_chain") or []
    return {
        "pid": pid,
        "name": name,
        "parent_pid": parent_pid,
        "parent_chain": parent_chain if isinstance(parent_chain, list) else [],
        "is_bsl_mitm": is_bsl_mitm,
    }


def _listener_owners(port: int) -> list:
    """Return unique process owners listening on a Windows TCP port.

    Inspection failures are raised so callers cannot confuse an unknown state
    with an empty port.
    """
    script = (
        f"$pids = @(Get-NetTCPConnection -State Listen -ErrorAction Stop | "
        f"Where-Object {{ $_.LocalPort -eq {int(port)} }} | "
        "Select-Object -ExpandProperty OwningProcess -Unique); "
        "$items = foreach ($pidValue in $pids) { "
        "$proc = Get-CimInstance Win32_Process -Filter \"ProcessId=$pidValue\" -ErrorAction SilentlyContinue; "
        "$chain = @(); $cursor = $proc; $depth = 0; "
        "while ($cursor -and $cursor.ParentProcessId -gt 0 -and $depth -lt 8) { "
        "$parent = Get-CimInstance Win32_Process -Filter \"ProcessId=$($cursor.ParentProcessId)\" -ErrorAction SilentlyContinue; "
        "if (-not $parent) { break }; "
        "$chain += [pscustomobject]@{ pid=[int]$parent.ProcessId; name=$parent.Name }; "
        "$cursor = $parent; $depth++ }; "
        "if ($proc) { [pscustomobject]@{ ProcessId=[int]$proc.ProcessId; Name=$proc.Name; "
        "ParentProcessId=[int]$proc.ParentProcessId; ParentChain=@($chain); "
        "ExecutablePath=$proc.ExecutablePath; CommandLine=$proc.CommandLine } } "
        "else { [pscustomobject]@{ ProcessId=[int]$pidValue; Name='unknown'; ParentProcessId=$null; ParentChain=@(); ExecutablePath=$null; CommandLine=$null } } }; "
        "ConvertTo-Json -InputObject @($items) -Depth 5 -Compress"
    )
    try:
        result = _subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True, text=True, timeout=8
        )
    except Exception as exc:
        raise RuntimeError(f"Port {port} inspection failed: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "PowerShell returned no diagnostics").strip()
        raise RuntimeError(f"Port {port} inspection failed: {detail[:600]}")
    try:
        raw = json.loads(result.stdout.strip() or "[]")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Port {port} inspection returned invalid JSON") from exc
    processes = raw if isinstance(raw, list) else [raw]
    owners = {}
    for process in processes:
        owner = _classify_mitm_owner(process)
        if owner["pid"] > 0:
            owners[owner["pid"]] = owner
    return list(owners.values())


_MITM_SUPERVISOR_LOCK = _threading.Lock()
_MITM_SUPERVISOR = {
    "desired_running": False,
    "tracked_pid": None,
    "last_state": None,
    "events": [],
}


def _record_mitm_transition(previous: Optional[str], current: str, runtime: dict) -> Optional[str]:
    if previous == current:
        return None
    transition = f"{previous or 'initial'}->{current}"
    _MITM_SUPERVISOR["events"].append({
        "ts": time.time(),
        "transition": transition,
        "owners": [{"pid": owner["pid"], "name": owner["name"]} for owner in runtime.get("owners", [])],
    })
    _MITM_SUPERVISOR["events"] = _MITM_SUPERVISOR["events"][-20:]
    return transition


def _reconcile_mitm_runtime(runtime: dict) -> dict:
    """Passively reconcile observed owners against the last verified BSL PID."""
    with _MITM_SUPERVISOR_LOCK:
        desired = bool(_MITM_SUPERVISOR["desired_running"])
        tracked_pid = _MITM_SUPERVISOR["tracked_pid"]
        bsl_pids = [owner["pid"] for owner in runtime.get("owners", []) if owner.get("is_bsl_mitm")]
        exclusive = bool(runtime.get("server")) and runtime.get("conflict") is False and len(bsl_pids) == 1
        tracked_owned = exclusive and (tracked_pid is None or tracked_pid == bsl_pids[0])
        ownership_lost = desired and not tracked_owned and runtime.get("state") != "unknown"

        if runtime.get("state") == "unknown":
            state = "unknown"
        elif tracked_owned:
            state = "owned"
        elif ownership_lost:
            state = "ownership-lost"
        elif runtime.get("port_occupied"):
            state = "foreign-owned"
        else:
            state = "stopped"

        previous = _MITM_SUPERVISOR["last_state"]
        transition = _record_mitm_transition(previous, state, runtime)
        _MITM_SUPERVISOR["last_state"] = state
        return {
            **runtime,
            "state": state,
            "desired_running": desired,
            "tracked_pid": tracked_pid,
            "ownership_verified": tracked_owned,
            "ownership_lost": ownership_lost,
            "transition": transition,
            "observed_at": time.time(),
            "lifecycle_events": list(_MITM_SUPERVISOR["events"]),
        }


def _set_mitm_supervisor_target(desired_running: bool, runtime: dict) -> dict:
    with _MITM_SUPERVISOR_LOCK:
        _MITM_SUPERVISOR["desired_running"] = desired_running
        bsl_pids = [owner["pid"] for owner in runtime.get("owners", []) if owner.get("is_bsl_mitm")]
        _MITM_SUPERVISOR["tracked_pid"] = bsl_pids[0] if desired_running and len(bsl_pids) == 1 else None
    return _reconcile_mitm_runtime(runtime)


def _mitm_runtime_status(port: Optional[int] = None) -> dict:
    config = cs_get_config()
    mitm_port = int(port if port is not None else config.get("mitm_port", 443))
    try:
        owners = _listener_owners(mitm_port)
    except Exception as exc:
        return {
            "state": "unknown", "inspection_error": str(exc), "server": False,
            "port_occupied": None, "owners": [], "conflict": None, "port": mitm_port,
        }
    server = any(owner["is_bsl_mitm"] for owner in owners)
    non_bsl_present = any(not owner["is_bsl_mitm"] for owner in owners)
    return {
        "state": "running" if server else ("occupied" if owners else "stopped"),
        "inspection_error": None,
        "server": server,
        "port_occupied": bool(owners),
        "owners": owners,
        "conflict": bool(owners) and (not server or non_bsl_present),
        "port": mitm_port,
    }


def _is_cert_trusted_windows() -> bool:
    """Check if the mitmproxy CA cert is installed in the Windows trust store."""
    try:
        result = _subprocess.run(
            ["certutil", "-verifystore", "Root", "mitmproxy"],
            capture_output=True, text=True, timeout=5
        )
        return result.returncode == 0
    except Exception:
        return False

@app.get("/api/mitm/status")
async def mitm_status():
    runtime, cert_trusted = await asyncio.gather(
        asyncio.to_thread(_mitm_runtime_status),
        asyncio.to_thread(_is_cert_trusted_windows) if _platform.system() == "Windows" else asyncio.to_thread(lambda: _os.path.exists(_mitmproxy_cert_path())),
    )
    cert_path = _mitmproxy_cert_path()
    cert_exists = _os.path.exists(cert_path)
    runtime = _reconcile_mitm_runtime(runtime)
    validated = MitmRuntimeStatus.model_validate(runtime).model_dump()
    return JSONResponse({
        "cert": cert_exists,
        "trusted": cert_trusted,
        "cert_path": cert_path,
        **validated,
    })

# â”€â”€â”€ Hosts File Auto-Edit â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

HOSTS_PATH = r"C:\Windows\System32\drivers\etc\hosts" if _platform.system() == "Windows" else "/etc/hosts"

@app.post("/api/mitm/hosts")
async def edit_hosts(request: Request):
    """Add or remove hosts file entries for a given IDE's domains."""
    try:
        body = await request.json()
        ide = body.get("ide")  # "antigravity" | "copilot" | "kiro"
        action = body.get("action", "add")  # "add" | "remove"

        IDE_DOMAINS = {
            "antigravity": ["daily-cloudcode-pa.googleapis.com"],  # auth domain cloudcode-pa MUST NOT be intercepted â€” breaks login
            "copilot":     ["api.individual.githubcopilot.com"],
            "kiro":        ["runtime.us-east-1.kiro.dev", "q.us-east-1.amazonaws.com", "codewhisperer.us-east-1.amazonaws.com"],
        }
        domains = IDE_DOMAINS.get(ide)
        if not domains:
            return JSONResponse({"ok": False, "error": f"Unknown IDE: {ide}"}, status_code=400)

        try:
            with open(HOSTS_PATH, "r") as f:
                lines = f.readlines()
        except PermissionError:
            return JSONResponse({"ok": False, "error": "Permission denied â€” run BSL Router as Administrator to edit hosts file."}, status_code=403)

        BSL_TAG = "# bsl-router"
        modified = False

        if action == "add":
            existing = {line.split()[1] for line in lines if line.strip() and not line.startswith("#") and len(line.split()) >= 2}
            for domain in domains:
                if domain not in existing:
                    lines.append(f"127.0.0.1 {domain} {BSL_TAG}\n")
                    modified = True
        elif action == "remove":
            original_len = len(lines)
            lines = [l for l in lines if not (BSL_TAG in l and any(d in l for d in domains))]
            if len(lines) != original_len:
                modified = True

        if not modified:
            # If the domains are already present (or already removed), skip writing.
            # This prevents throwing a PermissionError for users who manually edited
            # their hosts file but are running BSL Router as non-admin.
            return JSONResponse({"ok": True, "domains": domains, "action": action, "note": "already present"})

        try:
            with open(HOSTS_PATH, "w") as f:
                f.writelines(lines)
            return JSONResponse({"ok": True, "domains": domains, "action": action})
        except PermissionError:
            return JSONResponse({"ok": False, "error": "Permission denied â€” run BSL Router as Administrator."}, status_code=403)

    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

# â”€â”€â”€ MITM Process Lifecycle (verified via bslrouter.ps1) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

_MITM_LAUNCH_TIMEOUT_SECONDS = 45
_MITM_VERIFY_TIMEOUT_SECONDS = 8.0
_MITM_VERIFY_INTERVAL_SECONDS = 0.25
_MITM_LIFECYCLE_LOCK = asyncio.Lock()


def _mitm_launcher_path() -> str:
    return _os_module.path.join(_project_root(), "scripts", "bslrouter.ps1")


def _mitm_is_admin() -> bool:
    """Return whether this API process can safely control the privileged port."""
    if _platform.system() != "Windows":
        return True
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _run_mitm_launcher(action: str, *extra_args):
    """Run the PowerShell lifecycle command without inheriting its output pipes.

    `Start-Process -Background` returns after it launches mitmdump, but Windows
    descendants can retain inherited capture handles. Waiting on `communicate()`
    would then wait for the long-lived proxy rather than the bounded launcher.
    Temporary files preserve staged launcher diagnostics without that pipe leak.
    """
    import tempfile

    launcher = _mitm_launcher_path()
    if not _os_module.path.exists(launcher):
        raise FileNotFoundError(f"Launcher not found: {launcher}")
    cmd = [
        "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", launcher, action, "-Mitm",
    ]
    if extra_args:
        cmd.extend(extra_args)
    if action in {"start", "restart"}:
        cmd.append("-Background")
    with (
        tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as stdout_file,
        tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as stderr_file,
    ):
        process = _subprocess.Popen(
            cmd,
            cwd=_project_root(),
            stdout=stdout_file,
            stderr=stderr_file,
            creationflags=0x08000000 if _platform.system() == "Windows" else 0,
        )
        try:
            returncode = process.wait(timeout=_MITM_LAUNCH_TIMEOUT_SECONDS)
        except _subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=5)
            except _subprocess.TimeoutExpired:
                pass
            raise
        stdout_file.seek(0)
        stderr_file.seek(0)
        return _subprocess.CompletedProcess(
            cmd,
            returncode,
            stdout=stdout_file.read(),
            stderr=stderr_file.read(),
        )


def _poll_mitm_runtime(expected_running: bool) -> dict:
    deadline = time.monotonic() + _MITM_VERIFY_TIMEOUT_SECONDS
    status = _mitm_runtime_status()
    while time.monotonic() < deadline:
        if status["state"] == "unknown":
            return status
        verified = status["server"] and not status["conflict"] if expected_running else not status["port_occupied"]
        if verified:
            return status
        time.sleep(_MITM_VERIFY_INTERVAL_SECONDS)
        status = _mitm_runtime_status()
    return status


def _stage_error(code: str, message: str, runtime: dict, status_code: int = 500) -> JSONResponse:
    if runtime.get("inspection_error"):
        message = runtime["inspection_error"]
        status_code = 503
    return JSONResponse(
        {"ok": False, "code": code, "error": message, **runtime},
        status_code=status_code,
    )


def _admin_required(runtime: dict) -> JSONResponse:
    return _stage_error(
        "admin_required",
        "MITM lifecycle requires BSL Router to run as Administrator.",
        runtime,
        status_code=403,
    )


def _launcher_failure(result, runtime: dict) -> JSONResponse:
    output = (result.stderr or result.stdout or "MITM launcher failed").strip()
    stage_match = re.search(r"ERROR\[([a-z_]+)\]", output)
    code = stage_match.group(1) if stage_match else (
        "port_not_empty" if runtime.get("port_occupied") else "launch_failed"
    )
    if runtime.get("owners"):
        owner_text = ", ".join(f"{owner['name']} (PID {owner['pid']})" for owner in runtime["owners"])
        output = f"{output} Current owners: {owner_text}"
    return _stage_error(code, output[:1200], runtime)


async def _start_mitm_locked(evict_foreign: bool = False) -> JSONResponse:
    """Clear the MITM port, launch BSL, then verify ownership.

    ``evict_foreign`` is the CONSENT GATE. When False (the watchdog's path) a
    foreign owner of the port — e.g. 9Router's node.exe — is left strictly
    alone: we refuse to start rather than steal the port. Only an explicit user
    action (the Start Integration button) passes True, which forwards
    ``-EvictForeign`` to the launcher and permits eviction.

    EVICTION STRATEGY (evict_foreign=True):
    ``force_kill_mitm_port`` (taskkill /F /T + verify/retry loop) runs BEFORE
    the PS1 launcher so the port is guaranteed clear before mitmdump starts.
    The PS1 launcher's Stop-AllListeners then becomes a no-op fallback.
    """
    runtime = await asyncio.to_thread(_mitm_runtime_status)
    if runtime.get("inspection_error"):
        return _stage_error("ownership_not_verified", "MITM port state is unknown.", runtime)

    if runtime["server"] and not runtime["conflict"]:
        runtime = await asyncio.to_thread(_set_mitm_supervisor_target, True, runtime)
        return JSONResponse({"ok": True, "message": "BSL MITM already verified running.", **runtime})

    # Admin gate FIRST: never kill anything we cannot restart (audit F6).
    if not _mitm_is_admin():
        return _admin_required(runtime)

    # Consent gate: never evict a foreign proxy without an explicit user action.
    if not evict_foreign:
        foreign = [o for o in runtime.get("owners") or [] if not o.get("is_bsl_mitm")]
        if foreign:
            names = ", ".join(f"{o.get('name')}({o.get('pid')})" for o in foreign)
            return _stage_error(
                "foreign_owner_present",
                f"Port {runtime.get('port')} is held by another process ({names}). "
                "Refusing to evict it without explicit consent. "
                "Press Start Integration to take over.",
                runtime,
                status_code=409,
            )

    # Aggressive pre-clear BEFORE launch. mitm_start clears desired_running so
    # the watchdog cannot respawn while we kill; capture it so every failure
    # path restores the pre-start watchdog intent (audit F5/F1).
    with _MITM_SUPERVISOR_LOCK:
        prior_desired = bool(_MITM_SUPERVISOR["desired_running"])
        prior_tracked = _MITM_SUPERVISOR["tracked_pid"]

    def _restore_supervisor() -> None:
        with _MITM_SUPERVISOR_LOCK:
            _MITM_SUPERVISOR["desired_running"] = prior_desired
            _MITM_SUPERVISOR["tracked_pid"] = prior_tracked

    if evict_foreign and runtime.get("port_occupied"):
        # Import the MODULE, not the function, and dispatch through the module
        # attribute. A function-local `from ... import force_kill_mitm_port`
        # rebinds the callable at call time, which silently defeats
        # monkeypatch.setattr(mitm_kill_mod, "force_kill_mitm_port", ...) in the
        # tests and lets the suite issue REAL taskkill commands against port 443.
        from app.utils import mitm_kill as _mitm_kill_mod
        kill_ok, kill_detail = await asyncio.to_thread(
            _mitm_kill_mod.force_kill_mitm_port, runtime.get("port", 443)
        )
        if not kill_ok:
            _restore_supervisor()
            verified = await asyncio.to_thread(_mitm_runtime_status)
            return _stage_error(
                "kill_failed",
                f"Could not clear port {runtime.get('port', 443)} before starting MITM: {kill_detail}",
                verified,
            )
        runtime = await asyncio.to_thread(_mitm_runtime_status)

    try:
        launcher_args = ("-EvictForeign",) if evict_foreign else ()
        result = await asyncio.to_thread(_run_mitm_launcher, "start", *launcher_args)
        if result.returncode != 0:
            _restore_supervisor()
            verified = await asyncio.to_thread(_mitm_runtime_status)
            return _launcher_failure(result, verified)
        verified = await asyncio.to_thread(_poll_mitm_runtime, True)
        if not verified["server"] or verified["conflict"]:
            _restore_supervisor()
            return _stage_error(
                "ownership_not_verified",
                "BSL MITM did not acquire exclusive ownership of the configured port.",
                verified,
            )
        verified = await asyncio.to_thread(_set_mitm_supervisor_target, True, verified)
        return JSONResponse({"ok": True, "message": "BSL MITM verified running.", **verified})
    except _subprocess.TimeoutExpired:
        _restore_supervisor()
        verified = await asyncio.to_thread(_mitm_runtime_status)
        return _stage_error("launch_failed", "MITM launcher timed out.", verified)
    except Exception as exc:
        _restore_supervisor()
        verified = await asyncio.to_thread(_mitm_runtime_status)
        return _stage_error("launch_failed", str(exc), verified)


@app.post("/api/mitm/start")
async def mitm_start():
    """Atomically clear the MITM port, launch BSL, and verify exclusive ownership.

    This is the ONLY path allowed to evict a foreign proxy, because it is only
    reachable from an explicit user action (the Start Integration button).

    WATCHDOG GUARD: desired_running is set to False BEFORE the kill phase so
    the watchdog (5s interval) cannot respawn mitmdump while we're clearing
    the port.  _start_mitm_locked sets it back to True on success.
    tracked_pid is deliberately NOT cleared here (audit NIT-10): clearing it
    would transiently un-verify a still-running BSL mitmdump and flash the
    orange warning mid-start; _start_mitm_locked re-tracks after launch.
    """
    async with _MITM_LIFECYCLE_LOCK:
        with _MITM_SUPERVISOR_LOCK:
            _MITM_SUPERVISOR["desired_running"] = False
        return await _start_mitm_locked(evict_foreign=True)


@app.post("/api/mitm/stop")
async def mitm_stop(force: bool = False):
    """Stop MITM. When force=True, directly kills ALL listeners on the MITM port
    using raw taskkill before the standard stop flow. This bypasses the PS1
    launcher's ownership verification and works even when BSL Router isn't
    elevated (taskkill/F works for same-user processes). Without this, a zombie
    mitmdump from a previous crash survives "Stop Integration" and keeps the
    IDE frozen because the Antigravity Integration tab's stop-full route hits
    the admin gate (line 2505) before it can kill anything."""
    async with _MITM_LIFECYCLE_LOCK:
        # When force=True, nuke EVERYTHING on the MITM port directly before
        # any verification or launcher call. This is the ONLY reliable way to
        # kill zombie mitmdump processes that survive "Stop Integration".
        #
        # CRITICAL ORDER: set desired_running=False FIRST, then kill.
        # If we kill first, the watchdog loop (5s interval, _mitm_watchdog_loop)
        # sees the process died while desired_running=True and immediately
        # respawns it — making port 443 unkillable. Setting desired=False first
        # tells the watchdog to stay idle while we clear the port.
        if force:
            with _MITM_SUPERVISOR_LOCK:
                _MITM_SUPERVISOR["desired_running"] = False
                _MITM_SUPERVISOR["tracked_pid"] = None
            # Dispatch through the module attribute so tests can patch it.
            # See the note at the evict_foreign call site above.
            from app.utils import mitm_kill as _mitm_kill_mod
            kill_ok, kill_detail = await asyncio.to_thread(_mitm_kill_mod.force_kill_mitm_port)
            if not kill_ok:
                return _stage_error("kill_failed", f"Could not force-kill port: {kill_detail}", {})

        runtime = await asyncio.to_thread(_mitm_runtime_status)
        if runtime.get("inspection_error"):
            return _stage_error("ownership_not_verified", "MITM port state is unknown.", runtime)
        if not runtime["port_occupied"]:
            runtime = await asyncio.to_thread(_set_mitm_supervisor_target, False, runtime)
            return JSONResponse({"ok": True, "message": "MITM port verified empty.", **runtime})
        if (runtime["conflict"] or not runtime["server"]) and not force:
            return _stage_error(
                "ownership_not_verified",
                "Stop requires exclusive verified BSL MITM ownership of the configured port.",
                runtime,
            )
        if not _mitm_is_admin():
            return _admin_required(runtime)

        try:
            args = ("stop", "-ForceKill") if force else ("stop",)
            result = await asyncio.to_thread(_run_mitm_launcher, *args)
            verified = await asyncio.to_thread(_poll_mitm_runtime, False)
            if result.returncode != 0:
                return _launcher_failure(result, verified)
            if verified["port_occupied"] is not False:
                return _stage_error(
                    "port_not_empty",
                    "Configured MITM port still has listener processes after stop.",
                    verified,
                )
            verified = await asyncio.to_thread(_set_mitm_supervisor_target, False, verified)
            return JSONResponse({"ok": True, "message": "MITM port verified empty.", **verified})
        except _subprocess.TimeoutExpired:
            verified = await asyncio.to_thread(_mitm_runtime_status)
            return _stage_error("kill_failed", "MITM stop launcher timed out.", verified)
        except Exception as exc:
            verified = await asyncio.to_thread(_mitm_runtime_status)
            return _stage_error("kill_failed", str(exc), verified)

# â”€â”€â”€ Cloudflare Tunnel â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

_tunnel_process = None
_tunnel_url: str = ""

@app.post("/api/tunnel/cloudflare/start")
async def tunnel_start():
    config = cs_get_config()
    global _tunnel_process, _tunnel_url
    if _tunnel_process and _tunnel_process.poll() is None:
        return JSONResponse({"ok": True, "url": _tunnel_url, "already_running": True})
    try:
        port = config.get("server", {}).get("port", 6969)
        _tunnel_process = _subprocess.Popen(
            ["cloudflared", "tunnel", "--url", f"http://localhost:{port}"],
            stdout=_subprocess.PIPE, stderr=_subprocess.STDOUT,
            text=True, bufsize=1
        )
        # Read lines until we find the assigned URL (cloudflared prints it to stdout)
        import re as _re
        _tunnel_url = ""
        for _ in range(60):  # up to ~60 lines
            line = _tunnel_process.stdout.readline()
            if not line:
                break
            match = _re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
            if match:
                _tunnel_url = match.group(0)
                break
        if not _tunnel_url:
            return JSONResponse({"ok": False, "error": "Tunnel started but URL not detected yet. Check cloudflared is installed."})
        return JSONResponse({"ok": True, "url": _tunnel_url})
    except FileNotFoundError:
        return JSONResponse({"ok": False, "error": "cloudflared not found. Install from: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"}, status_code=500)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

@app.post("/api/tunnel/cloudflare/stop")
async def tunnel_stop():
    global _tunnel_process, _tunnel_url
    if _tunnel_process:
        _tunnel_process.terminate()
        _tunnel_process = None
        _tunnel_url = ""
    return JSONResponse({"ok": True})

@app.get("/api/tunnel/cloudflare/status")
async def tunnel_status():
    global _tunnel_process, _tunnel_url
    running = _tunnel_process is not None and _tunnel_process.poll() is None
    return JSONResponse({"running": running, "url": _tunnel_url if running else ""})

# â”€â”€â”€ Tailscale â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@app.get("/api/tunnel/tailscale/status")
async def tailscale_status():
    """Get Tailscale status and this machine's Tailscale IP/hostname."""
    config = cs_get_config()
    try:
        result = _subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            return JSONResponse({"ok": False, "error": "Tailscale not running or not installed."})
        import json as _json
        data = _json.loads(result.stdout)
        port = config.get("server", {}).get("port", 6969)
        self_node = data.get("Self", {})
        ts_ip = (self_node.get("TailscaleIPs") or [""])[0]
        hostname = self_node.get("DNSName", "").rstrip(".")
        url = f"http://{hostname}:{port}/v1" if hostname else (f"http://{ts_ip}:{port}/v1" if ts_ip else "")
        return JSONResponse({"ok": True, "ip": ts_ip, "hostname": hostname, "url": url})
    except FileNotFoundError:
        return JSONResponse({"ok": False, "error": "Tailscale CLI not found."})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})

async def _bsl_matrix_dispatch(
    body: dict,
    client_wants_anthropic: bool = False,
    client_wants_gemini: bool = False,
    request: Request = None,
):
    """Dispatch a bsl-chat request across its matrix entries in order."""
    config = cs_get_config()
    bsl_decision = route_bsl_chat(UniversalNormalizer.normalize_to_openai(body), config)
    bsl_cfg = _get_bsl_cfg(config)
    # Extract GLF via _extract_route() to handle both bare string and 3-slot dict.
    # This prevents a dict from entering the chain and being sent as body["model"].
    _glf_raw = bsl_cfg.get("global_last_fallback", "")
    _glf_primary, _glf_chain = _extract_route(_glf_raw) if _glf_raw else ("", [])
    matrix_chain = [
        bsl_decision.selected_model,
        *bsl_decision.fallback_chain,
        _glf_primary,
        *_glf_chain,
    ]
    _seen = set()
    matrix_chain = [entry for entry in matrix_chain if entry and not (entry in _seen or _seen.add(entry))]
    if not matrix_chain:
        return JSONResponse(
        {"error": "Blacksand-Chat has no configured route and no global_last_fallback."},
            status_code=503,
        )

    print(
        f"[blacksand-chat Matrix] chain={' -> '.join(matrix_chain)} "
        f"category={bsl_decision.category} "
        f"complexity={bsl_decision.complexity_level} "
        f"source={bsl_decision.source}",
        flush=True,
    )
    from app.middleware.route_registry import build_route_registry

    route_registry = build_route_registry(config, visible_only=True)
    _first_entry = matrix_chain[0] if matrix_chain else None

    # ── Recursive combo resolution for full chain observability ──
    # Resolves: matrix_entry > [combo_alias > sub_combo > ...] > provider/model
    _chain_labels, model_id = _resolve_combo_chain_segment(
        _first_entry or "", config, route_registry
    )

    # Build the >-separated chain segment for the log
    _chain_str = " > ".join(_chain_labels) if _chain_labels else ""

    canonical_line = (
        f"Blacksand-Chat > {bsl_decision.selected_model}"
        + (f" > {_chain_str}" if _chain_str else "")
        + f" > {model_id} + {bsl_decision.category} + {bsl_decision.complexity_bucket}"
    )
    # Push into the observability console_logs + persist so the Admin UI panel shows it.
    _route_id = f"bsl_{time.time_ns()}"
    _ts = datetime.now().isoformat()
    _entry = {"event": "bsl_chat_route", "text": canonical_line, "timestamp": _ts, "request_id": _route_id}
    try:
        obs.console_logs.append(_entry)
        if len(obs.console_logs) > 10000:
            obs.console_logs.pop(0)
        obs._persist_entry(obs._CONSOLE_LOG_PATH, _entry)
    except Exception as _e:
        print(f"[bsl_chat_route] obs persist failed: {_e}", flush=True)
    print(canonical_line, flush=True)

    for idx, entry in enumerate(matrix_chain):
        iter_body = copy.deepcopy(body)
        iter_body["model"] = entry
        iter_body["_bsl_original_model"] = "blacksand-chat"
        result = await _process_chat_completion(
            iter_body,
            client_wants_anthropic=client_wants_anthropic,
            client_wants_gemini=client_wants_gemini,
            request=request,
        )
        if (
            isinstance(result, JSONResponse)
            and result.status_code in _RECOVERABLE
            and idx < len(matrix_chain) - 1
        ):
            print(
                f"[blacksand-chat Matrix] '{entry}' returned HTTP {result.status_code}; "
                f"advancing to '{matrix_chain[idx + 1]}'",
                flush=True,
            )
            continue
        return result

    return JSONResponse(
        {"error": "Blacksand-Chat matrix contains no selectable entries."},
        status_code=503,
    )


async def _bsl_lite_dispatch(
    body: dict,
    client_wants_anthropic: bool = False,
    client_wants_gemini: bool = False,
    request: Request = None,
):
    """Dispatch a bsl-lite request through the 8-agent matrix.

    BSL-Lite is the non-agentic single-task router: pure task-route
    (classify → agent → model). No complexity estimation, no buckets.
    Chain: [primary, *fallbacks, global_last_fallback], deduped.
    Advances on recoverable errors only.
    """
    config = cs_get_config()
    lite_decision = route_bsl_lite(UniversalNormalizer.normalize_to_openai(body), config)
    lite_chain = [lite_decision.selected_model, *lite_decision.fallback_chain]
    _seen = set()
    lite_chain = [e for e in lite_chain if e and not (e in _seen or _seen.add(e))]
    if not lite_chain:
        return JSONResponse(
            {"error": "Blacksand-Lite has no configured route and no global_last_fallback."},
            status_code=503,
        )

    print(
        f"[blacksand-lite] chain={' -> '.join(lite_chain)} source={lite_decision.source}",
        flush=True,
    )

    # ── Route registry resolution + observability (mirrors _bsl_matrix_dispatch) ──
    from app.middleware.route_registry import build_route_registry, resolve_canonical_chain

    route_registry = build_route_registry(config, visible_only=True)
    model_id = lite_decision.selected_model

    _first_entry = lite_chain[0] if lite_chain else None

    # ── Recursive combo resolution for full chain observability ──
    # Resolves: matrix_entry > [combo_alias > sub_combo > ...] > provider/model
    _chain_labels, model_id = _resolve_combo_chain_segment(
        _first_entry or "", config, route_registry
    )

    _chain_str = " > ".join(_chain_labels) if _chain_labels else ""

    canonical_line = (
        f"Blacksand-Lite > {lite_decision.selected_model}"
        + (f" > {_chain_str}" if _chain_str else "")
        + f" > {model_id} + {lite_decision.source}"
    )
    # Push into the observability console_logs + persist so the Admin UI panel shows it.
    _route_id = f"bsl_lite_{time.time_ns()}"
    _ts = datetime.now().isoformat()
    _entry = {"event": "bsl_lite_route", "text": canonical_line, "timestamp": _ts, "request_id": _route_id}
    try:
        obs.console_logs.append(_entry)
        if len(obs.console_logs) > 10000:
            obs.console_logs.pop(0)
        obs._persist_entry(obs._CONSOLE_LOG_PATH, _entry)
    except Exception as _e:
        print(f"[bsl_lite_route] obs persist failed: {_e}", flush=True)
    print(canonical_line, flush=True)

    for idx, entry in enumerate(lite_chain):
        iter_body = copy.deepcopy(body)
        iter_body["model"] = entry
        iter_body["_bsl_original_model"] = "blacksand-lite"
        result = await _process_chat_completion(
            iter_body,
            client_wants_anthropic=client_wants_anthropic,
            client_wants_gemini=client_wants_gemini,
            request=request,
        )
        if (
            isinstance(result, JSONResponse)
            and result.status_code in _RECOVERABLE
            and idx < len(lite_chain) - 1
        ):
            print(
                f"[blacksand-lite] '{entry}' returned HTTP {result.status_code}; "
                f"advancing to '{lite_chain[idx + 1]}'",
                flush=True,
            )
            continue
        # Inject BSL-Lite routing headers for testing / observability
        _routing_hdrs = {
            "X-BSL-Lite-Category": str(lite_decision.category),
            "X-BSL-Lite-Source": str(lite_decision.source),
            "X-BSL-Lite-Selected": str(lite_decision.selected_model),
        }
        if lite_chain:
            _routing_hdrs["X-BSL-Lite-Chain"] = " → ".join(lite_chain)
        try:
            for _hk, _hv in _routing_hdrs.items():
                result.headers[_hk] = _hv
        except Exception:
            pass  # fail-safe: headers are nice-to-have for testing
        return result

    # 503 — also inject routing headers so the test suite can see the decision
    _routing_hdrs = {
        "X-BSL-Lite-Category": str(lite_decision.category),
        "X-BSL-Lite-Source": str(lite_decision.source),
        "X-BSL-Lite-Selected": str(lite_decision.selected_model),
    }
    if lite_chain:
        _routing_hdrs["X-BSL-Lite-Chain"] = " → ".join(lite_chain)
    return JSONResponse(
        {"error": "Blacksand-Lite contains no selectable entries."},
        status_code=503,
        headers={k: str(v) for k, v in _routing_hdrs.items()},
    )


async def _bsl_agentic_dispatch(
    body: dict,
    client_wants_anthropic: bool = False,
    client_wants_gemini: bool = False,
    request: Request = None,
):
    """Dispatch a blacksand-agentic request through the fast-tier agent matrix.

    Scout-first routing: classify -> agent -> 3-slot chain. Vision requests
    get a vision pre-flight chain prepended (vision models try first; if they
    fail recoverably the next agent in the chain self-answers). Advances on
    recoverable errors only. Always on - no flag gates.
    """
    config = cs_get_config()
    agentic_decision = route_bsl_agentic(UniversalNormalizer.normalize_to_openai(body), config)
    agentic_chain = [agentic_decision.selected_model, *agentic_decision.fallback_chain]
    _seen = set()
    agentic_chain = [e for e in agentic_chain if e and not (e in _seen or _seen.add(e))]
    if not agentic_chain:
        return JSONResponse(
            {"error": "Blacksand-Agentic has no configured route and no global_last_fallback."},
            status_code=503,
        )

    print(
        f"[BSLAgentic] chain={' -> '.join(agentic_chain)} source={agentic_decision.source}",
        flush=True,
    )

    # Route registry resolution + observability (mirrors _bsl_lite_dispatch).
    from app.middleware.route_registry import build_route_registry

    route_registry = build_route_registry(config, visible_only=True)

    _first_entry = agentic_chain[0] if agentic_chain else None

    # Recursive combo resolution for full chain observability.
    _chain_labels, model_id = _resolve_combo_chain_segment(
        _first_entry or "", config, route_registry
    )

    _chain_str = " > ".join(_chain_labels) if _chain_labels else ""

    canonical_line = (
        f"Blacksand-Agentic > {agentic_decision.selected_model}"
        + (f" > {_chain_str}" if _chain_str else "")
        + f" > {model_id} + {agentic_decision.source}"
    )
    _route_id = f"bsl_agentic_{time.time_ns()}"
    _ts = datetime.now().isoformat()
    _entry = {"event": "bsl_agentic_route", "text": canonical_line, "timestamp": _ts, "request_id": _route_id}
    try:
        obs.console_logs.append(_entry)
        if len(obs.console_logs) > 10000:
            obs.console_logs.pop(0)
        obs._persist_entry(obs._CONSOLE_LOG_PATH, _entry)
    except Exception as _e:
        print(f"[bsl_agentic_route] obs persist failed: {_e}", flush=True)
    print(canonical_line, flush=True)

    for idx, entry in enumerate(agentic_chain):
        iter_body = copy.deepcopy(body)
        iter_body["model"] = entry
        iter_body["_bsl_original_model"] = "blacksand-agentic"
        result = await _process_chat_completion(
            iter_body,
            client_wants_anthropic=client_wants_anthropic,
            client_wants_gemini=client_wants_gemini,
            request=request,
        )
        if (
            isinstance(result, JSONResponse)
            and result.status_code in _RECOVERABLE
            and idx < len(agentic_chain) - 1
        ):
            print(
                f"[BSLAgentic] '{entry}' returned HTTP {result.status_code}; "
                f"advancing to '{agentic_chain[idx + 1]}'",
                flush=True,
            )
            continue
        _routing_hdrs = {
            "X-BSL-Agentic-Category": str(agentic_decision.category),
            "X-BSL-Agentic-Source": str(agentic_decision.source),
            "X-BSL-Agentic-Selected": str(agentic_decision.selected_model),
        }
        if agentic_chain:
            _routing_hdrs["X-BSL-Agentic-Chain"] = " > ".join(agentic_chain)
        try:
            for _hk, _hv in _routing_hdrs.items():
                result.headers[_hk] = _hv
        except Exception:
            pass  # fail-safe: headers are nice-to-have for testing
        return result

    # 503 - also inject routing headers so the test suite can see the decision
    _routing_hdrs = {
        "X-BSL-Agentic-Category": str(agentic_decision.category),
        "X-BSL-Agentic-Source": str(agentic_decision.source),
        "X-BSL-Agentic-Selected": str(agentic_decision.selected_model),
    }
    if agentic_chain:
        _routing_hdrs["X-BSL-Agentic-Chain"] = " > ".join(agentic_chain)
    return JSONResponse(
        {"error": "Blacksand-Agentic contains no selectable entries."},
        status_code=503,
        headers={k: str(v) for k, v in _routing_hdrs.items()},
    )


async def _bsl_agentic_ultra_dispatch(
    body: dict,
    client_wants_anthropic: bool = False,
    client_wants_gemini: bool = False,
    request: Request = None,
):
    """Dispatch a blacksand-agentic-ultra request through the balanced tier.

    Mimics Blacksand Code balanced mode: Scout answers trivial questions
    (scout_direct); classified agent tasks get the lead route with the consult
    route prepended. Vision requests get a vision pre-flight chain prepended.
    Advances on recoverable errors only. Always on - no flag gates.
    """
    config = cs_get_config()
    ultra_decision = route_bsl_agentic_ultra(UniversalNormalizer.normalize_to_openai(body), config)
    # Extract the actual user query text from the last user message instead
    # of a repr of the entire messages list.
    _user_query = ""
    for _msg in reversed(body.get("messages", [])):
        if isinstance(_msg, dict) and _msg.get("role") == "user":
            _content = _msg.get("content", "")
            if isinstance(_content, list):
                _user_query = " ".join(
                    p.get("text", "") for p in _content if isinstance(p, dict)
                )
            else:
                _user_query = str(_content)
            break
    try:
        orchestration = build_balanced_plan(
            query=_user_query,
            category=ultra_decision.category,
        )
        orchestration_state = orchestration.state
    except (ValueError, TypeError) as exc:
        print(f"[BSLAgenticUltra] orchestration admission failed: {exc}", flush=True)
        orchestration_state = None
    ultra_chain = [ultra_decision.selected_model, *ultra_decision.fallback_chain]
    _seen = set()
    ultra_chain = [e for e in ultra_chain if e and not (e in _seen or _seen.add(e))]
    if not ultra_chain:
        return JSONResponse(
            {"error": "Blacksand-Agentic-Ultra has no configured route and no global_last_fallback."},
            status_code=503,
        )

    print(
        f"[BSLAgenticUltra] chain={' -> '.join(ultra_chain)} source={ultra_decision.source} "
        f"complexity={ultra_decision.complexity_level} consulted={ultra_decision.consulted}",
        flush=True,
    )

    # Route registry resolution + observability (mirrors _bsl_lite_dispatch).
    from app.middleware.route_registry import build_route_registry

    route_registry = build_route_registry(config, visible_only=True)

    _first_entry = ultra_chain[0] if ultra_chain else None

    # Recursive combo resolution for full chain observability.
    _chain_labels, model_id = _resolve_combo_chain_segment(
        _first_entry or "", config, route_registry
    )

    _chain_str = " > ".join(_chain_labels) if _chain_labels else ""

    canonical_line = (
        f"Blacksand-Agentic-Ultra > {ultra_decision.selected_model}"
        + (f" > {_chain_str}" if _chain_str else "")
        + f" > {model_id} + {ultra_decision.source}"
    )
    _route_id = f"bsl_agentic_ultra_{time.time_ns()}"
    _ts = datetime.now().isoformat()
    _entry = {"event": "bsl_agentic_ultra_route", "text": canonical_line, "timestamp": _ts, "request_id": _route_id}
    try:
        obs.console_logs.append(_entry)
        if len(obs.console_logs) > 10000:
            obs.console_logs.pop(0)
        obs._persist_entry(obs._CONSOLE_LOG_PATH, _entry)
    except Exception as _e:
        print(f"[bsl_agentic_ultra_route] obs persist failed: {_e}", flush=True)
    print(canonical_line, flush=True)

    for idx, entry in enumerate(ultra_chain):
        iter_body = copy.deepcopy(body)
        iter_body["model"] = entry
        iter_body["_bsl_original_model"] = "blacksand-agentic-ultra"
        result = await _process_chat_completion(
            iter_body,
            client_wants_anthropic=client_wants_anthropic,
            client_wants_gemini=client_wants_gemini,
            request=request,
        )
        if (
            isinstance(result, JSONResponse)
            and result.status_code in _RECOVERABLE
            and idx < len(ultra_chain) - 1
        ):
            print(
                f"[BSLAgenticUltra] '{entry}' returned HTTP {result.status_code}; "
                f"advancing to '{ultra_chain[idx + 1]}'",
                flush=True,
            )
            continue
        if orchestration_state is not None:
            try:
                finish_phase(
                    orchestration_state,
                    status="success" if getattr(result, "status_code", 500) < 400 else "error",
                    summary="Balanced phase completed",
                    model=entry,
                )
            except AmbiguousPhase:
                # Scout reassessment remains the only unresolved branch. The
                # synchronous proxy preserves the response rather than inventing
                # a new route here.
                print("[BSLAgenticUltra] phase requires Scout reassessment", flush=True)
            except Exception as exc:
                print(f"[BSLAgenticUltra] orchestration completion failed: {exc}", flush=True)
        _routing_hdrs = {
            "X-BSL-Agentic-Ultra-Category": str(ultra_decision.category),
            "X-BSL-Agentic-Ultra-Source": str(ultra_decision.source),
            "X-BSL-Agentic-Ultra-Selected": str(ultra_decision.selected_model),
            "X-BSL-Agentic-Ultra-Complexity": str(ultra_decision.complexity_level),
            "X-BSL-Agentic-Ultra-Consulted": str(ultra_decision.consulted),
        }
        if orchestration_state is not None:
            _routing_hdrs.update(phase_headers(orchestration_state))
        if ultra_chain:
            _routing_hdrs["X-BSL-Agentic-Ultra-Chain"] = " > ".join(ultra_chain)
        try:
            for _hk, _hv in _routing_hdrs.items():
                result.headers[_hk] = _hv
        except Exception:
            pass  # fail-safe: headers are nice-to-have for testing
        return result

    # 503 - also inject routing headers so the test suite can see the decision
    _routing_hdrs = {
        "X-BSL-Agentic-Ultra-Category": str(ultra_decision.category),
        "X-BSL-Agentic-Ultra-Source": str(ultra_decision.source),
        "X-BSL-Agentic-Ultra-Selected": str(ultra_decision.selected_model),
        "X-BSL-Agentic-Ultra-Complexity": str(ultra_decision.complexity_level),
        "X-BSL-Agentic-Ultra-Consulted": str(ultra_decision.consulted),
    }
    if ultra_chain:
        _routing_hdrs["X-BSL-Agentic-Ultra-Chain"] = " > ".join(ultra_chain)
    # All models exhausted - record the failed phase for orchestration state.
    if orchestration_state is not None:
        try:
            finish_phase(
                orchestration_state,
                status="error",
                summary="All models in chain returned recoverable errors",
                model=" -> ".join(ultra_chain),
            )
        except AmbiguousPhase:
            print("[BSLAgenticUltra] exhausted chain requires Scout reassessment", flush=True)
        except Exception as exc:
            print(f"[BSLAgenticUltra] orchestration completion failed on exhausted chain: {exc}", flush=True)
    return JSONResponse(
        {"error": "Blacksand-Agentic-Ultra contains no selectable entries."},
        status_code=503,
        headers={k: str(v) for k, v in _routing_hdrs.items()},
    )


async def _bsl_agentic_max_dispatch(
    body: dict,
    client_wants_anthropic: bool = False,
    client_wants_gemini: bool = False,
    request: Request = None,
):
    """Dispatch a blacksand-agentic-max request through dual-domain fusion.

    Fuses the bsl-agentic coding matrix with the bsl-chat matrix:
    dual-classify, pick the winning domain under the configured merge
    strategy, route through that domain, and append the losing domain's
    route as a cross-domain fallback. Always on - no flag gates
    (2026-08-06 directive).

    depth=balanced (locked), so Max shares the balanced-tier orchestration
    loop with Ultra: one-member plan, deterministic finish_phase gates.
    """
    config = cs_get_config()
    max_decision = route_bsl_agentic_max(UniversalNormalizer.normalize_to_openai(body), config)
    # Extract the actual user query text from the last user message.
    _user_query = ""
    for _msg in reversed(body.get("messages", [])):
        if isinstance(_msg, dict) and _msg.get("role") == "user":
            _content = _msg.get("content", "")
            if isinstance(_content, list):
                _user_query = " ".join(
                    p.get("text", "") for p in _content if isinstance(p, dict)
                )
            else:
                _user_query = str(_content)
            break
    # Dual-domain role selection for the balanced plan: coding categories map
    # to their OAC roles in the engine; chat categories fall through the
    # engine's .get(category, "scout") default to the scout role.
    _plan_category = (
        max_decision.coding_category
        if max_decision.domain == "coding"
        else max_decision.chat_category
    )
    try:
        orchestration = build_balanced_plan(
            query=_user_query,
            category=_plan_category,
        )
        orchestration_state = orchestration.state
    except (ValueError, TypeError) as exc:
        print(f"[BSLAgenticMax] orchestration admission failed: {exc}", flush=True)
        orchestration_state = None
    max_chain = [max_decision.selected_model, *max_decision.fallback_chain]
    _seen = set()
    max_chain = [e for e in max_chain if e and not (e in _seen or _seen.add(e))]
    if not max_chain:
        return JSONResponse(
            {"error": "Blacksand-Agentic-Max has no configured route and no global_last_fallback."},
            status_code=503,
        )

    print(
        f"[BSLAgenticMax] chain={' -> '.join(max_chain)} domain={max_decision.domain} "
        f"source={max_decision.source} merge={max_decision.merge_strategy}",
        flush=True,
    )

    # Route registry resolution + observability (mirrors _bsl_agentic_ultra_dispatch).
    from app.middleware.route_registry import build_route_registry

    route_registry = build_route_registry(config, visible_only=True)

    _first_entry = max_chain[0] if max_chain else None

    # Recursive combo resolution for full chain observability.
    _chain_labels, model_id = _resolve_combo_chain_segment(
        _first_entry or "", config, route_registry
    )

    _chain_str = " > ".join(_chain_labels) if _chain_labels else ""

    canonical_line = (
        f"Blacksand-Agentic-Max > {max_decision.selected_model}"
        + (f" > {_chain_str}" if _chain_str else "")
        + f" > {model_id} + {max_decision.domain} + {max_decision.merge_strategy} + {max_decision.source}"
    )
    _route_id = f"bsl_agentic_max_{time.time_ns()}"
    _ts = datetime.now().isoformat()
    _entry = {"event": "bsl_agentic_max_route", "text": canonical_line, "timestamp": _ts, "request_id": _route_id}
    try:
        obs.console_logs.append(_entry)
        if len(obs.console_logs) > 10000:
            obs.console_logs.pop(0)
        obs._persist_entry(obs._CONSOLE_LOG_PATH, _entry)
    except Exception as _e:
        print(f"[bsl_agentic_max_route] obs persist failed: {_e}", flush=True)
    print(canonical_line, flush=True)

    for idx, entry in enumerate(max_chain):
        iter_body = copy.deepcopy(body)
        iter_body["model"] = entry
        iter_body["_bsl_original_model"] = "blacksand-agentic-max"
        result = await _process_chat_completion(
            iter_body,
            client_wants_anthropic=client_wants_anthropic,
            client_wants_gemini=client_wants_gemini,
            request=request,
        )
        if (
            isinstance(result, JSONResponse)
            and result.status_code in _RECOVERABLE
            and idx < len(max_chain) - 1
        ):
            print(
                f"[BSLAgenticMax] '{entry}' returned HTTP {result.status_code}; "
                f"advancing to '{max_chain[idx + 1]}'",
                flush=True,
            )
            continue
        if orchestration_state is not None:
            try:
                finish_phase(
                    orchestration_state,
                    status="success" if getattr(result, "status_code", 500) < 400 else "error",
                    summary="Balanced phase completed",
                    model=entry,
                )
            except AmbiguousPhase:
                # Scout reassessment remains the only unresolved branch. The
                # synchronous proxy preserves the response rather than
                # inventing a new route here.
                print("[BSLAgenticMax] phase requires Scout reassessment", flush=True)
            except Exception as exc:
                print(f"[BSLAgenticMax] orchestration completion failed: {exc}", flush=True)
        _routing_hdrs = {
            "X-BSL-Agentic-Max-Domain": str(max_decision.domain),
            "X-BSL-Agentic-Max-Merge-Strategy": str(max_decision.merge_strategy),
            "X-BSL-Agentic-Max-Coding-Category": str(max_decision.coding_category),
            "X-BSL-Agentic-Max-Chat-Category": str(max_decision.chat_category),
            "X-BSL-Agentic-Max-Source": str(max_decision.source),
            "X-BSL-Agentic-Max-Selected": str(max_decision.selected_model),
        }
        if orchestration_state is not None:
            _routing_hdrs.update(phase_headers(orchestration_state))
        if max_chain:
            _routing_hdrs["X-BSL-Agentic-Max-Chain"] = " > ".join(max_chain)
        try:
            for _hk, _hv in _routing_hdrs.items():
                result.headers[_hk] = _hv
        except Exception:
            pass  # fail-safe: headers are nice-to-have for testing
        return result

    # 503 - also inject routing headers so the test suite can see the decision
    _routing_hdrs = {
        "X-BSL-Agentic-Max-Domain": str(max_decision.domain),
        "X-BSL-Agentic-Max-Merge-Strategy": str(max_decision.merge_strategy),
        "X-BSL-Agentic-Max-Coding-Category": str(max_decision.coding_category),
        "X-BSL-Agentic-Max-Chat-Category": str(max_decision.chat_category),
        "X-BSL-Agentic-Max-Source": str(max_decision.source),
        "X-BSL-Agentic-Max-Selected": str(max_decision.selected_model),
    }
    if max_chain:
        _routing_hdrs["X-BSL-Agentic-Max-Chain"] = " > ".join(max_chain)
    # All models exhausted - record the failed phase for orchestration state.
    if orchestration_state is not None:
        try:
            finish_phase(
                orchestration_state,
                status="error",
                summary="All models in chain returned recoverable errors",
                model=" -> ".join(max_chain),
            )
        except AmbiguousPhase:
            print("[BSLAgenticMax] exhausted chain requires Scout reassessment", flush=True)
        except Exception as exc:
            print(f"[BSLAgenticMax] orchestration completion failed on exhausted chain: {exc}", flush=True)
    return JSONResponse(
        {"error": "Blacksand-Agentic-Max contains no selectable entries."},
        status_code=503,
        headers={k: str(v) for k, v in _routing_hdrs.items()},
    )


def _extract_usage_tokens(usage: dict) -> tuple[int, int, int]:
    """Normalize a usage dict into (in_tokens, out_tokens, cached_tokens).

    OpenAI prompt_tokens is INCLUSIVE of cached tokens; Anthropic input_tokens
    is EXCLUSIVE (fresh only) with cache_read_input_tokens / cache_creation_input_tokens
    reported separately. This helper always returns an INCLUSIVE prompt count so the
    cached_tokens <= in_tokens invariant holds regardless of upstream format. Only
    cache READS map to cached_tokens (creation is fresh cache writes, not a
    discounted read). Shared across all stats/logging sites so they cannot diverge.
    """
    if not usage:
        return 0, 0, 0
    _cache_read = usage.get("cache_read_input_tokens", 0) or 0
    _cache_create = usage.get("cache_creation_input_tokens", 0) or 0
    _prompt = usage.get("prompt_tokens")
    if _prompt:
        # OpenAI shape: prompt_tokens is already inclusive of cache.
        # Truthy gate (not `is not None`): prompt_tokens=0 means "nothing
        # processed" or a format artifact â€” fall through to the Anthropic fold,
        # which correctly handles both cases instead of zeroing out input_tokens.
        _in = _prompt
    else:
        # Anthropic shape: fold fresh + read + creation into an inclusive count.
        _in = (usage.get("input_tokens", 0) or 0) + _cache_read + _cache_create
    _out = usage.get("completion_tokens", 0) or usage.get("output_tokens", 0) or 0
    _cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or _cache_read
    return _in, _out, _cached


def _extract_cache_write_tokens(usage: dict) -> int:
    """Extract cache write/creation tokens from OpenAI or Anthropic usage dict."""
    if not usage:
        return 0
    _cache_create = usage.get("cache_creation_input_tokens", 0) or 0
    if _cache_create:
        return _cache_create
    _prompt_details = usage.get("prompt_tokens_details") or {}
    if isinstance(_prompt_details, dict):
        return _prompt_details.get("cache_write_tokens", 0) or 0
    return 0


def _anthropic_terminal_error_frames(err_text: str, model: str = "bsl-routed") -> list:
    """Build a COMPLETE, VALID Anthropic SSE terminal sequence for a failed stream.

    Reuses the proven message_start -> ... -> message_stop shape from the
    combo-probe-exhausted path. The error text is placed in a content_block_delta
    so the failure is VISIBLE in the transcript rather than a silent empty turn.
    A content_block_stop is only ever paired with a content_block_start emitted by
    THIS helper, so it can never orphan a block the upstream stream opened.

    Module-level so the refused-fallback sites AND the unit test exercise the SAME
    builder (no mirror drift). Wrap each `yield` from this list in try/except so a
    client that already disconnected does not raise here.
    """
    _mid = f"msg_bslerr_{int(time.time() * 1000)}"
    _visible = f"[BSL Router] {err_text or 'stream_interrupted'}"
    return [
        (
            "event: message_start\n"
            f"data: {json.dumps({'type': 'message_start', 'message': {'id': _mid, 'type': 'message', 'role': 'assistant', 'model': model, 'content': [], 'stop_reason': None, 'usage': {'input_tokens': 0, 'output_tokens': 0}}})}\n\n"
        ).encode("utf-8"),
        (
            "event: content_block_start\n"
            f"data: {json.dumps({'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}})}\n\n"
        ).encode("utf-8"),
        (
            "event: content_block_delta\n"
            f"data: {json.dumps({'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': _visible}})}\n\n"
        ).encode("utf-8"),
        (
            "event: content_block_stop\n"
            f"data: {json.dumps({'type': 'content_block_stop', 'index': 0})}\n\n"
        ).encode("utf-8"),
        (
            "event: message_delta\n"
            f"data: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'}, 'usage': {'output_tokens': 0}})}\n\n"
        ).encode("utf-8"),
        (
            "event: message_stop\n"
            f"data: {json.dumps({'type': 'message_stop'})}\n\n"
        ).encode("utf-8"),
    ]


def _openai_terminal_error_frames(err_text: str, model: str = "bsl-routed", code: int = 504) -> list:
    """Build an OpenAI SSE terminal sequence (error frame + content + [DONE]).

    Matches the OpenAI sibling shape used at every other OpenAI-egress error exit.
    The error text appears both in the error frame and in a content delta so the
    failure is VISIBLE in the transcript rather than a silent empty turn.

    Module-level so the refused-fallback site AND the unit test exercise the SAME
    builder (no mirror drift). Wrap each `yield` from this list in try/except so a
    client that already disconnected does not raise here.
    """
    _visible = f"[BSL Router] {err_text or 'stream_interrupted'}"
    _cid = f"chatcmpl_bslerr_{int(time.time() * 1000)}"
    return [
        f"data: {json.dumps({'error': {'message': err_text or 'stream_interrupted', 'type': 'proxy_error', 'code': code}})}\n\n".encode("utf-8"),
        f"data: {json.dumps({'id': _cid, 'object': 'chat.completion.chunk', 'created': int(time.time()), 'model': model, 'choices': [{'index': 0, 'delta': {'content': _visible}, 'finish_reason': 'stop'}]})}\n\n".encode("utf-8"),
        b"data: [DONE]\n\n",
    ]


async def _accumulate_sse_stream(
    _sse_resp,
    *,
    _is_anthropic_fmt: bool,
    _target_model: str,
    _request=None,
    _label: str = "buf",
    _thinking_info=None,
):
    """Shared SSE accumulator (module-level so integration tests bind to the REAL
    production parser â€” eliminating mirror drift). Drains an SSE stream to [DONE],
    parses dual-format (Anthropic/OpenAI) chunks, assembles one OpenAI-shaped dict.
    Raises httpx.HTTPStatusError on mid-stream accumulation failure."""
    _a_cp: list[str] = []
    _a_rp: list[str] = []
    _a_tc: dict[int, dict] = {}
    _a_fr: str | None = None
    _a_rid = f"chatcmpl-{_label}-{int(time.time()*1000)}"
    _a_rmodel = _target_model
    _a_in = 0
    _a_out = 0
    _a_cached = 0
    _a_err = None

    try:
        _aiter = _sse_resp.aiter_bytes().__aiter__()
        _a_start = time.time()
        _a_seen_real_data = False
        # 9ROUTER PARITY (2026-08-04): the TTFT ceiling and stall timer that used
        # to live here are DELETED, matching `_stall_watchdog`.
        #
        # THIS WAS A LANDMINE. The old code read its budgets from
        # STREAM_TTFT_TIMEOUT_DEFAULT / STREAM_STALL_TIMEOUT_DEFAULT. Once those
        # became 0.0, `elapsed >= _ttft_ceiling` was true on the FIRST iteration,
        # so every single request would have failed instantly with a bogus
        # "TTFT timeout (0.0s)". Setting a constant to 0 is not the same as
        # removing the logic that consumes it — the consumer must go too.
        #
        # A silent upstream is now simply awaited, exactly as 9router does.
        # Genuine failures still raise (RemoteProtocolError, ReadError, reset)
        # and are handled by the existing except blocks below.
        while True:
            try:
                _raw = await _aiter.__anext__()
            except StopAsyncIteration:
                break
            # NOTE: the former `except asyncio.TimeoutError` branch is gone with
            # the `wait_for` wrapper that produced it. Nothing here imposes a
            # deadline any more, so that handler was unreachable code carrying a
            # misleading "Stall timeout (0.0s)" message.

            if _request is not None:
                try:
                    if await _request.is_disconnected():
                        break
                except Exception:
                    pass
            _txt = _raw.decode("utf-8", errors="replace")
            if not _txt.strip():
                continue

            # FREEZE FIX 2026-07-25: Don't treat metadata-only SSE events as
            # "real data" for TTFT purposes. message_start, content_block_start
            # (without content), and SSE comments (: ping) are metadata — the
            # provider may send them then go silent for minutes while thinking.
            # Only actual content/reasoning/tool_call data marks TTFT done.
            _has_real_content = False
            if _is_anthropic_fmt:
                for _ln in _txt.split("\n"):
                    _ln = _ln.strip()
                    if not _ln.startswith("data: "):
                        continue
                    _d = _ln[6:]
                    if _d == "[DONE]":
                        continue
                    try:
                        _ev = json.loads(_d)
                    except Exception:
                        continue
                    _et = _ev.get("type", "")
                    _ed = _ev.get("delta", {})
                    if _ed.get("type") in ("text_delta", "thinking_delta", "input_json_delta"):
                        _has_real_content = True
                        break
                    if _et == "content_block_start":
                        _blk = _ev.get("content_block", {})
                        if _blk.get("type") == "tool_use":
                            _has_real_content = True
                            break
            else:
                for _ln in _txt.split("\n"):
                    _ln = _ln.strip()
                    if not _ln.startswith("data: "):
                        continue
                    _ds = _ln[6:]
                    if _ds == "[DONE]":
                        continue
                    try:
                        _cd = json.loads(_ds)
                    except Exception:
                        continue
                    for _ch2 in _cd.get("choices", []):
                        _dl = _ch2.get("delta", {})
                        if _dl.get("content") or _dl.get("reasoning_content") or _dl.get("tool_calls"):
                            _has_real_content = True
                            break
                    if _has_real_content:
                        break

            if _has_real_content:
                _a_seen_real_data = True

            if _is_anthropic_fmt:
                for _ln in _txt.split("\n"):
                    _ln = _ln.strip()
                    if not _ln.startswith("data: "):
                        continue
                    _d = _ln[6:]
                    if _d == "[DONE]":
                        continue
                    try:
                        _ev = json.loads(_d)
                    except Exception:
                        continue
                    _et = _ev.get("type", "")
                    _ed = _ev.get("delta", {})
                    if _ed.get("type") == "thinking_delta":
                        _a_rp.append(_ed.get("thinking", ""))
                    elif _ed.get("type") == "text_delta":
                        _a_cp.append(_ed.get("text", ""))
                    elif _ed.get("type") == "input_json_delta":
                        _pj = _ed.get("partial_json", "")
                        if _pj and _a_tc:
                            _li = max(_a_tc.keys())
                            _a_tc[_li]["function"]["arguments"] += _pj
                    elif _et == "content_block_start":
                        _blk = _ev.get("content_block", {})
                        if _blk.get("type") == "tool_use":
                            _a_tc[len(_a_tc)] = {
                                "id": _blk.get("id", ""),
                                "type": "function",
                                "function": {
                                    "name": _blk.get("name", ""),
                                    "arguments": "",
                                },
                            }
                    elif _et == "message_delta":
                        _do = _ev.get("delta", {})
                        if _do.get("stop_reason"):
                            _srm = {
                                "end_turn": "stop",
                                "stop_sequence": "stop",
                                "max_tokens": "length",
                                "tool_use": "tool_calls",
                            }
                            _a_fr = _srm.get(_do["stop_reason"], _do["stop_reason"])
                        _uu = _ev.get("usage", {})
                        if _uu:
                            _a_out = _uu.get("output_tokens", _a_out)
                    elif _et == "message_start":
                        _uu = _ev.get("message", {}).get("usage", {})
                        if _uu:
                            # Anthropic input_tokens is EXCLUSIVE of cache; OpenAI
                            # prompt_tokens is INCLUSIVE. Fold fresh + read + creation
                            # so the assembled OpenAI-shaped usage keeps the
                            # cached_tokens <= prompt_tokens invariant. Only cache
                            # READS map to cached_tokens (creation is fresh writes).
                            _a_cache_read = _uu.get("cache_read_input_tokens", 0)
                            _a_cache_create = _uu.get("cache_creation_input_tokens", 0)
                            _fresh = _uu.get("input_tokens")
                            if _fresh is not None:
                                _a_in = _fresh + _a_cache_read + _a_cache_create
                            _a_cached = _a_cache_read or _a_cached
            else:
                for _ln in _txt.split("\n"):
                    _ln = _ln.strip()
                    if not _ln.startswith("data: "):
                        continue
                    _ds = _ln[6:]
                    if _ds == "[DONE]":
                        continue
                    try:
                        _cd = json.loads(_ds)
                    except Exception:
                        continue
                    for _ch2 in _cd.get("choices", []):
                        _dl = _ch2.get("delta", {})
                        if _dl.get("content"):
                            _a_cp.append(_dl["content"])
                        if _dl.get("reasoning_content"):
                            _a_rp.append(_dl["reasoning_content"])
                        _tcs2 = _dl.get("tool_calls")
                        if _tcs2:
                            for _tc2 in _tcs2:
                                _ix = _tc2.get("index", 0)
                                if _ix not in _a_tc:
                                    _a_tc[_ix] = {
                                        "id": _tc2.get("id", ""),
                                        "type": "function",
                                        "function": {"name": "", "arguments": ""},
                                    }
                                _fn2 = _tc2.get("function", {})
                                if _fn2.get("name"):
                                    _a_tc[_ix]["function"]["name"] += _fn2["name"]
                                if _fn2.get("arguments"):
                                    _a_tc[_ix]["function"]["arguments"] += _fn2["arguments"]
                        if _ch2.get("finish_reason"):
                            _a_fr = _ch2["finish_reason"]
                    _uu = _cd.get("usage", {})
                    if _uu:
                        _a_in = _uu.get("prompt_tokens", _a_in)
                        _a_out = _uu.get("completion_tokens", _a_out)
                        _ct = _uu.get("prompt_tokens_details", {})
                        if isinstance(_ct, dict):
                            _a_cached = _ct.get("cached_tokens", _a_cached)
                    if not _a_cp and _cd.get("id"):
                        _a_rid = _cd["id"]
                    # Don't override _a_rmodel with upstream's model echo.
                    # _target_model is the authoritative model name after combo
                    # fallback advancement. Upstream may echo a different model
                    # name (e.g. the original requested model), causing the
                    # display to show the wrong model in the successful call.
    except asyncio.CancelledError:
        raise
    except Exception as _ae:
        _a_err = str(_ae)
    finally:
        await _sse_resp.aclose()

    if _a_err:
        _a_req = getattr(_sse_resp, "request", None) or httpx.Request("POST", "http://upstream/sse-buffer")
        raise httpx.HTTPStatusError(
            f"StreamBuffer:{_label} accumulation failed: {_a_err}",
            request=_a_req,
            response=_sse_resp,
        )

    _a_msg = {"role": "assistant", "content": "".join(_a_cp)}
    if _a_rp:
        _a_msg["reasoning_content"] = "".join(_a_rp)
    if _a_tc:
        _a_msg["tool_calls"] = [
            _a_tc[k] for k in sorted(_a_tc.keys())
        ]

    return {
        "id": _a_rid,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": _a_rmodel,
        "choices": [{
            "index": 0,
            "message": _a_msg,
            "finish_reason": _a_fr or "stop",
        }],
        "usage": {
            "prompt_tokens": _a_in,
            "completion_tokens": _a_out,
            "total_tokens": _a_in + _a_out,
            **({"prompt_tokens_details": {"cached_tokens": _a_cached}} if _a_cached else {}),
        },
    }


class _SyntheticResponse:
    """Minimal httpx.Response shim for stream-then-buffer accumulation.

    Exposes just enough of the Response interface (.status_code, .json(),
    .text, .content, .headers, .aclose) for the existing non-streaming egress
    conversion, quality gate, and combo fallback paths to process a buffered
    stream result identically to a real non-streaming response.
    """
    def __init__(self, status_code: int, json_data: dict, text: str = None):
        self.status_code = status_code
        self._json_data = json_data
        self._text = text if text is not None else json.dumps(json_data)
        self.content = self._text.encode("utf-8")
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self._json_data

    @property
    def text(self):
        return self._text

    async def aclose(self):
        pass  # No-op â€” no underlying stream to close.


class _ComboFallbackNeeded(Exception):
    """Advance Gemini egress to the next combo entry before model output.

    Raised for retryable HTTP, transport, timeout, or body-stall failures while
    a further combo entry exists. The guarded generator catches the sentinel,
    re-invokes _process_chat_completion with the preserved retry state, and
    yields the replacement stream without falling through to native Google.
    """
    def __init__(self, status_code: int, err_text: str, retry_state: dict):
        self.status_code = status_code
        self.err_text = err_text
        self.retry_state = retry_state
        super().__init__(err_text)


async def _process_chat_completion(body: dict, client_wants_anthropic: bool = False, client_wants_gemini: bool = False, _retry_state: dict = None, request: Request = None):

    config = cs_get_config()
    print(f"[AFZ-FORENSIC] heartbeat route=chat active_streams={active_stream_count()}", flush=True)
    requested_model = body.get("model", "gpt-4o")
    model = _normalize_blacksand_model_id(requested_model)
    # Restore original alias from retry state first. Canonicalize Blacksand IDs so
    # internal retries keep stable product identity regardless of caller spelling.
    original_model = (
        (_retry_state.get("original_model") if _retry_state else None)
        or body.get("_bsl_original_model")
        or model
    )

    # ── BSL-Lite single-route dispatcher ───────────────────────────────────
    # L1 coding preset: route + global_last_fallback, no matrix.
    if model == "blacksand-lite":
        return await _bsl_lite_dispatch(
            body,
            client_wants_anthropic=client_wants_anthropic,
            client_wants_gemini=client_wants_gemini,
            request=request,
        )

    # ── BSL-Chat matrix dispatcher ───────────────────────────────────────
    # Resolve the BSL-Chat matrix before combo/alias/provider resolution. Each
    # matrix entry retains its own combo fallback chain inside the dispatcher.
    if model == "blacksand-chat":
        return await _bsl_matrix_dispatch(
            body,
            client_wants_anthropic=client_wants_anthropic,
            client_wants_gemini=client_wants_gemini,
            request=request,
        )

    # ── BSL-Agentic fast-tier dispatcher ────────────────────────────────
    # Scout-first routing: classify -> agent -> 3-slot chain (always on).
    if model == "blacksand-agentic":
        return await _bsl_agentic_dispatch(
            body,
            client_wants_anthropic=client_wants_anthropic,
            client_wants_gemini=client_wants_gemini,
            request=request,
        )

    # ── BSL-Agentic-Ultra balanced-tier dispatcher ───────────────────────
    # Mimics Blacksand Code balanced mode: Scout answers trivial, consult
    # route merged for agent tasks (always on).
    if model == "blacksand-agentic-ultra":
        return await _bsl_agentic_ultra_dispatch(
            body,
            client_wants_anthropic=client_wants_anthropic,
            client_wants_gemini=client_wants_gemini,
            request=request,
        )

    # ── BSL-Agentic-Max multi-domain fusion dispatcher ───────────────────
    # Fuses bsl-agentic (coding) + bsl-chat (chat) matrices; dual-domain
    # routing for OpenClaw / Hermes multi-purpose clients (always on).
    if model == "blacksand-agentic-max":
        return await _bsl_agentic_max_dispatch(
            body,
            client_wants_anthropic=client_wants_anthropic,
            client_wants_gemini=client_wants_gemini,
            request=request,
        )

    target_model = model
    provider_name = None

    # Step -2: public namespaced catalog ID, e.g. "ckey.vn/tanynguyen97/glm-5.2".
    # The published /v1/models IDs carry a provider's public namespace prefix.
    # Resolve it FIRST to the internal (provider_key, raw_model) so the upstream
    # sees only its raw model id. Falls through if no namespace matches.
    if isinstance(model, str) and "/" in model:
        _ns_provider, _ns_model = _resolve_namespaced_model(model)
        if _ns_provider:
            provider_name = _ns_provider
            target_model = _ns_model

    # Step -1: direct provider-qualified model reference, e.g. "banana/deepseek-v4-flash".
    # Used by provider model Test buttons and by any client that wants to bypass
    # same-model-ID ambiguity across providers. Also serves as legacy support for
    # "<provider_key>/<raw_model>" (e.g. "ckey/tanynguyen97/glm-5.2").
    if not provider_name and isinstance(model, str) and "/" in model:
        maybe_provider, maybe_model = model.split("/", 1)
        if maybe_provider in config.get("providers", {}):
            provider_name = maybe_provider
            target_model = maybe_model

    # Step -1b: Legacy alias may intentionally target a Combo alias.
    # UI selectors store Combo selections as provider="__combo__" so exact MITM
    # model keys (for example gemini-default) can route to configured fallback
    # chains like coder-2 / GPT-5.5 / Opus-VA-Thinking.
    model, _ = resolve_combo_alias_redirect(model, provider_name, config)

    # Step 0: Combo model resolution
    # globalConfig.combos = [{alias: str, chain: [model_id, ...], strategy: str}]
    # Strategy 'fallback' (default): use first available. Strategy 'round_robin': rotate.
    combo_thinking_override = None
    active_chain = None  # Populated during combo resolution; used for downstream fallback loop
    _combo_matched = False  # Set True when a combo alias matches; used to emit combo_route AFTER retry override
    _combo_result = resolve_combo(model, provider_name, config, ROUND_ROBIN_STATE)
    if _combo_result.matched:
        _combo_matched = True
        target_model = _combo_result.target_model
        provider_name = _combo_result.provider_name
        combo_thinking_override = _combo_result.thinking_override
        active_chain = _combo_result.active_chain
        if target_model is None and provider_name is None:
            return JSONResponse(
                {"error": f"Combo '{model}' has no available models in its chain. All chain members are offline or unregistered."},
                status_code=503,
            )

    # ── Combo Fallback Retry Override (RC5 + C2 + C3) ──────────────────
    # On a recursive retry (from a failed upstream dispatch), use the chain
    # SNAPSHOT captured in _retry_state — NOT the chain just rebuilt above.
    #
    # C2 (why snapshot, not rebuilt): a non-banning failure — e.g. a 400 unicode
    # reject or a thinking-param reject — does NOT ban the leaf. The combo
    # resolution above can reshape the chain (filtered_chain drops banned leaves,
    # round-robin re-seeds idx). Selecting from the rebuilt chain by a stale idx
    # can re-pick the very leaf that just failed, looping forever. The monotonic
    # snapshot idx always moves past it.
    #
    # RC5 (skip now-banned): a leaf banned mid-flight by another in-flight request
    # (or by this chain's own earlier failure) must be skipped, not re-dialed.
    #
    # C3 (write-back): the 14 downstream fallback sites compute
    # _next_idx = _retry_state['idx'] + 1. If we advance past banned leaves here
    # but don't write idx back, the next failure recomputes from the stale idx and
    # re-picks the current leaf. Writing back closes that loop.
    if _retry_state and _retry_state.get('chain'):
        _advance = advance_combo_retry(_retry_state, config, combo_alias=model)
        if _advance.exhausted:
            return JSONResponse(
                {"error": f"All {len(_retry_state['chain'])} combo chain entries exhausted for '{model}'."},
                status_code=502,
            )
        target_model = _advance.target_model
        provider_name = _advance.provider_name
        combo_thinking_override = _advance.thinking_override
        active_chain = _advance.active_chain
        print(f"[Combo] {model} > {provider_name}/{target_model} [{_advance.idx+1}/{len(_advance.active_chain)}, fallback-retry]", flush=True)

    # ── Combo Route Observability (moved AFTER retry override) ─────────────
    # Emit combo_route event AFTER the retry override so the admin UI shows
    # the ACTUAL serving model (not the first chain entry that failed).
    # The UI's _buildLifecycleRows copies _route_text onto the END log entry,
    # and formatLogLine prefers _route_text over log.model — so a stale
    # _route_text showing the first entry overrides the correct model field.
    # GUARD: skip on recursive retry (_retry_state) to avoid duplicate entries.
    if _combo_matched and not _retry_state:
        _combo_canonical = (
            f"Combo > {model} > "
            f"{provider_name}/{target_model}"
        )
        _route_id = f"bsl_{time.time_ns()}"
        _ts = datetime.now().isoformat()
        _entry = {"event": "combo_route", "text": _combo_canonical, "timestamp": _ts, "request_id": _route_id}
        try:
            obs.console_logs.append(_entry)
            if len(obs.console_logs) > 10000:
                obs.console_logs.pop(0)
            obs._persist_entry(obs._CONSOLE_LOG_PATH, _entry)
        except Exception as _e:
            print(f"[bsl_chat_route] obs persist failed: {_e}", flush=True)
        print(_combo_canonical, flush=True)


    # Step 1: Alias lookup
    if not provider_name:
        target_model, provider_name = resolve_alias(model, provider_name, config)

    # Step 2: If no alias matched, scan all providers' model lists
    if not provider_name:
        provider_name = find_provider_for_model(model, config)

    # Step 3: Still no match -> return a clear, actionable error
    if not provider_name:
        return build_not_found_error(model, config)

    provider_config = config.get("providers", {}).get(provider_name)
    if not provider_config:
        return JSONResponse({"error": f"Provider '{provider_name}' not configured"}, status_code=500)

    # Step 2 never sets target_model (it only sets provider_name). Fall back to the
    # raw request model name so internal_request.model isn't overwritten with None.
    if not target_model:
        target_model = model

    # Treat single-entry aliased/direct models as a 1-length active_chain so they
    # can trigger the existing status-peek probe and upstream failure loop.
    if not active_chain:
        active_chain = [(target_model, provider_name, None)]

    # Auto Error Prevention — skip models under an active soft/long-ban or disabled by self-heal.
    try:
        import app.error_prevention as ep
        banned, ban_type, remaining = ep.check_ban(config, provider_name, target_model)
        if banned:
            if ban_type == "disabled":
                detail = "auto-disabled after repeated failures (re-enable manually in the admin panel)"
                retry_after = None
            else:
                mins = int((remaining or 0) // 60) + 1
                detail = f"temporarily unavailable ({ban_type}), retry in ~{mins} min"
                retry_after = int(remaining or 0)
            headers = {"Retry-After": str(retry_after)} if retry_after else {}
            return JSONResponse(
                {"error": f"Model '{target_model}' is {detail}.", "ban_type": ban_type},
                status_code=503,
                headers=headers,
            )
    except Exception as _ep_err:
        print(f"[ErrorPrevention] ban check failed (non-blocking): {_ep_err}")
        

    # Restore cache_control breakpoints from a previous attempt (combo fallback retry).
    if _retry_state and _retry_state.get('cache_bp') and isinstance(body, dict):
        body["_bsl_cache_breakpoints"] = _retry_state['cache_bp']

    # Phase 7B: Extract cache_control side-channel BEFORE Pydantic parsing.
    # normalize_to_openai_from_anthropic stashes breakpoints here; we pop them
    # so they don't leak into Pydantic or OpenAI-format upstreams.
    _cache_breakpoints = body.pop("_bsl_cache_breakpoints", None) if isinstance(body, dict) else None

    # Convert incoming body to our strict internal Pydantic model
    try:
        internal_request = UniversalNormalizer.normalize_to_openai(body)
    except Exception as e:
        return JSONResponse({"error": f"Invalid payload schema: {str(e)}"}, status_code=400)
    
    # Overwrite model name for the upstream request
    internal_request.model = target_model

    # Scout.docs_parser â€” extract and conditionally summarize document attachments
    try:
        internal_request = await parse_documents(internal_request, http_client, config)
    except Exception as e:
        print(f"Docs Parser Scout error (non-blocking): {e}")

    # Scout.vision â€” polyfill vision capability for text-only models
    try:
        internal_request = await polyfill_vision(internal_request, http_client, config)
    except VisionPolyfillFailed as e:
        # Every candidate failed for every image. This is a deliberate hard
        # abort, not a soft error: dispatching now would spend a full (paid)
        # generation asking the target model to reason about an error string
        # standing in for the image. The generic handler below used to swallow
        # this, so the documented contract never actually ran.
        print(f"Vision Scout FAILED (aborting request): {e}", flush=True)
        return JSONResponse(
            {"error": {"message": f"Vision unavailable: {e}", "type": "vision_polyfill_failed"}},
            status_code=502,
        )
    except Exception as e:
        print(f"Vision Scout error (non-blocking): {e}")

    # Middleware.compaction â€” Context Budget Guard (skip: anthropic/openai/gemini)
    # GAP-2c: Compaction is a BSL addition NOT in 9Router. Its model URL is also
    # currently broken. Gate it off for Gemini-path requests to match 9Router.
    if not client_wants_gemini:
        try:
            internal_request = await apply_compaction(internal_request, http_client, config, provider_name=provider_name)
        except Exception as e:
            print(f"Compaction error (non-blocking): {e}")

    # Task Complexity Router removed — max_tokens is set by the token budget
    # (default 65535 floor when budget is off). Simplified per user directive.

    # Middleware.efficiency â€” Opus-style turn consolidation + tool batching
    try:
        _model_id = internal_request.model or ""
        internal_request.messages = inject_turn_consolidation(internal_request.messages, _model_id)
        internal_request.messages = inject_tool_batching(internal_request.messages, _model_id)
        # GLM Language Forcing: detect user language, force GLM to think in it.
        # Saves tokens on non-English tasks (GLM defaults to English reasoning).
        # GAP-2a: LangForce is a BSL addition NOT in 9Router. Gate off for
        # Gemini-path requests so the payload matches 9Router exactly.
        if not client_wants_gemini:
            internal_request.messages = inject_glm_language_forcing(internal_request.messages, _model_id)
    except Exception as e:
        print(f"Efficiency middleware error (non-blocking): {e}")

    # Apply Static-First sorting to maximize cache hits (gated by tools.caching_static_sort)
    internal_request = PromptCachingAdapter.apply_static_first_sort(internal_request, tools_config=config.get("tools", {}))

    # Select an active connection (connection-aware when model metadata
    # advertises which connection indexes can serve it). Falls back to the
    # legacy single-key / random-choice path when metadata is absent.
    _breaker = get_breaker()
    active_conn, _active_conn_index = resolve_active_connection(
        config, provider_name, target_model, breaker=_breaker
    )

    if active_conn is None:
        # Fallback to legacy structure if present
        if "api_key" in provider_config and "base_url" in provider_config:
            active_conn = provider_config
        else:
            return JSONResponse({"error": f"Provider {provider_name} has no active connections"}, status_code=500)

    # Resolve base_url â€” connection base_url â†’ provider base_url â†’ PROVIDER_DEFAULT_URLS
    resolved_base_url = (active_conn.get('base_url') or '').rstrip('/')
    if not resolved_base_url:
        resolved_base_url = (provider_config.get('base_url') or '').rstrip('/')
    if not resolved_base_url:
        resolved_base_url = PROVIDER_DEFAULT_URLS.get(provider_name, '').rstrip('/')
    if not resolved_base_url:
        return JSONResponse({"error": f"No base URL configured for provider '{provider_name}'. Please set one in the admin panel."}, status_code=500)

    # ── OAuth Token Refresh (systemic fix for recurring 401 loop) ────────
    # Every OAuth provider's access_token expires (~1h). Previously BSL used
    # the stored token blindly with no refresh, causing 401 loops. Now we
    # call ensure_fresh_token which checks expiry and refreshes if needed.
    _fresh_token = stored_api_key = active_conn.get('api_key', '')
    if active_conn.get('token_type') == 'oauth' and stored_api_key:
        try:
            _fresh_token = await ensure_fresh_token(
                provider_name, active_conn, provider_config
            )
        except Exception as _refresh_exc:
            print(f"[BSL] Token refresh failed for {provider_name}: {_refresh_exc}", flush=True)
            # Fail-open: use the stored token; 401-retry below will handle it
            _fresh_token = stored_api_key

    headers = {
        "Authorization": f"Bearer {_fresh_token}",
        "Content-Type": "application/json",
        # Prevent upstream from returning gzip-compressed SSE. httpx's
        # aiter_raw() yields compressed bytes without decompressing, which
        # crashes chunk.decode("utf-8") on gzip magic bytes (0x1f 0x8b).
        # "identity" forces plain-text responses from all providers.
        "Accept-Encoding": "identity",
    }

    # ── Anti-Detection: Spoof User-Agent for strict providers ──────────────
    # Replace the default httpx User-Agent with authentic per-provider values
    # so strict providers (Claude, Antigravity, Codex, Kiro) cannot detect
    # BSL Router by its transport fingerprint.
    _stealth_ua = _STEALTH_USER_AGENTS.get(provider_name)
    if _stealth_ua:
        headers["User-Agent"] = _stealth_ua

    # ── Provider-Specific Headers (Kiro / Grok CLI / Codex) ────────────────
    # Extracted to _inject_provider_headers so 401-retry paths can re-inject
    # after token refresh. Without this, Codex loses ChatGPT-Account-ID and
    # Grok loses version headers on retry, causing silent fallback failures.
    _inject_provider_headers(headers, provider_name, active_conn)
    
    # â”€â”€ Phase 2: Provider Profile Registry â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # Resolve provider profile from the registry â€” replaces hardcoded
    # if/else provider_name branches with declarative profiles.
    _profile = get_profile(provider_name, provider_config)
    _is_anthropic_fmt = is_anthropic_compatible(_profile)

    if _is_anthropic_fmt:
        upstream_payload = UniversalNormalizer.normalize_to_anthropic(internal_request)
        # Apply prompt caching only to first-party Anthropic (allows_anthropic_beta)
        if _profile.allows_anthropic_beta:
            upstream_payload = PromptCachingAdapter.apply_provider_caching(upstream_payload, provider_name, target_model, tools_config=config.get("tools", {}), obs=obs)
            headers["anthropic-version"] = "2023-06-01"
            headers["anthropic-beta"] = "prompt-caching-2024-07-31"
        else:
            # Anthropic-compatible providers (GLM-anthropic, etc):
            # Re-inject cache_control breakpoints extracted from the original
            # Anthropic request. Without this, the OpenAI round-trip strips all
            # cache_control tags, reducing cache hits to ~0.3%.
            if _cache_breakpoints:
                upstream_payload = UniversalNormalizer.reinject_cache_control(
                    upstream_payload, _cache_breakpoints
                )
                # Log for diagnostic visibility (Bug A investigation)
                bp_count = len(_cache_breakpoints)
                sys_bp = sum(1 for bp in _cache_breakpoints if bp.get("location") == "system")
                msg_bp = sum(1 for bp in _cache_breakpoints if bp.get("location") == "message")
                print(
                    f"[CacheControl] Re-injected {bp_count} breakpoints "
                    f"(system={sys_bp}, messages={msg_bp}) for {provider_name}/{target_model}",
                    flush=True,
                )
            # Send version header, no beta (Anthropic-compatible upstreams)
            if _profile.requires_anthropic_version:
                headers["anthropic-version"] = "2023-06-01"

        # Phase 3: Reasoning Policy Engine — inject thinking config
        thinking_setting = provider_config.get('thinking_config', {}).get(target_model, 'off')
        upstream_payload = apply_thinking_to_anthropic_payload(
            upstream_payload, target_model, provider_name, thinking_setting
        )
    else:
        # For OpenAI and Gemini (via OpenAI compat), just dump the model back to dict
        upstream_payload = internal_request.model_dump(exclude_none=True)
        # Strip side-channel fields that should never reach upstream providers
        upstream_payload.pop("_bsl_cache_breakpoints", None)

        # Apply prompt caching for OpenAI / compatible / Gemini paths
        upstream_payload = PromptCachingAdapter.apply_provider_caching(
            upstream_payload, provider_name, target_model, tools_config=config.get("tools", {}), obs=obs
        )

    # Adapter metadata is routing/response context, never a provider parameter.
    # ChatCompletionRequest(extra="allow") intentionally preserves unknown client
    # fields, so these must be removed explicitly at the provider-egress boundary.
    upstream_payload.pop("_bsl_original_model", None)
    upstream_payload.pop("x_antigravity_user_agent", None)
    upstream_payload.pop("x_gemini_tool_mode", None)

    # 9Router clean-egress parity: the Antigravity conversion may carry an
    # advisory reasoning_effort from Gemini's thinkingBudget, while BSL's model
    # config below injects the authoritative family-specific control. Clear all
    # inherited thinking variants first so providers receive exactly one form.
    if client_wants_gemini:
        upstream_payload = strip_thinking(upstream_payload)

    # -----------------------------------------
    # Apply 9Router Backend Thinking Patch
    # -----------------------------------------
    import re
    f_val = f"{provider_name}/{target_model}".lower()
    
    thinking_suffix = "auto"
    _model_max_output_tokens = 0
    reasoning_mode = None       # GPT-5.6 (standard/pro) or Fable/Mythos (adaptive/enabled)
    reasoning_context = None    # GPT-5.6 reasoning.context (auto/current_turn/all_turns)
    for m in provider_config.get("models", []):
        if m.get("id") == target_model:
            thinking_suffix = str(m.get("thinking", "auto")).lower()
            reasoning_mode = m.get("reasoning_mode")
            reasoning_context = m.get("reasoning_context")
            _mmo = m.get("max_output_tokens", 0)
            if isinstance(_mmo, (int, float)) and _mmo > 0:
                _model_max_output_tokens = int(_mmo)
            break

    # Combo-level per-entry thinking override takes precedence over provider default.
    if combo_thinking_override:
        thinking_suffix = str(combo_thinking_override).lower()

    # Thinking config actually applied â€” recorded on every request log entry so
    # the console shows which reasoning knobs a call ran with. For gpt-5.6-sol
    # and similar this includes reasoning_mode / reasoning_context in addition
    # to effort. Fail-open: logging must never break routing.
    try:
        thinking_info = {"effort": thinking_suffix}
        if reasoning_mode:
            thinking_info["reasoning_mode"] = reasoning_mode
        if reasoning_context:
            thinking_info["reasoning_context"] = reasoning_context
    except Exception:
        thinking_info = None
            
    # Model contract detection now lives in app/compat/families/. Each
    # contract owns its own detector and an explicit priority, so behavior
    # no longer depends on the physical order of an elif chain.
    # Qwen's max_tokens hard cap below asks the registry rather than
    # re-declaring a local regex.
    is_qwen = matches_contract(f_val, "qwen")
    
    # â”€â”€ max_tokens budget â”€â”€
    # Two modes:
    # 1. Budget OFF (default): floor = 65535 — every request gets at least
    #    65535 tokens. 65535 (not 65536) because Qwen API hard-caps at 65535;
    #    a floor of 65536 would trigger 400 on every Qwen-routed request.
    #    The floor still raises clients asking for less (anti-truncation).
    # 2. Budget ON: ``tools.max_tokens_budget`` is the hard ceiling. Requests
    #    declaring a higher max_tokens are REJECTED with 400 before leaving
    #    the router. The floor no longer raises. The quality-gate retry
    #    ceiling is also clamped to the budget (see _mt_budget_cap below).
    _mt_enabled = bool(config.get("tools", {}).get("max_tokens_budget_enabled", False))
    _client_mt = int(upstream_payload.get("max_tokens", 0) or 0)

    if _mt_enabled:
        _mt_budget = int(config.get("tools", {}).get("max_tokens_budget", 65535))
        _mt_budget = max(1024, min(_mt_budget, 65535))
        _mt_budget_cap = _mt_budget
        if _client_mt > _mt_budget:
            return JSONResponse(
                {
                    "error": {
                        "message": (
                            f"max_tokens={_client_mt} exceeds the active token budget "
                            f"({_mt_budget}). Lower max_tokens to at most {_mt_budget} "
                            f"or disable the budget in Tools → Token Budget."
                        ),
                        "type": "budget_exceeded",
                    }
                },
                status_code=400,
            )
        upstream_payload["max_tokens"] = min(_client_mt or _mt_budget, _mt_budget)
        if _model_max_output_tokens > 0:
            upstream_payload["max_tokens"] = min(
                upstream_payload["max_tokens"], _model_max_output_tokens
            )
    else:
        _mt_budget_cap = 65535
        _mt_floor = int(config.get("tools", {}).get("max_tokens_floor", 65535))
        if _model_max_output_tokens > 0:
            upstream_payload["max_tokens"] = min(max(_client_mt, _mt_floor), _model_max_output_tokens)
        else:
            upstream_payload["max_tokens"] = max(_client_mt, _mt_floor)

    # Qwen API hard-caps max_tokens at 65535. Apply after both budget and
    # floor paths converge; leave all other providers unchanged.
    if is_qwen and int(upstream_payload.get("max_tokens", 0) or 0) > 65535:
        upstream_payload["max_tokens"] = 65535
        
    if bool(re.search(r'kiro', f_val)):
        # Kiro CodeWhisperer protocol: transform OpenAI payload → AWS JSON body
        upstream_payload = kiro_adapter.openai_to_kiro(upstream_payload)
        headers["x-amz-target"] = "CodeWhisperer.GenerateAssistantResponse"
        headers["Content-Type"] = "application/x-amz-json-1.0"
        # DEBUG: log exact payload being sent to Kiro
        try:
            print(f"[Kiro Debug] Outbound payload:\n{json.dumps(upstream_payload, indent=2, ensure_ascii=False)[:2000]}", flush=True)
        except Exception:
            pass
            
    # ── Reasoning resolution: SINGLE WRITER ──────────────────────────
    # Every thinking/reasoning field is written here and nowhere else.
    # Contract selection, effort vocabulary, forbidden-parameter strips
    # and sampling-param sanitization all live in app/compat/families/,
    # one file per vendor, each contract carrying an explicit priority.
    #
    # The returned provenance records which contract and which rule set
    # each field, so a request log names the exact file to edit instead
    # of requiring a trace through a multi-branch cascade.
    upstream_payload, _thinking_prov = resolve_thinking(
        upstream_payload,
        f_val,
        thinking_suffix,
        reasoning_mode=reasoning_mode,
        reasoning_context=reasoning_context,
        wire_format=provider_config.get("format", "openai"),
    )

    # Attach provenance to the request log. Fail-open: diagnostics must
    # never break routing.
    try:
        if thinking_info is not None and _thinking_prov.records:
            thinking_info["resolved_by"] = _thinking_prov.summary()
            thinking_info["provenance"] = _thinking_prov.as_list()
    except Exception:
        pass

    # -----------------------------------------
    # Apply Dynamic Thinking Squeeze
    # If enabled and input context is near the model ceiling, cap budget_tokens
    # to the configured squeeze value (default 1024) to prevent context-too-large errors.
    t_cfg = config.get("tools", {})
    if t_cfg.get("output_thinking_squeeze", True):
        from app.middleware.compaction import _count_tokens
        input_token_estimate = _count_tokens(internal_request.messages)
        squeeze_threshold = int(t_cfg.get("compaction_threshold", 48000))
        squeeze_target = int(t_cfg.get("output_thinking_squeeze_tokens", 1024))
        if input_token_estimate >= squeeze_threshold:
            # Cap any budget_tokens already set to squeeze_target
            if "thinking" in upstream_payload and isinstance(upstream_payload["thinking"], dict):
                current_b = upstream_payload["thinking"].get("budget_tokens", 0)
                if current_b > squeeze_target:
                    upstream_payload["thinking"]["budget_tokens"] = squeeze_target
                    print(f"[ThinkingSqueeze] Capped budget_tokens {current_b} â†’ {squeeze_target} (input ~{input_token_estimate} tokens)")
            # Gemini native: generationConfig.thinkingConfig.thinkingBudget
            _gc = upstream_payload.get("generationConfig")
            if isinstance(_gc, dict):
                _tc = _gc.get("thinkingConfig")
                if isinstance(_tc, dict) and "thinkingBudget" in _tc:
                    current_b = _tc.get("thinkingBudget", 0)
                    if isinstance(current_b, int) and current_b > squeeze_target:
                        _tc["thinkingBudget"] = squeeze_target
                        print(f"[ThinkingSqueeze] Capped Gemini thinkingBudget {current_b} to {squeeze_target} (input ~{input_token_estimate} tokens)")
    # -----------------------------------------
    # -----------------------------------------

    # â”€â”€ Output Intent-Driven Format Enforcement â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # When enabled, scans the last user message for explicit output format
    # directives (JSON, table, code, bullet list, etc.) and injects a system
    # prompt augmentation to strictly enforce the detected format.
    # This is a secondary defense alongside response_format injection.
    # GAP-2b: IntentDriven is a BSL addition NOT in 9Router. Gate off for
    # Gemini-path requests so the payload matches 9Router exactly.
    if t_cfg.get("output_intent_driven", False) and not client_wants_gemini:
        try:
            intent = _detect_output_intent(internal_request.messages)
            if intent:
                upstream_payload = _inject_intent_format_block(upstream_payload, intent)
                print(f"[IntentDriven] Detected format intent '{intent}' â€” injected enforcement block", flush=True)
        except Exception:
            pass  # Fail-open: never break the proxy for a format detection error

    # Handle streaming vs non-streaming
    is_stream = internal_request.stream

    # Streaming anti-stop (S3-streaming): tap raw upstream bytes for
    # finish_reason="length"/"max_tokens"/"MAX_TOKENS" and splice ONE
    # continuation stream before the client sees stream end. Qwen-scoped
    # (Qwen3.8-Max hard-stops at the 65535 cap), opt-in via
    # tools.stream_anti_stop, fail-open by construction (detector disabled
    # → truncated never set → plain stream).
    _stream_anti_stop = bool(t_cfg.get("stream_anti_stop", False)) and is_stream and is_qwen
    _cont_state = {"used": False}
    _detector = StreamTruncationDetector(f_val, enabled=_stream_anti_stop)

    # â”€â”€ Stream-Then-Buffer: 524 Mitigation Gate â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # Non-streaming requests to Cloudflare-fronted upstreams (vsllm/744000)
    # die with HTTP 524 at ~100s because zero bytes flow during buffered
    # generation. When enabled, BSL transparently rewrites the upstream call
    # to stream:true, accumulates SSE chunks, and returns a single assembled
    # JSON â€” keeping bytes flowing to defeat Cloudflare's idle timer.
    # Design triangulated: GLM â†’ Kimi adversarial â†’ Opus audit â†’ GLM concede.
    # Corrections baked in: (1) stream_options.include_usage for vLLM usage;
    # (2) non-mutating payload copy (S3/S6 read upstream_payload); (3) drain
    # to [DONE] (usage chunk arrives AFTER finish_reason); (4) n>1 skip;
    # (5) error=drop+raise+combo-advance; (6) <2s fail-open, timeout=error;
    # (7) Anthropic thinking_delta extraction.
    _sb_cfg = config.get("upstream_stream_buffer", {})
    _buffer_enabled = _sb_cfg.get("enabled", True) if isinstance(_sb_cfg, dict) else True
    if isinstance(_sb_cfg, dict) and isinstance(_sb_cfg.get("providers"), dict):
        _prov_sb = _sb_cfg["providers"].get(provider_name)
        if isinstance(_prov_sb, dict):
            _buffer_enabled = _prov_sb.get("enabled", _buffer_enabled)
    # Skip for n>1 â€” streaming path only handles choices[0] (consistent).
    _has_n_gt_1 = isinstance(upstream_payload.get("n"), int) and upstream_payload["n"] > 1
    _apply_stream_buffer = (not is_stream and _buffer_enabled and not _has_n_gt_1)

    # â”€â”€ Response Format Resilience â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # Many "deep market" reverse-proxy sellers silently strip response_format
    # even though they accept it without error. Inject a JSON instruction into
    # the system prompt as a fallback so the model still produces structured output.
    # Fail-open: only modifies payload when response_format is present.
    if has_response_format(upstream_payload):
        upstream_payload = inject_json_instruction(upstream_payload)
        print(f"[ResponseFormat] JSON instruction injected for {provider_name}/{target_model}", flush=True)
    
    # Deterministic JSON serialization (sort_keys) protects GLM/DeepSeek implicit
    # prefix caching by guaranteeing byte-for-byte stable payloads. The actual
    # serialization now happens inside _build_req below so retries stay stable too.
    
    # Custom text providers store a canonical origin/prefix and use the
    # format-aware builder. Built-in provider defaults keep their existing paths.
    if provider_config.get("type") == "custom" and provider_config.get("format") in (
        "openai", "openai-responses", "anthropic", "gemini"
    ):
        try:
            _upstream_url = build_custom_text_upstream_url(
                resolved_base_url, provider_config.get("format", "openai"), target_model, is_stream
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
    elif provider_name == 'kiro':
        _upstream_url = f"{resolved_base_url}/generateAssistantResponse"
    elif provider_name in ('ollama', 'ollama-local'):
        _upstream_url = f"{resolved_base_url}/chat"
    elif _is_anthropic_fmt:
        _upstream_url = f"{resolved_base_url}/messages"
    else:
        _upstream_url = f"{resolved_base_url}/chat/completions"

    # ── Antigravity egress client selection ─────────────────────────────────
    # When the upstream target is a known Google Cloud Code host, the OS hosts
    # file redirects it to 127.0.0.1 (9Router). The generic http_client would
    # then receive 9Router's self-signed cert and raise CERTIFICATE_VERIFY_FAILED.
    # Use the egress client instead: it resolves via 8.8.8.8 (bypassing hosts),
    # connects to the real Google IP, and validates against the real Google cert.
    _upstream_host = httpx.URL(_upstream_url).host if _upstream_url else ""
    if provider_name == "antigravity" or _upstream_host in _ANTIGRAVITY_NATIVE_HOSTS:
        client = _get_antigravity_egress_client()
    elif provider_config.get("ssl_verify", True) is False:
        # Provider uses a self-signed cert (e.g. api.iamhc.cn, api.hcnsec.cn).
        # Disable TLS verification for this provider only.
        client = _get_ssl_disabled_client(active_conn.get("proxy_url"))
    else:
        client = _get_client_for_proxy(active_conn.get("proxy_url"))

    # OAuth 401-retry guard: ensures we only retry once per request on 401.
    _oauth_401_retried = False

    # Reusable request builder. Serializes deterministically (sort_keys) so any
    # rebuild â€” e.g. the thinking-degradation retry below â€” keeps byte-for-byte
    # stable prefixes for implicit prompt caching.
    def _build_req(payload_dict: dict):
        return client.build_request(
            "POST",
            _upstream_url,
            headers=_strip_bsl_identity_headers(headers),
            content=json.dumps(payload_dict, sort_keys=True).encode("utf-8"),
        )

    # Defensive serialization: if the payload contains unencodable characters,
    # catch UnicodeEncodeError at build-time and advance the combo fallback
    # (or return a safe error) instead of letting it bubble up unhandled.
    try:
        req = _build_req(upstream_payload)
    except UnicodeEncodeError as _ue_err:
        print(
            f"[Combo Fallback] '{model}' Unicode serialization error for "
            f"{target_model}/{provider_name}: {_ue_err} â€” advancing",
            flush=True,
        )
        _next_idx = (_retry_state['idx'] + 1) if _retry_state else 1
        try:
            _budget_remaining = _chain_budget_remaining() if _chain_budget_remaining else CHAIN_TOTAL_BUDGET
        except NameError:
            _budget_remaining = CHAIN_TOTAL_BUDGET
        if active_chain and _next_idx < len(active_chain) and _budget_remaining > 0:
            try:
                _deadline = _chain_deadline
            except NameError:
                _deadline = time.monotonic() + CHAIN_TOTAL_BUDGET
            return await _process_chat_completion(
                body, client_wants_anthropic, client_wants_gemini,
                _retry_state={'chain': active_chain, 'idx': _next_idx, 'cache_bp': _cache_breakpoints, 'original_model': original_model, 'deadline': _deadline},
                request=request,
            )
        if _budget_remaining <= 0:
            try:
                _elapsed = time.monotonic() - (_chain_deadline - CHAIN_TOTAL_BUDGET)
            except NameError:
                _elapsed = 0
            print(f"[AFZ-DEADLINE] chain budget exhausted after {_elapsed:.1f}s, idx={_next_idx}, refusing further fallback", flush=True)
        return JSONResponse(
            {"error": f"Payload serialization error for '{model}': {_ue_err}"},
            status_code=400,
        )

    # â”€â”€ Outbound forensics (GATED) â€” diff MITM path vs direct path â”€â”€
    # Captures the EXACT bytes sent upstream so we can diff the failing MITM
    # (client=gemini) path against the working direct path for the same model.
    # This was the evidence that ended the 1210 guessing loop.
    #
    # FREEZE FIX (2026-08-07): this dump serializes the ENTIRE upstream payload
    # (full conversation history) synchronously on the async request path and
    # appends to a multi-hundred-MB JSONL file. That is a proven latency
    # amplifier that worsens apparent freezes even when stream termination is
    # correct. It is now OFF in normal operation and ON only when explicitly
    # enabled via config `debug.outbound_forensics: true` OR the
    # BSL_OUTBOUND_FORENSICS env var. Logs already on disk are NOT deleted.
    _outbound_forensics_on = False
    try:
        _dbg = config.get("debug") if isinstance(config, dict) else None
        if isinstance(_dbg, dict) and _dbg.get("outbound_forensics"):
            _outbound_forensics_on = True
        elif __import__("os").environ.get("BSL_OUTBOUND_FORENSICS"):
            _outbound_forensics_on = True
    except Exception:
        _outbound_forensics_on = False
    if _outbound_forensics_on:
        try:
            import os as _os
            _client_tag = "gemini" if client_wants_gemini else ("anthropic" if client_wants_anthropic else "openai")
            _safe_hdrs = {k: v for k, v in headers.items() if k.lower() != "authorization"}
            _out_rec = {
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "client": _client_tag,
                "provider": provider_name,
                "model": target_model,
                "upstream_url": _upstream_url,
                "is_anthropic_fmt": _is_anthropic_fmt,
                "headers": _safe_hdrs,
                "payload": upstream_payload,
            }
            _os.makedirs(".brain/logs", exist_ok=True)
            with open(".brain/logs/outbound_upstream.jsonl", "a", encoding="utf-8") as _f:
                _f.write(json.dumps(_out_rec, ensure_ascii=False, default=str) + "\n")
            print(f"[OutboundDump] client={_client_tag} {provider_name}/{target_model} -> {_upstream_url}", flush=True)
        except Exception as _e:
            print(f"[OutboundDump] failed (non-blocking): {_e}", flush=True)

    # Reseller channel-roulette guard: only arm the degrade-retry when this
    # payload actually carries thinking/reasoning fields. If the upstream lands
    # on a channel that rejects those params with a 400, we retry ONCE against
    # the SAME provider with a stripped payload instead of hard-failing.
    _thinking_retry_armed = payload_has_thinking(upstream_payload)

    async def _send_stream_with_thinking_fallback(stream_req=None, stream_payload=None):
        """Open a streaming upstream response, degrading thinking params on 400.

        Sends the primary request (or stream_req if provided for the buffer
        path); if the channel rejects thinking/reasoning params with a 400,
        reads+closes the error body and reopens the stream against the SAME
        provider with a stripped payload. Returns the live (unclosed) streaming
        response for the caller to iterate. Fail-open: on any fallback error,
        the original 400 response is returned unchanged.
        """
        nonlocal _thinking_retry_armed
        _active_req = stream_req if stream_req is not None else req
        _active_payload = stream_payload if stream_payload is not None else upstream_payload
        # RC6: bound the pre-header wait. httpx read=None makes header-wait
        # infinite; a TCP-connected-but-silent leaf would hang forever. Gemini
        # callers wrap this helper in their own tighter 15s _conn_deadline race,
        # so this 20s bound never dominates for them; it only rescues the
        # non-Gemini single-model stream path (raw_upstream) that has no other
        # per-connection deadline. Body/reasoning latency after headers is NOT
        # bounded here â€” that stays owned by the stall watchdog.
        _hw_timeout = max(1.0, min(HEADER_WAIT_TIMEOUT, _chain_budget_remaining()))
        try:
            _resp = await asyncio.wait_for(
                client.send(_active_req, stream=True),
                timeout=_hw_timeout,
            )
        except asyncio.TimeoutError:
            # LABEL FIX (2026-08-11): str(asyncio.TimeoutError()) is EMPTY, so
            # the bare exception surfaced in the Gemini terminal frame as the
            # useless "[BSL Router] upstream error 504: TimeoutError". The
            # upstream never sent a 504 — the ROUTER gave up waiting for
            # headers and aborted the connection (which is why the provider
            # side logs a 499). Re-raise self-describing so every egress path
            # reports the real cause instead of an empty-string exception name.
            _left = max(0.0, _chain_budget_remaining())
            raise TimeoutError(
                f"upstream_header_timeout (waited {_hw_timeout:.0f}s for headers, "
                f"{_left:.0f}s chain budget left)"
            ) from None
        # ── OAuth 401-Retry: force-refresh token and retry once ────────────
        # If the upstream rejects with 401, the token may have expired between
        # our pre-check and the actual send. Force-refresh and retry ONCE.
        if (
            _resp.status_code == 401
            and active_conn.get('token_type') == 'oauth'
            and not _oauth_401_retried
        ):
            _oauth_401_retried = True
            _old_resp = _resp
            try:
                _forced_token = await ensure_fresh_token(
                    provider_name, active_conn, provider_config, force=True
                )

                headers["Authorization"] = f"Bearer {_forced_token}"
                _inject_provider_headers(headers, provider_name, active_conn)
                _active_req = _build_req(_active_payload)
                print(
                    f"[OAuth-401Retry] {provider_name}/{target_model} "
                    f"force-refreshed token, retrying stream",
                    flush=True,
                )
                # Open replacement BEFORE closing original so a raise
                # leaves the caller with a valid (open) response.
                _resp = await asyncio.wait_for(
                    client.send(_active_req, stream=True),
                    timeout=max(1.0, min(HEADER_WAIT_TIMEOUT, _chain_budget_remaining())),
                )
                await _old_resp.aclose()
            except Exception as _oauth_retry_err:
                print(
                    f"[OAuth-401Retry] {provider_name} refresh+retry failed: "
                    f"{_oauth_retry_err}",
                    flush=True,
                )
                # _resp still points to _old_resp (open, status 401),
                # safe for downstream consumers.
        if _thinking_retry_armed and _resp.status_code == 400:
            try:
                _err_body = await _resp.aread()
                _rej_text = _err_body.decode("utf-8", errors="replace")[:1000]
                if is_thinking_param_rejection(_resp.status_code, _rej_text):
                    print(
                        f"[ThinkingFallback] '{target_model}/{provider_name}' rejected thinking params "
                        f"(400, stream) â€” retrying once with stripped payload",
                        flush=True,
                    )
                    # Open replacement BEFORE closing original so a raise leaves
                    # the caller with a valid (open) response for error handling.
                    _new = await client.send(_build_req(strip_thinking(_active_payload)), stream=True)
                    await _resp.aclose()
                    _resp = _new
                    _thinking_retry_armed = False
            except Exception as _tf_err:
                print(f"[ThinkingFallback] stream degrade-retry failed (non-blocking): {_tf_err}", flush=True)
        return _resp

    start_time = time.time()
    # Chain total deadline: seeded on the FIRST entry, propagated verbatim
    # through every recursive _retry_state hop. Without it each recursive call
    # restarts its own budget and a dead chain holds the client for N x 120s.
    _chain_deadline = (_retry_state or {}).get('deadline') or (time.monotonic() + CHAIN_TOTAL_BUDGET)

    def _chain_budget_remaining() -> float:
        return _chain_deadline - time.monotonic()

    def _midstream_transport_fallback(stats, _emit, status_code: int, err: str) -> None:
        """Normalize a mid-stream transport death into the ONE fallback signal.

        BUG J (2026-08-04) — THE "freeze on any error" ROOT CAUSE.
        `resp.aiter_raw()` raises httpx.RemoteProtocolError ("peer closed
        connection without sending complete message body / incomplete chunked
        read") from the ITERATOR, i.e. at the `async for` line itself, outside
        the inner try that only guards chunk decoding. It is a plain Exception,
        NOT _ComboFallbackNeeded, so every egress family routed it to its
        generic `except Exception` handler — which logs, benches, emits a frame
        and RETURNS. The combo chain never advanced. That is why an upstream
        error stopped the request instead of failing over.

        Raising _ComboFallbackNeeded here puts transport death on the same rail
        as every other recoverable failure, so the existing catchers advance the
        chain exactly as they do for a non-200 or a stall.

        THE `out == 0` GATE IS THE CRUX. With zero content tokens delivered,
        nothing the client can render has been sent, so failover is invisible
        and correct. Once content HAS been delivered, splicing a second
        provider's response into the same live parser would corrupt the
        transcript — so we fall through and let the caller emit its terminal
        frame instead. Never retry after bytes of model output.

        Closes over _retry_state / active_chain / _cache_breakpoints /
        original_model / _chain_deadline / model / target_model /
        provider_name / config. It is nested rather than module-level for
        exactly that reason.
        """
        stats["status"] = status_code
        stats["error"] = err
        if stats.get("out", 0) == 0:
            _mid_next_idx = (_retry_state["idx"] + 1) if _retry_state else 1
            if active_chain and _mid_next_idx < len(active_chain):
                print(
                    f"[Combo Fallback] '{model}' mid-stream transport error for "
                    f"{target_model}/{provider_name}: {err} — advancing to entry {_mid_next_idx}",
                    flush=True,
                )
                if _chain_budget_remaining() <= 0:
                    print(
                        f"[AFZ-DEADLINE] chain budget exhausted after "
                        f"{time.monotonic() - (_chain_deadline - CHAIN_TOTAL_BUDGET):.1f}s, "
                        f"idx={_mid_next_idx}, refusing further fallback",
                        flush=True,
                    )
                elif _emit.may_fallback(f"midstream_transport_{status_code}"):
                    raise _ComboFallbackNeeded(
                        status_code,
                        err,
                        {
                            "chain": active_chain,
                            "idx": _mid_next_idx,
                            "cache_bp": _cache_breakpoints,
                            "original_model": original_model,
                            "deadline": _chain_deadline,
                        },
                    )
        # No fallback taken: bench the dead leaf so auto-heal avoids it next time.
        try:
            bench_leaf(config, provider_name, target_model, status_code, err, stats.get("out", 0))
        except Exception:
            pass

    async def _transport_guarded(_agen, stats, _emit, status_code: int = 502):
        """Wrap a raw upstream iterator so transport death routes to fallback.

        BUG J: applied at the ITERATOR rather than around each loop body, so a
        pump site becomes a one-line change and the 40+ lines of usage-accounting
        inside each loop stay untouched (re-indenting them would risk silent
        behavioural drift for no benefit).

        _ComboFallbackNeeded raised by _midstream_transport_fallback propagates
        out of this generator into the egress guard's existing catcher, which
        already knows how to advance the chain. If fallback is declined the
        generator simply ends, and the caller's terminal-frame logic runs.
        """
        try:
            async for _chunk in _agen:
                yield _chunk
        except asyncio.CancelledError:
            raise
        except (httpx.RemoteProtocolError, httpx.TransportError) as _mid_err:
            _midstream_transport_fallback(
                stats, _emit, status_code, f"midstream_transport: {_mid_err}"
            )

    client_label = "gemini" if client_wants_gemini else ("anthropic" if client_wants_anthropic else "openai")
    # Combo badge: when a combo matched, the combo alias (model) is the identity
    # the UI must show — NOT _bsl_original_model (the caller's raw model that was
    # mapped onto the combo) and NOT the resolved target_model (the chain leaf).
    _combo_label = (
        model if (_combo_matched and model != target_model)
        else (original_model if original_model and original_model != target_model else None)
    )
    # GUARD: only log_request_start on the FIRST entry, not on recursive retry.
    # Each retry re-enters _process_chat_completion and would create a new
    # request_id + start entry, making it look like "multiple calls" when
    # only the first chain entry resolved. Downstream log_request() calls
    # each get their own request_id per attempt, which is fine.
    if not _retry_state:
        request_id = obs.log_request_start(
            provider=provider_name,
            model=target_model,
            config=config,
            stream=is_stream,
            client=client_label,
            upstream_url=_upstream_url,
            thinking=thinking_info,
            combo=_combo_label,
        )
        _orig_request_id = request_id
    else:
        request_id = hex(time.time_ns())[2:12]  # inline short ID for this attempt
        # Preserve the ORIGINATING request_id + combo label so retry/stall END
        # rows still carry the combo badge (fixes combo-name drop/mislabel when a
        # fallback or ttft_stall attempt logs with a fresh random id).
        _orig_request_id = (_retry_state.get("orig_request_id") if _retry_state else None) or request_id
    
    # â”€â”€ Streaming Status Peek â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # Keep the pre-header probe for OpenAI and Anthropic streams so upstream
    # failures can be returned before their SSE sockets open. Gemini bypasses it:
    # gemini_egress_stream emits its heartbeat before awaiting upstream headers
    # and already translates upstream failures into Gemini SSE error frames.
    _probe_resp = None
    if _should_probe_stream_status(is_stream, active_chain, client_wants_gemini):
        try:
            # RC6: bound the pre-header wait for OpenAI/Anthropic combo streams.
            # On timeout, re-raise as a transport error so the existing
            # `except Exception as _probe_err` handler below benches this leaf
            # and advances the combo chain â€” exactly the dead-leaf path.
            try:
                _probe_resp = await asyncio.wait_for(
                    client.send(req, stream=True),
                    timeout=max(1.0, min(HEADER_WAIT_TIMEOUT, _chain_budget_remaining())),
                )
            except asyncio.TimeoutError as _hdr_to:
                raise httpx.ReadTimeout(
                    f"upstream header wait exceeded {HEADER_WAIT_TIMEOUT}s"
                ) from _hdr_to
            if _probe_resp.status_code != 200:
                _err_body = await _probe_resp.aread()
                _err_text = _err_body.decode("utf-8", errors="replace")
                
                # Check for thinking param rejection in the pre-header probe
                if _thinking_retry_armed and _probe_resp.status_code == 400 and is_thinking_param_rejection(_probe_resp.status_code, _err_text):
                    print(
                        f"[ThinkingFallback] '{target_model}/{provider_name}' rejected thinking params in probe "
                        f"(400) — retrying once with stripped payload",
                        flush=True,
                    )
                    await _probe_resp.aclose()
                    
                    # Re-build request with stripped payload
                    _stripped_payload = strip_thinking(upstream_payload)
                    _new_req = _build_req(_stripped_payload)
                    
                    try:
                        _probe_resp = await asyncio.wait_for(
                            client.send(_new_req, stream=True),
                            timeout=max(1.0, min(HEADER_WAIT_TIMEOUT, _chain_budget_remaining())),
                        )
                    except asyncio.TimeoutError as _hdr_to:
                        raise httpx.ReadTimeout(
                            f"upstream header wait exceeded {HEADER_WAIT_TIMEOUT}s"
                        ) from _hdr_to
                        
                    upstream_payload = _stripped_payload
                    req = _new_req
                    _thinking_retry_armed = False
                    
                    if _probe_resp.status_code != 200:
                        _err_body = await _probe_resp.aread()
                        _err_text = _err_body.decode("utf-8", errors="replace")
                
                if _probe_resp.status_code != 200:
                    await _probe_resp.aclose()
                    _err_text_short = _err_text[:500]
                    _dump_upstream_failure(
                        provider_name, target_model, _upstream_url, headers,
                        upstream_payload, _probe_resp.status_code, _err_text_short,
                    )
                    obs.log_request(
                        provider=provider_name, model=target_model,
                        status=_probe_resp.status_code, ttft=0,
                        in_tokens=0, out_tokens=0, cached_tokens=0,
                        config=config, error_msg=_err_text_short,
                        total_time=time.time() - start_time,
                        request_id=request_id, client=client_label,
                        stream=True, upstream_url=_upstream_url,
                        conn_index=_active_conn_index,
                        thinking=thinking_info,
                        combo=_combo_label,
                    )
                    print(f"[Combo Fallback] '{model}' upstream {_probe_resp.status_code} for {target_model}/{provider_name} — advancing")
                # ALL non-200 triggers fallback (per user directive). Even 400/422
                # payload errors advance — a different provider may accept the same
                # payload (different validation rules, different model capabilities).
                _next_idx = (_retry_state['idx'] + 1) if _retry_state else 1
                if active_chain and _next_idx < len(active_chain) and _chain_budget_remaining() > 0:
                    # FORENSIC (2026-08-04): the recursive call returns a Response
                    # object to THIS frame, which must hand it back up the stack
                    # untouched. If the outer frame has already begun streaming,
                    # this returned response is dropped on the floor and the client
                    # is left with an open connection. Log the handoff so the
                    # exhaustion-vs-dropped-response question is answerable.
                    print(
                        f"[AFZ-TRACE] recursing chain idx={_next_idx}/{len(active_chain)} "
                        f"client={client_label} stream={is_stream} rid={request_id}",
                        flush=True,
                    )
                    _recursed = await _process_chat_completion(
                        body, client_wants_anthropic, client_wants_gemini,
                        _retry_state={'chain': active_chain, 'idx': _next_idx, 'cache_bp': _cache_breakpoints, 'original_model': original_model, 'deadline': _chain_deadline},
                        request=request,
                    )
                    print(
                        f"[AFZ-TRACE] recursion idx={_next_idx} returned "
                        f"{type(_recursed).__name__} rid={request_id}",
                        flush=True,
                    )
                    return _recursed
                if _chain_budget_remaining() <= 0:
                    print(f"[AFZ-DEADLINE] chain budget exhausted after {time.monotonic() - (_chain_deadline - CHAIN_TOTAL_BUDGET):.1f}s, idx={_next_idx}, refusing further fallback", flush=True)
                # No more entries (or payload error) â€” build error response in the appropriate format
                print(
                    f"[AFZ-TRACE] CHAIN EXHAUSTED idx={_next_idx} len={len(active_chain) if active_chain else 0} "
                    f"client={client_label} stream={is_stream} status={_probe_resp.status_code} rid={request_id}",
                    flush=True,
                )
                if client_wants_gemini:
                    async def _gemini_probe_error():
                        # FORENSIC: proves whether this generator is actually
                        # ITERATED (i.e. FastAPI streamed it) versus merely
                        # constructed and discarded. A returned-but-never-consumed
                        # generator is indistinguishable from a missing handler in
                        # the logs, and that ambiguity blocked the last diagnosis.
                        print(f"[AFZ-TRACE] gemini exhaustion frame: generating rid={request_id}", flush=True)
                        # FREEZE FIX (2026-08-07): SOLE terminal contract. A
                        # preceding bare {"error":...} poisons the Antigravity
                        # Gemini parser (it stops consuming the later
                        # finishReason candidate), reproducing the IDE freeze.
                        from app.compat.adapters.gemini import terminal_error_frame as _g_term
                        yield gemini_sse_data(_g_term(_probe_resp.status_code, _err_text, target_model))
                        yield GEMINI_SSE_DONE
                        print(f"[AFZ-TRACE] gemini exhaustion frame: FLUSHED rid={request_id}", flush=True)

                    # ANTI-FREEZE: the Gemini probe-error stream must be watched by
                    # afz_guard like every other SSE egress, or a wedged client
                    # connection stays open and exhausts the IDE host pool.
                    _afz_sid = next_stream_id()
                    return StreamingResponse(
                        afz_guard(_gemini_probe_error(), _afz_sid, protocol="gemini"),
                        media_type="text/event-stream",
                        headers=gemini_response_headers(stream=True),
                    )
                print(f"[AFZ-FORENSIC] route=chat stream=True status=502 leaf={provider_name}/{target_model} active_streams={active_stream_count()} reason=combo_probe_exhausted", flush=True)
                # FREEZE FIX (2026-08-04): PROTOCOL MISMATCH.
                # This branch is reached with stream=True (see the forensic line
                # above), meaning the client opened an SSE stream and is waiting
                # for `data:` frames. Returning a bare JSONResponse hands it a
                # non-SSE body it cannot parse as a stream — so an OpenAI- or
                # Anthropic-shaped client hangs exactly like the Gemini one did.
                # This is the 502/503 freeze on non-Gemini clients.
                _exhausted_msg = (
                    f"All combo chain entries failed for '{model}'. "
                    f"Last: upstream {_probe_resp.status_code}"
                )
                if is_stream:
                    async def _sse_probe_error():
                        if client_wants_anthropic:
                            # Minimal well-formed Anthropic SSE termination. The
                            # client needs message_start -> ... -> message_stop to
                            # consider the stream closed; a bare error event is
                            # not enough for every consumer.
                            _mid = f"msg_bslerr_{int(time.time() * 1000)}"
                            yield (
                                "event: message_start\n"
                                f"data: {json.dumps({'type': 'message_start', 'message': {'id': _mid, 'type': 'message', 'role': 'assistant', 'model': target_model, 'content': [], 'stop_reason': None, 'usage': {'input_tokens': 0, 'output_tokens': 0}}})}\n\n"
                            ).encode("utf-8")
                            yield (
                                "event: content_block_start\n"
                                f"data: {json.dumps({'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}})}\n\n"
                            ).encode("utf-8")
                            yield (
                                "event: content_block_delta\n"
                                f"data: {json.dumps({'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': f'[BSL Router] {_exhausted_msg}'}})}\n\n"
                            ).encode("utf-8")
                            yield (
                                "event: content_block_stop\n"
                                f"data: {json.dumps({'type': 'content_block_stop', 'index': 0})}\n\n"
                            ).encode("utf-8")
                            yield (
                                "event: message_delta\n"
                                f"data: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'}, 'usage': {'output_tokens': 0}})}\n\n"
                            ).encode("utf-8")
                            yield (
                                "event: message_stop\n"
                                f"data: {json.dumps({'type': 'message_stop'})}\n\n"
                            ).encode("utf-8")
                        else:
                            # OpenAI shape: a chunk with finish_reason then [DONE].
                            yield f"data: {json.dumps({'error': {'message': _exhausted_msg, 'type': 'proxy_error', 'code': _probe_resp.status_code}})}\n\n".encode("utf-8")
                            yield f"data: {json.dumps({'id': f'chatcmpl_bslerr_{int(time.time() * 1000)}', 'object': 'chat.completion.chunk', 'created': int(time.time()), 'model': target_model, 'choices': [{'index': 0, 'delta': {'content': f'[BSL Router] {_exhausted_msg}'}, 'finish_reason': 'stop'}]})}\n\n".encode("utf-8")
                            yield b"data: [DONE]\n\n"

                    _afz_sid = next_stream_id()
                    return StreamingResponse(
                        afz_guard(
                            _sse_probe_error(),
                            _afz_sid,
                            # The terminal frame must match the CLIENT's parser.
                            # Claude Code has no `[DONE]` sentinel and terminates
                            # on message_stop, so an OpenAI frame here reads as
                            # silence and hangs the client.
                            protocol="anthropic" if client_wants_anthropic else "openai",
                        ),
                        media_type="text/event-stream",
                        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
                    )
                return JSONResponse(
                    {"error": _exhausted_msg},
                    status_code=502,
                )
        except Exception as _probe_err:
            if _probe_resp is not None:
                await _probe_resp.aclose()
            obs.log_request(
                provider=provider_name, model=target_model,
                status=500, ttft=0,
                in_tokens=0, out_tokens=0, cached_tokens=0,
                config=config, error_msg=str(_probe_err),
                total_time=time.time() - start_time,
                request_id=request_id, client=client_label,
                stream=True, upstream_url=_upstream_url,
                conn_index=_active_conn_index,
                thinking=thinking_info,
                combo=_combo_label,
            )
            _next_idx = (_retry_state['idx'] + 1) if _retry_state else 1
            if _next_idx < len(active_chain) and _chain_budget_remaining() > 0:
                print(f"[Combo Fallback] '{model}' stream network error for {target_model}/{provider_name}: {_probe_err} â€” advancing to entry {_next_idx}")
                return await _process_chat_completion(
                    body, client_wants_anthropic, client_wants_gemini,
                    _retry_state={'chain': active_chain, 'idx': _next_idx, 'cache_bp': _cache_breakpoints, 'original_model': original_model, 'deadline': _chain_deadline},
                    request=request,
                )
            if _chain_budget_remaining() <= 0:
                print(f"[AFZ-DEADLINE] chain budget exhausted after {time.monotonic() - (_chain_deadline - CHAIN_TOTAL_BUDGET):.1f}s, idx={_next_idx}, refusing further fallback", flush=True)
            _probe_resp = None
    if is_stream:
        # Egress: client hit /v1/messages (Anthropic) but upstream is OpenAI-format.
        # Convert the OpenAI SSE stream to Anthropic SSE so Claude Code can parse it.
        convert_egress = client_wants_anthropic and not _is_anthropic_fmt
        # Gemini egress: Antigravity client wants Gemini SSE; upstream is OpenAI-format.
        convert_gemini_egress = client_wants_gemini
        # Reverse egress: OpenAI client (/v1/chat/completions) but upstream is
        # Anthropic-compatible (GLM/Kimi/MiniMax). Convert Anthropic SSE â†’ OpenAI SSE.
        convert_anthropic_to_openai_egress = (
            not client_wants_anthropic and not client_wants_gemini and _is_anthropic_fmt
        )
        stats = {"ttft": 0.0, "in": 0, "out": 0, "cached": 0, "cache_write": 0, "error": None, "status": 500}

        async def _client_disconnected() -> bool:
            # Null-safe client-abort probe. Returns False when no Request is
            # threaded in (e.g. internal probe callers) or when the ASGI probe
            # raises. A client abort is a non-penalizing event â€” it must never
            # be treated as an upstream failure.
            if request is None:
                return False
            try:
                return await request.is_disconnected()
            except Exception:
                return False


        async def _stall_watchdog(raw_iter):
            """Forward upstream bytes. Nothing else.

            9ROUTER PARITY (2026-08-04). Pure passthrough — no active timeouts.
            Active timeouts (asyncio.wait_for) were tried and rejected because
            they can corrupt legitimate long-thinking streams (DeepSeek V4,
            MiniMax M3) that may be silent for minutes before producing tokens.

            The real fix for dead-provider hangs is at the CONNECTION level,
            not the stream level: TCP keepalive + half-open connection detection
            in the httpx transport (see _build_hardened_client). This matches
            9router's undici approach: infinite timeouts at the stream level,
            but dead connections are detected and recycled by the OS TCP stack.

            What this function DOES protect against:
              - Transport errors (RemoteProtocolError, TransportError) propagate
                immediately and are caught by _transport_guarded or the caller's
                except handler — no change to exception behavior.
              - The caller's existing terminal-frame logic runs when the
                generator ends normally or via exception.

            What this function does NOT do (by design):
              - No TTFT timeout (thinking models need unlimited wait)
              - No body-stall timeout (a slow model is not a dead model)
              - No total-stream timeout (the chain budget handles this at a
                higher level — see _chain_budget_remaining checks between
                fallback attempts)
            """
            async for chunk in raw_iter:
                yield chunk

        async def raw_upstream():
            resp = None
            buffer = ""
            # Tracks whether any byte of UPSTREAM MODEL OUTPUT reached the client.
            # This gates every combo-fallback decision (see may_fallback), because
            # a client parser cannot be un-fed: once real content is delivered,
            # splicing in a second provider would corrupt the transcript.
            #
            # BUG K-1 (2026-08-04): the blacksand-chat preamble below used to call
            # mark_emitted(). It is ROUTER-GENERATED cosmetic text, emitted BEFORE
            # the upstream request is even sent - so it flipped emitted=True while
            # zero model output existed, and may_fallback() returned False for the
            # rest of the request. Every fallback class (upstream 5xx, zero-output,
            # stall, mid-stream transport) was then refused, and the combo chain
            # for blacksand-chat was dead on arrival.
            _emit = StreamEmissionState()
            try:
                if original_model == "blacksand-chat" and not _is_anthropic_fmt and not client_wants_anthropic and not client_wants_gemini:
                    mock_prefill = 'data: {"choices": [{"delta": {"reasoning_content": "BSL Router: Routing payload...\n"}}]}\n\n'
                    # Deliberately NOT mark_emitted(): see BUG K-1 above. No model
                    # output has been delivered, so failover remains transparent -
                    # this status line stays valid whichever provider answers.
                    yield mock_prefill.encode('utf-8')

                if _probe_resp is not None:
                    resp = _probe_resp
                else:
                    resp = await _send_stream_with_thinking_fallback()
                    # COMBO ADVANCE FIX (2026-08-01): NO raise_for_status() here.
                    # It throws HTTPStatusError on 400/5xx BEFORE the L5041
                    # non-200 handler (which advances the combo chain) runs,
                    # so the error fell into except Exception -> error+DONE and
                    # the chain never advanced. Fall through to the status
                    # check below instead.
                stats["status"] = resp.status_code

                # NON-200 CHECK (2026-08-02): When upstream returns 400/5xx
                # (e.g. "model doesn't support images"), we must NOT iterate
                # the error body as raw chunks — the IDE would receive non-SSE
                # bytes, never see [DONE], and freeze. Read error body, bench
                # the leaf, then combo-fallback or emit error+[DONE].
                if resp.status_code != 200:
                    raw_err = await resp.aread()
                    try:
                        err_text = raw_err.decode("utf-8", errors="replace")[:500] if raw_err else f"upstream_{resp.status_code}"
                    except Exception:
                        err_text = f"upstream_{resp.status_code}"
                    stats["error"] = err_text
                    bench_leaf(config, provider_name, target_model, resp.status_code, err_text, stats.get("out", 0))
                    # Try combo fallback
                    _next_idx = (_retry_state["idx"] + 1) if _retry_state else 1
                    if active_chain and _next_idx < len(active_chain):
                        _fb_state = {
                            "chain": active_chain,
                            "idx": _next_idx,
                            "cache_bp": _cache_breakpoints,
                            "original_model": original_model,
                            "deadline": _chain_deadline,
                        }
                        print(f"[Combo Fallback] '{model}' raw upstream {resp.status_code} for {target_model}/{provider_name} — advancing to entry {_next_idx}", flush=True)
                        if _chain_budget_remaining() <= 0:
                            print(f"[AFZ-DEADLINE] chain budget exhausted after {time.monotonic() - (_chain_deadline - CHAIN_TOTAL_BUDGET):.1f}s, idx={_next_idx}, refusing further fallback", flush=True)
                        elif _emit.may_fallback(f"upstream_{resp.status_code}"):
                            # Reachable post-emission: the blacksand-chat prefill
                            # above may already have sent bytes to the client.
                            raise _ComboFallbackNeeded(resp.status_code, err_text[:500], _fb_state)
                    print(f"[AFZ-FORENSIC] route=chat stream=True status={resp.status_code} leaf={provider_name}/{target_model} active_streams={active_stream_count()} reason=stream_precontent_error_frame", flush=True)
                    # No fallback available — emit error + [DONE] so IDE unblocks
                    try:
                        _err_frame = {"error": {"message": err_text or f"upstream_{resp.status_code}", "type": "proxy_error"}}
                        yield f"data: {json.dumps(_err_frame)}\n\n".encode("utf-8")
                        yield b"data: [DONE]\n\n"
                    except Exception:
                        pass
                    return

                try:
                    async for chunk in _stall_watchdog(resp.aiter_raw()):
                        _detector.feed(chunk)
                        if await _client_disconnected():
                            stats["error"] = stats["error"] or "client_disconnected"
                            stats["status"] = 499
                            break

                        # Extract usage from chunk
                        try:
                            text_chunk = chunk.decode("utf-8")
                            if stats["ttft"] == 0.0:
                                _is_openai_content = (
                                    ('"content":"' in text_chunk and '"content":""' not in text_chunk) or
                                    ('"content": "' in text_chunk and '"content": ""' not in text_chunk) or
                                    ('"reasoning_content":"' in text_chunk and '"reasoning_content":""' not in text_chunk) or
                                    ('"text":"' in text_chunk and '"text":""' not in text_chunk)
                                )
                                _is_gemini_content = (
                                    '"candidates"' in text_chunk or
                                    '"parts"' in text_chunk or
                                    '"role":"' in text_chunk
                                )
                                _is_anthropic_content = (
                                    '"text_delta"' in text_chunk or
                                    '"content_block_delta"' in text_chunk or
                                    '"message_delta"' in text_chunk or
                                    '"thinking_delta"' in text_chunk
                                )
                                if _is_openai_content or _is_gemini_content or _is_anthropic_content:
                                    stats["ttft"] = time.time() - start_time
                            buffer += text_chunk
                            lines = buffer.split("\n")
                            buffer = lines.pop()

                            for line in lines:
                                if line.startswith("data: ") and line.strip() != "data: [DONE]":
                                    data_str = line[6:]
                                    try:
                                        data_json = json.loads(data_str)
                                        if "usage" in data_json and data_json["usage"]:
                                            usage = data_json["usage"]
                                            stats["in"], stats["out"], stats["cached"] = _extract_usage_tokens(usage)
                                            stats["cache_write"] = _extract_cache_write_tokens(usage)
                                    except json.JSONDecodeError:
                                        pass
                        except Exception:
                            pass

                        _emit.mark_emitted(chunk)
                        yield chunk
                except (httpx.RemoteProtocolError, httpx.TransportError) as _mid_err:
                    # BUG J: transport death mid-body. Route into the combo chain
                    # instead of falling through to `except Exception` (which
                    # never advanced the chain and left the IDE waiting).
                    _midstream_transport_fallback(
                        stats, _emit, 502, f"midstream_transport: {_mid_err}"
                    )
                    # Not re-raised: fallback declined (content already sent, or
                    # chain exhausted). Fall through to the terminal frame below.
                if _detector.truncated and not _cont_state["used"]:
                    _cont_state["used"] = True
                    try:
                        _cont_payload = build_continuation_stream_payload(upstream_payload, _detector.partial_text)
                        if _cont_payload:
                            print(
                                f"[AntiStop] {f_val} truncated at max_tokens — splicing continuation "
                                f"({len(_detector.partial_text)} chars)",
                                flush=True,
                            )
                            _cont_resp = await _send_stream_with_thinking_fallback(
                                stream_req=_build_req(_cont_payload), stream_payload=_cont_payload
                            )
                            _cont_resp.raise_for_status()
                            try:
                                async for _cc in _stall_watchdog(_cont_resp.aiter_raw()):
                                    _emit.mark_emitted(_cc)
                                    yield _cc
                            finally:
                                try:
                                    await _cont_resp.aclose()
                                except Exception:
                                    pass
                    except Exception as _ce:
                        print(f"[AntiStop] continuation failed (fail-open): {_ce}", flush=True)
                if stats.get("status") == 200 and stats.get("out", 0) == 0 and not stats.get("ttft"):
                    _zero_next_idx = (_retry_state["idx"] + 1) if _retry_state else 1
                    if active_chain and _zero_next_idx < len(active_chain):
                        _zero_retry_state = {
                            "chain": active_chain,
                            "idx": _zero_next_idx,
                            "cache_bp": _cache_breakpoints,
                            "original_model": original_model,
                            "deadline": _chain_deadline,
                        }
                        print(
                            f"[Combo Fallback] '{model}' OpenAI stream 200-with-0-tokens for "
                            f"{target_model}/{provider_name} — advancing to entry {_zero_next_idx}",
                            flush=True,
                        )
                        if _chain_budget_remaining() <= 0:
                            print(f"[AFZ-DEADLINE] chain budget exhausted after {time.monotonic() - (_chain_deadline - CHAIN_TOTAL_BUDGET):.1f}s, idx={_zero_next_idx}, refusing further fallback", flush=True)
                        elif _emit.may_fallback("zero_output_tokens"):
                            # A provider that omits usage data leaves out==0 even
                            # when real content streamed. Only advance pre-emission.
                            raise _ComboFallbackNeeded(504, "zero_output_tokens", _zero_retry_state)
                if stats.get("error") in ("ttft_stall", "stream_stall"):
                    _stall_next_idx = (_retry_state["idx"] + 1) if _retry_state else 1
                    if not stats.get("ttft") and active_chain and _stall_next_idx < len(active_chain):
                        _stall_retry_state = {
                            "chain": active_chain,
                            "idx": _stall_next_idx,
                            "cache_bp": _cache_breakpoints,
                            "original_model": original_model,
                            "deadline": _chain_deadline,
                        }
                        print(
                            f"[Combo Fallback] '{model}' OpenAI stream stall for "
                            f"{target_model}/{provider_name} — advancing to entry {_stall_next_idx}",
                            flush=True,
                        )
                        if _chain_budget_remaining() <= 0:
                            print(f"[AFZ-DEADLINE] chain budget exhausted after {time.monotonic() - (_chain_deadline - CHAIN_TOTAL_BUDGET):.1f}s, idx={_stall_next_idx}, refusing further fallback", flush=True)
                        elif _emit.may_fallback(stats.get("error") or "stream_stall"):
                            raise _ComboFallbackNeeded(504, stats.get("error") or "stream_stall", _stall_retry_state)
                    try:
                        _stall_err = {"error": {"message": stats.get("error") or "stream_stall", "type": "proxy_error"}}
                        yield f"data: {json.dumps(_stall_err)}\n\n".encode("utf-8")
                        yield b"data: [DONE]\n\n"
                    except Exception:
                        pass
            except (GeneratorExit, asyncio.CancelledError):
                stats["error"] = stats["error"] or "client_disconnected"
                stats["status"] = 499
                raise
            except _ComboFallbackNeeded:
                raise
            except Exception as e:
                stats["error"] = str(e)
                err_payload = f"data: {json.dumps({'error': stats['error']})}\n\n"
                yield err_payload.encode('utf-8')
                yield b"data: [DONE]\n\n"
            finally:
                if resp is not None:
                    try:
                        await resp.aclose()
                    except Exception:
                        pass

                # FREEZE FIX: report refused post-emission fallbacks.
                try:
                    _r = _emit.refusal_log(model=target_model, provider=provider_name)
                    if _r:
                        print(_r, flush=True)
                except Exception:
                    pass
                try:
                    obs.log_request(
                        provider=provider_name,
                        model=target_model,
                        status=stats["status"],
                        ttft=stats["ttft"],
                        in_tokens=stats["in"],
                        out_tokens=stats["out"],
                        cached_tokens=stats["cached"],
                        config=config,
                        error_msg=stats["error"],
                        total_time=time.time() - start_time,
                        request_id=request_id,
                        client=client_label,
                        stream=True,
                        upstream_url=_upstream_url,
                        thinking=thinking_info,
                        cache_write_tokens=stats.get("cache_write", 0),
                        combo=_combo_label,
                    )
                except Exception as _log_err:
                    print(f"[BSL Router] finally-log failed (non-blocking): {_log_err}", flush=True)


        async def raw_upstream_guarded():
            _raw_source = raw_upstream()
            try:
                async for _c in _raw_source:
                    yield _c
            except _ComboFallbackNeeded as _cf_raw:
                _raw_fallback = await _process_chat_completion(
                    body, client_wants_anthropic, client_wants_gemini,
                    _retry_state=_cf_raw.retry_state,
                    request=request,
                )
                if hasattr(_raw_fallback, "body_iterator"):
                    async for _fc in _raw_fallback.body_iterator:
                        yield _fc
                else:
                    async for _fc in _raw_fallback:
                        yield _fc
            except (GeneratorExit, asyncio.CancelledError):
                raise
            except Exception as _raw_guarded_err:
                # ANTI-FREEZE (2026-08-01): mid-stream exception = dead leaf;
                # bench it so auto-heal bans it for the NEXT request.
                bench_leaf(config, provider_name, target_model, stats.get("status", 502), stats.get("error") or str(_raw_guarded_err), stats.get("out", 0))
                try:
                    obs.log_request(
                        provider=provider_name, model=target_model,
                        status=stats["status"], ttft=stats["ttft"],
                        in_tokens=stats["in"], out_tokens=stats["out"],
                        cached_tokens=stats["cached"], config=config,
                        error_msg=stats["error"] or "stream_interrupted",
                        total_time=time.time() - start_time,
                        request_id=request_id, client=client_label, stream=True,
                        upstream_url=_upstream_url, conn_index=_active_conn_index,
                        thinking=thinking_info,
                        cache_write_tokens=stats.get("cache_write", 0),
                        combo=_combo_label,
                    )
                except Exception:
                    pass
                # FREEZE FIX (2026-08-01): never return an empty 200 stream.
                # A bare return terminates the generator with zero frames and
                # the IDE waits for [DONE] forever. Emit error + [DONE] so the
                # client unblocks, matching every other egress path.
                try:
                    _raw_err_frame = {"error": {"message": stats.get("error") or str(_raw_guarded_err) or "stream_interrupted", "type": "proxy_error"}}
                    yield f"data: {json.dumps(_raw_err_frame)}\n\n".encode("utf-8")
                    yield b"data: [DONE]\n\n"
                except Exception:
                    pass
                return
            finally:
                # LEAK FIX (2026-08-02): close the source generator so a client
                # disconnect reaches its finally and closes the upstream
                # response. `async for` alone does NOT do this.
                try:
                    await _raw_source.aclose()
                except BaseException:
                    pass

        if convert_egress:
            async def egress_stream():
                resp = None
                buffer = ""
                # FREEZE FIX: tracks whether any byte has reached the client.
                # Once it has, combo fallback is forbidden — starting a second
                # upstream stream into a parser that is mid-way through the
                # first one is what freezes the IDE.
                _emit = StreamEmissionState()
                try:
                    if _probe_resp is not None:
                        resp = _probe_resp
                    else:
                        resp = await _send_stream_with_thinking_fallback()
                    stats["status"] = resp.status_code

                    if resp.status_code != 200:
                        try:
                            raw_err = await asyncio.wait_for(resp.aread(), timeout=5.0)
                        except asyncio.TimeoutError:
                            raw_err = b"(error body read timed out)"
                        try:
                            err_text = raw_err.decode("utf-8")
                        except Exception:
                            err_text = str(raw_err)
                        stats["error"] = err_text[:500]
                        # ANTI-FREEZE (2026-08-01): bench the leaf so auto-heal
                        # bans/cooldowns it; the NEXT request skips this dead leaf.
                        bench_leaf(config, provider_name, target_model, resp.status_code, err_text[:500], stats.get("out", 0))
                        _fb_next_idx = (_retry_state['idx'] + 1) if _retry_state else 1
                        if active_chain and _fb_next_idx < len(active_chain):
                            _fb_retry_state = {
                                "chain": active_chain,
                                "idx": _fb_next_idx,
                                "cache_bp": _cache_breakpoints,
                                "original_model": original_model,
                                "deadline": _chain_deadline,
                            }
                            print(
                                f"[Combo Fallback] '{model}' Anthropic egress {resp.status_code} "
                                f"for {target_model}/{provider_name} — advancing to entry {_fb_next_idx}",
                                flush=True,
                            )
                            if _chain_budget_remaining() <= 0:
                                print(f"[AFZ-DEADLINE] chain budget exhausted after {time.monotonic() - (_chain_deadline - CHAIN_TOTAL_BUDGET):.1f}s, idx={_fb_next_idx}, refusing further fallback", flush=True)
                            elif _emit.may_fallback(f"upstream_{resp.status_code}"):
                                # Currently unreachable post-emission (this block
                                # runs before any content yield), but guarded so
                                # the invariant holds uniformly if the ordering
                                # ever changes.
                                raise _ComboFallbackNeeded(resp.status_code, err_text[:500], _fb_retry_state)
                        err_event = {
                            "type": "error",
                            "error": {"type": "upstream_error", "message": err_text[:1000]},
                        }
                        yield f"event: error\ndata: {json.dumps(err_event)}\n\n".encode("utf-8")
                        yield b"event: message_stop\ndata: {\"type\": \"message_stop\"}\n\n"
                        return

                    async def _raw_ok():
                        nonlocal buffer
                        async for chunk in _transport_guarded(_stall_watchdog(resp.aiter_raw()), stats, _emit):
                            _detector.feed(chunk)
                            if await _client_disconnected():
                                stats["error"] = stats["error"] or "client_disconnected"
                                stats["status"] = 499
                                break
                            try:
                                text_chunk = chunk.decode("utf-8")
                                if stats["ttft"] == 0.0:
                                    _is_openai_content = (
                                        ('"content":"' in text_chunk and '"content":""' not in text_chunk) or
                                        ('"content": "' in text_chunk and '"content": ""' not in text_chunk) or
                                        ('"reasoning_content":"' in text_chunk and '"reasoning_content":""' not in text_chunk) or
                                        ('"text":"' in text_chunk and '"text":""' not in text_chunk)
                                    )
                                    _is_gemini_content = (
                                        '"candidates"' in text_chunk or
                                        '"parts"' in text_chunk or
                                        '"role":"' in text_chunk
                                    )
                                    _is_anthropic_content = (
                                        '"text_delta"' in text_chunk or
                                        '"content_block_delta"' in text_chunk or
                                        '"message_delta"' in text_chunk or
                                        '"thinking_delta"' in text_chunk
                                    )
                                    if _is_openai_content or _is_gemini_content or _is_anthropic_content:
                                        stats["ttft"] = time.time() - start_time
                                buffer += text_chunk
                                lines = buffer.split("\n")
                                buffer = lines.pop()
                                for line in lines:
                                    if line.startswith("data: ") and line.strip() != "data: [DONE]":
                                        try:
                                            dj = json.loads(line[6:])
                                            if dj.get("usage"):
                                                u = dj["usage"]
                                                stats["in"], stats["out"], stats["cached"] = _extract_usage_tokens(u)
                                                stats["cache_write"] = _extract_cache_write_tokens(u)
                                        except json.JSONDecodeError:
                                            pass
                            except Exception:
                                pass
                            _emit.mark_emitted(chunk)
                            yield chunk
                        if stats.get("status") == 200 and stats.get("out", 0) == 0:
                            _zero_next_idx = (_retry_state["idx"] + 1) if _retry_state else 1
                            if active_chain and _zero_next_idx < len(active_chain):
                                _zero_retry_state = {
                                    "chain": active_chain,
                                    "idx": _zero_next_idx,
                                    "cache_bp": _cache_breakpoints,
                                    "original_model": original_model,
                                    "deadline": _chain_deadline,
                                }
                                print(
                                    f"[Combo Fallback] '{model}' Anthropic stream 200-with-0-tokens for "
                                    f"{target_model}/{provider_name} — advancing to entry {_zero_next_idx}",
                                    flush=True,
                                )
                                # FREEZE FIX: a provider that omits usage data
                                # leaves out==0 even when real content streamed.
                                # Falling back here would splice a second stream
                                # into a live parser. Only advance pre-emission.
                                if _emit.may_fallback("zero_output_tokens"):
                                    raise _ComboFallbackNeeded(504, "zero_output_tokens", _zero_retry_state)
                                else:
                                    # FREEZE FIX (2026-08-04): fallback refused
                                    # (post-emission). The Anthropic client only
                                    # considers the stream closed after message_stop;
                                    # without a terminal frame it sits mid-message on a
                                    # closed socket and hangs forever. The normalizer
                                    # would otherwise emit a SILENT empty turn — emit
                                    # the error visibly instead.
                                    for _tf in _anthropic_terminal_error_frames("zero_output_tokens", target_model):
                                        try:
                                            yield _tf
                                        except Exception:
                                            pass
                                    return
                        if stats.get("error") in ("ttft_stall", "stream_stall"):
                            _stall_next_idx = (_retry_state["idx"] + 1) if _retry_state else 1
                            if not stats.get("ttft") and active_chain and _stall_next_idx < len(active_chain):
                                _stall_retry_state = {
                                    "chain": active_chain,
                                    "idx": _stall_next_idx,
                                    "cache_bp": _cache_breakpoints,
                                    "original_model": original_model,
                                    "deadline": _chain_deadline,
                                }
                                print(
                                    f"[Combo Fallback] '{model}' Anthropic stream stall for "
                                    f"{target_model}/{provider_name} — advancing to entry {_stall_next_idx}",
                                    flush=True,
                                )
                                if _chain_budget_remaining() <= 0:
                                    print(f"[AFZ-DEADLINE] chain budget exhausted after {time.monotonic() - (_chain_deadline - CHAIN_TOTAL_BUDGET):.1f}s, idx={_stall_next_idx}, refusing further fallback", flush=True)
                                elif _emit.may_fallback(stats.get("error") or "stream_stall"):
                                    raise _ComboFallbackNeeded(504, stats.get("error") or "stream_stall", _stall_retry_state)
                                else:
                                    # FREEZE FIX (2026-08-04): fallback refused
                                    # (post-emission). Emit an Anthropic terminal
                                    # sequence so the client unblocks instead of
                                    # hanging mid-message on a closed socket.
                                    for _tf in _anthropic_terminal_error_frames(stats.get("error") or "stream_stall", target_model):
                                        try:
                                            yield _tf
                                        except Exception:
                                            pass
                                    return

                    async def _raw_ok_chain():
                        async for _c in _raw_ok():
                            yield _c
                        if _detector.truncated and not _cont_state["used"]:
                            _cont_state["used"] = True
                            try:
                                _cont_payload = build_continuation_stream_payload(upstream_payload, _detector.partial_text)
                                if _cont_payload:
                                    print(
                                        f"[AntiStop] {f_val} truncated — splicing continuation "
                                        f"(Anthropic egress, {len(_detector.partial_text)} chars)",
                                        flush=True,
                                    )
                                    _cont_resp = await _send_stream_with_thinking_fallback(
                                        stream_req=_build_req(_cont_payload), stream_payload=_cont_payload
                                    )
                                    _cont_resp.raise_for_status()
                                    try:
                                        async for _cc in _transport_guarded(_stall_watchdog(_cont_resp.aiter_raw()), stats, _emit):
                                            yield _cc
                                    finally:
                                        try:
                                            await _cont_resp.aclose()
                                        except Exception:
                                            pass
                            except Exception as _ce:
                                print(f"[AntiStop] continuation failed (fail-open): {_ce}", flush=True)

                    # BUG A gate: rescue text-form tool calls only when the
                    # request declared tools. Fail-open: any doubt -> False.
                    _req_has_tools = isinstance(upstream_payload, dict) and bool(upstream_payload.get("tools"))
                    normalizer = StreamNormalizer("openai_sse", "anthropic_sse", tools_in_request=_req_has_tools)
                    async for out in normalizer.convert_openai_to_anthropic(_raw_ok_chain()):
                        yield out
                except (GeneratorExit, asyncio.CancelledError):
                    stats["error"] = stats["error"] or "client_disconnected"
                    stats["status"] = 499
                    raise
                except Exception as e:
                    stats["error"] = str(e)
                    err_event = {
                        "type": "error",
                        "error": {"type": "proxy_error", "message": str(e)},
                    }
                    yield f"event: error\ndata: {json.dumps(err_event)}\n\n".encode("utf-8")
                    yield b"event: message_stop\ndata: {\"type\": \"message_stop\"}\n\n"
                finally:
                    if resp is not None:
                        try:
                            await resp.aclose()
                        except Exception:
                            pass
                    # FREEZE FIX: surface refused post-emission fallbacks. A
                    # non-empty line here means a leaf died mid-stream — the
                    # exact scenario that used to freeze the IDE.
                    try:
                        _refusal = _emit.refusal_log(model=target_model, provider=provider_name)
                        if _refusal:
                            print(_refusal, flush=True)
                    except Exception:
                        pass
                    try:
                        obs.log_request(
                            provider=provider_name, model=target_model, status=stats["status"],
                            ttft=stats["ttft"], in_tokens=stats["in"], out_tokens=stats["out"],
                            cached_tokens=stats["cached"], config=config, error_msg=stats["error"],
                            total_time=time.time() - start_time,
                            request_id=request_id, client=client_label, stream=True, upstream_url=_upstream_url,
                            conn_index=_active_conn_index,
                            thinking=thinking_info,
                            cache_write_tokens=stats.get("cache_write", 0),
                            combo=_combo_label,
                        )
                    except Exception as _log_err:
                        print(f"[BSL Router] finally-log failed (non-blocking): {_log_err}", flush=True)

            async def egress_stream_guarded():
                try:
                    async for _c in egress_stream():
                        yield _c
                except _ComboFallbackNeeded as _cf_egr:
                    _egr_fallback = await _process_chat_completion(
                        body, client_wants_anthropic, client_wants_gemini,
                        _retry_state=_cf_egr.retry_state,
                        request=request,
                    )
                    if hasattr(_egr_fallback, "body_iterator"):
                        async for _fc in _egr_fallback.body_iterator:
                            yield _fc
                    else:
                        async for _fc in _egr_fallback:
                            yield _fc
                except (GeneratorExit, asyncio.CancelledError):
                    raise
                except Exception as _egr_guarded_err:
                    # ANTI-FREEZE (2026-08-01): bench mid-stream exception leaf.
                    bench_leaf(config, provider_name, target_model, stats.get("status", 502), stats.get("error") or str(_egr_guarded_err), stats.get("out", 0))
                    try:
                        obs.log_request(
                            provider=provider_name, model=target_model,
                            status=stats["status"], ttft=stats["ttft"],
                            in_tokens=stats["in"], out_tokens=stats["out"],
                            cached_tokens=stats["cached"], config=config,
                            error_msg=stats["error"] or "stream_interrupted",
                            total_time=time.time() - start_time,
                            request_id=request_id, client=client_label, stream=True,
                            upstream_url=_upstream_url, conn_index=_active_conn_index,
                            thinking=thinking_info,
                            cache_write_tokens=stats.get("cache_write", 0),
                            combo=_combo_label,
                        )
                    except Exception:
                        pass
                    # FREEZE FIX (2026-08-01): never return an empty 200 stream.
                    # Emit terminal error + [DONE] so the client unblocks.
                    try:
                        _egr_err_frame = {"error": {"message": stats.get("error") or str(_egr_guarded_err) or "stream_interrupted", "type": "proxy_error"}}
                        yield f"data: {json.dumps(_egr_err_frame)}\n\n".encode("utf-8")
                        yield b"data: [DONE]\n\n"
                    except Exception:
                        pass
                    return

            _afz_sid = next_stream_id()
            return StreamingResponse(
                afz_guard(
                    egress_stream_guarded(),
                    _afz_sid,
                    protocol="anthropic" if client_wants_anthropic else "openai",
                ),
                media_type="text/event-stream",
            )
        if convert_gemini_egress:
            def _next_gemini_combo_retry_state():
                _next_idx = (_retry_state["idx"] + 1) if _retry_state else 1
                if not active_chain or _next_idx >= len(active_chain):
                    return None
                return {
                    "chain": active_chain,
                    "idx": _next_idx,
                    "cache_bp": _cache_breakpoints,
                    "original_model": original_model,
                    "deadline": _chain_deadline,
                }

            def _raise_gemini_combo_fallback(status_code: int, err_text: str, emit=None) -> None:
                """Advance the combo chain, unless bytes already reached the client.

                FREEZE FIX: this helper is called from 5 sites and used to raise
                unconditionally. Guarding it here covers every caller at one
                chokepoint. `emit` is optional so a caller that provably cannot
                have emitted yet may omit it.
                """
                _next_state = _next_gemini_combo_retry_state()
                if _next_state is None:
                    return
                if emit is not None and not emit.may_fallback(err_text or f"gemini_{status_code}"):
                    # Post-emission: the client is mid-parse. Return so the caller
                    # falls through to its terminal error+DONE frame instead.
                    return
                print(
                    f"[Combo Fallback] '{model}' {err_text} for "
                    f"{target_model}/{provider_name} (gemini) — advancing to "
                    f"entry {_next_state['idx']}",
                    flush=True,
                )
                if _chain_budget_remaining() <= 0:
                    print(f"[AFZ-DEADLINE] chain budget exhausted after {time.monotonic() - (_chain_deadline - CHAIN_TOTAL_BUDGET):.1f}s, idx={_next_state['idx']}, refusing further fallback", flush=True)
                else:
                    raise _ComboFallbackNeeded(status_code, err_text, _next_state)

            async def gemini_egress_stream():
                resp = None
                state = {
                    "is_antigravity": bool(request and "antigravity" in request.headers.get("user-agent", "").lower())
                }
                emitted_model_data = False
                # FREEZE FIX: this generator was already correct (the flag below
                # is set immediately before the client-facing yield). Unified
                # onto the shared guard so all four stream paths report refusals
                # through one mechanism instead of two.
                # NOTE: the heartbeat/keepalive yields below are SSE COMMENTS
                # (": ..."), which parsers discard. They are deliberately NOT
                # marked as emission — marking them would wrongly disable all
                # fallback for every Gemini request.
                _emit = StreamEmissionState()
                try:
                    yield b": heartbeat\n\n"

                    if _probe_resp is not None:
                        resp = _probe_resp
                    else:
                        _conn_task = asyncio.ensure_future(_send_stream_with_thinking_fallback())
                        _conn_timeout = HEADER_WAIT_TIMEOUT if thinking_info else GEMINI_EGRESS_CONNECT_TIMEOUT
                        _conn_deadline = asyncio.get_running_loop().time() + _conn_timeout

                        async def _cancel_connection_task():
                            if not _conn_task.done():
                                _conn_task.cancel()
                            try:
                                _leaked = await _conn_task
                                if _leaked is not None and hasattr(_leaked, 'aclose'):
                                    try:
                                        await _leaked.aclose()
                                    except Exception:
                                        pass
                            except asyncio.CancelledError:
                                # BUG I: this used to be `except (CancelledError,
                                # Exception): pass`, which discarded BOTH the
                                # expected cancellation of _conn_task AND a
                                # cancellation delivered to THIS coroutine by
                                # force_stop_all(). Cancellation is cooperative:
                                # swallowing it means the Stop button silently
                                # does nothing while force-stop reports success.
                                #
                                # Distinguish the two: if _conn_task is the thing
                                # that got cancelled, that is the expected cleanup
                                # path. Otherwise WE are being stopped and the
                                # exception must propagate.
                                if not _conn_task.cancelled():
                                    raise
                            except Exception:
                                pass

                        try:
                            while not _conn_task.done():
                                _remaining = _conn_deadline - asyncio.get_running_loop().time()
                                if _remaining <= 0:
                                    await _cancel_connection_task()
                                    stats["status"] = 504
                                    stats["error"] = "upstream_header_timeout"
                                    _raise_gemini_combo_fallback(504, stats["error"], _emit)
                                    # _raise_gemini_combo_fallback raises _ComboFallbackNeeded; no need to raise again
                                await asyncio.wait(
                                    {_conn_task},
                                    timeout=min(GEMINI_EGRESS_CONNECT_KEEPALIVE_INTERVAL, _remaining),
                                )
                                if _conn_task.done():
                                    break
                                if await _client_disconnected():
                                    stats["error"] = "client_disconnected"
                                    stats["status"] = 499
                                    await _cancel_connection_task()
                                    return
                                if asyncio.get_running_loop().time() >= _conn_deadline:
                                    await _cancel_connection_task()
                                    stats["status"] = 504
                                    stats["error"] = "upstream_header_timeout"
                                    _raise_gemini_combo_fallback(504, stats["error"], _emit)
                                    # _raise_gemini_combo_fallback raises _ComboFallbackNeeded; no need to raise again
                                yield b": keepalive\n\n"
                            resp = await _conn_task
                        except (GeneratorExit, asyncio.CancelledError):
                            await _cancel_connection_task()
                            stats["error"] = stats["error"] or "client_disconnected"
                            stats["status"] = 499
                            raise
                        except (httpx.TimeoutException, httpx.TransportError, asyncio.TimeoutError) as _transport_err:
                            await _cancel_connection_task()
                            stats["status"] = 502 if isinstance(_transport_err, httpx.TransportError) else 504
                            stats["error"] = str(_transport_err) or type(_transport_err).__name__
                            bench_leaf(config, provider_name, target_model, stats["status"], stats["error"], stats.get("out", 0))
                            # UNIVERSAL ANTI-FREEZE (2026-08-02): Connect timeout during
                            # pre-content phase (only heartbeat sent, no model data yet).
                            # Safe to combo-fallback — the IDE hasn't started parsing
                            # model output, so we can retry with the next chain entry.
                            if not emitted_model_data:
                                _raise_gemini_combo_fallback(stats["status"], stats["error"], _emit)
                            # POST-CONTENT: IDE is already parsing model output.
                            # Cannot splice a different model mid-stream. Emit
                            # the SOLE terminal contract (terminal_error_frame)
                            # and return — no preceding bare error frame.
                            # FREEZE FIX (2026-08-07): a top-level {"error":...}
                            # poisons the Gemini parser into an error state where
                            # it stops consuming the subsequent finishReason
                            # candidate, so the IDE hangs on a closed socket.
                            from app.compat.adapters.gemini import sse_data as _g_sse_data, SSE_DONE as _G_SSE_DONE, terminal_error_frame as _g_term
                            yield _g_sse_data(_g_term(stats["status"], stats["error"], target_model))
                            yield _G_SSE_DONE
                            return
                    stats["status"] = resp.status_code

                    if resp.status_code != 200:
                        try:
                            raw_err = await asyncio.wait_for(resp.aread(), timeout=5.0)
                        except asyncio.TimeoutError:
                            raw_err = b"(error body read timed out)"
                        try:
                            err_text = raw_err.decode("utf-8")
                        except Exception:
                            err_text = str(raw_err)
                        stats["error"] = err_text[:500]
                        bench_leaf(config, provider_name, target_model, resp.status_code, err_text[:500], stats.get("out", 0))
                        if _next_gemini_combo_retry_state() is not None:
                            _raise_gemini_combo_fallback(resp.status_code, err_text, _emit)
                        # FREEZE FIX (2026-08-07): no eligible leaf remains (or
                        # budget exhausted -> _raise_gemini_combo_fallback
                        # returned without raising). Emit the SOLE terminal
                        # contract and return. MUST NOT silently fall through,
                        # and MUST NOT emit a preceding top-level {"error":...}
                        # frame — that poisons the Gemini parser (see
                        # terminal_error_frame) and re-introduces the freeze.
                        from app.compat.adapters.gemini import sse_data as _g_sse_data, SSE_DONE as _G_SSE_DONE, terminal_error_frame as _g_term
                        yield _g_sse_data(_g_term(resp.status_code, err_text, target_model))
                        yield _G_SSE_DONE
                        return

                    async def _raw_ok():
                        buffer = ""
                        async for chunk in _transport_guarded(_stall_watchdog(resp.aiter_raw()), stats, _emit):
                            _detector.feed(chunk)
                            if await _client_disconnected():
                                stats["error"] = stats["error"] or "client_disconnected"
                                stats["status"] = 499
                                break
                            try:
                                _c = chunk.decode("utf-8")
                                if stats["ttft"] == 0.0:
                                    _is_openai_content = (
                                        ('"content":"' in _c and '"content":""' not in _c) or
                                        ('"content": "' in _c and '"content": ""' not in _c) or
                                        ('"reasoning_content":"' in _c and '"reasoning_content":""' not in _c) or
                                        ('"text":"' in _c and '"text":""' not in _c)
                                    )
                                    _is_gemini_content = (
                                        '"candidates"' in _c or
                                        '"parts"' in _c or
                                        '"role":"' in _c
                                    )
                                    _is_anthropic_content = (
                                        '"text_delta"' in _c or
                                        '"content_block_delta"' in _c or
                                        '"message_delta"' in _c or
                                        '"thinking_delta"' in _c
                                    )
                                    if _is_openai_content or _is_gemini_content or _is_anthropic_content:
                                        stats["ttft"] = time.time() - start_time
                                buffer += _c
                                lines = buffer.split("\n")
                                buffer = lines.pop()
                                for line in lines:
                                    stripped = line.strip()
                                    if not stripped.startswith("data: ") or stripped == "data: [DONE]":
                                        continue
                                    try:
                                        dj = json.loads(stripped[6:])
                                    except json.JSONDecodeError:
                                        continue
                                    if dj.get("usage"):
                                        _i, _o, _c = _extract_usage_tokens(dj["usage"])
                                        stats["in"] = _i or stats["in"]
                                        stats["out"] = _o or stats["out"]
                                        stats["cached"] = _c or stats["cached"]
                                        stats["cache_write"] = _extract_cache_write_tokens(dj["usage"]) or stats["cache_write"]
                                    elif dj.get("type") == "message_start":
                                        u = (dj.get("message", {}) or {}).get("usage", {}) or {}
                                        _i, _o, _c = _extract_usage_tokens(u)
                                        stats["in"] = _i or stats["in"]
                                        stats["cached"] = _c or stats["cached"]
                                        stats["cache_write"] = _extract_cache_write_tokens(u) or stats["cache_write"]
                                    elif dj.get("type") == "message_delta":
                                        u = dj.get("usage", {}) or {}
                                        stats["out"] = u.get("output_tokens", stats["out"])
                            except Exception:
                                pass
                            yield chunk

                    if _is_anthropic_fmt:
                        _normalizer = StreamNormalizer(
                            "anthropic_sse", "openai_sse",
                            model_name=target_model or "bsl-routed",
                        )
                        openai_source = _normalizer.convert_anthropic_to_openai(_raw_ok())
                    else:
                        openai_source = _raw_ok()

                    async def _openai_source_chain():
                        async for _c in openai_source:
                            yield _c
                        if _detector.truncated and not _cont_state["used"]:
                            _cont_state["used"] = True
                            try:
                                _cont_payload = build_continuation_stream_payload(upstream_payload, _detector.partial_text)
                                if _cont_payload:
                                    print(
                                        f"[AntiStop] {f_val} truncated — splicing continuation "
                                        f"(Gemini egress, {len(_detector.partial_text)} chars)",
                                        flush=True,
                                    )
                                    _cont_resp = await _send_stream_with_thinking_fallback(
                                        stream_req=_build_req(_cont_payload), stream_payload=_cont_payload
                                    )
                                    _cont_resp.raise_for_status()
                                    try:
                                        _cont_raw = _transport_guarded(_stall_watchdog(_cont_resp.aiter_raw()), stats, _emit)
                                        if _is_anthropic_fmt:
                                            _cont_src = _normalizer.convert_anthropic_to_openai(_cont_raw)
                                        else:
                                            _cont_src = _cont_raw
                                        async for _cc in _cont_src:
                                            yield _cc
                                    finally:
                                        try:
                                            await _cont_resp.aclose()
                                        except Exception:
                                            pass
                            except Exception as _ce:
                                print(f"[AntiStop] continuation failed (fail-open): {_ce}", flush=True)

                    obuffer = ""
                    _q: asyncio.Queue = asyncio.Queue()
                    _SENTINEL = object()
                    _drain_err = None

                    async def _drain_openai_source():
                        nonlocal _drain_err
                        try:
                            async for _c in _openai_source_chain():
                                await _q.put(_c)
                        except Exception as _de:
                            _drain_err = _de
                        finally:
                            await _q.put(_SENTINEL)

                    _drain_task = asyncio.ensure_future(_drain_openai_source())
                    try:
                        while True:
                            try:
                                ochunk = await asyncio.wait_for(
                                    _q.get(), timeout=GEMINI_EGRESS_KEEPALIVE_INTERVAL
                                )
                            except asyncio.TimeoutError:
                                if await _client_disconnected():
                                    stats["error"] = stats["error"] or "client_disconnected"
                                    stats["status"] = 499
                                    break
                                yield b": keepalive\n\n"
                                continue
                            if ochunk is _SENTINEL:
                                break
                            try:
                                obuffer += ochunk.decode("utf-8")
                                olines = obuffer.split("\n")
                                obuffer = olines.pop()
                                for line in olines:
                                    stripped = line.strip()
                                    if not stripped.startswith("data: ") or stripped == "data: [DONE]":
                                        continue
                                    try:
                                        openai_chunk = json.loads(stripped[6:])
                                    except json.JSONDecodeError:
                                        continue
                                    g = openai_chunk_to_gemini(openai_chunk, state)
                                    if g is not None:
                                        from app.compat.adapters.gemini import (
                                            sse_data as _g_sse_data,
                                            gemini_frame_has_content as _gemini_frame_has_content,
                                        )
                                        # BUG L (2026-08-04) - THE `after 0B` FREEZE.
                                        # Evidence: "[STREAM-GUARD] refused 1
                                        # post-emission fallback(s) after 0B
                                        # [stream_stall]" - the gate reported
                                        # post-emission while counting ZERO bytes.
                                        #
                                        # Cause: openai_chunk_to_gemini returns
                                        # non-None for frames with NO renderable
                                        # content - a usage-only chunk yields
                                        # parts=[{"text": ""}] (gemini.py:577-583),
                                        # as does a finish with no parts (:652-654).
                                        # This site called mark_emitted() with no
                                        # argument, so byte_count stayed 0 while
                                        # emitted flipped True. From then on
                                        # may_fallback() refused EVERY fallback, so
                                        # a later stall with Out:0 could not fail
                                        # over and the IDE waited on a stream that
                                        # never produced output.
                                        #
                                        # The gate's real question is "has the
                                        # client rendered something that a second
                                        # provider would corrupt?" - empty parts
                                        # and usage metadata answer NO. So mark
                                        # emission only for RENDERABLE content, and
                                        # pass the frame so byte_count stays
                                        # consistent with `emitted`.
                                        _frame = g if "response" in g else {"response": g}
                                        _payload = _g_sse_data(_frame)
                                        if _gemini_frame_has_content(_frame):
                                            emitted_model_data = True
                                            _emit.mark_emitted(_payload)
                                        yield _payload
                            except Exception:
                                pass
                    finally:
                        if not _drain_task.done():
                            _drain_task.cancel()
                        try:
                            await _drain_task
                        except BaseException:
                            pass
                        try:
                            _close_source = getattr(openai_source, "aclose", None)
                            if _close_source is not None:
                                await _close_source()
                        except BaseException:
                            pass

                    # leaf so the NEXT request starts on the fallback combo entry.
                    if stats.get("error") in ("ttft_stall", "stream_stall"):
                        stats["status"] = 504
                        # UNIVERSAL ANTI-FREEZE (2026-08-02): Distinguish between
                        # pre-content and post-content stalls:
                        # - PRE-CONTENT (emitted_model_data=False): IDE is still
                        #   waiting for first byte. Safe to combo-fallback and
                        #   retry with next model in chain.
                        # - POST-CONTENT (emitted_model_data=True): IDE is parsing
                        #   partial stream. Cannot splice a different model mid-stream
                        #   (wedges the IDE parser). Must fail-fast with error+DONE.
                        # Uses the shared guard rather than emitted_model_data so
                        # the refusal is COUNTED and LOGGED. The two are kept in
                        # sync (both set at the client-facing yield); the guard is
                        # the one that reports.
                        if _emit.may_fallback(stats.get("error") or "gemini_pre_content_stall"):
                            # PRE-CONTENT: Try combo fallback before giving up
                            _pre_next_idx = (_retry_state["idx"] + 1) if _retry_state else 1
                            if active_chain and _pre_next_idx < len(active_chain):
                                _pre_retry_state = {
                                    "chain": active_chain,
                                    "idx": _pre_next_idx,
                                    "cache_bp": _cache_breakpoints,
                                    "original_model": original_model,
                                    "deadline": _chain_deadline,
                                }
                                print(
                                    f"[Combo Fallback] '{model}' Gemini pre-content stall for "
                                    f"{target_model}/{provider_name} — advancing to entry {_pre_next_idx}",
                                    flush=True,
                                )
                                if _chain_budget_remaining() <= 0:
                                    print(f"[AFZ-DEADLINE] chain budget exhausted after {time.monotonic() - (_chain_deadline - CHAIN_TOTAL_BUDGET):.1f}s, idx={_pre_next_idx}, refusing further fallback", flush=True)
                                else:
                                    raise _ComboFallbackNeeded(504, stats.get("error") or "stream_stall", _pre_retry_state)
                        # POST-CONTENT or no fallback available: bench the dead leaf
                        # so the combo's next request skips it.
                        try:
                            import app.error_prevention as _ep_stall
                            _ep_stall.record_outcome(
                                config, provider_name, target_model, 504,
                                stats.get("error") or "stream_stall", out_tokens=0,
                            )
                        except Exception as _bench_err:
                            print(f"[ErrorPrevention] stall bench failed (non-blocking): {_bench_err}", flush=True)
                        from app.compat.adapters.gemini import sse_data as _g_sse_data, SSE_DONE as _G_SSE_DONE, terminal_error_frame as _g_term
                        # FREEZE FIX (2026-08-07): SOLE terminal contract. No
                        # preceding bare {"error":...} frame — it poisons the
                        # Gemini parser (see terminal_error_frame docstring).
                        yield _g_sse_data(_g_term(504, stats.get("error") or "stream_stall", target_model))
                        yield _G_SSE_DONE
                        return
                    # Zero-token 200: stream COMPLETED with status 200 but produced
                    # zero output tokens — dead leaf. Advance combo if possible.
                    if stats.get("status") == 200 and stats.get("out", 0) == 0 and not emitted_model_data:
                        _raise_gemini_combo_fallback(504, "zero_output_tokens", _emit)
                        # FREEZE FIX (2026-08-07): if we reach here, no eligible
                        # leaf/budget remained and _raise_gemini_combo_fallback
                        # returned WITHOUT raising. Emit the SOLE terminal contract
                        # and return — never fall through to a bare [DONE], which
                        # the Gemini parser cannot end on (no finishReason).
                        stats["status"] = 504
                        stats["error"] = "zero_output_tokens"
                        from app.compat.adapters.gemini import sse_data as _g_sse_data, SSE_DONE as _G_SSE_DONE, terminal_error_frame as _g_term
                        yield _g_sse_data(_g_term(504, "zero_output_tokens", target_model))
                        yield _G_SSE_DONE
                        return
                    if _drain_err is not None:
                        if isinstance(_drain_err, (httpx.TimeoutException, httpx.TransportError)):
                            stats["status"] = 504 if isinstance(_drain_err, httpx.TimeoutException) else 502
                            stats["error"] = str(_drain_err) or type(_drain_err).__name__
                            if not emitted_model_data:
                                _raise_gemini_combo_fallback(stats["status"], stats["error"], _emit)
                        # FREEZE FIX (2026-07-22): Same as above — do NOT raise _drain_err
                        # after data has been yielded. Emit error frame + [DONE] and return.
                        from app.compat.adapters.gemini import sse_data as _g_sse_data, SSE_DONE as _G_SSE_DONE, terminal_error_frame as _g_term
                        # FREEZE FIX (2026-08-07): SOLE terminal contract; no
                        # bare {"error":...} prefix (poisons Gemini parser).
                        yield _g_sse_data(_g_term(stats.get("status", 502), str(_drain_err) or "upstream drain error", target_model))
                        yield _G_SSE_DONE
                        return
                    if stats.get("status") == 499:
                        # FREEZE FIX 2026-07-25: emit [DONE] before return so the
                        # Gemini SSE client (Antigravity IDE) unblocks. Without this,
                        # the client waits forever for stream end -> freeze.
                        try:
                            from app.compat.adapters.gemini import SSE_DONE as _G_SSE_DONE_499
                            yield _G_SSE_DONE_499
                        except Exception:
                            pass
                        return
                    from app.compat.adapters.gemini import SSE_DONE as _G_SSE_DONE
                    yield _G_SSE_DONE
                except (GeneratorExit, asyncio.CancelledError):
                    # A real downstream cancellation is never a provider fallback.
                    stats["error"] = stats["error"] or "client_disconnected"
                    stats["status"] = 499
                    raise
                except _ComboFallbackNeeded:
                    raise
                except Exception as e:
                    stats["error"] = stats["error"] or str(e)
                    error_code = stats["status"] if isinstance(stats["status"], int) and stats["status"] >= 400 else 500
                    from app.compat.adapters.gemini import sse_data as _g_sse_data, SSE_DONE as _G_SSE_DONE, terminal_error_frame as _g_term
                    # FREEZE FIX (2026-08-07): SOLE terminal contract; no bare
                    # {"error":...} prefix (poisons Gemini parser -> freeze).
                    yield _g_sse_data(_g_term(error_code, str(e), target_model))
                    yield _G_SSE_DONE
                    # Do NOT re-raise: generator must return cleanly so FastAPI
                    # flushes the error+[DONE] frames before closing the HTTP
                    # response. Re-raising here terminates the generator before
                    # the bytes are flushed → IDE hangs forever (matches 9router
                    # t.end() pattern: emit error frame, close, done).
                    return
                finally:
                    # FIX B (2026-07-30): Wrap resp.aclose() in try/except so a
                    # failed close (e.g. socket already reset) doesn't skip logging
                    # or leave the connection in a half-open state that poisons the
                    # keepalive pool for subsequent requests.
                    if resp is not None:
                        try:
                            await resp.aclose()
                        except Exception:
                            pass
                    # FREEZE FIX: report refused post-emission fallbacks.
                    try:
                        _r = _emit.refusal_log(model=target_model, provider=provider_name)
                        if _r:
                            print(_r, flush=True)
                    except Exception:
                        pass
                    try:
                        obs.log_request(
                            provider=provider_name, model=target_model, status=stats["status"],
                            ttft=stats["ttft"], in_tokens=stats["in"], out_tokens=stats["out"],
                            cached_tokens=stats["cached"], config=config, error_msg=stats["error"],
                            total_time=time.time() - start_time,
                            request_id=request_id, client=client_label, stream=True, upstream_url=_upstream_url,
                            conn_index=_active_conn_index,
                            thinking=thinking_info,
                            cache_write_tokens=stats.get("cache_write", 0),
                            combo=_combo_label,
                        )
                    except Exception as _log_err:
                        print(f"[BSL Router] finally-log failed (non-blocking): {_log_err}", flush=True)

            async def gemini_egress_stream_guarded():
                _source = gemini_egress_stream()
                try:
                    async for _c in _source:
                        yield _c
                except _ComboFallbackNeeded as _cf:
                    _fallback = await _process_chat_completion(
                        body, client_wants_anthropic, client_wants_gemini,
                        _retry_state=_cf.retry_state,
                        request=request,
                    )
                    if hasattr(_fallback, "body_iterator"):
                        async for _fc in _fallback.body_iterator:
                            yield _fc
                    else:
                        async for _fc in _fallback:
                            yield _fc
                except (GeneratorExit, asyncio.CancelledError):
                    raise
                except Exception:
                    try:
                        obs.log_request(
                            provider=provider_name, model=target_model,
                            status=stats["status"], ttft=stats["ttft"],
                            in_tokens=stats["in"], out_tokens=stats["out"],
                            cached_tokens=stats["cached"], config=config,
                            error_msg=stats["error"] or "stream_interrupted",
                            total_time=time.time() - start_time,
                            request_id=request_id, client=client_label, stream=True,
                            upstream_url=_upstream_url, conn_index=_active_conn_index,
                            thinking=thinking_info,
                            cache_write_tokens=stats.get("cache_write", 0),
                            combo=_combo_label,
                        )
                    except Exception:
                        pass
                    # ANTI-FREEZE (2026-08-01): bench mid-stream exception leaf.
                    bench_leaf(config, provider_name, target_model, stats.get("status", 502), stats.get("error") or "stream_interrupted", stats.get("out", 0))
                    # Emit terminal error + [DONE] then RETURN (not raise).
                    # Re-raising here kills the generator before FastAPI flushes
                    # the bytes → IDE waits forever for [DONE] → freeze.
                    # 9router equivalent: t.end(error_frame) — synchronous close.
                    try:
                        # BUG J / FREEZE FIX (2026-08-04): this previously emitted a
                        # bare {"error": ...} object plus data: [DONE]. Per the
                        # adapter's own contract (see terminal_error_frame in
                        # app/compat/adapters/gemini.py) NEITHER can end a Gemini
                        # stream: the object carries no `candidates`, and [DONE] is
                        # an OpenAI sentinel the Gemini parser discards. So the IDE
                        # sat waiting on a closed connection - the freeze. The
                        # terminal frame below carries a finishReason, which is what
                        # the parser actually terminates on. SSE_DONE is kept after
                        # it: harmless, and preserves OpenAI-shaped consumers.
                        from app.compat.adapters.gemini import (
                            sse_data as _g_sse_guarded,
                            SSE_DONE as _G_SSE_GUARDED,
                            terminal_error_frame as _g_term_guarded,
                        )
                        yield _g_sse_guarded(_g_term_guarded(
                            stats.get("status") or 502,
                            stats.get("error") or "stream_interrupted",
                            target_model,
                        ))
                        yield _G_SSE_GUARDED
                    except Exception:
                        pass
                    return
                finally:
                    try:
                        await _source.aclose()
                    except BaseException:
                        pass

            _afz_sid = next_stream_id()
            # FREEZE FIX (2026-08-04): protocol="gemini" makes the hard-deadline
            # force-close emit a finishReason-bearing frame. The previous
            # _g_err_frame below was Gemini-FLAVOURED but still had no
            # `candidates`, so the IDE could not treat it as a stream end and
            # froze when the deadline fired. Both branches now pass protocol.
            try:
                from app.compat.adapters.gemini import SSE_DONE as _G_SSE_DEADLINE
                return StreamingResponse(
                    afz_guard(gemini_egress_stream_guarded(), _afz_sid, done_frame=_G_SSE_DEADLINE, protocol="gemini"),
                    media_type="text/event-stream",
                    headers=gemini_response_headers(stream=True),
                )
            except Exception:
                return StreamingResponse(
                    afz_guard(gemini_egress_stream_guarded(), _afz_sid, protocol="gemini"),
                    media_type="text/event-stream",
                    headers=gemini_response_headers(stream=True),
                )
        if convert_anthropic_to_openai_egress:
            # Reverse egress: OpenAI client (/v1/chat/completions) but upstream is
            # Anthropic-compatible (GLM/Kimi/MiniMax). Convert Anthropic SSE → OpenAI SSE.
            async def anthropic_to_openai_egress_stream():
                resp = None
                buffer = ""
                emitted_model_data = False  # Track if we've sent data to IDE (for pre-content check)
                # FREEZE FIX: emitted_model_data above was only set at the
                # normalizer yield, which sits BELOW the stall check that reads
                # it — so that check could never observe True and the guard was
                # dead code. This state object is marked at every yield, so the
                # invariant actually holds.
                _emit = StreamEmissionState()
                try:
                    # Use pre-sent probe response if available (combo fallback status-peek)
                    if _probe_resp is not None:
                        resp = _probe_resp
                    else:
                        resp = await _send_stream_with_thinking_fallback()
                    stats["status"] = resp.status_code

                    # Error-first: surface the real upstream error as an OpenAI-shaped
                    # error chunk instead of fabricating a phantom empty success.
                    if resp.status_code != 200:
                        try:
                            raw_err = await asyncio.wait_for(resp.aread(), timeout=5.0)
                        except asyncio.TimeoutError:
                            raw_err = b"(error body read timed out)"
                        try:
                            err_text = raw_err.decode("utf-8")
                        except Exception:
                            err_text = str(raw_err)
                        stats["error"] = err_text[:500]
                        bench_leaf(config, provider_name, target_model, resp.status_code, err_text[:500], stats.get("out", 0))
                        # ALL non-200 triggers combo fallback (per user directive).
                        _fb_next_idx = (_retry_state['idx'] + 1) if _retry_state else 1
                        if active_chain and _fb_next_idx < len(active_chain):
                            _fb_retry_state = {
                                "chain": active_chain,
                                "idx": _fb_next_idx,
                                "cache_bp": _cache_breakpoints,
                                "original_model": original_model,
                                "deadline": _chain_deadline,
                            }
                            print(
                                f"[Combo Fallback] '{model}' Anthropic→OpenAI egress {resp.status_code} "
                                f"for {target_model}/{provider_name} — advancing to entry {_fb_next_idx}",
                                flush=True,
                            )
                            if _chain_budget_remaining() <= 0:
                                print(f"[AFZ-DEADLINE] chain budget exhausted after {time.monotonic() - (_chain_deadline - CHAIN_TOTAL_BUDGET):.1f}s, idx={_fb_next_idx}, refusing further fallback", flush=True)
                            elif _emit.may_fallback(f"upstream_{resp.status_code}"):
                                raise _ComboFallbackNeeded(resp.status_code, err_text[:500], _fb_retry_state)
                        # convert_anthropic_to_openai_egress = client is OpenAI-format.
                        # Emit OpenAI SSE error + [DONE] so the client unblocks.
                        # (Former Gemini candidates[] emission here was wrong — this
                        # path is never reached by Gemini or Anthropic clients.)
                        _err_oa = {
                            "error": {
                                "message": f"Upstream error {resp.status_code}: {err_text[:400]}",
                                "type": "proxy_error",
                                "code": resp.status_code,
                            }
                        }
                        yield f"data: {json.dumps(_err_oa)}\n\n".encode("utf-8")
                        yield b"data: [DONE]\n\n"
                        return

                    async def _raw_anthropic_ok():
                        nonlocal buffer
                        async for chunk in _transport_guarded(_stall_watchdog(resp.aiter_raw()), stats, _emit):
                            if await _client_disconnected():
                                stats["error"] = stats["error"] or "client_disconnected"
                                stats["status"] = 499
                                break
                            try:
                                text_chunk = chunk.decode("utf-8")
                                if stats["ttft"] == 0.0:
                                    # OpenAI format: content/reasoning_content/text fields
                                    # Gemini/Antigravity format: candidates/parts/role fields
                                    _is_openai_content = (
                                        ('"content":"' in text_chunk and '"content":""' not in text_chunk) or
                                        ('"content": "' in text_chunk and '"content": ""' not in text_chunk) or
                                        ('"reasoning_content":"' in text_chunk and '"reasoning_content":""' not in text_chunk) or
                                        ('"text":"' in text_chunk and '"text":""' not in text_chunk)
                                    )
                                    _is_gemini_content = (
                                        '"candidates"' in text_chunk or
                                        '"parts"' in text_chunk or
                                        '"role":"' in text_chunk
                                    )
                                    _is_anthropic_content = (
                                        '"text_delta"' in text_chunk or
                                        '"content_block_delta"' in text_chunk or
                                        '"message_delta"' in text_chunk or
                                        '"thinking_delta"' in text_chunk
                                    )
                                    if _is_openai_content or _is_gemini_content or _is_anthropic_content:
                                        stats["ttft"] = time.time() - start_time
                                buffer += text_chunk
                                lines = buffer.split("\n")
                                buffer = lines.pop()
                                for line in lines:
                                    if line.startswith("data: ") and line.strip() != "data: [DONE]":
                                        try:
                                            dj = json.loads(line[6:])
                                            if dj.get("type") == "message_delta":
                                                u = dj.get("usage", {})
                                                stats["out"] = u.get("output_tokens", stats["out"])
                                            elif dj.get("type") == "message_start":
                                                msg = dj.get("message", {})
                                                u = msg.get("usage", {})
                                                _i, _o, _c = _extract_usage_tokens(u)
                                                stats["in"] = _i or stats["in"]
                                                stats["cached"] = _c or stats["cached"]
                                                stats["cache_write"] = _extract_cache_write_tokens(u) or stats["cache_write"]
                                        except json.JSONDecodeError:
                                            pass
                            except Exception:
                                pass
                            _emit.mark_emitted(chunk)
                            yield chunk
                        # Zero-token 200: stream completed with status 200 but produced
                        # zero output tokens — dead leaf. Advance combo if possible.
                        if stats.get("status") == 200 and stats.get("out", 0) == 0:
                            _zero_next_idx = (_retry_state["idx"] + 1) if _retry_state else 1
                            if active_chain and _zero_next_idx < len(active_chain):
                                _zero_retry_state = {
                                    "chain": active_chain,
                                    "idx": _zero_next_idx,
                                    "cache_bp": _cache_breakpoints,
                                    "original_model": original_model,
                                    "deadline": _chain_deadline,
                                }
                                print(
                                    f"[Combo Fallback] '{model}' Anthropic→OpenAI stream 200-with-0-tokens for "
                                    f"{target_model}/{provider_name} — advancing to entry {_zero_next_idx}",
                                    flush=True,
                                )
                                if _chain_budget_remaining() <= 0:
                                    print(f"[AFZ-DEADLINE] chain budget exhausted after {time.monotonic() - (_chain_deadline - CHAIN_TOTAL_BUDGET):.1f}s, idx={_zero_next_idx}, refusing further fallback", flush=True)
                                elif _emit.may_fallback("zero_output_tokens"):
                                    # A provider that omits usage data leaves
                                    # out==0 even when real content streamed.
                                    raise _ComboFallbackNeeded(504, "zero_output_tokens", _zero_retry_state)
                                else:
                                    # FREEZE FIX (2026-08-04): fallback refused
                                    # (post-emission or budget exhausted). This
                                    # egress is normalized Anthropic->OpenAI, so the
                                    # CLIENT is OpenAI-format — emit an OpenAI error
                                    # frame + [DONE], NOT an Anthropic message_stop.
                                    # Without it the OpenAI client waits for [DONE]
                                    # on a closed socket and hangs forever.
                                    for _tf in _openai_terminal_error_frames("zero_output_tokens", target_model, 504):
                                        try:
                                            yield _tf
                                        except Exception:
                                            pass
                                    return
                        # After watchdog loop: if a body-level stall was detected and the
                        # combo chain has more entries, raise _ComboFallbackNeeded so the
                        # guarded wrapper can advance to the next chain entry (same pattern
                        # as _raise_gemini_combo_fallback used in gemini_egress_stream).
                        if stats.get("error") in ("ttft_stall", "stream_stall"):
                            _stall_next_idx = (_retry_state["idx"] + 1) if _retry_state else 1
                            # PRE-CONTENT check: only fallback if we haven't sent data to IDE yet
                            if _emit.may_fallback("stream_stall_precheck") and active_chain and _stall_next_idx < len(active_chain):
                                _stall_retry_state = {
                                    "chain": active_chain,
                                    "idx": _stall_next_idx,
                                    "cache_bp": _cache_breakpoints,
                                    "original_model": original_model,
                                    "deadline": _chain_deadline,
                                }
                                print(
                                    f"[Combo Fallback] '{model}' Anthropic stream stall for "
                                    f"{target_model}/{provider_name} — advancing to entry {_stall_next_idx}",
                                    flush=True,
                                )
                                if _chain_budget_remaining() <= 0:
                                    print(f"[AFZ-DEADLINE] chain budget exhausted after {time.monotonic() - (_chain_deadline - CHAIN_TOTAL_BUDGET):.1f}s, idx={_stall_next_idx}, refusing further fallback", flush=True)
                                elif _emit.may_fallback(stats.get("error") or "stream_stall"):
                                    raise _ComboFallbackNeeded(504, stats.get("error") or "stream_stall", _stall_retry_state)
                                else:
                                    # FREEZE FIX (2026-08-04): inner refusal
                                    # (post-emission). Emit OpenAI terminal frames
                                    # so the OpenAI-format client unblocks.
                                    for _tf in _openai_terminal_error_frames(stats.get("error") or "stream_stall", target_model, 504):
                                        try:
                                            yield _tf
                                        except Exception:
                                            pass
                                    return
                            elif not _emit.may_fallback("stream_stall_precheck"):
                                # FREEZE FIX (2026-08-04): outer refusal — bytes
                                # already emitted, so the precheck forbids fallback.
                                # The OpenAI client needs [DONE] or it hangs forever.
                                for _tf in _openai_terminal_error_frames(stats.get("error") or "stream_stall", target_model, 504):
                                    try:
                                        yield _tf
                                    except Exception:
                                        pass
                                return

                    normalizer = StreamNormalizer("anthropic_sse", "openai_sse", model_name=target_model or "bsl-routed")
                    async for out in normalizer.convert_anthropic_to_openai(_raw_anthropic_ok()):
                        emitted_model_data = True  # Mark that we've sent data to IDE
                        _emit.mark_emitted(out)
                        yield out
                    # FREEZE FIX 2026-07-25: client-disconnect path. _raw_anthropic_ok()
                    # breaks on 499, normalizer ends, but no [DONE] was emitted.
                    # Emit it now so the OpenAI SSE client unblocks.
                    if stats.get("status") == 499 or stats.get("error") == "client_disconnected":
                        try:
                            yield b"data: [DONE]\n\n"
                        except Exception:
                            pass
                except Exception as e:
                    stats["error"] = str(e)
                    err_chunk = {"error": {"message": str(e), "type": "proxy_error"}}
                    yield f"data: {json.dumps(err_chunk)}\n\n".encode("utf-8")
                    yield b"data: [DONE]\n\n"
                finally:
                    # FIX B (2026-07-30): try/except to prevent pool poisoning.
                    if resp is not None:
                        try:
                            await resp.aclose()
                        except Exception:
                            pass
                    # FREEZE FIX: report refused post-emission fallbacks.
                    try:
                        _r = _emit.refusal_log(model=target_model, provider=provider_name)
                        if _r:
                            print(_r, flush=True)
                    except Exception:
                        pass
                    try:
                        obs.log_request(
                            provider=provider_name, model=target_model, status=stats["status"],
                            ttft=stats["ttft"], in_tokens=stats["in"], out_tokens=stats["out"],
                            cached_tokens=stats["cached"], config=config, error_msg=stats["error"],
                            total_time=time.time() - start_time,
                            request_id=request_id, client=client_label, stream=True, upstream_url=_upstream_url,
                            conn_index=_active_conn_index,
                            thinking=thinking_info,
                            cache_write_tokens=stats.get("cache_write", 0),
                            combo=_combo_label,
                        )
                    except Exception as _log_err:
                        print(f"[BSL Router] finally-log failed (non-blocking): {_log_err}", flush=True)

            async def anthropic_to_openai_egress_stream_guarded():
                _anthr_source = anthropic_to_openai_egress_stream()
                try:
                    async for _c in _anthr_source:
                        yield _c
                except _ComboFallbackNeeded as _cf_anthr:
                    # Body-level upstream failure (e.g. 524 stall after headers) —
                    # advance the combo chain instead of returning an error to the client.
                    _anthr_fallback = await _process_chat_completion(
                        body, client_wants_anthropic, client_wants_gemini,
                        _retry_state=_cf_anthr.retry_state,
                        request=request,
                    )
                    if hasattr(_anthr_fallback, "body_iterator"):
                        async for _fc in _anthr_fallback.body_iterator:
                            yield _fc
                    else:
                        async for _fc in _anthr_fallback:
                            yield _fc
                except (GeneratorExit, asyncio.CancelledError):
                    try:
                        obs.log_request(
                            provider=provider_name, model=target_model, status=499,
                            ttft=stats["ttft"], in_tokens=stats["in"], out_tokens=stats["out"],
                            cached_tokens=stats["cached"], config=config,
                            error_msg="client_disconnected",
                            total_time=time.time() - start_time,
                            request_id=request_id, client=client_label, stream=True,
                            upstream_url=_upstream_url, conn_index=_active_conn_index,
                            thinking=thinking_info,
                            cache_write_tokens=stats.get("cache_write", 0),
                            combo=_combo_label,
                        )
                    except Exception:
                        pass
                    raise
                except Exception:
                    try:
                        obs.log_request(
                            provider=provider_name, model=target_model,
                            status=stats["status"], ttft=stats["ttft"],
                            in_tokens=stats["in"], out_tokens=stats["out"],
                            cached_tokens=stats["cached"], config=config,
                            error_msg=stats["error"] or "stream_interrupted",
                            total_time=time.time() - start_time,
                            request_id=request_id, client=client_label, stream=True,
                            upstream_url=_upstream_url, conn_index=_active_conn_index,
                            thinking=thinking_info,
                            cache_write_tokens=stats.get("cache_write", 0),
                            combo=_combo_label,
                        )
                    except Exception:
                        pass
                    # ANTI-FREEZE (2026-08-01): bench mid-stream exception leaf.
                    bench_leaf(config, provider_name, target_model, stats.get("status", 502), stats.get("error") or "stream_interrupted", stats.get("out", 0))
                    # Emit a terminal OpenAI-format error frame so the IDE unblocks
                    # instead of hanging forever when the Sonnet/Anthropic stream fails.
                    try:
                        _err_openai = {"error": {"message": stats.get("error") or "stream_interrupted", "type": "proxy_error"}}
                        yield f"data: {json.dumps(_err_openai)}\n\n".encode("utf-8")
                        yield b"data: [DONE]\n\n"
                    except Exception:
                        pass
                    # Return cleanly so FastAPI flushes the error+[DONE] frames.
                    # Re-raising here terminates the generator before bytes flush -> IDE hangs.
                    return
                finally:
                    # LEAK FIX (2026-08-02): close the source generator so a
                    # client disconnect reaches its finally and closes the
                    # upstream response. `async for` alone does NOT do this.
                    try:
                        await _anthr_source.aclose()
                    except BaseException:
                        pass

            _afz_sid = next_stream_id()
            return StreamingResponse(
                afz_guard(
                    anthropic_to_openai_egress_stream_guarded(),
                    _afz_sid,
                    # This generator CONVERTS Anthropic upstream -> OpenAI egress,
                    # so the client here speaks OpenAI unless it asked for
                    # Anthropic explicitly. Keyed off the client, never upstream.
                    protocol="anthropic" if client_wants_anthropic else "openai",
                ),
                media_type="text/event-stream",
            )
        # Kiro streaming egress: wrap raw upstream bytes through Kiro SSE→OpenAI SSE converter
        if provider_name == 'kiro':
            _afz_sid = next_stream_id()
            return StreamingResponse(
                afz_guard(kiro_adapter.kiro_raw_to_openai_sse(raw_upstream_guarded()), _afz_sid),
                media_type="text/event-stream",
            )
        _afz_sid = next_stream_id()
        return StreamingResponse(afz_guard(raw_upstream_guarded(), _afz_sid), media_type="text/event-stream")
    else:
        try:

            # ── Stream-Buffer helper for S3/S6 continuation calls ────
            # Rewrites payload to stream:true, accumulates via the shared
            # _accumulate_sse_stream, returns _SyntheticResponse. On
            # immediate (<2s) rejection, falls back to plain non-stream
            # send. Raises httpx.HTTPStatusError on mid-stream error.
            async def _buffered_send(_payload_to_stream, *, _label="buf"):
                _bs_t0 = time.time()
                _bs_payload = {**_payload_to_stream, "stream": True}
                if not _is_anthropic_fmt:
                    _bs_payload["stream_options"] = {"include_usage": True}
                _bs_req = _build_req(_bs_payload)
                _bs_resp = await _send_stream_with_thinking_fallback(
                    stream_req=_bs_req, stream_payload=_bs_payload
                )
                _bs_elapsed = time.time() - _bs_t0
                if _bs_resp.status_code != 200:
                    if _bs_elapsed < 2.0:
                        await _bs_resp.aclose()
                        print(
                            f"[StreamBuffer:{_label}] '{target_model}/{provider_name}' "
                            f"stream rejected ({_bs_resp.status_code} in {_bs_elapsed:.1f}s) — fail-open",
                            flush=True,
                        )
                        return await client.send(_build_req(_payload_to_stream))
                    else:
                        try:
                            _bs_err_body = await asyncio.wait_for(_bs_resp.aread(), timeout=5.0)
                        except asyncio.TimeoutError:
                            _bs_err_body = b"(error body read timed out)"
                        await _bs_resp.aclose()
                        _bs_err_text = _bs_err_body.decode("utf-8", errors="replace")
                        return _SyntheticResponse(
                            _bs_resp.status_code,
                            {"error": _bs_err_text[:1000]},
                            _bs_err_text[:1000],
                        )

                _asm = await _accumulate_sse_stream(
                    _bs_resp,
                    _is_anthropic_fmt=_is_anthropic_fmt,
                    _target_model=target_model,
                    _request=request,
                    _label=_label,
                    _thinking_info=thinking_info,
                )
                print(
                    f"[StreamBuffer:{_label}] '{target_model}/{provider_name}' "
                    f"buffered ({_asm['usage']['completion_tokens']} tokens in {time.time() - _bs_t0:.1f}s)",
                    flush=True,
                )
                return _SyntheticResponse(200, _asm)

            # ── Stream-Then-Buffer Dispatch ───────────────────────────
            # When upstream_stream_buffer is enabled, transparently rewrite
            # the non-streaming request as streaming upstream to keep bytes
            # flowing (defeats Cloudflare 524 idle timeout), then accumulate
            # SSE chunks into a single JSON response. Falls back to a real
            # non-stream call on immediate (<2s) stream rejection.
            if _apply_stream_buffer:
                _sb_t0 = time.time()
                try:
                    # Non-mutating copy — S3/S6 retries read upstream_payload.
                    _sb_payload = {**upstream_payload, "stream": True}
                    if not _is_anthropic_fmt:
                        _sb_payload["stream_options"] = {"include_usage": True}
                    _sb_req = _build_req(_sb_payload)
                    _sb_resp = await _send_stream_with_thinking_fallback(
                        stream_req=_sb_req, stream_payload=_sb_payload
                    )
                    _sb_elapsed = time.time() - _sb_t0
                    if _sb_resp.status_code != 200:
                        # Immediate rejection (<2s): upstream doesn't support
                        # stream or rejected the payload. Fall back to real
                        # non-stream call (safe — no 524 risk at <2s).
                        if _sb_elapsed < 2.0:
                            await _sb_resp.aclose()
                            print(
                                f"[StreamBuffer] '{target_model}/{provider_name}' stream rejected "
                                f"({_sb_resp.status_code} in {_sb_elapsed:.1f}s) — fail-open to non-stream",
                                flush=True,
                            )
                            resp = await client.send(req)
                        else:
                            # Late error (>2s): likely upstream issue during
                            # generation. Don't retry non-stream (would 524).
                            # Copy error body into a real-looking response.
                            _sb_err_body = await _sb_resp.aread()
                            await _sb_resp.aclose()
                            _sb_err_text = _sb_err_body.decode("utf-8", errors="replace")
                            resp = _SyntheticResponse(
                                _sb_resp.status_code,
                                {"error": _sb_err_text[:1000]},
                                _sb_err_text[:1000],
                            )
                    else:
                        # ── Accumulate SSE chunks ─────────────────────────────
                        # Drain via shared helper, wrap in _SyntheticResponse.
                        _assembled = await _accumulate_sse_stream(
                            _sb_resp,
                            _is_anthropic_fmt=_is_anthropic_fmt,
                            _target_model=target_model,
                            _request=request,
                            _label="buf",
                            _thinking_info=thinking_info,
                        )
                        print(
                            f"[StreamBuffer] '{target_model}/{provider_name}' buffered "
                            f"({_assembled['usage']['completion_tokens']} tokens in {time.time() - _sb_t0:.1f}s)",
                            flush=True,
                        )
                        resp = _SyntheticResponse(200, _assembled)
                except httpx.HTTPStatusError as _sb_http_err:
                    # StreamBuffer accumulation failure (TTFT timeout, stall, etc).
                    # Synthesize a 504 so the normal combo fallback logic below
                    # can advance to the next entry instead of propagating as an
                    # unhandled 500 to the client.
                    _sb_err_text = str(_sb_http_err)
                    print(
                        f"[StreamBuffer] '{target_model}/{provider_name}' "
                        f"accumulation error: {_sb_err_text} — synthesizing 504 for combo fallback",
                        flush=True,
                    )
                    resp = _SyntheticResponse(504, {"error": _sb_err_text[:1000]})
                except Exception as _sb_exc:
                    print(
                        f"[StreamBuffer] '{target_model}/{provider_name}' dispatch failed: {_sb_exc} — fail-open to non-stream",
                        flush=True,
                    )
                    resp = await client.send(req)
            else:
                # ── Hardened non-stream send + generation budget ─────────────────────
                # Unified policy: non-stream total budget bounds how long we
                # wait for a single leaf to return. Transport/serialization
                # errors advance the combo fallback chain.
                _oauth_401_retried = False
                try:
                    resp = await asyncio.wait_for(
                        client.send(req), timeout=max(1.0, min(NONSTREAM_TOTAL_BUDGET, _chain_budget_remaining()))
                    )
                except asyncio.TimeoutError:
                    _budget_elapsed = time.time() - start_time
                    _budget_err = (
                        f"generation_budget_exceeded ({_budget_elapsed:.1f}s > "
                        f"{NONSTREAM_TOTAL_BUDGET}s)"
                    )
                    print(
                        f"[Combo Fallback] '{model}' non-stream budget exceeded for "
                        f"{target_model}/{provider_name}: {_budget_err} — advancing",
                        flush=True,
                    )
                    try:
                        obs.log_request(
                            provider=provider_name, model=target_model, status=504,
                            ttft=_budget_elapsed, in_tokens=0, out_tokens=0,
                            cached_tokens=0, config=config, error_msg=_budget_err,
                            total_time=_budget_elapsed, request_id=request_id,
                            client=client_label, stream=False, upstream_url=_upstream_url,
                            conn_index=_active_conn_index, thinking=thinking_info, combo=_combo_label,
                        )
                    except Exception:
                        pass
                    _next_idx = (_retry_state['idx'] + 1) if _retry_state else 1
                    if _next_idx < len(active_chain) and _chain_budget_remaining() > 0:
                        return await _process_chat_completion(
                            body, client_wants_anthropic, client_wants_gemini,
                            _retry_state={'chain': active_chain, 'idx': _next_idx, 'cache_bp': _cache_breakpoints, 'original_model': original_model, 'deadline': _chain_deadline},
                            request=request,
                        )
                    if _chain_budget_remaining() <= 0:
                        print(f"[AFZ-DEADLINE] chain budget exhausted after {time.monotonic() - (_chain_deadline - CHAIN_TOTAL_BUDGET):.1f}s, idx={_next_idx}, refusing further fallback", flush=True)
                    print(f"[AFZ-FORENSIC] route=chat stream=False status=504 leaf={provider_name}/{target_model} active_streams={active_stream_count()} reason=nonstream_budget_exhausted", flush=True)
                    return JSONResponse(
                        {
                            "error": {
                                "code": 504,
                                "message": f"Non-stream generation budget exceeded for '{model}'. "
                                           f"Last: {target_model}/{provider_name} — {_budget_err}",
                                "status": "GENERATION_BUDGET_EXCEEDED",
                            }
                        },
                        status_code=504,
                    )
                except (httpx.HTTPError, httpx.TimeoutException, ConnectionError, UnicodeEncodeError) as _send_err:
                    _send_err_str = str(_send_err)
                    print(
                        f"[Combo Fallback] '{model}' non-stream transport error for "
                        f"{target_model}/{provider_name}: {_send_err_str} — advancing",
                        flush=True,
                    )
                    # Always emit an END entry so the dashboard PENDING row resolves.
                    try:
                        obs.log_request(
                            provider=provider_name, model=target_model, status=502,
                            ttft=time.time() - start_time, in_tokens=0, out_tokens=0,
                            cached_tokens=0, config=config, error_msg=_send_err_str,
                            total_time=time.time() - start_time, request_id=request_id,
                            client=client_label, stream=False, upstream_url=_upstream_url,
                            conn_index=_active_conn_index, thinking=thinking_info, combo=_combo_label,
                        )
                    except Exception:
                        pass
                    _next_idx = (_retry_state['idx'] + 1) if _retry_state else 1
                    if _next_idx < len(active_chain) and _chain_budget_remaining() > 0:
                        return await _process_chat_completion(
                            body, client_wants_anthropic, client_wants_gemini,
                            _retry_state={'chain': active_chain, 'idx': _next_idx, 'cache_bp': _cache_breakpoints, 'original_model': original_model, 'deadline': _chain_deadline},
                            request=request,
                        )
                    if _chain_budget_remaining() <= 0:
                        print(f"[AFZ-DEADLINE] chain budget exhausted after {time.monotonic() - (_chain_deadline - CHAIN_TOTAL_BUDGET):.1f}s, idx={_next_idx}, refusing further fallback", flush=True)
                    print(f"[AFZ-FORENSIC] route=chat stream=False status=502 leaf={provider_name}/{target_model} active_streams={active_stream_count()} reason=nonstream_transport_exhausted", flush=True)
                    return JSONResponse(
                        {
                            "error": {
                                "code": 502,
                                "message": f"All combo chain entries exhausted for '{model}'. "
                                           f"Last: transport error — {_send_err_str}",
                                "status": "COMBO_EXHAUSTED",
                            }
                        },
                        status_code=502,
                    )
                except Exception as _send_err:
                    _send_err_str = str(_send_err)
                    print(
                        f"[Combo Fallback] '{model}' non-stream unexpected error for "
                        f"{target_model}/{provider_name}: {_send_err_str} â€” advancing",
                        flush=True,
                    )
                    # Always emit an END entry so the dashboard PENDING row resolves.
                    try:
                        obs.log_request(
                            provider=provider_name, model=target_model, status=500,
                            ttft=time.time() - start_time, in_tokens=0, out_tokens=0,
                            cached_tokens=0, config=config, error_msg=_send_err_str,
                            total_time=time.time() - start_time, request_id=request_id,
                            client=client_label, stream=False, upstream_url=_upstream_url,
                            conn_index=_active_conn_index, thinking=thinking_info, combo=_combo_label,
                        )
                    except Exception:
                        pass
                    _next_idx = (_retry_state['idx'] + 1) if _retry_state else 1
                    if _next_idx < len(active_chain) and _chain_budget_remaining() > 0:
                        return await _process_chat_completion(
                            body, client_wants_anthropic, client_wants_gemini,
                            _retry_state={'chain': active_chain, 'idx': _next_idx, 'cache_bp': _cache_breakpoints, 'original_model': original_model, 'deadline': _chain_deadline},
                            request=request,
                        )
                    if _chain_budget_remaining() <= 0:
                        print(f"[AFZ-DEADLINE] chain budget exhausted after {time.monotonic() - (_chain_deadline - CHAIN_TOTAL_BUDGET):.1f}s, idx={_next_idx}, refusing further fallback", flush=True)
                    print(f"[AFZ-FORENSIC] route=chat stream=False status=502 leaf={provider_name}/{target_model} active_streams={active_stream_count()} reason=nonstream_unexpected_exhausted", flush=True)
                    return JSONResponse(
                        {
                            "error": {
                                "code": 502,
                                "message": f"All combo chain entries exhausted for '{model}'. "
                                           f"Last: unexpected error â€” {_send_err_str}",
                                "status": "COMBO_EXHAUSTED",
                            }
                        },
                        status_code=502,
                    )

            # â”€â”€ Thinking-Degradation Retry (reseller channel-roulette) â”€â”€
            # OAuth 401-Retry (non-streaming path)
            # If the upstream rejects with 401, the token may have expired between
            # our pre-check and the actual send. Force-refresh and retry ONCE.
            if (
                resp.status_code == 401
                and active_conn.get('token_type') == 'oauth'
                and not _oauth_401_retried
            ):
                _oauth_401_retried = True
                _old_resp = resp
                try:
                    _forced_token = await ensure_fresh_token(
                        provider_name, active_conn, provider_config, force=True
                    )
                    headers["Authorization"] = f"Bearer {_forced_token}"
                    _inject_provider_headers(headers, provider_name, active_conn)
                    print(
                        f"[OAuth-401Retry] {provider_name}/{target_model} "
                        f"force-refreshed token, retrying non-stream",
                        flush=True,
                    )
                    # Open replacement BEFORE closing original so a raise
                    # leaves the caller with a valid (open) response.
                    resp = await asyncio.wait_for(
                        client.send(_build_req(upstream_payload)),
                        timeout=max(1.0, min(NONSTREAM_TOTAL_BUDGET, _chain_budget_remaining())),
                    )
                    await _old_resp.aclose()
                except Exception as _oauth_retry_err:
                    print(
                        f"[OAuth-401Retry] {provider_name} refresh+retry failed: "
                        f"{_oauth_retry_err}",
                        flush=True,
                    )
                    # resp still points to _old_resp (open, status 401),
                    # safe for downstream consumers.

            # If the upstream channel rejected the thinking/reasoning params with
            # a 400, retry ONCE against the SAME provider with those params
            # stripped. Non-blocking: any failure here falls through to normal
            # error handling with the original response.
            if (_thinking_retry_armed and resp.status_code == 400):
                try:
                    _rej_body = resp.text[:1000]
                    if is_thinking_param_rejection(resp.status_code, _rej_body):
                        _stripped = strip_thinking(upstream_payload)
                        print(
                            f"[ThinkingFallback] '{target_model}/{provider_name}' rejected thinking params "
                            f"(400) â€” retrying once with stripped payload",
                            flush=True,
                        )
                        # Open replacement BEFORE closing original so a raise
                        # leaves the caller with a valid response.
                        _new_resp = await client.send(_build_req(_stripped))
                        # FIX B (2026-07-30): try/except to prevent pool poisoning.
                        try:
                            await resp.aclose()
                        except Exception:
                            pass
                        resp = _new_resp
                except Exception as _tf_err:
                    print(f"[ThinkingFallback] degrade-retry failed (non-blocking): {_tf_err}", flush=True)

            ttft = time.time() - start_time
            
            in_tokens = 0
            out_tokens = 0
            cached_tokens = 0
            cache_write_tokens = 0
            error_msg = None

            if resp.status_code == 200:
                try:
                    data_json = resp.json()
                    if "usage" in data_json and data_json["usage"]:
                        usage = data_json["usage"]
                        in_tokens, out_tokens, cached_tokens = _extract_usage_tokens(usage)
                        cache_write_tokens = _extract_cache_write_tokens(usage)
                except Exception:
                    pass
                # Zombie guard: a non-stream 200 with zero output tokens AND
                # empty content is a dead/empty leaf, not a heavy thinker.
                # Reclassify the ACTUAL response status to 504 via
                # _SyntheticResponse so both the inner combo chain and the outer
                # BSL-Chat/BSL-Lite fallback loop (which checks _RECOVERABLE)
                # can advance to the next entry. Without this, the zombie 200 is
                # returned to the client as a "success" with empty content, and
                # no fallback is triggered.
                #
                # Check both out_tokens AND content: some upstreams return
                # content but omit usage data (0 tokens). Only reclassify when
                # the response is truly empty (no tokens AND no content).
                try:
                    _response_json = resp.json() if hasattr(resp, 'json') else {}
                except Exception:
                    _response_json = {}
                if not _response_has_model_output(_response_json, out_tokens):
                    error_msg = (
                        f"zombie_empty_response (out_tokens=0, ttft={ttft:.1f}s)"
                    )
                    print(
                        f"[Combo Fallback] '{model}' non-stream zombie for "
                        f"{target_model}/{provider_name}: {error_msg} â€” advancing",
                        flush=True,
                    )
                    resp = _SyntheticResponse(504, {"error": error_msg})
            else:
                error_msg = resp.text[:200]

            _log_status = resp.status_code
            obs.log_request(
                provider=provider_name,
                model=target_model,
                status=_log_status,
                ttft=ttft,
                in_tokens=in_tokens,
                out_tokens=out_tokens,
                cached_tokens=cached_tokens,
                config=config,
                error_msg=error_msg,
                total_time=time.time() - start_time,
                request_id=request_id,
                client=client_label,
                stream=False,
                upstream_url=_upstream_url,
                conn_index=_active_conn_index,
                thinking=thinking_info,
                cache_write_tokens=cache_write_tokens,
                combo=_combo_label,
            )

            # â”€â”€ Combo Fallback: Non-Streaming Retry â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
            # On upstream errors, advance to the next combo chain entry.
            # 524 = Cloudflare origin timeout (HTML body, not a transport err).
            # 408 = Request Timeout from upstream gateway.
            # Zombie empty 200s are reclassified above and must also advance.
            _is_zombie = bool(error_msg and str(error_msg).startswith("zombie_empty_response"))
            if active_chain and (
                _is_zombie or resp.status_code != 200
            ):
                _next_idx = (_retry_state['idx'] + 1) if _retry_state else 1
                if _next_idx < len(active_chain) and _chain_budget_remaining() > 0:
                    _fb_code = 504 if _is_zombie else resp.status_code
                    print(f"[Combo Fallback] '{model}' non-stream {_fb_code} for {target_model}/{provider_name} â€” advancing to entry {_next_idx}")
                    return await _process_chat_completion(
                        body, client_wants_anthropic, client_wants_gemini,
                        _retry_state={'chain': active_chain, 'idx': _next_idx, 'cache_bp': _cache_breakpoints, 'original_model': original_model, 'deadline': _chain_deadline},
                        request=request,
                    )
                if _chain_budget_remaining() <= 0:
                    print(f"[AFZ-DEADLINE] chain budget exhausted after {time.monotonic() - (_chain_deadline - CHAIN_TOTAL_BUDGET):.1f}s, idx={_next_idx}, refusing further fallback", flush=True)

            # ── HTML / 400 Bad-Response Detection ─────────────────────

            # ── HTML / 400 Bad-Response Detection ─────────────────────
            # Upstream aggregators return HTML bodies (Cloudflare 5xx)
            # or bare 400 errors. The matrix dispatcher only advances
            # on {404, 429, 500, 502, 503, 504}, so raw 400/HTML falls
            # through to client without combo fallback. Reclassify both
            # as 502 so the fallback chain can advance.
            if resp.status_code >= 400:
                try:
                    _raw_start = resp.content[:200].decode("utf-8", errors="replace").strip()
                    _is_html = _raw_start.startswith("<")
                    _is_400 = resp.status_code == 400
                    if _is_html or _is_400:
                        _reason = "HTML body" if _is_html else "400 error"
                        print(
                            f"[BadResponse] '{target_model}/{provider_name}' "
                            f"returned HTTP {resp.status_code} ({_reason}) "
                            f"— reclassifying as 502",
                            flush=True,
                        )
                        resp = _SyntheticResponse(502, {"error": _raw_start[:200]})
                except Exception:
                    pass
            # GLM Tool-Call Normalizer (P2.1) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
            # GLM models sometimes emit tool calls as <tool_call> XML blocks in
            # content text instead of structured tool_calls arrays. Normalize
            # BEFORE egress conversion so Anthropic/Gemini/raw paths all benefit.
            _normalized_json = None
            _response_mutated = False
            if resp.status_code == 200:
                try:
                    _normalized_json, _glm_changed = normalize_glm_tool_calls(resp.json(), target_model)
                    if _glm_changed:
                        _response_mutated = True
                except Exception:
                    _normalized_json = None  # Fail-open: use raw resp

            # â”€â”€ P3: S3 Anti-Stop Loop + S6 Quality Gate â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
            # S3: When upstream returns finish_reason="length" (max_tokens
            # truncation), send ONE continuation request appending the partial
            # output + a "CONTINUE" instruction. Concatenate results.
            # S6: When the response looks truncated (short + no terminal
            # punctuation), retry ONCE with 2x max_tokens. If retry is longer,
            # replace original.
            # Both: ALWAYS ON, fail-open, max 1 retry, non-streaming only.
            _anti_stop_enabled = True
            _quality_gate_enabled = True

            if resp.status_code == 200 and (_anti_stop_enabled or _quality_gate_enabled):
                try:
                    # Use normalized json if available, else parse fresh
                    _working_json = _normalized_json if _normalized_json is not None else resp.json()

                    # â”€â”€ S3: Anti-Stop Loop (finish_reason="length") â”€â”€
                    if _anti_stop_enabled and is_length_truncated(_working_json):
                        _partial = _extract_assistant_text(_working_json)
                        _cont_payload = build_continuation_payload(upstream_payload, _partial)
                        if _cont_payload is not None:
                            print(
                                f"[AntiStop] '{target_model}/{provider_name}' hit length limit "
                                f"({len(_partial)} chars) â€” sending continuation",
                                flush=True,
                            )
                            if _apply_stream_buffer:
                                _cont_resp = await _buffered_send(_cont_payload, _label="S3-cont")
                            else:
                                _cont_resp = await client.send(_build_req(_cont_payload))
                            if _cont_resp.status_code == 200:
                                try:
                                    _cont_json = _cont_resp.json()
                                    # Normalize continuation tool calls too
                                    _cont_json, _ = normalize_glm_tool_calls(_cont_json, target_model)
                                    _working_json = merge_continuation_response(_working_json, _cont_json)
                                    _normalized_json = _working_json  # Update for egress
                                    _response_mutated = True
                                    print(
                                        f"[AntiStop] Continuation merged (+{_extract_usage(_cont_json)['completion_tokens']} tokens)",
                                        flush=True,
                                    )
                                except Exception as _merge_err:
                                    print(f"[AntiStop] Continuation merge failed (fail-open): {_merge_err}", flush=True)
                            else:
                                print(f"[AntiStop] Continuation HTTP {_cont_resp.status_code} (fail-open)", flush=True)

                    # â”€â”€ S6: Quality Gate (suspicious truncation) â”€â”€
                    elif _quality_gate_enabled:
                        _orig_mt = int(upstream_payload.get("max_tokens", 0) or 0)
                        _should_retry, _new_mt = should_retry_with_higher_budget(_working_json, _orig_mt)
                        if _should_retry:
                            # Clamp retry ceiling to the active token budget.
                            # When budget is OFF, _mt_budget_cap=65535 which
                            # matches the quality gate's own cap — harmless.
                            _new_mt = min(_new_mt, _mt_budget_cap)
                            _retry_payload = dict(upstream_payload)
                            _retry_payload["max_tokens"] = _new_mt
                            # _buffered_send will non-mutatively set stream=True
                            # on its internal copy; strip here for the non-buffered
                            # fallback path (else branch below) which needs stream=False.
                            _retry_payload["stream"] = False
                            print(
                                f"[QualityGate] '{target_model}/{provider_name}' suspicious truncation "
                                f"(finish={_extract_finish_reason(_working_json)}, tokens={_extract_usage(_working_json)['completion_tokens']}) "
                                f"â€” retrying with max_tokens={_new_mt}",
                                flush=True,
                            )
                            if _apply_stream_buffer:
                                _retry_resp = await _buffered_send(_retry_payload, _label="S6-retry")
                            else:
                                _retry_resp = await client.send(_build_req(_retry_payload))
                            if _retry_resp.status_code == 200:
                                try:
                                    _retry_json = _retry_resp.json()
                                    _retry_json, _ = normalize_glm_tool_calls(_retry_json, target_model)
                                    _orig_len = len(_extract_assistant_text(_working_json))
                                    _retry_len = len(_extract_assistant_text(_retry_json))
                                    if _retry_len > _orig_len:
                                        _normalized_json = _retry_json  # Replace with better response
                                        _response_mutated = True
                                        print(
                                            f"[QualityGate] Retry improved output ({_orig_len} â†’ {_retry_len} chars)",
                                            flush=True,
                                        )
                                    else:
                                        print(
                                            f"[QualityGate] Retry not longer ({_retry_len} <= {_orig_len}), keeping original",
                                            flush=True,
                                        )
                                except Exception as _qg_err:
                                    print(f"[QualityGate] Retry parse failed (fail-open): {_qg_err}", flush=True)
                            else:
                                print(f"[QualityGate] Retry HTTP {_retry_resp.status_code} (fail-open)", flush=True)
                except Exception as _p3_err:
                    print(f"[P3 Quality] Error (fail-open): {_p3_err}", flush=True)

            # Egress conversion: Kiro upstream returns AWS CodeWhisperer JSON
            # not OpenAI JSON. Convert so client sees standard OpenAI format.
            if provider_name == 'kiro' and resp.status_code == 200:
                try:
                    kiro_json = _normalized_json if _normalized_json is not None else resp.json()
                    openai_json = kiro_adapter.kiro_nonstream_to_openai(kiro_json)
                    return JSONResponse(openai_json, status_code=200)
                except Exception as e:
                    print(f"[Egress] Kiro->OpenAI response conversion failed (passthrough): {e}")

            # Egress conversion: client hit /v1/messages (Anthropic) but upstream
            # is OpenAI-format. Convert the OpenAI response so Claude Code can parse it.
            if client_wants_anthropic and not _is_anthropic_fmt and resp.status_code == 200:
                try:
                    openai_json = _normalized_json if _normalized_json is not None else resp.json()
                    anthropic_json = UniversalNormalizer.openai_response_to_anthropic(openai_json, model=target_model)
                    return JSONResponse(anthropic_json, status_code=200)
                except Exception as e:
                    print(f"[Egress] OpenAI->Anthropic response conversion failed (passthrough): {e}")

            # Gemini non-stream egress (Phase 5B-1 / Antigravity): render the OpenAI
            # completion as a wrapped {"response": {candidates, usageMetadata, ...}}
            # object (spec Â§4b). Errors pass through as a Gemini-shaped error object.
            if client_wants_gemini:
                if resp.status_code == 200:
                    try:
                        openai_json = _normalized_json if _normalized_json is not None else resp.json()
                        gemini_json = openai_response_to_gemini(openai_json, model=target_model)
                        return JSONResponse(gemini_json, status_code=200)
                    except Exception as e:
                        print(f"[Egress] OpenAI->Gemini response conversion failed: {e}")
                        return JSONResponse(
                            {"error": {"code": 500, "message": str(e), "status": "CONVERSION_ERROR"}},
                            status_code=500,
                        )
                else:
                    try:
                        err_text = resp.text[:1000]
                    except Exception:
                        err_text = ""
                    print(f"[AFZ-FORENSIC] route=chat stream=False status={resp.status_code} leaf={provider_name}/{target_model} active_streams={active_stream_count()} reason=nonstream_upstream_error_return", flush=True)
                    return JSONResponse(
                        {"error": {"code": resp.status_code, "message": err_text, "status": "UPSTREAM_ERROR"}},
                        status_code=resp.status_code,
                    )

            # Reverse egress: OpenAI client (/v1/chat/completions) but upstream is
            # Anthropic-compatible (GLM/Kimi/MiniMax). Convert Anthropic JSON â†’ OpenAI JSON.
            if (not client_wants_anthropic and not client_wants_gemini
                    and _is_anthropic_fmt and resp.status_code == 200):
                try:
                    anthropic_json = _normalized_json if _normalized_json is not None else resp.json()
                    openai_json = UniversalNormalizer.anthropic_response_to_openai(anthropic_json, model=target_model)
                    return JSONResponse(openai_json, status_code=200)
                except Exception as e:
                    print(f"[Egress] Anthropic->OpenAI response conversion failed (passthrough): {e}")
                    obs.log_request(
                        provider=provider_name, model=target_model, status=200,
                        ttft=0.0, in_tokens=0, out_tokens=0, cached_tokens=0,
                        config=config, error_msg=f"reverse_egress_json_fail: {e}",
                        total_time=time.time() - start_time,
                        request_id=request_id, client=client_label, stream=False, upstream_url=_upstream_url,
                        conn_index=_active_conn_index,
                        thinking=thinking_info,
                        combo=_combo_label,
                    )

            # Raw OpenAI passthrough: use mutated JSON if GLM/S3/S6 changed it,
            # otherwise preserve byte-perfect upstream content for untouched traffic.
            # In both cases, patch the model field to reflect the actual serving
            # model (target_model) after combo fallback, not the upstream echo.
            if resp.status_code == 200:
                try:
                    _passthrough_json = _normalized_json if _normalized_json is not None else resp.json()
                    if _passthrough_json.get("model") != target_model:
                        _passthrough_json["model"] = target_model
                    return JSONResponse(_passthrough_json, status_code=200)
                except Exception:
                    pass  # Fall through to raw bytes passthrough
            return Response(
                content=resp.content,
                status_code=resp.status_code,
                media_type=resp.headers.get("content-type", "application/json"),
                headers={k: v for k, v in resp.headers.items() if k.lower() not in ("content-encoding", "transfer-encoding")}
            )
        except Exception as e:
            ttft = time.time() - start_time
            obs.log_request(
                provider=provider_name,
                model=target_model,
                status=500,
                ttft=ttft,
                in_tokens=0,
                out_tokens=0,
                cached_tokens=0,
                config=config,
                error_msg=str(e),
                total_time=time.time() - start_time,
                request_id=request_id,
                client=client_label,
                stream=False,
                upstream_url=_upstream_url,
                conn_index=_active_conn_index,
                thinking=thinking_info,
                combo=_combo_label,
            )
            # â”€â”€ Combo Fallback: Non-Streaming Network Error Retry â”€â”€â”€â”€â”€
            # On network exceptions (timeout, connection reset, DNS), advance
            # to the next combo chain entry if available.
            if (active_chain
                    and not isinstance(e, (json.JSONDecodeError, ValueError, KeyError, TypeError))):
                _next_idx = (_retry_state['idx'] + 1) if _retry_state else 1
                if _next_idx < len(active_chain) and _chain_budget_remaining() > 0:
                    print(f"[Combo Fallback] '{model}' non-stream network error for {target_model}/{provider_name}: {e} â€” advancing to entry {_next_idx}")
                    return await _process_chat_completion(
                        body, client_wants_anthropic, client_wants_gemini,
                        _retry_state={'chain': active_chain, 'idx': _next_idx, 'cache_bp': _cache_breakpoints, 'original_model': original_model, 'deadline': _chain_deadline},
                        request=request,
                    )
                if _chain_budget_remaining() <= 0:
                    print(f"[AFZ-DEADLINE] chain budget exhausted after {time.monotonic() - (_chain_deadline - CHAIN_TOTAL_BUDGET):.1f}s, idx={_next_idx}, refusing further fallback", flush=True)
            if client_wants_gemini:
                # Return a Gemini-shaped error so Antigravity IDE terminates cleanly
                # instead of freezing on an unrecognized {"error": "..."} bare JSON.
                return JSONResponse(
                    {"error": {"code": 500, "message": str(e), "status": "PROXY_ERROR"}},
                    status_code=500,
                )
            return JSONResponse({"error": str(e)}, status_code=500)

# â”€â”€â”€ Output Intent Detection â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

import re as _re_intent

# Ordered list of (intent_label, regex_pattern). First match wins.
_OUTPUT_INTENT_PATTERNS = [
    ("json", _re_intent.compile(r'\b(json|json\s*schema|json\s*mode|json\s*output|return\s+json|respond\s+with\s+json)\b', _re_intent.IGNORECASE)),
    ("table", _re_intent.compile(r'\b(table|tabular|markdown\s+table|render\s+(?:a|as|in)\s*(?:table|tabular))\b', _re_intent.IGNORECASE)),
    ("code", _re_intent.compile(r'\b(code\s+block|code\s*only|output\s+code|return\s+code|respond\s+with\s+code|snippet|function\s+implementation)\b', _re_intent.IGNORECASE)),
    ("bullet-list", _re_intent.compile(r'\b(bullet\s+points|bullet\s+list|list\s+out|enumerate|numbered\s+list|itemize)\b', _re_intent.IGNORECASE)),
    ("concise", _re_intent.compile(r'\b(concise|brief|short\s+answer|summarize|tl;dr|one\s+sentence|one\s+word|yes\s*/\s*no|yes/no)\b', _re_intent.IGNORECASE)),
    ("detailed", _re_intent.compile(r'\b(detailed|thorough|comprehensive|in-depth|elaborate|step\s+by\s+step|walkthrough)\b', _re_intent.IGNORECASE)),
]


def _detect_output_intent(messages) -> str:
    """Scan the last user message for output format directives. Returns intent label or ''."""
    if not messages:
        return ""
    # Find the last user-role message
    for msg in reversed(messages):
        role = getattr(msg, "role", "")
        if role != "user":
            continue
        text = ""
        content = getattr(msg, "content", "")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            # Anthropic content-block list: extract all text blocks
            parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
            text = " ".join(parts)
        if not text:
            continue

        for label, pattern in _OUTPUT_INTENT_PATTERNS:
            if pattern.search(text):
                return label
        break  # Only scan the LAST user message
    return ""


def _inject_intent_format_block(payload: dict, intent: str) -> dict:
    """Inject a format-enforcement system prompt block for the detected intent."""
    FORMAT_BLOCKS = {
        "json": (
            "\n\nFORMAT DIRECTIVE: You MUST respond with valid JSON only. "
            "Do not include any explanatory text, markdown fences, or commentary outside the JSON object. "
            "Your entire response must be parseable by JSON.parse()."
        ),
        "table": (
            "\n\nFORMAT DIRECTIVE: You MUST present your response as a Markdown table. "
            "Use proper column headers and aligned rows. Do not wrap the table in code fences."
        ),
        "code": (
            "\n\nFORMAT DIRECTIVE: You MUST respond with code only. "
            "Provide the implementation as a single code block with the appropriate language tag. "
            "No preamble, no postscript, no explanation unless absolutely critical."
        ),
        "bullet-list": (
            "\n\nFORMAT DIRECTIVE: You MUST structure your response as a bulleted or numbered list. "
            "Each item should be a distinct bullet point (use '-' or '1.' prefix). "
            "Keep items parallel in structure."
        ),
        "concise": (
            "\n\nFORMAT DIRECTIVE: You MUST be extremely concise. "
            "Provide the shortest possible answer that still fully addresses the query. "
            "Omit all preamble, context-setting, and tangential detail."
        ),
        "detailed": (
            "\n\nFORMAT DIRECTIVE: You MUST provide a thorough, detailed response. "
            "Include step-by-step reasoning, examples, and edge-case analysis. "
            "Leave nothing implicit â€” explain every assumption and tradeoff."
        ),
    }

    block = FORMAT_BLOCKS.get(intent)
    if not block:
        return payload

    # Inject into system prompt (Anthropic format: system is top-level string or content array)
    existing_system = payload.get("system")
    if isinstance(existing_system, str):
        payload["system"] = existing_system + block
    elif isinstance(existing_system, list):
        # Content-block list â€” append a text block
        payload["system"] = existing_system + [{"type": "text", "text": block}]
    else:
        # No system prompt yet â€” create one
        payload["system"] = block.strip()

    return payload


# â”€â”€â”€ Async Polling Helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

async def _poll_qwen_task(
    client: httpx.AsyncClient,
    base_url: str,
    task_id: str,
    headers: dict,
    max_wait: int = 120
) -> dict:
    """Poll DashScope async task until SUCCEEDED/FAILED. Returns OpenAI-compatible image response."""
    poll_url = f"{base_url}/tasks/{task_id}"
    # Strip the async header â€” it shouldn't be sent on poll requests
    poll_headers = {k: v for k, v in headers.items() if k != "X-DashScope-Async"}
    attempts = max_wait // 3
    for _ in range(attempts):
        await asyncio.sleep(3)
        try:
            resp = await client.get(poll_url, headers=poll_headers)
            data = resp.json()
        except Exception as e:
            return {"error": f"Qwen task poll failed: {e}"}
        status = data.get("output", {}).get("task_status", "PENDING")
        if status == "SUCCEEDED":
            results = data.get("output", {}).get("results", [])
            return {
                "created": int(time.time()),
                "data": [{"url": r.get("url", "")} for r in results]
            }
        if status in ("FAILED", "CANCELED", "UNKNOWN"):
            msg = data.get("message", "") or data.get("output", {}).get("message", "Unknown error")
            return {"error": f"Qwen task {status}: {msg}"}
    return {"error": f"Qwen task timed out after {max_wait}s (task_id={task_id})"}

async def _poll_veo_lro(
    client: httpx.AsyncClient,
    base_url: str,
    operation_name: str,
    headers: dict,
    max_wait: int = 300
) -> dict:
    """Poll a Google Long-Running Operation until done=true. Returns the response payload."""
    # operation_name is relative (e.g. 'operations/xyz') or absolute path
    # The poll URL is base_url + '/' + operation_name
    poll_url = f"{base_url}/{operation_name}"
    attempts = max_wait // 5
    for _ in range(attempts):
        await asyncio.sleep(5)
        try:
            resp = await client.get(poll_url, headers=headers)
            data = resp.json()
        except Exception as e:
            return {"error": f"Veo LRO poll failed: {e}"}
        if data.get("error"):
            err = data["error"]
            return {"error": f"Veo operation error {err.get('code')}: {err.get('message', err)}"}
        if data.get("done"):
            return data.get("response", data)
    return {"error": f"Veo LRO timed out after {max_wait}s (operation={operation_name})"}

# â”€â”€â”€ End of Polling Helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@app.post("/api/providers/{provider_id}/discover-models")
async def discover_provider_models(provider_id: str):
    """Probe upstream /v1/models and return discovered models.
    Does NOT modify config.yaml - returns preview for admin review."""
    config = cs_get_config()
    provider_config = config.get("providers", {}).get(provider_id)
    if not provider_config:
        return JSONResponse({"error": f"Unknown provider: {provider_id}"}, status_code=404)
    clear_discovery_cache(provider_id)
    result = await discover_models(provider_id, provider_config, http_client)
    return JSONResponse(result)


@app.post("/api/providers/{provider_id}/apply-discovered-models")
async def apply_discovered_models_endpoint(provider_id: str):
    """Probe upstream and merge discovered models into config.yaml.
    New models are added as enabled. Existing models keep their state."""
    config = get_mutable_config()
    provider_config = config.get("providers", {}).get(provider_id)
    if not provider_config:
        return JSONResponse({"error": f"Unknown provider: {provider_id}"}, status_code=404)
    clear_discovery_cache(provider_id)
    result = await discover_models(provider_id, provider_config, http_client)
    if not result.get("discovered") and result.get("source") == "error":
        return JSONResponse({"error": result.get("error", "Discovery failed")}, status_code=502)
    discovered_ids = {m["id"] for m in result.get("models", [])}
    existing_models = provider_config.get("models", [])
    existing_ids = {m.get("id") for m in existing_models}
    added = []
    for m in result.get("models", []):
        if m["id"] not in existing_ids:
            existing_models.append({
                "id": m["id"],
                "name": m["id"],
                "thinking": "auto",
                "connection_indexes": [0],
                "enabled": True
            })
            added.append(m["id"])
    # P1 fix: previously this did `cs_get_config()[...] = existing_models` (a dead
    # no-op on a discarded copy) then a hand-rolled non-atomic yaml.dump of the whole
    # file (the 2026-08-03 config-wipe class). Now: mutate the mutable copy and commit
    # via the single sanctioned swap — atomic, no-wipe-guarded, and LIVE immediately.
    config["providers"][provider_id]["models"] = existing_models
    try:
        _replace_runtime_config(config)
    except Exception as _e:
        return JSONResponse({"error": f"Failed to persist config: {_e}"}, status_code=500)
    return JSONResponse({
        "provider": provider_id,
        "discovered": len(discovered_ids),
        "added": added,
        "existing": len(existing_ids),
        "total": len(existing_models)
    })


@app.get("/v1/models")
@app.get("/anthropic/v1/models")
@app.get("/gemini/v1/models")
async def list_models(discover: bool = False):
    config = cs_get_config()
    models = []
    for prov_id, prov_data in config.get("providers", {}).items():
        # Hidden providers are excluded from the public catalog.
        if prov_data.get("hidden"):
            continue
        # Builtin virtual providers (e.g. "blacksand") publish their naked
        # model IDs via the dedicated BSL tools block below; skip them here
        # so the generic namespaced path does not also emit duplicates like
        # "blacksand/blacksand-chat" alongside the naked "blacksand-chat".
        if prov_data.get("builtin"):
            continue
        for m in prov_data.get("models", []):
            if m.get("enabled", True):  # only expose enabled models
                raw_id = m.get("id")
                published_id = _public_model_id(prov_id, raw_id)
                entry = {"id": published_id, "object": "model", "owned_by": prov_id}
                # Advertise capabilities so IDEs stop stripping images from
                # vision-capable models. Uses the same heuristic as
                # route_registry._detect_capabilities, extended with prefixes
                # for modern multimodal models BSL routes.
                model_id_lower = (raw_id or "").lower()
                _vision, _tools, _reasoning = _detect_capabilities_lite(m, model_id_lower)
                entry["capabilities"] = {
                    "vision": _vision,
                    "imageInput": _vision,
                    "tools": _tools,
                    "reasoning": _reasoning,
                }
                models.append(entry)

    # Dynamic discovery: probe upstream /v1/models for eligible providers
    if discover:
        import asyncio as _asyncio
        _discovery_tasks = []
        for _prov_id, _prov_data in config.get("providers", {}).items():
            if _prov_data.get("hidden") or _prov_data.get("builtin"):
                continue
            _discovery_tasks.append(discover_models(_prov_id, _prov_data, http_client))
        _results = await _asyncio.gather(*_discovery_tasks, return_exceptions=True)
        _existing_ids = {m["id"] for m in models}
        for _result in _results:
            if isinstance(_result, Exception):
                continue
            if not _result.get("discovered") and _result.get("source") == "error":
                continue
            _prov_id = _result.get("provider", "")
            for _m in _result.get("models", []):
                _published_id = _public_model_id(_prov_id, _m["id"])
                if _published_id not in _existing_ids:
                    # Best-effort capabilities from model ID heuristic (no
                    # config entry for dynamically discovered models).
                    _mid_lower = (_m.get("id") or "").lower()
                    _vis, _tls, _rsn = _detect_capabilities_lite({}, _mid_lower)
                    models.append({
                        "id": _published_id,
                        "object": "model",
                        "owned_by": _prov_id,
                        "capabilities": {
                            "vision": _vis,
                            "imageInput": _vis,
                            "tools": _tls,
                            "reasoning": _rsn,
                        },
                    })
                    _existing_ids.add(_published_id)

    # Naked aliases stay out of the public catalog: they carry no upstream
    # model source and exist only as routing keys resolved directly on the
    # chat/completions path. Combos and namespaced provider models are the
    # real catalog entries that IDEs should discover and select.
    for combo in config.get("combos", []):
        alias = combo.get("alias")
        if alias:
            # Combos are polyfilled by the Vision Scout when
            # vision_bridge_enabled is true, so they are always vision-capable
            # from the IDE's perspective.
            models.append({
                "id": alias,
                "object": "model",
                "owned_by": "combo",
                "capabilities": {"vision": True, "imageInput": True, "tools": True, "reasoning": True},
            })
    # Expose BSL virtual routers so IDEs can discover and select them.
    # Catalog visibility is controlled solely by bsl_models.*.enabled.
    # Routing itself is ALWAYS ON (2026-08-06 directive).
    _bsl_models_cfg = config.get("bsl_models", {}) or {}
    for _bsl_key, _bsl_id in (
        ("bsl_chat", "blacksand-chat"),
        ("bsl_lite", "blacksand-lite"),
        ("bsl_agentic", "blacksand-agentic"),
        ("bsl_agentic_ultra", "blacksand-agentic-ultra"),
        ("bsl_agentic_max", "blacksand-agentic-max"),
    ):
        _bsl_sec = _bsl_models_cfg.get(_bsl_key) or {}
        if _bsl_sec.get("enabled", False):
            # BSL virtual routers are vision-capable via Vision Scout polyfill.
            models.append({
                "id": _bsl_id,
                "object": "model",
                "owned_by": "blacksand-labs",
                "capabilities": {"vision": True, "imageInput": True, "tools": True, "reasoning": True},
            })
    return JSONResponse({"object": "list", "data": models})

@app.post("/v1/images/generations")
async def images_generations(request: Request):
    config = cs_get_config()
    body = await request.json()
    model = body.get("model", "")
    provider_name = None
    target_model = model

    # Public namespaced catalog ID (e.g. "ckey.vn/...") â†’ internal provider + raw.
    if isinstance(model, str) and "/" in model:
        _ns_provider, _ns_model = _resolve_namespaced_model(model)
        if _ns_provider:
            provider_name = _ns_provider
            target_model = _ns_model

    if not provider_name and model in config.get("aliases", {}):
        target_model = config["aliases"][model].get("model", model)
        provider_name = config["aliases"][model].get("provider")

    if not provider_name:
        for prov_id, prov_data in config.get("providers", {}).items():
            for m in prov_data.get("models", []):
                if m.get("id") == model:
                    provider_name = prov_id
                    break
            if provider_name:
                break

    if not provider_name:
        return JSONResponse({"error": f"Image model '{model}' not found."}, status_code=404)

    provider_config = config.get("providers", {}).get(provider_name)
    _format = provider_config.get('format', 'openai-image')
    
    active_conn, _ = resolve_active_connection(config, provider_name, target_model)
    if not active_conn:
        active_conn = {}  # preserve fallback behavior for PROVIDER_DEFAULT_URLS
    resolved_base_url = (active_conn.get('base_url') or PROVIDER_DEFAULT_URLS.get(provider_name, '')).rstrip('/')
    client = _get_client_for_proxy(active_conn.get("proxy_url"))

    headers = {
        "Authorization": f"Bearer {active_conn.get('api_key', '')}",
        "Content-Type": "application/json"
    }

    # ── Anti-Detection: Spoof User-Agent for image providers ────────────────
    _stealth_ua = _STEALTH_USER_AGENTS.get(provider_name)
    if _stealth_ua:
        headers["User-Agent"] = _stealth_ua

    upstream_url = f"{resolved_base_url}/images/generations"
    upstream_payload = body.copy()
    upstream_payload["model"] = target_model
    
    if _format == "gemini-image":
        upstream_url = f"{resolved_base_url}/models/{target_model}:predict"
        prompt = body.get("prompt", "")
        n = body.get("n", 1)
        upstream_payload = {
            "instances": [{"prompt": prompt}],
            "parameters": {"sampleCount": n}
        }
        # Gemini native endpoints use x-goog-api-key, NOT Authorization: Bearer
        api_key_val = headers.pop("Authorization", "").replace("Bearer ", "")
        headers["x-goog-api-key"] = api_key_val
    elif _format == "qwen-image":
        upstream_url = f"{resolved_base_url}/services/aigc/text2image/image-synthesis"
        headers["X-DashScope-Async"] = "enable"
        upstream_payload = {
            "model": target_model,
            "input": {"prompt": body.get("prompt", "")},
            "parameters": {"n": body.get("n", 1)}
        }

    req = client.build_request("POST", upstream_url, headers=_strip_bsl_identity_headers(headers), json=upstream_payload)
    resp = await client.send(req)
    if resp.status_code != 200:
        return Response(content=resp.content, status_code=resp.status_code,
                        media_type=resp.headers.get("content-type", "application/json"))

    # Qwen async: initial response is a task ID â€” poll until the image is ready
    if _format == "qwen-image":
        init_data = resp.json()
        task_id = init_data.get("output", {}).get("task_id")
        if not task_id:
            return JSONResponse({"error": "Qwen did not return a task_id", "raw": init_data}, status_code=502)
        result = await _poll_qwen_task(client, resolved_base_url, task_id, headers)
        if "error" in result:
            return JSONResponse(result, status_code=502)
        return JSONResponse(result)

    return Response(content=resp.content, status_code=resp.status_code,
                    media_type=resp.headers.get("content-type", "application/json"))

@app.post("/v1/videos/generations")
async def videos_generations(request: Request):
    config = cs_get_config()
    body = await request.json()
    model = body.get("model", "")
    provider_name = None
    target_model = model

    if model in config.get("aliases", {}):
        target_model = config["aliases"][model].get("model", model)
        provider_name = config["aliases"][model].get("provider")

    if not provider_name:
        for prov_id, prov_data in config.get("providers", {}).items():
            for m in prov_data.get("models", []):
                if m.get("id") == model:
                    provider_name = prov_id
                    break
            if provider_name:
                break

    if not provider_name:
        return JSONResponse({"error": f"Video model '{model}' not found."}, status_code=404)

    provider_config = config.get("providers", {}).get(provider_name)
    _format = provider_config.get('format', 'openai-video')
    
    active_conn, _ = resolve_active_connection(config, provider_name, target_model)
    if not active_conn:
        active_conn = {}  # preserve fallback behavior for PROVIDER_DEFAULT_URLS
    resolved_base_url = (active_conn.get('base_url') or PROVIDER_DEFAULT_URLS.get(provider_name, '')).rstrip('/')
    client = _get_client_for_proxy(active_conn.get("proxy_url"))

    headers = {
        "Authorization": f"Bearer {active_conn.get('api_key', '')}",
        "Content-Type": "application/json"
    }

    upstream_url = f"{resolved_base_url}/videos/generations"
    upstream_payload = body.copy()
    upstream_payload["model"] = target_model

    # ── Anti-Detection: Spoof User-Agent for video providers ────────────────
    _stealth_ua = _STEALTH_USER_AGENTS.get(provider_name)
    if _stealth_ua:
        headers["User-Agent"] = _stealth_ua

    if _format == "gemini-video":
        upstream_url = f"{resolved_base_url}/models/{target_model}:predictLongRunning"
        # Gemini native LRO endpoint uses x-goog-api-key, NOT Authorization: Bearer
        api_key_val = headers.pop("Authorization", "").replace("Bearer ", "")
        headers["x-goog-api-key"] = api_key_val

    req = client.build_request("POST", upstream_url, headers=_strip_bsl_identity_headers(headers), json=upstream_payload)
    resp = await client.send(req)
    if resp.status_code != 200:
        return Response(content=resp.content, status_code=resp.status_code,
                        media_type=resp.headers.get("content-type", "application/json"))

    # Gemini Veo: LRO â€” initial response has operation name, must poll until done
    if _format == "gemini-video":
        init_data = resp.json()
        operation_name = init_data.get("name")
        if not operation_name:
            return JSONResponse({"error": "Veo did not return an operation name", "raw": init_data}, status_code=502)
        result = await _poll_veo_lro(client, resolved_base_url, operation_name, headers)
        if "error" in result:
            return JSONResponse(result, status_code=502)
        return JSONResponse(result)

    return Response(content=resp.content, status_code=resp.status_code,
                    media_type=resp.headers.get("content-type", "application/json"))

@app.post("/v1/chat/completions")
@app.post("/gemini/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    # â”€â”€ 9router "Chain" ingress â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # When 9router owns :443 and its chat brain is redirected to BSL via
    # MITM_ROUTER_BASE=http://localhost:6969, 9router POSTs the intercepted Cloud
    # Code / Gemini envelope HERE (its interceptor hardcodes the path to
    # /v1/chat/completions), after rewriting body.model to the aliases.json value
    # and stripping the caller's Google authorization header. Detect that envelope
    # and route it through the SAME tested adapter used by the /v1internal:* routes
    # so the reply is emitted as Gemini SSE (9router relays it verbatim to the IDE).
    #
    # Acceptance mirrors the /v1internal handler exactly (see antigravity_generate):
    #   accept  ==  is_antigravity_request(body) OR inner.contents present
    # A normal OpenAI call (top-level messages[], no request wrapper, no userAgent)
    # never matches, so this is fully fail-open for existing callers. Any detection
    # error also falls through to the standard OpenAI path rather than 500 (Rule 3).
    try:
        inner = gemini_unwrap_request(body)
        if is_antigravity_request(body) or inner.get("contents"):
            # body.model is authoritative here: 9router already rewrote it to the
            # aliases.json target, so it is handed straight to BSL's resolver.
            openai_body = gemini_request_to_openai(inner, body.get("model", ""))
            # 9router's antigravity interceptor always relays via SSE (pipeSSE) and
            # Antigravity streams by default (spec Â§8.12), so force a streaming egress.
            openai_body["stream"] = True
            if body.get("userAgent"):
                openai_body["x_antigravity_user_agent"] = body["userAgent"]
            response = await _process_chat_completion(
                openai_body, client_wants_gemini=True, request=request
            )
            if isinstance(response, JSONResponse) and response.status_code >= 400:
                try:
                    error_json = json.loads(response.body)
                    error_payload = {"error": error_json.get("error", error_json) if isinstance(error_json, dict) else error_json}
                except Exception:
                    error_payload = {"error": {"code": response.status_code, "message": response.body.decode("utf-8", errors="replace"), "status": "UNKNOWN"}}
                # FREEZE FIX (2026-08-04): bare error + [DONE] is ignored by the
                # Antigravity Gemini parser; emit a terminal candidate first.
                from app.compat.adapters.gemini import terminal_error_frame as _g_term
                _env_msg = ""
                try:
                    _e = error_payload.get("error") if isinstance(error_payload, dict) else error_payload
                    _env_msg = _e.get("message", "") if isinstance(_e, dict) else str(_e)
                except Exception:
                    _env_msg = "antigravity envelope error"
                # FREEZE FIX (2026-08-07): SOLE terminal contract; no bare error prefix.
                return Response(
                    content=(
                        gemini_sse_data(_g_term(response.status_code, _env_msg or "antigravity envelope error", ""))
                        + GEMINI_SSE_DONE
                    ),
                    status_code=200,
                    media_type="text/event-stream",
                )
            return response
    except Exception as exc:  # fail-open to the standard OpenAI path (project Rule 3)
        print(f"[Chain] antigravity envelope detection skipped: {type(exc).__name__}", flush=True)
    return await _process_chat_completion(body, request=request)

@app.post("/anthropic/v1/messages")
@app.post("/v1/messages")
async def anthropic_messages(request: Request):
    config = cs_get_config()
    body = await request.json()

    # â”€â”€ Claude Code Alias Resolution â”€â”€
    # Maps Claude Code's hardcoded model names (claude-sonnet-*, claude-opus-*, etc)
    # to BSL combo/provider models defined in config.yaml.
    # This endpoint is only hit by Claude Code (no other client sends Anthropic format).
    model_aliases = config.get("claude_code_aliases", {})
    raw_model = body.get("model", "")
    if raw_model in model_aliases:
        body["model"] = model_aliases[raw_model]
    else:
        # Wildcard match (e.g. "*sonnet*" -> combo_sonnet)
        for pattern, alias in model_aliases.items():
            if fnmatch.fnmatch(raw_model.lower(), pattern.lower()):
                body["model"] = alias
                break

    openai_body = UniversalNormalizer.normalize_to_openai_from_anthropic(body)
    openai_body["_bsl_original_model"] = raw_model
    return await _process_chat_completion(openai_body, client_wants_anthropic=True, request=request)


def _should_probe_stream_status(is_stream: bool, active_chain: list, client_wants_gemini: bool) -> bool:
    """Probe combo stream status before opening non-Gemini SSE responses.

    Gemini must bypass this direct await because gemini_egress_stream owns the
    bounded header task, comment keepalives, combo fallback, and terminal SSE
    error frames. Probing Gemini here would bypass its connection deadline and
    allow an upstream header wait to remain pending indefinitely.
    """
    return bool(is_stream and active_chain and len(active_chain) >= 1 and not client_wants_gemini)

# â”€â”€â”€ Antigravity (Google Cloud Code / Gemini private API) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Phase 5B-1: `v1internal:generateContent` / `:streamGenerateContent`. The path
# uses Google's private Cloud Code RPC verb (NOT public v1beta). Detection and
# conversion live in app/compat/adapters/gemini.py; this route is a thin wrapper
# that unwraps the Cloud Code envelope, normalizes the model, converts Geminiâ†’
# OpenAI, then dispatches to _process_chat_completion with client_wants_gemini
# so the egress converts OpenAIâ†’Gemini on the way back. See spec Â§1/Â§2.
@app.post("/v1internal:generateContent")
@app.post("/v1internal:streamGenerateContent")
@app.post("/v1beta/models/{model}:generateContent")
@app.post("/v1beta/models/{model}:streamGenerateContent")
@app.post("/v1/models/{model}:generateContent")
@app.post("/v1/models/{model}:streamGenerateContent")
@app.post("/v1alpha/models/{model}:generateContent")
@app.post("/v1alpha/models/{model}:streamGenerateContent")
async def antigravity_generate(request: Request, model: str = None):
    config = cs_get_config()
    raw_body = await request.body()
    path = request.url.path
    is_stream = path.endswith(":streamGenerateContent")
    # MITM sets this header before forwarding an explicitly mapped model. Once present,
    # this request is BSL-only: even malformed payloads must never fall through to
    # native Google, or the mapped account's quota can be consumed.
    mitm_alias = request.headers.get("x-bsl-antigravity-alias", "").strip()
    try:
        body = json.loads(raw_body)
    except (TypeError, ValueError):
        if mitm_alias:
            _mapped_message = "Mapped BSL request body is invalid JSON."
            if is_stream:
                from app.compat.adapters.gemini import terminal_error_frame as _g_term
                return Response(
                    content=(
                        gemini_sse_data(_g_term(400, _mapped_message, mitm_alias))
                        + GEMINI_SSE_DONE
                    ),
                    status_code=200,
                    media_type="text/event-stream",
                )
            return JSONResponse(
                {"error": {"code": 400, "message": _mapped_message, "status": "BSL_MAPPING_ERROR"}},
                status_code=400,
            )
        return await _forward_antigravity_native_or_error(
            request,
            is_stream,
            raw_body,
            model or "<unknown>",
            "the request body could not be mapped",
        )

    # 9router pipeline: MITM resolves alias and sets x-bsl-antigravity-alias header.
    # If header is present, use it directly — MITM already rewrote body.model too.
    # Fallback: look up from config mappings (direct-endpoint callers without MITM).
    # No enabled-gate: if MITM set the alias the request is already committed to BSL.
    #
    # Initialize raw_model/source_model up front so they are always bound, even on
    # the MITM branch where the body has already been rewritten to the alias and
    # cannot be used to recover the original client model. MITM also ships a trusted
    # internal x-bsl-antigravity-source-model header for exactly this attribution.
    raw_model = body.get("model") or model or ""
    source_model = raw_model if isinstance(raw_model, str) and raw_model else "<unknown>"
    integration = _antigravity_integration_settings()

    if mitm_alias:
        mapping_target = mitm_alias
        # MITM rewrites body.model to the alias; recover the original source from
        # the trusted internal header, bounded by the pre-rewrite payload/path model.
        source_model = request.headers.get("x-bsl-antigravity-source-model", "").strip() or source_model
        raw_model = source_model
    else:
        # IDE sends internal names (e.g. "gemini-pro-agent") but config maps
        # UI keys (e.g. "gemini-3.1-pro-high").  This synonym table is SHARED
        # with mitm.py via app.main_antigravity_synonyms — both layers must
        # resolve identically to prevent alias drift.
        from app.main_antigravity_synonyms import ANTIGRAVITY_REVERSE_SYNONYMS
        possible_keys = [source_model] + ANTIGRAVITY_REVERSE_SYNONYMS.get(source_model, [])
        mapping_target = None
        for pk in possible_keys:
            mapping_target = integration["mappings"].get(pk)
            if mapping_target:
                break
                
        if not integration["enabled"] and not mapping_target:
            return await _forward_antigravity_native_or_error(
                request,
                is_stream,
                raw_body,
                source_model,
                "direct integration is disabled",
            )

    # Unmapped slot â€” no MITM alias, no config mapping â€” native pass-through.
    if not mapping_target:
        raw_model = body.get("model") or model or ""
        source_model = raw_model if isinstance(raw_model, str) and raw_model else "<unknown>"
        return await _forward_antigravity_native_or_error(
            request,
            is_stream,
            raw_body,
            source_model,
            "unmapped",
        )

    try:
        if not _is_known_antigravity_mapping_target(config, mapping_target):
            raise ValueError("configured mapping target is unavailable")

        inner = gemini_unwrap_request(body)
        if not is_antigravity_request(body) and not inner.get("contents"):
            raise ValueError("request is not a supported Antigravity inference payload")

        # The dedicated mapping is authoritative. Do not normalize this target or
        # consult global aliases before dispatching it through BSL's resolver.
        openai_body = gemini_request_to_openai(inner, mapping_target)
        openai_body["_bsl_original_model"] = source_model
        openai_body["stream"] = is_stream
        if body.get("userAgent"):
            openai_body["x_antigravity_user_agent"] = body["userAgent"]

        # 9router pipeline: inject thinking for -thinking model targets.
        # pix4k and similar providers require this param; Antigravity IDE never sends it.
        # Use 'adaptive' â€” lets the model choose depth, accepted by pix4k + Anthropic.
        if mapping_target.lower().endswith("-thinking") and "thinking" not in openai_body:
            openai_body["thinking"] = {"type": "adaptive"}

        # Capture only mapped BSL requests. Native fallback paths preserve caller
        # authorization but intentionally avoid payload/header diagnostic logging.
        try:
            import os as _os
            _hdrs = {
                key: value
                for key, value in request.headers.items()
                if key.lower() not in ("authorization", "x-goog-api-key", "x-api-key", "cookie")
            }
            _rec = {
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "path": path,
                "query": str(request.url.query),
                "content_type": request.headers.get("content-type", ""),
                "headers": _hdrs,
                "raw_body": body,
                "converted_openai_body": openai_body,
            }
            _os.makedirs(".brain/logs", exist_ok=True)
            with open(".brain/logs/antigravity_inbound.jsonl", "a", encoding="utf-8") as capture_file:
                capture_file.write(json.dumps(_rec, ensure_ascii=False, default=str) + "\n")
            print(f"[AntigravityIntegration] mapped {raw_model} -> {mapping_target}", flush=True)
        except Exception as exc:
            print(f"[AntigravityIntegration] mapped capture failed ({type(exc).__name__})", flush=True)

        response = await _process_chat_completion(
            openai_body,
            client_wants_gemini=True,
            request=request,
        )
        # Mapped BSL route failed â€” return error directly as SSE.
        # Do NOT fall back to native Google: the user explicitly mapped this slot
        # to BSL, so the correct UX is a fast BSL error, not a slow/hanging Google
        # round-trip that may have no valid credential anyway.
        if isinstance(response, JSONResponse) and response.status_code >= 400:
            print(
                f"[AntigravityIntegration] BSL mapping failed for {raw_model} "
                f"(status={response.status_code}); returning SSE error (no native fallback for mapped slots)",
                flush=True,
            )
            try:
                _err_body = json.loads(response.body)
                _raw_err = _err_body.get("error", _err_body) if isinstance(_err_body, dict) else _err_body
                if isinstance(_raw_err, dict):
                    _err_payload = {"error": _raw_err}
                else:
                    _err_payload = {
                        "error": {
                            "code": response.status_code,
                            "message": str(_raw_err),
                            "status": "BSL_ERROR",
                        }
                    }
            except Exception:
                _err_payload = {"error": {"code": response.status_code, "message": response.body.decode("utf-8", errors="replace"), "status": "BSL_ERROR"}}
            if is_stream:
                # FREEZE FIX (2026-08-04): bare error + [DONE] is ignored by the
                # Antigravity Gemini parser; emit a terminal candidate first so the
                # IDE does not hang on the user's mapped-slot failure path.
                from app.compat.adapters.gemini import terminal_error_frame as _g_term
                _mapped_msg = ""
                try:
                    _me = _err_payload.get("error") if isinstance(_err_payload, dict) else _err_payload
                    _mapped_msg = _me.get("message", "") if isinstance(_me, dict) else str(_me)
                except Exception:
                    _mapped_msg = f"BSL mapping failed for {raw_model}"
                # FREEZE FIX (2026-08-07): SOLE terminal contract; no bare error prefix.
                return Response(
                    content=(
                        gemini_sse_data(_g_term(response.status_code, _mapped_msg or f"BSL mapping failed for {raw_model}", raw_model))
                        + GEMINI_SSE_DONE
                    ),
                    status_code=200,
                    media_type="text/event-stream",
                )
            return JSONResponse(_err_payload, status_code=response.status_code)
        return response
    except Exception as exc:
        print(
            f"[AntigravityIntegration] mapped routing exception for {raw_model}: {type(exc).__name__}: {exc}",
            flush=True,
        )
        _exc_payload = {"error": {"code": 500, "message": str(exc), "status": "BSL_ROUTING_ERROR"}}
        if is_stream:
            # FREEZE FIX (2026-08-07): SOLE terminal contract; no bare error prefix.
            from app.compat.adapters.gemini import terminal_error_frame as _g_term
            return Response(
                content=(
                    gemini_sse_data(_g_term(500, str(exc), raw_model))
                    + GEMINI_SSE_DONE
                ),
                status_code=200,
                media_type="text/event-stream",
            )
        return JSONResponse(_exc_payload, status_code=500)

# â”€â”€â”€ Antigravity CCPA control-plane gateway â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# The cloud_code_endpoint must also serve authenticated bootstrap, entitlement,
# and model-discovery RPCs. Inference stays on the exact routes above so it can
# continue through BSL's model mapping; this narrow route forwards only other
# valid Cloud Code RPC operation names to an approved Google origin.
_ANTIGRAVITY_CCPA_BASE_URL = "https://cloudcode-pa.googleapis.com"
_ANTIGRAVITY_CCPA_OPERATION_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")


def _validated_antigravity_ccpa_base_url(base_url: Optional[str] = None) -> str:
    """Return an approved Google CCPA origin and reject local or arbitrary targets."""
    parsed = httpx.URL(_ANTIGRAVITY_CCPA_BASE_URL if base_url is None else base_url)
    host = (parsed.host or "").lower()
    if (
        parsed.scheme != "https"
        or host not in _ANTIGRAVITY_NATIVE_HOSTS
        or parsed.port not in (None, 443)
        or parsed.path not in ("", "/")
    ):
        raise ValueError("CCPA control-plane upstream must target an approved Google Cloud Code HTTPS origin.")
    return f"https://{host}"


def _build_antigravity_ccpa_url(
    request: Request,
    operation: str,
    approved_base_url: Optional[str] = None,
) -> str:
    """Construct the pinned CCPA RPC URL while preserving its query string."""
    base = approved_base_url or _validated_antigravity_ccpa_base_url()
    query = str(request.url.query)
    return f"{base}/v1internal:{operation}{'?' + query if query else ''}"


def _antigravity_ccpa_error(status_code: int, status: str, message: str) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": status_code, "status": status, "message": message}},
        status_code=status_code,
    )


def _log_antigravity_ccpa_stage_failure(operation: str, stage: str, exc: Exception) -> None:
    """Log only safe CCPA failure metadata, never request data or credentials."""
    print(
        f"[AntigravityCCPA] operation={operation} stage={stage} error={type(exc).__name__}",
        flush=True,
    )


async def _forward_antigravity_ccpa_control(request: Request, operation: str):
    """Stream a non-inference Cloud Code RPC to the pinned Google CCPA origin."""
    if not _ANTIGRAVITY_CCPA_OPERATION_RE.fullmatch(operation):
        return _antigravity_ccpa_error(
            404,
            "UNSUPPORTED_CONTROL_OPERATION",
            "Only valid /v1internal:* Cloud Code control operations are supported.",
        )
    if not _antigravity_native_credentials_present(request):
        return _antigravity_ccpa_error(
            401,
            "UNAUTHENTICATED",
            "CCPA control operations require forwarded Authorization or x-goog-api-key credentials.",
        )

    # A rejected origin is the only condition that may be reported as
    # UNSAFE_UPSTREAM. Keep validation outside the transport path: httpx and
    # Starlette can also raise ValueError for unrelated request/response work.
    try:
        approved_base_url = _validated_antigravity_ccpa_base_url()
    except ValueError as exc:
        _log_antigravity_ccpa_stage_failure(operation, "upstream-url-validation", exc)
        return _antigravity_ccpa_error(
            502,
            "UNSAFE_UPSTREAM",
            "CCPA control-plane upstream is not an approved Google Cloud Code HTTPS origin.",
        )

    stage = "shared-client"
    try:
        # Use egress client so BSL Router connects to Google directly (via 8.8.8.8)
        # bypassing the local MITM hosts file which would cause an infinite loop.
        client = _get_antigravity_egress_client()
        stage = "request-body"
        raw_body = await request.body()
        stage = "request-build"
        upstream_request = client.build_request(
            request.method,
            _build_antigravity_ccpa_url(request, operation, approved_base_url),
            headers=_antigravity_native_request_headers(request),
            content=raw_body,
        )
        stage = "upstream-send"
        upstream_response = await client.send(upstream_request, stream=True)
        try:
            stage = "response-headers"
            upstream_headers = _antigravity_native_response_headers(upstream_response)
            stage = "response-stream"
            # ANTI-FREEZE: register this CCPA control-plane passthrough so
            # POST /api/antifreeze/force-stop can cancel it. Control-plane
            # /v1internal:* RPCs are non-SSE (JSON), so we deliberately force
            # the registry-only path: stream_deadline's SSE terminal frames
            # would corrupt the forwarded JSON body. Registry-only preserves
            # force-stop visibility without touching the bytes.
            _afz_sid, _afz_body = _afz_passthrough_guard(
                upstream_response.aiter_raw(),
                upstream_response.headers.get("content-type", ""),
                allow_deadline_frames=False,
            )
            return StreamingResponse(
                _afz_body,
                status_code=upstream_response.status_code,
                headers=upstream_headers,
                background=BackgroundTask(upstream_response.aclose),
            )
        except Exception:
            await upstream_response.aclose()
            raise
    except Exception as exc:
        # Never include request data or headers here: Google credentials must not
        # reach process logs even when control-plane transport is unavailable.
        _log_antigravity_ccpa_stage_failure(operation, stage, exc)
        return _antigravity_ccpa_error(
            502,
            "UPSTREAM_UNAVAILABLE",
            "Google Cloud Code control-plane upstream is unavailable.",
        )


@app.api_route("/v1internal:{operation}", methods=["GET", "POST"])
@app.api_route("/v1internal/{operation}", methods=["GET", "POST"])
async def antigravity_ccpa_control_proxy(request: Request, operation: str):
    """Forward CCPA control-plane POSTs after exact inference routes have matched."""
    return await _forward_antigravity_ccpa_control(request, operation)


# â”€â”€â”€ Antigravity auth/bootstrap handshake â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Exact inference routes remain local. This CCPA control-plane gateway forwards
# only other /v1internal:* POST operations to an allowlisted Google origin, so
# cloud_code_endpoint preserves authentication, quota, and model-discovery RPCs.

@app.post("/v1/responses")
async def responses_endpoint(request: Request):
    """Phase 6: OpenAI Responses API endpoint (Codex CLI, modern OpenAI clients)."""
    body = await request.json()
    chat_body = ResponsesConverter.responses_to_chat(body)
    return await _process_chat_completion(chat_body, request=request)

# ── Security Scanner Endpoints ─────────────────────────────────────────────────

@app.get("/api/scan-keys")
async def scan_keys_endpoint():
    """Run security scan on all provider configurations. Returns findings."""
    config = cs_get_config()
    from app.security.key_scanner import scan_provider_config
    result = scan_provider_config(config)
    return JSONResponse(result.to_dict())


@app.post("/api/scan-keys")
async def scan_keys_snapshot_endpoint(request: Request):
    """Run security scan on a config snapshot (before saving). Returns findings."""
    try:
        snapshot = await request.json()
        from app.security.key_scanner import scan_provider_config
        result = scan_provider_config(snapshot)
        return JSONResponse(result.to_dict())
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


# ── Auto-Update Endpoints ──────────────────────────────────────────────────────

_BSL_VERSION_FILE = _os_module.path.join(_os_module.path.dirname(_os_module.path.abspath(__file__)), "..", "VERSION")
_BSL_GITHUB_REPO = "bsl-router"


def _get_bsl_version() -> str:
    """Read current version from VERSION file."""
    try:
        with open(_BSL_VERSION_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return "0.0.0"


@app.get("/api/check-update")
async def check_update_endpoint():
    """Check GitHub for newer BSL Router releases."""
    try:
        github_repo = cs_get_config().get("update", {}).get("github_repo", _BSL_GITHUB_REPO)
        if not github_repo:
            return JSONResponse({"update_available": False, "error": "No GitHub repo configured"})
        # Query GitHub releases API
        url = f"https://api.github.com/repos/{github_repo}/releases/latest"
        resp = await http_client.get(url, timeout=15.0, headers={"Accept": "application/vnd.github.v3+json"})
        if resp.status_code != 200:
            return JSONResponse({"update_available": False, "error": f"GitHub API returned {resp.status_code}"})
        release = resp.json()
        latest_tag = release.get("tag_name", "").lstrip("v")
        current = _get_bsl_version()
        update_available = _compare_versions(latest_tag, current) > 0
        return JSONResponse({
            "update_available": update_available,
            "current_version": current,
            "latest_version": latest_tag,
            "release_url": release.get("html_url", ""),
            "release_notes": release.get("body", "")[:500],
            "published_at": release.get("published_at", ""),
        })
    except Exception as e:
        return JSONResponse({"update_available": False, "error": str(e)})

@app.post("/api/trigger-update")
async def trigger_update_endpoint():
    """Trigger the auto-update process. Downloads latest release and restarts."""
    try:
        import subprocess as _sp
        import tempfile as _tempfile

        github_repo = cs_get_config().get("update", {}).get("github_repo", _BSL_GITHUB_REPO)
        if not github_repo:
            return JSONResponse({"ok": False, "error": "No GitHub repo configured"})

        # Get latest release info
        url = f"https://api.github.com/repos/{github_repo}/releases/latest"
        resp = await http_client.get(url, timeout=15.0, headers={"Accept": "application/vnd.github.v3+json"})
        if resp.status_code != 200:
            return JSONResponse({"ok": False, "error": f"GitHub API returned {resp.status_code}"})
        release = resp.json()
        latest_tag = release.get("tag_name", "").lstrip("v")

        # Find the update ZIP asset
        assets = release.get("assets", [])
        update_zip_url = None
        for asset in assets:
            if asset.get("name", "").endswith(".zip") and "update" in asset.get("name", "").lower():
                update_zip_url = asset.get("browser_download_url")
                break
        if not update_zip_url and assets:
            # Fallback: use source code zip
            update_zip_url = release.get("zipball_url")

        if not update_zip_url:
            return JSONResponse({"ok": False, "error": "No update package found in latest release"})

        # Write the updater script to temp and execute detached
        updater_path = _os_module.path.join(_os_module.path.dirname(_os_module.path.abspath(__file__)), "..", "scripts", "update_bsl_router.py")
        if not _os_module.path.exists(updater_path):
            return JSONResponse({"ok": False, "error": "Updater script not found"})

        # Spawn the updater as a detached process
        creation_flags = 0x08000000  # CREATE_NO_WINDOW
        if hasattr(_sp, 'DETACHED_PROCESS'):
            creation_flags |= 0x00000008  # DETACHED_PROCESS

        _sp.Popen(
            [_sys.executable, updater_path, "--url", update_zip_url, "--version", latest_tag],
            creationflags=creation_flags,
            stdout=_sp.DEVNULL,
            stderr=_sp.DEVNULL,
            stdin=_sp.DEVNULL,
        )

        return JSONResponse({
            "ok": True,
            "message": "Update process started. BSL Router will restart automatically.",
            "target_version": latest_tag,
        })
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.get("/api/version")
async def get_version_endpoint():
    """Return current BSL Router version."""
    return JSONResponse({"version": _get_bsl_version()})


def _compare_versions(v1: str, v2: str) -> int:
    """Compare two semantic version strings. Returns >0 if v1>v2, 0 if equal, <0 if v1<v2."""
    try:
        parts1 = [int(x) for x in v1.split(".")]
        parts2 = [int(x) for x in v2.split(".")]
        for a, b in zip(parts1, parts2):
            if a != b:
                return a - b
        return len(parts1) - len(parts2)
    except Exception:
        return 0


if __name__ == "__main__":
    import uvicorn
    load_config()
    config = cs_get_config()
    port = config.get("server", {}).get("port", 6969)
    host = config.get("server", {}).get("host", "0.0.0.0")
    # Reload is opt-in via config.server.reload (default OFF). Auto-reload on a
    # production router is a footgun: any file save â€” an agent editing app/
    # sources, or churn under .brain/ (task specs, logs, jsonl) â€” restarts the
    # worker mid-request and drops in-flight streams (including the inference
    # stream of an agent that routes through this very router). When reload IS
    # enabled for dev, exclude high-churn non-source paths so only real app/
    # source edits trigger a reload.
    reload_enabled = bool(config.get("server", {}).get("reload", False))
    _reload_kwargs = {}
    if reload_enabled:
        _reload_kwargs["reload_excludes"] = [".brain/*", "scratch/*", "*.log", "*.jsonl"]
    # Note: uvloop is automatically used by uvicorn if installed (on non-Windows)
    uvicorn.run("app.main:app", host=host, port=port, reload=reload_enabled, **_reload_kwargs)


