# ⚡ BSL Router `v1.1.1`

### Stop Routing Blind. Stop Losing Context Mid-Stream. Stop Paying Frontier Prices for Downgraded Models.
### *Ngừng Định Tuyến Mù. Ngừng Đứt Mạch Ngữ Cảnh Giữa Chừng. Ngừng Trả Giá Frontier Cho Model Bị Hạ Cấp Ngầm.*

> **🇬🇧 The 100% local, self-hosted AI FinOps, Observability & Routing Command Center engineered for power builders and engineering teams.** Connect **Claude Code (Desktop & CLI)**, **Antigravity IDE**, **Cursor**, and any OpenAI/Anthropic/Gemini client into one intelligent `:6969` control plane — featuring **5-Tier Blacksand Cognitive Routing**, **Session-Sticky KV-Cache Affinity & Multi-Credential Failover**, **1-Button Unified Prompt Caching**, **Faithful Execution Layer (FEL)**, and a built-in **Model SLA & Quantization Forensics Lab (`TraitMark V2/V3`)** that catches silent model downgrades and endpoint drift in minutes.
>
> **🇻🇳 Trung tâm điều phối, tối ưu chi phí (FinOps) và kiểm định chất lượng AI 100% local dành cho kỹ sư thực chiến và đội ngũ phát triển.** Kết nối **Claude Code (Desktop & CLI)**, **Antigravity IDE**, **Cursor**, và mọi ứng dụng OpenAI/Anthropic/Gemini vào một cổng điều khiển `:6969` duy nhất — tích hợp **Định Tuyến Nhận Thức 5 Tầng Blacksand**, **Ghim Phiên Bảo Toàn KV-Cache & Chuyển Đổi Dự Phòng Đa Khóa**, **Smart Prompt Caching 1 Nút Bấm**, **Faithful Execution Layer (FEL)**, và **Phòng Thí Nghiệm Kiểm Định SLA & Độ Chuẩn Xác Model (`TraitMark V2/V3`)**.

