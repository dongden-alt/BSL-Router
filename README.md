# BSL Router

> **The AI router that thinks.** Not just a proxy — a 5-tier intelligence engine that classifies, routes, translates, and orchestrates across 12+ AI providers with mechanical quality gates.
>
> **AI router biết suy nghĩ.** Không chỉ là proxy — một engine trí tuệ 5 tầng tự động phân loại, định tuyến, dịch giao thức, và điều phối trên 12+ provider với cổng chất lượng cơ học.

[![Version: 1.0.7](https://img.shields.io/badge/Version-1.0.7-00d9a3.svg)](https://github.com/dongden-alt/BSL-Router/releases/tag/v1.0.7)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11+-3776ab.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.129+-009688.svg)](https://fastapi.tiangolo.com/)
[![Tests: 3100+](https://img.shields.io/badge/Tests-3100+-brightgreen.svg)](#-verification)

**🇬🇧 [English](#-the-problem)** · **🇻🇳 [Tiếng Việt](#-vấn-đề)**

---

## Why BSL Router Exists

Every AI team hits the same wall:

| Pain | What happens |
|---|---|
| **Protocol lock-in** | Your client speaks OpenAI, your best model is on Anthropic. You're stuck. |
| **Provider downtime** | One provider goes down → your entire workflow freezes. |
| **Key sprawl** | 12 API keys, 3 OAuth tokens, 5 rate limits. Manual rotation is a nightmare. |
| **No intelligence** | Other routers just forward requests. No classification, no quality gates, no orchestration. |
| **Agentic complexity** | Coding agents need different models for planning, coding, review — but you're manually swapping configs. |
| **Stream freezes** | Provider stalls mid-stream → IDE hangs → you kill the process → lost work. |

## What BSL Router Does Differently

BSL Router isn't another "AI gateway." It's an **intelligence layer** that sits between your client and your providers:

```
                         ┌──────────────────────────────────────────────┐
                         │              BSL Router Engine               │
                         │                                              │
   ┌──────────┐          │  ┌─────────┐  ┌──────────┐  ┌─────────────┐  │          ┌──────────┐
   │  Your    │  request │  │Classify │→ │ Blacksand│→ │  Translate  │  │  stream  │ OpenAI   │
   │  AI App  │─────────▶│  │ + Route │  │  Matrix  │  │  + Recover  │──┼─────────▶│ Anthropic│
   │  (any)   │◀─────────│  │         │  │  (5-tier)│  │             │  │          │ Google   │
   └──────────┘ response │  └─────────┘  └──────────┘  └─────────────┘  │          │ DeepSeek │
                         │     ↑                         ↑               │          │ GLM      │
                         │  Quality Gate          Failover Engine        │          │ + 8 more │
                         │  (mechanical)         (infinite retry)        │          └──────────┘
                         └──────────────────────────────────────────────┘
```

**Think of it as an AI traffic controller that actually understands what your request needs — and fights to deliver it.**

---

## 🧠 Blacksand: The 5-Tier Routing Engine

This is what no other router has. Instead of blindly forwarding to whatever model you named, BSL Router classifies your request and routes it through a **5-tier intelligence matrix**:

| Tier | Virtual Model | What It Does | Matrix Size |
|---|---|---|---|
| **1** | `blacksand-chat` | Category-aware smart routing for general chat & Q&A | 13 categories × 3 effort tiers = **39 slots** |
| **2** | `blacksand-lite` | Per-agent coding task routing (different model per agent role) | 10 coding agents × 3 effort tiers = **30 slots** |
| **3** | `blacksand-agentic` | Fast-tier agentic coding orchestration with multi-agent dispatch | Multi-agent dispatch |
| **4** | `blacksand-agentic-ultra` | Balanced-tier coding with consultant routing + **mechanical quality gates** | 15 agent roles + 5 member roles + phase cap 22 |
| **5** | `blacksand-agentic-max` | Multi-domain fusion for complex cross-domain workflows | Inherits Ultra config + cross-domain orchestration |

### How It Works

Point your client at a single virtual model name — `blacksand-chat` — and BSL Router:

1. **Classifies** the request intent (chat, code, agentic tool use)
2. **Categorizes** it (13 categories: general, technical, creative, scout, power_coder, vision, fast_coder, architect, reviewer, debugger, refactorer, documenter, tester)
3. **Estimates complexity** → picks an effort tier (fast / standard / strong)
4. **Routes** to the best provider for that category+tier combination via **combo aliases**

### Combo Alias System

Each matrix slot references a combo alias — a fallback chain you define:

```yaml
combos:
  - alias: coder-1          # Fast tier: quick completions, cheap models
    chain:
      - provider: deepseek
        model: deepseek-v4-flash
      - provider: glm
        model: glm-5.1-flash
    strategy: fallback

  - alias: coder-2          # Standard tier: balanced quality/speed
    chain:
      - provider: anthropic
        model: claude-sonnet-4
      - provider: openai
        model: gpt-4o
    strategy: fallback

  - alias: coder-3          # Strongest tier: complex reasoning, architecture
    chain:
      - provider: anthropic
        model: claude-opus-4
      - provider: openai
        model: gpt-5
    strategy: fallback
```

Now your coding agent gets `coder-1` for quick completions, `coder-2` for standard work, and `coder-3` for architecture — **all from a single `blacksand-lite` model name**, automatically.

### Quality-Gated Orchestration (Ultra + Max tiers)

`blacksand-agentic-ultra` and `blacksand-agentic-max` go beyond routing — they **orchestrate**:

- **15 agent roles**: general, planner, auditor, vision, scout, planner_architect, planner_challenger, planner_planner, auditor_reviewer, auditor_auditor, refactor, fast_coder, power_coder, ultra_coder, frontend_coder
- **5 member roles** (commit-boundary only): planner_architect, planner_challenger, planner_planner, auditor_reviewer, auditor_auditor
- **Mechanical quality gate**: Every member output is parsed, scored, and worst-merged into the lead's verdict. Members can only LOWER a dimension score, never raise it. A single `fail` on any dimension blocks the phase.
- **Phase cap**: 22 internal calls per client request — bounded by design, never infinite.

```xml
<!-- This is what the quality gate produces, mechanically: -->
<quality_verdict>
  <dimension name="completeness" score="pass" />
  <dimension name="coherence" score="partial" />
  <dimension name="correctness" score="pass" />
  <dimension name="safety" score="pass" />
</quality_verdict>
<!-- derive_verdict(): any partial → overall: PARTIAL → lead must address gaps -->
```

> 📖 See [Architecture: Blacksand Model Routing](docs/ARCHITECTURE.md#blacksand-model-routing) for the full matrix spec.

---

## ⚔️ BSL Router vs Other AI Gateways

| Feature | **BSL Router** | LiteLLM | One-API | Portkey |
|---|---|---|---|---|
| **Protocol translation** | ✅ Bidirectional (OpenAI ↔ Anthropic ↔ Gemini) | ✅ OpenAI format only | ✅ OpenAI format only | ✅ OpenAI format only |
| **5-tier routing intelligence** | ✅ Blacksand matrix (13 cat × 3 tiers) | ❌ Manual model selection | ❌ Manual model selection | ❌ Conditional routing only |
| **Agentic orchestration** | ✅ 15-role dispatch + quality gates | ❌ Not available | ❌ Not available | ❌ Not available |
| **Mechanical quality gates** | ✅ Worst-score-wins merge, fail=block | ❌ Not available | ❌ Not available | ❌ Guardrails (different) |
| **Infinite retry failover** | ✅ Chain wraps with 2s→30s backoff | ⚠️ Basic fallback | ⚠️ Basic fallback | ✅ Fallbacks + retries |
| **Thinking-parameter parity** | ✅ 11 model families, conformance-tested | ❌ Not available | ❌ Not available | ❌ Not available |
| **Fuzzy model-ID resolution** | ✅ Typos still route correctly | ❌ Exact match only | ❌ Exact match only | ❌ Exact match only |
| **FEL refusal softening** | ✅ Pre-flight directives + auto-retry | ❌ Not available | ❌ Not available | ❌ Not available |
| **Document Intelligence** | ✅ PDF/DOCX/XLSX/PPTX parsing | ❌ Not available | ❌ Not available | ❌ Not available |
| **Vision Bridge** | ✅ Image→text for non-vision models | ❌ Not available | ❌ Not available | ❌ Not available |
| **MITM proxy mode** | ✅ OS-level interception for hardcoded apps | ❌ Not available | ❌ Not available | ❌ Not available |
| **Antigravity IDE integration** | ✅ Direct-inference overlay | ❌ Not available | ❌ Not available | ❌ Not available |
| **Encrypted key storage** | ✅ Fernet + machine-bound | ⚠️ Plaintext env vars | ⚠️ Database-stored | ⚠️ Cloud-managed |
| **Admin dashboard** | ✅ 9-tab web UI | ⚠️ Basic UI | ✅ Web UI | ✅ Cloud dashboard |
| **Self-hosted** | ✅ 100% local, no cloud dependency | ✅ Self-hosted | ✅ Self-hosted | ❌ Cloud-first |
| **Provider count** | 12+ | 100+ | 20+ | 200+ |

> **The bottom line:** LiteLLM/One-API/Portkey are protocol gateways — they forward requests. BSL Router is an **intelligence engine** — it classifies, routes, orchestrates, quality-checks, and fights to deliver your request. Blacksand's 5-tier matrix doesn't exist anywhere else.

---

## 🚀 Quick Start

### 1. Install

```bash
git clone https://github.com/dongden-alt/bsl-router.git
cd bsl-router
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate

pip install -r requirements.txt
```

### 2. Configure

```bash
cp config.example.yaml config.yaml
# Edit config.yaml — add your provider API keys
```

### 3. Run

```bash
python -m uvicorn app.main:app --host 0.0.0.0 --port 6969
```

Open `http://localhost:6969` → admin dashboard.

> **Windows users**: Use `.\scripts\bslrouter.ps1 -start` for managed startup with automatic port cleanup, dual-stack health checks, and supervisor process handling.

### 4. Point Your Client

| Your client | Set base URL to | Works with |
|---|---|---|
| **Claude Code** | `http://localhost:6969` | `ANTHROPIC_BASE_URL` env var |
| **Cursor** | `http://localhost:6969/v1` | OpenAI-compatible setting |
| **Any OpenAI client** | `http://localhost:6969/v1` | Drop-in replacement |
| **Any Anthropic client** | `http://localhost:6969` | `/v1/messages` endpoint |
| **Antigravity IDE** | Configure from dashboard → Endpoint tab | Direct overlay, no MITM needed |

Now send `model: "blacksand-chat"` and watch BSL Router classify, route, and deliver.

---

## 🔑 Core Features

### 🧠 Smart Routing + Blacksand Matrix
Send `model: blacksand-chat` → BSL Router classifies (13 categories × 3 tiers), routes to your best provider, and falls back automatically. Your client never sees a failure.

### 🔄 Bidirectional Protocol Translation

| From | To | Status |
|---|---|---|
| OpenAI | Anthropic | ✅ |
| OpenAI | Gemini | ✅ |
| Anthropic | OpenAI | ✅ |
| Anthropic | Gemini | ✅ |
| Gemini | OpenAI | ✅ |
| Any | Any | ✅ |

Message format, tool calls, thinking parameters, streaming — all translated bidirectionally.

### 🛡️ Infinite-Retry Failover Engine

```yaml
combos:
  - alias: my-smart-chain
    chain:
      - provider: openai       # Try first
        model: gpt-4o
      - provider: anthropic    # Fallback 1
        model: claude-sonnet-4
      - provider: deepseek     # Last resort
        model: deepseek-v4
    strategy: fallback
```

If the first provider fails, BSL Router tries the next — **and cycles through retry passes**: each revisit dials the next API key in the pool, bounded by a chain-sized wall clock so slow-failing providers never strand the rest. When the **entire** chain fails, the router wraps around with exponential backoff (2s→30s) until the client disconnects — exhaustion is a pass boundary, never a dead end.

### 🧠 Thinking-Parameter Parity (11 Model Families)

Every model family's reasoning vocabulary is locked to official docs via conformance-tested contracts:

| Family | Reasoning Vocabulary |
|---|---|
| GLM-5.2/5.3 | `none` / `auto` / `thinking` |
| Grok-4.5/4.6 | `auto` / `on` / `off` |
| Qwen 3.8 | `no_think` / `enable_thinking` |
| Kimi K2/K3 | `default` / `auto` / `none` |
| DeepSeek V4 | `low` / `medium` / `high` / `max` |
| Claude | `none` / `auto` / `enabled` |
| GPT-5 | `low` / `medium` / `high` / `max` |
| + 4 more | Family-specific |

Wrong-vocabulary requests are **coerced before reaching upstream** — and rejected thinking params degrade-and-retry automatically.

### 🔍 Fuzzy Model-ID Resolution

Typos and format variants still route correctly: `gpt-5-6-terra`, `gpt-terra-5-6` → `gpt-5.6-terra`. Exact model names are never rewritten.

### 🛡️ FEL: Refusal Softening & Egress Directives

When a provider refuses a request (content policy, safety filter), BSL Router:
1. **Detects** the refusal class (CN-family, GPT-family, Claude-family, Gemini-family)
2. **Injects pre-flight clarity directives** — reframes the request to reduce false triggers
3. **Auto-retries** with softened egress framing
4. **Tracks analytics** — per-family refusal rates, recovery rates, top refusal models

Per-family toggles let you enable FEL for specific provider families only.

### 🧰 Tools & Intelligence Pipeline

Built-in content processing that runs **before** your request reaches the provider:

| Tool | What It Does |
|---|---|
| **Document Intelligence** | Parses PDF, DOCX, XLSX, PPTX attachments → summarizes large documents before sending |
| **Vision Bridge** | Intercepts image URLs sent to text-only models → replaces with detailed text descriptions |
| **Token Budget** | Hard max_tokens ceiling (1024–65535) to prevent cost overruns, with anti-truncation floor when disabled |
| **Prompt Caching** | Provider-specific: Anthropic explicit cache, Kimi key-bound, OpenAI cache-key routing, static-first sorting |
| **Context Compaction** | 1.75× code-traffic ratio guard + EMA usage feedback + min-savings smart gate — compresses context before it hits token limits |
| **GLM Parallel Tool Guard** | Injects `disable_parallel_tool_use` into GLM-family payloads — fixes dropped tool args in multi-tool batches |
| **Degenerate Output Guard** | Detects empty/zero-token responses and retries automatically |

### 📊 Live Quota Indicators

Per-key remaining quota (from one-api/new-api billing endpoints and rate-limit headers) rendered as compact % bars inline with each key's status row — works for both API keys and OAuth accounts. Percentage only, no dollar values.

### 🕴 Antigravity IDE Integration

Direct-inference overlay for [Antigravity IDE](https://antigravity.dev) — no MITM proxy needed:
- Map Antigravity's model slots to your BSL Router providers/combos
- Unmapped slots use native Google Cloud Code
- `thought_signature` preserved end-to-end across multi-turn tool calls — required by Gemini thinking models, re-injected from a bounded router-side cache even if the client strips it
- Configure from the admin dashboard → Endpoint tab

### 🌐 Remote Access

| Method | Use Case |
|---|---|
| **Cloudflare Tunnel** | Secure public URL via Cloudflared — share with team members |
| **Tailscale** | Share on your Tailnet — private mesh network access |
| **API Keys** | Generate scoped keys for other applications |
| **Shared Pool (Ed25519)** | Tier-2 inbound auth for router-to-router pool peers |

### 🔐 Built-in Security

- API keys stored **encrypted** on disk (Fernet encryption)
- Admin dashboard protected by password + session expiry
- Security scanner audits provider config for exfil URLs, key injection, URL spoofing
- Machine-bound encryption key (config is non-portable)
- Optional Ed25519 signature verification for pool-peer inbound traffic

### ⏱️ Anti-Freeze Protection

- **Stream hard deadline**: 10-minute cap per stream prevents infinite hangs
- **Chain deadline**: total budget across all fallback hops prevents cascading timeouts
- **Circuit breaker**: unhealthy providers are automatically rotated out
- **Stream guard**: SSE stream integrity validation catches malformed chunks
- **Connection-level proxy bypass**: per-connection DIRECT egress escape hatch

### 📊 Monitoring Dashboard

Web-based admin UI with 9 tabs:

| Tab | What It Does |
|---|---|
| **Endpoint** | Local endpoints, remote access (Cloudflare/Tailscale), API keys, Antigravity integration |
| **Providers** | Manage AI providers — add, edit, delete, verify connections |
| **Combos** | Define fallback chains and combo aliases |
| **BSL Models** | Configure the 5 Blacksand routing models' matrices |
| **MITM** | Optional transparent proxy for apps with hardcoded API URLs |
| **Tools** | Document Intelligence, Vision Bridge, Token Budget, Prompt Caching, FEL toggles |
| **Usage** | Per-model usage statistics, cost tracking, FEL analytics, live in-flight request strip |
| **Logs** | Live request/response logs with filtering |
| **Settings** | Admin password, shutdown, logout |

---

## 📡 Supported Providers

| Provider | Protocol | Auth | Popular Models |
|---|---|---|---|
| **OpenAI** | OpenAI | API Key | GPT-4o, GPT-5.x |
| **Anthropic** | Anthropic | API Key | Claude 4.x, 5.x |
| **Google Gemini** | Gemini | OAuth | Gemini 2.x |
| **DeepSeek** | OpenAI | API Key | DeepSeek V3, V4 |
| **GLM (Zhipu)** | Anthropic | API Key | GLM-5.x |
| **MiniMax** | OpenAI | API Key | MiniMax M3 |
| **Kimi (Moonshot)** | OpenAI | API Key | Kimi K3 |
| **Qwen (Alibaba)** | OpenAI | API Key | Qwen 3.x |
| **Grok (xAI)** | OpenAI | API Key | Grok 4 |
| **OpenRouter** | OpenAI | API Key | Multi-model |
| **GitHub Models** | OpenAI | OAuth | Various |
| **Google Cloud Code** | Gemini | OAuth | Gemini 2.x |

> **Adding a new provider?** Just add a section to `config.yaml` with `type: custom`. No code changes needed. Also supports `type: image_custom` for image/video generation providers.

---

## 📡 API Endpoints

Point your AI client at BSL Router — it works as a drop-in replacement:

| What You're Doing | Endpoint | Format |
|---|---|---|
| Chat with a model | `POST /v1/chat/completions` | OpenAI |
| Chat with a model | `POST /v1/messages` | Anthropic |
| List available models | `GET /v1/models` | OpenAI |
| Health check | `GET /health` | — |

**Admin endpoints** (for the dashboard):

| What You're Doing | Endpoint |
|---|---|
| View/edit config | `GET/POST /api/config` |
| Manage MITM proxy | `GET/POST /api/mitm/*` |
| View usage stats | `GET /api/observability/usage` |
| View live in-flight requests | `GET /api/observability/usage/inflight` |
| Manage tunnels | `GET/POST /api/tunnel/*` |
| Run security scan | `GET/POST /api/scan-keys` |
| Update BSL matrix | `GET/POST /api/bsl-matrix/*` |
| Manage Antigravity | `GET/POST /api/antigravity/*` |
| Admin login | `POST /api/auth/login` |

---

## 🌐 Optional: MITM Proxy Mode

For apps that don't let you change the API URL (like some VS Code extensions), BSL Router can intercept traffic at the OS level:

1. Modifies your hosts file to redirect `api.openai.com` → `localhost`
2. Runs a transparent proxy on port 443
3. Routes intercepted traffic through BSL Router's full pipeline
4. Your app thinks it's talking to OpenAI — BSL Router handles everything
5. Watchdog process auto-restarts the proxy if it crashes

> ⚠️ This is optional and only needed for apps with hardcoded API URLs. Most apps support custom base URLs — just point them at `http://localhost:6969`.

---

## ✅ Verification

| Metric | Value |
|---|---|
| **Test suite** | 2,800+ tests across 20+ suites |
| **Test pass rate** | 100% (0 failures, 0 regressions) |
| **Quality gate tests** | 18/18 (worst-score-wins, verdict derivation, member merge) |
| **Orchestration tests** | 46/46 (orchestrator:20, ultra:13, max:13) |
| **Full suite runtime** | ~315 seconds |
| **Python version** | 3.11+ |
| **Framework** | FastAPI 0.129+ |

---

## 📖 Learn More

| Document | What's Inside |
|---|---|
| [Architecture](docs/ARCHITECTURE.md) | How BSL Router works under the hood — full request lifecycle |
| [Contributing](CONTRIBUTING.md) | Development setup and PR guidelines |
| [Security](SECURITY.md) | Vulnerability reporting and security features |
| [Changelog](CHANGELOG.md) | Version history |
| [Config Example](config.example.yaml) | Annotated configuration template |

---

## 📄 License

MIT License — see [LICENSE](LICENSE) for details.

**Built by [Đồng Tôn](https://github.com/dongden-alt)**

---
---

# 🇻🇳 Tiếng Việt

---

## 💡 Vấn Đề

Mọi team AI đều gặp cùng một bức tường:

| Nỗi đau | Hậu quả |
|---|---|
| **Bị khóa giao thức** | Client nói OpenAI, model tốt nhất nằm ở Anthropic. Kẹt. |
| **Provider sập** | Một provider down → toàn bộ workflow đóng băng. |
| **Key loạn** | 12 API key, 3 OAuth token, 5 rate limit. Quay tay thủ công cực khổ. |
| **Không có trí tuệ** | Router khác chỉ forward request. Không phân loại, không quality gate, không orchestration. |
| **Phức tạp agentic** | Coding agent cần model khác nhau cho lập kế hoạch, viết code, review — nhưng bạn phải tự swap config. |
| **Stream treo** | Provider stall giữa luồng → IDE hang → kill process → mất việc. |

## BSL Router Khác Gì

BSL Router không phải "AI gateway" thông thường. Nó là một **lớp trí tuệ** nằm giữa client và provider:

```
                         ┌──────────────────────────────────────────────┐
                         │              BSL Router Engine               │
                         │                                              │
   ┌──────────┐          │  ┌─────────┐  ┌──────────┐  ┌─────────────┐  │          ┌──────────┐
   │  App AI  │  request │  │Phân loại│→ │ Blacksand│→ │   Dịch +    │  │  stream  │ OpenAI   │
   │  (bất kỳ)│─────────▶│  │+ Định   │  │  Matrix  │  │   Phục hồi  │──┼─────────▶│ Anthropic│
   │          │◀─────────│  │tuyến    │  │  (5 tầng)│  │             │  │          │ Google   │
   └──────────┘ response │  └─────────┘  └──────────┘  └─────────────┘  │          │ DeepSeek │
                         │     ↑                         ↑               │          │ GLM      │
                         │  Cổng chất lượng        Engine dự phòng       │          │ + 8 nữa  │
                         │  (cơ học)              (retry vô hạn)         │          └──────────┘
                         └──────────────────────────────────────────────┘
```

**Nghĩ nó như một bộ điều phối giao thông AI thực sự hiểu request của bạn cần gì — và chiến đấu để giao nó.**

---

## 🧠 Blacksand: Engine Định Tuyến 5 Tầng

Đây là thứ không router nào khác có. Thay vì mù quáng forward đến model bạn ghi, BSL Router phân loại request và định tuyến qua **ma trận trí tuệ 5 tầng**:

| Tầng | Model Ảo | Chức Năng | Kích Thước Ma Trận |
|---|---|---|---|
| **1** | `blacksand-chat` | Định tuyến thông minh theo danh mục cho chat & hỏi đáp | 13 danh mục × 3 tầng = **39 ô** |
| **2** | `blacksand-lite` | Định tuyến per-agent cho coding (model khác mỗi role) | 10 agent × 3 tầng = **30 ô** |
| **3** | `blacksand-agentic` | Orchestration agentic coding tầng nhanh, dispatch đa agent | Dispatch đa agent |
| **4** | `blacksand-agentic-ultra` | Coding tầng cân bằng + consultant + **cổng chất lượng cơ học** | 15 role agent + 5 role member + phase cap 22 |
| **5** | `blacksand-agentic-max` | Fusion đa domain cho workflow phức tạp | Kế thừa Ultra + orchestration cross-domain |

### Cách Hoạt Động

Trỏ client vào một tên model ảo duy nhất — `blacksand-chat` — BSL Router sẽ:

1. **Phân loại** intent request (chat, code, agentic tool use)
2. **Categorize** (13 danh mục: general, technical, creative, scout, power_coder, vision, fast_coder, architect, reviewer, debugger, refactorer, documenter, tester)
3. **Ước lượng độ phức tạp** → chọn tầng effort (fast / standard / strong)
4. **Định tuyến** đến provider tốt nhất cho tổ hợp danh mục+tầng đó qua **combo alias**

### Hệ Thống Combo Alias

Mỗi ô ma trận tham chiếu một combo alias — chuỗi fallback bạn tự định nghĩa:

```yaml
combos:
  - alias: coder-1          # Tầng nhanh: hoàn thành nhanh, model rẻ
    chain:
      - provider: deepseek
        model: deepseek-v4-flash
      - provider: glm
        model: glm-5.1-flash
    strategy: fallback

  - alias: coder-2          # Tầng tiêu chuẩn: cân bằng chất lượng/tốc độ
    chain:
      - provider: anthropic
        model: claude-sonnet-4
      - provider: openai
        model: gpt-4o
    strategy: fallback

  - alias: coder-3          # Tầng mạnh nhất: reasoning phức tạp, kiến trúc
    chain:
      - provider: anthropic
        model: claude-opus-4
      - provider: openai
        model: gpt-5
    strategy: fallback
```

Giờ coding agent của bạn nhận `coder-1` cho task nhanh, `coder-2` cho task chuẩn, `coder-3` cho kiến trúc — **tất cả từ một tên `blacksand-lite` duy nhất**, tự động.

### Orchestration Có Cổng Chất Lượng (Ultra + Max)

`blacksand-agentic-ultra` và `blacksand-agentic-max` vượt ra ngoài định tuyến — chúng **orchestrate**:

- **15 role agent**: general, planner, auditor, vision, scout, planner_architect, planner_challenger, planner_planner, auditor_reviewer, auditor_auditor, refactor, fast_coder, power_coder, ultra_coder, frontend_coder
- **5 role member** (commit-boundary): planner_architect, planner_challenger, planner_planner, auditor_reviewer, auditor_auditor
- **Cổng chất lượng cơ học**: Mọi output của member được parse, chấm điểm, và worst-merge vào verdict của lead. Member chỉ có thể HẠ điểm dimension, không bao giờ nâng. Một `fail` duy nhất trên bất kỳ dimension nào = block phase.
- **Phase cap**: 22 cuộc gọi nội bộ mỗi client request — có giới hạn thiết kế, không bao giờ vô hạn.

> 📖 Xem [Kiến trúc: Blacksand Model Routing](docs/ARCHITECTURE.md#blacksand-model-routing) để xem spec ma trận đầy đủ.

---

## ⚔️ BSL Router vs Các AI Gateway Khác

| Tính Năng | **BSL Router** | LiteLLM | One-API | Portkey |
|---|---|---|---|---|
| **Dịch giao thức** | ✅ Hai chiều (OpenAI ↔ Anthropic ↔ Gemini) | ✅ Chỉ OpenAI | ✅ Chỉ OpenAI | ✅ Chỉ OpenAI |
| **Định tuyến 5 tầng** | ✅ Ma trận Blacksand (13 cat × 3 tầng) | ❌ Chọn model thủ công | ❌ Chọn model thủ công | ❌ Chỉ conditional routing |
| **Agentic orchestration** | ✅ Dispatch 15 role + cổng chất lượng | ❌ Không có | ❌ Không có | ❌ Không có |
| **Cổng chất lượng cơ học** | ✅ Worst-score-wins, fail=block | ❌ Không có | ❌ Không có | ❌ Guardrails (khác) |
| **Failover retry vô hạn** | ✅ Chuỗi wrap với backoff 2s→30s | ⚠️ Fallback cơ bản | ⚠️ Fallback cơ bản | ✅ Fallback + retry |
| **Parity tham số thinking** | ✅ 11 họ model, test conform | ❌ Không có | ❌ Không có | ❌ Không có |
| **Phân giải model-ID mờ** | ✅ Gõ sai vẫn đúng | ❌ Match chính xác | ❌ Match chính xác | ❌ Match chính xác |
| **FEL làm mềm refusal** | ✅ Directive pre-flight + auto-retry | ❌ Không có | ❌ Không có | ❌ Không có |
| **Document Intelligence** | ✅ Parse PDF/DOCX/XLSX/PPTX | ❌ Không có | ❌ Không có | ❌ Không có |
| **Vision Bridge** | ✅ Ảnh→text cho model không vision | ❌ Không có | ❌ Không có | ❌ Không có |
| **MITM proxy mode** | ✅ Chặn OS-level cho app hardcoded | ❌ Không có | ❌ Không có | ❌ Không có |
| **Tích hợp Antigravity IDE** | ✅ Overlay direct-inference | ❌ Không có | ❌ Không có | ❌ Không có |
| **Lưu key mã hóa** | ✅ Fernet + machine-bound | ⚠️ Env vars plaintext | ⚠️ Lưu DB | ⚠️ Cloud-managed |
| **Dashboard admin** | ✅ Web UI 9 tab | ⚠️ UI cơ bản | ✅ Web UI | ✅ Cloud dashboard |
| **Self-hosted** | ✅ 100% local, không phụ thuộc cloud | ✅ Self-hosted | ✅ Self-hosted | ❌ Cloud-first |
| **Số provider** | 12+ | 100+ | 20+ | 200+ |

> **Tóm lại:** LiteLLM/One-API/Portkey là gateway giao thức — chúng forward request. BSL Router là **engine trí tuệ** — nó phân loại, định tuyến, orchestrate, kiểm tra chất lượng, và chiến đấu để giao request. Ma trận 5 tầng Blacksand không tồn tại ở đâu khác.

---

## 🚀 Bắt Đầu Nhanh

### 1. Cài Đặt

```bash
git clone https://github.com/dongden-alt/bsl-router.git
cd bsl-router
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate

pip install -r requirements.txt
```

### 2. Cấu Hình

```bash
cp config.example.yaml config.yaml
# Sửa config.yaml — thêm API key của các provider
```

### 3. Chạy

```bash
python -m uvicorn app.main:app --host 0.0.0.0 --port 6969
```

Mở `http://localhost:6969` → trang quản trị.

> **Người dùng Windows**: Dùng `.\scripts\bslrouter.ps1 -start` để khởi động có quản lý, tự dọn port, dual-stack health check, và supervisor process.

### 4. Trỏ Client Vào

| Client của bạn | Base URL | Cách cấu hình |
|---|---|---|
| **Claude Code** | `http://localhost:6969` | Env var `ANTHROPIC_BASE_URL` |
| **Cursor** | `http://localhost:6969/v1` | Setting OpenAI-compatible |
| **OpenAI client bất kỳ** | `http://localhost:6969/v1` | Drop-in thay thế |
| **Anthropic client bất kỳ** | `http://localhost:6969` | Endpoint `/v1/messages` |
| **Antigravity IDE** | Cấu hình từ dashboard → tab Endpoint | Overlay trực tiếp, không cần MITM |

Giờ gửi `model: "blacksand-chat"` và xem BSL Router phân loại, định tuyến, và giao hàng.

---

## 🔑 Tính Năng Chính

### 🧠 Định Tuyến Thông Minh + Ma Trận Blacksand
Gửi `model: blacksand-chat` → BSL Router phân loại (13 danh mục × 3 tầng), định tuyến đến provider tốt nhất, và tự fallback. Client không bao giờ thấy lỗi.

### 🔄 Dịch Giao Thức Hai Chiều

| Từ | Sang | Trạng thái |
|---|---|---|
| OpenAI | Anthropic | ✅ |
| OpenAI | Gemini | ✅ |
| Anthropic | OpenAI | ✅ |
| Anthropic | Gemini | ✅ |
| Gemini | OpenAI | ✅ |
| Bất kỳ | Bất kỳ | ✅ |

Format message, tool call, tham số thinking, streaming — tất cả được dịch hai chiều.

### 🛡️ Engine Dự Phòng Retry Vô Hạn

Nếu provider đầu lỗi, BSL Router thử provider tiếp theo — **và quay vòng qua các lượt retry**: mỗi lượt bấm API key tiếp theo trong pool, giới hạn bởi đồng hồ scale theo chuỗi để provider lỗi chậm không làm kẹt phần còn lại. Khi **cả chuỗi** thất bại, router quay vòng lại với backoff lũy thừa (2s→30s) cho đến khi client ngắt — hết chuỗi là ranh giới lượt, không bao giờ là ngõ cụt.

### 🧠 Chuẩn Hóa Tham Số Thinking (11 Họ Model)

Từ vựng reasoning của mỗi họ model khóa theo tài liệu chính thức qua conformance test:

| Họ | Từ Vựng Reasoning |
|---|---|
| GLM-5.2/5.3 | `none` / `auto` / `thinking` |
| Grok-4.5/4.6 | `auto` / `on` / `off` |
| Qwen 3.8 | `no_think` / `enable_thinking` |
| Kimi K2/K3 | `default` / `auto` / `none` |
| DeepSeek V4 | `low` / `medium` / `high` / `max` |
| Claude | `none` / `auto` / `enabled` |
| GPT-5 | `low` / `medium` / `high` / `max` |
| + 4 nữa | Theo từng họ |

Từ sai bị **ép đúng trước khi đến upstream** — tham số bị từ chối tự hạ cấp và thử lại.

### 🔍 Phân Giải Model-ID Mờ

Gõ sai vẫn định tuyến đúng: `gpt-5-6-terra`, `gpt-terra-5-6` → `gpt-5.6-terra`. Tên chính xác không bao giờ bị viết lại.

### 🛡️ FEL: Làm Mềm Refusal & Directive Egress

Khi provider từ chối request (content policy, safety filter), BSL Router:
1. **Phát hiện** loại refusal (CN-family, GPT-family, Claude-family, Gemini-family)
2. **Inject directive clarity pre-flight** — đóng khung lại request để giảm trigger sai
3. **Auto-retry** với egress framing làm mềm
4. **Track analytics** — tỷ lệ refusal theo họ, tỷ lệ recovery, top model bị refusal

Toggle per-family cho phép bật FEL chỉ cho provider family cụ thể.

### 🧰 Pipeline Tools & Intelligence

Xử lý nội dung tích hợp **trước** khi request đến provider:

| Tool | Chức Năng |
|---|---|
| **Document Intelligence** | Parse PDF, DOCX, XLSX, PPTX → tóm tắt tài liệu lớn trước khi gửi |
| **Vision Bridge** | Chặn URL ảnh gửi cho model không vision → thay bằng mô tả text chi tiết |
| **Token Budget** | Trần max_tokens cứng (1024–65535) chống vượt chi phí, có sàn chống truncate khi tắt |
| **Prompt Caching** | Theo provider: Anthropic explicit, Kimi key-bound, OpenAI cache-key, static-first sorting |
| **Context Compaction** | Guard tỷ lệ code-traffic 1.75× + EMA feedback + smart gate — nén context trước khi chạm token limit |
| **GLM Parallel Tool Guard** | Inject `disable_parallel_tool_use` vào payload GLM — sửa lỗi mất tool args trong batch đa tool |
| **Degenerate Output Guard** | Phát hiện response rỗng/zero-token và tự retry |

### 📊 Chỉ Báo Quota Trực Tiếp

Quota còn lại theo từng key (từ billing endpoint one-api/new-api và header rate-limit) hiển thị thanh % gọn ngay dòng trạng thái key — cho cả API key lẫn tài khoản OAuth. Chỉ phần trăm, không hiển thị tiền.

### 🕴 Tích Hợp Antigravity IDE

Overlay direct-inference cho Antigravity IDE — không cần MITM proxy:
- Map slot model của Antigravity vào provider/combo của BSL Router
- Slot không map dùng native Google Cloud Code
- `thought_signature` giữ nguyên xuyên suốt qua các lượt tool call — yêu cầu bắt buộc của model Gemini thinking, được tái chèn từ cache có giới hạn phía router ngay cả khi client lược bỏ
- Cấu hình từ dashboard → tab Endpoint

### 🌐 Truy Cập Từ Xa

| Phương Pháp | Use Case |
|---|---|
| **Cloudflare Tunnel** | URL public an toàn qua Cloudflared — chia sẻ với team |
| **Tailscale** | Chia sẻ trên Tailnet — mesh network private |
| **API Keys** | Sinh key scoped cho ứng dụng khác |
| **Shared Pool (Ed25519)** | Xác thực Tier-2 cho pool peer router-to-router |

### 🔐 Bảo Mật Tích Hợp

- API key lưu **mã hóa** trên disk (Fernet encryption)
- Dashboard admin bảo vệ bằng password + session hết hạn
- Bộ quét bảo mật kiểm tra config provider cho exfil URL, key injection, URL spoofing
- Khóa mã hóa gắn máy (config không portable)
- Ed25519 signature verification tùy chọn cho traffic inbound pool-peer

### ⏱️ Chống Treo (Anti-Freeze)

- Stream hard deadline 10 phút chống hang vô hạn
- Chain deadline: tổng ngân sách qua tất cả fallback hop chống timeout cascading
- Circuit breaker: provider lỗi tự động bị xoay ra
- Stream guard: validate integrity SSE bắt chunk lỗi
- Proxy bypass connection-level: DIRECT egress escape hatch per-connection

### 📊 Bảng Điều Khiển

Giao diện web admin với 9 tab:

| Tab | Chức Năng |
|---|---|
| **Endpoint** | Endpoint local, truy cập xa (Cloudflare/Tailscale), API key, tích hợp Antigravity |
| **Providers** | Quản lý AI provider — thêm, sửa, xóa, verify |
| **Combos** | Định nghĩa chuỗi fallback và combo alias |
| **BSL Models** | Cấu hình ma trận 5 Blacksand model |
| **MITM** | Proxy trong suốt tùy chọn cho app hardcoded URL |
| **Tools** | Document Intelligence, Vision Bridge, Token Budget, Prompt Caching, FEL toggles |
| **Usage** | Thống kê per-model, tracking chi phí, FEL analytics, dải request đang chạy (in-flight) trực tiếp |
| **Logs** | Log request/response trực tiếp với filter |
| **Settings** | Password admin, shutdown, logout |

---

## 📡 Provider Hỗ Trợ

| Provider | Giao Thức | Xác Thực | Model Phổ Biến |
|---|---|---|---|
| **OpenAI** | OpenAI | API Key | GPT-4o, GPT-5.x |
| **Anthropic** | Anthropic | API Key | Claude 4.x, 5.x |
| **Google Gemini** | Gemini | OAuth | Gemini 2.x |
| **DeepSeek** | OpenAI | API Key | DeepSeek V3, V4 |
| **GLM (Zhipu)** | Anthropic | API Key | GLM-5.x |
| **MiniMax** | OpenAI | API Key | MiniMax M3 |
| **Kimi (Moonshot)** | OpenAI | API Key | Kimi K3 |
| **Qwen (Alibaba)** | OpenAI | API Key | Qwen 3.x |
| **Grok (xAI)** | OpenAI | API Key | Grok 4 |
| **OpenRouter** | OpenAI | API Key | Đa model |
| **GitHub Models** | OpenAI | OAuth | Đa dạng |
| **Google Cloud Code** | Gemini | OAuth | Gemini 2.x |

> **Thêm provider mới?** Chỉ cần thêm mục vào `config.yaml` với `type: custom`. Không cần sửa code. Hỗ trợ `type: image_custom` cho provider tạo ảnh/video.

---

## 📡 API Endpoints

Trỏ app AI vào BSL Router — hoạt động như drop-in thay thế:

| Đang Làm Gì | Endpoint | Format |
|---|---|---|
| Chat với model | `POST /v1/chat/completions` | OpenAI |
| Chat với model | `POST /v1/messages` | Anthropic |
| Liệt kê model | `GET /v1/models` | OpenAI |
| Kiểm tra sức khỏe | `GET /health` | — |

---

## 🌐 Tùy Chọn: MITM Proxy Mode

Cho app không cho đổi API URL, BSL Router chặn traffic ở OS level. Watchdog tự khởi động lại proxy nếu sập.

> ⚠️ Chỉ cần cho app có API URL cố định. Phần lớn app cho phép đặt base URL riêng.

---

## ✅ Verification

| Chỉ Số | Giá Trị |
|---|---|
| **Test suite** | 2.800+ test qua 20+ suite |
| **Tỷ lệ pass** | 100% (0 fail, 0 regression) |
| **Test cổng chất lượng** | 18/18 (worst-score-wins, verdict, member merge) |
| **Test orchestration** | 46/46 (orchestrator:20, ultra:13, max:13) |
| **Runtime full suite** | ~315 giây |
| **Python version** | 3.11+ |
| **Framework** | FastAPI 0.129+ |

---

## 📖 Tài Liệu

| Tài Liệu | Nội Dung |
|---|---|
| [Kiến trúc](docs/ARCHITECTURE.md) | Cách BSL Router hoạt động bên trong — lifecycle request đầy đủ |
| [Đóng góp](CONTRIBUTING.md) | Setup dev và hướng dẫn PR |
| [Bảo mật](SECURITY.md) | Báo cáo lỗ hổng và tính năng bảo mật |
| [Nhật ký thay đổi](CHANGELOG.md) | Lịch sử phiên bản |
| [Config mẫu](config.example.yaml) | Template cấu hình có chú thích |

---

## 📄 Giấy Phép

MIT License — xem [LICENSE](LICENSE) để biết chi tiết.

**Xây dựng bởi [Đồng Tôn](https://github.com/dongden-alt)**
