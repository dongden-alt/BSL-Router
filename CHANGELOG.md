# Changelog

All notable changes to BSL Router are documented in this file.

Mọi thay đổi đáng chú ý của BSL Router được ghi lại trong file này.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

**🇬🇧 [English](#105---2026-09-21)** · **🇻🇳 [Tiếng Việt](#-tiếng-việt)**

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