[![Version: 1.1.1](https://img.shields.io/badge/Version-1.1.1-00d9a3.svg)](https://github.com/dongden-alt/BSL-Router/releases/tag/v1.1.1)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11+-3776ab.svg)](https://www.python.org/downloads/)
[![Tests: 3830+ Passed](https://img.shields.io/badge/Tests-3830%2B_Passed-brightgreen.svg)](#-verification--engineering-discipline)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.129+-009688.svg)](https://fastapi.tiangolo.com/)

**🇬🇧 [English](#-get-running-in-60-seconds)** · **⚔️ [BSL Router vs. LiteLLM, One-API, Portkey & Local Proxies](#-head-to-head-bsl-router-vs-litellm-one-api-omnirouter--standard-gateways)** · **🇻🇳 [Tiếng Việt](#-tiếng-việt--bản-tiếng-việt-đầy-đủ)**

---

## ⚡ Get Running in 60 Seconds

```bash
git clone https://github.com/dongden-alt/BSL-Router.git
cd BSL-Router
python -m venv .venv

# Windows: .venv\Scripts\activate   |   macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml

# Launch the Dual-Stack Server (:6969)
python -m app.dualstack_serve --port 6969
```

👉 **Open `http://localhost:6969`** to access the **10-Tab Admin Command Center**, connect your provider API keys or workspace credentials, click **Start Integration** for **Antigravity IDE** or **Sync to Claude Code**, and start shipping.

---

## 🔥 Why Builders Switch to BSL Router (6 Production Pain Points Standard Gateways Can't Solve)

Standard AI gateways like **LiteLLM**, **One-API / NewAPI**, **OmniRouter**, and pass-through local proxies were built as *stateless JSON pipes*: they take a request, translate the wrapper, and forward it. When you run real 100k+ token agentic coding workflows every day, **a stateless pipe breaks down in 6 critical ways — and BSL Router solves every single one:**

| # | The Real-World Engineering Pain Point | Why Stateless Gateways & Proxies Fail | How **BSL Router `v1.1.0`** Solves It Permanently |
|---|---|---|---|
| **1** | **"Is an upstream endpoint silently serving a quantized or downgraded sibling model?"** | Zero behavioral verification. When a third-party cloud endpoint or regional deployment experiences silent quantization drift or routes a flagship alias to a smaller sibling model, pass-through gateways forward it blindly while your code quality drops. | **🔬 Built-In Model SLA & Quantization Forensics Lab (`TraitMark V2/V3`):** Audits any endpoint against frozen canonical baselines across tokenizer jitter, capability curves, knowledge needles, **5 forensic V2/V3 lanes** (`discriminator`, `spatial_curve`, `token_jsd`, `hard_series`, plus the judge-free **3-prong `tier_splitter`**: `svg_compiler` + `constraint_sat` + `state_trace`), and a **Two-Stage Intra-Family Matcher** (`_disambiguate_siblings`). Detects silent quantization drift and intra-family tier mismatches (`Flash/Luna/Haiku` vs. `Max/Pro/Opus/Argon`) across all 7 model families in minutes. |
| **2** | **"Naive round-robin load balancing destroys my 100k-token KV prompt cache and triggers rate-limit thrashing!"** | Stateless per-request round-robin sprays consecutive turns of the *same* coding session across different credentials or upstream nodes — wiping KV prompt-cache locality on every turn (forcing 100k+ uncached token re-prefills) and triggering upstream concurrency rate-limit locks. | **🛡️ Session-Sticky KV-Cache Affinity + Exhaustive Per-Leaf Failover:** Assigns the starting credential **per new conversation/session** while pinning all consecutive turns inside an active session to **one sticky upstream lane**. If a credential hits a rate limit (`429`), BSL exhaustively fails over across **every healthy credential on that model first** before advancing to the next model in your fallback combo. |
| **3** | **"I have zero real-time visibility into rolling `5h`/`7d` token budgets or workspace plan tiers."** | Headless proxies offer only raw CLI logs; basic dashboards show static green/red dots and fail mid-stream when a credential hits its rolling quota ceiling. | **📊 Live Dual `5h` + `7d` Quota Telemetry & Plan Tier Badges:** Real-time `5h` and weekly `7d` utilization bars, reset countdowns (`↻ 2h 14m`), `Force Refresh`, structured **Workspace Credential Import**, and color-coded **Plan Tier Badges** (`Free`, `Plus`, `Pro`, `Ultra`, `Pro x5/x20/x50`, `Max 5x/20x`, `SuperGrok`, `Power`, `Vivace`) across 9 provider integrations — with automatic pre-selection of credentials that have remaining headroom. |
| **4** | **"Long-horizon coding sessions stop halfway with lazy `// TODO` placeholders or stall on false-positive DevOps/SecOps terminology."** | Standard gateways pass prompts without execution contracts. Models frequently truncate large file refactors mid-stream, leave unfinished `// TODO` stubs, or misclassify benign cybersecurity/DevOps engineering tasks. | **⚡ Faithful Execution Layer (FEL) `v1.1.0` Overhaul:** Pre-flight starter-word indexed domain-terminology framing (`<0.9ms` per 14 KB — `~223x` faster), cache-aligned positive completion contracts (zero `// TODO` truncation), automated stream-continuity recovery (`_fel_stream_with_recovery` across EN/VI/ZH), full coverage across all 6 streaming paths + native **`gemini-4-argon`**, and an interactive **Dry-Run Preview Sandbox** in the UI. |
| **5** | **"Connecting custom model combos to Antigravity IDE or Claude Code Desktop is brittle and drops streams."** | External `:50999` shims open HTTP `200` streams prematurely (breaking fallback on upstream errors), while raw `/v1/models` endpoints dump 50+ uncurated provider models into Claude Desktop's model picker. | **🖥️ 1-Click Native IDE & CLI Bridge (`Antigravity` + `Claude Code`):** Directly configures Antigravity IDE/Desktop endpoints to talk to `:6969` (zero `:50999` shim, isolated native vs. router HTTP/2 connection pools, automatic stale-socket healing). For Claude Code, mints clean `claude-bsl-*` aliases & dedicated `Opus`/`Sonnet`/`Haiku` tier slots and syncs a curated picker directly into `~/.claude/settings.json` and Claude Desktop's `inferenceModels`. |
| **6** | **"My inference bill is exploding because every model vendor uses an incompatible prompt-caching protocol."** | Engineers have to maintain separate logic for Anthropic `cache_control`, OpenAI `prompt_cache_key`, Kimi, and DeepSeek — and stateless `/responses` calls frequently drop cache keys altogether. | **💰 1-Button Unified `Smart Prompt Caching` (All 2026 Families):** One master switch auto-injects 3-stage `cache_control` breakpoints (`tools[-1]`, `system`, rolling history) on Anthropic/GLM/MiniMax, propagates deterministic `prompt_cache_key` across OpenAI (`GPT-5.6/6.1`), `/responses`, and Kimi (`K3*`), and anchors static prefixes for DeepSeek (`V4*`), Gemini (`4*`/`Argon`), and Qwen. |

---

## ⚔️ Head-to-Head: BSL Router vs. LiteLLM, One-API, OmniRouter & Standard Gateways

| Capability | **⚡ BSL Router `v1.1.0`** | **LiteLLM** | **One-API / NewAPI** | **OmniRouter** | **Headless CLI Proxies** |
|---|---|---|---|---|---|
| **Primary Architecture** | **5-Tier Cognitive Routing, FinOps & SLA Verification Engine** | Python Protocol Gateway | Multi-Key Billing & Token Hub | Multi-Provider API Proxy | Stateless CLI Tunnel |
| **Model SLA & Quantization Forensics (`TraitMark V2/V3`)** | ✅ **5 Forensic Lanes (`discriminator`, `spatial_curve`, `token_jsd`, `hard_series`, 3-Prong `tier_splitter`) + Two-Stage Intra-Family Matcher across 7 Families** | ❌ None | ❌ None | ❌ None | ❌ None |
| **Native Antigravity IDE & Desktop Bridge** | ✅ **1-Click `:6969` Direct Endpoint Bridge + Isolated Dual HTTP/2 Pools (Zero `:50999` shim)** | ❌ None | ❌ None | ❌ None | ⚠️ Fragile `:50999` shim |
| **Native Claude Code (Desktop + CLI) Bridge** | ✅ **1-Click Curated Picker (`inferenceModels`) + `Opus`/`Sonnet`/`Haiku` Tier Slots** | ⚠️ Manual env vars | ⚠️ Manual env vars | ⚠️ Manual env vars | ⚠️ Manual env vars |
| **Multi-Credential Load Balancing & KV Locality** | ✅ **Session-Sticky KV-Cache Affinity + Exhaustive Per-Leaf Failover** | ⚠️ Stateless RR (cache-miss risk) | ⚠️ Stateless key rotation | ⚠️ Basic key rotation | ⚠️ Stateless rotation |
| **Live `5h` + `7d` Quota Telemetry & Plan Badges** | ✅ **9 Provider Integrations + Antigravity `Google`/`Claude` Pool Split + Tier Badges** | ⚠️ API spend only | ⚠️ Static credit counters | ❌ Basic counters | ❌ CLI logs only |
| **Faithful Execution Layer (FEL)** | ✅ **`<0.9ms` Domain Framing + Zero-TODO Completion Contracts + Native `gemini-4-argon`** | ❌ None | ❌ None | ❌ None | ❌ None |
| **1-Button Unified Smart Prompt Caching** | ✅ **Auto 3-Stage Breakpoints + `prompt_cache_key` Propagation + 2026 Family Wildcards** | ⚠️ Partial / manual | ⚠️ Pass-through only | ⚠️ Pass-through only | ⚠️ Pass-through only |
| **5-Tier Blacksand Smart Routing + Quality Gates** | ✅ **13 Categories × 3 Tiers + 15-Role Orchestrator + Worst-Score-Wins Gate** | ⚠️ Static conditional rules | ❌ Manual model only | ⚠️ Static rules | ❌ Manual model only |
| **Thinking-Parameter Parity & `thought_signature` Cache** | ✅ **11 Model Families Conformance-Tested + Router-Side `thought_signature` Preservation** | ❌ Pass-through | ❌ Pass-through | ❌ Pass-through | ⚠️ Partial |
| **True Token/Cache Accounting & False-`499` Fix** | ✅ **Unified Cross-Dialect Accounting + Independent Backfill + Post-`[DONE]` `200 OK`** | ⚠️ Varies by adapter | ⚠️ Misses streaming usage | ⚠️ Basic token logs | ❌ No usage DB |
| **3-Layer Supply-Chain & Tool-Call Security Shield** | ✅ **Static Key/URL/Header Scanner + SSRF/DNS-Rebinding Guard + Response & Tool-Call Injection Guard (`log_only` / `redact`)** | ❌ None | ❌ None | ❌ None | ❌ None |
| **100% Local & Machine-Bound Encrypted Vault** | ✅ **Fernet + Windows DPAPI (`enc:`) + 4-Gate Pre-Push Secret Scanner** | ⚠️ Env / external DB | ⚠️ SQL DB | ⚠️ Env / DB | ⚠️ Plaintext JSON files |

---

## 🧠 Tour the Core Engine (`v1.1.0`)

```
                          ┌──────────────────────────────────────────────────────────────┐
                          │                    BSL Router v1.1.0 Engine                  │
                          │                                                              │
   ┌───────────────┐      │  ┌──────────────┐   ┌──────────────┐   ┌──────────────────┐  │      ┌───────────────┐
   │ Claude Code   │      │  │ FEL Pre-Flight│   │  Blacksand   │   │  Smart Caching   │  │      │ Anthropic API │
   │ Antigravity   │─────▶│  │ Contracts +   │──▶│ 5-Tier Matrix│──▶│ + Session-Sticky │──┼─────▶│ OpenAI / Codex│
   │ Cursor / IDE  │◀─────│  │ <0.9ms Framing│   │ + Combo Tree │   │ KV-Cache Affinity│  │      │ Gemini 4 Argon│
   └───────────────┘      │  └──────────────┘   └──────────────┘   └──────────────────┘  │      │ GLM/Kimi/V4.1+│
                          │         ▲                   ▲                    ▲           │      └───────────────┘
                          │   Stream Continuity  Mechanical Quality   Per-Leaf Multi-Key │
                          │   & Completion Guard   Gates (Ultra/Max)   Zero-Drop Failover│
                          └──────────────────────────────────────────────────────────────┘
```

### 1. 🖥️ Unified Native IDE & CLI Bridge (`Antigravity` + `Claude Code`)
- **Antigravity IDE & Desktop (Zero `:50999` Middleman):** Click **Start Integration** in the **Endpoint** tab. BSL Router configures Antigravity's local inference endpoint directly to `http://127.0.0.1:6969`, exposes your custom Combo Models through `/v1internal:fetchAvailableModels` and `/GetAvailableModels`, and isolates native Google Cloud Code traffic (`_ANTIGRAVITY_EGRESS_CLIENT`) from router traffic (`_ROUTER_ANTIGRAVITY_EGRESS_CLIENT`) with automatic stale-socket retry and edge-IP resilience.
- **Claude Code Desktop & CLI:** Configure your `Opus`, `Sonnet`, `Haiku`, and `Default` tier slots plus custom models in one UI. Clicking **Sync to Claude Code** atomically updates `~/.claude/settings.json` and Claude Desktop's `inferenceModels` config — filtering `/v1/models` so your Claude Desktop dropdown shows **only** your curated BSL models.

### 2. 🔬 Model SLA & Quantization Forensics Lab (`TraitMark V2/V3` + 3-Prong `tier_splitter` & Two-Stage Intra-Family Matcher)
Don't guess whether an endpoint is serving the exact frontier model you are paying for — **verify it mathematically**. Open the **Authenticity** tab, select any provider/model endpoint, and run a multi-layer forensic audit against frozen canonical baselines across all **7 model families** (`Claude`, `GPT`, `Gemini`, `Qwen`, `GLM`, `DeepSeek`, `Kimi`) with clean probe middleware isolation (`X-BSL-Authenticity-Probe: 1`):
- **Layer 1 (Structural & Tokenizer Signature):** Measures sub-word tokenization boundaries and latency jitter.
- **Layer 2 (Knowledge Needles & Temporal Cutoff):** Shuffled pre-cutoff and post-cutoff needles graded per candidate baseline.
- **Layer 3 (`TraitMark V2/V3` Forensic Lanes & Two-Stage Intra-Family Disambiguation):**
  - `discriminator`: 10-probe behavioral boundary & style consistency digest battery.
  - `spatial_curve`: 2D/3D geometric reasoning decay curve.
  - `token_jsd`: Jensen-Shannon divergence over token logprob distributions.
  - `hard_series`: Verified hard-math tiers + cross-family judge-graded `visual_svg`, `spatial_3d`, and `sys_design` probes.
  - `tier_splitter` (**Judge-Free 3-Prong Tier Splitter**): Deterministic, procedurally seeded probes (`svg_compiler` 12-attribute geometric SVG compiler, `constraint_sat` 4×4 exact-sum + modular parity + anti-knight solver, and `state_trace` 8-step 5-register bitwise/modular VM execution trace) that separate budget (`Flash`/`Luna`/`Haiku`), mid-tier (`Plus`/`Sonnet`/`SOL`/`Pro`), and flagship (`Max`/`Opus`/`Fable`/`Astra`/`Argon`/`V4-Pro`/`K3`) siblings without any LLM judge.
  - **Two-Stage Intra-Family Matcher (`_disambiguate_siblings`):** Stage 1 locks the model family and candidate pool; when sibling models share family-wide style axes, Stage 2 isolates high-variance sub-version lanes (`tier_splitter`, `hard_series`, `discriminator`, `knowledge_bank`, `spatial_curve`, `capability_curve`, `token_jsd`) to catch silent quantization drift or intra-family tier mismatches with zero false abstains.

### 3. 🛡️ Session-Sticky KV-Cache Affinity, Dual `5h`/`7d` Quota Telemetry & Plan Badges
- **Session-Sticky Load Distribution:** Balances load across your multi-credential pool (`codex`, `claude`, `antigravity`, `gemini-cli`, `kiro`, `grok-cli`, `cursor`, `github`, `kimi`) by assigning the starting credential **on each new conversation**, while locking all consecutive turns of an active conversation to a single credential — preserving **100% KV prompt-cache locality**.
- **Dual `5h` & `7d` Quota Bars + Plan Tier Badges:** Every connected workspace displays real-time `5h` and `7d` remaining `%`, reset countdowns, and color-coded plan badges (`Free`, `Plus`, `Pro`, `Ultra`, `Pro x5/x20/x50`, `Max 5x/20x`, `SuperGrok`, `Power`, `Vivace`). Antigravity integrations split quota bars into separate **`Google`** and **`Claude`** pools matching the native IDE.
- **Hardened Workspace Profile Import:** Import structured credential JSON profiles directly from the UI (`POST /api/oauth/codex/import-json`) with 256 KiB stream bounding, duplicate-key rejection, and automatic token refresh.

### 4. ⚡ Faithful Execution Layer (FEL) Overhaul & Native `gemini-4-argon` Support
- **Why FEL Exists:** In complex enterprise engineering, DevOps, and cybersecurity workflows, frontier models frequently stop early with lazy `"// TODO: implement the rest"` placeholders or misinterpret benign system administration terminology.
- **How FEL Works:**
  - **Pre-Flight Domain-Terminology Precision (`<0.9ms`):** Starter-word indexed framing engine (`_COMPILED_SENSITIVITY_CACHE`) normalizes ambiguous phrasing into precise software engineering terminology (in English & Vietnamese) across the latest turn or full conversation history (`context_scope: "full"`), attaching structured workspace context once.
  - **Cache-Aligned Completion Contracts:** Appends deterministic zero-truncation execution contracts to the system tail while keeping Anthropic `cache_control: {"type": "ephemeral"}` aligned at the tail block so your prompt cache stays 100% warm.
  - **Deterministic Stream-Continuity Recovery:** Detects premature stream stops or false-positive terminology blocks across English, Vietnamese, and Chinese (`_fel_stream_with_recovery`) and transparently resumes with structured technical context, while strictly honoring hard safety boundaries (`blocked`).
  - **Interactive UI Sandbox:** Preview the exact pre-flight framing and completion contract against any model (including `gemini-4-argon`) in the **Tools → FEL Dry-Run Preview** box before sending a single API call.

### 5. 🧠 Blacksand 5-Tier Smart Routing & Recursive Multi-Credential Combos
Point your client at one virtual model and let Blacksand route every turn:

| Tier | Virtual Model | What It Does | Matrix Size |
|---|---|---|---|
| **1** | `blacksand-chat` | Category-aware smart routing for general chat & Q&A | 13 categories × 3 effort tiers = **39 slots** |
| **2** | `blacksand-lite` | Per-agent coding task routing (different model per agent role) | 10 coding agents × 3 effort tiers = **30 slots** |
| **3** | `blacksand-agentic` | Fast-tier agentic coding orchestration with multi-agent dispatch | Multi-agent dispatch |
| **4** | `blacksand-agentic-ultra` | Balanced-tier coding with consultant routing + **mechanical quality gates** | 15 agent roles + 5 member roles + phase cap 22 |
| **5** | `blacksand-agentic-max` | Multi-domain fusion for complex cross-domain workflows | Inherits Ultra config + cross-domain orchestration |

- **Exhaustive Per-Leaf Multi-Credential Failover:** If `providerA/model1` has 4 API keys or workspace credentials, BSL Router tries **all 4 connections on `model1`** before advancing to `model2` in your Combo — and supports **recursive nested Combos** (`Combo A → Combo B → Combo C → Model`).

### 6. 🛡️ 3-Layer Supply-Chain & Tool-Call Security Shield
Connecting external inference endpoints exposes coding agents to credential exfiltration, SSRF, and poisoned `tool_calls`. BSL Router protects your workstation across three independent security layers:
- **Layer 1 — Static Key, Endpoint & Header Scanner (`app/security/key_scanner.py`):** Blocks shell metacharacters, CRLF header injection, Unicode BiDi trojans, Base64-encoded payloads, env-var interpolation (`${...}`), webhook/tunnel exfiltration endpoints (`ngrok`, `webhook.site`, `oastify`, `trycloudflare`, `pipedream`, `serveo`, etc.), and malicious custom headers before keys are ever saved or verified (`/api/verify-key`, `/api/scan-keys`).
- **Layer 2 — Network & SSRF Boundary (`app/security/url_validator.py`):** Blocks cloud metadata endpoints (`169.254.169.254`, `metadata.google.internal`, `100.100.100.200`), loopback/private CIDR probing, dangerous schemes (`file://`, `gopher://`, `dict://`, `ftp://`), and DNS-rebinding attacks on custom provider URLs.
- **Layer 3 — Post-Upstream Response & Tool-Call Injection Guard (`app/middleware/response_guard.py`):** Inspects both assistant prose and serialized `tool_calls` / `tool_use` / `functionCall` arguments returned by upstream endpoints across streaming and non-streaming paths. Detects credential theft (`.env`, `.bsl_key`, SSH keys, cloud credentials), shell/LOLBAS RCE (`curl | sh`, `powershell -enc`, `reg save HKLM\SAM`, `mshta`, `regsvr32`), MCP/agent config hijacking (`mcp_config.json`, `claude_desktop_config.json`, `authorized_keys`), and stealth zero-width/BiDi payloads — with configurable **`log_only`** or active **`redact`** mode (replacing poisoned prose and stripping malicious `tool_calls` before they reach your IDE) plus live telemetry in **Tools → Supply-Chain & Response Injection Shield**.

---

## 🛠️ Our Commitment to Continuous Fixes & Relentless Engineering

Most open-source AI routers are weekend side-projects that break the moment OpenAI, Anthropic, or Google updates a streaming header. **BSL Router is our live, 24/7 production backbone.** Here is our engineering pledge to every builder and team running this repo:

1. **Daily Dogfooding Under Heavy Agentic Load:** Every line of BSL Router is battle-tested daily on multi-hour Claude Code, Antigravity IDE, and multi-agent coding sessions. When an upstream protocol shifts (`/responses` wire changes, HTTP/2 stream resets, Anthropic `message_delta` token accounting, or false `499` stream disconnects), we diagnose, fix, and regression-test it immediately.
2. **Day-Zero 2026 Frontier Model Support:** Built-in reasoning contracts, caching wildcards, and family adapters ship ready for **`Gemini 4 Argon`**, **`GPT-5.6 / GPT-6.1 (SOL / Terra / Luna)`**, **`Claude 4.6 / Opus 5`**, **`Kimi K2.7 / K3`**, **`DeepSeek V4 / V4.1+`**, **`GLM-5.2 / 5.3`**, **`MiniMax M2.7 / M3.1`**, and **`Qwen 3.8`**.
3. **3,830+ Automated Tests & Zero-Downtime Reloads:** Every fix ships with permanent regression tests (`3,830+` tests across `150+` test files). Graceful server restarts (`POST /api/version/restart`) drain active IDE streams before swapping processes so your coding agent never drops a connection.
4. **Strict 4-Gate Security & Privacy Hygiene:** Every release passes `scripts/pre-push-check.ps1` (tracked-tree secret sweep, machine-path sweep, diff key scan, and gitignore verification) before a single commit reaches GitHub.

---

## 📖 Documentation & Architecture

| Document | What's Inside |
|---|---|
| **[Architecture Guide (EN / VN)](docs/ARCHITECTURE.md)** | Deep-dive into the request lifecycle, Blacksand 5-tier matrix, FEL, Authenticity Lab, Session-Sticky KV Affinity, and Native IDE Integration |
| **[Changelog (EN / VN)](CHANGELOG.md)** | Complete release notes for `v1.1.0` and all prior versions |
| **[Config Template](config.example.yaml)** | Annotated `config.yaml` starter template |
| **[Security Policy](SECURITY.md)** | Fernet/DPAPI encryption, key scanner, and vulnerability reporting |
| **[Contributing](CONTRIBUTING.md)** | Development setup and test suite guidelines |

---
---

# 🇻🇳 Tiếng Việt — Bản Tiếng Việt Đầy Đủ

---

## ⚡ Khởi Chạy Trong 60 Giây

```bash
git clone https://github.com/dongden-alt/BSL-Router.git
cd BSL-Router
python -m venv .venv

# Windows: .venv\Scripts\activate   |   macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml

# Khởi chạy Server Dual-Stack (:6969)
python -m app.dualstack_serve --port 6969
```

👉 **Mở ngay `http://localhost:6969`** để vào **Trung Tâm Chỉ Huy 10 Tab**, kết nối API key hoặc tài khoản workspace, bấm **Start Integration** cho **Antigravity IDE** hoặc **Sync to Claude Code**, và bắt đầu triển khai.

---

## 🔥 Vì Sao Dân Kỹ Thuật Chuyển Sang BSL Router? (6 Bài Toán Sống Còn Mà Gateway Thông Thường Bó Tay)

Các công cụ gateway như **LiteLLM**, **One-API / NewAPI**, **OmniRouter**, hay các local proxy thông thường đều được thiết kế như những *"ống dẫn phi trạng thái" (stateless pipes)*: nhận request, đổi vỏ JSON rồi đẩy đi. Khi bạn chạy các phiên coding agent 100k+ token liên tục mỗi ngày, **ống dẫn thụ động sẽ lộ rõ 6 điểm nghẽn chí mạng — và BSL Router `v1.1.0` giải quyết tận gốc cả 6:**

| # | Bài Toán Thực Tế Khi Vận Hành AI Cường Độ Cao | Vì Sao LiteLLM / One-API / Proxy Thông Thường Thất Bại | Cách **BSL Router `v1.1.0`** Giải Quyết Triệt Để |
|---|---|---|---|
| **1** | **"Liệu endpoint đang dùng có bị hạ cấp lượng tử hóa (quantized) hoặc tráo sang model bậc thấp không?"** | Không có bất kỳ công cụ kiểm chứng hành vi nào. Khi một endpoint bên thứ ba bị suy giảm chất lượng do lượng tử hóa hoặc định tuyến nhầm alias flagship sang model nhỏ hơn, các gateway thông thường vẫn chuyển tiếp mù quáng khiến chất lượng code sụt giảm. | **🔬 Phòng Thí Nghiệm Kiểm Định SLA & Model (`TraitMark V2/V3`):** Đo vân tay bất kỳ endpoint nào so với chuẩn gốc đã đóng băng qua độ lệch tokenizer, đường cong năng lực, kim tri thức (knowledge needles), **5 làn kiểm định V2/V3** (`discriminator`, `spatial_curve`, `token_jsd`, `hard_series`, cùng **`tier_splitter` 3 mũi nhọn không cần LLM judge**: `svg_compiler` + `constraint_sat` + `state_trace`) và **Bộ Khớp Nội Họ 2 Giai Đoạn (`_disambiguate_siblings`)**. Phát hiện chính xác sự suy giảm lượng tử hóa hoặc sai lệch bậc model (`Flash/Luna/Haiku` so với `Max/Pro/Opus/Argon`) trên cả 7 họ model chỉ trong vài phút. |
| **2** | **"Chia tải Round-Robin thông thường làm mất sạch 100k+ token KV Prompt Cache và gây nghẽn Rate-Limit!"** | Round-robin phi trạng thái theo từng request sẽ rải các lượt trả lời liên tiếp của *cùng một phiên làm việc* qua nhiều khóa/endpoint khác nhau — phá vỡ hoàn toàn bộ nhớ đệm KV prompt cache (buộc nạp lại 100k+ token uncached mỗi lượt) và gây xung đột giới hạn tốc độ (rate-limit). | **🛡️ Ghim Phiên Bảo Toàn KV-Cache (Session-Sticky Affinity) + Chuyển Đổi Dự Phòng Cạn Kiệt Từng Lá:** Phân bổ kết nối **theo từng phiên hội thoại mới**, nhưng ghim toàn bộ các lượt trong cùng một phiên vào **1 kết nối duy nhất** để giữ ấm 100% KV cache. Nếu chạm ngưỡng `429`, BSL tự động thử hết **mọi kết nối còn khả dụng trên cùng model đó** trước khi chuyển sang model dự phòng tiếp theo trong Combo. |
| **3** | **"Không thể theo dõi trực tiếp hạn mức 5h/7d còn lại hay phân biệt hạng gói của từng kết nối."** | Các proxy CLI không có giao diện quản trị; các dashboard cơ bản chỉ hiện chấm xanh/đỏ tĩnh hoặc chờ đến khi gọi lỗi giữa chừng mới biết cạn hạn mức. | **📊 Thanh Hạn Mức Kép `5h` + `7d` Thời Gian Thực & Huy Hiệu Gói:** Hiển thị trực tiếp phần trăm hạn mức `5h` và `7d` (tuần), đồng hồ đếm ngược hồi phục (`↻ 2h 14m`), nút `Force Refresh`, **Import cấu hình JSON** an toàn, và **Huy hiệu hạng gói** (`Free`, `Plus`, `Pro`, `Ultra`, `Pro x5/x20/x50`, `Max 5x/20x`, `SuperGrok`, `Power`, `Vivace`) cho cả 9 nhóm tích hợp — tự động ưu tiên kết nối còn hạn mức. |
| **4** | **"Agent dừng ngang xương với dòng `// TODO` dang dở hoặc hiểu nhầm thuật ngữ DevOps / SecOps chuyên ngành."** | Các gateway khác gửi nguyên xi prompt mà không có hợp đồng kiểm soát đầu ra. Model dễ cắt cụt file lớn giữa chừng, để lại `// TODO`, hoặc nhạy cảm quá mức với các thuật ngữ kiểm thử bảo mật / vận hành hệ thống hợp lệ. | **⚡ Đại Tu Faithful Execution Layer (FEL) `v1.1.0`:** Chuẩn hóa thuật ngữ kỹ thuật chuyên ngành trong `<0,9ms` (`~223x` nhanh hơn nhờ `_COMPILED_SENSITIVITY_CACHE`), gắn hợp đồng hoàn thành công việc không cắt cụt mà vẫn giữ nguyên `cache_control`, tự động duy trì mạch stream liền mạch (`_fel_stream_with_recovery` hỗ trợ Anh/Việt/Trung), hỗ trợ **`gemini-4-argon` native**, kèm hộp **Dry-Run Preview** trực quan. |
| **5** | **"Tích hợp tổ hợp model vào Antigravity IDE hay Claude Code Desktop phức tạp và dễ đứt kết nối."** | Các giải pháp dùng shim trung gian `:50999` mở sớm stream HTTP `200` làm hỏng cơ chế fallback của Combo; trong khi `/v1/models` thông thường đổ tràn hơn 50 model thô vào menu của Claude Desktop. | **🖥️ Cầu Nối Ứng Dụng Native 1-Click (`Antigravity` + `Claude Code`):** Kết nối trực tiếp cấu hình điểm cuối của Antigravity về `:6969` (không qua shim `:50999`, tách riêng pool HTTP/2 native và router, tự động khôi phục kết nối). Với Claude Code, tự tạo alias `claude-bsl-*` & các slot tầng `Opus`/`Sonnet`/`Haiku`, đồng bộ danh sách model tinh gọn vào `~/.claude/settings.json` và `inferenceModels` của Claude Desktop. |
| **6** | **"Chi phí token tăng vọt vì mỗi hãng AI lại dùng một chuẩn Prompt Caching khác nhau."** | Kỹ sư phải tự cấu hình riêng rẽ `cache_control` cho Anthropic, `prompt_cache_key` cho OpenAI/Kimi, trong khi các lệnh gọi `/responses` phi trạng thái thường làm rơi mất khóa cache. | **💰 `Smart Prompt Caching` 1 Nút Bấm Hợp Nhất (Mọi Họ Model 2026):** Một công tắc duy nhất tự động chèn 3 tầng điểm ngắt `cache_control` (`tools[-1]`, `system`, lịch sử hội thoại) cho Anthropic/GLM/MiniMax, truyền `prompt_cache_key` xuyên suốt cho OpenAI (`GPT-5.6/6.1`), `/responses`, Kimi (`K3*`), và neo tiền tố tĩnh cho DeepSeek (`V4*`), Gemini (`4*`/`Argon`), Qwen. |

---

## ⚔️ Đặt Lên Bàn Cân: BSL Router vs. LiteLLM, One-API, OmniRouter & Các Gateway Thông Thường

| Tính Năng / Năng Lực | **⚡ BSL Router `v1.1.0`** | **LiteLLM** | **One-API / NewAPI** | **OmniRouter** | **CLI Proxy Thuần** |
|---|---|---|---|---|---|
| **Kiến trúc cốt lõi** | **Engine Định Tuyến 5 Tầng, FinOps & Kiểm Định SLA Model** | Gateway Giao Thức Python | Quản Lý Khóa & Billing | Multi-Provider API Proxy | CLI Tunnel (Không UI) |
| **Kiểm định SLA & Độ chuẩn xác Model (`TraitMark V2/V3`)** | ✅ **5 Làn Pháp Y (`discriminator`, `spatial_curve`, `token_jsd`, `hard_series`, `tier_splitter` 3 Mũi Nhọn) + Bộ Khớp Nội Họ 2 Giai Đoạn Trên 7 Họ Model** | ❌ Không có | ❌ Không có | ❌ Không có | ❌ Không có |
| **Cầu nối Native Antigravity IDE & Desktop** | ✅ **Kết Nối Trực Tiếp `:6969` + Cách Ly Pool HTTP/2 (Không cần `:50999`)** | ❌ Không có | ❌ Không có | ❌ Không có | ⚠️ Cần shim `:50999` |
| **Cầu nối Native Claude Code (Desktop + CLI)** | ✅ **Đồng Bộ 1-Click Menu Tinh Gọn (`inferenceModels`) + Slot Tầng `Opus`/`Sonnet`/`Haiku`** | ⚠️ Đặt biến môi trường tay | ⚠️ Đặt biến môi trường tay | ⚠️ Đặt biến môi trường tay | ⚠️ Đặt biến môi trường tay |
| **Điều phối đa kết nối & Bảo toàn KV-Cache** | ✅ **Ghim Theo Phiên Giữ Ấm KV-Cache + Chuyển Đổi Dự Phòng Cạn Kiệt Từng Lá** | ⚠️ Xoay theo từng request (mất cache) | ⚠️ Xoay khóa cơ bản | ⚠️ Xoay khóa cơ bản | ⚠️ Xoay cơ bản |
| **Thanh Hạn Mức Kép `5h` + `7d` & Huy Hiệu Gói** | ✅ **9 Nhóm Tích Hợp + Tách Hạn Mức `Google`/`Claude` Cho Antigravity + Huy Hiệu Gói** | ⚠️ Chỉ hiện chi phí API | ⚠️ Bộ đếm tĩnh | ❌ Bộ đếm đơn giản | ❌ Chỉ xem log CLI |
| **Faithful Execution Layer (FEL)** | ✅ **Chuẩn Hóa `<0,9ms` + Hợp Đồng Không Cắt Cụt + Hỗ Trợ `gemini-4-argon` Native** | ❌ Không có | ❌ Không có | ❌ Không có | ❌ Không có |
| **Smart Prompt Caching 1 Nút Bấm** | ✅ **Tự Chèn 3 Tầng Breakpoint + `prompt_cache_key` + Wildcard Thế Hệ 2026** | ⚠️ Thủ công / một phần | ⚠️ Chỉ chuyển tiếp | ⚠️ Chỉ chuyển tiếp | ⚠️ Chỉ chuyển tiếp |
| **Định Tuyến Thông Minh 5 Tầng Blacksand** | ✅ **13 Danh Mục × 3 Tầng + Điều Phối 15 Role + Cổng Chất Lượng Cơ Học** | ⚠️ Luật điều kiện tĩnh | ❌ Chọn model thủ công | ⚠️ Luật tĩnh | ❌ Chọn model thủ công |
| **Chuẩn hóa Thinking & Cache `thought_signature`** | ✅ **11 Họ Model Được Kiểm Chứng + Tự Động Bảo Toàn `thought_signature` Phía Router** | ❌ Chỉ chuyển tiếp | ❌ Chỉ chuyển tiếp | ❌ Chỉ chuyển tiếp | ⚠️ Một phần |
| **Đếm Token/Cache Chuẩn & Sửa Lỗi Giả `499`** | ✅ **Chuẩn Hóa Đa Giao Thức + Backfill Độc Lập + Nâng Cấp `200 OK` Sau `[DONE]`** | ⚠️ Tùy adapter | ⚠️ Thiếu usage khi stream | ⚠️ Log token cơ bản | ❌ Không có DB usage |
| **Lá Chắn Bảo Mật Chuỗi Cung Ứng 3 Tầng** | ✅ **Quét Key/URL/Header Tĩnh + Chặn SSRF/DNS-Rebinding + Response & Tool-Call Injection Guard (`log_only` / `redact`)** | ❌ Không có | ❌ Không có | ❌ Không có | ❌ Không có |
| **100% Local & Mã Hóa Khóa Gắn Theo Máy** | ✅ **Fernet + Windows DPAPI (`enc:`) + 4 Cổng Quét Bí Mật Trước Khi Push** | ⚠️ Env / DB ngoài | ⚠️ SQL DB | ⚠️ Env / DB | ⚠️ File JSON plaintext |

### 🛡️ Lá Chắn Bảo Mật Chuỗi Cung Ứng & Kiểm Soát Tool-Call 3 Tầng
Khi kết nối các endpoint bên ngoài, coding agent của bạn có nguy cơ bị rò rỉ thông tin nhạy cảm, tấn công SSRF hoặc chèn lệnh ẩn trong `tool_calls`. BSL Router bảo vệ máy trạm qua 3 tầng độc lập:
- **Tầng 1 — Quét Tĩnh API Key, Endpoint & Custom Header (`app/security/key_scanner.py`):** Chặn ký tự điều khiển shell, CRLF header injection, Unicode BiDi trojan ẩn, payload mã hóa Base64 nhúng trong key, nội suy biến môi trường (`${...}`), tên miền webhook/tunnel thu thập dữ liệu (`ngrok`, `webhook.site`, `oastify`, `trycloudflare`, `pipedream`, `serveo`, v.v.) và các custom header bất thường ngay trước khi lưu hoặc xác minh key (`/api/verify-key`, `/api/scan-keys`).
- **Tầng 2 — Vành Đai Mạng & Chống SSRF (`app/security/url_validator.py`):** Chặn truy cập cloud metadata (`169.254.169.254`, `metadata.google.internal`, `100.100.100.200`), dải mạng nội bộ/loopback, giao thức nguy hiểm (`file://`, `gopher://`, `dict://`, `ftp://`) và tấn công DNS-rebinding trên các URL provider tùy chỉnh.
- **Tầng 3 — Kiểm Soát Phản Hồi & Chặn Tool-Call Độc Hại Từ Upstream (`app/middleware/response_guard.py`):** Quét cả văn bản trả lời lẫn tham số `tool_calls` / `tool_use` / `functionCall` trả về từ provider trên cả luồng stream và non-stream. Phát hiện hành vi truy cập tệp nhạy cảm (`.env`, `.bsl_key`, khóa SSH, cloud credentials), thực thi mã từ xa qua shell/LOLBAS (`curl | sh`, `powershell -enc`, `reg save HKLM\SAM`, `mshta`, `regsvr32`), can thiệp cấu hình MCP/agent (`mcp_config.json`, `claude_desktop_config.json`, `authorized_keys`) và chuỗi ký tự ẩn zero-width/BiDi — hỗ trợ chế độ **`log_only`** (chỉ ghi log) hoặc **`redact`** chủ động (tự động cắt bỏ `tool_calls` độc hại và lọc nội dung bị tiêm nhiễm trước khi trả về IDE) ngay trên tab **Tools**.

---

## 🛠️ Cam Kết Sửa Lỗi Liên Tục & Nâng Cấp Thực Chiến

Phần lớn các AI router mã nguồn mở chỉ là dự án cuối tuần và nhanh chóng lỗi thời ngay khi OpenAI, Anthropic hay Google đổi cấu trúc header/stream. **BSL Router là xương sống vận hành thực tế 24/7 của chính chúng tôi.** Khi tải về và sử dụng BSL Router, bạn nhận được cam kết kỹ thuật rõ ràng:

1. **Thực chiến mỗi ngày dưới tải Agentic nặng:** Mọi tính năng đều được rèn giũa trực tiếp qua hàng trăm nghìn token mỗi ngày trên Claude Code, Antigravity IDE và các luồng multi-agent. Bất kỳ thay đổi giao thức nào từ upstream (như `/responses`, ngắt kết nối HTTP/2, sai lệch token `message_delta`, hay báo giả `499 client_disconnected`) đều được khoanh vùng, sửa tận gốc và khóa bằng test tự động.
2. **Hỗ trợ ngay lập tức thế hệ Model Frontier 2026:** Tích hợp sẵn hợp đồng tham số suy luận (reasoning), wildcard bộ nhớ đệm và bộ nhận diện họ model cho **`Gemini 4 Argon`**, **`GPT-5.6 / GPT-6.1 (SOL / Terra / Luna)`**, **`Claude 4.6 / Opus 5`**, **`Kimi K2.7 / K3`**, **`DeepSeek V4 / V4.1+`**, **`GLM-5.2 / 5.3`**, **`MiniMax M2.7 / M3.1`**, và **`Qwen 3.8`**.
3. **Hơn 3.830+ bài kiểm thử tự động & Khởi động lại không rớt mạng:** Mọi bản vá đều đi kèm regression test (`3.830+` tests trên `150+` bộ test). Cơ chế khởi động lại êm ái (`POST /api/version/restart`) chờ các luồng stream trong IDE hoàn tất rồi mới chuyển giao tiến trình.
4. **Kỷ luật bảo mật 4 tầng nghiêm ngặt:** Mọi bản cập nhật đều bắt buộc vượt qua `scripts/pre-push-check.ps1` (quét khóa bí mật, quét đường dẫn máy cá nhân, kiểm tra diff và bảo vệ `.gitignore`) trước khi phát hành.

---

## 📖 Tài Liệu & Kiến Trúc

| Tài Liệu | Nội Dung Chi Tiết |
|---|---|
| **[Kiến Trúc Hệ Thống (EN / VN)](docs/ARCHITECTURE.md)** | Giải thích chi tiết vòng đời request, ma trận 5 tầng Blacksand, FEL, Authenticity Lab, Ghim Phiên KV-Cache và Cầu Nối IDE Native |
| **[Nhật Ký Thay Đổi (EN / VN)](CHANGELOG.md)** | Chi tiết bản phát hành lớn `v1.1.0` và lịch sử các phiên bản |
| **[File Cấu Hình Mẫu](config.example.yaml)** | Mẫu `config.yaml` có chú thích đầy đủ |
| **[Chính Sách Bảo Mật](SECURITY.md)** | Cơ chế mã hóa Fernet/DPAPI, trình quét khóa và báo cáo lỗ hổng |
| **[Hướng Dẫn Đóng Góp](CONTRIBUTING.md)** | Thiết lập môi trường phát triển và quy chuẩn kiểm thử |

---

## 📄 License / Giấy Phép

MIT License — see [LICENSE](LICENSE) for details / xem [LICENSE](LICENSE) để biết chi tiết.

**Built with relentless engineering by [Đồng Tôn](https://github.com/dongden-alt)**
