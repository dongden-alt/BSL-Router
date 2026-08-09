"""Regression tests for Gemini tool-name validation."""
from app.compat.adapters.gemini import gemini_request_to_openai


def test_skips_nameless_declaration_and_call():
    result = gemini_request_to_openai(
        {
            "tools": [{"functionDeclarations": [{"description": "invalid"}]}],
            "contents": [{"role": "model", "parts": [{"functionCall": {"args": {"q": "x"}}}]}],
        },
        "test-model",
    )
    assert "tools" not in result
    assert all("tool_calls" not in message for message in result["messages"])


def test_preserves_trimmed_valid_tool_names():
    result = gemini_request_to_openai(
        {
            "tools": [{"functionDeclarations": [{"name": "  search  ", "parameters": {}}]}],
            "contents": [{"role": "model", "parts": [{"functionCall": {"name": "  search  ", "args": {"q": "x"}}}]}],
        },
        "test-model",
    )
    assert result["tools"][0]["function"]["name"] == "search"
    assert result["messages"][0]["tool_calls"][0]["function"]["name"] == "search"
