"""Faithful Execution Layer (FEL) — directive builder tests.

Covers family detection, per-family directive sets (bilingual CN policy,
GPT scoping, claude/gemini autonomous line, research-context everywhere),
the forbidden-vocabulary guard across every family × lang_policy,
system-prompt tail merging, and the eligibility gate.
"""
from __future__ import annotations

import copy

import pytest

from app.middleware.faithful_execution import (
    FORBIDDEN_PATTERNS,
    build_directives,
    detect_family,
    fel_eligible,
    merge_directives,
)

MARKER = "# Execution contract (router-injected)"

_FAMILY_MODELS = [
    ("cn", "glm-5.2"),
    ("gpt", "gpt-5.5"),
    ("claude", "claude-opus-5"),
    ("gemini", "gemini-3-pro"),
    ("other", "llama-4"),
]


def _has_cjk(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in text)


# --- detect_family ---------------------------------------------------------


@pytest.mark.parametrize(
    "model_id,expected",
    [
        ("glm-5.2", "cn"),
        ("qwen-max", "cn"),
        ("deepseek-v4", "cn"),
        ("kimi-k3", "cn"),
        ("MiniMax-M3", "cn"),
        ("gpt-5.5", "gpt"),
        ("codex-x", "gpt"),
        ("chatgpt-4o", "gpt"),
        ("o3-mini", "gpt"),
        ("o4-mini", "gpt"),
        ("claude-opus-5", "claude"),
        ("sonnet-9", "claude"),
        ("haiku-5", "claude"),
        ("gemini-3-pro", "gemini"),
        ("vertex-ai", "gemini"),
        ("llama-4", "other"),
        ("mystery-model", "other"),
    ],
)
def test_detect_family_by_model(model_id: str, expected: str) -> None:
    assert detect_family(model_id, "") == expected


def test_detect_family_provider_fallback() -> None:
    assert detect_family("mystery", "anthropic") == "claude"
    assert detect_family("mystery", "OpenAI") == "gpt"
    assert detect_family("mystery", "zai") == "cn"
    assert detect_family("mystery", "google-vertex") == "gemini"


def test_detect_family_model_beats_provider() -> None:
    assert detect_family("glm-5.2-anthropic", "anthropic") == "cn"


# --- build_directives ------------------------------------------------------


@pytest.mark.parametrize("family,model_id", _FAMILY_MODELS)
def test_build_directives_non_empty(family: str, model_id: str) -> None:
    directives = build_directives(model_id, "")
    assert isinstance(directives, list)
    assert len(directives) > 0
    assert all(isinstance(d, str) and d.strip() for d in directives)


def test_cn_auto_is_bilingual() -> None:
    directives = build_directives("glm-5.2", "", "auto")
    joined = "\n".join(directives)
    assert _has_cjk(joined)
    assert any(d.isascii() for d in directives)


def test_cn_en_has_no_cjk() -> None:
    joined = "\n".join(build_directives("glm-5.2", "", "en"))
    assert not _has_cjk(joined)


def test_cn_zh_is_chinese_only() -> None:
    directives = build_directives("glm-5.2", "", "zh")
    assert directives
    assert _has_cjk("\n".join(directives))


def test_gpt_scoping_present() -> None:
    assert "professional developer" in "\n".join(build_directives("gpt-5.5", ""))


@pytest.mark.parametrize("model_id", ["claude-opus-5", "gemini-3-pro"])
def test_claude_gemini_autonomous_line(model_id: str) -> None:
    assert "autonomous coding agent" in "\n".join(build_directives(model_id, ""))


@pytest.mark.parametrize("family,model_id", _FAMILY_MODELS)
def test_research_context_every_family(family: str, model_id: str) -> None:
    joined = "\n".join(build_directives(model_id, "")).lower()
    assert any(
        word in joined
        for word in ("research", "operator", "infrastructure")
    ), f"family {family!r} missing research-context scoping"


