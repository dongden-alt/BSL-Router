"""AgentRouter VN preflight — provider-scoped only."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.middleware.agentrouter_policy import (
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
