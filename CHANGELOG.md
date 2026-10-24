# Changelog

All notable changes to BSL Router are documented in this file.

Mọi thay đổi đáng chú ý của BSL Router được ghi lại trong file này.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

**🌐 [English](#110---2026-10-23)** · **🇻🇳 [Tiếng Việt](#-tiếng-việt)**

---

## [1.1.0] - 2026-10-23

> **Major Release (`v1.1.0`) — 90 files changed, +14,975 insertions:** Unified App Integration (`Antigravity IDE/Desktop` + `Claude Code Desktop/CLI`), Faithful Execution Layer (FEL) Full Overhaul (`~223x` faster sensitivity engine + 100% streaming & native `gemini-4-argon` coverage), OAuth Multi-Account Per-Session Anti-Ban + Dual `5h`/`7d` Quota Bars + Subscription Tier Badges + Codex JSON Import, 1-Button Unified `Smart Prompt Caching` + 2026 Model-Family Wildcards, Multi-Token Per-Leaf Combo Priority + Recursive Nested Combos, True Token/Cache Accounting + False-Positive `499` Fix, Native Google Cloud Code Pool Isolation, and Model Authenticity Testing V2/V3 (`spatial_curve`, `token_jsd`, `hard_series`) + 17-Defect Audit Hardening.

### 🇬🇧 English — Added & Fixed

- **3-Layer Malicious API Key & Rogue Provider Shield — Static Key/URL/Header Scanner, Network SSRF Guard & Post-Upstream Response/Tool-Call Injection Guard with Active Redaction & UI Telemetry (`app/security/key_scanner.py`, `app/middleware/response_guard.py`, `app/main.py`, `app/static/app.js`, `app/tests/test_key_scanner.py`, `app/tests/test_response_guard.py`)**:
  - **Layer 1 — Static Key, Endpoint & Custom Header Scanner (`app/security/key_scanner.py`, `/api/verify-key`, `/api/scan-keys`):** Added hidden Unicode BiDi/zero-width trojan detection (`HIDDEN_UNICODE_PATTERN`), env-var expansion (`${...}`) / path traversal (`../`) / direct command injection detection (`KEY_PAYLOAD_PATTERNS`), Base64-encoded command/exfiltration payload inspection inside crafted API keys (`_detect_base64_payload_in_key`), 28+ suspicious tunnel/webhook domains (`SUSPICIOUS_DOMAINS`) + webhook exfiltration paths (`WEBHOOK_EXFIL_PATHS`), and custom header scanning (`_scan_header_custom` blocking `Host`/`Authorization`/`X-Forwarded-Host` overrides, CRLF injection, and payload markers).
  - **Layer 3 — Post-Upstream Response & Tool-Call Injection Guard (`app/middleware/response_guard.py`, `app/main.py`):** Added `tool_secret_exfil_network` (credential files + outbound network command), `tool_lolbas_exec` (`mshta`, `regsvr32`, `rundll32`, `reg save HKLM\SAM`, `powershell -EncodedCommand`), and `tool_agent_config_hijack` (`mcp_config.json`, `claude_desktop_config.json`, `.claude/settings.json`, `authorized_keys`, `.bashrc`, `.zshrc` + remote payload); recorded `marker.category` for `_TOOL_ARG_MARKERS`; implemented active `redact` mode in `apply_guard(verdict, response, mode="log_only")` across OpenAI Chat, Anthropic Messages, Gemini `v1beta`/`v1internal`, and OpenAI Responses formats (scrubbing prose and stripping malicious `tool_calls`/`tool_use`/`functionCall`); wired `apply_guard` into `app/main.py` non-streaming path; and added `GET /api/security/response-guard` and `POST /api/security/response-guard/clear`.
  - **Admin UI Security Shield Card & `/api/verify-key` Findings Display (`app/static/app.js`):** Surfaced `data.blocked` and `data.scan_findings` in `fetchProviderModels`, `verifyProviderKey`, and `verifyConnectionKey`, and added the **🛡️ Malicious API Key & Response Injection Shield** card in the **Tools** tab (`updateResponseGuardConfig`, `runProviderSecurityScan`, `loadResponseGuardTelemetry`, `clearResponseGuardTelemetry`).
- **Faithful Execution Layer (FEL) Full Overhaul — 9 Bug Fixes, 4 Upgrades & Native `gemini-4-argon` Cloud Code Envelope Support (`app/middleware/faithful_execution.py`, `app/middleware/fel_wiring.py`, `app/middleware/response_guard.py`, `app/observability.py`, `app/main.py`, `app/static/index.html`, `app/static/app.js`, `app/tests/test_fel_overhaul.py`)**:
  - **100% Streaming Observability, Native `gemini-4-argon` Passthrough & Post-Log SQLite `refusal_class` Backfill:** Wrapped all 6 streaming return paths (`anthropic_egress`, `gemini_egress`, `anthropic_to_openai_egress_stream_guarded`, `kiro`, `codex`, and `raw_upstream_guarded`) plus native Antigravity IDE passthrough (`_forward_antigravity_native`) with `_wrap_egress_stream()` (`_fel_observe_stream` + `_guard_observe_stream`), wired `_apply_fel_to_antigravity_native_body()` and Google Cloud Code envelope (`{"model": "...", "request": {"contents": [...], "systemInstruction": {"parts": [...]}}}`) support in `fel_wiring.py` (`extract_last_user_text`, `set_last_user_text`, `reframe_context_wide`, `apply_fel_directives`) so native `gemini-4-argon` IDE calls receive full FEL coverage alongside routed calls, extended `FelStreamObserver` and `ResponseGuardObserver` to parse Gemini `candidates[].content.parts[]` (including Cloud Code `response.candidates[]`) and OpenAI Responses API `response.output_text.delta` SSE frames, and updated `obs.note_fel_context()` (`_RECENT_REQUEST_ROW_IDS`) to backfill `refusal_class` and `fel` onto committed SQLite `usage_events` rows when stream finalization completes after `obs.log_request()`.
  - **Starter-Word Indexed Regex Cache (`~223x` Speedup, Zero `re._MAXCACHE` Eviction):** Replaced the per-request 503-phrase `re.compile` + `_fold_diacritics` loop in `detect_sensitivity()` with `_COMPILED_SENSITIVITY_CACHE` (starter-word token indexing + compiled alternation per starter word) and a zero-allocation `text.isascii()` fast path in `_fold_diacritics()`, reducing 20 × 14 KB sensitivity scans from `3,804.2 ms` to `17.06 ms` (`<0.9 ms` per 14 KB block).
  - **2026 Routing Prefix & Combo Family Resolution:** Normalized vendor prefixes, bracket tags, and suffixes in `detect_family()` (`z-ai/glm-5.3-free`, `zai-web1/glm-5.3-max`, `arena-web/kimi-k2.7-code`, `ZHIPU/GLM-5.3`, `[opencode]deepseek-v4-pro`, `hy4-preview`, `tencent/hy3-free`, `mimo-v2.6-pro`, `muse-spark-*`, `step-*`, `ernie-*`, `seed-*`, `gemini-4-argon`, `argon-*`) and added `_resolve_preflight_fel_family()` in `app/main.py` to resolve aliases and recursive combo chains during pre-flight.
  - **Cache-Aligned Directive Merging, Context-Scope History Reframing & Multilingual Refusals:** Updated `merge_directives()` to move Anthropic `cache_control` to the appended directive tail block without mutating inputs; decoupled `reframe_context_wide` (`context_scope == "full"`) from `_fel_write_ok` and added Anthropic `tool_result` + Gemini `contents` block rewriting; prevented double context/engagement prepending between `clarity_preprocess` and `reframe_text`; added Chinese (ZH) and expanded Vietnamese (VI) refusal and hard-wall markers; added early-stream refusal interception (`_fel_stream_with_recovery`) and streaming `data_inspection_failed` retry; and fixed the Tools → FEL UI engagement schema (`tools.fel.engagement.default` + flat fallback) while adding Semantic Reframing controls, a Live FEL Telemetry pill bar, and a `POST /admin/fel/preview` Dry-Run Sandbox (`308/308` tests passing).

### Fixed

- **Antigravity OAuth Out-of-the-Box Credentials on Fresh `git clone`, Antigravity Custom Model SSE `[DONE]` Protobuf Unmarshal Crash Fix & Anthropic-to-Anthropic Streaming Passthrough Terminal Frame Synthesis (`app/oauth.py`, `app/config_state.py`, `config.example.yaml`, `app/compat/adapters/gemini.py`, `app/antifreeze.py`, `app/mitm.py`, `app/middleware/anthropic_tools.py`, `app/main.py`, `app/tests/test_anthropic_tool_repair.py`, `app/tests/test_gemini_error_termination.py`, `app/tests/test_oauth_9router_parity.py`, `app/tests/test_oauth_quota_endpoint.py`)**:
  - **Zero-Setup Antigravity OAuth on Fresh Clones:** Restored the standard Google Cloud Code public desktop OAuth client ID and secret as lazy base64-decoded fallbacks (`_AG_DEFAULT_CLIENT_ID_B64` / `_AG_DEFAULT_CLIENT_SECRET_B64`) across `oauth_start()`, `_exchange_authorization_code()`, and `refresh_connection_token()` (with priority: `config.yaml` override -> `BSL_ANTIGRAVITY_OAUTH_CLIENT_ID` / `BSL_ANTIGRAVITY_OAUTH_CLIENT_SECRET` env vars -> built-in defaults) and merged `oauth` defaults in `get_config()` so fresh `git clone` installations can connect Antigravity OAuth immediately without manual credential setup.
  - **Antigravity Custom/Mapped Model Post-Answer Crash Fix (`SSE_DONE = b": [DONE]\n\n"` & FEL Keepalive Passthrough):** Fixed a critical bug where custom/mapped models in Antigravity IDE (`Gemini-Argon`, `Chat2API`, etc. via `/v1internal:streamGenerateContent?alt=sse`) streamed their full answer and then immediately failed with `Error Unknown: Agent execution terminated due to error` (`proto: syntax error (line 1:1): unexpected token [` in `language_server.log`). Antigravity's Go `language_server.exe` (`codeassistclient.responseStream.scanner`) skips SSE comment lines (`!strings.HasPrefix(line, "data: ")`) and invokes `protojson.Unmarshal` on every non-empty `data: ` line; emitting `b"data: [DONE]\n\n"` fed `[DONE]` into `protojson.Unmarshal`. Changing `SSE_DONE` in `app/compat/adapters/gemini.py` (and `stream_deadline` in `app/antifreeze.py`) to the SSE comment sentinel `b": [DONE]\n\n"` lets `language_server.exe` skip it cleanly like `: heartbeat` / `: keepalive`, while `_fel_stream_with_recovery()` in `app/main.py` now forwards `: heartbeat` / `: keepalive` comments immediately during pre-text buffering so long-thinking `gemini-4-argon` streams never time out.
  - **Anthropic-to-Anthropic Streaming Passthrough Missing Terminal Frames Fix (`AnthropicStreamLifecycleTracker`):** Fixed a critical streaming bug where Anthropic-format upstream relays backed by OpenAI Responses API models (such as `agentrouter/gpt-6.1-sol`) close the `/v1/messages` HTTP stream immediately after the final `content_block_delta` without emitting `content_block_stop`, `message_delta`, or `message_stop` — causing Claude Code (`v2.1.308`) to discard the unclosed `text` block, fire 4–6 automatic mid-stream continuation retries (`"Your response above was cut off mid-stream"` / `StreamTruncated`), and continue running after the answer finished streaming. `AnthropicStreamLifecycleTracker` in `app/middleware/anthropic_tools.py` (wired into `raw_upstream()` in `app/main.py`) now synthesizes any missing `content_block_stop`, `message_delta` (with backfilled `usage.output_tokens`), and `message_stop` frames at clean `200` EOF while leaving healthy streams byte-identical, and runs `_is_degenerate_output()` after `_backfill_missing_usage()`.
  - **Repo vs. Local Deep Audit Hardening (`app/dualstack_serve.py`, `app/main.py`, `app/middleware/fel_wiring.py`, `app/middleware/response_guard.py`, `app/oauth.py`, `app/config_state.py`, `app/static/app.js`, `app/utils/google_cloudcode_egress.py`, `config.example.yaml`, `requirements.txt`, `app/tests/test_repo_audit_fixes.py`):**
    - Created `.brain/logs` in `acquire_instance_lock()` before opening `.brain/logs/router-{port}.lock` so `python -m app.dualstack_serve --port 6969` never fails with `FileNotFoundError` on a fresh `git clone`.
    - Updated `FelStreamObserver.observe` and `ResponseGuardObserver.observe` to unwrap Cloud Code `v1internal` SSE envelopes (`obj["response"]["candidates"]`) so `_fel_stream_with_recovery` streams Antigravity `/v1internal:streamGenerateContent` chunks in real time instead of buffering until EOF, and extended `response_visible_text()` and `serialize_tool_args()` to support Anthropic, Gemini/`v1internal`, and OpenAI Responses non-streaming payloads.
    - Updated `_fel_stream_with_recovery()` to re-raise upstream exceptions after flushing `_buffered` (preserving `afz_guard` terminal error frames and `_ComboWrapRestart` propagation) and to await `_rec_resp.background()` on recovery responses.
    - Added `OAUTH_PROVIDER_FORMATS` and `ensure_oauth_provider_defaults()` in `app/oauth.py` (plus `init_config()` and `fetchConfig()` enforcement) so connecting an OAuth provider on a fresh clone always persists canonical `type: "oauth"`, `format` (`anthropic` for `claude`, `kiro` for `kiro`, `openai` for others), and backfilled default `models` (including `gemini-4-argon`, `gemini-4-argon-medium`, `gemini-4-argon-low`, and `gemini-3.8-flash-tiered`).
    - Added `HostsBypassSyncBackend` / `google_cloudcode_sync_post()` in `app/utils/google_cloudcode_egress.py` for `_probe_oauth_quota()` (`antigravity` / `gemini-cli`), stripped `content-encoding` on decoded non-stream native Antigravity fallback responses while preserving raw streaming passthrough headers, defaulted `claude_code_integration.enabled` to `False` when absent on a fresh clone, updated `config.example.yaml` (`admin.password: '123456'`) and `requirements.txt` (`pywin32>=306; sys_platform == 'win32'`), and stripped UTF-8 BOMs from 4 test files.
    - **Pass 2 Full-Repo Audit Hardening (`app/config_state.py`, `app/main.py`, `app/antifreeze.py`, `app/middleware/fel_wiring.py`, `app/utils/google_cloudcode_egress.py`):** Removed the shadowed function-local `import shutil` in `init_config()` (`UnboundLocalError` on first-run auto-copy); added the bounded `while True:` + `except _ComboWrapRestart:` loop and `_stream_guard=False` across `egress_stream_guarded`, `gemini_egress_stream_guarded`, and `anthropic_to_openai_egress_stream_guarded`; wrapped `print(..., flush=True)` in `_safe_print()` in `app/antifreeze.py` to prevent Windows pipe `OSError: [Errno 22]` from severing active SSE streams; broadened Google Cloud Code stale-pool retry catches from `httpx.RemoteProtocolError` to `httpx.ProtocolError` (covering `LocalProtocolError: RECV_PING in state CLOSED`); and tracked `_had_thinking` in `FelStreamObserver` so `_fel_stream_with_recovery` flushes thinking/reasoning streams immediately (`4,138/4,138` full repo test suite green).
- **Usage Table, Upstream/Downstream Token & Cache Hit Accounting & False-Positive `499 client_disconnected` Fixes (`app/observability.py`, `app/authenticity/probes.py`, `app/main.py`, `app/middleware/quality.py`, `app/middleware/caching.py`, `app/codex_adapter.py`, `app/normalizer.py`, `app/compat/stream_normalizer.py`, `app/compat/adapters/gemini.py`)**:
  - **Deterministic `authenticity` Judge & `pytest` Exclusion:** Excluded `provider="authenticity"` (`in=0, out=0`) from SQLite `usage_events`, JSONL usage archives, `get_usage()`, error prevention, and circuit-breaker state; guarded `_persist_entry()` and `_persist_usage()` against `PYTEST_CURRENT_TEST` so unit tests never write to production `data/usage_stats.sqlite3` or `data/*.jsonl`; and cleaned historical `data/usage_stats.sqlite3` (`588,216` -> `374,708` genuine rows).
  - **True Provider Token & Cache Accounting + Missing-Usage Backfill:** Prevented Anthropic `message_delta` from overwriting `stats["in"]`/`stats["cached"]` with `0`; unified `_extract_usage_tokens()` across OpenAI, Anthropic, Hybrid (`total_tokens == input_tokens + output_tokens`), Responses API / Codex (`input_tokens_details.cached_tokens`), DeepSeek (`prompt_cache_hit_tokens`), and Gemini (`usageMetadata`) without double-counting cached input tokens; updated `_backfill_missing_usage()` to backfill missing `in` or `out` independently (`tool_call_chars` and bare `delta.reasoning` included) across streaming and non-streaming paths; and removed duplicate non-stream `200 OK` log emission in `app/observability.py`.
  - **False-Positive `499 client_disconnected` Elimination:** Updated `app/authenticity/probes.py` to drain up to 8 trailing chunks after `finish_reason` to capture the terminal `usage` frame, and updated streaming `CancelledError`/`GeneratorExit` handlers across OpenAI, Anthropic, and Gemini egress paths in `app/main.py` to classify post-`[DONE]`/`message_stop`/`finish_reason`/`out > 0` disconnects as `200 OK` while preserving `in_cached`/`in_uncached` on genuine `499` and cached error rows.
- **Combo Multi-Token/Cookie Per-Leaf Failover, Recursive Nested Combos, Live Chain Pruning & Codex OAuth Cache/Quota Alignment (`app/routing/combo_resolver.py`, `app/codex_adapter.py`, `app/circuit_breaker.py`, `app/utils/model_resolver.py`, `app/utils/google_cloudcode_egress.py`, `app/main.py`)**:
  - **Per-Leaf Multi-Token/Cookie Rotation Before Advancing Model ID:** Removed the OAuth-only gate in `_mark_conn_failed_for_retry()` and the `_next_idx < len(active_chain)` pre-gate across all 7 retry sites in `app/main.py`, and keyed `tried_conns` by `f"{provider_name}/{target_model}"` so **all** multi-connection providers (OAuth and non-OAuth API-key/cookie/token pools, including 1-model combos and the last model in a combo) rotate through all eligible tokens/cookies on the current `provider/model` leaf before moving to the next model ID.
  - **Recursive Nested Combo Expansion & Live Config Pruning:** Updated `_expand_nested_combo()` in `app/routing/combo_resolver.py` to recursively expand arbitrary nested combo depth (`Combo -> SubCombo -> SubSubCombo -> Model`) with cycle detection, and updated `advance_combo_retry()` and `_combo_restart_or_give_up()` to prune leaves removed from live `config.yaml` during in-flight retries or Never-Stop pass wraps.
  - **Codex OAuth `prompt_cache_key` Propagation, Quota-Aware Circuit Breaker, Per-Session OAuth Anti-Ban & Unified 1-Button `Smart Prompt Caching` (`app/oauth_antiban.py`, `app/middleware/caching.py`, `app/codex_adapter.py`, `app/circuit_breaker.py`, `app/utils/model_resolver.py`, `app/static/app.js`, `app/main.py`):** Wired `propagate_prompt_cache_key()` onto the OpenAI/Codex branch in `app/main.py` and preserved/synthesized `prompt_cache_key` in `codex_adapter.openai_to_responses()` so `store: False` `/responses` requests achieve server-side prompt cache hits across turns; updated `CircuitBreaker.record_outcome()` to honor upstream `resets_in_seconds`/`resets_at` on `quota_exceeded` (`usage_limit_reached`); updated `model_resolver._pick_connection()` to skip `0%` remaining `5h`/`7d` OAuth accounts when sibling accounts have remaining quota; updated `oauth_antiban.apply_affinity()` so `round_robin: true` on an OAuth provider rotates the starting account **per new conversation/session** (`_SESSION_RR_CURSOR`) while keeping all turns inside an active session pinned to 1 account (`AffinityStore`); and consolidated the 4 per-family caching toggles in the Tools tab into a single **`Smart Prompt Caching (All Model Families)`** switch (`window.toggleSmartPromptCaching`) with 3-stage `cache_control: {"type": "ephemeral"}` injection (`tools[-1]`, `system`, rolling `messages` history) across first-party and Anthropic-compatible lanes, `gpt-6.1-sol/terra/luna` cache-key support, and Kimi folded-prefix `prompt_cache_key` extraction.
  - **Google Cloud Code Egress Edge-IP Rotation:** Added `rotate_google_ip_cache()` in `app/utils/google_cloudcode_egress.py` and wired 3-attempt stale-pool retry + IP cache rotation across `_forward_antigravity_native`, `_forward_antigravity_ccpa_control`, and `antigravity_fetch_available_models`.
- **Native Google Cloud Code Upstream Isolation & 502 Stale-Pool Retry (`app/utils/google_cloudcode_egress.py`, `app/main.py`)**:
  - **Dedicated Router vs Native Egress Pools:** Separated BSL Router's `antigravity` OAuth inference pool (`_ROUTER_ANTIGRAVITY_EGRESS_CLIENT` via `_get_router_antigravity_egress_client()` in `_process_chat_completion_pass`) from the Native Antigravity IDE inference & CCPA control pool (`_ANTIGRAVITY_EGRESS_CLIENT` via `_get_antigravity_egress_client()` in `_forward_antigravity_native`, `_forward_antigravity_ccpa_control`, and `antigravity_fetch_available_models`). Router OAuth model traffic can no longer exhaust, poison, or reset the HTTP/2 connection pool used by native Antigravity IDE models (e.g., Gemini 4 Argon).
  - **Unbounded Streaming Read/Write Timeout & Automatic Stale-Socket Retry:** Changed `_UPSTREAM_TIMEOUT` in `app/utils/google_cloudcode_egress.py` from `read=600.0, write=60.0` (`connect=15.0, pool=60.0`) to `httpx.Timeout(connect=30.0, read=None, write=None, pool=15.0)` (matching `app/main.py:_UPSTREAM_TIMEOUT`) and added a 1-attempt automatic retry + pool reset on `(httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError)` across `_forward_antigravity_native`, `_forward_antigravity_ccpa_control`, and `antigravity_fetch_available_models` when Google closes an idle HTTP/2 keep-alive connection (`RemoteProtocolError: Server disconnected`).
  - **Fail-Open Custom Model Catalog Injection:** Hardened `_merge_antigravity_custom_models_catalog()` and `_inject_custom_models_into_grpc_web_proto()` with per-entry validation and fail-open `try/except` guards so a malformed or broken custom router model entry never corrupts `/v1internal:fetchAvailableModels` or `/GetAvailableModels` or hides the native healthy model list.
- **Authenticity Testing Module Deep Audit Fixes (17 Verified Defects Fixed Across `app/authenticity/*`, `app/main.py`, `app/static/app.js`)**:
  - **Cache Isolation & `auto` Escalation Cache Bypass (`app/authenticity/cache.py`, `app/authenticity/gate.py`, `app/main.py`):** Keyed `AuthenticityCache` by optional `reference_hint` (`endpoint::model_id::reference_hint` when `reference_hint` is non-empty, preserving `endpoint::model_id` for 2-arg callers) and set `_deep_cfg["no_cache"] = True` during `auto` mode shallow->deep escalation in `run_auth_test` so a shallow `family_match` cache hit never short-circuits the deep escalation pass.
  - **Shuffled V1 Knowledge Needles & Per-Candidate Needle Grading (`app/authenticity/gate.py`, `app/authenticity/knowledge_needles.py`, `app/authenticity/matcher.py`):** Fixed `gate.py` to grade against `probe.get("_items") or needles` (preserving the shuffled prompt order from `build_needle_probe`), recorded `"answered"` per item in `classify_needle_answers()`, and updated `matcher.py` to evaluate each candidate standard against its own `known_facts` needle text and `post_cutoff_unknown` `is_unknown` classification instead of relying solely on the pooled `kind` tag.
  - **`token_jsd` Unwrapping & `DECISIVE_LANES` Weight Fallback (`app/authenticity/standard_v2.py`, `app/authenticity/gate.py`, `app/authenticity/judge_rank.py`):** Fixed `compute_v2_dims()` to unwrap `b_lp = _logprob_profile_of(candidates.get(k) or b)` from the full candidate standard before `v2_of()` strips `structural.logprob_profile`, passed `sus_lp` and `b_lp` directly to `similarity_token_jsd()`, forwarded live `features.logprob_profile` into `features.v2_profile` in `gate.py`, and updated `decisive_lane_margin()` and `coverage_adjust()` in `judge_rank.py` to use `FEATURE_WEIGHTS.get(d, 0.03)` so `"token_jsd"` never raises `KeyError` while preserving `sum(FEATURE_WEIGHTS.values()) == 1.0`.
  - **Scoring, Family Classification, Cutoff & Scope Disambiguation (`app/authenticity/scoring.py`, `app/authenticity/classifier.py`, `app/authenticity/fingerprint.py`, `app/authenticity/scope.py`, `app/authenticity/matcher.py`):** Prevented honest unknown Tier-1 replies (`total == 0.30`, `behavior_family == "unknown"`) from false-flagging as `"substitution"`, inverted `fam_score` when `family_verdict.family in ("impostor", "injection")`, required `behavior_family == claimed_family_name` in `classifier.py`, clamped capability ratio to `<= 1.0`, honored `match_result.alias_group` in downgrade checks, preserved underscore-delimited version strings in `canonical_submodel()` (e.g. `deepseek_v4_pro` -> `deepseek-v4-pro`), checked explicit family names first in `scope.detect_family()` while tightening bare `"2.5"`/`"r1"`/`"sol"` patterns, and guarded `fingerprint.py` cutoff extraction so lowercase modal `"may"` (`"I may not know..."`) never sets `cutoff_month = 5`.
  - **Cooperative Cancellation, Standards Harvester & UI Lifecycle (`app/authenticity/gate.py`, `app/authenticity/harvest.py`, `app/authenticity/diffmatrix.py`, `app/authenticity/traitmark.py`, `app/main.py`, `app/static/app.js`):** Propagated cooperative user cancellation (`RuntimeError` matching `"cancelled"`) across all deep lanes in `gate.py` while keeping non-cancel runtime errors fail-open; ignored `_cancel_check` heartbeat steps in `_auth_test_progress`; guarded `_AUTH_TEST_TASKS` against superseding task races; added `source_tag` support to `DELETE /api/authenticity/standards/{family}/{sub_model:path}`, `delete_standard()`, and `authDeleteStandard()`; preserved `MetaLayer.cutoff_override` in `TraitMark`; fixed `diffmatrix.detect_corruption()` needle indices; guarded `None` probe returns and persisted `spatial_curve`/`hard_series` in `harvest.py`; and updated `app/static/app.js` (`authCancelTest`, `_authStartStepPolling`, `authHarvest`).

### Added

- **OAuth Multi-Account Round-Robin, Automatic Next-Account Failover & Dual `5h` / `7d` Quota Indicators (`app/oauth_antiban.py`, `app/circuit_breaker.py`, `app/error_prevention.py`, `app/middleware/stream_guard.py`, `app/codex_adapter.py`, `app/main.py`, `app/static/app.js`, `app/static/index.html`)**:
  - **Unified OAuth Connections Card UI (`VSLLM-Claude` Style) & `Force Refresh` Endpoint:** Unified the OAuth provider `Connections` card in `app/static/app.js` with the Custom Provider `Connections` card layout (`+ Add Account` / `+ Connect`, `Import JSON` for Codex, `Force Refresh` button calling `POST /api/oauth/{provider_id}/force-refresh` + `refreshQuotaBars(true)`, and the `Round Robin` `.switch` slider toggle). When `round_robin: true` is enabled on an OAuth provider, `oauth_antiban.apply_affinity()` bypasses sticky 1-account session pinning so `model_resolver._pick_connection` rotates across all healthy OAuth accounts.
  - **Connection Move Up/Down Persistence & Identity-Keyed Quota Cache:** Fixed `_apply_connections_stale_save_guard()` in `app/main.py` so body rows are merged first in client order (preserving `moveConnectionUp` / `moveConnectionDown` across saves and hard refreshes) while server-only rows from stale tabs are appended afterwards; added `_swapModelConnectionIndexes()` in `app/static/app.js`; keyed `_OAUTH_PROBE_CACHE` by connection identity (`_oauth_conn_cache_key`) and remapped `_OAUTH_QUOTA` and `oauth_antiban._CONNECTION_USAGE` on connection reorder in `POST /api/config` (with monotonic `_quotaFetchSeq` in `refreshQuotaBars`) so Cookie 1 and Cookie 2 never swap displayed quota limits after reordering.
  - **Codex Non-Stream SSE Assembly & Fast `/api/test-model` Probe:** Extended `codex_adapter.assemble_responses_from_sse()` and `responses_json_to_openai()` to capture `response.output_text.done`, `response.content_part.done`, `response.reasoning_summary_text.delta`, and `response.failed` / `response.incomplete` / `error` SSE events (propagating upstream `429`/`401`/`400`/`502` errors for multi-account failover instead of misclassifying as `504 zombie_empty_response`); added `_bsl_test_probe` in `/api/test-model` to skip the 229-token FEL execution contract injection and use `effort: "low"` on Codex connectivity probes.
  - **Automatic Next-Account OAuth Failover:** Added `"claude"` to `OAUTH_AFFINITY_PROVIDERS` and `provider_config.get("type") == "oauth"` detection in `is_oauth_provider()`; `oauth_antiban.govern_post_send()` now immediately releases sticky session affinity on `401`, `402`, `403`, `429`, or `5xx`; `circuit_breaker._classify_for_breaker()` classifies `402` and `400` responses carrying OAuth quota/credit/token markers (`usage_limit_reached`, `insufficient_quota`, `credit balance`, `plan_limit`, `invalid_grant`, `token_expired`, `account_deactivated`, etc.) before generic `invalid_request_error` client errors; `ErrorPreventionManager.record_success()` clears provider/model `softban` state when a sibling OAuth account succeeds; `stream_guard.py` recognizes Codex Responses scaffolding events (`response.created`, `response.in_progress`, `response.output_item.added`, `response.content_part.added`) so pre-content `event: response.failed` frames fail over cleanly; and `app/main.py` wires `_mark_conn_failed_for_retry()` across streaming and non-streaming fallback paths to keep the combo index on the current OAuth provider while untried sibling accounts remain.
  - **Dual `5h` & `7d` (Weekly) OAuth Quota Bars, Auto-Refresh & Antigravity `Google` / `Claude` Quota Groups:** Upgraded `GET /api/quota/status` (`?provider=<id>&force=1`) and `_probe_oauth_quota()` with automatic expired-token refresh (`_ensure_and_probe_oauth_quota` with per-connection failure cooldown and non-sleeping refresh serialization + stdlib DNS fallback in `google_cloudcode_egress.py` when `dnspython` is absent, plus `_update_connection_token` syncing both `api_key` and `access_token`) and a 3-tier quota engine combining passive response headers (Claude `anthropic-ratelimit-unified-5h/7d-*`, Codex `x-codex-primary/secondary-*`), active upstream OAuth probes (`codex` `/backend-api/wham/usage` with `chatgpt-account-id`, `claude` `/api/oauth/usage` with `anthropic-beta: oauth-2025-04-20`, `antigravity`/`gemini-cli` via `daily-cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary` / `fetchAvailableModels` divided into separate **`Google`** and **`Claude`** `5h`/`7d` quota groups synced with the Antigravity app, `kiro` `q.{region}.amazonaws.com/getUsageLimits` with `profileArn` + `freeTrialInfo` breakdown, `grok-cli` `cli-chat-proxy.grok.com/v1/billing?format=credits` with `x-xai-token-auth` / `x-grok-client-identifier` / `_unbox_val` unwrapping, `cursor` `DashboardService/GetCurrentPeriodUsage` + `WorkosCursorSessionToken` fallback, `github` `/copilot_internal/user` `quota_snapshots`, `kimi` `api.kimi.com/coding/v1/usages`), and a universal local 5h/7d sliding-window tracker (`oauth_antiban._CONNECTION_USAGE` retained for 7 days).
  - **OAuth Account Subscription Tier Detection & UI Pill Badges (`_normalize_oauth_tier`, `plan`, `[data-conn-tier]`):** Added `_normalize_oauth_tier()` in `app/main.py` and `_inferInitialConnTier()` / `_renderTierBadgeHtml()` in `app/static/app.js` to identify and render a color-coded subscription tier pill beside every OAuth connection cookie/token name: **Antigravity / Gemini CLI** (`Free`, `Plus`, `Pro Trial`, `Pro`, `Ultra`, `Business`, `Enterprise` via `v1internal:loadCodeAssist` with `{"metadata": {"ideType": "ANTIGRAVITY"}, "mode": 1}` inspecting `paidTier` before `currentTier`), **Codex** (`Free`, `Go`, `K12`, `Edu`, `Plus`, `Pro`, `Pro x5`, `Pro x20`, `Pro x50`, `Team`, `Business`, `Enterprise` from `wham/usage`, JWT `https://api.openai.com/auth.chatgpt_plan_type`, and `provider_data`), **Claude** (`Free`, `Pro`, `Max`, `Max 5x`, `Max 20x`, `Team`, `Enterprise`), **Grok CLI** (`Free`, `SuperGrok`, `SuperGrok Lite`, `SuperGrok Heavy`, `X Basic`, `X Premium`, `X Premium Plus`, `Grok Code` from JWT `tier` / `hasGrokCodeAccess` and billing config), **Kiro** (`Free`, `Free Trial`, `Pro`, `Pro+`, `Pro Trial`, `Power` from `subscriptionInfo.subscriptionTitle` and `freeTrialInfo`), **Cursor** (`Free`, `Free Trial`, `Pro`, `Pro+`, `Pro Trial`, `Business`, `Ultra` from `GetPlanInfo` / `planInfo`), **GitHub Copilot** (`Free`, `Pro`, `Pro+`, `Student`, `Business`, `Enterprise` from `copilot_plan` / `access_type_sku`), and **Kimi** (`Moderato`, `Allegretto`, `Allegro`, `Vivace` from `user.membership.level`).
- **Unified App Integration Tab (`Antigravity` + `Claude Code`) & Native Antigravity Runtime Patcher (`app/antigravity_patcher.py`)** - Replaced the single-app Antigravity tab with a unified **App Integration** tab covering **Antigravity (App / IDE)** and **Claude Code (Desktop / CLI)**, both powered by BSL Router's shared Combo Model selector modal (`openAddModelModal('antigravity' | 'claude_code')`).
  - **Antigravity (App / IDE) Direct Integration (no `:50999` middle proxy, Option A Toolbar & Startup Persistence):** Eliminates the external `antigravity-add-model` (`ag-model`) proxy that previously prematurely opened downstream HTTP `200` Gemini SSE streams before reading the first upstream chunk (which tripped `StreamGuard` and blocked BSL Combo Model fallback). BSL Router now natively patches Antigravity IDE `settings.json` (`jetski.cloudCodeUrl: http://127.0.0.1:6969`) and Antigravity Desktop `resources/app.asar` (`dist/main.js` + `dist/languageServer.js`), injects custom models into `/v1internal:fetchAvailableModels` (JSON) and `/GetAvailableModels` (gRPC-web protobuf), and resolves `MODEL_PLACEHOLDER_M<idx>` (`1000 + idx`) and `models/custom/<slug>` directly through `_process_chat_completion` with full combo fallback and circuit-breaker support. Custom models are never auto-imported from a machine-local `~/.gemini/antigravity/` directory (2026-10-22: the legacy migration lane was removed so the public repo ships a clean empty list, and `~/.gemini/antigravity/custom_models.json` is cleaned up). The Antigravity card toolbar uses **Option A**: `Start Integration` (`POST /api/mitm/start-full`) and `Stop Integration` (`POST /api/mitm/stop-full`) act as the single master toggle calling `patch_antigravity(restore=False/True)`, paired with a conditional `Restart Antigravity to Apply` button (`#app-ag-restart-btn` -> `restart_and_apply_staged_asar()`) visible only when `desktop_status === "staged_restart_required"` plus a 2-second lifespan watcher (`_staged_asar_watcher`) that automatically swaps `app.asar.bsl-staged` into `app.asar` when Antigravity closes. Both Antigravity (`patch_antigravity(restore=False)`) and Claude Code (`_sync_claude_code_settings()`) integrations are automatically re-enforced on every server boot in `lifespan()`.
  - **Claude Code (Desktop / CLI) Direct Integration:** Adds `claude_code_integration` config management, safe `claude-bsl-<N>` alias generation (passing Claude Desktop's `ko()`/`SCe` competitor blocklist and CLI's `/(claude|anthropic)/i` filter while preserving custom `display_name` labels), `/v1/models` catalog injection (returning **only** the configured BSL Router Integration models on Claude Desktop gateway discovery requests when `replace_builtin: true` so raw provider/combo inference models never pollute the dropdown), always emitting dedicated tier slots (`claude-bsl-tier-opus`, `claude-bsl-tier-sonnet`, `claude-bsl-tier-haiku`, `claude-bsl-tier-default`) even when a tier slot shares a target with a custom model, `/v1/messages` alias + tier-slot resolution, and `POST /api/app-integration/claude-code/sync` atomic updates to both `~/.claude/settings.json` (`ANTHROPIC_BASE_URL`, `CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY="1"`, `Default`/`Opus`/`Sonnet`/`Haiku` tier overrides, and `modelPicker` with `replaceBuiltInOptions`) and `%LOCALAPPDATA%\Claude-3p\configLibrary\<appliedId>.json` (`inferenceModels` with `labelOverride`).

### Fixed

- **Model Authenticity Testing V2 Lanes (`spatial_curve`, `token_jsd`, `hard_series`) (`app/authenticity/standard_v2.py`, `app/authenticity/gate.py`, `app/authenticity/harvest.py`)**: Added V2 authenticity verification lanes `spatial_curve` (2D/3D spatial reasoning decay curve), `token_jsd` (Jensen-Shannon divergence over token logprobs), and `hard_series` (verified hard-math tiers + judge-graded expert probes `visual_svg`, `spatial_3d`, `sys_design`).
- **Error Prevention ban parameters ignored + anti-downgrade guard (softban/longban/consecutive_threshold now honored)** - User report: "the soft ban & hard ban function doesnt work correctly. when active, i change the parameters (soft ban/long ban time, consecutive error,...) but it doesnt change according to." Root cause (two coupled defects in `ErrorPreventionManager.record_error`): (1) the zero-strike immediate-cooldown branch fired for EVERY classified error type before the streak could reach the user-configured `consecutive_threshold`, making the softban -> longban -> disabled escalation ladder unreachable and the user's softban/longban durations dead knobs; (2) concurrent in-flight requests that had selected a leaf before a ladder ban landed re-entered the immediate-cooldown branch late and OVERWROTE the active escalated ban with a ~90s cooldown (proven: strike 3 overwrote an active 420s softban with 90s, strike 5 overwrote an active 4620s longban with 90s - a user setting softban_duration_minutes=7 observed 90-second bans). Fix: the immediate cooldown now only fires while `streak < threshold` and the streak accumulates across errors; an anti-downgrade guard placed after the streak increment but before ban mutation returns `None` for a permanent `disabled` state and for any active `softban`/`longban` whose `ban_until` exceeds the new cooldown (a strictly longer new cooldown still extends the ban). Escalation ladder, 400/422/499 exclusion, billing-marker exception, persistence/sidecar split, and `_cooldown_by_type` mapping unchanged. 4 new regression tests in `app/tests/test_dead_leaf_self_heal.py`. Full suite 3833/3833 on Python 3.11 and 3.12; focused ban suites 29/29 on both; scenario proof exits 0 on both.

### 🇻🇳 Tiếng Việt — Tính Năng Mới & Sửa Lỗi Trong `v1.1.0`

- **Tab Tích Hợp Ứng Dụng Hợp Nhất (`Antigravity` + `Claude Code`) & Runtime Patcher Trực Tiếp (`app/antigravity_patcher.py`, `app/main.py`, `app/static/app.js`)**:
  - **Tích hợp trực tiếp Antigravity (App / IDE) (Loại bỏ proxy trung gian `:50999`, Thanh công cụ Option A & Tự động duy trì khi khởi động):** Patch trực tiếp `settings.json` của Antigravity IDE (`jetski.cloudCodeUrl: http://127.0.0.1:6969`) và `resources/app.asar` của Antigravity Desktop, bơm danh mục custom model vào `/v1internal:fetchAvailableModels` và `/GetAvailableModels`, định tuyến `MODEL_PLACEHOLDER_M<idx>` qua chuỗi fallback của BSL Router mà không mở sớm stream HTTP 200.
  - **Tích hợp trực tiếp Claude Code (Desktop / CLI):** Tự động tạo alias an toàn `claude-bsl-<N>` và các slot tầng (`claude-bsl-tier-opus/sonnet/haiku/default`), trả về danh mục model độc quyền trên `/v1/models` khi bật `replace_builtin: true` (loại bỏ 53+ model thô khỏi menu chọn model của Claude Desktop), và đồng bộ nguyên tử vào cả `~/.claude/settings.json` lẫn `%LOCALAPPDATA%\Claude-3p\configLibrary\<appliedId>.json` (`inferenceModels`).
- **Đại Tu Toàn Diện Faithful Execution Layer (FEL) & Hỗ Trợ `gemini-4-argon` Native (`app/middleware/faithful_execution.py`, `app/middleware/fel_wiring.py`, `app/main.py`)**:
  - Bọc toàn bộ 6 luồng streaming egress cộng với luồng passthrough native của Antigravity IDE (`_forward_antigravity_native`) bằng `_wrap_egress_stream()`, hỗ trợ phong bì Google Cloud Code (`request.contents` / `request.systemInstruction`) cho `gemini-4-argon`, và backfill `refusal_class` + `fel` vào SQLite `usage_events` qua `_RECENT_REQUEST_ROW_IDS`.
  - Tăng tốc quét từ nhạy cảm **~223 lần** (`3.804,2 ms -> 17,06 ms` cho 20 khối 14 KB) nhờ bộ nhớ đệm regex theo từ bắt đầu (`_COMPILED_SENSITIVITY_CACHE`) và đường tắt ASCII.
  - Giữ nguyên `cache_control` của Anthropic khi nối directive FEL, hỗ trợ làm mềm từ chối đầu stream (`_fel_stream_with_recovery`), bổ sung bộ nhận diện từ chối tiếng Trung (ZH) và tiếng Việt (VI), kèm thanh Telemetry FEL 7 ngày và hộp thử nghiệm Dry-Run (`POST /admin/fel/preview`) ngay trên giao diện Tools.
- **Chống Khóa Tài Khoản OAuth Theo Phiên (Per-Session Anti-Ban), Thanh Quota Kép `5h`/`7d`, Huy Hiệu Hạng Thuê Bao & Nhập JSON Codex (`app/oauth_antiban.py`, `app/oauth.py`, `app/main.py`, `app/static/app.js`)**:
  - Khi bật `Round Robin` trên provider OAuth, router xoay tài khoản bắt đầu **theo từng phiên hội thoại mới** (`_SESSION_RR_CURSOR`) nhưng ghim toàn bộ các lượt trong cùng một phiên vào 1 tài khoản duy nhất (`AffinityStore`), vừa triệt tiêu nguy cơ bị khóa tài khoản do trộn token giữa cuộc hội thoại vừa giữ ấm bộ nhớ đệm KV prompt cache.
  - Hiển thị thanh tiến trình quota **`5h` và `7d`**, thời gian hồi phục (`↻`), và huy hiệu hạng tài khoản (`Free`, `Plus`, `Pro`, `Ultra`, `Pro x5/x20/x50`, `Max 5x/20x`, `SuperGrok`, `Power`, v.v.) cho toàn bộ 9 provider OAuth, cùng tính năng **Import JSON** an toàn cho Codex (`POST /api/oauth/codex/import-json`).
- **Bộ Nhớ Đệm Thông Minh 1 Nút Bấm (`Smart Prompt Caching`), Wildcard Họ Model 2026, Xoay Đa Token Trong Combo & Chuẩn Hóa Thống Kê Token/Cache**:
  - Hợp nhất 4 công tắc cache thành 1 nút duy nhất **`Smart Prompt Caching (All Model Families)`** tự động chèn 3 tầng điểm ngắt `cache_control` và `prompt_cache_key` trên các họ model 2026 (`K3*`, `M3.1`/`M3*`, `V4*`/`V4.1+`, `Gemini 4*`/`Argon`, `GPT-5.6/6.1`).
  - Xoay hết toàn bộ token/cookie khả dụng trên cùng một `provider/model` trước khi chuyển sang model kế tiếp trong Combo; hỗ trợ Combo lồng nhau đệ quy.
  - Loại bỏ request nội bộ của `authenticity` và `pytest` khỏi bảng Usage, chuẩn hóa đếm token/cache hit không tính trùng, và sửa dứt điểm lỗi báo giả `499 client_disconnected` sau khi stream đã hoàn tất (`200 OK`).
- **Cách Ly Pool Google Cloud Code Native & Sửa 17 Lỗi Kiểm Định Độ Xác Thực Model (Authenticity V2/V3)**:
  - Tách biệt hoàn toàn pool kết nối HTTP/2 của Antigravity IDE native (`_ANTIGRAVITY_EGRESS_CLIENT`) khỏi pool OAuth của router (`_ROUTER_ANTIGRAVITY_EGRESS_CLIENT`) kèm cơ chế tự xoay IP (`rotate_google_ip_cache()`); bổ sung 3 làn kiểm định V2 (`spatial_curve`, `token_jsd`, `hard_series`) và khắc phục 17 lỗi kiểm toán trong mô-đun Authenticity.
- **Sửa Lỗi OAuth Antigravity Ngay Khi `git clone`, Sửa Lỗi Crash Cuối Stream Custom Model Antigravity (`SSE_DONE = b": [DONE]\n\n"`) & Tổng Hợp Khung Kết Thúc SSE Anthropic-to-Anthropic (`agentrouter/gpt-6.1-sol` / Claude Code)**:
  - Khôi phục thông tin OAuth public client mặc định của Google Cloud Code (giải mã base64 lúc chạy) và tự động trộn cấu hình `oauth` trong `get_config()` để người dùng mới `git clone` đăng nhập OAuth Antigravity ngay lập tức mà không cần cấu hình biến môi trường thủ công.
  - Đổi `SSE_DONE` trên luồng Gemini SSE (`app/compat/adapters/gemini.py` và `app/antifreeze.py`) từ `b"data: [DONE]\n\n"` sang dòng chú thích SSE `b": [DONE]\n\n"` và cho phép `_fel_stream_with_recovery()` chuyển tiếp ngay lập tức các khung `: heartbeat` / `: keepalive`, khắc phục triệt để lỗi `proto: syntax error (line 1:1): unexpected token [` (`Error Unknown: Agent execution terminated due to error`) trong `language_server.exe` của Antigravity IDE sau khi custom model (`Gemini-Argon`, `Chat2API`, v.v.) vừa trả lời xong.
  - Bổ sung `AnthropicStreamLifecycleTracker` (`app/middleware/anthropic_tools.py` + `app/main.py`) tự động tổng hợp các khung `content_block_stop`, `message_delta` (kèm `output_tokens` đã backfill) và `message_stop` bị thiếu khi các relay Anthropic như `agentrouter/gpt-6.1-sol` đóng luồng HTTP ngay sau `content_block_delta`, chấm dứt hoàn toàn lỗi Claude Code tự gọi lặp 4–6 lần (`cut off mid-stream` / `StreamTruncated`) và tiếp tục chạy ngầm sau khi câu trả lời đã hiển thị xong.
  - **Kiểm Toán Chuyên Sâu Repo vs. Local (`test_repo_audit_fixes.py`):** Tự động tạo `.brain/logs` trong `acquire_instance_lock()` khi `git clone` mới; bóc tách phong bì `v1internal` (`obj["response"]["candidates"]`) trong `FelStreamObserver` và `ResponseGuardObserver` để luồng `/v1internal:streamGenerateContent` của Antigravity IDE luôn stream thời gian thực khi bật FEL; mở rộng `response_visible_text()` và `serialize_tool_args()` cho Anthropic/Gemini/Responses API; chuyển tiếp ngoại lệ từ `_fel_stream_with_recovery()` về `afz_guard` và đóng task nền của `_rec_resp`; tự động gán `format` chuẩn (`anthropic` cho `claude`, `kiro` cho `kiro`, `openai` cho các provider khác) và danh sách `models` mặc định (bổ sung `gemini-4-argon`, `gemini-4-argon-medium`, `gemini-4-argon-low`, `gemini-3.8-flash-tiered`) khi kết nối OAuth trên bản clone mới; dùng `google_cloudcode_sync_post` vượt hosts file cho `_probe_oauth_quota`; ép `accept-encoding: identity` cho luồng passthrough native Antigravity; và chuẩn hóa `config.example.yaml` + `requirements.txt` (`pywin32` trên Windows).
- **Lá Chắn Bảo Mật 3 Tầng Chống API Key Độc Hại & Relay Giả Mạo (`app/security/key_scanner.py`, `app/middleware/response_guard.py`, `app/main.py`, `app/static/app.js`, `app/tests/test_key_scanner.py`, `app/tests/test_response_guard.py`)**:
  - **Tầng 1 — Quét Tĩnh API Key, Endpoint & Custom Header (`key_scanner.py`, `/api/verify-key`, `/api/scan-keys`):** Phát hiện ký tự ẩn Unicode BiDi/zero-width trojan (`HIDDEN_UNICODE_PATTERN`), nội suy biến môi trường (`${...}`) / path traversal (`../`) / lệnh độc hại (`KEY_PAYLOAD_PATTERNS`), giải mã và kiểm tra payload Base64 nhúng trong API key (`_detect_base64_payload_in_key`), mở rộng danh sách 28+ tên miền webhook/tunnel nghi ngờ (`SUSPICIOUS_DOMAINS`) + đường dẫn webhook exfil (`WEBHOOK_EXFIL_PATHS`), và quét `header_custom` (`_scan_header_custom` chặn ghi đè `Host`/`Authorization`/`X-Forwarded-Host`, CRLF và payload).
  - **Tầng 3 — Kiểm Soát Phản Hồi & Chặn Tool-Call Độc Hại Từ Upstream (`response_guard.py`, `app/main.py`):** Bổ sung `tool_secret_exfil_network`, `tool_lolbas_exec`, và `tool_agent_config_hijack` trong `_TOOL_ARG_MARKERS`; ghi nhận `marker.category` cho các phát hiện trên `tool_calls`; triển khai chế độ `redact` chủ động trong `apply_guard()` trên cả 4 giao thức (`OpenAI Chat`, `Anthropic Messages`, `Gemini v1beta/v1internal`, `OpenAI Responses`) để tự động che văn bản bị tiêm nhiễm và cắt bỏ `tool_calls`/`tool_use`/`functionCall` độc hại; bổ sung API `GET /api/security/response-guard` và `POST /api/security/response-guard/clear`.
  - **Giao Diện Quản Trị & Cảnh Báo Trực Quan (`app/static/app.js`):** Hiển thị chi tiết `data.blocked` và `data.scan_findings` khi xác minh key hoặc tải danh sách model, và bổ sung thẻ **🛡️ Malicious API Key & Response Injection Shield** ngay trên tab **Tools** với bộ chọn chế độ `log_only` / `redact`, nút quét bảo mật toàn bộ provider, và bảng thống kê telemetry thời gian thực.

---

## [1.0.10] - 2026-09-27

### Fixed

- **502 `midstream_transport` on keepalive-before-death streams — SSE comment bytes no longer veto fallback** — providers (vsllm, a6api, seekai, zhipu) emit SSE comment keepalives (`: PING\n\n`, `: keepalive`, `: ka`) during slow reasoning TTFT. The raw pump paths called `mark_emitted(chunk)` unconditionally on every forwarded byte, so an 8-byte PING flipped `emitted=True` permanently — `may_fallback()` then refused every later fallback, including the combo advance when the leaf died mid-stream with zero content. Logged shape, verbatim: `[STREAM-GUARD] vsllm-o/glm-5.2: refused 1 post-emission fallback(s) after 8B [midstream_transport_502] | first-bytes: b': PING\n\n'`. Fix: the BUG M helper `is_scaffolding_only()` (already in `stream_guard.py` but never called) is now consulted at all four raw-pump emission sites (OpenAI pump, tool-guard output, Anthropic pump, Anthropic→OpenAI pre-normalizer pump). Keepalive-only chunks are still yielded (preserving TTFT/keepalive), but they no longer commit the transcript. The fail-open contract is preserved: any chunk that cannot be *proven* scaffolding still counts as content. New test file `test_stream_guard_keepalive.py` (11 tests). 374 stream/quality tests pass; no existing suite regressed.
- **502 `midstream_transport` on thinking-heavy streams — AntiStop continuation now fires** — `StreamTruncationDetector._observe` only accumulated body `content`/`text` deltas, never `reasoning_content` (OpenAI), `thinking_delta`/`delta.thinking` (Anthropic), or Gemini thought parts. On thinking-heavy streams `partial_text` stayed empty, so `should_splice_continuation` refused on its `not partial_text.strip()` gate and the transport death became a terminal 502 instead of a same-provider resume. Fix: `_observe` now accumulates reasoning/thinking deltas across all three dialects. Across the entire 383 MB production log there was not a single `[AntiStop] transport death ... splicing continuation` line — this gate was the blocker. Same-provider invariant (BUG J) fully preserved; no second provider is ever spliced into a live parser.
- **MintRouter In: 0 / Out: 0 on healthy streams — usage backfill from delivered text** — aggregator relays (MintRouter is the observed case) stream content frames but omit the terminal usage object, leaving `stats["in"]`/`stats["out"]` at 0 even though the request succeeded. The log read `In: 0 | Out: 0` and the zero-output fallback mis-fired. Fix: `_backfill_missing_usage()` runs at stream end only when both counts are 0 AND real text was delivered — a genuine usage frame always wins (early return), and a dead leaf (empty `partial_text`) is never masked. Estimates `out` from delivered text length and `in` from `_count_tokens(internal_request.messages)`. Fail-open: any estimator error leaves stats untouched.
- **IDE freeze on MintRouter streams — Gemini egress drain loop had an UNBOUNDED keepalive** — the Antigravity IDE talks to the router over `client=gemini`, which routes through the Gemini egress adapter (`gemini_egress_stream`), NOT the OpenAI `_pump` path. MintRouter streams content then leaves the TCP socket half-open (no `[DONE]`, no EOF). The drain loop's `except asyncio.TimeoutError` branch yielded `: keepalive` and `continue`d **forever** — the upstream never closed, `_drain_openai_source` never finished, `_q` never saw the `_SENTINEL`, so the response never completed and the IDE's per-conversation request lock stayed held (closing the IDE window did NOT free it — the wedged connection lives router-side, keyed to the conversation). Fix: new `GEMINI_EGRESS_MAX_IDLE = 300.0` ceiling. The drain loop tracks `_last_upstream_data` (monotonic), reset ONLY on a real upstream chunk (a keepalive tick never resets it), and breaks cleanly past 300s so the existing terminal/zero-token/combo-fallback handling runs and the response ENDS. 300s sits above the worst observed legit reasoning gap (~180s), so a healthy long-thinking stream (every chunk resets) never trips it; only a relay that produced NO upstream bytes for the full window ends early. New test file `test_gemini_egress_idle.py` (8 tests: cap ends a never-closing relay, healthy close not killed, keepalive ticks don't reset, second-chunk reset, client-disconnect pre-empts, backfill fires, real usage untouched, dead leaf not masked).
- **MintRouter In: 0 / Out: 0 on the Gemini (IDE) path — backfill now wired there too** — the morning's backfill only wired the OpenAI `_pump` path (~L9668), which the IDE never uses. The Gemini path read usage inline in `_raw_ok` but MintRouter omits the terminal usage frame, so `In: 0 | Out: 0` persisted for IDE requests even after the first fix. Fix: `_backfill_missing_usage` is now also called on the Gemini egress path (after the drain loop completes, before the zero-token guard), mirroring the OpenAI site with the path's own `stats` / `internal_request.messages` / `_detector.partial_text`. Verified live on a `:6971` test instance with an IDE-mimicking `client=gemini` streaming request to `mintrouter/muse-spark-1.3-contributor-free`: `END status=200 client=gemini ... in=1099 out=358` (was `0/0`), completed in 8s with `error=-` (no freeze).

- **Antigravity Integration: mapped models non-sticky, revert on F5 (Blacksand targets silently dropped)** — the admin UI's Antigravity Integration dropdown (`agOptions()` in `app/static/app.js`) offers three target sources: combo aliases, **BSL Models** (bare Blacksand family IDs such as `blacksand-agentic`, `blacksand-chat`, `blacksand-agentic-ultra`, `blacksand-agentic-max`), and `provider/model`. The save-time validator `_is_known_antigravity_mapping_target` recognized only combo aliases and `provider/model`, so a slot mapped to a **BSL Model** was silently discarded on `POST /api/config` and reverted to "Native Antigravity inference" on the next page load — even though the toast reported "Mapped …". Verified live with a controlled round-trip: a combo-alias target survived save+reload while a `blacksand-agentic` target was dropped. Fix: `_is_known_antigravity_mapping_target` now normalizes the target through the same `_BLACKSAND_MODEL_ALIASES` table the runtime dispatch uses and accepts Blacksand family IDs (including the `bsl-*`/`BSL-*` spellings). These IDs were already routable at inference time (`_BLACKSAND_MODEL_ALIASES` + the `_bsl_*_dispatch` branches); the gap was purely in the save-time validator. 34/34 antigravity tests pass (1 new regression test asserting all 5 Blacksand family IDs are accepted); the 4-gate pre-push check passes.

---

## [1.0.8] - 2026-09-24

### Fixed

- **GLM-5.3 streaming tool calls broken (invalid tool call in IDE)** — the 5th-dialect ChatML `<tool_use><invoke>` parser was wired into the *buffered* path (`normalize_glm_tool_calls`) but the *streaming* rescue path (`stream_normalizer._drain_rescue` via `find_earliest_opener`) had no `<tool_use>` opener, so GLM-5.3 tool calls leaked as raw prose text to the IDE instead of structured tool_use blocks. Added `_TOOL_USE_OPEN_RE`/`_TOOL_USE_CLOSE_RE` and extended the opener/closer/parser trio to a 3-mode dispatch (`"tool_call"` / `"unicode"` / `"tool_use"`). Affects all GLM-5.3 providers (a6api, vsllm, zhipu direct) on streaming requests with tools. 62/62 GLM tool-call tests pass.
- **CRITICAL: stale-tab config wipe protection — 94 provider keys wiped via blank-key save** — a stale admin dashboard tab (loaded before an earlier session's config changes) sent `POST /api/config` with blank `api_key` fields. The existing `_apply_connections_stale_save_guard` protected only the `antigravity` provider; all other 77 providers' keys were atomically persisted to disk as blank. Recovery from the last backup restored 89/94 keys; 5 keys (added post-Sept-21) remain lost. Three independent safeguards added to `_persist_config_snapshot` and `_replace_runtime_config`:
  - **Blank-key restoration guard** (`_replace_runtime_config`): if any incoming connection has `api_key: ''` while the live runtime holds a real key for that provider, the live key is restored before any disk write. All providers covered, not just `antigravity`.
  - **Key-count regression gate** (`_persist_config_snapshot`): counts non-empty `api_key`/`refresh_token`/`access_token` fields on disk vs the incoming config. **Refuses** any write that would drop ≥50% of existing secrets (the 94→0 wipe signature). **Warns** on any 10–49% drop. Existing gates 2 (zero-provider) and 2b (provider-count) did not check inside connection secrets — this fills the gap.
  - **Multi-generation backup rotation** (`_persist_config_snapshot`): replaces the single `.bak` (overwritten on every save) with up to 5 timestamped `config.yaml.bak-YYYYMMDD-HHMMSS` backups. A bad save can no longer silently overwrite the only recovery point.
  - All 6 persistence-gate unit tests pass; full CI matrix (3.10/3.11/3.12) green: 3168 × 3 = 9504.
- **Antigravity / Gemini 400 `thought_signature` tool-call errors — cache-miss hardening + name/id-agnostic fallback re-signing** — the Antigravity IDE echoes prior tool calls back on the next turn WITHOUT the inline `thoughtSignature` Google requires on `functionCall` parts, and Gemini 400s the request. The signature cache (`app/compat/adapters/antigravity_upstream.py`) had three compounding gaps: (1) the primary lookup key embeds the volatile per-turn `call_{name}_{counter}` id AND the tool `name`, both of which churn across turns (the IDE substitutes placeholder names like `default_api:invalid`); (2) TTL was a short 900s; (3) eviction used `time.monotonic()`, which drifts from wall-clock across suspend/resume. Fix: extract a shared `_args_digest` (canonical sort_keys sha256 of the tool args) so store-side and lookup-side always agree; bump `_SIGNATURE_CACHE_TTL` 900s → 3600s and switch the clock to `time.time()`; add `_signature_cache_lookup_fallback(model, args)` — a **name/id-agnostic** last-resort match on `(model, args_digest)` that returns a signature ONLY when exactly one distinct live signature matches (ambiguous → None, never attaches the wrong signature); and add a throttled one-shot `_warn_unsigned_once` diagnostic when a `functionCall` would go out unsigned (the part Google 400s on). Verified live: `gemini-pro-agent` and `gemini-3.6-flash-high` multi-turn tool calls (turn 2 echoes results with the signature stripped) re-signed from the fallback → Google 200, no 400. 27/27 thought_signature tests pass (4 new regression tests: fallback re-injection, placeholder-name survival, TTL expiry, unambiguous-match).
- **502 `midstream_transport` on long GLM/reasoning streams — traced; confirmed handled, root cause is upstream-side** — 935 occurrences across `app.out.log`, overwhelmingly `midstream_transport: peer closed` (385 exact) where the upstream provider (vsllm / a6api / seekai / zhipu) closes the HTTP/2 connection mid-stream after ~120–160s on long `effort:max` GLM-5.3 reasoning generations. The router's `_midstream_transport_fallback` (BUG J) already normalizes this into the combo-failover rail: `out==0` → clean advance to the next chain entry (invisible to the client); `out>0` → the AntiStop continuation splice (`app/middleware/quality.py::should_splice_continuation`, live at 3 stream sites) resumes the SAME provider/model from the partial text instead of corrupting the transcript with a second provider. Only 5/935 (~0.5%) escalate to a client-visible terminal 502 — those are cases where the chain was already exhausted AND content had started, so no safe failover exists. Residual is an upstream capacity/timeout characteristic, not a router defect; mitigation is config-side (chain depth / shorter reasoning effort / provider health), no code change required.

---

## [1.0.7] - 2026-09-24

A reliability + research-tooling wave: terminal relay walls (`upstream_safety_blocked`, `data_inspection_failed`) are now recognized as **hard provider-side blocks** and stopped at the source instead of being retried forever — plus a **failure-aware context-wide FEL reframe** (Tier-2) so large conversation payloads that trip GLM/Qwen input inspection get ONE whole-context reframe + retry before any wall is declared terminal.

### Added

- **FEL context-wide reframe (Tier-2, failure-aware)** — `reframe.context_scope: "full"` opt-in reframes the WHOLE payload (user history + assistant history + tool outputs) via the same proven bilingual sensitivity-map span rewriter (`_reframe_span_text`, extracted from `reframe_text` — last-turn behavior byte-identical, pinned by regression test). System prompt, the last user turn, and non-text parts (images/base64) are never touched; caps (`context_max_messages` 200 / `context_max_chars` 400k) bound the walk; fully fail-open. Attestation/engagement line still prepends exactly ONCE (last turn) — never duplicated per message. **Smart escalation** (`reframe.escalate_on_inspection_fail`, default ON): when a FEL-enabled request hits `data_inspection_failed`, the router reframes the full context and re-dispatches exactly ONCE (`bsl_fel_context_escalated` guard) before falling through to the terminal-wall path — zero cost on the happy path, no infinite loop on the failure path.
- **Consumption chart — group-by (By model / By provider) with stacked series + legend** — the "Consumption over time" card gains a two-option group-by toggle (`usageGroupBy`, reuses the existing `renderUsageTable()` refresh path, no extra fetch). Server-side `_build_buckets_for_sql` now emits per-bucket `tokens` (`in_cached + cache_write_tokens + in_uncached + out`) plus `by_model` (top-8 + `other`) and `by_provider` (top-10 + `other`) series via a fail-open `_bucket_series` helper (reuses the existing `{clause}/params` binding, folds the remainder into `other`, never returns raw rows). The frontend stacks colored segments by a global top-8 + `other` ranking (stable colors across buckets) with per-segment tooltips and a matching swatch legend. Backward-compat keys (`requests`/`cost`/`label`/`start`/`end`) unchanged.

### Fixed

- **Terminal relay walls no longer re-loop or spam the console** — CC Max's `upstream_safety_blocked` and GLM/Qwen's `data_inspection_failed` return HTTP 400, but `400` was in `_RECOVERABLE`, so the combo fallback chain advanced to a sibling model in the *same* walled family and the never-stop retry wrap re-looped indefinitely (`[400] … upstream_safety_blocked` every ~15s). A new body-aware detector `_is_terminal_relay_wall(status, err_text)` (status-gated to 400/403, case-insensitive marker match, fully fail-open) now short-circuits the wall at **every** egress path: OpenAI raw stream, Anthropic egress, Anthropic→OpenAI egress, the Gemini combo funnel, the non-streaming combo retry, the 400→502 reclassification, and the single `_raise_combo_wrap` never-stop chokepoint. The wall surfaces **once** with its real status so the client unblocks immediately. `_RECOVERABLE` contents and the `combo_infinite_retry` default are unchanged — generic recoverable 400s (e.g. `unknown field system`) still advance the chain. 19/19 terminal-wall tests pass; full suite green.
- **Consumption chart token mode now plots real tokens, not request counts** — the Y-axis previously read `bucket.requests` (flat low ceiling, e.g. 0–72) while the header said "Tokens". It now uses the new per-bucket `tokens` aggregate (with a `requests` fallback for legacy payloads), and `maxVal` is derived from the correct metric so gridlines and bar heights match the header unit.
- **Consumption chart x-axis is now chronological (oldest → newest, left → right)** — `_build_buckets_for_sql` emitted buckets newest-first, so the timeline rendered reversed (e.g. `22:00 … 00:00 … 23:00` with the newest hour mid-axis). Buckets now emit oldest→newest; a regression test asserts ascending `start` across all six timeframes.

---

## [1.0.6] - 2026-09-24

9 commits since 1.0.5 — a robustness + research-tooling wave: GLM tool-call repair-first + a 5th tool-call dialect parser, StepFun step-5 reasoning contract, FEL intent-bound engagement framing, and per-provider zero-retention controls.

### Added

- **FEL intent-bound engagement framing (`reframe.bind_engagement`)** — binds the operator's configured engagement profile *into* the reframed task text (e.g. `Engagement: Commissioned security research and vendor tooling by <org> | scope: ... | ref: ...`), so deep-guardrail model families that read *intent* (Claude, DeepSeek-pro) accept honest authorized-research framing instead of refusing. Strictly honest: it surfaces only the operator-configured profile and never fabricates context (empty profile ⇒ NO-OP fallback to the prior reframe path). 84/84 FEL tests pass.
- **Per-provider `retention_headers` config key** — arbitrary zero-retention headers injected by `_inject_provider_headers` for *any* provider without needing a new header profile. Fail-open: malformed/non-dict values are skipped silently and never block a request; non-string values are str-cast; empty keys/values skipped. Honest scope documented in-code: OpenAI Responses ⇒ `store:false`; Anthropic/Google paid API ⇒ no-training-by-default (policy, no header exists); custom gateways ⇒ their proprietary header here. 16/16 header-profile tests pass.
- **StepFun step-5 reasoning contract** — new compat family (`app/compat/families/stepfun.py`) with the reasoning contract, a UI reasoning-effort gate, and an SSE token-precision fold.
- **GLM 5th tool-call dialect parser** — recovers the ChatML `<tool_use><invoke name=X><parameter name=k>v</parameter></invoke></tool_use>` shape that the four existing parsers all miss (the recurring GLM tool-batch drop bug via yet another wire shape). Wired into the multi-parser ladder so both the buffered normalize path and the streaming rescue path recover it; fail-open on malformed input.

### Fixed

- **GLM tool-batch drop syndrome (repair-first protocol)** — `app/middleware/tool_arg_repair.py` repairs malformed tool-call argument blocks before parse instead of dropping the batch.
- **Post-audit hardening wave** — AntiStop mid-stream continuation, Response Guard (log_only) for injected second-person imperatives, in-window `view_file` dedup, and GLM tool-batch repair v2.
- **Antigravity `thought_signature` response path** — the OpenAI-SSE → Anthropic-SSE converter now preserves `thought_signature` on `tool_use` blocks so multi-turn tool conversations stop being rejected by Google with `400 INVALID_ARGUMENT`.
- **Dashboard version/update UX** — version badge now has the `id` the updater script targets (so `checkForUpdate()` can refresh it), and the update check is tag-aware so tag-only releases are reported.

---

## [1.0.5] - 2026-09-21

14 commits since 1.0.4 — a provider-integration and observability wave: CommandCode alpha transport with `thought_signature` preservation, FEL stream observability phase 1, OAuth/usage hardening, and a security-bumped pinned stack (fastapi 0.129.1 / starlette 0.52.1 / python-multipart 0.0.32). **Python 3.10 support is dropped**: fastapi 0.129.1 pulls `typing-inspection>=0.4.2` (requiring `typing-extensions>=4.12.0`) while mitmproxy 11.0.2 caps `typing-extensions<=4.11.0` on python<3.11; the CI matrix is now 3.11/3.12.

Post-tag wave (2026-09-22, folded into 1.0.5): the Antigravity `thought_signature` response-path fix — the OpenAI-SSE → Anthropic-SSE converter emitted `tool_use` blocks carrying no signature, so clients stored unsigned tool calls and echoed them back unsigned, and Google rejected the history with `400 INVALID_ARGUMENT` on every multi-turn tool conversation through `antigravity/gemini-pro-agent`. A variant of the same fix inside shadow-only `normalizer_v2` was reverted (egress/ingress asymmetry broke the shadow round-trip comparator) and deferred to a separately scoped task.

Post-tag wave (2026-09-23, folded into 1.0.5): a 5th GLM tool-call dialect parser — production capture (chat2api guest lane, `zai-web2/glm-5.3-flash-max`) showed GLM emitting tool calls in a ChatML dialect (`<tool_use><invoke name=X><parameter name=k>v</parameter></invoke></tool_use>`) that the four existing parsers (unicode `｜tool▁calls｜`, `<tool_call>` XML, DeepSeek inner markers, pseudo-XML `<name>/<arguments>`) all miss. Because no parser matched, `normalize_glm_tool_calls` left `tool_calls` empty and the whole batch was dropped — the recurring GLM-5.3 tool-batch drop bug surfacing through yet another wire shape. Added a `_TOOL_USE_INVOKE_RE` outer-wrapper matcher (accepts `<tool_use>` or `<tool_call>`, quoted/unquoted attrs) plus `_calls_from_invoke_block`, which recovers every `<invoke>` in a block (so a 2+ invoke batch keeps all members), merges `<parameter>` children into one JSON object argument, coerces parameter values that are valid JSON to native types, skips invokes with no usable name, and stays fail-open on malformed input. Wired into the multi-parser ladder (`_parse_tool_call_block_multi`, strategy 5) so both the buffered normalize path and the streaming rescue path (`parse_streamed_tool_block`) recover the dialect; `normalize_glm_tool_calls` also finds and strips bare `<tool_use>` wrappers that the earlier finders miss. Tests +16 in `test_glm_block_batch_drop.py` (live capture, 2-invoke batch, bare invoke, valid-JSON parameter, unquoted/quoted attributes, malformed fail-open, streaming rescue, end-to-end normalize) plus a `tool_use_invoke` label on the existing parametrized batch-member test.

### Added

- **CommandCode alpha transport** — `thought_signature` preserved through the egress path; CommandCode provider endpoint moved to `/provider/v1` (auth-wall verified); envelope repair + load-time connection dedup.
- **FEL stream observability phase 1** — CBNF orphan guard + `aclose` leak fix alongside the phase-1 stream observability hooks.
- **Antigravity native-OAuth bypass for unmapped `/api/test-model` slots** — the antigravity probe speaks the Cloud Code wire (`v1internal:streamGenerateContent`).
- **Provider-injection Response Guard (`tools.response_guard`, default OFF)** — opt-in detector for injected second-person imperatives aimed at the user (exfiltration / code-execution instructions) inside assembled provider responses, covering the streaming path (an observer wraps the chunk stream and yields every chunk unchanged) and the non-stream path (a scan of the assembled response JSON). Ships in `log_only` mode only: emits one `guard_event` telemetry record per response to `.brain/logs/response_guard.jsonl` and never mutates content; every scan is fail-open, and a disabled or absent config costs a single dict lookup. Placeholder documented in `config.example.yaml`.
- **In-window `view_file` dedup** — both Antigravity ingress sites strip redundant `view_file` tool pairs whose path was already read earlier in the same request while still inside the model's context window, running before the OpenAI conversion so the Gemini-side FIFO id-minter stays in sync. Paired removal is atomic and fail-open; a `[DEDUP]` line reports pairs removed and estimated bytes saved.

### Fixed

- **Tag-aware dashboard update check** — `latestVersion` now resolves as the max semver of GitHub releases **and** tags, so a tag-only post-tag-wave release (v1.0.5 shipped with no Release object) is reported correctly; previously `/releases/latest` alone returned 1.0.4 and the upgrade pill stayed suppressed for 1.0.4 users. Applied to both `/api/version/check` and the `/api/check-update` twin via one shared resolver; the unauthenticated tags fetch fails open (release-only behavior) and non-semver tags (`pre-extraction-snapshot`, pre-releases) are ignored.
- **Antigravity `thought_signature` lost on the response path (Gemini 3.1-Pro `400`)** — `stream_normalizer._tool_events` (the OpenAI-SSE → Anthropic-SSE converter) emitted `content_block_start` with no `thought_signature` carrier, so the client stored an unsigned tool call, echoed it back unsigned on the next turn, and Google rejected the whole history with `INVALID_ARGUMENT` ("Function call is missing a thought_signature … position 4"). Ingress already read the key — it simply never received a value. Three-layer fix: (1) the streaming converter now latches the signature into `tool_blocks` and emits it on the Anthropic `tool_use` block, with the legacy `function_call` shim and the Gemini-native → OpenAI chunk path covered too; (2) `antigravity_upstream` reads the signature from both wire nesting levels into a bounded in-memory LRU cache (512 entries, TTL, two integer hit/miss counters — no log writer, AGENTS.md §2 compliant); (3) `openai_to_cloudcode_envelope` re-injects from that cache at egress, so clients or history compaction that strip the unknown key can no longer desynchronize the conversation. The non-streaming aggregator was verified unaffected (it passes `tool_calls` by reference). Tests 14 → 23 (SIG14/SIG15 series); full suite green on both CI legs.
- **GLM parallel tool-call batches silently dropped (two independent defects)** — a batch of N calls arrived at the IDE incomplete or entirely empty, worst on `glm-5.3`, milder on `claude-opus-4-6-antigravity`, and absent on `kimi`/`qwen`. Two separate code paths caused it, which is why the first fix appeared to do nothing. **(1) ASCII block parser capped at one call** — `glm_tools._parse_tool_call_block` returned `Optional[Dict]`, so a whole batch wrapped in a single `<tool_response>` block could never survive: a JSON array decoded to a `list`, failed the `isinstance(dict)` check, found no `<name>` tag, and returned `None`, so `normalize_glm_tool_calls` reported `changed=False` and the batch vanished **with no log line at all**; concatenated objects (`{…}{…}`) hit `Extra data`; and repeated pseudo-XML matched only the first pair via `re.search`. Replaced with a batch-capable `_parse_tool_call_block_multi` returning every member, plus a `raw_decode` splitter for concatenated objects and positional `findall` pairing for pseudo-XML; all three call sites (buffered normalize, streaming rescue, unicode fallback) now extend rather than append. **(2) accumulator index collapse** — `gemini.openai_chunk_to_gemini` keyed tool-call slots on `tc.get("index", 0)`; upstreams that omit `index` (GLM via MITM) resolved every call in a batch to key 0, concatenating N argument objects into one buffer that then failed `json.loads` with `Extra data` and was dropped, so a batch of 4 emitted 0 calls. Slots now key on `("i", index)` when an index is present, else `("s", seq)` allocated per distinct call id, with argument-only continuations attached to the most recently opened slot; homogeneous tuple keys keep the finish flush's `sorted()` safe. Both paths fail open and preserve ids for request/response correlation. Tests +44 (`test_glm_block_batch_drop.py`, `test_gemini_tool_index_collapse.py`).
- **7h-stale Usage dashboard on UTC+7 hosts** — `_safe_ts_epoch` now preserves the UTC offset.
- **Usage charts render the full-window summary** instead of a 500-row table page.
- **OAuth duplicate connections** — collapsed on save; all matching token rows refresh together.
- **Config case-variant state keys** normalized.
- **MintRouter direct-stream usage frames** fixed.
- **Revert** — empty-credential prune + OpenCode Zen free-tier terminal gate rolled back (2026-09-20) after live-fire issues.
- **Midstream transport death no longer force-stops a partially streamed response (502 transient)** — a transport error (`RemoteProtocolError` / peer reset / timeout) after tokens had already streamed (`out > 0`) hit the midstream guard, which deliberately declines combo failover at that point (splicing a second provider into a live parser corrupts the transcript), so the stream simply ended and the client saw a force-stop with no terminal frame. The death now sets `TRANSPORT_DIED_PARTIAL_FLAG`, and the post-loop AntiStop continuation splice — which resumes the same provider/model from the partial text already accumulated, with no live-parser splice — completes the response; `should_splice_continuation` (quality.py) keeps the existing opt-out contract (infinite retry disabled means no splice), and `out == 0` deaths still take the normal combo failover without setting the flag. Fail-safe: if the continuation request itself dies, the existing terminal-frame path runs unchanged.

### Changed

- **Pinned security stack** — fastapi 0.111.0→0.129.1, starlette 0.37.2→0.52.1, python-multipart 0.0.9→0.0.32; full suite re-validated on the Py3.11 and Py3.12 legs.
- **Python 3.10 dropped** — CI matrix `['3.10','3.11','3.12']` → `['3.11','3.12']`; README floors raised to 3.11+.

### Maintenance

- **AGENTS.md rewrite (2026-09-20)** — Gate 4 aligned to the verified 5-path gitignore invariant; repo quick-facts table added.

---

## [1.0.4] - 2026-09-08

79 commits since 1.0.3 — a stability flagship wave (fixes the capture-log stall that killed IDE sessions, WinError-64 accept-loop death, supervisor hardening) plus two new subsystems: the Faithful Execution Layer and Normalizer Hub v2. The legacy web-provider lane was fully extracted to the standalone Chat2API app and ships zero code here.

Post-tag wave (2026-09-11, folded into 1.0.4): live Usage-tab observability — a 2-second signature-gated poller (preserves expanded windows), an in-flight request registry rendered as a pulsing strip, and removal of the legacy usage recompute lane (the SQLite ledger is the sole source of truth). Late wave (2026-09-11): bare `reasoning` SSE-key accumulation fix for Mimo-style thinking models, truthful zombie-gate out_tokens, and opencode Zen identity headers.

### Added

- **Faithful Execution Layer (FEL)** — pure directive builder + live wiring for clarity preprocessing, per-wire-format egress directive merge, strict refusal classification, and single recovery re-dispatch. FEL-5 hard wall: CSAM/weapons-class refusals classify `blocked` (terminal, immune to recovery/research mode, checked before the 600-char gate). Bilingual EN/VI sensitivity reframe. Default-OFF (config-gated); 186 tests green. Files: `app/middleware/faithful_execution.py`, 4-stage wiring in `app/main.py`, admin UI FEL panel.
- **Normalizer Hub v2 (N-series)** — `normalizer_v2` registry with schema dialects (`gemini_last_role`, `tool_arg_repair`, `reasoning_policy`, `stream_normalizer`) and a shadow comparator with capped off-loop shadow logging (no hot-path back-pressure). Adopted in the main request path; 107 tests green.
- **Dual-stack listener** — IPv4+IPv6 accept ends ECONNREFUSED for localhost/::1 clients; bind-retry (10013/10048, up to 120s) and self-health watchdog in `dualstack_serve`; split-brain socket health probes (dual-stack either-OK) with supervised respawn.
- **B1 watchdog-supervisor** — self-healing respawn lineage; PS1 lifecycle audit fixes F2–F7; launcher idempotent-start health gate (unauthenticated `/health` on both stacks, no credential in the launcher).
- **Pre-restart smoke battery** — catalog/chat/stream/mapped-Gemini/admin checks for staging-first deploys.
- **Pre-commit mojibake scanner + residual scanner** — blocks future cp1252 double-encoded commits; 1,398 corrupted tokens repaired across 5 files, CHANGELOG purged, full audit PASS (577 fix-adjacent tests, zero source residual).
- **Pricing/detection** — GLM-5.3, GPT-6 Astra, Muse Spark, Hunyuan Hy3/Hy4 families.
- **Live Usage-tab observability** — 2-second signature-gated refresh (preserves expanded windows) plus an in-flight registry: a bounded `OrderedDict` (2,000 cap, 10-minute stale self-heal) exposed at `GET /api/observability/usage/inflight` and rendered as a pulsing strip in the Usage tab; records complete on both success and failure paths (no leaks).

### Fixed

- **Capture-log I/O stall that killed Antigravity IDE sessions** — inbound capture wrote full payloads synchronously on the FastAPI event loop with no cap (file reached 29.8 GB, each append stalled the loop minutes). Now an async drop-on-full queue writer (`asyncio.to_thread`), 50MB rotation on all three loggers, boot-time self-heal truncation, and metadata-only inbound capture by default (`BSL_CAPTURE_INBOUND=1` opts back in).
- **WinError 64 accept-loop death (F8)** — the accept loop re-arms AcceptEx after the error instead of dying; live-verified against real bursts.
- **Never-stop combo wrap** — 16 exhaustion sites now wrap to combo fallback instead of force-stopping; Gemini stream-start fallback ungated (final-entry 429 wraps).
- **Malformed request bodies** — OpenAI-style JSON 400 (was unhandled 500) across 5 inference endpoints.
- **GLM parallel tool guard** — `disable_parallel_tool_use` injection fixes GLM-5.x dropping tool arguments.
- **Stale-snapshot guard** — AEP/OAuth snapshot holders no longer wipe imported providers; provider delete persistence, OAuth dedup, dead-connection disable.
- **Empty-key auth fix** + antigravity thought-signature tests; NFKD transcode + VN preflight now covers the `agentrouter*` family.
- **MITM hardening** — kill-path WMI pre-scan with critical-process blocklist; DNS hijack bound to MITM liveness (boot reconcile / stop removal / start rollback); rogue respawn-supervisor tree-kill guard.
- **Hunyuan Hy4-preview reasoning controls**; variant-ID separator normalization (dash IDs → dotted canonical contracts); gpt-6-astra effort contract parity; Fable/Mythos 5.1 thinking contract (always-on adaptive + effort clamp, 456/456 lock suite).
- **Loopback base_urls warn-not-block; circuit-breaker stub → real coverage.**
- **Launcher idempotent-start health gate** — unauthenticated `/health` on both stacks, per-stack try/catch.
- **Usage tab stale during live streams** — rows previously landed only ~2s after stream completion; the tab now refreshes every 2 seconds while streams are active.
- **Bare `reasoning` SSE key (Mimo via Zen/OpenRouter dialect)** — reasoning-only streams assembled empty messages and hit the zombie 504, burning one combo-fallback entry per call; the bare `reasoning` delta key is now accepted at all four extraction sites (TTFT detection, SSE accumulator, output classifier, non-stream relay) and folded into `reasoning_content`. Zombie-gate forensics hardened: the 504 now reports the real billed `out_tokens` (was hardcoded 0 — the live case billed out=5/in=403 with zero deltas relayed); a billed-but-empty 200 stays a 504 so combo fallback advances. Regression: `test_openai_bare_reasoning_key_accumulation` + `test_response_has_model_output_billed_but_empty_is_zombie`.

- **CI red since Aug 13 — root-caused and fixed** — GitHub Actions has never been green (the failure emails were never about recent pushes). Two stacked causes: (1) `requirements.txt` floors (`fastapi>=0.115.0`) let CI resolve FastAPI 0.141/Starlette 1.6 while the whole suite is validated on FastAPI 0.111/Starlette 0.37/httpx 0.27, and the new `_IncludedRouter` wrapper (no `.path`) crashed the CCPA gateway route-ordering test at iteration; (2) `config.yaml` is gitignored (live credentials), so every fresh CI runner lacked it and 11 lifespan-booting tests died with `FileNotFoundError` — masked on dev machines by their existing config and masked on CI by `-x` stopping at the first failure. Fix: runtime deps pinned to the validated production stack (`dnspython`/`openpyxl` finally declared), new `requirements-dev.txt` adds `pytest-timeout` (restores the `pytest.ini` 10s timeout net), route iteration is version-robust, a session-scoped conftest fixture seeds `config.yaml` from the tracked example when absent (removed after the session; a real developer config is never touched), and CI installs dev deps with `checkout@v5`/`setup-python@v6` (Node-20 deprecation cleared).

### Changed

- **Web-provider lane extracted** — all web providers (~35 commits, GLM/Kimi/Qwen web, OAuth UI) reverted in `ca5c5c8` (−7,400 lines) and preserved in the standalone **Chat2API** app. Zero web provider code ships in 1.0.4; the `nodriver` dependency pin is removed.
- **Key health dimming** in the admin UI; reasoning controls for new model families.
- **Version alignment** — `VERSION` file, dashboard version pill, and GitHub tag all read 1.0.4; the update notification under the logo fires via the live GitHub latest-release check when the remote version is newer than local.
- **SQLite ledger is the sole usage source of truth** — `recompute_usage_costs`, `invalidate_recompute_cache`, `usage_stats_shim`, and `_UsageListShim` deleted; costs materialize at write time with no reprice-on-read path.

### Maintenance

- **Mojibake purge** — 1,398 cp1252 double-encoded tokens repaired across 5 files (gated, idempotent fixer); CHANGELOG 161-runs purged; pre-commit hook + residual scanner now blocks future mojibake commits. Full audit PASS (577 fix-adjacent tests, zero source residual).
- **Dead code removal** — 24 provably dead symbols (−450 lines).
- **`.brain` runtime debris + live-token artifacts gitignored**; hidden background-spawn console (no more empty cmd popups).
- **Legacy-absence test guards** — perf suite rewritten with guards against the recompute lane returning; 34/34 inflight+perf and 195/195 downstream green.

## [1.0.3] - 2026-08-26

Post-tag wave (folded into the release): Kiro binary event-stream egress, multi-key loss guards (orphan-key coverage), empty-content-block egress repair, live GitHub update check for the version pill, and an anchored compaction skip-regex that un-excludes GLM wire-format models.

77 commits since 1.0.2 — combo-chain resilience overhaul (incl. never-stop retry), live quota indicators, official thinking-parameter parity across model families, the Ox Alpha contract, plus a post-tag wave: zero-network Kiro imports, `blacksand-agentic-ultra` orchestration, multi-key loss guards, and MITM supervisor cleanup.

### Added

- **Live per-key quota remaining** — one-api/new-api billing probes report remaining quota per API key (browser-UA variant for Cloudflare-fronted gateways), surfaced as compact % bars inline with each key's status row in the dashboard. Percentage only — no dollar values.
- **Passive OAuth quota indicator** — rate-limit response headers are captured and rendered as quota bars for all OAuth accounts (merged with endpoint quota data).
- **Ox Alpha reasoning-effort contract** — new family contract for Ox Alpha / `x-preview-f-free` (community-fingerprinted GLM-5.3, reseller-served): 4-word effort vocabulary `low/medium/high/max` (default `max`), `xhigh`→`max`, budget-string coercion, top-level `reasoning_effort`, Anthropic-wire strip, and structured 1210 error markers so an invalid effort triggers one degrade-and-retry instead of a terminal 400. Live-verified against opencode-zen.
- **Anti-freeze controls** — Settings tab: stream kill button, live stream badge, auto-restart watchdog.
- **Provider header profiles** — per-provider strict client-identity headers (`default`/`codex`/`claude_code`/`custom`), applied on requests and key verification; single dropdown control in the admin modal.
- **Usage history in SQLite** — persisted usage store (100k-record retention) replacing JSONL, one-shot migration guard, startup init.
- **Tier-2 pool inbound Ed25519 verifier** — signature-verified pool auth (body-hash binding, fail-closed config).
- **Codex Responses-API egress adapter** — `app/codex_adapter.py` translates OpenAI chat payloads to/from Codex's `/responses` endpoint (fixes the Cloudflare HTML 403 on `/chat/completions`): forces `store:false`+`stream:true`, normalizes effort (minimal→low, max→xhigh, default medium), strips unsupported params, converts Responses SSE → OpenAI chunk frames.
- **Kiro auto-import + profileArn** — lazy import of Kiro connections from the AWS SSO cache on first request; `profileArn` top-level injection (fixes 400 "profileArn is required"); refresh routing split social (kiro.dev) vs OIDC/builder-id with UUID clientId (AWS OIDC).
- **Fuzzy model-ID normalization** — dash/order-insensitive last-resort resolution (`gpt-5-6-terra` / `gpt-terra-5-6` → `gpt-5.6-terra`) in the chat ladder, images endpoint, and model-test path; exact matches are never rewritten.
- **Kiro zero-network IDE-cache import** — new `/kiro/import-ide` endpoint imports connections from the local AWS SSO cache without a single network call at import time (immune to Kiro anti-abuse rate flags); recommended button in the provider modal.
- **Kiro SSO auto-detect** — the Kiro provider modal probes the IDE SSO cache and offers a one-click "session detected" connect (manual key/device modes remain as fallback).
- **`blacksand-agentic-ultra` full orchestration** — runs the complete balanced-mode loop (7 phase templates, lead+1 member on commit phases, 22-message cap, substance gate, synthesis step, aggregated usage), ported from Blacksand Code.

### Changed

- **Thinking-contract official parity wave** — per-vendor reasoning vocabularies rebuilt from official docs: GLM-5.2 (none/minimal→disabled) and GLM-5.3 (3-word vocab, mandatory thinking, 1210 fixes), Grok-4.5/4.6 (xhigh gating, penalty/stop sanitize), Qwen 3.8 (official sampling defaults, published enum only), Hunyuan Hy3 (chat_template_kwargs nesting + sampling defaults), Muse Spark 1.1/1.2 wire split, numeric-version routing.
- **Deterministic multi-key selection** — `_pick_connection()` replaces `random.choice`: top-first by default, real `round_robin` toggle per provider, rotation reset on config save.
- **Circuit breaker default ON** — 429'd keys rotate out of the pool automatically.
- **MITM parse caching** — config.yaml parse cached by mtime (CSafeLoader), cutting per-connection auth latency.
- **Observability** — logs newest-first, Logs/Usage tab pagination, throttled cost recompute, capped DOM rows; usage logged on all non-stream 200 egress paths.

### Fixed

- **Combo continuous fallback** — chains now cycle through retry passes instead of hard-stopping after one pass ("All N combo chain entries exhausted"). `expand_chain_for_retries()` sizes passes by the widest key pool, clamped (max 6 passes / 24 attempts), bounded by a request-wide wall clock.
- **Never-stop combo retry** — chain exhaustion is now a pass boundary, not a terminal. When every expanded entry fails (including wall-budget exhaustion), the chain wraps to entry 0 with cleared key-failover state and exponential backoff (2s→4s→8s→16s→30s cap), then keeps retrying until the client disconnects — the only permitted terminator. `return await` recursion adds no stack frames; the wall clock re-arms each pass. `settings.combo_infinite_retry: false` restores the terminal 502.
- **Chain-sized wall budget** — the retry wall clock now scales with chain length (`max(240s, entries × 130s)`), so slow Cloudflare-524 leaves (~125s each) can no longer strand later entries (the Opus-Tabitoken force-stop). Flat 240s budget retired.
- **CHAIN_TOTAL_BUDGET 150s → 960s** — deadline-stall fires can always advance the chain.
- **Per-entry combo budget reset (BUG K)** — stops chain starvation on header-timeout/midstream fallback.
- **Zero-renderable-output streams** — advance the combo chain and drain gracefully on restart instead of returning an empty/None response; v3 finish frames emit empty text (not None); no-renderable-output force-stop and a JSONResponse TypeError eliminated.
- **Reasoning-only pre-render buffer (BUG N)** — pre-content thought frames are held without committing emission, keeping combo fallback legal when a reasoning stream dies mid-transport.
- **Test-endpoint app-kill** — `/api/test-model` probes run under a 2-slot semaphore + 75s timeout; the dashboard also refuses overlapping tests. Spamming Test can no longer saturate the event loop and kill the app.
- **Kilocode tools[0].type 400** + spurious network-disconnect on slow reasoning.
- **Antigravity Cloud Code envelope forging** — combo upstream calls (Vision/compaction) no longer 404.
- **Gemini egress** — visible no-output notice, fileData passthrough, gate exclusion.
- **AgentRouter Vietnamese content** — NFKD transcode replaces the hard block (precomposed codepoints were the only 400 trigger).
- **Multi-key failover** — request-scoped `tried_conns`/`exclude_indexes` so retries dial the NEXT key instead of re-dialing the drained one; still honors `connection_indexes` authorization, breaker, and round-robin.
- **Kiro request schema** — corrected from captured ground truth (`inferenceConfig` top-level, `modelId` inside `userInputMessage`, `chatTriggerType`/`conversationId`/`origin`) — fixes `REQUEST_BODY_INVALID`.
- **OAuth connection persistence** — via the sanctioned config-state swap path (was reading the deleted `main.config` global).
- **Recoverability reclassification** — transport-level timeouts/500/502 route through combo fallback (Py3.10 builtin TimeoutError escape); 4xx treated as recoverable where appropriate; insufficient_user_quota classified as auth; artifacts 500 and gemini-3.6 slots fixed.
- **Edit-Provider modal key clobber** — a blank key field now keeps the existing key; a different key appends a new connection (parity with "+ Add API Key"); fixes silent key loss on multi-key providers (Tabitoken/Gorouter/Seekai).
- **Two-tab stale-save connection loss** — lost-update saves can no longer drop connections edited in another tab; merge guard with `_deleted_connection` opt-out for intentional deletes.
- **Codex 400 `Unsupported parameter: messages`** — intent-driven payload injection no longer runs on Codex Responses payloads; the fold helper folds content into `instructions` instead of creating a `messages` key.
- **Kiro import route shadowing** — the dedicated `/kiro/import` route is registered above the generic `/{provider}/import` (FastAPI match order), so Kiro imports reach the Kiro handler.
- **MITM respawn-supervisor guard** — `force_kill_mitm_port` now walks each listener's parent chain (WMI/CIM) and tree-kills respawn supervisors (`while($true){mitmdump}` PowerShell loops) BEFORE the listener kill loop, so a rogue respawner can no longer defeat port cleanup and cascade 503s; PID 0/4/self are protected.
- **Dead-account 400s now softban** — deterministic upstream account rejects ("Action plan limited", "user is not allowed to access") are classified as `auth`, triggering an immediate 90s cooldown and combo-chain skip; previously the dead leaf was re-selected as chain primary on every request (~5s wasted per call).
- **GPT-5.6 ultra effort honesty** — `ultra` effort coerced to `max` with honest UI/comment (ultra is Codex multi-agent orchestration, not a wire parameter).
- **Ox Alpha thinking dropdown** — effort selector on the model row; `/api/test-model` concurrency 2→3.

## [1.0.2] - 2026-08-15

### Fixed

- **TOOL-META: Antigravity IDE tool validation error** — Upstream models (DeepSeek, Qwen, GLM, Kimi) don't generate `toolSummary`/`toolAction` fields required by the Antigravity IDE. New `_inject_tool_metadata()` in `app/compat/adapters/gemini.py` injects defaults via `setdefault` at both functionCall emission sites. Live-verified on both 6969 and 6970.

- **ZOMBIE: Reasoning-only responses blocking combo fallback** — `_response_has_model_output()` in `app/main.py` treated `reasoning_content`/`reasoning` fields as usable output. When a reasoning model produced ONLY thinking tokens with empty `content`, combo fallback was skipped and the user received an empty response. Rewrote to only count `content` as visible output. Added Anthropic-format support (`content[].text` / `content[].type == "tool_use"`).

- **VISION-FAILOPEN: Vision scout failure blocking all responses** — When all vision candidates failed for an image, `VisionPolyfillFailed` was raised and `main.py` returned a 502, blocking the ENTIRE response. `vision.py` now substitutes `PLACEHOLDER_UNREADABLE` and lets the request continue. The `VisionPolyfillFailed` handler is kept only for the total-budget timeout case (504).

- **VISION-ANTHROPIC: Vision scout now supports Anthropic-format providers** — Vision scout only spoke OpenAI multimodal format. Anthropic-format providers (ltn-ai, a6api) were silently skipped. Added `"anthropic"` to `_VISION_SUPPORTED_FORMATS`, new `_build_vision_payload_anthropic()` builder, and format auto-detection in `_describe_image_once()`.

- **OBS-PRICING: Observability pricing registry merge + log ordering** — `_load_pricing_registry()` only loaded the seeded official registry, ignoring the detected pricing file. Logs endpoint returned oldest-first instead of newest-first. Fixed: merges both registry sources with null-price fill-in, added `invalidate_recompute_cache()` after pricing detection, logs endpoint now returns newest-first.

- **OAUTH: Missing client_id causing 400 "invalid_request" on all OAuth providers** — The `authorize` endpoint allowed empty/unset `client_id` strings to pass through to providers, triggering provider-side 400 errors. Fixed: (1) Added `_missing_client_id_hint()` helper with provider-aware error messages. (2) Generalized the `authorize()` validation to use it for all `authorization_code` flows. (3) Added `clientId` validation to the `device_code()` endpoint for providers with static client IDs (skipped `kiro` via `dynamicClientId` flag). (4) Fixed `_prepare_provider_config()` consistency in `device_code()` and `poll()` — both now use the prepared config instead of raw `entry["config"]`. (5) Removed debug print statements from `app/oauth.py`.

- **Debug print cleanup** — Removed `[ZOMBIE-DEBUG]`, `[DEBUG:{_label}]`, and `[Kiro Debug]` print statements from `app/main.py` that would spam production output with per-request forensic logs.

---

## [1.0.1] - 2026-08-14

### Changed
- **STREAM-GUARD first-bytes diagnostics** — when the guard refuses a
  post-emission fallback (e.g. the `GPT-5.6-SOL` midstream transport death
  that returned 502 after 11204B with `out: 0`), the refusal log now includes
  a capped 256B sample of the FIRST bytes that were emitted. This answers
  whether the veto was justified (reasoning scaffolding vs user-visible
  content) without weakening the no-second-stream invariant. Veto behavior
  itself is unchanged — this is forensics, not a policy change.

### Fixed
- **GPT-5.6-SOL midstream 502 (reasoning-only streams)** — when a vision/reasoning
  model (DeepSeek V4, MiniMax M3 via `qwencoder/gpt-5.6-sol`) produced ONLY
  `thought:true` frames before the leaf died mid-transport, the emission gate
  treated reasoning-pane text as committed content, vetoed combo fallback, and
  the IDE froze on a dead stream. The Gemini egress now holds pre-content
  thought frames in a capped buffer (256 KiB) without marking emission, so a
  transport death/stall can still fail over to the next combo entry. The first
  visible body-content frame flushes the buffer in order and commits, exactly
  as before. Usage-only/finish-only scaffolding passes through uncommitted.
  New classifier `gemini_frame_is_thought_only` in `app/compat/adapters/gemini.py`;
  regression suite `app/tests/test_thought_buffer_prender.py` (19 tests).
- **Logs & Usage tabs loading in 2-3 minutes** — the Usage endpoint re-read
  `config.yaml` + the pricing registry from disk and recomputed costs for all
  ~10k entries on every tab load; both endpoints serialized the full 10k-entry
  lists synchronously; and the frontend rendered every row in one DOM write
  with per-keystroke search re-renders. Fix: `?limit=&offset=` pagination
  (default 500, clamp 1..2000) returning `{total, entries, has_more}`; cost
  recompute throttled to a 60s TTL + registry-mtime guard; frontend caps DOM
  rows at 500 with a "Load 500 more" button and debounces the search input
  (300ms); `X-Total-Count` header lets the 2s live-poller skip body parsing
  when nothing changed. 15 new tests in `test_observability_perf.py`.
- **Vision Scout 502 timeout loop** — the vision polyfill could exhaust its
  total wall-clock budget before every fallback candidate got a turn
  (`Vision unavailable: vision description exceeded the 120.0s budget`).
  Root cause: a 60s per-attempt timeout meant only 2 of 4 candidates fit in
  the 120s budget. Lowered the per-attempt timeout to 15s and the total
  budget to 65s so all 4 candidates get a full attempt (4 × 15s = 60s plus
  margin) while still bounding how long the IDE can stall.
- **Self-signed certificate upstreams** — added per-provider `ssl_verify:
  false` support so connections to upstreams with self-signed certificates
  no longer fail with `SSL: CERTIFICATE_VERIFY_FAILED`.
- **Auth pipeline latency** — decrypted config is now cached in
  `config_state` instead of being re-decrypted on every request.
- **Circuit breaker accuracy** — billing and authentication errors are now
  marked distinctly so the breaker does not misclassify them as provider
  outages.

### Security
- Purged sensitive config snapshots from Git history and gitignored
  `.brain/` state directories and config backups to prevent key leakage.
- Hardened `export-public.ps1` against exporting `config.backup.*` files
  and root-level scratch scripts.

---

## [1.0.0] - 2026-08-09

### Added
- Initial public release
- Multi-provider routing with a 5-tier pipeline (Chat, Lite, Agentic, Ultra, Max)
- Protocol translation between OpenAI, Anthropic, and Gemini formats
- Combo/chain fallback with deadline-based budget control
- MITM proxy integration via mitmproxy, with watchdog and auto-restart
- OAuth 2.0 support for Google, GitHub, Anthropic, and custom providers
- Stream normalization across all provider formats
- Web admin UI with real-time monitoring
- Cloudflare Tunnel and Tailscale support
- Circuit breaker for failing provider connections
- JSONL-based observability logging
- Vision scout for image analysis, with polyfill for non-vision providers
- Document parser for RAG
- Auto-update from GitHub releases
- Model pricing detection

### Security
- Fernet symmetric encryption for API keys and OAuth tokens at rest
- Machine-bound encryption key, so a stolen config file is unusable elsewhere
- Admin session authentication with automatic expiry
- On-demand security scanner with eight configuration checks
- Atomic config writes to prevent corruption on interrupted saves

### Fixed
- `NameError` in the `UnicodeEncodeError` handler when the chain budget was not
  yet defined
- Hardcoded absolute paths in `bslrouter.ps1` and `ninerouter_isolation.ps1`
  that prevented the scripts from running outside the author's machine; paths
  are now auto-detected with environment variable overrides
- Bare `FileNotFoundError` on a fresh clone with no `config.yaml`; startup now
  prints actionable setup instructions
- Empty or malformed `config.yaml` producing a confusing `AttributeError`
  downstream; these now fail fast with a clear message
- Alarming `[CRYPTO] Decryption failed:` output with an empty reason on first
  run; unfilled example placeholders are now recognised, and genuine failures
  report the exception type plus remediation steps
- 15 unused imports across 14 files (F401/F811 lint cleanup)
- Redundant `quote_plus` redefinition in `oauth.py`

### Fixed — post-tag wave (2026-08-26)
- **New API keys never used** (fallback + round-robin): per-model `connection_indexes`
  froze at save time — providers with appended keys (seekai, orcarouter, opencode-zen,
  llm7-io, kilocode) could never reach the newest key. Load-time orphan-key pass now
  extends every model's allow-list with enabled indexes beyond its max, and both UI
  key-append sites update model indexes on save.
- **`model output must contain either output text or tool calls`** on Anthropic-protocol
  clients (agentrouter/gorouter/seekai max-thinking models): upstreams returned empty
  `content` with the full output in `reasoning_content`, which both egress converters
  dropped. Non-stream responses now fall back to `reasoning_content`; streaming
  accumulates it and emits a text block at close.
- **Kiro binary event-stream egress** — `vnd.amazon.eventstream` 200s decoded via a
  dedicated decoder + `_SyntheticResponse` shim before the zombie guard; Kiro excluded
  from the text-SSE stream buffer; stale IDE caches refresh once at import and preserve
  `authMethod`/`clientId`/`clientSecret`/`region`; `TokenType: EXTERNAL_IDP` gated to
  IdC auth methods (403 fix).
- **Version pill never appearing** — `/api/version/check` hardcoded `hasUpdate:false`;
  now performs a live GitHub releases check (default `dongden-alt/BSL-Router`,
  overridable via `update.github_repo`) with a 5-minute cache; stale `bsl-router`
  default slug corrected.
- **Compaction never firing** — the skip-regex substring-matched `anthropic`, silently
  excluding `glm-5.3-anthropic`-style wire-format models (the exact models compaction
  targets). Regex now anchors on the leading family token; a startup eligibility line
  (`[Compaction] eligibility: X/Y`) makes coverage observable.

---

# 🇻🇳 Tiếng Việt

---

> Xem phần tóm tắt song ngữ đầy đủ của bản phát hành lớn **[1.1.0] - 2026-10-23** ở đầu file ([🇻🇳 Tiếng Việt — Tính Năng Mới & Sửa Lỗi Trong v1.1.0](#-tiếng-việt--tính-năng-mới--sửa-lỗi-trong-v110)).

---

## [1.0.9] - 2026-09-26

### Đã Sửa

- **Antigravity Integration: mapped model không "dính", tự quay về sau khi F5 (target Blacksand bị thả lặng lẽ)** — dropdown Antigravity Integration trong admin UI (`agOptions()` ở `app/static/app.js`) cho phép chọn 3 nguồn target: combo alias, **BSL Models** (ID thuần của nhóm Blacksand như `blacksand-agentic`, `blacksand-chat`, `blacksand-agentic-ultra`, `blacksand-agentic-max`), và `provider/model`. Validator lúc lưu `_is_known_antigravity_mapping_target` chỉ nhận combo alias và `provider/model`, nên khi map một slot sang **BSL Model**, mapping bị thả lặng lẽ trong `POST /api/config` và quay về "Native Antigravity inference" sau khi tải lại trang — dù toast vẫn báo "Mapped …". Đã kiểm chứng live bằng round-trip có đối chứng: target combo-alias sống sót sau save+reload còn target `blacksand-agentic` bị thả. Fix: `_is_known_antigravity_mapping_target` giờ chuẩn hóa target qua cùng bảng `_BLACKSAND_MODEL_ALIASES` mà runtime dispatch dùng và chấp nhận ID nhóm Blacksand (kể cả dạng `bsl-*`/`BSL-*`). Các ID này vốn đã routable lúc inference (`_BLACKSAND_MODEL_ALIASES` + các nhánh `_bsl_*_dispatch`); lỗ hổng chỉ nằm ở validator lúc lưu. 34/34 test antigravity pass (1 regression test mới khẳng định cả 5 ID nhóm Blacksand được chấp nhận); 4-gate pre-push check pass.

---

## [1.0.8] - 2026-09-24

### Đã Sửa

- **Lỗi 400 `thought_signature` trên tool-call Antigravity / Gemini — gia cố cache-miss + fallback re-sign bất chấp name/id** — Antigravity IDE echo lại các tool call của turn trước MÀ KHÔNG kèm `thoughtSignature` inline mà Google bắt buộc trên `functionCall`, khiến Gemini trả 400. Signature cache (`app/compat/adapters/antigravity_upstream.py`) có 3 lỗ hổng chồng nhau: (1) primary lookup key gắn với id volatile `call_{name}_{counter}` mỗi turn VÀ `name` của tool — cả hai đều đổi qua các turn (IDE thay bằng placeholder kiểu `default_api:invalid`); (2) TTL chỉ 900s; (3) eviction dùng `time.monotonic()` — lệch wall-clock sau suspend/resume. Fix: tách `_args_digest` dùng chung (sha256 canonical sort_keys của tool args) để store-side và lookup-side luôn khớp; nâng `_SIGNATURE_CACHE_TTL` 900s → 3600s và đổi clock sang `time.time()`; thêm `_signature_cache_lookup_fallback(model, args)` — match last-resort **bất chấp name/id** theo `(model, args_digest)`, chỉ trả signature khi có đúng MỘT signature sống khớp (mơ hồ → None, không bao giờ gán sai chữ ký); và thêm diagnostic một lần `_warn_unsigned_once` khi `functionCall` sắp ra đi unsigned (đúng phần Google 400). Kiểm chứng live: `gemini-pro-agent` và `gemini-3.6-flash-high` tool-call nhiều turn (turn 2 echo kết quả đã strip signature) được re-sign từ fallback → Google 200, không 400. 27/27 test thought_signature pass (4 regression test mới).
- **502 `midstream_transport` trên stream GLM/reasoning dài — đã trace; xác nhận đã xử lý, gốc lỗi phía upstream** — 935 lần trong `app.out.log`, phần lớn là `midstream_transport: peer closed` (385 chính xác) khi provider upstream (vsllm / a6api / seekai / zhipu) đóng kết nối HTTP/2 giữa stream sau ~120–160s trên các generation GLM-5.3 `effort:max` dài. `_midstream_transport_fallback` (BUG J) của router đã chuẩn hóa tình huống này vào rail combo-failover: `out==0` → advance sạch sang entry kế trong chain (client không thấy); `out>0` → AntiStop continuation splice (`app/middleware/quality.py::should_splice_continuation`, live ở 3 stream site) resume CÙNG provider/model từ phần text đã có thay vì làm hỏng transcript bằng provider thứ hai. Chỉ 5/935 (~0.5%) leo thang thành 502 client thấy — đó là khi chain đã cạn VÀ content đã bắt đầu nên không còn failover an toàn. Phần dư là đặc tính capacity/timeout của upstream, không phải defect router; giảm thiểu ở phía config, không cần đổi code.

---

## [1.0.7] - 2026-09-24

Đợt gia cố độ tin cậy + tooling nghiên cứu: các tường chặn relay terminal (`upstream_safety_blocked`, `data_inspection_failed`) nay được nhận diện là **khóa cứng phía provider** và dừng ngay tại nguồn thay vì retry vô hạn — kèm **FEL context-wide reframe failure-aware (Tier-2)** để payload hội thoại lớn bị GLM/Qwen input inspection chặn được reframe toàn bộ context + retry đúng MỘT lần trước khi kết luận tường terminal.

### Thêm Mới

- **FEL context-wide reframe (Tier-2, failure-aware)** — opt-in `reframe.context_scope: "full"` reframe TOÀN BỘ payload (user history + assistant history + tool outputs) qua cùng span-rewriter sensitivity-map song ngữ đã kiểm chứng (`_reframe_span_text`, tách từ `reframe_text` — hành vi last-turn giữ nguyên byte-identical, có regression test pin). Không động vào system prompt, last user turn, hay non-text parts (ảnh/base64); caps (`context_max_messages` 200 / `context_max_chars` 400k) giới hạn vòng quét; hoàn toàn fail-open. Dòng attestation/engagement vẫn prepend đúng MỘT lần (last turn) — không nhân đôi theo message. **Escalation thông minh** (`reframe.escalate_on_inspection_fail`, mặc định BẬT): khi request có FEL gặp `data_inspection_failed`, router reframe toàn context và re-dispatch đúng MỘT lần (guard `bsl_fel_context_escalated`) trước khi rơi vào đường terminal-wall — không tốn chi phí ở happy path, không vòng lặp vô hạn ở failure path.

### Đã Sửa

- **Tường relay terminal không còn lặp lại hay spam console** — `upstream_safety_blocked` của CC Max và `data_inspection_failed` của GLM/Qwen trả về HTTP 400, nhưng `400` nằm trong `_RECOVERABLE`, nên chuỗi combo fallback nhảy sang một model anh em trong *cùng* family bị chặn và wrap never-stop retry lặp vô hạn (`[400] … upstream_safety_blocked` mỗi ~15s). Detector nhận biết theo nội dung body `_is_terminal_relay_wall(status, err_text)` (gate theo status 400/403, khớp marker không phân biệt hoa-thường, hoàn toàn fail-open) nay chặn tường ở **mọi** đường egress: OpenAI raw stream, Anthropic egress, Anthropic→OpenAI egress, funnel combo Gemini, retry combo non-streaming, bước reclassify 400→502, và chokepoint never-stop `_raise_combo_wrap`. Tường hiện **một lần** với status thật để client gỡ block ngay. Nội dung `_RECOVERABLE` và mặc định `combo_infinite_retry` không đổi — các 400 recoverable thông thường (vd `unknown field system`) vẫn advance chuỗi. 19/19 test terminal-wall pass; full suite xanh.

---

## [1.0.6] - 2026-09-24

9 commit từ 1.0.5 — đợt gia cố độ bền + tooling nghiên cứu: sửa tool-call GLM (repair-first) + parser phương ngữ tool-call thứ 5, hợp đồng reasoning StepFun step-5, khung gắn engagement theo intent cho FEL, và kiểm soát zero-retention theo từng provider.

### Thêm Mới

- **Khung FEL gắn engagement theo intent (`reframe.bind_engagement`)** — gắn hồ sơ engagement do operator cấu hình *vào* nội dung task đã reframe, để các model guardrail sâu đọc *ý định* (Claude, DeepSeek-pro) chấp nhận framing nghiên cứu hợp lệ thay vì từ chối. Hoàn toàn trung thực: chỉ hiển thị hồ sơ operator cấu hình, không bịa context. 84/84 test FEL pass.
- **Key cấu hình `retention_headers` theo provider** — header zero-retention tùy ý được `_inject_provider_headers` chèn cho *mọi* provider mà không cần header_profile mới. Fail-open, không bao giờ chặn request. Phạm vi trung thực ghi trong code: OpenAI Responses ⇒ `store:false`; Anthropic/Google trả phí ⇒ mặc định không train (chính sách); gateway tự xây ⇒ header riêng tại đây. 16/16 test header pass.
- **Hợp đồng reasoning StepFun step-5** — family compat mới (`app/compat/families/stepfun.py`) kèm gate reasoning-effort trên UI và fold độ chính xác token SSE.
- **Parser phương ngữ tool-call GLM thứ 5** — khôi phục dạng ChatML `<tool_use><invoke ...>` mà bốn parser hiện có đều bỏ sót (lỗi drop tool-batch GLM lặp lại). Tích hợp vào ladder multi-parser; fail-open khi input lỗi.

### Đã Sửa

- **Hội chứng drop tool-batch GLM (giao thức repair-first)** — `app/middleware/tool_arg_repair.py` sửa block tham số tool-call lỗi trước khi parse thay vì drop cả batch.
- **Đợt gia cố sau audit** — AntiStop mid-stream continuation, Response Guard (log_only), dedup `view_file` trong window, và GLM tool-batch repair v2.
- **Đường response `thought_signature` của Antigravity** — converter OpenAI-SSE → Anthropic-SSE giờ giữ `thought_signature` trên block `tool_use`, hết lỗi Google `400 INVALID_ARGUMENT` trên hội thoại tool nhiều lượt.
- **UX phiên bản/cập nhật dashboard** — badge phiên bản có đúng `id` cho script updater; update check nhận biết tag nên release chỉ-có-tag vẫn được báo.

---

## [1.0.5] - 2026-09-21

14 commit từ 1.0.4 — đợt tích hợp provider + observability: transport CommandCode alpha (giữ `thought_signature` qua đường egress), FEL stream observability giai đoạn 1, gia cố OAuth/usage, cùng stack pin nâng bảo mật (fastapi 0.129.1 / starlette 0.52.1 / python-multipart 0.0.32). **Python 3.10 ngừng hỗ trợ**: fastapi 0.129.1 kéo `typing-inspection>=0.4.2` (cần `typing-extensions>=4.12.0`) còn mitmproxy 11.0.2 chặn `typing-extensions<=4.11.0` trên python<3.11; matrix CI giờ là 3.11/3.12.

Đợt sau tag (2026-09-22, gộp vào 1.0.5): sửa đường response `thought_signature` của Antigravity — bộ chuyển đổi OpenAI-SSE → Anthropic-SSE phát block `tool_use` không kèm chữ ký, nên client lưu tool call không chữ ký rồi gửi lại y nguyên, và Google từ chối lịch sử bằng `400 INVALID_ARGUMENT` trên mọi hội thoại tool nhiều lượt qua `antigravity/gemini-pro-agent`. Biến thể của cùng bản sửa này bên trong `normalizer_v2` (chỉ chạy shadow) đã bị revert (egress/ingress bất đối xứng phá vỡ bộ so sánh round-trip shadow) và dời sang task riêng có phạm vi rõ.

### Thêm Mới

- **Transport CommandCode alpha** — `thought_signature` giữ nguyên qua đường egress; endpoint provider CommandCode chuyển sang `/provider/v1` (đã xác minh auth-wall); sửa envelope + dedup kết nối lúc load.
- **FEL stream observability giai đoạn 1** — guard CBNF orphan + sửa leak `aclose` đi cùng hook observability giai đoạn 1.
- **Bypass native-OAuth Antigravity cho slot `/api/test-model` chưa map** — probe antigravity nói đúng wire Cloud Code (`v1internal:streamGenerateContent`).
- **Response Guard chống injection từ provider (`tools.response_guard`, mặc định TẮT)** — detector opt-in nhận diện các mệnh lệnh ngôi thứ hai nhắm vào user (chỉ thị exfiltration / thực thi code) nằm trong response provider đã assemble, phủ cả đường streaming (observer bọc chunk stream và yield mọi chunk y nguyên) lẫn đường non-stream (quét JSON response đã assemble). Chỉ ship chế độ `log_only`: phát đúng một bản ghi telemetry `guard_event` mỗi response vào `.brain/logs/response_guard.jsonl`, không bao giờ sửa nội dung; mọi lần quét đều fail-open, config tắt hoặc vắng mặt chỉ tốn một dict lookup. Placeholder đã ghi trong `config.example.yaml`.
- **Dedup `view_file` trong window** — hai ingress Antigravity giờ loại các cặp tool `view_file` dư thừa có path đã được đọc trước đó trong cùng request khi vẫn còn nằm trong context window của model, chạy trước bước chuyển OpenAI để FIFO id-minter phía Gemini giữ đồng bộ. Removal theo cặp là atomic và fail-open; dòng `[DEDUP]` báo số cặp đã bỏ và số byte tiết kiệm ước tính.

### Sửa Lỗi

- **Kiểm tra cập nhật dashboard nhận biết tag** — `latestVersion` giờ được tính là semver lớn nhất giữa GitHub releases **và** tags, nên release đợt sau tag chỉ có tag (v1.0.5 phát hành không kèm Release object) vẫn được báo đúng; trước đây `/releases/latest` đứng riêng trả về 1.0.4 và pill nâng cấp bị chặn vĩnh viễn với người dùng 1.0.4. Áp dụng cho cả `/api/version/check` lẫn bản sao `/api/check-update` qua một bộ resolver dùng chung; lượt gọi tags không cần xác thực và fail-open (giữ đúng hành vi chỉ-đọc-release khi lỗi), các tag không semver (`pre-extraction-snapshot`, pre-release) bị bỏ qua.
- **`thought_signature` của Antigravity mất trên đường response (Gemini 3.1-Pro `400`)** — `stream_normalizer._tool_events` (bộ chuyển đổi OpenAI-SSE → Anthropic-SSE) phát `content_block_start` không có carrier `thought_signature`, nên client lưu tool call không chữ ký, lượt sau gửi lại không chữ ký, và Google từ chối toàn bộ lịch sử bằng `INVALID_ARGUMENT` ("Function call is missing a thought_signature … position 4"). Đầu vào vốn đã đọc key này — chỉ là chưa bao giờ nhận được giá trị. Sửa 3 lớp: (1) bộ chuyển đổi streaming latch chữ ký vào `tool_blocks` và phát kèm trên block `tool_use` Anthropic, phủ luôn shim `function_call` legacy và đường chunk Gemini-native → OpenAI; (2) `antigravity_upstream` đọc chữ ký từ cả hai mức lồng wire vào cache LRU trong bộ nhớ có giới hạn (512 entry, TTL, hai bộ đếm hit/miss dạng số nguyên — không log writer, đúng AGENTS.md §2); (3) `openai_to_cloudcode_envelope` tái chèn từ cache đó lúc egress, nên client hay cơ chế nén lịch sử có lược bỏ key lạ cũng không thể làm lệch pha hội thoại. Bộ gộp non-streaming đã xác minh không bị ảnh hưởng (truyền `tool_calls` theo tham chiếu). Test 14 → 23 (chuỗi SIG14/SIG15); full suite xanh trên cả hai chân CI.
- **Batch tool call song song của GLM bị rớt âm thầm (hai lỗi độc lập)** — batch N call tới IDE bị thiếu hoặc rỗng hoàn toàn, nặng nhất trên `glm-5.3`, nhẹ hơn trên `claude-opus-4-6-antigravity`, và không xảy ra với `kimi`/`qwen`. Hai đường code riêng biệt gây ra, nên bản sửa đầu tiên tưởng như vô tác dụng. **(1) Parser block ASCII chỉ nhận một call** — `glm_tools._parse_tool_call_block` trả về `Optional[Dict]`, nên cả batch gói trong một block `<tool_response>` không thể sống sót: JSON array giải mã thành `list`, trượt kiểm tra `isinstance(dict)`, không tìm thấy thẻ `<name>`, rồi trả `None`, khiến `normalize_glm_tool_calls` báo `changed=False` và cả batch biến mất **không kèm một dòng log nào**; object nối tiếp (`{…}{…}`) dính `Extra data`; còn pseudo-XML lặp lại chỉ khớp cặp đầu tiên do `re.search`. Đã thay bằng `_parse_tool_call_block_multi` trả về mọi thành viên, kèm bộ tách `raw_decode` cho object nối tiếp và ghép cặp `findall` theo vị trí cho pseudo-XML; cả ba điểm gọi (normalize buffered, rescue streaming, fallback unicode) nay extend thay vì append. **(2) Sụp index ở bộ tích lũy** — `gemini.openai_chunk_to_gemini` đặt khóa slot theo `tc.get("index", 0)`; upstream bỏ `index` (GLM qua MITM) khiến mọi call trong batch rơi về khóa 0, nối N object tham số thành một buffer rồi trượt `json.loads` với `Extra data` và bị loại, nên batch 4 call chỉ phát ra 0. Slot nay khóa theo `("i", index)` khi có index, ngược lại `("s", seq)` cấp phát theo từng call id riêng biệt, còn delta chỉ-chứa-tham-số thì gắn vào slot vừa mở; khóa tuple đồng nhất giữ cho `sorted()` ở bước flush an toàn. Cả hai đường đều fail-open và giữ nguyên id để tương quan request/response. Test +44 (`test_glm_block_batch_drop.py`, `test_gemini_tool_index_collapse.py`).
- **Usage dashboard lệch 7 giờ trên host UTC+7** — `_safe_ts_epoch` giờ giữ nguyên offset UTC.
- **Biểu đồ Usage render tóm tắt toàn cửa sổ** thay vì trang bảng 500 dòng.
- **OAuth trùng kết nối** — gộp khi lưu; mọi dòng token khớp được refresh cùng lúc.
- **Chuẩn hóa key state biến thể hoa/thường trong config.**
- **Sửa usage-frame direct-stream MintRouter.**
- **Revert** — prune credential rỗng + cổng terminal free-tier OpenCode Zen được rollback (2026-09-20) sau vấn đề live-fire.
- **Transport chết giữa stream không còn force-stop response đã stream một phần (502 thoáng qua)** — lỗi transport (`RemoteProtocolError` / peer reset / timeout) xảy ra sau khi token đã stream (`out > 0`) rơi vào guard midstream vốn cố tình từ chối failover combo tại đúng điểm đó (splice provider thứ hai vào parser đang chạy sẽ làm hỏng transcript), nên stream cứ thế kết thúc và client thấy force-stop mà không có terminal frame. Giờ cái chết đó set `TRANSPORT_DIED_PARTIAL_FLAG`, và nhánh splice tiếp diễn AntiStop sau vòng lặp — resume đúng provider/model cũ từ partial text đã tích lũy, không splice parser — hoàn tất response; `should_splice_continuation` (quality.py) giữ nguyên opt-out hiện hành (tắt infinite retry thì không splice), còn death `out == 0` vẫn đi failover combo bình thường và không set flag. Fail-safe: nếu chính continuation request chết, nhánh terminal-frame hiện hành chạy y nguyên.

### Thay Đổi

- **Stack pin bảo mật** — fastapi 0.111.0→0.129.1, starlette 0.37.2→0.52.1, python-multipart 0.0.9→0.0.32; full suite xác minh lại trên chân Py3.11 và Py3.12.
- **Python 3.10 bị loại** — matrix CI `['3.10','3.11','3.12']` → `['3.11','3.12']`; README nâng sàn lên 3.11+.

### Bảo Trì

- **Viết lại AGENTS.md (2026-09-20)** — Gate 4 căn theo invariant gitignore 5-path đã xác minh; thêm bảng quick-facts repo.

---

## [1.0.4] - 2026-09-08

79 commit từ 1.0.3 — đợt flagship ổn định (sửa capture-log stall từng giết session IDE, chết accept-loop WinError-64, gia cố supervisor) cùng hai hệ thống con mới: Faithful Execution Layer và Normalizer Hub v2. Toàn bộ lane web-provider được tách hẳn sang app Chat2API độc lập, không còn dòng code nào ở đây.

Đợt sau tag (2026-09-11, gộp vào 1.0.4): quan sát trực tiếp tab Usage — poller 2s theo chữ ký (giữ cửa sổ đang mở rộng), registry request đang chạy + dải hiển thị, và dọn sạch lane recompute usage cũ (SQLite ledger là nguồn sự thật duy nhất). Đợt muộn (2026-09-11): sửa gợp key `reasoning` trần trong SSE cho model thinking kiểu Mimo, zombie 504 báo đúng out_tokens, kèm header định danh opencode Zen.

### Thêm Mới

- **Faithful Execution Layer (FEL)** — bộ chỉ thị thuần + wiring trực tiếp cho tiền xử lý clarity, gộp chỉ thị egress theo wire-format, phân loại refusal nghiêm ngặt, và một lần recovery re-dispatch. Tường cứng FEL-5: refusal nhóm CSAM/vũ khí classify `blocked` (chốt, miễn nhiễm recovery/research mode, kiểm trước cổng 600 ký tự). Reword độ nhạy song ngữ EN/VI. Mặc định TẮT (theo config); 186 test xanh.
- **Normalizer Hub v2 (chuỗi N)** — registry `normalizer_v2` với phương ngữ schema (`gemini_last_role`, `tool_arg_repair`, `reasoning_policy`, `stream_normalizer`) và bộ so sánh shadow với shadow-log có cap, nằm ngoài hot-path. Áp dụng vào đường request chính; 107 test xanh.
- **Listener dual-stack** — accept IPv4+IPv6 hết ECONNREFUSED cho client localhost/::1; bind-retry (10013/10048, tới 120s) và watchdog tự kiểm tra trong `dualstack_serve`; dò sức khỏe socket chống split-brain với respawn có giám sát.
- **B1 watchdog-supervisor** — dòng tái sinh tự lành; sửa vòng đời PS1 F2–F7; health gate idempotent cho launcher (`/health` không cần xác thực trên cả hai stack, launcher không chứa credential).
- **Pin smoke battery trước restart** — kiểm tra catalog/chat/stream/mapped-Gemini/admin cho deploy staging trước.
- **Máy quét mojibake pre-commit + residual scanner** — chặn commit cp1252 double-encode về sau; 1.398 token hỏng sửa trên 5 file, CHANGELOG làm sạch, audit toàn phần ĐẠT (577 test cận fix, không còn sót trong source).
- **Giá/phát hiện model** — các họ GLM-5.3, GPT-6 Astra, Muse Spark, Hunyuan Hy3/Hy4.
- **Quan sát trực tiếp tab Usage** — poll 2s theo chữ ký (giữ nguyên cửa sổ đang mở rộng) cùng registry in-flight: `OrderedDict` có giới hạn (cap 2.000, tự lành mục treo quá 10 phút) mở tại `GET /api/observability/usage/inflight`, render thành dải pulse ngay trên tab Usage; bản ghi hoàn tất trên cả đường thành công lẫn thất bại (không rò rỉ bộ nhớ).

### Sửa Lỗi

- **Capture-log stall từng giết session Antigravity IDE** — inbound capture ghi payload đầy đủ đồng bộ ngay trên event loop FastAPI, không cap (file đạt 29,8 GB, mỗi lần ghi làm loop treo hàng phút). Giờ là queue writer async drop-on-full (`asyncio.to_thread`), xoay vòng 50MB cho cả ba logger, tự cắt lúc boot, và mặc định chỉ ghi metadata (`BSL_CAPTURE_INBOUND=1` để bật lại payload đầy đủ).
- **Chết accept-loop WinError 64 (F8)** — vòng accept re-arm AcceptEx sau lỗi thay vì chết; đã xác minh trực tiếp với burst thật.
- **Combo wrap never-stop** — 16 điểm cạn chuỗi giờ wrap về combo fallback thay vì force-stop; fallback stream-start Gemini bỏ cổng (429 entry cuối cũng wrap).
- **Body request sai định dạng** — JSON 400 kiểu OpenAI (trước là 500 không xử lý) trên 5 endpoint inference.
- **Guard tool song song GLM** — chèn `disable_parallel_tool_use`, sửa GLM-5.x mất tham số tool.
- **Guard snapshot cũ** — holder snapshot AEP/OAuth không còn xóa provider đã nhập; persist khi xóa provider, dedup OAuth, tắt kết nối chết.
- **Sửa auth key rỗng** + test thought-signature antigravity; NFKD transcode + preflight VN giờ phủ cả họ `agentrouter*`.
- **Gia cố MITM** — WMI pre-scan khi kill kèm blocklist tiến trình hệ thống; DNS hijack gắn với liveness MITM (reconcile lúc boot / gỡ khi stop / rollback khi start); guard tree-kill chống respawn-supervisor lậu.
- **Tab Usage không cập nhật khi stream đang chạy** — dữ liệu trước đây chỉ về ~2s sau khi stream kết thúc; giờ tab tự refresh mỗi 2s khi có stream hoạt động.
- **Điều khiển reasoning Hunyuan Hy4-preview**; chuẩn hóa dấu phân tách variant-ID (ID gạch → contract chấm); parity effort contract gpt-6-astra; thinking contract Fable/Mythos 5.1 (bộ khóa 456/456).
- **base_url loopback: cảnh-báo-thay-vì-chặn; circuit-breaker từ stub → phủ thật.**
- **Health gate idempotent của launcher** — `/health` không xác thực trên cả hai stack, try/catch riêng cho từng stack.
- **Key `reasoning` trần trong SSE (Mimo qua Zen, phương ngữ OpenRouter)** — stream chỉ có reasoning bị lắp thành message rỗng rồi dính zombie 504, mỗi lần đốt một lượt combo fallback; giờ key `reasoning` trần được nhận ở cả bốn điểm trích xuất (TTFT, accumulator, classifier, non-stream relay) và gụp vào `reasoning_content`. Zombie 504 giờ báo đúng `out_tokens` đã tính tiền (trước ghi cứng 0 — case thực tế out=5/in=403, không relay delta nào); message rỗng dù đã tính tiền vẫn giứ 504 để combo tiếp tục. Kèm header định danh opencode Zen (`x-opencode-session`/`x-opencode-request`/`x-opencode-client`).

### Thay Đổi

- **Tách lane web-provider** — toàn bộ web provider (~35 commit, GLM/Kimi/Qwen web, UI OAuth) revert trong `ca5c5c8` (−7.400 dòng), bảo tồn trong app **Chat2API** độc lập. 1.0.4 không ship dòng code web-provider nào; pin `nodriver` gỡ bỏ.
- **SQLite là nguồn sự thật duy nhất cho usage** — xóa `recompute_usage_costs`, `invalidate_recompute_cache`, `usage_stats_shim`, `_UsageListShim`; chi phí tính ngay lúc ghi, không còn đường đọc-tính-lại.
- **Làm mờ key theo sức khỏe** trong UI admin; điều khiển reasoning cho các họ model mới.
- **Thống nhất phiên bản** — file `VERSION`, pill phiên bản dashboard, GitHub tag đều là 1.0.4; thông báo cập nhật dưới logo bắn qua kiểm tra GitHub latest-release trực tiếp khi bản remote mới hơn bản local.

### Bảo Trì

- **Dọn mojibake** — 1.398 token cp1252 double-encode sửa trên 5 file (fixer có cổng, idempotent); CHANGELOG làm sạch sau 161 lần chạy; pre-commit hook + residual scanner chặn mojibake từ nay. Audit toàn phần ĐẠT (577 test cận fix, không còn sót).
- **Xóa code chết** — 24 ký hiệu chứng minh chết (−450 dòng).
- **Gitignore rác runtime `.brain` + artifact live-token**; ẩn console spawn nền (hết cửa sổ cmd trống bật lên).
- **Test guard chống tái xuất hiện lane recompute** — perf test viết lại kèm guard vắng mặt legacy; 34/34 inflight+perf và 195/195 downstream xanh.

## [1.0.3] - 2026-08-26

Đợt sau tag (gộp vào release): egress eventstream nhị phân Kiro, bảo vệ mất key (orphan-key), sửa empty-content-block egress, kiểm tra cập nhật GitHub trực tiếp cho pill phiên bản, và neo lại regex skip của compaction để bỏ loại sai các model GLM.

77 commit từ 1.0.2 — đại tu khả năng phục hồi combo-chain (kèm never-stop retry), chỉ báo quota trực tiếp, chuẩn hóa tham số thinking theo tài liệu chính thức cho mọi họ model, contract Ox Alpha, cùng đợt sau tag: nhập Kiro zero-network, orchestration `blacksand-agentic-ultra`, chống mất key đa tab, và dọn supervisor MITM.

### Thêm Mới

- **Quota còn lại theo key (trực tiếp)** — dò billing one-api/new-api báo quota còn lại cho từng API key (biến thể browser-UA cho các gateway sau Cloudflare), hiển thị thanh % gọn ngay dòng trạng thái key. Chỉ phần trăm — không hiển thị tiền.
- **Chỉ báo quota OAuth thụ động** — header rate-limit từ phản hồi được ghi lại và hiển thị thành thanh quota cho mọi tài khoản OAuth.
- **Contract reasoning-effort cho Ox Alpha** — contract mới cho Ox Alpha / `x-preview-f-free` (fingerprint GLM-5.3, qua reseller): từ vựng effort 4 mức `low/medium/high/max` (mặc định `max`), `xhigh`→`max`, gửi `reasoning_effort` top-level, marker lỗi 1210 để effort sai chỉ hạ cấp và thử lại một lần thay vì 400 cuối. Đã xác minh trực tiếp với opencode-zen.
- **Điều khiển chống treo** — tab Settings: nút kill stream, badge stream trực tiếp, watchdog tự khởi động lại.
- **Header profile theo provider** — header định danh client nghiêm ngặt (`default`/`codex`/`claude_code`/`custom`), áp dụng cho request và xác minh key.
- **Lịch sử dùng trong SQLite** — lưu usage bền vững (giữ 100k bản ghi), thay JSONL.
- **Trình xác minh Ed25519 đầu vào pool Tier-2** — xác thực chữ ký (body-hash binding, cấu hình fail-closed).
- **Adapter egress Codex Responses-API** — dịch payload OpenAI sang/đừ `/responses` của Codex (sửa 403 HTML): ép `store:false`+`stream:true`, chuẩn hóa effort, chuyển SSE Responses → khung OpenAI.
- **Tự nhập Kiro + profileArn** — tự nhập kết nối Kiro từ AWS SSO cache; chèn `profileArn` top-level (sửa 400); tách đường refresh social (kiro.dev) vs OIDC (AWS).
- **Chuẩn hóa model-ID mờ** — phân giải dự phòng không phân biệt gạch/chữ số/thứ tự (`gpt-5-6-terra` → `gpt-5.6-terra`); tên chính xác không bao giờ bị viết lại.
- **Kiro nhập từ IDE-cache zero-network** — endpoint `/kiro/import-ide` nhập kết nối từ AWS SSO cache local, không gọi mạng lúc nhập (miễn nhiễm flag anti-abuse của Kiro); nút khuyến nghị trong modal provider.
- **Kiro tự dò SSO** — modal provider Kiro dò IDE SSO cache và đề xuất kết nối một chạm "session detected" (các chế độ thủ công vẫn còn làm fallback).
- **`blacksand-agentic-ultra` orchestration đầy đủ** — chạy trọn vòng balanced-mode (7 mẫu phase, lead+1 member ở phase commit, trần 22 message, substance gate, bước synthesis, usage gộp), port từ Blacksand Code.

### Thay Đổi

- **Sóng chuẩn hóa thinking-contract chính thức** — từ vựng reasoning theo vendor dựng lại từ tài liệu chính thức: GLM-5.2/5.3, Grok-4.5/4.6, Qwen 3.8, Hunyuan Hy3, Muse Spark 1.1/1.2.
- **Chọn key đa tầng tất định** — thay `random.choice` bằng top-first mặc định + công tắc `round_robin` thật.
- **Circuit breaker bật mặc định** — key 429 tự động rút khỏi pool.
- **Cache parse MITM** — config.yaml cache theo mtime, giảm độ trễ xác thực mỗi kết nối.
- **Khả năng quan sát** — log mới nhất trước, phân trang tab Logs/Usage, ghi usage trên mọi đường 200 không stream.

### Sửa Lỗi

- **Continuous fallback cho combo** — chuỗi giờ quay vòng qua các lượt retry thay vì dừng cứng sau một lượt ("All N combo chain entries exhausted").
- **Never-stop combo retry** — hết chuỗi giờ là ranh giới lượt, không phải điểm dừng: khi mọi entry thất bại (kể cả hết wall budget), chuỗi quay về entry 0, xóa state failover key, backoff luỹ thừa (2s→30s) và thử lại đến khi client ngắt kết nối — terminator duy nhất. Đặt `settings.combo_infinite_retry: false` để khôi phục 502 cũ.
- **Wall budget theo kích thước chuỗi** — đồng hồ tường giờ scale theo độ dài chuỗi (`max(240s, entries × 130s)`), lá 524 chậm (~125s) không thể làm kẹt các entry sau (lỗi force-stop Opus-Tabitoken).
- **CHAIN_TOTAL_BUDGET 150s → 960s**.
- **Reset budget từng entry (BUG K)** — hết đói chuỗi khi header-timeout/midstream fallback.
- **Stream không render được** — tiến chuỗi combo + drain nhẹ nhàng khi restart; frame kết thúc rỗng thay vì None; hết force-stop và JSONResponse TypeError.
- **Buffer tiền render reasoning (BUG N)** — khung thought trước nội dung được giữ không cam kết emission, fallback combo hợp pháp khi stream reasoning chết giữa đường.
- **App-kill khi test model** — `/api/test-model` chạy dưới semaphore 2 slot + timeout 75s; dashboard chặn test chồng lấn.
- **Kilocode tools[0].type 400** + ngắt kết nối giả khi reasoning chậm.
- **Forge envelope Cloud Code cho Antigravity** — gọi upstream combo (Vision/compaction) hết 404.
- **Gemini egress** — thông báo không-đầu-ra hiển thị, fileData passthrough, loại trừ gate.
- **Nội dung tiếng Việt AgentRouter** — chuyển mã NFKD thay chặn cứng (chỉ codepoint precomposed gây 400).
- **Failover đa key** — `tried_conns` theo request để retry bấm key KẾ TIẾP thay vì key vừa cạn; vẫn tôn trọng `connection_indexes`, breaker, round-robin.
- **Schema request Kiro** — sửa từ ground truth bắt được (`inferenceConfig` top-level, `modelId` trong `userInputMessage`, `chatTriggerType`/`conversationId`/`origin`).
- **Lưu kết nối OAuth** — qua đường config-state swap chuẩn.
- **Phân loại lại khả năng phục hồi** — timeout/500/502 mức transport đi qua combo fallback; insufficient_user_quota xếp là auth.
- **Modal Edit-Provider ghi đè key** — ô key bỏ trống giờ giữ key cũ; key khác sẽ thêm connection mới (ngang "+ Add API Key"); hết mất key âm thầm trên provider đa key (Tabitoken/Gorouter/Seekai).
- **Mất connection do lưu stale hai tab** — save lost-update không thể rơi connection chỉnh ở tab khác; merge guard kèm opt-out `_deleted_connection` cho lần xóa chủ đích.
- **Codex 400 `Unsupported parameter: messages`** — khối inject payload theo intent không còn chạy trên payload Responses của Codex; helper fold gộp nội dung vào `instructions` thay vì tạo key `messages`.
- **Route import Kiro bị che** — route riêng `/kiro/import` đăng ký trước generic `/{provider}/import` (thứ tự match FastAPI).
- **Guard supervisor respawn MITM** — `force_kill_mitm_port` đi parent chain từng listener (WMI/CIM) và tree-kill supervisor respawn (vòng PowerShell `while($true){mitmdump}`) TRƯỚC vòng kill listener, nên respawner lạ không còn phá dọn port và gây chuỗi 503; bảo vệ PID 0/4/self.
- **400 tài khoản chết giờ bị softban** — lỗi từ chối tài khoản deterministic ("Action plan limited", "user is not allowed to access") xếp loại `auth`, cooldown 90s ngay lập tức và combo bỏ qua lá chết; trước đây lá chết được chọn lại làm primary mỗi request (~5s phí mỗi lần gọi).
- **Effort `ultra` GPT-5.6 trung thực** — ép về `max` kèm UI/comment rõ (ultra là orchestration multi-agent của Codex, không phải tham số wire).
- **Dropdown thinking Ox Alpha** — chọn effort ngay trên dòng model; concurrency `/api/test-model` 2→3.

---

## [1.0.2] - 2026-08-15

### Sửa Lỗi

- **TOOL-META: Lỗi xác thực tool của Antigravity IDE** — Các model ngược dòng (DeepSeek, Qwen, GLM, Kimi) không tạo ra trường `toolSummary`/`toolAction` mà Antigravity IDE yêu cầu. Hàm `_inject_tool_metadata()` mới trong `app/compat/adapters/gemini.py` tự động điền giá trị mặc định qua `setdefault` tại cả hai điểm phát functionCall. Đã kiểm chứng trực tiếp trên cả 6969 và 6970.

- **ZOMBIE: Phản hồi chỉ-reasoning chặn combo fallback** — Hàm `_response_has_model_output()` trong `app/main.py` coi trường `reasoning_content`/`reasoning` là output hợp lệ. Khi model reasoning chỉ tạo thinking tokens mà `content` rỗng, combo fallback bị bỏ qua và user nhận phản hồi trống. Đã sửa: chỉ tính `content` là output hiển thị. Thêm hỗ trợ format Anthropic (`content[].text` / `content[].type == "tool_use"`).

- **VISION-FAILOPEN: Vision scout lỗi chặn toàn bộ phản hồi** — Khi tất cả ứng viên vision đều thất bại cho một ảnh, `VisionPolyfillFailed` được raise và `main.py` trả 502, chặn TOÀN BỘ phản hồi. `vision.py` giờ thay thế bằng `PLACEHOLDER_UNREADABLE` và cho request tiếp tục. Handler `VisionPolyfillFailed` chỉ giữ lại cho trường hợp timeout hết ngân sách (504).

- **VISION-ANTHROPIC: Vision scout hỗ trợ provider format Anthropic** — Vision scout trước đây chỉ hiểu format multimodal OpenAI. Các provider format Anthropic (ltn-ai, a6api) bị bỏ qua âm thầm. Đã thêm `"anthropic"` vào `_VISION_SUPPORTED_FORMATS`, builder `_build_vision_payload_anthropic()` mới, và tự động nhận diện format trong `_describe_image_once()`.

- **OBS-PRICING: Gộp registry giá observability + sắp xếp log** — `_load_pricing_registry()` chỉ tải registry chính thức được seed, bỏ qua file giá đã phát hiện. Endpoint logs trả về cũ-trước thay vì mới-trước. Đã sửa: gộp cả hai nguồn registry với điền null-price, thêm `invalidate_recompute_cache()` sau khi phát hiện giá, endpoint logs giờ trả mới-trước.

- **OAUTH: Thiếu client_id gây lỗi 400 "invalid_request" trên tất cả OAuth provider** — Endpoint `authorize` cho phép `client_id` rỗng/không đặt truyền thẳng đến provider, gây lỗi 400 từ phía provider. Đã sửa: (1) Thêm helper `_missing_client_id_hint()` với thông báo lỗi theo từng provider. (2) Tổng quát hóa validation `authorize()` cho tất cả flow `authorization_code`. (3) Thêm validation `clientId` cho endpoint `device_code()` với provider có static client ID (bỏ qua `kiro` qua flag `dynamicClientId`). (4) Sửa nhất quán `_prepare_provider_config()` trong `device_code()` và `poll()` — cả hai giờ dùng prepared config thay vì `entry["config"]` thô. (5) Xóa debug print trong `app/oauth.py`.

- **Dọn dẹp debug print** — Xóa các print `[ZOMBIE-DEBUG]`, `[DEBUG:{_label}]`, và `[Kiro Debug]` khỏi `app/main.py` — các log forensic chi tiết từng request sẽ spam output production.

---

## [1.0.1] - 2026-08-14

### Thay Đổi
- **STREAM-GUARD chẩn đoán first-bytes** — khi guard từ chối fallback post-emission, log từ chối giờ kèm mẫu 256B đầu tiên được emit, giúp xác định veto có hợp lý không (reasoning scaffolding vs user-visible content) mà không suy yếu bất biến no-second-stream.

### Sửa Lỗi
- **GPT-5.6-SOL midstream 502 (stream chỉ-reasoning)** — khi model vision/reasoning (DeepSeek V4, MiniMax M3 qua `qwencoder/gpt-5.6-sol`) chỉ tạo `thought:true` frames trước khi leaf chết giữa vận chuyển, emission gate coi reasoning-pane text là content đã commit, phủ nhận combo fallback, và IDE đóng băng trên stream chết. Gemini egress giờ giữ thought frames pre-content trong buffer giới hạn (256 KiB) mà không đánh dấu emission, nên transport death/stall vẫn có thể failover. Body-content frame đầu tiên flush buffer theo thứ tự và commit như cũ. Bộ phân loại mới `gemini_frame_is_thought_only` trong `app/compat/adapters/gemini.py`; bộ test hồi quy `app/tests/test_thought_buffer_prender.py` (19 test).
- **Tab Logs & Usage tải 2-3 phút** — endpoint Usage đọc lại `config.yaml` + registry giá từ đĩa và tính lại chi phí cho ~10k entry mỗi lần mở tab; cả hai endpoint tuần tự hóa danh sách 10k entry đồng bộ; frontend render mỗi hàng trong một DOM write với re-render theo keystroke. Sửa: phân trang `?limit=&offset=` (mặc định 500, kẹp 1..2000) trả `{total, entries, has_more}`; tính lại chi phí throttle TTL 60s + guard registry-mtime; frontend giới hạn DOM hàng ở 500 với nút "Load 500 more" và debounce search (300ms); header `X-Total-Count` cho live-poller 2s bỏ qua parse body khi không đổi. 15 test mới trong `test_observability_perf.py`.
- **Vision Scout 502 timeout loop** — vision polyfill có thể hết ngân sách wall-clock trước khi mọi ứng viên fallback được chạy. Nguyên nhân: timeout 60s mỗi lần thử chỉ đủ 2/4 ứng viên trong ngân sách 120s. Giảm timeout mỗi lần xuống 15s và tổng ngân sách xuống 65s để cả 4 ứng viên có đủ lượt thử (4 × 15s = 60s + margin) vẫn giới hạn thời gian IDE stall.
- **Upstream chứng chỉ self-signed** — thêm hỗ trợ `ssl_verify: false` từng provider để kết nối tới upstream có chứng chỉ self-signed không còn lỗi `SSL: CERTIFICATE_VERIFY_FAILED`.
- **Độ trễ auth pipeline** — config đã giải mã giờ được cache trong `config_state` thay vì giải mã lại mỗi request.
- **Độ chính xác circuit breaker** — lỗi billing và auth giờ được đánh dấu riêng để breaker không phân loại sai thành provider outage.

### Bảo Mật
- Xóa snapshot config nhạy cảm khỏi Git history và gitignore thư mục `.brain/` và config backup để chống leak key.
- Cứng hóa `export-public.ps1` chống xuất `config.backup.*` và scratch script ở root.

---

## [1.0.0] - 2026-08-09

### Thêm Mới
- Bản phát hành công khai đầu tiên
- Định tuyến đa provider với pipeline 5 tầng (Chat, Lite, Agentic, Ultra, Max)
- Dịch giao thức giữa các format OpenAI, Anthropic và Gemini
- Chuỗi dự phòng combo với kiểm soát ngân sách theo deadline
- Tích hợp MITM proxy qua mitmproxy, kèm watchdog và tự khởi động lại
- Hỗ trợ OAuth 2.0 cho Google, GitHub, Anthropic và provider tùy chỉnh
- Chuẩn hóa stream cho mọi format provider
- Trang quản trị web với theo dõi thời gian thực
- Hỗ trợ Cloudflare Tunnel và Tailscale
- Circuit breaker cho các kết nối provider bị lỗi
- Ghi log quan sát dạng JSONL
- Vision scout phân tích ảnh, kèm bù năng lực cho provider không hỗ trợ vision
- Bộ đọc tài liệu cho RAG
- Tự cập nhật từ GitHub releases
- Phát hiện giá model

### Bảo Mật
- Mã hóa Fernet cho API key và OAuth token khi lưu trữ
- Khóa mã hóa gắn với máy, nên file config bị đánh cắp không dùng được ở nơi khác
- Xác thực session quản trị với tự động hết hạn
- Bộ quét bảo mật theo yêu cầu với tám hạng mục kiểm tra
- Ghi config kiểu atomic để tránh hỏng file khi lưu bị ngắt

### Sửa Lỗi
- `NameError` trong bộ xử lý `UnicodeEncodeError` khi ngân sách chuỗi chưa được
  định nghĩa
- Đường dẫn tuyệt đối ghi cứng trong `bslrouter.ps1` và
  `ninerouter_isolation.ps1` khiến script không chạy được ngoài máy tác giả;
  giờ đường dẫn được tự phát hiện kèm biến môi trường để ghi đè
- `FileNotFoundError` trơ trọi khi clone mới mà chưa có `config.yaml`; giờ khởi
  động sẽ in hướng dẫn setup cụ thể
- `config.yaml` rỗng hoặc sai định dạng gây `AttributeError` khó hiểu ở tầng
  dưới; giờ báo lỗi rõ ràng ngay lập tức
- Thông báo `[CRYPTO] Decryption failed:` gây lo lắng với lý do trống khi chạy
  lần đầu; giờ nhận biết được placeholder mẫu chưa điền, và lỗi thật sẽ báo kèm
  loại ngoại lệ cùng cách khắc phục
- 15 import không dùng trong 14 file (dọn lint F401/F811)
- Khai báo trùng `quote_plus` trong `oauth.py`

### Sửa Lỗi — đợt sau tag (2026-08-26)
- **Key API mới không bao giờ được dùng** (fallback + round-robin): `connection_indexes`
  theo model đóng băng từ lúc lưu — các provider thêm key (seekai, orcarouter,
  opencode-zen, llm7-io, kilocode) không bao giờ dùng được key mới nhất. Pass orphan-key
  lúc load giờ mở rộng allow-list mọi model với các index enabled vượt max, và cả 2 chỗ
  thêm key trong UI cập nhật index khi lưu.
- **`model output must contain either output text or tool calls`** trên client Anthropic
  (agentrouter/gorouter/seekai ở effort max): upstream trả `content` rỗng với toàn bộ
  output trong `reasoning_content`, cả 2 bộ chuyển đổi egress đều bỏ qua. Non-stream giờ
  fallback sang `reasoning_content`; streaming tích luỹ và phát block text lúc kết thúc.
- **Egress eventstream nhị phân Kiro** — 200 `vnd.amazon.eventstream` được giải mã bằng
  decoder riêng + shim `_SyntheticResponse` trước zombie guard; Kiro loại khỏi stream
  buffer text-SSE; cache IDE cũ refresh một lần khi import và giữ `authMethod`/
  `clientId`/`clientSecret`/`region`; `TokenType: EXTERNAL_IDP` chỉ áp dụng cho IdC (sửa 403).
- **Pill phiên bản không bao giờ hiện** — `/api/version/check` hardcode
  `hasUpdate:false`; giờ kiểm tra GitHub releases trực tiếp (mặc định
  `dongden-alt/BSL-Router`, ghi đè qua `update.github_repo`) với cache 5 phút; sửa slug
  mặc định `bsl-router` cũ.
- **Compaction không bao giờ chạy** — regex skip khớp substring `anthropic`, loại nhầm
  các model dạng `glm-5.3-anthropic` (chính các model compaction nhắm tới). Regex giờ
  neo theo token family đứng đầu; dòng khởi động (`[Compaction] eligibility: X/Y`)
  giúp quan sát được độ phủ.
- **CI 3.11 flake fix** — relaxed the 5-10ms egress timing budgets (keepalive/connect keepalive/connect/body-stall) in the `test_builtin_timeout_error_*` regression tests to 0.05/0.05/0.3/0.1s, above GitHub shared-runner scheduling jitter; the deterministic TimeoutError is raised by the mock stream, so the budgets gate nothing the assertions depend on (run 34715722609, py3.11 leg).

### 2026-09-25 - v1.0.8 (cont.) - Gemini 3.5 Flash Retirement + Anthropic thought_signature Cache Fix

- **Removed 3 retired Gemini 3.5 Flash models** (gemini-3.5-flash-medium, gemini-3.5-flash-high, gemini-3.5-flash-low) from the Antigravity Integration mapped model list. The Antigravity IDE removed these from its model menu; the router now follows. Retired slots moved to _ANTIGRAVITY_OBSOLETE_SLOTS so saved configs auto-purge them. The gemini-3-flash-agent legacy alias migration retargets to gemini-3.6-flash-high (the surviving Flash-high slot).
- **Fixed Anthropic-path thought_signature 400 on gemini-pro-agent**: stream_normalizer.py (the Anthropic client path) captured the thoughtSignature from Gemini responses and stamped it onto the wire but never stored it in the shared signature cache. Turn-2 echo re-injection then had an empty cache and emitted an unsigned functionCall, causing Google to reject with 400 Function call is missing a thought_signature. Added a lazy-import _signature_cache_store call at the capture site so the cache is populated on the Anthropic path exactly as it already was on the OpenAI path. Regression test test_sig15g_stream_normalizer_stores_signature_in_shared_cache locks the store -> lookup -> fallback re-inject cycle.

### 2026-10-18 - Codex egress bridge for Gemini/Anthropic clients + terminal error events

- **Codex upstreams behind Gemini and Anthropic streaming clients no longer drop content.** The Anthropic SSE egress converter and the Gemini drain previously received raw Codex Responses-SSE dialect chunks that their converters ignored, producing empty/zero-output streams. New bridges in `app/main.py` (`_anthropic_egress_openai_source`, `_tee_codex_response_source`, `_flush_codex_response_tee`, `_gemini_frame_commits_transcript`) convert Codex Responses SSE into OpenAI chat chunks exactly once via `codex_adapter.responses_sse_to_openai_sse` before the normalizers run, and tee the raw transcript for forensics (env-gated, capped writers off the hot path).
- **Codex terminal failures now surface as parser-safe error events** instead of silently truncated streams: `app/codex_adapter.py` maps `response.failed` and `response.incomplete` to error chunks (`_error_chunks`) in both the SSE and single-event converters, including multi-chunk and non-JSON-tolerant variants.
- **Boot regression guard**: restored module-level `_CAPTURE_QUEUE_MAX = 256` consumed by the lifespan capture queue (its absence was a boot-fatal NameError after the egress refactor wave).
- New regression suites: `app/tests/test_codex_adapter_error_events.py`, `app/tests/test_gemini_drain_emission.py`, `app/tests/test_anthropic_egress_codex.py`.
- Verified: full suite green on BOTH CI legs (Py3.11 3790/3790, Py3.12 3790/3790) + runtime boot smoke on :6977 (200 OK, isolated config). Known pre-existing failure NOT from this change (reproduced identically at HEAD 1dfb1cf, Py3.10 dev venv only): `test_builtin_timeout_error_post_content_emits_terminal_error_frame` - builtin TimeoutError (OSError family before the 3.11 aliasing) escapes into the AntiStop continuation path and re-dials the same leaf; the client-visible terminal contract (single terminal frame + single [DONE], no splice) still holds.
- Also ships `scripts/certify_standard.py` (Phase-2 certifier for the Canonical Standard Harvest Protocol in ARCHITECTURE.md; disk-only, zero API calls).

### 2026-10-19 - CI: discrimination-index floor recalibrated to the public standards corpus

- **`test_real_standards_build_index` was red in CI from birth** (run 72, both legs): the local dev tree carries 60 standard files (18 public canonical + provider-tagged files kept local by `.gitignore` since privacy split b129e71), so the old `standards_count >= 50` floor passed locally but asserted `18 >= 50` on a clean CI checkout. Recalibrated to `>= 15` - headroom below the 18-file public corpus while still catching catastrophic corpus loss. Canonical-model gates untouched (they pass on 18).
- Verified in an isolated worktree at HEAD d30437d: full suite green on BOTH CI legs (Py3.12: 3786 passed / 471.59s; Py3.11: 3786 passed / 486.15s).