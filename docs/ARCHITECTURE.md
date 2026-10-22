# How BSL Router Works

> **Codex JSON credential import (DONE — isolated runtime verified 2026-10-22; production backend awaiting explicit restart):** The Codex provider page offers a secondary **Import JSON** action while preserving browser OAuth. `POST /api/oauth/codex/import-json` reads at most 256 KiB from the request stream (independent of `Content-Length`), rejects duplicate JSON keys and malformed/deep schemas, strictly reconciles flat/nested/wrapped aliases, requires a nonempty ASCII header-safe ChatGPT account ID, validates access/refresh/ID tokens as printable ASCII without embedded whitespace or controls (benign outer spaces are trimmed), and normalizes aware ISO or numeric expiration timestamps with overflow-safe conversions. Account metadata may fall back from the ID token to access-token claims and is subject to the same account-ID safety check; access-token `exp` governs credential lifetime. Expired credentials require a refresh token. Raw ID tokens are used only for metadata extraction and are never persisted; access and refresh tokens follow the existing encrypted `_save_connection` path and email upsert policy. The admin UI applies the same UTF-8 byte limit and validates JSON syntax/object shape locally, but sends the original raw JSON so backend duplicate-key enforcement remains authoritative for both file and paste input. It also enforces file/paste ambiguity checks, pre-read duplicate-submit locking, abort/late-response suppression, and close/Escape credential cleanup. Isolated Playwright verification on `:6977` exercised the real endpoint with synthetic config, including duplicate-key file/paste 400s, retry, and no-save assertions. The production process on `:6969` was not stopped or restarted and needs an explicit restart before this backend route/fix is live; static assets may update independently on refresh.

> A plain-English guide to the architecture. No jargon, no assumptions.
>
> Hướng dẫn kiến trúc bằng ngôn ngữ dễ hiểu. Không thuật ngữ khó, không giả định kiến thức.

