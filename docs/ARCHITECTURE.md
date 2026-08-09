# How BSL Router Works

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

Each provider family has its own **family adapter** (12 adapters total):

| Adapter | Quirks handled |
|---|---|
| `openai.py` | Standard OpenAI format, cache-key routing |
| `anthropic.py` | Thinking blocks, cache_control, prompt_tokens_details |
| `gemini.py` | Gemini envelope format, streamGenerateContent |
| `deepseek.py` | Reasoning effort mapping |
| `glm.py` | Tool call format translation (via `glm_tools.py`) |
| `kimi.py` | Key-bound prompt caching |
| `minimax.py` | Reasoning effort mapping |
| `qwen.py` | Complex - 11KB adapter for Qwen's unique format quirks |
| `grok.py` | Standard OpenAI-compatible |
| `openrouter.py` | Multi-model routing |

Infrastructure adapters: `_base.py` (shared base), `_effort.py` (reasoning effort ladder), `_legacy_reference.py`.

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
Audits your provider configuration for risky settings. Checks for: exfil URLs, key injection, URL spoofing, credential harvesting, local network exfil, insecure transport, token tampering, and duplicate keys.

Findings are graded **block** (must fix), **warn** (review), or **info**.

### 🕵️ MITM Proxy (Optional)
Intercepts traffic for apps that don't let you change the API URL:
1. Adds entries to your hosts file
2. Runs a transparent proxy on port 443
3. Watchdog process monitors and restarts if it crashes
4. Tree-kill + verify+retry loop ensures clean port cleanup

### 📊 Observability
- Every request/response logged in JSONL format
- Usage stats per model with cost tracking
- Error tracking with per-provider breakdown
- Live log streaming in the admin dashboard

### 🔍 Scouts (Optional)
Pre-process specific content types before routing:

| Scout | Purpose |
|---|---|
| **Vision** | Analyzes images, polyfills vision for providers that don't support it |
| **Docs Parser** | Extracts text from documents for RAG |
| **Canvas** | Analyzes UI/canvas elements |

---

## Middleware Pipeline

The routing pipeline consists of 26 middleware modules. Here's the full inventory:

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

---

## Admin Dashboard Architecture

The admin dashboard is a single-page app (`app/static/`) with 8 tabs:

| Tab | HTML ID | Purpose |
|---|---|---|
| **Endpoint** | `endpoint` | Local endpoints, Cloudflare Tunnel, Tailscale, API keys, Antigravity integration |
| **Providers** | `providers` | Provider CRUD, connection verification, model management |
| **Combos** | `combos` | Combo alias definition and fallback chain editor |
| **BSL Models** | `bsl-models` | Matrix editor for all 5 Blacksand models, auto-select, read-only cross-references |
| **MITM** | `mitm` | MITM proxy control, hosts file management, watchdog status |
| **Tools** | `tools` | Document Intelligence, Vision Bridge, Token Budget, Prompt Caching |
| **Usage** | `usage` | Per-model usage table with filtering and cost tracking |
| **Logs** | `logs` | Live request/response log streaming |
| **Settings** | `settings` | Admin password, shutdown, logout |

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
│   │   ├── families/            # 12 provider family adapters
│   │   ├── stream_normalizer.py # Stream format normalization
│   │   ├── tool_ledger.py       # Tool call ID tracking
│   │   └── reasoning_policy.py  # Thinking parameter rules
│   ├── middleware/              # 26 routing pipeline modules
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
│   │   └── ...                        # + 7 more modules
│   ├── routing/
│   │   └── combo_resolver.py    # Combo chain resolution + fallback
│   ├── scouts/                  # Vision, docs, canvas analysis
│   ├── security/                # Key scanning
│   ├── static/                  # Admin dashboard (HTML/JS/CSS)
│   └── tests/                   # 64+ test files
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
| `POST /api/tunnel/cloudflare/*` | Manage Cloudflare tunnels |
| `GET/POST /api/bsl-matrix/*` | Read/apply Blacksand matrix config |
| `GET/POST /api/antigravity/*` | Antigravity integration control |
| `POST /api/auth/login` | Admin login |

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
12 family adapter: openai, anthropic, gemini, deepseek, glm, kimi, minimax, qwen, grok, openrouter + infrastructure adapters.

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
Kiểm tra: exfil URL, key injection, URL spoofing, credential harvesting, local network exfil, insecure transport, token tampering, duplicate keys.

### 🕵️ MITM Proxy (Tùy chọn)
Chặn traffic cho app có URL cố định. Watchdog tự restart. Tree-kill + verify+retry.

### 📊 Quan Sát & Ghi Log
JSONL logging, usage stats, error tracking, live log streaming.

### 🔍 Scouts (Tùy chọn)
Vision (phân tích ảnh), Docs Parser (trích text), Canvas (UI/canvas).

---

## Middleware Pipeline

26 module middleware:

**Classification**: request_intent, category_classifier, coding_category_classifier, task_complexity

**Routing**: route_registry, bsl_chat_router, bsl_lite_router, bsl_agentic_router, bsl_agentic_ultra_router, bsl_agentic_max_router, bsl_auto_select, bsl_router_utils

**Orchestration**: bsl_orchestrator, bsl_orchestrator_engine, bsl_orchestrator_gates

**Quality & Efficiency**: quality, efficiency, compaction, thinking_fallback, response_format_guard

**Stream Protection**: stream_guard, caching, glm_tools

**Benchmark**: bsl_benchmark_sheet, bsl_lite_benchmark_sheet, bsl_agentic_benchmark_sheet

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
