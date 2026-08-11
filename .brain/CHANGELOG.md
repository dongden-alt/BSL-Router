# BSL Router Changelog

## [2026-08-11] Gemini Egress Timeout Label Fix (499/504 confusion) — commit aedd924

### Investigated + Fixed
- **Bug report**: `[BSL Router] upstream error 504: TimeoutError` in IDE while provider dashboard showed 499.
- **Root cause**: NOT a status misread. The router's own pre-header `wait_for` in `app/main.py:_send_stream_with_thinking_fallback` fires, aborts the upstream connection (provider logs the abort as 499), and reports a synthetic 504. The bare `asyncio.TimeoutError` has an empty `str()`, so the terminal frame fell back to the useless type-name label `TimeoutError`.
- **Fix**: wrap the pre-header `wait_for` in try/except and re-raise as `TimeoutError("upstream_header_timeout (waited Ns for headers, M chain budget left)")`. Unifies the label vocabulary between the inner timer and the outer Gemini `_conn_deadline` loop; applies to both Gemini and raw_upstream egress paths.
- **Not-a-bug clarified**: `qwencoder` 504 rows in console_logs are GENUINE upstream nginx 504 HTML responses (provider gateway timeout), logged faithfully. Provider-side 499 for those is the provider's own gateway abort.
- **Deferred (Option B)**: removing the duplicate inner timer in favor of the single Gemini deadline loop — needs its own test pass; inner timer is the only header-wait guard for the non-Gemini `raw_upstream` path.

### Verified
- `py_compile app/main.py`: pass
- `test_chain_deadline.py`: 3/3 pass (151s)
- First edit attempt silently no-op'd; detected via `git diff` and re-applied successfully.

---

## [2026-08-10] Config Wipe Protection + Provider Bloat Cleanup + Log Rotation

### Fixed
- **Config wipe prevention (Protection 2b)**: Added provider-count regression gate in both `app/main.py:_persist_config_snapshot` and `app/error_prevention.py:_persist_config_yaml`. Refuses any write where new provider count < 50% of existing count (floor=10). Blocks the 2026-08-10 wipe signature where a corrupted in-memory config with 1 provider would atomically overwrite a 64-provider config.yaml.
- **NoneType error fix**: `app/compat/stream_normalizer.py` lines 340, 375 - changed `.get("choices", [])` to `.get("choices") or []` and `.get("tool_calls", [])` to `.get("tool_calls") or []`. Root cause: upstream APIs sending explicit `"choices": null` (not missing key) caused `.get(default=[])` to return `None`, then `None[0]` -> `TypeError: 'NoneType' object is not subscriptable`.
- **Stream hard deadline**: `app/antifreeze.py` - added `STREAM_HARD_DEADLINE_SECONDS = 600.0` to prevent infinite streaming hangs on slow reasoning models.

### Changed
- Purged 55 bloat providers from `config.yaml` (119 to 64 providers, 229KB to 208KB). Rule: a provider survives only if it appears in BOTH frontend `KNOWN_PROVIDERS` (app.js) AND backend `OAUTH_PROVIDERS`/`PROVIDER_DEFAULT_URLS` (oauth.py/main.py). Deleted:
  - 11 orphan OAuth providers (claude_code, cline, clinepass, gemini-cli, iflow, kilo, kilocode, openai_codex, qwen, xai, xai_grok)
  - 50 duplicate/orphan API providers (azure x2, glm x2, minimax x2, ollama x2, opencode x4, vertex x4, xiaomi x4, + 30 orphans: blackbox, cerebras, chutes, cloudflare, cohere, fireworks, hyperbolic, nebius, nvidia, perplexity, qoder, siliconflow, together, vercel, volcengine, alicode, alibaba, byteplus, mimo-free, etc.)

### Maintenance
- Truncated `.brain/logs/antigravity_inbound.jsonl` (73MB to ~200 lines)
- Truncated `.brain/logs/mitm_live_debug.log` (16MB to ~200 lines)
- Truncated `.brain/logs/mitm_egress_frames.jsonl` (6.5MB to ~200 lines)
- Deleted 11 old CC debug logs from `.brain/` (~1.3MB total)
- Cleared `.brain/scratch/` (100+ diagnostic scripts from prior debugging sessions)
- Cleared `.brain/cc_tasks/` (old task files)

### Verified
- `test_config_persist_atomic.py`: 7/7 pass
- `test_stream_normalizer_failfast.py` + `test_stream_tool_rescue.py` + `test_anthropic_to_openai_tool_ids.py`: 14/14 pass
- Live wipe test: 1-provider write over 64-provider config -> `[CONFIG-GUARD] refused` -> 64 providers preserved
- No combos reference any deleted provider

---

## [2026-08-09] Timeout Fix + Documentation Overhaul

### Fixed
- **Stream timeout**: `app/antifreeze.py` - `STREAM_HARD_DEADLINE_SECONDS = 600.0` for slow reasoning models (DeepSeek V4, MiniMax).

### Documentation
- Full revision of `README.md` and `docs/ARCHITECTURE.md` to reflect public-safe feature set.
- Removed sensitive provider names from public-facing docs.

---

## [2026-08-03] Initial Config Wipe Discovery

### Identified
- Root cause of config wipe: minimal 1-provider in-memory config persisted to disk, overwriting full 119-provider `config.yaml`.
- Added Protection 2 (zero-providers gate) in `app/main.py` and `app/error_prevention.py`.