# --- forbidden vocabulary --------------------------------------------------


@pytest.mark.parametrize("family,model_id", _FAMILY_MODELS)
@pytest.mark.parametrize("lang", ["auto", "en", "zh"])
def test_no_forbidden_vocabulary(family: str, model_id: str, lang: str) -> None:
    joined = "\n".join(build_directives(model_id, "", lang))
    for pattern in FORBIDDEN_PATTERNS:
        assert pattern.search(joined) is None, (
            f"family={family!r} lang={lang!r} matched forbidden "
            f"pattern {pattern.pattern!r}"
        )


# --- merge_directives ------------------------------------------------------


def test_merge_str_system() -> None:
    system = "You are a helpful assistant."
    out = merge_directives(system, ["finish the work"])
    assert isinstance(out, str)
    assert system in out
    assert MARKER in out
    assert "finish the work" in out


def test_merge_list_system_anthropic_style() -> None:
    blocks = [
        {"type": "text", "text": "sys a"},
        {"type": "text", "text": "sys b"},
    ]
    snapshot = copy.deepcopy(blocks)
    out = merge_directives(blocks, ["d1"])
    assert isinstance(out, list)
    assert len(out) == len(blocks) + 1
    assert out[-1]["type"] == "text"
    assert MARKER in out[-1]["text"]
    assert "d1" in out[-1]["text"]
    assert blocks == snapshot


def test_merge_list_system_gemini_style() -> None:
    blocks = [{"text": "sys"}]
    out = merge_directives(blocks, ["d1"])
    assert isinstance(out, list)
    assert len(out) == 2
    assert MARKER in out[-1]["text"]


def test_merge_none_system() -> None:
    out = merge_directives(None, ["d1"])
    assert MARKER in out
    assert "d1" in out


def test_merge_never_mutates_input() -> None:
    blocks = [{"type": "text", "text": "original"}]
    snapshot = copy.deepcopy(blocks)
    merge_directives(blocks, ["x"])
    assert blocks == snapshot

    system = "original"
    merge_directives(system, ["x"])
    assert system == "original"


# --- fel_eligible ----------------------------------------------------------


def test_fel_eligible_disabled() -> None:
    assert fel_eligible({"tools": {"fel_enabled": False}}, "zai", "glm-5.2") == (
        False,
        "disabled",
    )
    assert fel_eligible({}, "zai", "glm-5.2") == (False, "disabled")
    assert fel_eligible({}, "zai", "glm-5.2", None) == (False, "disabled")


def test_fel_eligible_enabled_default() -> None:
    result = fel_eligible({"tools": {"fel_enabled": True}}, "zai", "glm-5.2")
    assert result == (True, "family:cn")


@pytest.mark.parametrize(
    "headers",
    [
        {"X-BSL-FEL": "off"},
        {"x-bsl-fel": "off"},
        {"X-Bsl-Fel": "OFF"},
    ],
)
def test_fel_eligible_header_off(headers: dict) -> None:
    result = fel_eligible({"tools": {"fel_enabled": True}}, "zai", "glm-5.2", headers)
    assert result == (False, "header-off")


def test_fel_eligible_profile_off() -> None:
    config = {"tools": {"fel_enabled": True, "fel_profiles": {"zai": "off"}}}
    assert fel_eligible(config, "zai", "glm-5.2") == (False, "profile-off")
    # other providers unaffected by the profile
    ok, reason = fel_eligible(config, "other-prov", "claude-opus-5")
    assert ok is True
    assert reason == "family:claude"


def test_fel_eligible_never_raises_on_malformed() -> None:
    cases = [
        {},
        {"tools": None},
        {"tools": {"fel_enabled": "yes", "fel_profiles": None}},
        None,
    ]
    for config in cases:
        result = fel_eligible(config, "zai", "glm-5.2", None)
        assert isinstance(result, tuple) and len(result) == 2
        assert isinstance(result[0], bool)
        assert isinstance(result[1], str)
