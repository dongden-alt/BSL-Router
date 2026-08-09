# Changelog

All notable changes to BSL Router are documented in this file.

Mọi thay đổi đáng chú ý của BSL Router được ghi lại trong file này.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

**🇬🇧 [English](#100---2026-08-09)** · **🇻🇳 [Tiếng Việt](#-tiếng-việt)**

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

---
---

# 🇻🇳 Tiếng Việt

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
