# Changelog

All notable changes to BSL Router are documented in this file.

Mọi thay đổi đáng chú ý của BSL Router được ghi lại trong file này.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

**🇬🇧 [English](#100---2026-08-09)** · **🇻🇳 [Tiếng Việt](#-tiếng-việt)**

---

## [1.0.1] - 2026-08-14

### Changed
- **STREAM-GUARD first-bytes diagnostics** — when the guard refuses a
  post-emission fallback (e.g. the `GPT-5.6-SOL` midstream transport death
  that returned 502 after 11204B with `out: 0`), the refusal log now includes
  a capped 256B sample of the FIRST bytes that were emitted. This answers
  whether the veto was justified (reasoning scaffolding vs user-visible
  content) without weakening the no-second-stream invariant. Veto behavior
  itself is unchanged — this is forensics, not a policy change.

### Fixed
- **GPT-5.6-SOL midstream 502 (reasoning-only streams)** — when a vision/reasoning
  model (DeepSeek V4, MiniMax M3 via `qwencoder/gpt-5.6-sol`) produced ONLY
  `thought:true` frames before the leaf died mid-transport, the emission gate
  treated reasoning-pane text as committed content, vetoed combo fallback, and
  the IDE froze on a dead stream. The Gemini egress now holds pre-content
  thought frames in a capped buffer (256 KiB) without marking emission, so a
  transport death/stall can still fail over to the next combo entry. The first
  visible body-content frame flushes the buffer in order and commits, exactly
  as before. Usage-only/finish-only scaffolding passes through uncommitted.
  New classifier `gemini_frame_is_thought_only` in `app/compat/adapters/gemini.py`;
  regression suite `app/tests/test_thought_buffer_prender.py` (19 tests).
- **Vision Scout 502 timeout loop** — the vision polyfill could exhaust its
  total wall-clock budget before every fallback candidate got a turn
  (`Vision unavailable: vision description exceeded the 120.0s budget`).
  Root cause: a 60s per-attempt timeout meant only 2 of 4 candidates fit in
  the 120s budget. Lowered the per-attempt timeout to 15s and the total
  budget to 65s so all 4 candidates get a full attempt (4 × 15s = 60s plus
  margin) while still bounding how long the IDE can stall.
- **Self-signed certificate upstreams** — added per-provider `ssl_verify:
  false` support so connections to upstreams with self-signed certificates
  no longer fail with `SSL: CERTIFICATE_VERIFY_FAILED`.
- **Auth pipeline latency** — decrypted config is now cached in
  `config_state` instead of being re-decrypted on every request.
- **Circuit breaker accuracy** — billing and authentication errors are now
  marked distinctly so the breaker does not misclassify them as provider
  outages.

### Security
- Purged sensitive config snapshots from Git history and gitignored
  `.brain/` state directories and config backups to prevent key leakage.
- Hardened `export-public.ps1` against exporting `config.backup.*` files
  and root-level scratch scripts.

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
