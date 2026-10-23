# ⚡ BSL Router `v1.1.0`

### Stop Routing Blind. Stop Getting Banned. Stop Paying Frontier Prices for Fake Relays.
### *Ngừng Định Tuyến Mù. Ngừng Bị Khóa Tài Khoản. Ngừng Trả Giá Đắt Cho Model Bị Tráo.*

> **🇬🇧 The only 100% local, self-hosted AI Command Center engineered for power builders.** Plug **Claude Code (Desktop & CLI)**, **Antigravity IDE**, **Cursor**, and any OpenAI/Anthropic/Gemini app into one intelligent `:6969` endpoint — featuring **5-Tier Blacksand Routing**, **Per-Session Anti-Ban OAuth Rotation**, **1-Button Smart Prompt Caching**, **Faithful Execution Layer (FEL)**, and a built-in **Model Authenticity Lab (`TraitMark V2/V3`)** that catches fake or downgraded relay models red-handed.
>
> **🇻🇳 Trung tâm chỉ huy AI 100% local duy nhất được thiết kế cho dân thực chiến.** Kết nối **Claude Code (Desktop & CLI)**, **Antigravity IDE**, **Cursor**, và mọi ứng dụng OpenAI/Anthropic/Gemini vào một cổng `:6969` duy nhất — tích hợp **Định Tuyến Thông Minh 5 Tầng Blacksand**, **Xoay Tài Khoản OAuth Chống Ban Theo Phiên**, **Smart Prompt Caching 1 Nút Bấm**, **Faithful Execution Layer (FEL)**, và **Phòng Thí Nghiệm Kiểm Định Model Thật/Giả (`TraitMark V2/V3`)**.

