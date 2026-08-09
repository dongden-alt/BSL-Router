# Security Policy

**🇬🇧 [English](#reporting-a-vulnerability)** · **🇻🇳 [Tiếng Việt](#-tiếng-việt)**

---

## Reporting a Vulnerability

If you discover a security vulnerability in BSL Router, please report it responsibly:

1. **DO NOT** open a public GitHub issue
2. Email: tonluongdong@gmail.com
3. Include:
   - Description of the vulnerability
   - Steps to reproduce
   - Potential impact
   - Suggested fix (if any)

## Response Timeline

| Stage | Target |
|---|---|
| Acknowledgment | Within 48 hours |
| Initial assessment | Within 7 days |
| Fix release | Within 30 days (severity-dependent) |

---

## Security Features

### Encryption at Rest
API keys and OAuth tokens are encrypted with [Fernet](https://cryptography.io/en/latest/fernet/) symmetric encryption before being written to `config.yaml`. Plaintext values found in an existing config are migrated automatically on startup, with a backup saved alongside.

> [!IMPORTANT]
> The encryption key is **machine-bound**. Copying `config.yaml` to a different computer will not transfer working credentials — you must re-enter your API keys on the new machine. This is intentional: a stolen config file is useless without the host key.

### Admin Authentication
Optional password-protected admin sessions with automatic expiry. Disabled by default for local-only use; enable it before exposing the router beyond localhost.

### Security Scanner
An on-demand audit of your provider configuration. Run it from the admin UI or call `GET /api/scan-keys`. To audit a config before saving it, `POST /api/scan-keys` with the candidate config as the body.

It performs eight checks:

| Check | What it catches | Severity |
|---|---|---|
| Exfil URL | `base_url` pointing at webhook, paste, or tunnel services | block |
| Key injection | Shell, SQL, or HTML injection patterns inside an API key | block |
| Credential harvesting | Credentials passed as URL query parameters | block |
| Local network exfil | A cloud provider format pointed at a private or loopback IP | block |
| Insecure transport | Plain `http://`, which transmits keys in cleartext | block |
| URL spoofing | Lookalike domains, e.g. `api.openai.com.evil.com`, or punycode | warn |
| Token tampering | OAuth tokens that don't match the provider's expected format | warn |
| Duplicate keys | The same API key reused across multiple providers | warn |

A scan passes when there are no **block**-level findings. Warnings are surfaced but do not fail the scan.

**Why on demand instead of always on?** The scanner reads decrypted credentials to inspect them. Keeping it explicit means secrets are only touched when you ask for an audit, there is no per-request latency cost, and no background job can silently rewrite your config. Configuration changes are the only thing that can introduce these risks, so a check at change time (or before saving) is sufficient.

### Additional Protections
- Atomic config writes, so an interrupted save cannot corrupt `config.yaml`
- Circuit breaker that isolates repeatedly failing provider connections
- Optional request/response logging for audit trails

---

## Deployment Best Practices

1. **Keep credentials encrypted** — never commit a `config.yaml`; it is gitignored for this reason
2. **Enable admin auth** — set a strong admin password before any non-local deployment
3. **Bind to localhost** — use `127.0.0.1` unless you deliberately need external access
4. **Prefer tunnels** — Cloudflare Tunnel or Tailscale instead of opening a port
5. **Run the scanner** after adding or editing providers
6. **Keep dependencies updated** — `pip install -r requirements.txt --upgrade`
7. **Enable observability logging** for production audit trails

---
---

# 🇻🇳 Tiếng Việt

---

## Báo Cáo Lỗ Hổng

Nếu bạn phát hiện lỗ hổng bảo mật trong BSL Router, vui lòng báo cáo có trách nhiệm:

1. **KHÔNG** mở GitHub issue công khai
2. Email: tonluongdong@gmail.com
3. Kèm theo:
   - Mô tả lỗ hổng
   - Các bước tái hiện
   - Tác động tiềm tàng
   - Đề xuất cách sửa (nếu có)

## Thời Gian Phản Hồi

| Giai đoạn | Mục tiêu |
|---|---|
| Xác nhận đã nhận | Trong 48 giờ |
| Đánh giá ban đầu | Trong 7 ngày |
| Phát hành bản sửa | Trong 30 ngày (tùy mức nghiêm trọng) |

---

## Tính Năng Bảo Mật

### Mã Hóa Khi Lưu Trữ
API key và OAuth token được mã hóa bằng [Fernet](https://cryptography.io/en/latest/fernet/) trước khi ghi vào `config.yaml`. Nếu phát hiện giá trị dạng thô trong config cũ, hệ thống tự động mã hóa khi khởi động và lưu một bản backup kèm theo.

> [!IMPORTANT]
> Khóa mã hóa **gắn với máy**. Copy `config.yaml` sang máy khác sẽ không mang theo thông tin đăng nhập dùng được — bạn phải nhập lại API key ở máy mới. Đây là thiết kế có chủ đích: file config bị đánh cắp sẽ vô dụng nếu không có khóa của máy gốc.

### Xác Thực Quản Trị
Session quản trị bảo vệ bằng mật khẩu, tự hết hạn (tùy chọn). Mặc định tắt để dùng nội bộ; hãy bật trước khi mở router ra ngoài localhost.

### Bộ Quét Bảo Mật
Kiểm tra cấu hình provider theo yêu cầu. Chạy từ trang quản trị hoặc gọi `GET /api/scan-keys`. Muốn kiểm tra một cấu hình trước khi lưu, dùng `POST /api/scan-keys` với config đó trong body.

Tám kiểm tra được thực hiện:

| Kiểm tra | Phát hiện gì | Mức độ |
|---|---|---|
| Exfil URL | `base_url` trỏ tới dịch vụ webhook, paste, tunnel | block |
| Key injection | Mẫu tấn công shell, SQL, HTML trong API key | block |
| Credential harvesting | Thông tin đăng nhập truyền qua query parameter | block |
| Local network exfil | Provider định dạng cloud nhưng trỏ vào IP nội bộ/loopback | block |
| Insecure transport | Dùng `http://` khiến key truyền dạng thô | block |
| URL spoofing | Domain giả mạo, ví dụ `api.openai.com.evil.com`, hoặc punycode | warn |
| Token tampering | OAuth token không đúng định dạng của provider | warn |
| Duplicate keys | Cùng một API key dùng lại ở nhiều provider | warn |

Kết quả đạt khi không có phát hiện mức **block**. Cảnh báo vẫn được hiển thị nhưng không làm scan thất bại.

**Vì sao chạy theo yêu cầu mà không phải luôn bật?** Bộ quét cần đọc thông tin đăng nhập đã giải mã để kiểm tra. Giữ nó ở dạng chủ động nghĩa là: secret chỉ bị chạm tới khi bạn yêu cầu, không tốn thêm độ trễ cho mỗi request, và không có tiến trình ngầm nào có thể tự sửa config của bạn. Chỉ khi thay đổi cấu hình mới sinh ra các rủi ro này, nên kiểm tra tại thời điểm thay đổi (hoặc trước khi lưu) là đủ.

### Bảo Vệ Bổ Sung
- Ghi config kiểu atomic, nên lưu bị ngắt giữa dòng cũng không làm hỏng `config.yaml`
- Circuit breaker cô lập các kết nối provider lỗi liên tục
- Ghi log request/response (tùy chọn) để phục vụ truy vết

---

## Thực Hành Tốt Khi Triển Khai

1. **Luôn mã hóa thông tin đăng nhập** — không bao giờ commit `config.yaml`; file này đã bị gitignore vì lý do đó
2. **Bật xác thực quản trị** — đặt mật khẩu mạnh trước khi triển khai ra ngoài máy cá nhân
3. **Chỉ bind vào localhost** — dùng `127.0.0.1` trừ khi bạn thực sự cần truy cập từ ngoài
4. **Ưu tiên tunnel** — dùng Cloudflare Tunnel hoặc Tailscale thay vì mở cổng trực tiếp
5. **Chạy bộ quét** sau khi thêm hoặc sửa provider
6. **Cập nhật thư viện** — `pip install -r requirements.txt --upgrade`
7. **Bật ghi log** cho môi trường production để có dấu vết kiểm toán
