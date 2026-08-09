# BSL Router

> **One proxy to rule all your AI models.** Use any AI client with any AI provider — BSL Router handles the translation, failover, and routing automatically.
>
> **Một proxy quản lý tất cả AI model.** Dùng bất kỳ AI client nào với bất kỳ AI provider nào — BSL Router tự động dịch, chuyển hướng, và xử lý lỗi.

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

## ✅ The Solution

BSL Router sits between your AI client and your providers. It speaks every protocol, so your client doesn't have to.

```
┌──────────────┐         ┌──────────────┐         ┌──────────────┐
│  Your AI     │         │              │         │   OpenAI     │
│  Client      │────────▶│  BSL Router  │────────▶│   Anthropic  │
│  (any app)   │◀────────│              │◀────────│   Google     │
└──────────────┘         └──────────────┘         │   DeepSeek   │
                          Translates,              │   + 8 more   │
                          routes, and               └──────────────┘
                          recovers
```

**Think of it as a universal translator + traffic controller for AI APIs.**

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

Open `http://localhost:6969` for the admin dashboard.

> **Windows users**: You can also use `.\scripts\bslrouter.ps1 -start` for managed startup.

---

## 🔑 Key Features

### 🔀 Smart Routing
Send a request → BSL Router picks the best provider automatically. If that provider fails, it tries the next one in your fallback chain. Your client never sees the failure.

### 🔄 Protocol Translation
Your client speaks OpenAI format? Your provider uses Anthropic format? No problem. BSL Router translates on the fly:

| From | To | Works? |
|---|---|---|
| OpenAI | Anthropic | ✅ |
| OpenAI | Gemini | ✅ |
| Anthropic | OpenAI | ✅ |
| Anthropic | Gemini | ✅ |
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

If the first provider returns an error, BSL Router automatically tries the next one. Your client gets a response — not an error.

### 🔐 Built-in Security
- API keys stored **encrypted** on disk (Fernet encryption)
- Admin dashboard protected by password + session expiry
- Automatic plaintext key detection warns you if you forget to encrypt

### 📊 Monitoring Dashboard
Web-based admin UI showing:
- Live request/response logs
- Provider health and error rates
- Usage statistics per model
- One-click configuration changes

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

> **Adding a new provider?** Just add a section to `config.yaml`. No code changes needed.

---

## 🔌 API Endpoints

Point your AI client at BSL Router and it works like a drop-in replacement:

| What you're doing | Endpoint | Format |
|---|---|---|
| Chat with a model | `POST /v1/chat/completions` | OpenAI |
| Chat with a model | `POST /v1/messages` | Anthropic |
| List available models | `GET /v1/models` | OpenAI |
| Health check | `GET /health` | — |

**Admin endpoints** (for the dashboard):

| What you're doing | Endpoint |
|---|---|
| View/edit config | `GET/POST /api/config` |
| Manage MITM proxy | `GET/POST /api/mitm/*` |
| View usage stats | `GET /api/observability/usage` |
| Manage tunnels | `GET/POST /api/tunnel/*` |

---

## 🌐 Optional: MITM Proxy Mode

For apps that don't let you change the API URL (like some VS Code extensions), BSL Router can intercept traffic at the OS level:

1. Modifies your hosts file to redirect `api.openai.com` → `localhost`
2. Runs a transparent proxy on port 443
3. Routes intercepted traffic through BSL Router
4. Your app thinks it's talking to OpenAI — but BSL Router handles everything

> ⚠️ This is optional and only needed for apps with hardcoded API URLs.

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

MIT License — see [LICENSE](LICENSE) for details.

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

## ✅ Giải Pháp

BSL Router nằm giữa app AI của bạn và các provider. Nó nói được mọi giao thức, nên app của bạn không cần phải biết.

