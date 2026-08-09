"""Tests for request_intent — the current-turn isolation + scaffolding stripper.

These lock in the behaviors that fix the Chatbot misrouting: the classifier
must see the user's CURRENT request, not the injected envelope.
"""

from app.models import ChatCompletionRequest, Message
from app.middleware.request_intent import extract_current_intent


def _req(messages):
    return ChatCompletionRequest(model="bsl-chat", messages=messages)


# ── Turn isolation ──────────────────────────────────────────────────────────

def test_last_user_turn_is_isolated_from_history():
    """A trivial follow-up must not inherit prior technical turns."""
    req = _req([
        Message(role="user", content="Debug this FastAPI router and refactor the middleware."),
        Message(role="assistant", content="Here is the refactored code..."),
        Message(role="user", content="thanks"),
    ])
    intent = extract_current_intent(req)
    assert intent.text == "thanks"


def test_system_prompt_is_ignored():
    req = _req([
        Message(role="system", content="You are a coding assistant. Debug Python. Refactor classes."),
        Message(role="user", content="hello"),
    ])
    assert extract_current_intent(req).text == "hello"


# ── Query markers ─────────────────────────────────────────────────────────────

def test_query_marker_keeps_trailing_segment():
    req = _req([
        Message(role="user", content="[CONTEXT] lots of technical scaffolding here. User question: hello"),
    ])
    intent = extract_current_intent(req)
    assert intent.text == "hello"
    assert intent.had_marker is True


def test_vietnamese_query_marker():
    req = _req([
        Message(role="user", content="[NGỮ CẢNH] phân tích kỹ thuật. Câu hỏi: xin chào"),
    ])
    assert extract_current_intent(req).text == "xin chào"


def test_last_marker_wins():
    req = _req([
        Message(role="user", content="User: analyze this deeply. Assistant context. User question: ok"),
    ])
    assert extract_current_intent(req).text == "ok"


# ── Scaffolding stripping ─────────────────────────────────────────────────────

def test_bracketed_directive_blocks_stripped():
    req = _req([
        Message(role="user", content="[DEEP PERSONA: strategist] analyze deeply and compare. hello there"),
    ])
    intent = extract_current_intent(req)
    assert "DEEP PERSONA" not in intent.text
    assert intent.had_scaffolding is True


def test_angle_tag_blocks_stripped():
    req = _req([
        Message(role="user", content="<context>huge technical dump: debug refactor analyze</context> hi"),
    ])
    intent = extract_current_intent(req)
    assert "debug refactor analyze" not in intent.text
    assert "hi" in intent.text


def test_fenced_blocks_stripped():
    req = _req([
        Message(role="user", content="```\ndef debug(): refactor()\n``` what is this?"),
    ])
    intent = extract_current_intent(req)
    assert "refactor()" not in intent.text
    assert "what is this?" in intent.text


# ── Chatbot injection-pair (the REAL pattern) ─────────────────────────────────

def test_chatbot_injection_pair_real_query_survives():
    """Chatbot injects [SYSTEM INSTRUCTIONS] as its OWN user message + assistant
    ack, then the real query is a separate later user message. Isolation must
    pick the real query."""
    req = _req([
        Message(role="user", content=(
            "[SYSTEM INSTRUCTIONS — FOLLOW PRECISELY]\n\n"
            "You are a strategic engine. Debug code. Refactor. Analyze deeply.\n\n"
            "Acknowledge and internalize these instructions completely."
        )),
        Message(role="assistant", content="Understood. I have internalized all system instructions."),
        Message(role="user", content="hello"),
    ])
    assert extract_current_intent(req).text == "hello"


# ── Fail-open guarantees ──────────────────────────────────────────────────────

def test_never_empty_for_nonempty_input():
    """If stripping over-consumes, we must fall back, never return empty."""
    req = _req([
        Message(role="user", content="[ONLY A DIRECTIVE BLOCK WITH NO REAL QUERY]"),
    ])
    intent = extract_current_intent(req)
    assert intent.text != ""  # fell back to pre-strip / raw


def test_real_technical_query_preserved():
    req = _req([
        Message(role="user", content="Debug this Python FastAPI router and analyze the traceback."),
    ])
    text = extract_current_intent(req).text
    assert "Debug" in text and "traceback" in text


def test_empty_messages_safe():
    assert extract_current_intent(_req([])).text == ""
