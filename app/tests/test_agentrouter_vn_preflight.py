"""AgentRouter VN preflight — provider-scoped only."""
import os
import sys
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.middleware.agentrouter_policy import (
    agentrouter_nfkd_transcode,
    agentrouter_should_skip_for_vietnamese,
    detect_vietnamese_content,
    format_agentrouter_vn_skip_error,
)


def test_pure_english_not_flagged():
    hit, reason = detect_vietnamese_content("Reply with exactly: OK. Summarize this file.")
    assert hit is False
    assert reason == ""


def test_diacritic_vn_flagged():
    hit, reason = detect_vietnamese_content(
        "Summarize this file:\n\nHệ thống quản trị biên tập tin tức bóng đá."
    )
    assert hit is True
    assert reason.startswith("vietnamese_diacritic:")


def test_accent_stripped_phrase_flagged():
    # Live P3 body that 400'd on agentrouter.org
    text = (
        "Summarize this file in one word:\n\n"
        "Day la file du an: He thong quan tri bien tap tin tuc bong da, "
        "tu dong hoa quy trinh duyet bai va xuat ban."
    )
    hit, reason = detect_vietnamese_content(text)
    assert hit is True
    assert "vietnamese_phrase:" in reason


def test_chinese_not_flagged_as_vn():
    hit, reason = detect_vietnamese_content("Summarize in one word:\n\n新闻编辑系统，自动化审核流程。")
    assert hit is False


def test_provider_gate_only_agentrouter():
    msgs = [{"role": "user", "content": "He thong quan tri bien tap tin tuc bong da"}]
    skip, _ = agentrouter_should_skip_for_vietnamese("agentrouter", msgs)
    assert skip is True
    skip2, reason2 = agentrouter_should_skip_for_vietnamese("pix4k", msgs)
    assert skip2 is False
    assert reason2 == ""
    skip3, _ = agentrouter_should_skip_for_vietnamese("x5m5x", msgs)
    assert skip3 is False


def test_error_payload_shape():
    body = format_agentrouter_vn_skip_error("vietnamese_phrase:he thong")
    assert body["error"]["code"] == "content-blocked-preflight"
    assert body["error"]["provider"] == "agentrouter"
    assert "Vietnamese" in body["error"]["message"]


def test_message_object_content_list():
    class Msg:
        def __init__(self, role, content):
            self.role = role
            self.content = content

    msgs = [
        Msg("user", [{"type": "text", "text": "Please review:\nquan tri bien tap tin tuc"}]),
    ]
    skip, reason = agentrouter_should_skip_for_vietnamese("agentrouter", msgs)
    assert skip is True
    assert "vietnamese_phrase:" in reason


# ---- NFKD transcode tests ----

# Precomposed VN codepoints that NFKD decomposes (U+1EA0..U+1EF9).
# These are what agentrouter.org 400s on; NFKD rewrites them to base +
# combining mark. Đ/đ (U+0110/U+0111) are NOT decomposed by NFKD and AR
# accepts them, so they are intentionally excluded from this set.
_VN_PRECOMPOSED = set(chr(c) for c in range(0x1EA0, 0x1EFA))


def _has_precomposed_vn(s: str) -> bool:
    return any(ch in _VN_PRECOMPOSED for ch in s)


def test_nfkd_removes_precomposed_vn_codepoints():
    original = "Hệ thống quản trị biên tập tin tức bóng đá."
    assert _has_precomposed_vn(original)

    msgs = [{"role": "user", "content": original}]
    changed, summary = agentrouter_nfkd_transcode("agentrouter", msgs)

    assert changed is True
    assert summary.startswith("nfkd:")
    rewritten = msgs[0]["content"]
    assert not _has_precomposed_vn(rewritten)
    # NFKD round-trips back to the original NFC via NFKC/NFC composition.
    assert unicodedata.normalize("NFC", rewritten) == unicodedata.normalize("NFC", original)


def test_nfkd_ascii_untouched_returns_false():
    msgs = [{"role": "user", "content": "Reply with exactly: OK. Summarize this file."}]
    changed, summary = agentrouter_nfkd_transcode("agentrouter", msgs)
    assert changed is False
    assert summary == ""
    assert msgs[0]["content"] == "Reply with exactly: OK. Summarize this file."


def test_nfkd_provider_gate_not_agentrouter():
    original = "Hệ thống quản trị biên tập tin tức bóng đá."
    msgs = [{"role": "user", "content": original}]
    changed, summary = agentrouter_nfkd_transcode("pix4k", msgs)
    assert changed is False
    assert summary == ""
    # Untouched — precomposed VN still present.
    assert _has_precomposed_vn(msgs[0]["content"])


def test_nfkd_content_list_messages():
    msgs = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Hệ thống quản trị tin tức."},
                {"type": "text", "text": "Bóng đá."},
            ],
        }
    ]
    changed, summary = agentrouter_nfkd_transcode("agentrouter", msgs)
    assert changed is True
    blocks = msgs[0]["content"]
    for block in blocks:
        assert not _has_precomposed_vn(block["text"])


def test_nfkd_object_with_content_attr():
    class Msg:
        def __init__(self, content):
            self.content = content

    original = "Hệ thống quản trị."
    msg = Msg(original)
    changed, summary = agentrouter_nfkd_transcode("agentrouter", [msg])
    assert changed is True
    assert not _has_precomposed_vn(msg.content)