**🇬🇧 [English](#the-big-picture)** · **🇻🇳 [Tiếng Việt](#-tiếng-việt)**

---

## The Big Picture

BSL Router does **four things**:

1. **Receives** a request from your AI client
2. **Classifies** it to determine the best routing tier
3. **Translates** it into the right format for the target provider
4. **Sends** it upstream and streams the response back

If the provider fails, it automatically tries the next one in your fallback chain.

```
  Your App                BSL Router               AI Providers
  ────────    request    ──────────    translate    ──────────
  Claude   ──────────▶  Classify +  ──────────────▶  OpenAI
  Code                   Route +      ◀─────────────  Anthropic
  Cursor                 Translate                   Google
  Any IDE  ◀──────────  Normalize  ◀──────────────   DeepSeek
            response    stream       response       GLM
                                                      Kimi
                                                      ...etc
```

---

## What Happens When You Send a Request

Here's the full journey of a single chat request, step by step:

### Step 1: Request Arrives
Your client sends a `POST /v1/chat/completions` (or `/v1/messages` for Anthropic format).
Malformed bodies (truncated JSON, non-object payloads, invalid UTF-8) are rejected at the door with an OpenAI-style JSON 400 (`error.code: invalid_json_body`) — never an unhandled 500 (H0, 2026-09-06).

### Step 2: Authentication Check
If admin auth is enabled, BSL Router verifies the session. API requests use the BSL key from your config.

### Step 3: Request Classification
BSL Router looks at your request and classifies it through multiple layers:
- **Request intent**: Is this simple chat, code generation, or agentic tool use?
- **Category**: 13 categories (general, technical, creative, scout, power_coder, vision, etc.)
- **Coding category**: 8 sub-categories for coding-specific routing (fast_coder, architect, reviewer, etc.)
- **Task complexity**: Estimated complexity determines the effort tier (fast/standard/strong)

### Step 4: Provider Selection
Based on the classification, BSL Router picks the best provider:
- If you specified a **Blacksand model** (e.g. `blacksand-chat`) -> routes through the 5-tier matrix
- If you specified a **combo** (fallback chain) -> starts with the first provider in the chain
- If you specified a model directly -> uses that model's provider
- If auto-select is enabled -> picks based on capability + cost

### Step 5: Protocol Translation
Your client speaks OpenAI format, but the provider uses Anthropic? BSL Router translates:
- Message format conversion
- Tool call ID mapping
- Thinking/reasoning parameter injection
- Streaming protocol normalization
- GLM tool translation (GLM has a unique tool call format)

### Step 6: Upstream Request
BSL Router sends the translated request to the provider with:
- Connection pooling (reuses TCP connections for speed)
- Timeout management (chain deadline + per-stream deadline)
- OAuth token auto-refresh (if the provider uses OAuth)
- Circuit breaker (skips unhealthy providers)

### Step 7: Response Streaming
The provider streams back chunks. BSL Router:
- Normalizes the stream format back to what your client expects
- Validates stream integrity (catches malformed chunks via Stream Guard)
- Handles thinking/reasoning blocks per provider rules
- Applies quality gates (truncation detection + retry)

### Step 8: Fallback (if needed)
If the provider returns an error:
1. BSL Router checks if there's a next provider in the combo chain
2. If yes -> repeats Steps 5-7 with the next provider
3. If no more providers -> returns the error to your client
4. A **chain deadline timer** prevents infinite retries across all hops

---

## Blacksand Model Routing

BSL Router's signature feature is the **5-tier Blacksand routing system**. Instead of picking a specific model, you point your client at a virtual model name and BSL Router handles the rest.

### The 5 Routing Tiers

| Tier | Model Name | When it's used | Matrix |
|---|---|---|---|
| 1 | `blacksand-chat` | General chat, Q&A, mixed tasks | 13 categories × 3 effort tiers |
| 2 | `blacksand-lite` | Coding-agent single-task routing | 10 coding agents × 3 effort tiers |
| 3 | `blacksand-agentic` | Fast-tier agentic coding (depth=fast) | Multi-agent dispatch |
| 4 | `blacksand-agentic-ultra` | Balanced coding with consult (depth=balanced) | Agent + consultant |
| 5 | `blacksand-agentic-max` | Multi-domain fusion (depth=balanced) | Cross-domain orchestration |

### Combo Alias System

Each matrix slot accepts a **combo alias** that maps to your configured providers:

| Alias | Tier | Typical use |
|---|---|---|
| `coder-1` | Fast | Quick completions, simple tasks, scout routing |
| `coder-2` | Standard | General coding, balanced quality/speed |
| `coder-3` | Strongest | Complex reasoning, architecture, multi-step planning |

You define what `coder-1/2/3` mean in your `combos` config. Each alias can have a fallback chain.

### Category Classification Pipeline

```
Request → Request Intent → Category (13-way) → Coding Category (8-way)
         ↓                    ↓                      ↓
         chat/code/agent      general/technical      fast_coder/architect/
                              creative/scout/...     reviewer/...
                                                      ↓
                              Effort Tier: fast / standard / strong
```

The 13 categories for `blacksand-chat`:
`general`, `technical`, `creative`, `scout`, `power_coder`, `vision`, `fast_coder`, `architect`, `reviewer`, `debugger`, `refactorer`, `documenter`, `tester`

The 10 coding agents for `blacksand-lite`:
`scout`, `fast_coder`, `power_coder`, `architect`, `reviewer`, `debugger`, `refactorer`, `documenter`, `tester`, `vision`

### Matrix Configuration

The matrix is configured in `config.yaml` under `bsl_models`:

```yaml
bsl_models:
  bsl_chat:
    enabled: true
    category_overrides:
      general:
        fast: "coder-1"           # Use combo alias
        standard: "coder-2"
        strong: "coder-3"
      technical:
        fast: "coder-1"
        standard: "coder-2"
        strong: "coder-3"
    default_route_enabled: true
    default_route: "coder-2"      # Fallback for unconfigured categories
    global_last_fallback: "coder-1"  # Safety net (always active)

  bsl_lite:
    enabled: true
    category_overrides:
      scout:
        standard: "coder-1"
      power_coder:
        standard: "coder-2"
    default_route_enabled: true
    default_route: "coder-2"
    global_last_fallback: "coder-1"

  bsl_agentic:
    enabled: true
    agent_routes:
      scout: "coder-1"
      planner: "coder-3"
    default_route_enabled: true
    default_route: "coder-2"
    global_last_fallback: "coder-1"

  bsl_agentic_ultra:
    enabled: true
    agent_routes: {}
    consult_routes: {}
    default_route_enabled: true
    default_route: "coder-2"
    global_last_fallback: "coder-1"

  bsl_agentic_max:
    enabled: true
    agent_routes: {}
    chat_routes: {}
    default_route_enabled: true
    default_route: "coder-2"
    global_last_fallback: "coder-1"
```

> You don't need to fill every slot. **Auto-Select** fills empty slots from a recommended route table. Unconfigured categories fall through to `default_route`, then `global_last_fallback`.

### Agentic Orchestration (Tiers 3-5)

Tiers 3-5 use an **orchestrator engine** that:
1. Classifies the request into an agent role (planner, coder, reviewer, etc.)
2. Routes to the model configured for that role
3. Tier 4 (Ultra) adds a **consult** step - a second model reviews the primary output
4. Tier 5 (Max) fuses multiple domains (coding + analysis + creative) for complex workflows

The orchestrator uses **gates** to decide whether to escalate from fast -> balanced depth.

---

## Key Components

### 🔄 Protocol Translation Layer
**What it does**: Converts between API formats so any client works with any provider.

BSL Router supports three protocol families:

| Protocol | Who uses it |
|---|---|
| **OpenAI format** | OpenAI, DeepSeek, Kimi, Qwen, Grok, MiniMax, OpenRouter |
| **Anthropic format** | Anthropic, GLM |
| **Gemini format** | Google Gemini, Google Cloud Code |

Each provider family has its own **family adapter** (14 adapters total):

| Adapter | Quirks handled |
|---|---|
| `openai.py` | Standard OpenAI format, cache-key routing. GPT-6-Astra tool gate (2026-10-02): strips `reasoning_effort`/`reasoning` whenever tools are present (upstream 400s the combo; Astra thinks by default and cannot disable it) |
| `anthropic.py` | Thinking blocks, cache_control, prompt_tokens_details. AgentRouter lanes (2026-10-02): `agentrouter`/`agentrouter-o` on the anthropic / openai-responses wires emit adaptive thinking ONLY — `output_config` suppressed (Bedrock ValidationException when both co-ride one request) |
| `gemini.py` | Gemini envelope format, streamGenerateContent |
| `deepseek.py` | Reasoning effort mapping |
| `glm.py` | Tool call format translation (via `glm_tools.py`) |
| `kimi.py` | Key-bound prompt caching |
| `minimax.py` | Reasoning effort mapping |
| `qwen.py` | Complex - 11KB adapter for Qwen's unique format quirks |
| `grok.py` | Standard OpenAI-compatible |
| `openrouter.py` | Multi-model routing |
| `doubao.py` | ByteDance Doubao (Ark/Volcengine): `minimal/low/medium/high` effort vocabulary, `minimal` disables thinking |
| `hunyuan.py` | Tencent Hunyuan Hy3: `reasoning_effort` nested inside `chat_template_kwargs` (`no_think`/`low`/`high`) |
| `kat_coder.py` | KwaiPilot Kat-Coder: effort vocabulary undocumented, pass-through strategy |
| `muse.py` | Meta Muse Spark: version-split wire shapes (1.1 vs 1.2), 1M context |

Infrastructure adapters: `_base.py` (shared base), `_effort.py` (reasoning effort ladder), `_legacy_reference.py`.

#### Codex Responses-wire reasoning sanitize (2026-10-19) — DONE (runtime-verified), live on next user-commanded restart

`codex_adapter.openai_to_responses()` converts the egress body to the Responses shape **before**
`resolve_thinking()` runs, so the `gpt-?[56]` family contract (`app/compat/families/openai.py::_apply_gpt5`)
previously stamped chat-completions controls — top-level `reasoning_effort` and `reasoning.mode="pro"` —
onto the already Responses-shaped body. Live probes (`.brain/scratch/codex_live_probe3-6*`) proved
`chatgpt.com/backend-api/codex/responses` returns **HTTP 400** on both keys ("Unsupported parameter:
reasoning_effort" / "reasoning.mode is not supported with this model"), which surfaced downstream as the
codex OAuth **502 tool-call error**. Clean bodies (`reasoning.effort` + `reasoning.context="all_turns"`,
mode absent or `"standard"`) return 200.

Fix: a `codex_responses_wire_sanitize` guard at the TOP of `_apply_gpt5` — when the payload is
Responses-shaped (`"input" in payload and "messages" not in payload`) it strips top-level
`reasoning_effort`, pops `reasoning.mode` only when it equals `"pro"` (effort/context untouched),
records provenance (including a manual `ProvenanceRecord` fallback so the rule is always attributed),
and returns immediately, skipping all chat-shaped OpenAI controls. The chat/completions path is
unchanged (negative-control test). Tests: `app/tests/test_codex_wire_sanitize.py` (3) —
124/124 targeted green on `.venv311` + `.venv312`.

Runtime E2E (2026-10-19): sandbox router on `:6979` (isolated `BSL_CONFIG_PATH`, production `:6969`
untouched) served the original failing shape — tool-carrying `/v1/chat/completions` on
`codex/gpt-5.6-sol` — as **HTTP 200 in 4.1s** with `finish=tool_calls` (`get_weather {"city":"Tokyo"}`),
provenance `resolved_by:gpt-5:codex_responses_wire_sanitize[]` live in the router log.
Digest: `.brain/scratch/codex_e2e_verify.txt`.

#### Codex lane: Chat Completions → Responses translation (2026-10-09)

`app/codex_adapter.py::openai_to_responses` is a **separate wire dialect** from the family adapters above: the
codex OAuth lane speaks the OpenAI **Responses** API, whose `input[]` array accepts only
`user|assistant|system|developer` roles. Chat Completions history does not map 1:1, and passing roles through
verbatim is what produced a live 400 (`Invalid value: 'tool' … param: input[6]`) on `codex/gpt-5.6-sol`.

Translation contract (shapes mirror `app/normalizer_v2.py::_egress_responses`):

| Chat Completions input | Responses output |
|---|---|
| `role:"tool"` / `role:"function"` | **top-level** `{"type":"function_call_output","call_id","output"}` — never an input message |
| tool message missing `tool_call_id` | degrades to a `user` message (never a null `call_id`) |
| `assistant.tool_calls[]` | **top-level** `{"type":"function_call","call_id","name","arguments"}`; `arguments` always a string (`json.dumps` when upstream sent a dict); accompanying text emitted first |
| multiple system messages | concatenated into a single `instructions` |
| unknown / empty role | `user` |

Streaming parity (phase 2): `response.output_item.done` → `delta.tool_calls`;
`response.function_call_arguments.delta` → arguments passthrough; `response.completed` carrying a
`function_call` → `finish_reason:"tool_calls"` (previously `"stop"`, which told agentic clients the turn was
finished while a tool call was still pending).

> [!WARNING]
> `tools` forwarding is gated on `has_tool_history and _codex_forward_tools_enabled()` — so turn 1 of a fresh
> tool conversation (tools declared, no tool history yet) gets **no** `body["tools"]` and cannot emit its first
> call. The narrow gate is pinned by the legacy `test_codex_kiro_oauth.py::test_strips_unsupported_params`
> (encodes pre-tool-support behaviour from the 2026-08-22 probe) plus
> `test_bare_tools_declaration_without_tool_history_keeps_strip`. Kill-switch `BSL_CODEX_FORWARD_TOOLS`
> (default ENABLED; `0`/`false`/`no` disables; read per-call). Widening this needs that legacy test updated —
> open decision.

Tests: `app/tests/test_codex_adapter_tool_roles.py` (35), plus `test_codex_responses_payload_integrity.py` and
`test_codex_kiro_oauth.py` (67 targeted, green on `.venv311` + `.venv312`).

### 🔗 Combo/Chain Fallback
**What it does**: Tries multiple providers in order until one works.

You define chains in your config. The system uses a **chain deadline timer** to prevent infinite retries. If all providers in the chain fail within the deadline, the error is returned to your client.

### 🛡️ Anti-Freeze System
**What it does**: Prevents frozen IDEs and infinite hangs.

| Component | Protection |
|---|---|
| **Stream Hard Deadline** | 10-minute cap per stream. Catches stuck streams while allowing legitimate long reasoning chains. |
| **Chain Deadline** | Total budget across all fallback hops. Prevents cascading timeouts (4 hops × 120s = 480s without it, capped to ~150s with it). |
| **Circuit Breaker** | Health-aware endpoint rotation. Unhealthy providers are automatically skipped. Tracks 429/401/403 and TPM co-occurrence. |
| **Stream Guard** | SSE stream integrity validation. Catches malformed chunks, missing `message_stop` events, and stall-silence freezes. |
| **Thinking Fallback** | Reasoning parameter fallback. If a provider rejects thinking parameters, retries without them. |
| **Idle Terminal Frame** | Bounded upstream silence (`GEMINI_EGRESS_MAX_IDLE` = 300s) on all 4 egress lanes. On expiry: `upstream_idle_timeout` + status 504 + terminal error frame + `[DONE]` — never a bare `200 [DONE]` that wedges the IDE parser. |
| **Relay Wall Gate (2026-10-02)** | Terminal short-circuit for provider-side content walls: `sensitive_words_detected` (AgentRouter answers it as HTTP 500) is terminal — no combo advance, no never-stop wrap, no retry against a wall that can never pass. Any other 500 body keeps the recoverable-5xx path; 400/403 keep the legacy marker gate. Tests: `app/tests/test_terminal_relay_walls.py` (27) |

### 🧰 Tools & Intelligence Layer
**What it does**: Pre-processes content before routing.

#### Document Intelligence
- Parses PDF, DOCX, XLSX, PPTX attachments
- Summarizes documents above the skip threshold (default: 8000 tokens)
- Uses a configurable summarization model (defaults to cheapest active connection)
- Documents below threshold pass through verbatim

#### Vision Bridge
- Intercepts image URLs sent to text-only models
- Replaces them with detailed text descriptions using a vision model
- Configurable token budget (512/1024/2048/4096)
- UI/UX design context override mode (forces 4096 tokens + exhaustive prompts)

#### Token Budget
- Hard `max_tokens` ceiling (1024–65535 range)
- When disabled: 65535-token floor applied (anti-truncation)
- When enabled: requests declaring higher max_tokens are rejected with HTTP 400
- Also caps quality-gate truncation retries

#### Prompt Caching & Compaction
Four provider-specific caching strategies:

| Strategy | Provider | How it works |
|---|---|---|
| **Anthropic Explicit** | Anthropic | Injects `cache_control: ephemeral` on system prompt blocks |
| **Kimi Key-Bound** | Kimi/Moonshot | Injects hashed `prompt_cache_key` on system blocks |
| **OpenAI Cache-Key** | OpenAI (GPT-5.6) | Hashed `prompt_cache_key` for static system prefixes ≥1024 chars |
| **Static-First Sorting** | DeepSeek, Gemini, OpenAI | Reorders messages to anchor system blocks at top for implicit caching |

Plus an optional 24h cache retention flag for OpenAI (experimental).

### 🔐 Credential Management
- **API keys**: Stored encrypted with Fernet symmetric encryption
- **OAuth tokens**: Automatically refreshed before expiry
- **Key scanner**: On-demand security audit

> [!IMPORTANT]
> The encryption key is **machine-bound**. Copying `config.yaml` to another computer will not carry the credentials over.

### 🛡️ Security Scanner
Audits your provider configuration for risky settings. Checks for: exfil URLs, key injection, URL spoofing, credential leakage, local network exfil, insecure transport, token tampering, and duplicate keys.

Findings are graded **block** (must fix), **warn** (review), or **info**.

### 🕵️ MITM Proxy (Optional)
Intercepts traffic for apps that don't let you change the API URL:
1. Adds entries to your hosts file
2. Runs a transparent proxy on port 443
3. Watchdog process monitors and restarts if it crashes
4. Tree-kill + verify+retry loop ensures clean port cleanup

### Model Authenticity Verification (2026-10-03)

A baseline-driven gate that answers: *"is the upstream actually serving the model it claims?"*
Lives in `app/authenticity/`. Adapted from the BazaarLink probe-engine methodology for router use.

**Pipeline (tiered, fail-open, config-gated by `authenticity.enabled`, default OFF):**

| Tier | Name | What it does |
|---|---|---|
| 0 | Cache | Fresh stable verdict short-circuits re-probing (TTL ~30min). Suspect verdicts are NOT cached. |
| 1 | Surface fingerprint | 5 probes (identity_direct, identity_adversarial, style_sample, cutoff_probe, capability_boundary) -> feature extraction with **self-claim zeroing** -> family classification. |
| 2 | Deterministic match | cutoff/capability probes -> `match_submodel` against frozen TraitMark standard. |
| 2.5 | Judge (LLM) | Grades creative/EQ/humor/design probes. Judge from a DIFFERENT family than the suspect (never hardcoded; user picks). |

**Key structures:**
- **TraitMark** (`traitmark.py`) - frozen official-standard schema per sub-model, harvested ONCE from a trusted anchor; the trusted source is never re-called at test time.
- **Scope registry** (`scope.py`) - `TRUSTED_ANCHORS` maps 7 families to their trusted source: GLM/Kimi/DeepSeek/Qwen = `aibox` (gold, direct vendor API); Claude/GPT/Gemini = `mintrouter`/`codex`/`antigravity` OAuth wrappers (reference).
- **Layer weights** (`LAYER_WEIGHTS`) - identity 0.10, knowledge 0.28, reasoning 0.10, style 0.05, lexical 0.20, noncoding 0.12, structural 0.15. Structural (tokenizer/tool/code) is deepest; style is lowest (wrapper-shiftable).
- **Reality verdicts** (`scoring.py` `compute_reality`) - `real / downgrade / substitution / family_match / unverifiable / error`, plus a `modifications[]` list (prompt injection, output cap, context truncation, thinking strip). Confidence is a 5-level verbal badge, never a raw %.

**Standards lifecycle:** harvested once from a trusted anchor via `POST /api/authenticity/standards/harvest`, stored under `app/authenticity/reference/standards/*.json` (gitignored). New models onboard by JSON snapshot - no code change. **The exact onboarding procedure is the Canonical Standard Harvest Protocol — see the tail section of this doc; follow it mechanically for every new model/family.**

**Probe observability (2026-10-07):** every authenticity probe is recorded via `observability.log_request` (the same in-process pipeline the `/v1` handler uses), tagged `provider="authenticity"` + `client="authenticity:<probe_id>"`. A Run Test therefore surfaces one END row per probe in the admin **Logs** tab (`GET /api/observability/logs`) and **Usage** tab, instead of running as invisible outbound traffic. Fail-open: telemetry never aborts a probe.

**Fixed (2026-10-07):** `compute_reality` (`scoring.py`) bound `matched`/`sub_score` only inside the `match_result is not None` block, so a full-scope run with `match_result=None` and family confidence >= `REAL_THRESHOLD` (0.80) raised `UnboundLocalError: local variable 'matched'`, which the fail-open catch wrapped into an ERROR/UNVERIFIABLE verdict with empty `errors`. `matched`/`sub_score` are now bound before the verdict decision. Regression: `app/tests/test_authenticity_scoring_matched_unbound.py`.

**Structural fixes + full-depth standards rebuild (2026-10-08, user trust-repair):** the matcher previously read FLAT keys (`cutoff_year` / `capability_correct` / `noncoding.judge`) that TraitMark standards do not carry (they nest under `knowledge.*` and top-level `noncoding.{axis}.score`), so every standard scored 0.5-neutral and never compared noncoding — the single defect behind the "always-deep / low family confidence / wrong-model match" cluster. `matcher.py` now reads the nested TraitMark shape with legacy-flat fallback. Six additional logic holes fixed in the same pass: refusal error-clobber (a failed probe no longer overrides a real verdict), judge tie-break false-REAL (override now requires `match_score>=0.70`), cutoff garbage freeze (year range-gated 2019..now+1, context-preferred), single-shot judge parse (now `_parse_judge_json` balanced-brace scan + `_judge_once` retry), self/same-family judge guard (harvest endpoint drops a conflicting judge), and harvest lexical capture (slop/vocab were 0.0 on all 40 standards — now computed from the non-coding outputs). 12 provably-garbage standards swept; 26 canonical standards re-harvested to full depth via `scripts/rebuild_thin_standards.py` (in-process against the live router, each with a different-family judge). Auth suites 118/118 on Py3.10/3.11/3.12. See `.brain/CHANGELOG.md` → `AUTH-STRUCTURAL-FIX-2026`. **Status: CODE DONE — code fixes await user-commanded router restart to go live on :6969.**

**Trusted-sources-only benchmarks (2026-10-08, user directive):** canonical authenticity standards may be harvested ONLY from trusted sources — **codex, antigravity, mintrouter, aibox**. Every other lane (vsllm, a6api, agentrouter, sharellm, chat2api, iamhc, zhipu, pix4k, ...) is an untrusted relay/proxy whose data must never be frozen into a benchmark; vsllm is explicitly banned. Enforcement is structural, not convention: `POST /api/authenticity/standards/harvest` now refuses a canonical harvest (empty `source_tag`) from a non-whitelisted lane with HTTP 400 (provider-tagged snapshots stay exempt — they never enter the match pool via the `baseline.py` source_tag filter), and `scripts/rebuild_thin_standards.py` carries a `TRUSTED_SOURCES` whitelist + `_trusted()` guard. The standards pool was purged to exactly the trusted set — 15 canonical models (claude x6 via mintrouter, gpt x4 via codex, glm x2 via aibox/mintrouter, kimi-k3 via mintrouter, qwen3.8-max via aibox, deepseek-v4-pro via aibox) — and every canonical standard that was harvested off an untrusted lane (or had no trusted lane) was deleted, along with all 17 vsllm-sourced tagged snapshots. A UTF-8 BOM regression (from a PowerShell prefix-strip) that silently dropped 9 canonicals from the match pool was also fixed.

**Canonical-standard audit status (2026-10-08):** *Matcher correctness* -- **DONE** (full-evidence ranking, needle dict crash, rescue-over-tie, asked-scope recall, non-deterministic stability, same-basis runner-up, judge summary nested keys; 138/138 authenticity tests on Py3.10/3.11/3.12). *Standard depth* -- **PARTIAL**: median 6/9 matcher dimensions populated; cutoff 3/15, knowledge needles 7/15, logprob 1/15, deterministic stability 0/15; `capability_boundary` is 3 trivial questions (0 bits). With a realistic suspect **19 of 22 same-family sibling pairs are ambiguous** (3 separable); the matcher abstains instead of guessing (0 confident-wrong, 0 wrong-family) but cannot yet *detect* sub-versions for the claude and gpt families. *Standard v2* (capability curve with fresh random instances, cross-labelled knowledge bank, tokenizer fingerprint via usage counts, variance-aware standards, honest alias groups) -- **PLANNED**, not built.

**Probe timeouts + retry (2026-10-07):** content probes are now **stream-first** — they open an SSE stream, accumulate chunks, and enforce a first-chunk deadline (`probe_ttft_s`) plus a per-chunk stall guard (`probe_idle_s`), with a **non-SSE JSON fallback** when the upstream returns a scalar body. The logprob probe is deliberately pinned `stream:false` (logprobs require the whole-response JSON body). Retries fire on status `0` / `>=500` / error (2s gap), never on 4xx; each attempt emits its own telemetry row tagged `#r<n>`. Two value tiers:

| Path | `probe_ttft_s` | `probe_idle_s` | `probe_retries` | `total_timeout_s` |
|---|---|---|---|---|
| **Test** (`run_auth_test`, via `cfg.setdefault`) | 75 | 30 | 1 | 240 |
| **Production** (config unset → legacy) | `None` (30s scalar whole-response) | — | 0 | 90 |

Leaving `probe_ttft_s=None` keeps the exact legacy scalar path, so production callers are byte-for-byte unchanged. The test tier is relaxed (not the router's inference ceiling) because probes are short content samples: 75s first-byte is generous for a slow-but-real proxy seller (observed 45s generations), while the 30s idle guard catches a stalled seller without waiting forever on a test. This is intentionally **not** the router's inference doctrine (`HEADER_WAIT_TIMEOUT=300` + `read=None` unbounded body); probes bound both ends so a hung seller can't wedge a Run Test.

**OAuth provider model catalog (2026-10-07):** `app/oauth.py` `OAUTH_PROVIDER_MODELS` is the single source of truth for the seven OAuth provider pickers (codex, antigravity, grok-cli, claude, cursor, kiro, github), served at `GET /api/oauth/providers/models`; `app/static/app.js` carries a **manual mirror** of the same dict (the browser never imports oauth.py) and the two MUST be kept in sync. The catalog is static by design: these OAuth wrappers expose no unauthenticated `/v1/models` to self-discover from, so the list is refreshed by research, not fetch. The 2026-10-07 refresh removed retired IDs (`claude-opus-4-6-thinking`, `grok-build`, `gpt-5.2-codex`, `gemini-2.5-pro`, `kimi-k2.5`, the old kiro/cursor lineups) and added the current lineups (opus/sonnet 5.5 low/medium/high on antigravity; GPT-6.x + GPT-5.6 on codex/github; grok-4.20/4.7/4.3 on grok-cli; Composer 2.5 + Muse Spark 1.3 on cursor). Seeding/backfill in `app.js` now mints new catalog rows with `enabled:false` (was `true`) — the user enables models manually. Backfill is append-only: stripping a model from the catalog does NOT remove or disable rows already in `config.yaml`; antigravity self-purges retired slots via `_ANTIGRAVITY_OBSOLETE_SLOTS`, the other six providers have no purge. Kiro/cursor 2026 IDs are extrapolated from each vendor's naming pattern (no machine-readable catalog exists) — MEDIUM confidence, flagged in the changelog.

**Antigravity catalog tier split (2026-10-18):** the 16 plan-gated antigravity rows now carry a precise `tier` tag instead of one merged `pro-ultra` label - `gemini-4-argon*` (4 rows) = `ultra` (Google's models doc: "Ultra and Enterprise only"); `claude-{opus,sonnet}-5-5-*` (6) + `gemini-3.1-pro-{low,high}` (2) = `pro` (models doc: "Pro (non-trial), Ultra and Enterprise"; agentpedia 2026 plan comparison lists them inside Pro's full 5-model set); `gemini-2.5-pro` + `gemini-3.6-flash-*` (4) keep the `pro-ultra` union tag because sources conflict (conservative call). Evidence cross-validated 3-way: Google's `antigravity.google/docs/models` page, the agentpedia Pro-vs-Ultra plan comparison, and the live `v1internal:fetchAvailableModels` registry (tieredModelIds). The tag is informational only - grep-verified no code consumer reads `tier` (only catalog rows + comments reference it), so request routing is untouched; `GET /api/oauth/providers/models` serves it as passthrough metadata. Parity re-verified after the split: `app/oauth.py` == `app/static/app.js` 27/27 rows, identical tag counts (pro-ultra=4 / pro=8 / ultra=4); 35/35 provider/oauth/antigravity tests green on both CI legs.

**Identified-as circularity fix (2026-10-07):** the report previously echoed the suspect's own claim as its "identification" (`Identified as: <input>`) and claimed it "matched the frozen official standard for `<input>`" — circular, evidence-free. Root cause: the REAL path set `identified_as = matched or claimed or model_id`, so a matcher abstain (`matched == ""`) fell through to the claim. Now: when the sub-model matcher matches, `identified_as` = the matched frozen standard id; when it abstains, `identified_as` = the confirmed **family** and the rationale/UI state "family only — sub-version unconfirmed" instead of fabricating a standard match. `submodel_match` and `identified_kind` (`submodel`|`family`) are now serialized so the UI can distinguish a true sub-model identification from a family-only one. Regression: `app/tests/test_authenticity_scoring_matched_unbound.py` (asserts family, not claim echo).

**Test cascade redesign (2026-10-07, user-driven):** the objective is "IS the model REAL or FAKE" — family is only the secondary tier. Changes: (1) **REAL now requires a confirmed sub-model standard match** (`total >= REAL_THRESHOLD AND matched`); family-only high confidence reports **FAMILY MATCH** with ranked closest candidates (never a shrug dressed as REAL). (2) **Matcher-first cascade**: normal pass classifies family + matches standards; a single high-confidence match stops and reports (REAL/downgrade after family corroboration via the fused family+submodel score and substitution-on-conflict). (3) **Escalation to deep** fires when the matcher abstains (no single high-conf match) — unchanged auto path. (4) **Judge tie-break** (new `judge_rank.py`): deep mode, only on a TRUE tie (abstain with best/runner-up gap < 0.05), hands the tied top candidates' frozen profiles to the judge (a different family) which ranks them; a decisive pick (confidence >= 0.6) resolves the match. Fail-open always. (5) **Coverage-aware weights**: `match_submodel` renormalizes dimension weights over compared dims (`coverage_adjust`), so a missing axis is neutral, not an implicit penalty — the precision lever for thin candidates. (6) **Cancel granularity**: `_check_cancel` between probes + RuntimeError propagation in `_progress`/lane catches + pre-escalation re-check in main.py, so a cancel lands at the next probe boundary instead of running the full battery. UI renders ranked closest-candidate chips on FAMILY MATCH/UNVERIFIABLE with per-candidate confidence zones. Regression: `app/tests/test_authenticity_cancel_tiebreak.py`, updated `test_authenticity.py` + `test_authenticity_scoring_matched_unbound.py`.


**V2 canary fail-open (2026-10-18):** `run_standard_v2`'s pre-flight canary (single non-streaming request) is no longer a single point of failure. A canary miss (timeout, 5xx, refusal) used to set `gap=lane_unreachable` and abort all four v2 lanes — every live suspect run that lost the canary lost the whole v2 profile, collapsing sibling separability into the fixed 2-candidate tie cluster (the vsllm-o/claude-* symptom). Now a miss is recorded as diagnostics (`canary_gap`, e.g. `http_500` / `ReadTimeout`) plus `gap=canary_failed_open`, and the lanes proceed under their own per-call budgets, retries and per-lane timeout isolation. Offline tests pin the contract: dead transport → all four lanes attempted, per-lane `lane_gaps` reported. Harness hazard (documented in the tests): `_pace()` sleeps per CALL and transport/5xx errors retry through `_RETRY_GAPS=(4.0, 10.0, 25.0)`, so offline fail-open batteries neutralize `_MIN_INTERVAL` and strip retry gaps (`gaps=()` at direct call sites; a `call_with_backoff` wrapper in `TestGateCascade._run_gate` — gaps is a def-time default arg, so patching the module global is useless). Verified on all three legs (Py3.10 60/60; venv311 60/60; venv312 60/60). Live at the next user-commanded `:6969` restart (AGENTS.md §1).

### 📊 Observability
- Every request/response logged in JSONL format
- Usage stats per model with cost tracking (see Usage Store below)
- Live in-flight request registry (`/api/observability/usage/inflight`) shown as a pulsing strip in the Usage tab
- Error tracking with per-provider breakdown
- Live log streaming in the admin dashboard

#### Usage Store (SQLite)

Usage history is stored in a lightweight `data/usage_stats.sqlite3` database. This replaces the previous in-memory list + JSONL rotation approach so the system can handle 6 M+ rows (a full six-month window).

**Key design decisions:**
- **Write path**: every successful (`status == 200`) request writes a row to SQLite via `append_usage_event()`. Cost and savings are computed at write time using the current pricing registry, so reads never need to reprice.
- **Read path**: paginated keyset cursor `(ts_epoch DESC, id DESC)` — the API returns one page of up to 500 newest-first entries. The frontend loads additional pages by sending the `next_before_id` from the previous response.
- **Summary endpoint**: `/api/observability/usage/summary` returns server-side aggregates (totals, top providers/models, timeframe-aligned buckets, DB size) without returning any raw rows.
- **In-flight registry (2026-09-11)**: a thread-safe bounded `OrderedDict` (`_INFLIGHT_REQUESTS`, 2,000-entry cap) registers each request at start and completes it on both success and failure paths, so entries never leak. Entries older than 600s are pruned as stale on read. Exposed read-only via `GET /api/observability/usage/inflight`; the Usage tab polls it every 2 seconds while streams are active and renders a pulsing strip. With the legacy recompute lane removed, this SQLite ledger is the sole usage source of truth.
- **JSONL archive**: existing `data/usage_stats.jsonl` lines are migrated once into SQLite on startup (idempotent, fingerprint-guarded). Corrupt lines are skipped. The file is retained as an archive but no longer rotated or treated as source of truth.
- **No silent deletion**: there is no hard row-count cap or size-based rotation for usage history. Rows accumulate indefinitely until a future age-based cleanup job is added.
- **Column filters**: UI column-filter dropdowns remain local to the currently loaded page. Total counts come from the summary endpoint.
- **Fail-open**: SQLite errors never break `log_request()` or proxy requests. If the database is unreachable the router continues logging to the console log path normally.

### 🔍 Scouts (Optional)
Pre-process specific content types before routing:

| Scout | Purpose |
|---|---|
| **Vision** | Analyzes images, polyfills vision for providers that don't support it |
| **Docs Parser** | Extracts text from documents for RAG |
| **Canvas** | Analyzes UI/canvas elements |

---

### Streaming Resilience (2026-09-06)
- **Degenerate-output guard (vsllm-r/gpt):** `_degenerate_effort_cap_decision` in `app/main.py` caps reasoning effort xhigh→high when provider=vsllm-r + gpt-5/6 contract + openai-responses wire + input≥40k tokens (upstream returns 200 with 9-112-token rubble at xhigh on large contexts). Post-stream telemetry `note_degenerate_output` flags degenerate END rows; kill-switch `tools.degenerate_output_guard` (default ON).
- **SSE keepalive pre-first-chunk:** `_sse_keepalive_pre_first_chunk` emits `event: ping` (Anthropic lanes) / `: keepalive` (OpenAI lanes) every 10s while waiting for the first upstream chunk, then stops — byte-faithful mid-stream. Fixes CC/IDE clients whose 30s no-data watchdog aborted slow-TTFT (15-55s) requests. Gate `tools.sse_keepalive` (default ON).
- **Boot-time log self-heal:** `_selfheal_errlog` truncates `.brain/logs/app.err.log` >50MB to a 10MB tail at boot (in-place; the cmd supervisor holds the `2>>` handle). WinError-64 shadow `accept_coro` exceptions are now consumed via done-callbacks (`_track_shadow_accept_tasks`) — no more "Task exception was never retrieved" spam. END console lines serialize error bodies single-line (`_single_line_error`) so 400-error rows stay greppable.

- **Idle terminal-frame parity (2026-10-02):** all four egress lanes (OpenAI, pure-Anthropic, anthropic-to-openai, Gemini) cap upstream silence at `GEMINI_EGRESS_MAX_IDLE` (300s; reset only on real upstream chunks, never on keepalive ticks); expiry routes through the post-drain stall branch (`upstream_idle_timeout` + status 504 → terminal error frame + `[DONE]`, pre-content combo-fallback, dead-leaf benching). Fixes the Antigravity IDE freeze: a wedged MintRouter stream used to end as a bare `data: [DONE]` with HTTP 200, and the IDE parser hangs forever waiting for a terminal error frame on non-success termination. Tests: `app/tests/test_gemini_egress_idle.py` (14), `app/tests/test_anthropic_to_openai_idle.py` (9).

#### Mid-stream transport death → continuation splice (2026-10-09)

`upstream error 502: midstream_transport: peer closed connection without sending complete message body` is a
**socket death after content already reached the client**. Recovering from it requires two things to be true at once,
and both were broken — which is why the failure surfaced as a user-visible *force stop 502* on an answer that had
already streamed.

| Defect | Mechanism | Fix |
|---|---|---|
| **B1** — flag gated on the wrong signal | `_midstream_transport_fallback` set `TRANSPORT_DIED_PARTIAL_FLAG` only when `stats["out"] > 0`, but `out` is fed **exclusively** by the terminal usage frame (`_usage_from_sse_data`) — precisely the frame that never arrives when the transport dies. Flag unset → `should_splice_continuation` (`quality.py` L523-557) False → splice never armed. | Gate on **delivery**, not usage: entry `if stats.get("out", 0) == 0:`, flag written under `if _delivered:`. Census before the fix: **1528** terminal `reason=OTHER` rows vs **zero** `splicing continuation` rows. |
| **B2** — no status restore | After a successful continuation `stats["status"]` stayed 502 and `stats["error"]` stayed set → the `finally:` `obs.log_request` logged `[502] … ERROR:`, and the Gemini guard at L12188-12197 (`error and 400 <= status and status != 499`) emitted `terminal_error_frame(502)` + `SSE_DONE` **on top of a complete answer**. | One shared `def _continuation_status_guard():` restores `status = 200`, `error = None`, pops the flag, flushes. Called at all three splice sites: OpenAI (guard L10581 / commit L10626), Anthropic (L11287 / L11326), Gemini (L11829 / L11872). |

> [!CAUTION]
> **Three design facts — do not "simplify" them away.**
> 1. `partial_text` does **not** grow during a continuation; no continuation loop fed the detector before this fix.
>    Each site therefore keeps **local** `_cont_chunks` / `_cont_bytes` counters and passes them to `_commit`.
> 2. `_emit.byte_count` **lags** at commit time — Gemini drains through an `asyncio.Queue`
>    (`_q` / `_SENTINEL` / `_drain_openai_source` → outer pump L12593), Anthropic through `StreamNormalizer`
>    (L11236-11238). Never read `byte_count` as the continuation total.
> 3. `_commit`'s third condition is load-bearing: the Anthropic/Gemini continuation loops run **inside**
>    `_transport_guarded`, so a *second* death writes a fresh 502 that `_commit` must **not** overwrite with 200.

> [!WARNING]
> **Rejected variant — the `_delivered` entry gate.** Gating the whole fallback path on `if not _delivered:` breaks
> combo failover. `partial_text` accumulates upstream reasoning/thought deltas (`quality.py::_observe` L442-455,
> L463-480, L487-501 incl. `thought:true`) while on the Gemini lane buffered thought frames are deliberately **not**
> `mark_emitted` pre-commit (L11949-11952, L12003-12005). A thought-only stream that dies therefore looks
> "delivered" (`partial_text` non-empty) yet emitted **zero** bytes → failover skipped → the router resumes the
> **dead** provider. `may_fallback()` (`app/stream_guard.py` L102-114) is the real emission veto.

Ordering invariant inside every splice loop: `_detector.feed(_cc)` → `_emit.mark_emitted(_cc)` → `yield _cc`.

**Doc debt (open):** `app/middleware/quality.py` L516-518 still comments that the flag is written "on its out>0
fall-through (out==0 still takes the existing combo failover)" — now **false**. `quality.py` was read-only for this
change; correct the comment in a follow-up.

Tests: `app/tests/test_midstream_transport_partial_flag.py` (18) — source-contract assertions across all three
splice sites plus flag-gate behaviour (`out==0` + delivered → flag set; nothing delivered → flag unset; `out>0`
back-compat). Status: 🔨 **CODE DONE**, awaiting a user-commanded `:6969` restart.
**Restart-safety gate GREEN (2026-10-10):** full suite on the Py3.11 leg against the dirty tree -- the 3 modified
+ 2 new files here, PLUS 43 unrelated uncommitted `app/authenticity/*` edits (11 of them deleted standards JSONs)
that a restart would also load -- returned **3589 passed, 1 warning, 566.50 s, `PYTEST_EXIT=0`** with zero
`FAILED`/`ERROR` headers (`.brain/scratch/boot_gate_full311_0410.txt`). No `--deselect` was required: `fb96e07`
added the cascade timeout markers, so `test_authenticity_cascade.py` ran inside the suite and passed.
**Why the same 502 was re-reported on 2026-10-10:** the live process is PID **17744**, started
**2026-10-09 17:50:57** -- roughly 3.5 h BEFORE `app/main.py`'s mtime of **21:25:45** -- so it is still executing
pre-fix bytecode. Python binds code at import; a green suite plus an untouched process is NOT a live fix.
This is a process-lifecycle gap, not a second defect.

#### Client error sanitization — transport reprs + combo sentinels never leak (2026-10-14)

Two user-reported 502s, one CLASS of defect: internal exception text reaching the client error bubble.

| Bug | Reported message | Root cause | Fix |
|---|---|---|---|
| **S1** — sentinel leak | `upstream error 502: upstream stream failed: _ComboWrapRestart` | Never-stop combo pass-boundary sentinels (`_ComboWrapRestart`, `_ComboFallbackNeeded`) fell into `afz_guard`'s catch-all `except BaseException` and were terminalized as client-visible 502s. | `afz_guard` re-raises internal sentinels untouched BEFORE the catch-all — sentinels stay control-flow, never become terminal frames. |
| **S2** — raw transport repr | `upstream error 502: midstream_transport: peer closed connection without sending complete message body (incomplete chunked read)` | All three terminal-frame builders forwarded raw httpx exception text verbatim into the client bubble. | All three builders — `gemini.py::terminal_error_frame`, `main.py::_openai_terminal_error_frames`, `main.py::_anthropic_terminal_error_frames` — route the message through `sanitize_client_error`. |

**Sanitizer contract** (`app/antifreeze.py::sanitize_client_error`): noisy transport tokens (`incomplete chunked read`, `peer closed connection`, `server disconnected`, `connection aborted`, `connection reset by peer`, `remote end closed`) collapse to ONE stable sentence — "upstream connection dropped mid-stream" — so clients see a consistent label across every httpx/anyio variant. Sentinel class names map to "internal retry signal". Legit short labels (`upstream_idle_timeout`, `zero_output_tokens`, `stream_stall`, `Upstream error 429: ...`) pass through UNCHANGED. Full transport detail stays in server-side logs; the client bubble is deliberately lossy.

> [!CAUTION]
> **Do not widen the sanitizer into a message rewriter.** It is a de-noise filter with a token list, not a classifier: unknown messages pass through untouched. A future transport failure whose text is not in `_NOISY_TRANSPORT_TOKENS` will still leak a raw repr — add the token, do not re-architect.

Tests: `app/tests/test_combo_wrap_sentinel_not_terminalized.py` (10) — sentinel propagation through `afz_guard`, genuine-transport terminalization, exact-reported-message mapping pins, per-builder sanitization (Gemini / OpenAI / Anthropic). Verified: full suite **3737 passed, 0 failed** on BOTH CI legs (venv311 591.50 s, venv312 607.08 s). The lone dev-venv (Py3.10) failure `test_builtin_timeout_error_post_content_emits_terminal_error_frame` is PRE-EXISTING at HEAD — causality-proven two independent ways: git-stash of the 3 fix files, AND a pristine detached worktree at HEAD `1f67cc9` (verified 0 `raw_decode` / 0 `sanitize_client_error` hits, zero parallel-session contamination) that reproduces the identical `models=['m_partial','m_partial']` failure — and passes on both CI legs — the Py3.11+ `builtins.TimeoutError` aliasing artifact of AGENTS.md section 5. Status: 🔨 **CODE DONE**, awaiting a user-commanded `:6969` restart (code on disk != live code).

#### Judge tie-break JSON extraction — raw_decode (2026-10-15)

`judge_rank.py::rank_candidates` extracted the judge's JSON verdict with
`content.find("{")` .. `content.rfind("}")` + `json.loads`. When a judge model emitted a second
JSON object or brace-bearing prose after the verdict, the slice spanned both and raised
`json.JSONDecodeError: Extra data` — and because the judge is fail-open by contract, the tie-break
silently never fired (the fable-5 / sonnet-5-5 cross-family inversion symptom). Fix: one line —
`json.JSONDecoder().raw_decode(content, start)` parses exactly ONE complete object from the first
`{` and ignores the tail. Fail-open wrapper, idx/conf validation unchanged. Tests:
`app/tests/test_judge_rank_json.py` (4) — trailing second object, trailing brace-prose, clean
object, no-braces → None. Verified on the .venv311 CI leg together with the full suite
(3737 passed / 0 failed, 591.5 s). Status: 🔨 **CODE DONE**, live with the next `:6969` restart.

**Decisive-lane live verification (2026-10-15):** a scoped in-process run (tokenizer +
knowledge_bank only, live mode k=1, 63 calls across the three mintrouter Claude listings via
`:6969`, no router restart) confirmed the decisive lanes discriminate as designed: sonnet-5-5
and fable-5-1 both matched their own frozen standards (namesake CORRECT), while the
"opus-5-5" listing fingerprinted as sonnet-5-5 (namesake MISMATCH; tokenizer baseline 5416 in
the sonnet/fable ~6.2–6.4k band, not canonical opus 5242). No tie occurred, so the
`raw_decode` path itself was not exercised live — it remains covered by
`test_judge_rank_json.py`. The decisive-lane evidence is exactly the signal FIX F's
`judge_pick_contradicted` guard reads; the original inversion (judge picking fable over opus on
style) cannot survive these lanes once the judge tie-break is reachable.

**a6api swap (2026-10-15):** the same decisive-lane harness was run against
`a6api/claude-opus-5-5` before swapping (21 calls): it ALSO fingerprints as sonnet-5-5-class
(ranked sonnet 0.8163 > opus 0.7332; tokenizer baseline 337, jitter 0 - a different provider
overhead profile from mintrouter's ~5.4k, but the same sibling ordering). The user accepted the
swap anyway: mintrouter's `claude-opus-5-5` row is now disabled and a6api's enabled, applied
live through the `GET`/`POST /api/config` round-trip (no restart; provider/connection counts
verified unchanged at 73/92). Bare `claude-opus-5-5` remains enabled under VSLLM-Claude and
Xpiki as well, so bare-id requests still resolve across three publishers; the namespaced
`a6api/claude-opus-5-5` pins a6api via the pass-1 exact published-ID match in
`_resolve_namespaced_model`.

**RETRACTION (2026-10-15):** the mismatch verdicts above are harness artifacts, not evidence about
the providers. Three defects, found by zero-cost disk-only re-analysis: (1) `similarity_tokenizer`
derives `slack = 1 + sus.jitter + base.jitter`, and the canonical sonnet standard recorded
jitter=3 while opus recorded 0 — every suspect therefore gets 4 tokens of free tolerance against
sonnet and only 1 against opus. Recomputed at matched slack, mintrouter opus vs canonical opus
goes 0.721 → **0.900** (sonnet 0.923, margin +0.023) and a6api opus 0.515 → **0.805** (sonnet
0.843, margin +0.038); both fall under the 0.05 decisive threshold → ABSTAIN. (2)
`pairwise_separability` already reports opus-vs-sonnet distance 0.098 < `SEPARABLE_DISTANCE`
0.12 — an ALIAS group the code documents as "no black-box probe in this standard can tell them
apart"; the verdicts were built on a lane the harness declares non-discriminating. (3) The
tokenizer lane measures the PROVIDER's tokenizer, not the model: all three live mintrouter
listings converged to identical deltas (digits_x4=69, emoji=30, arabic=20) where the 2026-10-09
standards still differed. Note also that `meta.anchor_lane` for the canonical opus standard is
`mintrouter/claude-opus-5-5` — mintrouter is ground truth by construction and matches that
standard better (0.900) than a6api does (0.805). Full analysis: `.brain/CHANGELOG.md`
2026-10-15 retraction entry. **FIXED same day:** the slack tilt itself is corrected in
[standard_v2.py](../app/authenticity/standard_v2.py) — `compute_v2_dims` now ranks every
candidate at one matched tolerance via `_common_tokenizer_slack`, and a new
`tokenizer_decisive` guard abstains when two siblings land within `TOKENIZER_DECISIVE_GAP`.
See `.brain/CHANGELOG.md` 2026-10-15 fix entry.

#### Standard v2 lane inventory: `spatial_curve` (Phase A) + `token_jsd` (Phase B) — CODE DONE (2026-10-19)

Two additional v2 evidence lanes now feed `compute_v2_dims` and the matcher, aimed squarely at the
2-candidate tie-cluster / high-vs-low-tier ambiguity that made the verifier abstain on most
same-family sibling pairs. Both are **offline-verified only** — no harvested standard carries the
new axes yet, so both lanes report `lane_returned_empty` on live runs until a targeted backfill
lands (see *Backfill discipline* below). **Status: CODE DONE, awaiting per-model lane backfill;
never full-re-harvest to fill one lane** (KI `ki-bslrouter-targeted-backfill-never-full-reharvest`).

| Lane | Signal source | Extra paid calls | Implementation |
|---|---|---|---|
| `spatial_curve` (Phase A) | 3-D ray-occlusion curve over the model's own reasoning/answer geometry | none (reuses the harvested probe set) | `_sp_ray_occlusion` (ray parameter `t1` now on the correct side of near-face depth `tn`) + `similarity_curve(..., min_shared=3)` |
| `token_jsd` (Phase B) | Jensen–Shannon divergence over harvested **logprob profiles** (distribution shape, not argmax) | none — reuses existing `logprob_profile` data | `_logprob_profile_of` → `_top_dist` → `_jsd_pair` → `similarity_token_jsd` |

Wiring — **all five sites must carry a lane, or it is silently dead**: `compute_v2_dims` and
`pairwise_separability` in [standard_v2.py](../app/authenticity/standard_v2.py); the `_d2`
collection loop + `V2_VETO` soft veto in [matcher.py](../app/authenticity/matcher.py);
`DECISIVE_LANES` + `FEATURE_WEIGHTS` in [judge_rank.py](../app/authenticity/judge_rank.py);
`_V2_LANES` + `lane_status` in [gate.py](../app/authenticity/gate.py). The rebalanced weights sum
to `1.0000000000000002` in IEEE754, which is why `match_submodel` rounds `match_score` /
`runner_up_score` to 3 decimals at the `MatchResult` boundary (matching the long-standing rounding
of `ranked[].score` and every dim). Rounding is presentation-only — abstain decisions are made from
the pre-round scores.

*Backfill discipline:* one missing lane → `scripts/backfill_v2_lane.py --lane-name <lane>`, which
merges into the existing standard and leaves judge axes + other lanes untouched. Full re-harvest is
the last resort and needs an explicit user command (~50-70 paid calls; it has destroyed good judge
axes twice).

Verified: `.venv311` + `.venv312` — 70 passed (`test_standard_v2_spatial_curve.py` +
`test_authenticity_cascade.py` + `test_authenticity_standard_v2.py`) and 282 passed on the wider
`-k "authenticity or standard_v2 or judge or matcher"` selection, both legs. Details:
`.brain/CHANGELOG.md` 2026-10-19 Phase B entry.

#### Standard v2 lane inventory: `hard_series` — CODE DONE (2026-10-20)

Seventh v2 evidence lane, aimed at the residual sibling-tier ambiguity (Claude Fable/Opus/Sonnet
class). Two halves, both under one `hard_series` key in the v2 dict:

| Half | Signal | Paid calls | Notes |
|---|---|---|---|
| math | 3 tiers of verified hard math (exact-arithmetic mod, constrained counting, sigma/divisor counting); ground truth computed at generation time, exact `ANSWER: <int>` grading | 3 (live) / 6 (harvest, k=2) | judge-free; runs in every mode |
| judged | 3 expert probes (production architecture / self-contained animation script / deep domain) graded 0-10 by an external judge lane | 3 suspect + 3 judge calls | **harvest-only** — runs only when `run_standard_v2(..., judge=(endpoint, key, model))` is given; the gate's live pass passes nothing and gets the math half only (no `judged` sub-key) |

The judge call is implemented **inside**
[standard_v2.py](../app/authenticity/standard_v2.py) (`_judge_hard_answer` — the module cannot
import harvest: harvest already imports standard_v2, so the reverse edge would be circular). It is
fail-open: judge HTTP/parse errors omit that axis, never raise. `similarity_hard_series` means the
two halves both sides carry and abstains below 3 shared components. `FEATURE_WEIGHTS` rebalanced to
fund `hard_series=0.11` (cutoff .15→.11, knowledge_bank .13→.11, capability_curve .11→.10,
tokenizer .05→.04, discriminator .13→.11, refusal .05→.04; `spatial_curve` pinned at 0.10 by its
existing test) — sum stays exactly 1.0 — and `hard_series` joined `DECISIVE_LANES`. Backfill:
`scripts/backfill_v2_lane.py --lane-name hard_series [--judge <model>]` (k=2, judge defaults to
`codex/gpt-6-astra`). **Status: CODE DONE, offline-verified only; no harvested standard carries the
lane yet.** Verified offline: 26/26 `test_standard_v2_hard_series.py`, 27/27
`test_authenticity_standard_v2.py` (lane_gaps pins extended), spatial/discriminator/cascade 66/66,
`.venv` (Py3.10).

#### Gemini 4 argon rides the thinkingLevel enum contract (2026-10-15)

`gemini-4-argon` (a6api / tokenforge `gemini-web/*` / aibox entries in `config.yaml`) previously
fell through the `gemini-3` contract pattern (`gemini.*3|gemini-pro-agent`) into `gemini-legacy`'s
numeric-budget path: `thinking: low` silently became an 8192-token `thinkingBudget` instead of the
`thinkingLevel` enum the model speaks, and the admin UI only offered off/16k/32k.

The enum contract now covers Gemini 4: pattern widened to `gemini.*[34]|gemini-4-argon|gemini-pro-agent`
in [families/gemini.py](../app/compat/families/gemini.py) (contract + legacy exclude), mirrored in
`_legacy_reference.py` and the UI spec in [app.js](../app/static/app.js) (`getThinkingSpec`).

- Vocabulary: off / low / medium / high / max (xhigh coerces to max; legacy budget words map via
  `_LEVEL_FALLBACK` — the live aibox `thinking: 32k` entry resolves to medium).
- Transport shapes unchanged: native gemini → `generationConfig.thinkingConfig.thinkingLevel` +
  `includeThoughts`; openai gateway → `reasoning_effort`; anthropic-shaped → `thinking.{type,
  budget_tokens}`.
- `thinking: auto` still emits nothing (effort_is_explicit gate) — zero behavioral change until a
  level is selected.

Status: 🔨 CODE DONE, awaiting a user-commanded `:6969` restart.

## Middleware Pipeline

The routing pipeline consists of 36 middleware modules. Here's the full inventory:

### Classification Layer
| Module | Purpose |
|---|---|
| `request_intent.py` | Classifies request as chat, code, or agent |
| `category_classifier.py` | 13-way category classification |
| `coding_category_classifier.py` | 8-way coding-specific classification |
| `task_complexity.py` | Estimates task complexity for effort tier selection |

### Routing Layer
| Module | Purpose |
|---|---|
| `route_registry.py` | Centralized route definitions |
| `bsl_chat_router.py` | Tier 1: Chat routing (blacksand-chat) |
| `bsl_lite_router.py` | Tier 2: Lite coding routing (blacksand-lite) |
| `bsl_agentic_router.py` | Tier 3: Agentic routing (blacksand-agentic) |
| `bsl_agentic_ultra_router.py` | Tier 4: Ultra routing with consult |
| `bsl_agentic_max_router.py` | Tier 5: Max multi-domain fusion |
| `bsl_auto_select.py` | Automatic model selection + preset management |
| `bsl_router_utils.py` | Shared routing utilities |

### Orchestration Layer
| Module | Purpose |
|---|---|
| `bsl_orchestrator.py` | Multi-agent orchestration engine |
| `bsl_orchestrator_engine.py` | Core orchestration execution |
| `bsl_orchestrator_gates.py` | Depth escalation gates (fast -> balanced) |
| `blacksand_orchestrator.py` | Balanced-mode orchestration loop: one client request → N internal phase calls (lead + parallel members, cap 22) |

### Quality & Efficiency Layer
| Module | Purpose |
|---|---|
| `quality.py` | Response quality gates (truncation detection + retry) |
| `efficiency.py` | Opus efficiency optimization (token budget management) |
| `compaction.py` | Context window management (compaction before overflow) |
| `thinking_fallback.py` | Reasoning parameter fallback |
| `response_format_guard.py` | Response format validation |

### Stream Protection Layer
| Module | Purpose |
|---|---|
| `stream_guard.py` | SSE stream integrity validation |
| `caching.py` | Prompt caching injection (4 strategies) |
| `glm_tools.py` | GLM tool call translation |

### Benchmark Sheets
| Module | Purpose |
|---|---|
| `bsl_benchmark_sheet.py` | Chat tier benchmark data |
| `bsl_lite_benchmark_sheet.py` | Lite tier benchmark data |
| `bsl_agentic_benchmark_sheet.py` | Agentic tier benchmark data |

### Tool Hygiene & Repair Layer (2026-09 wave)
| Module | Purpose |
|---|---|
| `anthropic_tools.py` | Same-dialect (Anthropic→Anthropic) tool-call hygiene; repairs GLM-5.x malformed tool-argument JSON |
| `tool_arg_repair.py` | Dialect-agnostic JSON-repair ladder for tool-argument strings, shared by all 4 format lanes |
| `glm_parallel_guard.py` | Injects `disable_parallel_tool_use` into the final Anthropic payload for GLM-family targets (fixes dropped tool args in multi-tool batches) |
| `agentrouter_policy.py` | AgentRouter provider policy: Vietnamese-content preflight (VN text triggers upstream 400 content-blocked) |

### Execution Integrity Layer
| Module | Purpose |
|---|---|
| `faithful_execution.py` | FEL directive builder (pure) — counters false refusals, skipped steps, dropped tool calls, premature stops |
| `fel_wiring.py` | FEL backend wiring into the live request path (default OFF, fail-open — see FEL section) |
| `normalizer_shadow.py` | Normalizer v2 shadow/active round-trip wiring (default OFF, one JSONL record per request) |
| `responses_thread_store.py` | Responses API thread store — `previous_response_id` emulation (default OFF) |
| `gemini_last_role.py` | Appends a minimal user continuation when the trailing role would be rejected by Gemini `generateContent` |

---

## Faithful Execution Layer (FEL)

The Faithful Execution Layer counters four recurring agent failure modes on the model fleet: **false refusals** on legitimate professional work, **skipped steps**, **dropped tool calls**, and **premature stops**. It operates exclusively through positive-framing capability/completion contracts appended to the system-prompt tail — never obedience-forcing or jailbreak-style language (enforced by import-time forbidden-vocabulary self-check in `app/middleware/faithful_execution.py`).

### Pipeline stages (wired in `app/main.py` via `app/middleware/fel_wiring.py`)

| Stage | Where | What happens |
|---|---|---|
| Pre-flight (clarity) | Before dispatch | Vocabulary-map rewrite + engagement-context prepend on the last user message; ambiguity flagged log-only |
| Egress (directives) | Payload final per wire shape | Directive block merged into the system tail (openai-chat system message / responses `instructions` / gemini `systemInstruction` / anthropic `system`) |
| Recovery | First completed non-stream response | Strict classifier (`classify_refusal` — ≥2 distinct markers, <600 chars, no tool calls) sees a refusal → ONE router-injected clarification turn re-dispatch; recursion impossible via request.state guard |
| Hard wall (FEL-5) | Inside `classify_refusal`, before the length gate | A refusal-shaped text (≥2 distinct markers) citing `_RECOVERY_BLOCK_MARKERS` (CSAM/weapons-class subject matter) → terminal **`blocked`** verdict, preserved verbatim — recovery never re-dispatches it. Bypasses the <600-char length gate (live evidence 2026-09-07: verbose wall refusals — Kimi 1337ch / Qwen 2248ch — must stay visible to analytics), but never the ≥2-marker refusal-shape gate, so benign technical mentions of block-listed subjects can never classify `blocked` |
| Stream final | Buffered-stream assembly | Classifies assembled visible text, log-only — no mid-stream recovery |

### Gates & config

- Master switch: `tools.fel.enabled` (default **OFF** — disabled/malformed config costs one dict lookup, zero mutation).
- Per-target opt-out: `tools.fel_profiles` (`{provider-or-model: "off"}`) or request header `x-bsl-fel: off`.
- **Per-family toggles (FEL-3)**: `tools.fel.families` — `{cn, gpt, claude, gemini}`, default all-on; unchecked families skip clarity, directives, recovery, and stream classification (`_fel_family_enabled` gate in `app/main.py`, family resolved by `detect_family` from model-id prefix, falling back to provider-name substring).
- **Research modes (FEL-5)**: `tools.fel.research` — `{content, coding}`, both default OFF. Each enabled lane adds ONE research-context directive to the egress merge (content: politics/conflict/sexuality research framing; coding: own-systems security research framing). Hard walls are never softened — a wall refusal in a research lane still classifies `blocked`.
- Everything is fail-open: any exception leaves the payload untouched.

### Response surface

| Header | Meaning |
|---|---|
| `x-bsl-fel: applied` | Egress merged the directive block |
| `x-bsl-clarified` | Pre-flight adjusted the last user message |
| `x-bsl-recovered` | Refusal rewritten into a compliant response |
| `x-bsl-refusal` | Refusal preserved (recovery disabled, failed again, or FEL-5 hard wall) |

### Refusal analytics (FEL-3)

Stage signals ride `request.state` → `obs.note_fel_context()` → `log_request()` attaches them to the SQLite usage row. Two columns on `usage_events` (guarded ALTER migration, NULL for non-FEL traffic):

- `fel` — family tag (`cn` / `gpt` / `claude` / `gemini` / `other`) when FEL engaged
- `refusal_class` — final verdict: `refusal` / `tos_lecture` / `filter_block` / `blocked` / `recovered` / `clean` / `unknown`

`GET /api/observability/usage/summary` returns a `fel` block: `applied` (rows with a family tag), `refusals` (counts by class), `recovered`, `by_family` (`{requests, refusals, recovered}`), and `by_model` (top 8 models by hard-refusal count). The Usage tab renders a **FEL SOFTENING** card (applied / refusals / recovered / filter-block + per-family chips + top refusal models) only when FEL data exists; an empty store yields zeros, never errors.

---

## Admin Dashboard Architecture

The admin dashboard is a single-page app (`app/static/`) with 10 tabs:

| Tab | HTML ID | Purpose |
|---|---|---|
| **Endpoint** | `endpoint` | Local endpoints, Cloudflare Tunnel, Tailscale, API keys, Antigravity integration |
| **Providers** | `providers` | Provider CRUD, connection verification, model management |
| **Combos** | `combos` | Combo alias definition and fallback chain editor |
| **BSL Models** | `bsl-models` | Matrix editor for all 5 Blacksand models, auto-select, read-only cross-references |
| **MITM** | `mitm` | MITM proxy control, hosts file management, watchdog status |
| **Tools** | `tools` | Document Intelligence, Vision Bridge, Token Budget, Prompt Caching |
| **Usage** | `usage` | Per-model usage table with filtering, cost tracking, and a live in-flight request strip |
| **Logs** | `logs` | Live request/response log streaming |
| **Settings** | `settings` | Admin password, shutdown, logout |
| **Authenticity** | `authenticity` | Model Authenticity Test (suspect/judge pickers, verdict report), Frozen Standards admin panel |

### Antigravity Integration (Endpoint Tab)
Direct-inference overlay for Antigravity IDE 2.1.1:
- Maps Antigravity's model slots to BSL Router providers/combos
- No MITM proxy, CA cert, or hosts file needed
- Unmapped slots use native Google Cloud Code
- Diagnostics panel shows real-time integration status

---

## Remote Access

| Method | How it works |
|---|---|
| **Cloudflare Tunnel** | Runs `cloudflared` to create a secure public URL. Managed from the Endpoint tab. |
| **Tailscale** | Detects Tailscale IP for mesh network sharing. One-click URL retrieval. |
| **API Keys** | Generate scoped keys for other applications. Keys are stored in config under `keys: []`. |

---

## Project Structure

```
bsl-router/
├── app/
│   ├── main.py                  # Server entry point + all HTTP routes
│   ├── models.py                # Data models (Pydantic)
│   ├── config_state.py          # Config loading and hot-reload
│   ├── crypto.py                # Fernet encryption/decryption
│   ├── oauth.py                 # OAuth 2.0 flows + token refresh
│   ├── normalizer.py            # Response normalization
│   ├── observability.py         # Request/response logging
│   ├── antifreeze.py            # Stream deadline + kill registry
│   ├── mitm.py                  # MITM proxy management
│   ├── compat/                  # Protocol translation
│   │   ├── families/            # 14 provider family adapters
│   │   ├── stream_normalizer.py # Stream format normalization
│   │   ├── tool_ledger.py       # Tool call ID tracking
│   │   └── reasoning_policy.py  # Thinking parameter rules
│   ├── middleware/              # 36 routing pipeline modules
│   │   ├── bsl_chat_router.py   # Tier 1: Chat
│   │   ├── bsl_lite_router.py   # Tier 2: Lite
│   │   ├── bsl_agentic_router.py       # Tier 3: Agentic
│   │   ├── bsl_agentic_ultra_router.py # Tier 4: Ultra
│   │   ├── bsl_agentic_max_router.py   # Tier 5: Max
│   │   ├── bsl_orchestrator.py         # Multi-agent orchestration
│   │   ├── bsl_auto_select.py          # Auto model selection
│   │   ├── category_classifier.py      # 13-way classification
│   │   ├── coding_category_classifier.py # 8-way coding classification
│   │   ├── task_complexity.py          # Complexity estimation
│   │   ├── route_registry.py           # Central route definitions
│   │   ├── stream_guard.py             # SSE integrity validation
│   │   ├── caching.py                  # Prompt caching (4 strategies)
│   │   ├── quality.py                  # Quality gates
│   │   ├── efficiency.py              # Opus efficiency
│   │   ├── compaction.py              # Context management
│   │   ├── thinking_fallback.py       # Reasoning fallback
│   │   ├── glm_tools.py               # GLM tool translation
│   │   └── ...                        # + 18 more modules
│   ├── routing/
│   │   └── combo_resolver.py    # Combo chain resolution + fallback
│   ├── scouts/                  # Vision, docs, canvas analysis
│   ├── security/                # Key scanning
│   ├── static/                  # Admin dashboard (HTML/JS/CSS)
│   └── tests/                   # 147 test files
├── scripts/
│   ├── bslrouter.ps1            # Unified launcher (port-kill retry)
│   └── update_bsl_router.py     # GitHub auto-update script
├── config.example.yaml          # Config template
└── requirements.txt             # Python dependencies
```

---

## Configuration Reference

BSL Router uses a single `config.yaml` file. Key sections:

```yaml
# AI Providers
providers:
  my-openai:
    type: custom                 # custom | image_custom
    format: openai               # openai | anthropic | gemini
    connections:
      - api_key: enc:YOUR_KEY    # Encrypted API key
        base_url: https://api.openai.com/v1
    models:
      - id: gpt-4o
        enabled: true
        thinking: auto           # auto | max | off

# Combo aliases (referenced by Blacksand matrix)
combos:
  - alias: coder-1               # Fast/cheap tier
    chain:
      - provider: my-openai
        model: gpt-4o-mini
    strategy: fallback

  - alias: coder-3               # Strongest tier
    chain:
      - provider: my-anthropic
        model: claude-sonnet-4
    strategy: fallback

# Blacksand routing matrices
bsl_models:
  bsl_chat:
    enabled: true
    category_overrides:
      general:
        fast: "coder-1"
        standard: "coder-2"
        strong: "coder-3"
    default_route: "coder-2"
    global_last_fallback: "coder-1"
  bsl_lite:
    enabled: true
    # ... similar structure

# Tools & Intelligence
tools:
  docs_parser_enabled: false
  docs_skip_threshold: 8000
  docs_summary_model: "gpt-4o-mini"
  vision_bridge_enabled: false
  vision_bridge_model: "gpt-4o-mini"
  vision_max_tokens: 1024
  max_tokens_budget_enabled: false
  max_tokens_budget: 65535
  caching_anthropic_explicit: false
  caching_kimi_key_bound: false
  caching_static_sort: false
  caching_openai_key_bound: true
  # Faithful Execution Layer (see FEL section above) — default OFF
  fel:
    enabled: false
    refusal_recovery: { enabled: true, max_recoveries: 1 }
    clarity: { enabled: true, vocabulary_map: {} }
    families: { cn: true, gpt: true, claude: true, gemini: true }

# Antigravity IDE integration
antigravity_integration:
  enabled: false
  mappings:
    slot_key: "combo-alias-or-provider/model"

# Admin
admin:
  password_enabled: false
  password: enc:YOUR_ADMIN_PASS

# API keys for external access
keys: []
```

See [config.example.yaml](../config.example.yaml) for a complete working template.

---

## API Reference

### For AI Clients (drop-in compatible)

| Endpoint | What it does |
|---|---|
| `POST /v1/chat/completions` | Chat completions (OpenAI format) |
| `POST /v1/messages` | Messages (Anthropic format) |
| `GET /v1/models` | List all available models (includes Blacksand virtual models) |
| `GET /health` | Health check |

### For the Admin Dashboard

| Endpoint | What it does |
|---|---|
| `GET/POST /api/config` | Read or update configuration |
| `GET/POST /api/scan-keys` | Run the security scanner |
| `GET/POST /api/mitm/*` | Control MITM proxy |
| `GET /api/observability/usage` | View usage statistics |
| `GET /api/observability/usage/inflight` | Live in-flight requests |
| `POST /api/tunnel/cloudflare/*` | Manage Cloudflare tunnels |
| `GET/POST /api/bsl-matrix/*` | Read/apply Blacksand matrix config |
| `GET/POST /api/antigravity/*` | Antigravity integration control |
| `POST /api/auth/login` | Admin login |
| `GET /api/authenticity/standards` | List frozen TraitMark standards |
| `POST /api/authenticity/standards/harvest` | Harvest a new standard from a trusted anchor (background) |
| `GET /api/authenticity/standards/harvest/status/{model_id}` | Poll harvest job status |
| `DELETE /api/authenticity/standards/{family}/{sub_model}` | Delete a frozen standard |
| `POST /api/authenticity/test` | Submit an authenticity gate job (auto = normal → escalate to deep on ambiguous). Returns **202 Accepted** immediately; verdict arrives via the status endpoint |
| `GET /api/authenticity/test/status/{model_id}` | Poll job step feed + terminal verdict (`status`: idle/running/done/error/cancelled) |
| `POST /api/authenticity/test/cancel/{model_id}` | Cancel a running job: sets the cancel flag, writes terminal `cancelled` status, and cancels the background task |

---

## Dependencies

| Package | Why it's needed |
|---|---|
| **FastAPI** | Web framework for the API server |
| **httpx** | Async HTTP client for upstream requests |
| **mitmproxy** | Transparent proxy (optional, for MITM mode) |
| **cryptography** | Fernet encryption for API keys |
| **PyYAML** | Config file parsing |
| **pydantic** | Request/response data validation |

---
---

# 🇻🇳 Tiếng Việt

---

## Tổng Quan

BSL Router làm **bốn việc**:

1. **Nhận** request từ app AI của bạn
2. **Phân loại** để xác định tầng định tuyến tốt nhất
3. **Dịch** nó sang đúng format của provider đích
4. **Gửi** lên provider và stream response về

Nếu provider lỗi, nó tự động thử provider tiếp theo trong chuỗi dự phòng.

---

## Điều Gì Xảy Ra Khi Bạn Gửi Request

Hành trình đầy đủ của một request chat, từng bước:

### Bước 1: Request Đến
App gửi `POST /v1/chat/completions` (hoặc `/v1/messages` cho format Anthropic).

### Bước 2: Kiểm Tra Xác Thực
Nếu bật admin auth, BSL Router xác minh session. Request API dùng BSL key.

### Bước 3: Phân Loại Request
BSL Router phân loại qua nhiều lớp:
- **Intent**: chat đơn giản, sinh code, hay agentic tool use?
- **Danh mục**: 13 danh mục (general, technical, creative, scout, power_coder, vision, v.v.)
- **Coding category**: 8 sub-category cho coding (fast_coder, architect, reviewer, v.v.)
- **Complexity**: ước lượng độ phức tạp để chọn effort tier (fast/standard/strong)

### Bước 4: Chọn Provider
- Nếu chỉ định **Blacksand model** -> định tuyến qua ma trận 5 tầng
- Nếu chỉ định **combo** -> bắt đầu từ provider đầu chuỗi
- Nếu chỉ định model cụ thể -> dùng provider của model đó
- Nếu bật auto-select -> chọn theo năng lực + chi phí

### Bước 5: Dịch Giao Thức
BSL Router dịch: format tin nhắn, tool call ID, tham số thinking, streaming protocol, GLM tool translation.

### Bước 6: Gửi Lên Provider
Kèm: connection pooling, timeout management, OAuth refresh, circuit breaker.

### Bước 7: Stream Response
Chuẩn hóa format stream, kiểm tra tính toàn vẹn (Stream Guard), xử lý thinking blocks, áp dụng quality gates.

### Bước 8: Dự Phòng (nếu cần)
Nếu provider lỗi -> thử provider kế tiếp. Bộ đếm **chain deadline** ngăn thử lại vô hạn.

---

## Blacksand Model Routing

Hệ thống định tuyến 5 tầng - tính năng signature của BSL Router. Thay vì chọn model cụ thể, bạn trỏ client vào tên model ảo.

### 5 Tầng Định Tuyến

| Tầng | Model | Mục đích | Ma trận |
|---|---|---|---|
| 1 | `blacksand-chat` | Chat chung, hỏi đáp | 13 danh mục × 3 tầng |
| 2 | `blacksand-lite` | Coding-agent tác vụ đơn | 10 coding agent × 3 tầng |
| 3 | `blacksand-agentic` | Agentic coding tầng nhanh | Dispatch đa agent |
| 4 | `blacksand-agentic-ultra` | Coding cân bằng + consult | Agent + consultant |
| 5 | `blacksand-agentic-max` | Fusion đa domain | Điều phối cross-domain |

### Combo Alias

| Alias | Tầng | Dùng cho |
|---|---|---|
| `coder-1` | Nhanh | Hoàn thành nhanh, tác vụ đơn giản |
| `coder-2` | Tiêu chuẩn | Coding chung, cân bằng |
| `coder-3` | Mạnh nhất | Reasoning phức tạp, kiến trúc |

### Ma Trận Cấu Hình

Ma trận cấu hình trong `config.yaml` dưới `bsl_models`. Mỗi slot chấp nhận combo alias hoặc provider/model trực tiếp. **Auto-Select** tự điền slot trống. Slot chưa cấu hình -> `default_route` -> `global_last_fallback`.

---

## Thành Phần Chính

### 🔄 Tầng Dịch Giao Thức
14 family adapter: openai, anthropic, gemini, deepseek, glm, kimi, minimax, qwen, grok, openrouter, doubao, hunyuan, kat_coder, muse + infrastructure adapters (_base, _effort, _legacy_reference).

### 🔗 Combo/Chain Dự Phòng
Thử lần lượt provider. Chain deadline ngăn timeout cascading.

### 🛡️ Anti-Freeze
| Thành phần | Bảo vệ |
|---|---|
| Stream Hard Deadline | Cap 10 phút mỗi stream |
| Chain Deadline | Tổng budget tất cả hop |
| Circuit Breaker | Tự xoay provider lỗi |
| Stream Guard | Bắt chunk SSE lỗi |
| Thinking Fallback | Fallback tham số reasoning |

### 🧰 Tools & Intelligence
- **Document Intelligence**: parse PDF/DOCX/XLSX/PPTX + tóm tắt
- **Vision Bridge**: polyfill vision cho model không hỗ trợ
- **Token Budget**: trần max_tokens cứng
- **Prompt Caching**: 4 chiến lược (Anthropic, Kimi, OpenAI, static-first)

### 🔐 Quản Lý Thông Tin Đăng Nhập
API key mã hóa Fernet, OAuth tự refresh, key scanner.

### 🛡️ Bộ Quét Bảo Mật
Kiểm tra: exfil URL, key injection, URL spoofing, credential leakage, local network exfil, insecure transport, token tampering, duplicate keys.

### 🕵️ MITM Proxy (Tùy chọn)
Chặn traffic cho app có URL cố định. Watchdog tự restart. Tree-kill + verify+retry.

### 📊 Quan Sát & Ghi Log
JSONL logging, usage stats, error tracking, live log streaming, và registry request đang chạy (`GET /api/observability/usage/inflight`) hiển thị dải pulse trên tab Usage.

### 🔍 Scouts (Tùy chọn)
Vision (phân tích ảnh), Docs Parser (trích text), Canvas (UI/canvas).

---

## Middleware Pipeline

36 module middleware:

**Classification**: request_intent, category_classifier, coding_category_classifier, task_complexity

**Routing**: route_registry, bsl_chat_router, bsl_lite_router, bsl_agentic_router, bsl_agentic_ultra_router, bsl_agentic_max_router, bsl_auto_select, bsl_router_utils

**Orchestration**: bsl_orchestrator, bsl_orchestrator_engine, bsl_orchestrator_gates, blacksand_orchestrator

**Quality & Efficiency**: quality, efficiency, compaction, thinking_fallback, response_format_guard

**Stream Protection**: stream_guard, caching, glm_tools

**Benchmark**: bsl_benchmark_sheet, bsl_lite_benchmark_sheet, bsl_agentic_benchmark_sheet

**Tool Hygiene & Repair** (đợt 2026-09): anthropic_tools, tool_arg_repair, glm_parallel_guard, agentrouter_policy

**Execution Integrity**: faithful_execution, fel_wiring, normalizer_shadow, responses_thread_store, gemini_last_role

---

## Cấu Trúc Dự Án

Xem [phần English](#project-structure) - cấu trúc thư mục giống nhau.

---

## Tham Chiếu Cấu Hình

```yaml
providers:        # Provider AI
combos:           # Combo alias (coder-1/2/3)
bsl_models:       # Ma trận 5 Blacksand model
tools:            # Document Intelligence, Vision, Token Budget, Caching
antigravity_integration:  # Antigravity IDE overlay
admin:            # Mật khẩu quản trị
keys: []          # API key cho external access
```

Xem [config.example.yaml](../config.example.yaml) để có template hoàn chỉnh.

---

## Tham Chiếu API

### Cho App AI

| Endpoint | Chức năng |
|---|---|
| `POST /v1/chat/completions` | Chat (format OpenAI) |
| `POST /v1/messages` | Messages (format Anthropic) |
| `GET /v1/models` | Liệt kê model (bao gồm Blacksand virtual models) |
| `GET /health` | Kiểm tra sức khỏe |

### Cho Trang Quản Trị

| Endpoint | Chức năng |
|---|---|
| `GET/POST /api/config` | Đọc/cập nhật cấu hình |
| `GET/POST /api/scan-keys` | Quét bảo mật |
| `GET/POST /api/mitm/*` | Điều khiển MITM |
| `GET /api/observability/usage` | Thống kê sử dụng |
| `GET /api/observability/usage/inflight` | Request đang chạy trực tiếp (in-flight) |
| `POST /api/tunnel/cloudflare/*` | Cloudflare tunnel |
| `GET/POST /api/bsl-matrix/*` | Ma trận Blacksand |
| `GET/POST /api/antigravity/*` | Antigravity integration |
| `POST /api/auth/login` | Đăng nhập quản trị |

---

## Thư Viện Phụ Thuộc

| Package | Vì sao cần |
|---|---|
| **FastAPI** | Web framework |
| **httpx** | HTTP client async |
| **mitmproxy** | Proxy trong suốt (tùy chọn) |
| **cryptography** | Mã hóa Fernet |
| **PyYAML** | Đọc file config |
| **pydantic** | Kiểm tra dữ liệu |

## Authenticity Standard v3 — `discriminator` lane (2026-10-09, CODE DONE; rebuilt 2026-10-10)

Fourth v2-dimension added to `app/authenticity/standard_v2.py`, stored under
`structural.tokenizer_signature.v2.discriminator` with shape
`{"answers": {probe_id: [bucket * k]}, "legacy_features": {probe_id: [schema_features * k]}, "k": k}`.
`answers` holds `feature_bucket` whole-answer digests (PRIMARY compared field);
`legacy_features` holds the old `schema_features` strings for offline comparison only.

**Why (status: DONE, offline-verified; live re-scoring of existing standards PENDING):**
tokenizer / capability_curve / knowledge_bank all saturate at the frontier, so
`pairwise_separability` under-reported distance and violated transitivity
(terra->astra 0.046 < terra->sol 0.075). The discriminator measures
behavioural response habits across a 10-probe / 4-family battery
(behavioral boundary / temporal-cutoff / reasoning style / style self-consistency).

**2026-10-10 battery rebuild:** the original 10 `sch_*` probes were audited 0/10
stable+separable (over-constrained "JSON only" formats collapsed siblings onto one
output; the `schema_features` prose prefix-branch kept only the first 6 words, which
share safety-trained openers). The battery now constrains the QUESTION, never the
output format, and `feature_bucket` (sha1-10 digest over the whole normalized answer,
prefixed by a human-inspectable shape code) replaces prefix features as the compared
field. `schema_features` is kept byte-identical and stored side-by-side as
`legacy_features` for offline comparison.

**Scoring:** `similarity_discriminator` = fraction of shared probes whose per-run
bucket lists overlap exactly (`None` when <5 shared). `pairwise_separability` folds it
into the mean alongside the other three; absent lanes are excluded, so standards
harvested before v3 remain fully scoreable — no migration required.

**2026-10-11 informative-filter correction:** `informative_discriminator_items` selects
which probes are allowed to contribute to the score, so that probes on which every model
answers identically (pure noise floor) cannot dilute the few probes that actually diverge.
The previous implementation took a **33-way `set.intersection`** over the candidate pool's
probe IDs, plus a single-anchor disjointness check. On the live 33-model pool that
intersection collapsed to the **empty set** (one model missing one probe zeroes the whole
intersection), so the scorer silently fell through to `all_probes` — every probe counted as
informative, and the constant-answer majority pinned similarity near 1.0 for unrelated
models. Replaced with a **union-based, pairwise-disjoint criterion**: a probe is eligible if
it appears in ≥2 candidates, and it is informative if it separates **any** pair of models
(not one fixed anchor). Measured on live standards: informative probes **0 → 18**
(`bb_*`, `tc_*`, `rs_*`, `fs_blue`, `sch_*`); sibling `disc` similarity now spreads
0.125–0.875 with self-match 1.0, instead of a saturated constant. The fix is pure in-memory
set logic — no re-harvest, no backfill, no standard file rewritten (Rule 10).

**Status labels:**
- Lane implementation + wiring + tests — **DONE** (23/23 discriminator, 22/22 standard_v2).
- Informative-probe filter (union-based pairwise-disjoint) — **DONE** (verified against all 60
  live standard files; regression tests in `app/tests/test_authenticity_discriminator.py`).
- Matcher veto near-miss instrumentation — **DONE** (`.brain/scratch/disc_veto_nearmiss.jsonl`,
  capped ~1MB, never raises).
- Backfill `--replace` flag — **DONE** (disjoint keyspace rotates wholesale; overlapping
  merges per-key, new values win; failure leaves the file untouched).
- Existing references + canonicals re-scored with the new battery — **DONE (32/33)**
  (2026-10-11 wave 1: 3 lanes — glm-5-3-flash, qwen3.8-max, qwen3.8-max-0902;
  2026-10-11 wave 2: 29/30 old-battery lanes rotated via targeted
  `backfill_v2_lane.py --lane-name discriminator --replace`, ~20 calls each, k=2;
  NO full re-harvest per Rule 10. Sole holdout: `deepseek-v4.1-flash` — anchor
  lane `aibox/deepseek-v4.1-flash` returned HTTP 503 on every probe
  (`model_not_found`: no available channel in the distributor's default group) and
  the alternate `a6api` lane also 503s (`smart_route_no_active_candidates`), i.e.
  an upstream-wide flash outage; the file was left untouched (fail-closed) and its
  OLD_SCH keyspace shares 0 probes with the new battery → similarity returns None
  → the lane abstains, never penalized. Retry once the aibox channel recovers.
  27 provider snapshots have no discriminator lane and abstain by design,
  excluded from the mean. Census: 60 standard files = 32 NEW_10 + 1 OLD_SCH +
  27 no-lane, measured 2026-10-11.)
- Live matcher/gate calibration — **PENDING** (threshold lives in `V2_VETO["discriminator"]`;
  router `:6969` restart required to load the rebuilt battery — Rule 1, user-commanded only).

- AntiStop continuation-send retry (502 mid-stream hardening, Part B) — **CODE DONE**
  (2026-10-15; live with next `:6969` restart — Rule 1, user-commanded only).
  The post-loop continuation splice re-sends to the SAME upstream; a transient 5xx
  (or transport/timeout error) on that send previously died fail-open and surfaced
  a client-visible 502 after partial content had already been delivered. New
  module-level helper `app/main.py::_open_continuation_with_retry` retries
  send+raise_for_status up to 3 total attempts (0.5s/1.0s backoff) on 5xx,
  httpx.TransportError, httpx.TimeoutException, TimeoutError, asyncio.TimeoutError;
  4xx is NEVER retried (deterministic rejection). Retries only occur before any
  continuation byte is yielded — zero transcript-duplication risk. Final failure
  re-raises into the existing per-site fail-open so stats keep the truthful 502.
  Applied at all 3 splice sites: openai-cont, anthropic-egress-cont, gemini-cont.
  Verified: 37/37 (new retry + midstream continuation + transport-partial-flag)
  and 40/40 (combo-wrap sentinel, stream keepalive, thought-buffer) green on BOTH
  CI legs (.venv311 + .venv312); py_compile clean both legs; pre-push gates PASS.

## Canonical Standard Harvest Protocol — new model / new family (2026-10-15)

The mechanical procedure for onboarding a canonical (gold-standard) TraitMark. Follow it
phase-by-phase with minimum adjustment; do not improvise. A full harvest costs
**~50-70 paid API calls per model** — every phase below exists because a past incident
burned credit or shipped a false verdict when it was skipped.

| Phase | Cost | Output |
|---|---|---|
| 0 — Scope | 0 calls | anchor picked, family registered, siblings censused |
| 1 — Harvest | ~50-70 calls (first time only) | raw TraitMark JSON |
| 2 — Certify | 0 calls (disk-only) | standard is *trusted* to accuse others |
| 3 — Register | 0 calls | catalogs + docs in the same commit |

### Phase 0 — Scope (research; zero API calls)

1. **Anchor selection** — pick from `TRUSTED_ANCHORS` in [scope.py](../app/authenticity/scope.py).
   Gold = direct vendor API via `aibox` (glm, kimi, deepseek, qwen). Reference = real-weights
   OAuth wrappers (claude → `mintrouter`, gpt → `codex`, gemini → `antigravity`).
   Canonical harvests are whitelisted to **codex / antigravity / mintrouter / aibox** only
   (enforced with HTTP 400 in the harvest endpoint; vsllm explicitly banned, 2026-10-08
   directive). If the family has **no trusted anchor → DEFER** (hunyuan/mimo precedent);
   never freeze an untrusted relay's output into a benchmark.
2. **New-family registration** — only when `detect_family()` returns `None`. Add, in one
   commit: `SCOPE` patterns, `_FAMILY_VENDOR` display entry, `JUDGE_SUGGESTIONS` mapping
   (judge MUST be a different family than the suspect), and the `TRUSTED_ANCHORS` row.
   Until then the family verdict is honestly **UNVERIFIABLE** — never "fake".
3. **VARIANT_NOTES research** — knowledge cutoff, parameter count, price tier, and any
   known silent reroute (deepseek v4-pro → v4.1-flash precedent). Record in `VARIANT_NOTES`.
4. **Sibling census** — list existing same-family standards and predict separability.
   opus↔sonnet measures 0.098 < `SEPARABLE_DISTANCE` 0.12 → expect an **ALIAS group**
   ("family confirmed, sub-version ambiguous"), not a pick. Set expectations before spending.

### Phase 1 — Harvest (paid; confirm scope BEFORE the first call)

- **State the per-model call estimate and confirm the smallest scope with the user first.**
  Full harvest ~50-70 calls; judge-axis backfill ~3; single v2 lane ~20.
- **Rule 10 is absolute:** never run a full re-harvest to fix ONE missing probe/axis/lane.
  Judge axis zeroed → `scripts/rejudge_axis.py`. One v2 lane missing →
  `scripts/backfill_v2_lane.py --lane-name <lane>` (merges in place, skips if present,
  leaves judge axes and other lanes untouched). A full re-harvest is the LAST resort and
  requires the user to explicitly say "full re-harvest" in the current message.
- Entry point: `harvest_model(endpoint, api_key, model_id, *, judge_endpoint, judge_api_key,
  judge_model, include_noncoding, source_tag, config)` via
  `POST /api/authenticity/standards/harvest` (background) or `scripts/harvest_reference.py`.
  It NEVER raises — fail-open per layer, gaps land in `meta.harvest_gaps`. Canonical
  (empty `source_tag`) sets `meta.tier="canonical"` and `meta.anchor_lane = model_id`.
- Judge: `suggest_judge(family)` — always a DIFFERENT family; never hardcode.

### Phase 2 — Certify (the gate that was missing; zero-cost, disk-only)

**Executable:** `scripts/certify_standard.py --model <id>` (or `--file <path>`) runs all four
checks and prints PASS / ALIAS / FAIL per gate. Exit 0 = certified, 1 = a gate failed,
2 = WARN/ALIAS only. Full JSON report persists to `.brain/scratch/certify_<model>.json`.

A new standard is NOT trusted to accuse anyone until ALL four checks pass:

1. **Quality grade** — `standard_quality(tm)` returns grade `"full"` (judge-graded noncoding
   + ≥4 layers). No `v2_*:unavailable` gaps in `meta.harvest_gaps` for required lanes.
2. **Jitter sanity** — tokenizer jitter < `UNSTABLE_JITTER` (25). Watch the 2026-10-15 trap:
   a jitter=0 vs jitter=3 sibling pair used to hand the wobblier sibling free ranking
   tolerance (fixed by matched slack via `_common_tokenizer_slack`); verify no sibling pair
   reintroduces asymmetric tolerance.
3. **Separability** — `pairwise_separability` vs EVERY same-family sibling ≥
   `SEPARABLE_DISTANCE` (0.12). Below threshold → record an honest **ALIAS group**;
   never force a decisive pick on a non-discriminating lane (`tokenizer_decisive` /
   `TOKENIZER_DECISIVE_GAP` 0.05 abstains by design).
4. **Self-match** — a suspect probed from the anchor lane MUST match its own standard.
   A MISMATCH here means harness bug, not provider fraud (the 2026-10-15 retraction).

### Phase 3 — Register & publish (same commit, no browser-tab autosave)

1. `save_standard()` writes atomically; filename from `standard_filename(family, sub_model,
   source_tag)`. Canonical = empty `source_tag`; provider-tagged snapshots stay private via
   the `_PRIV` filter and never surface in `list_testable_standards` public output.
2. **Catalog sync in the SAME commit** (argon lesson): if the model is served through an
   OAuth provider, update `app/oauth.py` `OAUTH_PROVIDER_MODELS` AND its manual mirror in
   `app/static/app.js` together. New catalog rows mint `enabled:false` — the user enables
   manually. Never trust a stale browser tab's autosave to carry model rows.
3. If the sibling set changed, re-run `scripts/build_discrimination_index.py`.
4. Docs: dated `.brain/CHANGELOG.md` entry + status label in this file
   (DONE / CODE DONE / PLANNED / PARTIAL — never DONE on build/typecheck alone).
5. Router `:6969` restart is **user-commanded only** (Rule 1). New standards go live on
   restart; until then report "ready, awaiting user-commanded restart".

### Incident → gate mapping (why each gate exists)

| Incident | Gate that now prevents it |
|---|---|
| kimi creative=6 → 0 destroyed by a full re-harvest run for ONE missing lane (2026-10-09) | Phase 1 Rule 10: targeted backfill only; full re-harvest needs explicit user words |
| gemini-4-argon model rows wiped by stale-catalog UI autosave | Phase 3.2: catalog + mirror updated in the same commit, server-side |
| Jitter-tilt false MISMATCH verdicts on mintrouter/a6api opus (2026-10-15, retracted) | Phase 2.2/2.3/2.4: jitter sanity, separability-vs-alias, self-match before trust |
| Standards silently scored 0.5-neutral from flat-key reads (2026-10-08) | Phase 2.1: `standard_quality` grade must be `full`, gaps surfaced not assumed |

## Codex Egress Bridge -- Gemini/Anthropic streaming clients on codex upstreams (2026-10-18)

**Status: DONE** (runtime-verified: boot smoke :6977 200 OK; CI legs Py3.11 + Py3.12 3790/3790 green)

Codex-family upstreams speak the OpenAI **Responses API** SSE dialect (typed events such as `response.output_text.delta`), not chat-completion chunks. Two client egress paths assumed the chat dialect and silently dropped the content:

- **Anthropic streaming egress** (`/v1/messages`, stream): `convert_openai_to_anthropic` received raw Codex chunks and emitted nothing.
- **Gemini drain**: converted Codex frames were not recognized as transcript commits, leaving the drain unterminated/empty.

### Bridge (app/main.py)

- `_anthropic_egress_openai_source` -- wraps the raw upstream source; for codex-format providers it re-chunks the stream through `codex_adapter.responses_sse_to_openai_sse` exactly once, ahead of the Anthropic converter.
- `_tee_codex_response_source` / `_flush_codex_response_tee` / `_write_codex_response_tee` / `_codex_response_tee_enabled` -- capture-side tee of the raw Responses SSE (env-gated) with capped writers kept off the hot path (log-cap invariant, see repo rules).
- `_gemini_frame_commits_transcript` -- lets the Gemini drain treat converted Codex frames as transcript commits so the stream terminates with a proper terminal frame.
- `_CAPTURE_QUEUE_MAX = 256` stays module-level: the lifespan capture queue consumes it at boot; removing it is boot-fatal.

### Error events (app/codex_adapter.py)

- `response.failed` / `response.incomplete` map to parser-safe error chunks (`_error_chunks`) in BOTH `responses_sse_to_openai_sse` and `_codex_event_to_openai_chunk`, including multi-chunk and non-JSON-tolerant variants.

### Tests

- `app/tests/test_codex_adapter_error_events.py` -- failed / incomplete / healthy tool-call / usage SSE events.
- `app/tests/test_gemini_drain_emission.py` -- function-call transcript commitment; env-gated codex tee.
- `app/tests/test_anthropic_egress_codex.py` -- end-to-end Codex Responses SSE -> Anthropic SSE conversion.

### Known issue (pre-existing at HEAD 1dfb1cf; Py3.10 dev-venv only; NOT shipped by this change)

`test_builtin_timeout_error_post_content_emits_terminal_error_frame` fails on Py3.10: builtin TimeoutError (OSError family before the 3.11 aliasing) escapes into the AntiStop continuation path and re-dials the same leaf, tripping the dial-count assertion. The client-visible terminal contract (single finishReason frame + single [DONE], no spliced fallback) still holds. CI legs (3.11/3.12) are unaffected. Tracked as separate fix scope.