[![Version: 1.1.0](https://img.shields.io/badge/Version-1.1.0-00d9a3.svg)](https://github.com/dongden-alt/BSL-Router/releases/tag/v1.1.0)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11+-3776ab.svg)](https://www.python.org/downloads/)
[![Tests: 3830+ Passed](https://img.shields.io/badge/Tests-3830%2B_Passed-brightgreen.svg)](#-verification--engineering-discipline)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.129+-009688.svg)](https://fastapi.tiangolo.com/)

**🇬🇧 [English](#-get-running-in-60-seconds)** · **⚔️ [BSL vs 9Router / CLIProxyAPI / OmniRouter](#-head-to-head-bsl-router-vs-9router-cliproxyapi-omnirouter--litellm)** · **🇻🇳 [Tiếng Việt](#-tiếng-việt--bản-tiếng-việt-đầy-đủ)**

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

👉 **Open `http://localhost:6969`** to access the **10-Tab Admin Command Center**, connect your OAuth accounts or API keys, click **Start Integration** for **Antigravity IDE** or **Sync to Claude Code**, and start shipping.

---

## 🔥 Why Builders Switch to BSL Router (Pain Points Other Routers Can't Solve)

Tools like **9Router**, **CLIProxyAPI**, **OmniRouter**, and **LiteLLM** were built as *pass-through pipes*: they take a request, translate the JSON wrapper, and forward it. When you run real 100k+ token agentic coding workflows every day, **a dumb pipe breaks down in 6 ways — and BSL Router solves every single one:**

| # | The Real-World Pain Point | Why 9Router / CLIProxyAPI / OmniRouter Fail | How **BSL Router `v1.1.0`** Solves It Permanently |
|---|---|---|---|
| **1** | **"Is my relay silently serving a quantized or downgraded model?"** | Zero verification. If a cheap relay renames `Sonnet` or `Flash` as `Claude Opus 5` or `DeepSeek V4-Pro`, dumb routers forward it blindly while you pay top dollar for degraded code. | **🔬 Built-In Model Authenticity Lab (`TraitMark V2/V3`):** Fingerprints any endpoint against frozen gold standards across tokenizer jitter, capability curves, knowledge needles, and 4 forensic V2/V3 lanes (`discriminator`, `spatial_curve`, `token_jsd`, `hard_series` hard-math + expert SVG/3D/architecture judges). Exposes impostors and silent downgrades in minutes. |
| **2** | **"Turning on OAuth Round-Robin got my accounts banned and wiped my prompt cache!"** | Naive per-request round-robin sprays consecutive turns of the *same* coding session across 5 different OAuth accounts — triggering upstream anti-fraud flags and destroying KV prompt cache locality (forcing 100k+ uncached token re-prefills every turn). | **🛡️ Per-Session Sticky OAuth Anti-Ban + Exhaustive Per-Leaf Rotation:** Rotates the starting account **per new conversation/session** while pinning all turns inside an active session to **1 account**. If a token hits a rate limit, BSL rotates through **every healthy token/cookie on that model first** before falling back to the next model in your combo. |
| **3** | **"I have no idea which OAuth account is out of 5h/7d quota or which plan tier it's on."** | CLIProxyAPI has no web UI; other routers show generic green/red dots or fail mid-stream when an exhausted account is picked. | **📊 Live Dual `5h` + `7d` Quota Bars & Subscription Tier Badges:** Real-time `5h` and weekly `7d` progress bars, recovery countdowns (`↻ 2h 14m`), `Force Refresh`, hardened **Codex JSON Import**, and color-coded **Subscription Tier Badges** (`Free`, `Plus`, `Pro`, `Ultra`, `Pro x5/x20/x50`, `Max 5x/20x`, `SuperGrok`, `Power`, `Vivace`) across all 9 OAuth providers — plus automatic pre-selection of accounts with remaining quota. |
| **4** | **"My agent refuses legitimate security/reverse-engineering tasks or stops halfway with TODOs."** | Other routers pass prompts raw. Over-sensitive upstream filters block legitimate security audits, binary analysis, or automation scripts — or the model lazily leaves placeholders. | **⚡ Faithful Execution Layer (FEL) `v1.1.0` Overhaul:** Pre-flight starter-word indexed semantic reframing (`<0.9ms` per 14 KB — `~223x` faster), cache-aligned positive completion contracts, early-stream soft-refusal recovery (`_fel_stream_with_recovery` across EN/VI/ZH), full coverage across all 6 streaming paths + native **`gemini-4-argon`**, and an interactive **Dry-Run Preview Sandbox** in the UI. |
| **5** | **"Integrating custom models into Antigravity IDE or Claude Code Desktop is fragile and freezes."** | External `:50999` shims (`ag-model`) open HTTP `200` streams prematurely (breaking fallback on upstream errors), while raw `/v1/models` endpoints dump 50+ messy provider models into Claude Desktop's picker. | **🖥️ 1-Click Native App Integration (`Antigravity` + `Claude Code`):** Directly patches Antigravity IDE `settings.json` & Desktop `app.asar` to talk to `:6969` (zero `:50999` shim, isolated native vs. router HTTP/2 pools, auto-healing on restart). For Claude Code, mints safe `claude-bsl-*` aliases & dedicated `Opus`/`Sonnet`/`Haiku` tier slots and syncs a clean, exclusive picker directly into `~/.claude/settings.json` and Claude Desktop's `inferenceModels`. |
| **6** | **"My token bill is exploding because each provider uses a different caching protocol."** | You have to configure separate rules for Anthropic `cache_control`, OpenAI `prompt_cache_key`, Kimi, and DeepSeek — and Codex `/responses` (`store: False`) drops cache keys altogether. | **💰 1-Button Unified `Smart Prompt Caching` (All 2026 Families):** One master switch auto-injects 3-stage `cache_control` breakpoints (`tools[-1]`, `system`, rolling history) on Anthropic/GLM/MiniMax, propagates deterministic `prompt_cache_key` across OpenAI (`GPT-5.6/6.1`), Codex `/responses`, and Kimi (`K3*`), and anchors static prefixes for DeepSeek (`V4*`), Gemini (`4*`/`Argon`), and Qwen. |

---

## ⚔️ Head-to-Head: BSL Router vs. 9Router, CLIProxyAPI, OmniRouter & LiteLLM

| Capability | **⚡ BSL Router `v1.1.0`** | **9Router** | **CLIProxyAPI** | **OmniRouter** | **LiteLLM / One-API** |
|---|---|---|---|---|---|
| **Primary Architecture** | **5-Tier AI Intelligence & Verification Engine** | Local OAuth/API Proxy | Headless CLI OAuth Tunnel | Multi-Provider API Proxy | Protocol Gateway / Billing |
| **Model Authenticity Lab (`TraitMark V2/V3`)** | ✅ **4 Forensic Lanes (`discriminator`, `spatial_curve`, `token_jsd`, `hard_series`)** | ❌ None | ❌ None | ❌ None | ❌ None |
| **Native Antigravity IDE & Desktop Integration** | ✅ **Direct `:6969` ASAR/Settings Patcher + Isolated Native Pool (No `:50999` proxy)** | ⚠️ Requires external `:50999` shim or MITM | ❌ Manual config only | ❌ None | ❌ None |
| **Native Claude Code (Desktop + CLI) Integration** | ✅ **1-Click Exclusive Picker (`inferenceModels`) + `Opus`/`Sonnet`/`Haiku` Tier Slots** | ⚠️ Manual env vars | ⚠️ Manual env vars | ⚠️ Manual env vars | ⚠️ Manual env vars |
| **OAuth Multi-Account Rotation** | ✅ **Per-Session Sticky Anti-Ban + Exhaustive Per-Leaf Failover** | ⚠️ Per-request RR (ban & cache-miss risk) | ⚠️ Basic token rotation | ⚠️ Basic key rotation | ⚠️ Basic load balancing |
| **Live OAuth `5h` + `7d` Quota Bars & Tier Badges** | ✅ **9 OAuth Providers + Antigravity `Google`/`Claude` Split + Plan Badges** | ⚠️ Partial / single-window | ❌ CLI logs only | ❌ Basic counters | ⚠️ API credits only |
| **Faithful Execution Layer (FEL)** | ✅ **`<0.9ms` Semantic Reframe + Early-Stream Refusal Recovery + Native `gemini-4-argon`** | ❌ None | ❌ None | ❌ None | ❌ None |
| **1-Button Unified Smart Prompt Caching** | ✅ **Auto 3-Stage Breakpoints + Codex/Kimi/GPT-6.1 `prompt_cache_key` + 2026 Wildcards** | ⚠️ Pass-through only | ⚠️ Pass-through only | ⚠️ Pass-through only | ⚠️ Partial / manual |
| **5-Tier Blacksand Smart Routing + Quality Gates** | ✅ **13 Categories × 3 Tiers + 15-Role Orchestrator + Worst-Score-Wins Gate** | ❌ Manual model only | ❌ Manual model only | ⚠️ Static rules | ⚠️ Conditional routing |
| **Thinking-Parameter Parity & `thought_signature` Cache** | ✅ **11 Model Families Conformance-Tested + Router-Side `thought_signature` Re-Signing** | ⚠️ Partial | ⚠️ Partial | ❌ Pass-through | ❌ Pass-through |
| **True Token/Cache Accounting & False-`499` Fix** | ✅ **Unified Cross-Dialect Accounting + Independent Backfill + Post-`[DONE]` `200 OK`** | ⚠️ Misses relay usage | ❌ No usage DB | ⚠️ Basic token logs | ⚠️ Varies by adapter |
| **100% Local & Machine-Bound Encrypted Keys** | ✅ **Fernet + Windows DPAPI (`enc:`) + 4-Gate Pre-Push Scanner** | ⚠️ Local JSON | ⚠️ Plaintext auth files | ⚠️ Env / DB | ⚠️ Env / DB |

---

## 🧠 Tour the Core Engine (`v1.1.0`)

```
                          ┌──────────────────────────────────────────────────────────────┐
                          │                    BSL Router v1.1.0 Engine                  │
                          │                                                              │
   ┌───────────────┐      │  ┌──────────────┐   ┌──────────────┐   ┌──────────────────┐  │      ┌───────────────┐
   │ Claude Code   │      │  │ FEL Pre-Flight│   │  Blacksand   │   │  Smart Caching   │  │      │ Anthropic/OAuth│
   │ Antigravity   │─────▶│  │ Clarity +     │──▶│ 5-Tier Matrix│──▶│ + Per-Session    │──┼─────▶│ Codex / OpenAI │
   │ Cursor / IDE  │◀─────│  │ <0.9ms Reframe│   │ + Combo Tree │   │ OAuth Anti-Ban   │  │      │ Gemini 4 Argon│
   └───────────────┘      │  └──────────────┘   └──────────────┘   └──────────────────┘  │      │ GLM/Kimi/V4.1+│
                          │         ▲                   ▲                    ▲           │      └───────────────┘
                          │   Early-Stream       Mechanical Quality   Per-Leaf Multi-Key │
                          │  Refusal Recovery     Gates (Ultra/Max)   Infinite Failover  │
                          └──────────────────────────────────────────────────────────────┘
```

### 1. 🖥️ Unified App Integration (`Antigravity IDE/Desktop` + `Claude Code Desktop/CLI`)
- **Antigravity IDE & Desktop (Zero `:50999` Middle Proxy):** Click **Start Integration** in the **Endpoint** tab. BSL Router patches Antigravity IDE's `settings.json` and Desktop's `resources/app.asar` directly to `http://127.0.0.1:6969`, injects your custom Combo Models into `/v1internal:fetchAvailableModels` and `/GetAvailableModels`, and isolates native Google Cloud Code traffic (`_ANTIGRAVITY_EGRESS_CLIENT`) from router OAuth traffic (`_ROUTER_ANTIGRAVITY_EGRESS_CLIENT`) with automatic stale-socket retry and edge-IP rotation.
- **Claude Code Desktop & CLI:** Configure your `Opus`, `Sonnet`, `Haiku`, and `Default` tier slots plus custom models in one UI. Clicking **Sync to Claude Code** atomically updates `~/.claude/settings.json` and Claude Desktop's `inferenceModels` config — filtering `/v1/models` so your Claude Desktop dropdown shows **only** your curated BSL models.

### 2. 🔬 Model Authenticity Lab (`TraitMark V2/V3`)
Don't trust third-party relays — **verify them**. Open the **Authenticity** tab, pick any suspect provider/model, and run a multi-layer forensic audit against frozen canonical standards:
- **Layer 1 (Structural & Tokenizer Signature):** Measures sub-word tokenization boundaries and latency jitter.
- **Layer 2 (Knowledge Needles & Temporal Cutoff):** Shuffled pre-cutoff and post-cutoff needles graded per candidate standard.
- **Layer 3 (`TraitMark V2/V3` Forensic Lanes):**
  - `discriminator`: 10-probe behavioral boundary & style consistency digest battery.
  - `spatial_curve`: 2D/3D geometric reasoning decay curve.
  - `token_jsd`: Jensen-Shannon divergence over token logprob distributions.
  - `hard_series`: Verified hard-math tiers + cross-family judge-graded `visual_svg`, `spatial_3d`, and `sys_design` probes.

### 3. 🛡️ Per-Session OAuth Anti-Ban, Dual `5h`/`7d` Quotas & Subscription Tier Badges
- **Session-Sticky Round-Robin:** Spreads load across your OAuth pool (`codex`, `claude`, `antigravity`, `gemini-cli`, `kiro`, `grok-cli`, `cursor`, `github`, `kimi`) by rotating the starting account **on each new conversation**, while locking all turns of an active conversation to a single account.
- **Dual `5h` & `7d` Quota Bars + Tier Badges:** Every OAuth connection displays real-time `5h` and `7d` remaining `%`, reset countdowns, and color-coded plan badges (`Free`, `Plus`, `Pro`, `Ultra`, `Pro x5/x20/x50`, `Max 5x/20x`, `SuperGrok`, `Power`, `Vivace`). Antigravity accounts even split quota bars into separate **`Google`** and **`Claude`** pools matching the native IDE.
- **Hardened Codex JSON Import:** Import `auth.json` files or raw JSON directly from the UI (`POST /api/oauth/codex/import-json`) with 256 KiB stream bounding, duplicate-key rejection, and automatic token refresh.

### 4. ⚡ Faithful Execution Layer (FEL) Overhaul & Native `gemini-4-argon` Support
- **Why FEL Exists:** Frontier models frequently false-refuse benign security testing, reverse engineering, or DevOps automation prompts — or stop early with `"// TODO: implement the rest"`.
- **How FEL Works:**
  - **Pre-Flight Clarity & `<0.9ms` Semantic Reframing:** Starter-word indexed regex engine (`_COMPILED_SENSITIVITY_CACHE`) rewrites trigger phrases into accurate professional engineering terminology (in English & Vietnamese) across the last turn or full conversation history (`context_scope: "full"`), prepending operator authorization context once.
  - **Cache-Aligned Completion Directives:** Appends positive-framing execution contracts to the system tail while shifting Anthropic `cache_control: {"type": "ephemeral"}` to the tail block so your prompt cache stays 100% warm.
  - **Early-Stream & Non-Stream Refusal Recovery:** Detects soft refusals across English, Vietnamese, and Chinese (`_fel_stream_with_recovery`) and transparently re-dispatches with a clarification turn, while strictly respecting hard safety walls (`blocked`).
  - **Interactive UI Sandbox:** Test any prompt against any model (including `gemini-4-argon`) in the **Tools → FEL Dry-Run Preview** box before sending a single API call.

### 5. 🧠 Blacksand 5-Tier Smart Routing & Recursive Multi-Token Combos
Point your client at one virtual model and let Blacksand route every turn:

| Tier | Virtual Model | What It Does | Matrix Size |
|---|---|---|---|
| **1** | `blacksand-chat` | Category-aware smart routing for general chat & Q&A | 13 categories × 3 effort tiers = **39 slots** |
| **2** | `blacksand-lite` | Per-agent coding task routing (different model per agent role) | 10 coding agents × 3 effort tiers = **30 slots** |
| **3** | `blacksand-agentic` | Fast-tier agentic coding orchestration with multi-agent dispatch | Multi-agent dispatch |
| **4** | `blacksand-agentic-ultra` | Balanced-tier coding with consultant routing + **mechanical quality gates** | 15 agent roles + 5 member roles + phase cap 22 |
| **5** | `blacksand-agentic-max` | Multi-domain fusion for complex cross-domain workflows | Inherits Ultra config + cross-domain orchestration |

- **Exhaustive Per-Leaf Multi-Token Failover:** If `providerA/model1` has 4 API keys or OAuth cookies, BSL Router tries **all 4 connections on `model1`** before advancing to `model2` in your Combo — and supports **recursive nested Combos** (`Combo A → Combo B → Combo C → Model`).

---

## 🛠️ Our Commitment to Continuous Fixes & Relentless Upgrades

Most open-source AI routers are weekend side-projects that rot the moment OpenAI, Anthropic, or Google changes an upstream header. **BSL Router is our live, 24/7 production backbone.** Here is our engineering pledge to every user who downloads this repo:

1. **Daily Dogfooding Under Heavy Agentic Load:** Every line of BSL Router is battle-tested daily on multi-hour Claude Code, Antigravity IDE, and multi-agent coding sessions. When an upstream breaks (`Codex /responses` wire changes, Google Cloud Code `v1internal` HTTP/2 disconnects, Anthropic `message_delta` token accounting, or false `499` stream disconnects), we diagnose, fix, and regression-test it immediately.
2. **Day-Zero 2026 Frontier Model Support:** Built-in reasoning contracts, caching wildcards, and family adapters ship ready for **`Gemini 4 Argon`**, **`GPT-5.6 / GPT-6.1 (SOL / Terra / Luna)`**, **`Claude 4.6 / Opus 5`**, **`Kimi K2.7 / K3`**, **`DeepSeek V4 / V4.1+`**, **`GLM-5.2 / 5.3`**, **`MiniMax M2.7 / M3.1`**, and **`Qwen 3.8`**.
3. **3,830+ Automated Tests & Zero-Downtime Reloads:** Every fix ships with permanent regression tests (`3,830+` tests across `150+` test files). Graceful server restarts (`POST /api/version/restart`) drain active IDE streams before swapping processes so your coding agent never drops a connection.
4. **Strict 4-Gate Security & Privacy Hygiene:** Every release passes `scripts/pre-push-check.ps1` (tracked-tree secret sweep, machine-path sweep, diff key scan, and gitignore verification) before a single commit reaches GitHub.

---

## 📖 Documentation & Architecture

| Document | What's Inside |
|---|---|
| **[Architecture Guide (EN / VN)](docs/ARCHITECTURE.md)** | Deep-dive into the request lifecycle, Blacksand 5-tier matrix, FEL, Authenticity Lab, OAuth Anti-Ban, and App Integration |
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

👉 **Mở ngay `http://localhost:6969`** để vào **Trung Tâm Chỉ Huy 10 Tab**, kết nối tài khoản OAuth hoặc API key, bấm **Start Integration** cho **Antigravity IDE** hoặc **Sync to Claude Code**, và bắt đầu chiến.

---

## 🔥 Vì Sao Dân Kỹ Thuật Chuyển Sang BSL Router? (6 Nỗi Đau Mà Router Khác Bó Tay)

Các công cụ như **9Router**, **CLIProxyAPI**, **OmniRouter**, hay **LiteLLM** đều được thiết kế như những *"ống dẫn thụ động" (dumb pipes)*: nhận request, đổi vỏ JSON rồi đẩy đi. Khi bạn chạy các phiên coding agent 100k+ token liên tục mỗi ngày, **ống dẫn thụ động sẽ vỡ trận ở 6 điểm chí mạng — và BSL Router `v1.1.0` giải quyết tận gốc cả 6:**

| # | Nỗi Đau Thực Tế Khi Dùng AI Hàng Ngày | Vì Sao 9Router / CLIProxyAPI / OmniRouter Thất Bại | Cách **BSL Router `v1.1.0`** Giải Quyết Triệt Để |
|---|---|---|---|
| **1** | **"Liệu bên bán API/relay có lén tráo model rẻ tiền hoặc bản lượng tử hóa (quantized) không?"** | Không có bất kỳ công cụ kiểm chứng nào. Nếu một bên relay gắn nhãn `Sonnet` hoặc `Flash` thành `Claude Opus 5` hay `DeepSeek V4-Pro`, các router khác vẫn chuyển tiếp mù quáng trong khi bạn trả tiền giá cao cho code chất lượng thấp. | **🔬 Phòng Thí Nghiệm Kiểm Định Model (`TraitMark V2/V3`):** Đo vân tay bất kỳ endpoint nào so với chuẩn gốc đã đóng băng qua độ lệch tokenizer, đường cong năng lực, kim tri thức (knowledge needles) và 4 làn kiểm định V2/V3 (`discriminator`, `spatial_curve`, `token_jsd`, `hard_series` gồm toán khó + giám khảo chấm SVG/3D/kiến trúc). Vạch mặt model giả/bị hạ cấp chỉ trong vài phút. |
| **2** | **"Bật Round-Robin cho OAuth xong thì tài khoản bị khóa hàng loạt, mất sạch Prompt Cache!"** | Round-robin ngây thơ theo từng request sẽ rải các lượt trả lời liên tiếp của *cùng một cuộc hội thoại* qua 5 tài khoản OAuth khác nhau — vừa kích hoạt cờ chống gian lận của nhà cung cấp (dẫn đến khóa nick), vừa làm mất toàn bộ bộ nhớ đệm KV prompt cache (buộc nạp lại 100k+ token uncached mỗi lượt). | **🛡️ Chống Ban OAuth Theo Phiên (Per-Session Sticky) + Xoay Cạn Kiệt Từng Lá:** Xoay tài khoản bắt đầu **theo từng phiên hội thoại mới**, nhưng ghim toàn bộ các lượt trong cùng một phiên vào **1 tài khoản duy nhất**. Nếu chạm giới hạn, BSL tự động thử hết **mọi token/cookie còn sống trên cùng model đó** trước khi chuyển sang model tiếp theo trong Combo. |
| **3** | **"Không biết tài khoản OAuth nào đã cạn quota 5h/7d hay đang ở gói thuê bao nào."** | CLIProxyAPI không có giao diện web; các router khác chỉ hiện chấm xanh/đỏ chung chung hoặc chờ đến khi gọi lỗi giữa chừng mới biết hết hạn mức. | **📊 Thanh Quota Kép `5h` + `7d` Trực Tiếp & Huy Hiệu Hạng Thuê Bao:** Hiển thị thanh phần trăm quota `5h` và `7d` (tuần), đồng hồ đếm ngược hồi phục (`↻ 2h 14m`), nút `Force Refresh`, **Import JSON Codex** an toàn, và **Huy hiệu hạng thuê bao** (`Free`, `Plus`, `Pro`, `Ultra`, `Pro x5/x20/x50`, `Max 5x/20x`, `SuperGrok`, `Power`, `Vivace`) cho cả 9 provider OAuth — tự động bỏ qua tài khoản `0%` quota khi chọn kết nối. |
| **4** | **"Model tự dưng từ chối các tác vụ kiểm thử bảo mật / dịch ngược hợp lệ hoặc dừng ngang xương."** | Các router khác gửi nguyên xi prompt thô. Bộ lọc nhạy cảm quá mức của upstream dễ chặn nhầm các yêu cầu audit bảo mật, phân tích mã nhị phân, tự động hóa — hoặc model lười biếng để lại `// TODO`. | **⚡ Đại Tu Faithful Execution Layer (FEL) `v1.1.0`:** Tự động viết lại từ nhạy cảm sang thuật ngữ kỹ thuật chuẩn xác trong `<0,9ms` (`~223x` nhanh hơn nhờ `_COMPILED_SENSITIVITY_CACHE`), nối hợp đồng hoàn thành công việc mà vẫn giữ nguyên `cache_control`, tự phục hồi từ chối mềm ngay đầu stream (`_fel_stream_with_recovery` hỗ trợ Anh/Việt/Trung), hỗ trợ cả **`gemini-4-argon` native**, kèm hộp **Dry-Run Preview** trực quan. |
| **5** | **"Tích hợp model ngoài vào Antigravity IDE hay Claude Code Desktop rất dễ lỗi và treo stream."** | Các bản vá ngoài dùng proxy trung gian `:50999` (`ag-model`) mở sớm stream HTTP `200` làm hỏng cơ chế fallback của Combo; trong khi `/v1/models` thông thường đổ tràn hơn 50 model thô vào menu của Claude Desktop. | **🖥️ Tích Hợp Ứng Dụng Native 1-Click (`Antigravity` + `Claude Code`):** Patch trực tiếp `settings.json` và `app.asar` của Antigravity về `:6969` (bỏ hoàn toàn `:50999`, tách riêng pool HTTP/2 native và router, tự áp dụng khi khởi động). Với Claude Code, tự tạo alias `claude-bsl-*` & các slot tầng `Opus`/`Sonnet`/`Haiku`, đồng bộ danh sách model gọn sạch vào `~/.claude/settings.json` và `inferenceModels` của Claude Desktop. |
| **6** | **"Chi phí token tăng vọt vì mỗi hãng AI lại dùng một chuẩn Prompt Caching khác nhau."** | Bạn phải tự cấu hình riêng rẽ `cache_control` cho Anthropic, `prompt_cache_key` cho OpenAI/Kimi, trong khi Codex `/responses` (`store: False`) thường làm rơi mất khóa cache. | **💰 `Smart Prompt Caching` 1 Nút Bấm Hợp Nhất (Mọi Họ Model 2026):** Một công tắc duy nhất tự động chèn 3 tầng điểm ngắt `cache_control` (`tools[-1]`, `system`, lịch sử hội thoại) cho Anthropic/GLM/MiniMax, truyền `prompt_cache_key` xuyên suốt cho OpenAI (`GPT-5.6/6.1`), Codex `/responses`, Kimi (`K3*`), và neo tiền tố tĩnh cho DeepSeek (`V4*`), Gemini (`4*`/`Argon`), Qwen. |

---

## ⚔️ Đặt Lên Bàn Cân: BSL Router vs. 9Router, CLIProxyAPI, OmniRouter & LiteLLM

| Tính Năng / Năng Lực | **⚡ BSL Router `v1.1.0`** | **9Router** | **CLIProxyAPI** | **OmniRouter** | **LiteLLM / One-API** |
|---|---|---|---|---|---|
| **Kiến trúc cốt lõi** | **Engine Trí Tuệ 5 Tầng & Kiểm Định Độ Xác Thực AI** | Local OAuth/API Proxy | CLI OAuth Tunnel (Không UI) | Multi-Provider API Proxy | Gateway Giao Thức / Billing |
| **Kiểm định Model Thật/Giả (`TraitMark V2/V3`)** | ✅ **4 Làn Pháp Y (`discriminator`, `spatial_curve`, `token_jsd`, `hard_series`)** | ❌ Không có | ❌ Không có | ❌ Không có | ❌ Không có |
| **Tích hợp Native Antigravity IDE & Desktop** | ✅ **Patch Trực Tiếp `:6969` ASAR/Settings + Cách Ly Pool Native (Không cần `:50999`)** | ⚠️ Cần shim `:50999` ngoài hoặc MITM | ❌ Cấu hình thủ công | ❌ Không có | ❌ Không có |
| **Tích hợp Native Claude Code (Desktop + CLI)** | ✅ **Đồng Bộ 1-Click Menu Độc Quyền (`inferenceModels`) + Slot Tầng `Opus`/`Sonnet`/`Haiku`** | ⚠️ Đặt biến môi trường tay | ⚠️ Đặt biến môi trường tay | ⚠️ Đặt biến môi trường tay | ⚠️ Đặt biến môi trường tay |
| **Xoay đa tài khoản OAuth** | ✅ **Ghim Theo Phiên Chống Ban + Xoay Cạn Kiệt Từng Lá Trong Combo** | ⚠️ Xoay theo từng request (dễ bị ban & mất cache) | ⚠️ Xoay token cơ bản | ⚠️ Xoay key cơ bản | ⚠️ Load balancing cơ bản |
| **Thanh Quota Kép `5h` + `7d` & Huy Hiệu Gói** | ✅ **9 Provider OAuth + Tách Quota `Google`/`Claude` Cho Antigravity + Huy Hiệu Gói** | ⚠️ Một phần / 1 cửa sổ | ❌ Chỉ xem log CLI | ❌ Bộ đếm đơn giản | ⚠️ Chỉ hiện tiền API |
| **Faithful Execution Layer (FEL)** | ✅ **Reframe `<0,9ms` + Phục Hồi Từ Chối Đầu Stream + Hỗ Trợ `gemini-4-argon` Native** | ❌ Không có | ❌ Không có | ❌ Không có | ❌ Không có |
| **Smart Prompt Caching 1 Nút Bấm** | ✅ **Tự Chèn 3 Tầng Breakpoint + `prompt_cache_key` Codex/Kimi/GPT-6.1 + Wildcard 2026** | ⚠️ Chỉ chuyển tiếp | ⚠️ Chỉ chuyển tiếp | ⚠️ Chỉ chuyển tiếp | ⚠️ Thủ công / một phần |
| **Định Tuyến Thông Minh 5 Tầng Blacksand** | ✅ **13 Danh Mục × 3 Tầng + Điều Phối 15 Role + Cổng Chất Lượng Cơ Học** | ❌ Chọn model thủ công | ❌ Chọn model thủ công | ⚠️ Luật tĩnh | ⚠️ Định tuyến điều kiện |
| **Chuẩn hóa Thinking & Cache `thought_signature`** | ✅ **11 Họ Model Được Kiểm Chứng + Tự Động Ký Lại `thought_signature` Phía Router** | ⚠️ Một phần | ⚠️ Một phần | ❌ Chỉ chuyển tiếp | ❌ Chỉ chuyển tiếp |
| **Đếm Token/Cache Chuẩn & Sửa Lỗi Giả `499`** | ✅ **Chuẩn Hóa Đa Giao Thức + Backfill Độc Lập + Nâng Cấp `200 OK` Sau `[DONE]`** | ⚠️ Thiếu usage từ relay | ❌ Không có DB usage | ⚠️ Log token cơ bản | ⚠️ Tùy adapter |
| **100% Local & Mã Hóa Khóa Gắn Theo Máy** | ✅ **Fernet + Windows DPAPI (`enc:`) + 4 Cổng Quét Bí Mật Trước Khi Push** | ⚠️ File JSON local | ⚠️ File auth plaintext | ⚠️ Env / DB | ⚠️ Env / DB |

---

## 🛠️ Cam Kết Sửa Lỗi Liên Tục & Nâng Cấp Thực Chiến

Phần lớn các AI router mã nguồn mở chỉ là dự án cuối tuần và nhanh chóng lỗi thời ngay khi OpenAI, Anthropic hay Google đổi cấu trúc header/stream. **BSL Router là xương sống vận hành thực tế 24/7 của chính chúng tôi.** Khi tải về và sử dụng BSL Router, bạn nhận được cam kết kỹ thuật rõ ràng:

1. **Thực chiến mỗi ngày dưới tải Agentic nặng:** Mọi tính năng đều được rèn giũa trực tiếp qua hàng trăm nghìn token mỗi ngày trên Claude Code, Antigravity IDE và các luồng multi-agent. Bất kỳ lỗi giao thức nào từ upstream (như Codex `/responses`, ngắt kết nối HTTP/2 của Google Cloud Code `v1internal`, sai lệch token `message_delta`, hay báo giả `499 client_disconnected`) đều được khoanh vùng, sửa tận gốc và khóa bằng test tự động.
2. **Hỗ trợ ngay lập tức thế hệ Model Frontier 2026:** Tích hợp sẵn hợp đồng tham số suy luận (reasoning), wildcard bộ nhớ đệm và bộ nhận diện họ model cho **`Gemini 4 Argon`**, **`GPT-5.6 / GPT-6.1 (SOL / Terra / Luna)`**, **`Claude 4.6 / Opus 5`**, **`Kimi K2.7 / K3`**, **`DeepSeek V4 / V4.1+`**, **`GLM-5.2 / 5.3`**, **`MiniMax M2.7 / M3.1`**, và **`Qwen 3.8`**.
3. **Hơn 3.830+ bài kiểm thử tự động & Khởi động lại không rớt mạng:** Mọi bản vá đều đi kèm regression test (`3.830+` tests trên `150+` bộ test). Cơ chế khởi động lại êm ái (`POST /api/version/restart`) chờ các luồng stream trong IDE hoàn tất rồi mới chuyển giao tiến trình.
4. **Kỷ luật bảo mật 4 tầng nghiêm ngặt:** Mọi bản cập nhật đều bắt buộc vượt qua `scripts/pre-push-check.ps1` (quét khóa bí mật, quét đường dẫn máy cá nhân, kiểm tra diff và bảo vệ `.gitignore`) trước khi phát hành.

---

## 📖 Tài Liệu & Kiến Trúc

| Tài Liệu | Nội Dung Chi Tiết |
|---|---|
| **[Kiến Trúc Hệ Thống (EN / VN)](docs/ARCHITECTURE.md)** | Giải thích chi tiết vòng đời request, ma trận 5 tầng Blacksand, FEL, Authenticity Lab, OAuth Anti-Ban và App Integration |
| **[Nhật Ký Thay Đổi (EN / VN)](CHANGELOG.md)** | Chi tiết bản phát hành lớn `v1.1.0` và lịch sử các phiên bản |
| **[File Cấu Hình Mẫu](config.example.yaml)** | Mẫu `config.yaml` có chú thích đầy đủ |
| **[Chính Sách Bảo Mật](SECURITY.md)** | Cơ chế mã hóa Fernet/DPAPI, trình quét khóa và báo cáo lỗ hổng |
| **[Hướng Dẫn Đóng Góp](CONTRIBUTING.md)** | Thiết lập môi trường phát triển và quy chuẩn kiểm thử |

---

## 📄 License / Giấy Phép

MIT License — see [LICENSE](LICENSE) for details / xem [LICENSE](LICENSE) để biết chi tiết.

**Built with relentless engineering by [Đồng Tôn](https://github.com/dongden-alt)**
