# Contributing to BSL Router

Thanks for your interest in contributing. This document covers development setup and guidelines.

Cảm ơn bạn đã quan tâm đóng góp. Tài liệu này hướng dẫn cách setup môi trường phát triển.

**🇬🇧 [English](#development-setup)** · **🇻🇳 [Tiếng Việt](#-tiếng-việt)**

---

## Development Setup

### Prerequisites
- Python 3.10+
- Git
- (Optional) mitmproxy for MITM features

### Getting Started

```bash
# Clone
git clone https://github.com/dongden-alt/bsl-router.git
cd bsl-router

# Create venv
python -m venv .venv
.venv\Scripts\activate       # Windows
# source .venv/bin/activate  # Linux/macOS

# Install deps
pip install -r requirements.txt

# Copy config
cp config.example.yaml config.yaml
# Edit config.yaml with your provider credentials

# Run dev server
python -m uvicorn app.main:app --host 0.0.0.0 --port 6969 --reload
```

> [!NOTE]
> `config.yaml` is gitignored because it holds live credentials. If you skip the copy step, the server will exit with setup instructions rather than a stack trace.

### Running Tests

```bash
# Full test suite
python -m pytest app/tests/ tests/ -v

# Specific test
python -m pytest app/tests/test_acl_full.py -v

# With coverage
python -m pytest app/tests/ --cov=app --cov-report=term-missing
```

### Code Style

- Follow PEP 8 (enforced via flake8)
- Use type hints where possible
- Keep functions focused and small
- Document complex logic with inline comments
- Use descriptive variable names

### Architecture Guidelines

- **No new frameworks** — use FastAPI + httpx only
- **Pydantic first** — define models in `app/models.py` before routing logic
- **Fail open** — advanced features (scout, smart trimming) must be wrapped in try/except so a failure degrades to a plain proxy request instead of breaking it
- **Atomic edits** — make targeted changes, not full file rewrites
- **No machine-specific paths** — never hardcode an absolute path like `D:\Projects\...`; derive paths from `__file__` or `$PSScriptRoot`, with an environment variable override where useful
- **Test coverage** — add tests for new features

### Pull Request Process

1. Create a feature branch (`git checkout -b feature/my-feature`)
2. Write tests for your changes
3. Ensure all tests pass (`python -m pytest`)
4. Ensure code compiles (`python -m py_compile app/main.py`)
5. Update documentation if needed
6. Submit a PR with a clear description

### Reporting Issues

Use GitHub Issues with the appropriate template:
- **Bug Report** — something is broken
- **Feature Request** — a new capability
- **Provider Support** — a new provider integration

---
---

# 🇻🇳 Tiếng Việt

---

## Thiết Lập Môi Trường

### Yêu Cầu
- Python 3.10+
- Git
- (Tùy chọn) mitmproxy cho tính năng MITM

### Bắt Đầu

```bash
# Clone
git clone https://github.com/dongden-alt/bsl-router.git
cd bsl-router

# Tạo môi trường ảo
python -m venv .venv
.venv\Scripts\activate       # Windows
# source .venv/bin/activate  # Linux/macOS

# Cài thư viện
pip install -r requirements.txt

# Copy config
cp config.example.yaml config.yaml
# Sửa config.yaml, thêm thông tin provider của bạn

# Chạy dev server
python -m uvicorn app.main:app --host 0.0.0.0 --port 6969 --reload
```

> [!NOTE]
> `config.yaml` bị gitignore vì chứa thông tin đăng nhập thật. Nếu bạn bỏ qua bước copy, server sẽ thoát kèm hướng dẫn setup chứ không phải stack trace khó hiểu.

### Chạy Test

```bash
# Toàn bộ test
python -m pytest app/tests/ tests/ -v

# Một test cụ thể
python -m pytest app/tests/test_acl_full.py -v

# Kèm đo coverage
python -m pytest app/tests/ --cov=app --cov-report=term-missing
```

### Quy Ước Code

- Tuân theo PEP 8 (kiểm tra bằng flake8)
- Dùng type hint khi có thể
- Giữ hàm nhỏ và tập trung một việc
- Ghi comment cho logic phức tạp
- Đặt tên biến rõ nghĩa

### Nguyên Tắc Kiến Trúc

- **Không thêm framework mới** — chỉ dùng FastAPI + httpx
- **Pydantic trước** — định nghĩa model trong `app/models.py` trước khi viết logic định tuyến
- **Fail open** — các tính năng nâng cao (scout, smart trimming) phải bọc trong try/except để khi lỗi thì hạ cấp thành proxy thường, không làm sập request
- **Sửa nhỏ gọn** — thay đổi có mục tiêu, không viết lại cả file
- **Không hardcode đường dẫn máy cá nhân** — không bao giờ ghi cứng đường dẫn kiểu `D:\Projects\...`; hãy suy ra từ `__file__` hoặc `$PSScriptRoot`, kèm biến môi trường để ghi đè khi cần
- **Có test** — thêm test cho tính năng mới

### Quy Trình Pull Request

1. Tạo nhánh tính năng (`git checkout -b feature/ten-tinh-nang`)
2. Viết test cho thay đổi của bạn
3. Đảm bảo mọi test pass (`python -m pytest`)
4. Đảm bảo code compile (`python -m py_compile app/main.py`)
5. Cập nhật tài liệu nếu cần
6. Gửi PR kèm mô tả rõ ràng

### Báo Lỗi

Dùng GitHub Issues với template phù hợp:
- **Bug Report** — có gì đó bị lỗi
- **Feature Request** — đề xuất tính năng mới
- **Provider Support** — tích hợp provider mới
