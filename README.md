# BSL Router

> **One proxy to rule all your AI models.** Use any AI client with any AI provider - BSL Router handles the translation, failover, and routing automatically.
>
> **Một proxy quản lý tất cả AI model.** Dùng bất kỳ AI client nào với bất kỳ AI provider nào - BSL Router tự động dịch, chuyển hướng, và xử lý lỗi.

[![Python 3.10+](https://img.shields.io/badge/Python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688.svg)](https://fastapi.tiangolo.com/)

**🇬🇧 [English](#-the-problem)** · **🇻🇳 [Tiếng Việt](#-vấn-đề)**

---

## 💡 The Problem

You use multiple AI providers (OpenAI, Anthropic, Google, DeepSeek, etc.), but:

- Your favorite AI client only speaks **one protocol** (OpenAI or Anthropic format)
- When a provider goes down, your workflow **stops**
- Managing API keys, OAuth tokens, and rate limits across providers is **painful**
- You can't easily switch models without changing your client config
- Agentic coding tools need different models for different sub-tasks (planning, coding, review)
- Long reasoning chains freeze your IDE when a provider stalls mid-stream

## ✅ The Solution

BSL Router sits between your AI client and your providers. It speaks every protocol, so your client doesn't have to.

```
┌──────────────┐         ┌──────────────┐         ┌──────────────┐
│  Your AI     │         │              │         │   OpenAI     │
│  Client      │────────▶│  BSL Router  │────────▶│   Anthropic  │
│  (any app)   │◀────────│              │◀────────│   Google     │
└──────────────┘         └──────────────┘         │   DeepSeek   │
                          Translates,              │   GLM        │
                          routes, and              │   + more     │
                          recovers                 └──────────────┘
```

**Think of it as a universal translator + traffic controller + failover shield for AI APIs.**

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
# Edit config.yaml - add your provider API keys
```

### 3. Run

```bash
python -m uvicorn app.main:app --host 0.0.0.0 --port 6969
```

Open `http://localhost:6969` for the admin dashboard.

> **Windows users**: Use `.\\scripts\\bslrouter.ps1 -start` for managed startup with automatic port cleanup.

---

## 🔑 Key Features

### 🔀 Smart Routing
Send a request -> BSL Router picks the best provider automatically. If that provider fails, it tries the next one in your fallback chain. Your client never sees the failure.

### 🧠 Blacksand Routing Models
Five virtual models that handle intelligent multi-model orchestration. Point your client at a single model name and BSL Router handles the rest:

| Model | Purpose | Matrix |
|---|---|---|
| `blacksand-chat` | Category-aware smart routing for general chat | 13 categories × 3 tiers |
| `blacksand-lite` | Coding-agent single-task router | 10 coding agents × 3 tiers |
| `blacksand-agentic` | Fast-tier agentic coding orchestration | Multi-agent dispatch |
| `blacksand-agentic-ultra` | Balanced-tier with consult routing | Agent + consultant dispatch |
| `blacksand-agentic-max` | Multi-domain fusion for complex workflows | Cross-domain orchestration |

Each model uses a **combo alias** system (`coder-1` = fast/cheap, `coder-2` = standard, `coder-3` = strongest) that maps to your configured providers. The matrix is fully configurable per-category.

> See [Architecture: Blacksand Model Routing](docs/ARCHITECTURE.md#blacksand-model-routing) for how the 5-tier system works.

### 🔄 Protocol Translation
Your client speaks OpenAI format? Your provider uses Anthropic format? No problem. BSL Router translates on the fly:

| From | To | Works? |
|---|---|---|
| OpenAI | Anthropic | ✅ |
| OpenAI | Gemini | ✅ |
| Anthropic | OpenAI | ✅ |
| Anthropic | Gemini | ✅ |
| Gemini | OpenAI | ✅ |
| Any | Any | ✅ |

### 🛡️ Automatic Failover
Define fallback chains (called **combos**) in your config:

```yaml
combos:
  - alias: my-smart-chain
    chain:
      - provider: openai       # Try OpenAI first
        model: gpt-4o
      - provider: anthropic    # If OpenAI fails, try Anthropic
        model: claude-sonnet-4
      - provider: deepseek     # Last resort
        model: deepseek-v4
    strategy: fallback
```

If the first provider returns an error, BSL Router automatically tries the next one. A chain deadline timer prevents infinite retries.

### 🧰 Tools & Intelligence
Built-in content processing that runs before your request reaches the provider:

| Tool | What it does |
|---|---|
| **Document Intelligence** | Parses PDF, DOCX, XLSX, PPTX attachments and summarizes large documents before sending |
| **Vision Bridge** | Intercepts image URLs sent to text-only models and replaces them with detailed text descriptions |
| **Token Budget** | Hard max_tokens ceiling to prevent cost overruns (1024–65535 range, with anti-truncation floor when disabled) |
| **Prompt Caching** | Provider-specific caching strategies: Anthropic explicit cache, Kimi key-bound cache, OpenAI cache-key routing, static-first sorting |

### 🕵️ Antigravity IDE Integration
Direct-inference overlay for [Antigravity IDE](https://antigravity.dev) - no MITM proxy needed:

- Map Antigravity's model slots to your BSL Router providers/combos
- Unmapped slots use native Google Cloud Code
- Configure from the admin dashboard Endpoint tab

### 🌐 Remote Access
Expose your local BSL Router to external networks:

| Method | Use case |
|---|---|
| **Cloudflare Tunnel** | Secure public URL via Cloudflared - share with team members |
| **Tailscale** | Share on your Tailnet - private mesh network access |
| **API Keys** | Generate scoped keys for other applications |

### 🔐 Built-in Security
- API keys stored **encrypted** on disk (Fernet encryption)
- Admin dashboard protected by password + session expiry
- Security scanner audits provider config for exfil URLs, key injection, URL spoofing, and more
- Machine-bound encryption key (config is non-portable)

### ⏱️ Anti-Freeze Protection
- **Stream hard deadline**: 10-minute cap per stream prevents infinite hangs
- **Chain deadline**: total budget across all fallback hops prevents cascading timeouts
- **Circuit breaker**: unhealthy providers are automatically rotated out
- **Stream guard**: SSE stream integrity validation catches malformed chunks

### 📊 Monitoring Dashboard
Web-based admin UI with 8 tabs:

| Tab | What it does |
|---|---|
| **Endpoint** | Local endpoints, remote access (Cloudflare/Tailscale), API keys, Antigravity integration |
| **Providers** | Manage AI providers - add, edit, delete, verify connections |
| **Combos** | Define fallback chains and combo aliases |
| **BSL Models** | Configure the 5 Blacksand routing models' matrices |
| **MITM** | Optional transparent proxy for apps with hardcoded API URLs |
| **Tools** | Document Intelligence, Vision Bridge, Token Budget, Prompt Caching |
| **Usage** | Per-model usage statistics and cost tracking |
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

> **Adding a new provider?** Just add a section to `config.yaml` with `type: custom`. No code changes needed. BSL Router also supports `type: image_custom` for image/video generation providers.

---

## 🔌 API Endpoints

Point your AI client at BSL Router and it works like a drop-in replacement:

| What you're doing | Endpoint | Format |
|---|---|---|
| Chat with a model | `POST /v1/chat/completions` | OpenAI |
| Chat with a model | `POST /v1/messages` | Anthropic |
| List available models | `GET /v1/models` | OpenAI |
| Health check | `GET /health` | - |

**Admin endpoints** (for the dashboard):

| What you're doing | Endpoint |
|---|---|
| View/edit config | `GET/POST /api/config` |
| Manage MITM proxy | `GET/POST /api/mitm/*` |
| View usage stats | `GET /api/observability/usage` |
| Manage tunnels | `GET/POST /api/tunnel/*` |
| Run security scan | `GET/POST /api/scan-keys` |
| Update BSL matrix | `GET/POST /api/bsl-matrix/*` |
| Manage Antigravity | `GET/POST /api/antigravity/*` |
| Admin login | `POST /api/auth/login` |

---

## 🌐 Optional: MITM Proxy Mode

For apps that don't let you change the API URL (like some VS Code extensions), BSL Router can intercept traffic at the OS level:

1. Modifies your hosts file to redirect `api.openai.com` -> `localhost`
2. Runs a transparent proxy on port 443
3. Routes intercepted traffic through BSL Router
4. Your app thinks it's talking to OpenAI - but BSL Router handles everything
5. Watchdog process auto-restarts the proxy if it crashes

> ⚠️ This is optional and only needed for apps with hardcoded API URLs. Most apps support custom base URLs - just point them at `http://localhost:6969`.

---

## 📖 Learn More

| Document | What's inside |
|---|---|
| [Architecture](docs/ARCHITECTURE.md) | How BSL Router works under the hood |
| [Contributing](CONTRIBUTING.md) | Development setup and PR guidelines |
| [Security](SECURITY.md) | Vulnerability reporting and security features |
| [Changelog](CHANGELOG.md) | Version history |
| [Config Example](config.example.yaml) | Annotated configuration template |

---

## 📄 License

MIT License - see [LICENSE](LICENSE) for details.

**Built by [Đồng Tôn](https://github.com/dongden-alt)**

---
---

# 🇻🇳 Tiếng Việt

---

## 💡 Vấn Đề

Bạn dùng nhiều AI provider (OpenAI, Anthropic, Google, DeepSeek, v.v.), nhưng:

- App AI yêu thích của bạn chỉ nói **một giao thức** (format OpenAI hoặc Anthropic)
- Khi một provider sập, công việc của bạn **dừng lại**
- Quản lý API key, OAuth token, rate limit giữa nhiều provider **rất mệt**
- Không thể dễ dàng đổi model mà không sửa config client
- Tool agentic coding cần model khác nhau cho từng tác vụ (lập kế hoạch, viết code, review)
- Chuỗi reasoning dài làm treo IDE khi provider bị stall giữa luồng

## ✅ Giải Pháp

BSL Router nằm giữa app AI của bạn và các provider. Nó nói được mọi giao thức, nên app của bạn không cần phải biết.

```
┌──────────────┐         ┌──────────────┐         ┌──────────────┐
│  App AI      │         │              │         │   OpenAI     │
│  của bạn     │────────▶│  BSL Router  │────────▶│   Anthropic  │
│  (bất kỳ)    │◀────────│              │◀────────│   Google     │
└──────────────┘         └──────────────┘         │   DeepSeek   │
                          Dịch, định               │   GLM        │
                          tuyến, và tự             │   + khác     │
                          phục hồi                 └──────────────┘
```

**Hãy nghĩ nó như một phiên dịch viên đa năng + bộ điều phối giao thông + lá chắn dự phòng cho AI API.**

---

## 🚀 Bắt Đầu Nhanh

### 1. Cài đặt

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

### 2. Cấu hình

```bash
cp config.example.yaml config.yaml
# Sửa config.yaml - thêm API key của các provider
```

### 3. Chạy

```bash
python -m uvicorn app.main:app --host 0.0.0.0 --port 6969
```

Mở `http://localhost:6969` để vào trang quản trị.

> **Người dùng Windows**: Dùng `.\\scripts\\bslrouter.ps1 -start` để khởi động có quản lý, tự dọn port.

---

## 🔑 Tính Năng Chính

### 🔀 Định Tuyến Thông Minh
Gửi request -> BSL Router tự chọn provider tốt nhất. Nếu provider đó lỗi, nó tự thử provider tiếp theo. Client của bạn không bao giờ thấy lỗi.

### 🧠 Blacksand Routing Models
Năm model ảo xử lý điều phối đa model thông minh. Trỏ client vào một tên model duy nhất, BSL Router lo phần còn lại:

| Model | Mục đích | Ma trận |
|---|---|---|
| `blacksand-chat` | Định tuyến thông minh theo danh mục cho chat chung | 13 danh mục × 3 tầng |
| `blacksand-lite` | Router tác vụ đơn cho coding agent | 10 coding agent × 3 tầng |
| `blacksand-agentic` | Điều phối agentic coding tầng nhanh | Dispatch đa agent |
| `blacksand-agentic-ultra` | Tầng cân bằng với consult routing | Agent + consultant dispatch |
| `blacksand-agentic-max` | Fusion đa domain cho workflow phức tạp | Điều phối cross-domain |

Mỗi model dùng hệ thống **combo alias** (`coder-1` = nhanh/rẻ, `coder-2` = tiêu chuẩn, `coder-3` = mạnh nhất) map vào provider đã cấu hình. Ma trận cấu hình được tùy chỉnh theo từng danh mục.

### 🔄 Dịch Giao Thức
Client nói format OpenAI? Provider dùng format Anthropic? Không vấn đề. BSL Router dịch ngay lập tức:

| Từ | Sang | Hoạt động? |
|---|---|---|
| OpenAI | Anthropic | ✅ |
| OpenAI | Gemini | ✅ |
| Anthropic | OpenAI | ✅ |
| Anthropic | Gemini | ✅ |
| Gemini | OpenAI | ✅ |
| Bất kỳ | Bất kỳ | ✅ |

### 🛡️ Tự Động Chuyển Hướng Khi Lỗi
Định nghĩa chuỗi dự phòng (gọi là **combo**) trong config. Bộ đếm deadline ngăn thử lại vô hạn.

### 🧰 Tools & Intelligence
Xử lý nội dung tích hợp trước khi request đến provider:

| Tool | Chức năng |
|---|---|
| **Document Intelligence** | Parse PDF, DOCX, XLSX, PPTX và tóm tắt tài liệu lớn trước khi gửi |
| **Vision Bridge** | Chặn URL ảnh gửi cho model không hỗ trợ vision, thay bằng mô tả text chi tiết |
| **Token Budget** | Trần max_tokens cứng chống vượt chi phí (1024–65535, có sàn chống truncate khi tắt) |
| **Prompt Caching** | Chiến lược cache theo provider: Anthropic explicit, Kimi key-bound, OpenAI cache-key, static-first sorting |

### 🕵️ Tích Hợp Antigravity IDE
Overlay direct-inference cho Antigravity IDE - không cần MITM proxy. Map slot model của Antigravity vào provider/combo của BSL Router.

### 🌐 Truy Cập Từ Xa
Expose BSL Router local ra mạng ngoài: Cloudflare Tunnel (URL public), Tailscale (mesh network), API key (scoped access).

### 🔐 Bảo Mật Tích Hợp
- API key lưu **mã hóa** Fernet
- Trang quản trị bảo vệ bằng mật khẩu + session hết hạn
- Bộ quét bảo mật kiểm tra cấu hình provider
- Khóa mã hóa gắn máy (config không portable)

### ⏱️ Chống Treo (Anti-Freeze)
- Stream hard deadline 10 phút
- Chain deadline ngăn timeout cascading
- Circuit breaker tự xoay provider lỗi
- Stream guard bắt chunk SSE lỗi

### 📊 Bảng Điều Khiển
Giao diện web quản trị với 8 tab: Endpoint, Providers, Combos, BSL Models, MITM, Tools, Usage, Logs, Settings.

---

## 📡 Provider Hỗ Trợ

| Provider | Giao thức | Xác thực | Model phổ biến |
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

## 🔌 API Endpoints

Trỏ app AI vào BSL Router - hoạt động như drop-in thay thế:

| Bạn đang làm gì | Endpoint | Format |
|---|---|---|
| Chat với model | `POST /v1/chat/completions` | OpenAI |
| Chat với model | `POST /v1/messages` | Anthropic |
| Liệt kê model | `GET /v1/models` | OpenAI |
| Kiểm tra sức khỏe | `GET /health` | - |

---

## 🌐 Tùy Chọn: MITM Proxy Mode

Cho những app không cho đổi API URL, BSL Router có thể chặn traffic ở tầng hệ điều hành. Watchdog tự khởi động lại proxy nếu sập.

> ⚠️ Chỉ cần cho app có API URL cố định. Phần lớn app cho phép đặt base URL riêng.

---

## 📖 Tài Liệu

| Tài liệu | Nội dung |
|---|---|
| [Kiến trúc](docs/ARCHITECTURE.md) | Cách BSL Router hoạt động bên trong |
| [Đóng góp](CONTRIBUTING.md) | Hướng dẫn setup và PR |
| [Bảo mật](SECURITY.md) | Báo cáo lỗ hổng và tính năng bảo mật |
| [Nhật ký thay đổi](CHANGELOG.md) | Lịch sử phiên bản |
| [Config mẫu](config.example.yaml) | Template cấu hình có chú thích |

---

## 📄 Giấy Phép

MIT License - xem [LICENSE](LICENSE) để biết chi tiết.

**Xây dựng bởi [Đồng Tôn](https://github.com/dongden-alt)**