```
┌──────────────┐         ┌──────────────┐         ┌──────────────┐
│  App AI      │         │              │         │   OpenAI     │
│  của bạn     │────────▶│  BSL Router  │────────▶│   Anthropic  │
│  (bất kỳ)    │◀────────│              │◀────────│   Google     │
└──────────────┘         └──────────────┘         │   DeepSeek   │
                          Dịch, định               │   + 8 khác   │
                          tuyến, và tự             └──────────────┘
                          phục hồi
```

**Hãy nghĩ nó như một phiên dịch viên đa năng + bộ điều phối giao thông cho AI API.**

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
# Sửa config.yaml — thêm API key của các provider
```

### 3. Chạy

```bash
python -m uvicorn app.main:app --host 0.0.0.0 --port 6969
```

Mở `http://localhost:6969` để vào trang quản trị.

> **Người dùng Windows**: Có thể dùng `.\scripts\bslrouter.ps1 -start` để khởi động có quản lý.

---

## 🔑 Tính Năng Chính

### 🔀 Định Tuyến Thông Minh
Gửi request → BSL Router tự chọn provider tốt nhất. Nếu provider đó lỗi, nó tự thử provider tiếp theo. Client của bạn không bao giờ thấy lỗi.

### 🔄 Dịch Giao Thức
Client nói format OpenAI? Provider dùng format Anthropic? Không vấn đề. BSL Router dịch ngay lập tức:

| Từ | Sang | Hoạt động? |
|---|---|---|
| OpenAI | Anthropic | ✅ |
| OpenAI | Gemini | ✅ |
| Anthropic | OpenAI | ✅ |
| Anthropic | Gemini | ✅ |
| Bất kỳ | Bất kỳ | ✅ |

### 🛡️ Tự Động Chuyển Hướng Khi Lỗi
Định nghĩa chuỗi dự phòng (gọi là **combo**) trong config:

```yaml
combos:
  - alias: chuoi-thong-minh
    chain:
      - provider: openai       # Thử OpenAI trước
        model: gpt-4o
      - provider: anthropic    # OpenAI lỗi → thử Anthropic
        model: claude-sonnet-4
      - provider: deepseek     # Phương án cuối
        model: deepseek-v4
    strategy: fallback
```

Nếu provider đầu trả lỗi, BSL Router tự động thử provider kế tiếp. Client nhận response — không phải lỗi.

### 🔐 Bảo Mật Tích Hợp
- API key lưu **mã hóa** trên ổ đĩa (Fernet encryption)
- Trang quản trị bảo vệ bằng mật khẩu + session hết hạn tự động
- Tự phát hiện key chưa mã hóa và cảnh báo

### 📊 Bảng Điều Khiển
Giao diện web quản trị hiển thị:
- Log request/response thời gian thực
- Sức khỏe provider và tỷ lệ lỗi
- Thống kê sử dụng theo model
- Thay đổi cấu hình một click

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

> **Thêm provider mới?** Chỉ cần thêm mục vào `config.yaml`. Không cần sửa code.

---

## 🔌 API Endpoints

Trỏ app AI vào BSL Router — hoạt động như drop-in thay thế:

| Bạn đang làm gì | Endpoint | Format |
|---|---|---|
| Chat với model | `POST /v1/chat/completions` | OpenAI |
| Chat với model | `POST /v1/messages` | Anthropic |
| Liệt kê model | `GET /v1/models` | OpenAI |
| Kiểm tra sức khỏe | `GET /health` | — |

---

## 🌐 Tùy Chọn: MITM Proxy Mode

Cho những app không cho đổi API URL (như một số extension VS Code), BSL Router có thể chặn traffic ở tầng hệ điều hành:

1. Sửa file hosts để chuyển hướng `api.openai.com` → `localhost`
2. Chạy proxy trong suốt trên cổng 443
3. Định tuyến traffic qua BSL Router
4. App nghĩ nó đang nói chuyện với OpenAI — nhưng BSL Router xử lý tất cả

> ⚠️ Đây là tùy chọn, chỉ cần cho app có hardcoded API URL.

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

MIT License — xem [LICENSE](LICENSE) để biết chi tiết.

**Xây dựng bởi [Đồng Tôn](https://github.com/dongden-alt)**
