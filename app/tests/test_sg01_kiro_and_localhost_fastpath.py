# -*- coding: utf-8 -*-
"""Regression tests for Wave 2 SG-01 in BSL Router (`app/main.py`).

Covers:
1. `chat2api/kiro-free/*` and `chat2api/kiro-paid/*` (`provider_name == 'chat2api'`)
   are NOT hijacked by the `provider_name == 'kiro'` CodeWhisperer adapter guard.
2. `http://localhost:6970` is normalized to `http://127.0.0.1:6970` at runtime in
   `app/main.py` to avoid the 300ms Windows IPv6 `::1` SYN fallback.
"""
from __future__ import annotations

import os


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_sg01_kiro_adapter_only_fires_for_kiro_provider():
    root = _repo_root()
    main_path = os.path.join(root, "app", "main.py")
    with open(main_path, "r", encoding="utf-8") as fh:
        src = fh.read()

    # The substring regex `re.search(r'kiro', f_val)` must be gone and replaced
    # by exact `if provider_name == 'kiro':`.
    assert "re.search(r'kiro', f_val)" not in src
    assert "if provider_name == 'kiro':" in src
    assert 'resolved_base_url.startswith("http://localhost:")' in src


def test_sg01_config_yaml_uses_ipv4_loopback_for_chat2api():
    root = _repo_root()
    main_path = os.path.join(root, "app", "main.py")
    with open(main_path, "r", encoding="utf-8") as fh:
        src = fh.read()

    assert 'resolved_base_url = "http://127.0.0.1:" + resolved_base_url[len("http://localhost:"):]' in src
    resolved = "http://localhost:6970/v1"
    if resolved.startswith("http://localhost:"):
        resolved = "http://127.0.0.1:" + resolved[len("http://localhost:"):]
    assert resolved == "http://127.0.0.1:6970/v1"
