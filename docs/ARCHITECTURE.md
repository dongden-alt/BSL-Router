# How BSL Router Works

> A plain-English guide to the architecture. No jargon, no assumptions.
>
> Hướng dẫn kiến trúc bằng ngôn ngữ dễ hiểu. Không thuật ngữ khó, không giả định kiến thức.

**🇬🇧 [English](#the-big-picture)** · **🇻🇳 [Tiếng Việt](#-tiếng-việt)**

---

## The Big Picture

BSL Router does **three things**:

1. **Receives** a request from your AI client
2. **Translates** it into the right format for the target provider
3. **Sends** it upstream and streams the response back

If the provider fails, it automatically tries the next one in your fallback chain.

```
  Your App                BSL Router               AI Providers
  ────────    request    ──────────    translate    ──────────
  Claude   ──────────▶  Route +    ──────────────▶  OpenAI
  Code                   Translate                   Anthropic
           ◀──────────  Normalize  ◀──────────────   Google
             response    stream       response       DeepSeek
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
BSL Router looks at your request and classifies it:
- **Simple chat** → lightweight routing
- **Code generation** → routes to coding-optimized models
- **Agent/tool use** → routes to models that handle tool calls well

### Step 4: Provider Selection
Based on the classification, BSL Router picks the best provider:
- If you specified a model directly → uses that model's provider
- If you specified a **combo** (fallback chain) → starts with the first provider in the chain
- If auto-select is enabled → picks based on capability + cost

### Step 5: Protocol Translation
Your client speaks OpenAI format, but the provider uses Anthropic? BSL Router translates:
- Message format conversion
- Tool call ID mapping
- Thinking/reasoning parameter injection
- Streaming protocol normalization

### Step 6: Upstream Request
BSL Router sends the translated request to the provider with:
- Connection pooling (reuses TCP connections for speed)
- Timeout management
- OAuth token auto-refresh (if the provider uses OAuth)

### Step 7: Response Streaming
The provider streams back chunks. BSL Router:
- Normalizes the stream format back to what your client expects
- Validates stream integrity (catches malformed chunks)
- Handles thinking/reasoning blocks per provider rules

### Step 8: Fallback (if needed)
If the provider returns an error:
1. BSL Router checks if there's a next provider in the combo chain
2. If yes → repeats Steps 5-7 with the next provider
3. If no more providers → returns the error to your client
4. A deadline timer prevents infinite retries

---

## The Five Routing Tiers

BSL Router has five routing tiers, from simplest to most complex:

| Tier | Name | When it's used |
|---|---|---|
| 1 | **Chat** | Simple conversations, Q&A |
| 2 | **Lite** | Quick tasks, short responses |
| 3 | **Agentic** | Tool-using requests, function calls |
| 4 | **Ultra** | Complex multi-step agent workflows |
| 5 | **Max** | Maximum capability — orchestrated multi-model tasks |

> You don't choose the tier manually. BSL Router classifies your request and picks the right tier automatically.

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

Each provider family has its own quirks (different tool call formats, thinking parameters, stream chunk shapes). BSL Router handles all of them through **family adapters** — one per provider family.

### 🔗 Combo/Chain Fallback
**What it does**: Tries multiple providers in order until one works.

You define chains in your config:
```yaml
combos:
  - alias: my-chain
    chain:
      - provider: openai
        model: gpt-4o          # Try first
      - provider: deepseek
        model: deepseek-v4     # Fallback
    strategy: fallback
```

The system uses a **deadline timer** to prevent infinite retries. If all providers in the chain fail within the deadline, the error is returned to your client.

### 🔐 Credential Management
**What it does**: Keeps your API keys encrypted and OAuth tokens fresh.

- **API keys**: Stored encrypted with [Fernet](https://cryptography.io/en/latest/fernet/) symmetric encryption in `config.yaml`
- **OAuth tokens**: Automatically refreshed before expiry — you never see "token expired" errors
- **Key scanner**: On-demand security audit of your provider settings (see below)

> [!IMPORTANT]
> The encryption key is **machine-bound**. Copying `config.yaml` to another computer will not carry the credentials over — you'll need to re-enter your API keys there.

### 🛡️ Security Scanner
**What it does**: Audits your provider configuration for risky settings.

Run it on demand from the admin UI, or call `GET /api/scan-keys`. It checks for:

| Check | What it catches |
|---|---|
| Exfil URL | `base_url` pointing at webhook/paste/tunnel services |
| Key injection | Shell, SQL, or HTML injection patterns inside an API key |
| URL spoofing | Lookalike domains, e.g. `api.openai.com.evil.com` |
| Credential harvesting | Credentials passed as URL query parameters |
| Local network exfil | Cloud provider pointed at a private IP |
| Insecure transport | Plain `http://` which sends keys in cleartext |
| Token tampering | OAuth tokens that don't match the expected format |
| Duplicate keys | The same key reused across multiple providers |

Findings are graded **block** (must fix), **warn** (review), or **info**.

### 🕵️ MITM Proxy (Optional)
**What it does**: Intercepts traffic for apps that don't let you change the API URL.

How it works:
1. Adds entries to your hosts file (e.g., `127.0.0.1 api.openai.com`)
2. Runs a transparent proxy on port 443
3. Intercepts requests that your app thinks are going to OpenAI
4. Routes them through BSL Router instead
5. A watchdog process monitors the proxy and restarts it if it crashes

> This is only needed for apps with hardcoded API URLs. Most apps let you set a custom base URL — in that case, just point it at `http://localhost:6969`.

### 📊 Observability
**What it does**: Logs everything so you can debug issues and track usage.

- Every request/response is logged in JSONL format
- Usage stats available via the admin dashboard
- Error tracking with per-provider breakdown

### 🔍 Scouts (Optional)
**What they do**: Pre-process specific content types before routing.

| Scout | Purpose |
|---|---|
| **Vision** | Analyzes images in your request, polyfills vision for providers that don't support it |
| **Docs Parser** | Extracts text from documents for RAG (retrieval-augmented generation) |
| **Canvas** | Analyzes UI/canvas elements |

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
│   ├── mitm.py                  # MITM proxy management
│   ├── compat/                  # Protocol translation
│   │   ├── families/            # Per-provider adapters (12 families)
│   │   ├── stream_normalizer.py # Stream format normalization
│   │   ├── tool_ledger.py       # Tool call ID tracking
│   │   └── reasoning_policy.py  # Thinking parameter rules
│   ├── middleware/              # Routing pipeline
│   │   ├── bsl_chat_router.py   # Tier 1: Chat
│   │   ├── bsl_lite_router.py   # Tier 2: Lite
│   │   ├── bsl_agentic_router.py       # Tier 3: Agentic
│   │   ├── bsl_agentic_ultra_router.py # Tier 4: Ultra
│   │   ├── bsl_agentic_max_router.py   # Tier 5: Max
│   │   ├── bsl_auto_select.py   # Automatic model selection
│   │   ├── task_complexity.py   # Request complexity estimation
│   │   └── ...                  # Stream guards, caching, etc.
│   ├── routing/
│   │   └── combo_resolver.py    # Combo chain resolution + fallback
│   ├── scouts/                  # Vision, docs, canvas analysis
│   ├── security/                # Key scanning
│   ├── static/                  # Admin dashboard (HTML/JS/CSS)
│   └── tests/                   # 64 test files
├── scripts/                     # Management scripts
├── tests/                       # Integration tests
├── config.example.yaml          # Config template
└── requirements.txt             # Python dependencies
```

---

## Configuration Reference

BSL Router uses a single `config.yaml` file. Here's the structure:

```yaml
# Define your AI providers
providers:
  my-openai:                           # Your name for this provider
    type: custom                       # "custom" for user-defined
    format: openai                     # Protocol: openai | anthropic | gemini
    connections:
      - api_key: enc:YOUR_KEY_HERE     # Encrypted API key
        base_url: https://api.openai.com/v1
    models:
      - id: gpt-4o                     # Model ID
        enabled: true
        thinking: auto                 # Thinking mode: auto | max | off

# Define fallback chains
combos:
  - alias: my-fallback-chain
    chain:
      - provider: my-openai
        model: gpt-4o
    strategy: fallback                 # Strategy: fallback

# Admin dashboard settings
admin:
  password_enabled: false
  password: enc:YOUR_ADMIN_PASS_HERE
```

See [config.example.yaml](../config.example.yaml) for a complete working template.

---

## API Reference

### For AI Clients (drop-in compatible)

| Endpoint | What it does |
|---|---|
| `POST /v1/chat/completions` | Chat completions (OpenAI format) |
| `POST /v1/messages` | Messages (Anthropic format) |
| `GET /v1/models` | List all available models |
| `GET /health` | Health check |

### For the Admin Dashboard

| Endpoint | What it does |
|---|---|
| `GET/POST /api/config` | Read or update configuration |
| `GET/POST /api/scan-keys` | Run the security scanner |
| `GET/POST /api/mitm/*` | Control MITM proxy |
| `GET /api/observability/usage` | View usage statistics |
| `POST /api/tunnel/cloudflare/*` | Manage Cloudflare tunnels |
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

BSL Router làm **ba việc**:

1. **Nhận** request từ app AI của bạn
2. **Dịch** nó sang đúng format của provider đích
3. **Gửi** lên provider và stream response về

Nếu provider lỗi, nó tự động thử provider tiếp theo trong chuỗi dự phòng.

```
  App của bạn             BSL Router               AI Providers
  ───────────   request   ──────────    dịch        ──────────
  Claude    ──────────▶  Định tuyến ──────────────▶  OpenAI
  Code                    + Dịch                     Anthropic
            ◀──────────  Chuẩn hóa  ◀──────────────  Google
              response     stream      response      DeepSeek
                                                     ...v.v.
```

---

## Điều Gì Xảy Ra Khi Bạn Gửi Request

Hành trình đầy đủ của một request chat, từng bước:

### Bước 1: Request Đến
App của bạn gửi `POST /v1/chat/completions` (hoặc `/v1/messages` cho format Anthropic).

### Bước 2: Kiểm Tra Xác Thực
Nếu bật admin auth, BSL Router xác minh session. Request API dùng BSL key trong config.

### Bước 3: Phân Loại Request
BSL Router xem request và phân loại:
- **Chat đơn giản** → định tuyến nhẹ
- **Sinh code** → chuyển tới model tối ưu cho code
- **Dùng tool/agent** → chuyển tới model xử lý tool tốt

### Bước 4: Chọn Provider
Dựa trên phân loại, BSL Router chọn provider tốt nhất:
- Nếu bạn chỉ định model cụ thể → dùng provider của model đó
- Nếu bạn chỉ định **combo** (chuỗi dự phòng) → bắt đầu từ provider đầu chuỗi
- Nếu bật auto-select → chọn theo năng lực + chi phí

### Bước 5: Dịch Giao Thức
App nói format OpenAI, provider dùng format Anthropic? BSL Router dịch:
- Chuyển đổi format tin nhắn
- Map ID của tool call
- Chèn tham số thinking/reasoning
- Chuẩn hóa giao thức streaming

### Bước 6: Gửi Lên Provider
BSL Router gửi request đã dịch kèm:
- Connection pooling (tái dùng kết nối TCP cho nhanh)
- Quản lý timeout
- Tự làm mới OAuth token (nếu provider dùng OAuth)

### Bước 7: Stream Response
Provider stream về từng chunk. BSL Router:
- Chuẩn hóa format stream về đúng cái app mong đợi
- Kiểm tra tính toàn vẹn stream (bắt chunk lỗi)
- Xử lý khối thinking/reasoning theo quy tắc từng provider

### Bước 8: Dự Phòng (nếu cần)
Nếu provider trả lỗi:
1. BSL Router kiểm tra còn provider tiếp theo trong combo không
2. Nếu có → lặp lại Bước 5-7 với provider kế tiếp
3. Nếu hết provider → trả lỗi về cho app
4. Bộ đếm deadline ngăn thử lại vô hạn

---

## Năm Tầng Định Tuyến

BSL Router có năm tầng, từ đơn giản đến phức tạp nhất:

| Tầng | Tên | Dùng khi nào |
|---|---|---|
| 1 | **Chat** | Hội thoại đơn giản, hỏi đáp |
| 2 | **Lite** | Việc nhanh, response ngắn |
| 3 | **Agentic** | Request dùng tool, function call |
| 4 | **Ultra** | Luồng agent nhiều bước phức tạp |
| 5 | **Max** | Năng lực tối đa — điều phối nhiều model |

> Bạn không cần chọn tầng thủ công. BSL Router tự phân loại và chọn tầng phù hợp.

---

## Thành Phần Chính

### 🔄 Tầng Dịch Giao Thức
**Chức năng**: Chuyển đổi giữa các format API để app nào cũng dùng được provider nào.

BSL Router hỗ trợ ba họ giao thức:

| Giao thức | Ai dùng |
|---|---|
| **Format OpenAI** | OpenAI, DeepSeek, Kimi, Qwen, Grok, MiniMax, OpenRouter |
| **Format Anthropic** | Anthropic, GLM |
| **Format Gemini** | Google Gemini, Google Cloud Code |

Mỗi họ provider có đặc thù riêng (format tool call, tham số thinking, hình dạng chunk stream khác nhau). BSL Router xử lý tất cả qua **family adapter** — mỗi họ một adapter.

### 🔗 Chuỗi Dự Phòng (Combo)
**Chức năng**: Thử lần lượt nhiều provider cho tới khi có cái chạy được.

Định nghĩa chuỗi trong config:
```yaml
combos:
  - alias: chuoi-cua-toi
    chain:
      - provider: openai
        model: gpt-4o          # Thử trước
      - provider: deepseek
        model: deepseek-v4     # Dự phòng
    strategy: fallback
```

Hệ thống dùng **bộ đếm deadline** để ngăn thử lại vô hạn. Nếu tất cả provider trong chuỗi đều lỗi trong thời hạn, lỗi được trả về app.

### 🔐 Quản Lý Thông Tin Đăng Nhập
**Chức năng**: Giữ API key được mã hóa và OAuth token luôn mới.

- **API key**: Lưu mã hóa bằng [Fernet](https://cryptography.io/en/latest/fernet/) trong `config.yaml`
- **OAuth token**: Tự động làm mới trước khi hết hạn — bạn không bao giờ gặp lỗi "token expired"
- **Key scanner**: Kiểm tra bảo mật cấu hình provider theo yêu cầu (xem dưới)

> [!IMPORTANT]
> Khóa mã hóa **gắn với máy**. Copy `config.yaml` sang máy khác sẽ KHÔNG mang theo được thông tin đăng nhập — bạn phải nhập lại API key ở máy đó.

### 🛡️ Bộ Quét Bảo Mật
**Chức năng**: Kiểm tra cấu hình provider để tìm thiết lập rủi ro.

Chạy theo yêu cầu từ trang quản trị, hoặc gọi `GET /api/scan-keys`. Nó kiểm tra:

| Kiểm tra | Phát hiện gì |
|---|---|
| Exfil URL | `base_url` trỏ tới dịch vụ webhook/paste/tunnel |
| Key injection | Mẫu tấn công shell, SQL, HTML nằm trong API key |
| URL spoofing | Domain giả mạo, ví dụ `api.openai.com.evil.com` |
| Credential harvesting | Thông tin đăng nhập truyền qua query parameter |
| Local network exfil | Provider cloud lại trỏ vào IP nội bộ |
| Insecure transport | Dùng `http://` khiến key truyền dạng thô |
| Token tampering | OAuth token không đúng định dạng mong đợi |
| Duplicate keys | Cùng một key dùng lại ở nhiều provider |

Kết quả phân loại **block** (phải sửa), **warn** (nên xem lại), hoặc **info**.

### 🕵️ MITM Proxy (Tùy chọn)
**Chức năng**: Chặn traffic cho những app không cho đổi API URL.

Cách hoạt động:
1. Thêm dòng vào file hosts (ví dụ `127.0.0.1 api.openai.com`)
2. Chạy proxy trong suốt trên cổng 443
3. Chặn request mà app tưởng đang gửi tới OpenAI
4. Chuyển chúng qua BSL Router
5. Watchdog theo dõi proxy và tự khởi động lại nếu sập

> Chỉ cần cho app có API URL cố định. Phần lớn app cho phép đặt base URL riêng — khi đó chỉ cần trỏ vào `http://localhost:6969`.

### 📊 Quan Sát & Ghi Log
**Chức năng**: Ghi lại mọi thứ để bạn debug và theo dõi mức dùng.

- Mọi request/response ghi dạng JSONL
- Thống kê sử dụng xem được trên trang quản trị
- Theo dõi lỗi chi tiết theo từng provider

### 🔍 Scouts (Tùy chọn)
**Chức năng**: Tiền xử lý các loại nội dung đặc biệt trước khi định tuyến.

| Scout | Mục đích |
|---|---|
| **Vision** | Phân tích ảnh trong request, bù năng lực vision cho provider không hỗ trợ |
| **Docs Parser** | Trích xuất text từ tài liệu cho RAG |
| **Canvas** | Phân tích phần tử UI/canvas |

---

## Cấu Trúc Dự Án

Xem [phần English](#project-structure) — cấu trúc thư mục giống nhau, chú thích bằng tiếng Anh trong code.

Các file quan trọng nhất:

| File | Vai trò |
|---|---|
| `app/main.py` | Điểm khởi động server + toàn bộ route HTTP |
| `app/config_state.py` | Nạp config và hot-reload |
| `app/crypto.py` | Mã hóa/giải mã Fernet |
| `app/oauth.py` | Luồng OAuth 2.0 + làm mới token |
| `app/compat/` | Dịch giao thức giữa các provider |
| `app/middleware/` | Pipeline định tuyến 5 tầng |
| `app/routing/combo_resolver.py` | Giải chuỗi combo + dự phòng |
| `app/security/key_scanner.py` | Quét bảo mật cấu hình |
| `app/static/` | Trang quản trị (HTML/JS/CSS) |

---

## Tham Chiếu Cấu Hình

BSL Router dùng duy nhất một file `config.yaml`:

```yaml
# Khai báo provider AI
providers:
  my-openai:                           # Tên bạn tự đặt
    type: custom                       # "custom" cho provider tự định nghĩa
    format: openai                     # Giao thức: openai | anthropic | gemini
    connections:
      - api_key: enc:YOUR_KEY_HERE     # API key đã mã hóa
        base_url: https://api.openai.com/v1
    models:
      - id: gpt-4o                     # ID model
        enabled: true
        thinking: auto                 # Chế độ thinking: auto | max | off

# Khai báo chuỗi dự phòng
combos:
  - alias: chuoi-du-phong
    chain:
      - provider: my-openai
        model: gpt-4o
    strategy: fallback

# Thiết lập trang quản trị
admin:
  password_enabled: false
  password: enc:YOUR_ADMIN_PASS_HERE
```

Xem [config.example.yaml](../config.example.yaml) để có template hoàn chỉnh.

---

## Tham Chiếu API

### Cho App AI (thay thế trực tiếp)

| Endpoint | Chức năng |
|---|---|
| `POST /v1/chat/completions` | Chat completion (format OpenAI) |
| `POST /v1/messages` | Messages (format Anthropic) |
| `GET /v1/models` | Liệt kê toàn bộ model |
| `GET /health` | Kiểm tra sức khỏe |

### Cho Trang Quản Trị

| Endpoint | Chức năng |
|---|---|
| `GET/POST /api/config` | Đọc hoặc cập nhật cấu hình |
| `GET/POST /api/scan-keys` | Chạy bộ quét bảo mật |
| `GET/POST /api/mitm/*` | Điều khiển MITM proxy |
| `GET /api/observability/usage` | Xem thống kê sử dụng |
| `POST /api/tunnel/cloudflare/*` | Quản lý Cloudflare tunnel |
| `POST /api/auth/login` | Đăng nhập quản trị |

---

## Thư Viện Phụ Thuộc

| Package | Vì sao cần |
|---|---|
| **FastAPI** | Web framework cho API server |
| **httpx** | HTTP client async gọi provider |
| **mitmproxy** | Proxy trong suốt (tùy chọn, cho chế độ MITM) |
| **cryptography** | Mã hóa Fernet cho API key |
| **PyYAML** | Đọc file config |
| **pydantic** | Kiểm tra dữ liệu request/response |
