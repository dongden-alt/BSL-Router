"""ERE-R5: unit tests for the Gemini last-role guard (app/middleware/gemini_last_role.py).

Offline, pure-function tests: valid bodies pass through untouched, assistant/
model-trailing bodies get a "(continue)" user continuation turn.
"""
import app.middleware.gemini_last_role as glm
from app.middleware.gemini_last_role import ensure_gemini_last_role


def test_assistant_last_appends_continue_user_turn():
    contents = [
        {"role": "user", "parts": [{"text": "hi"}]},
        {"role": "assistant", "parts": [{"text": "partial answer"}]},
    ]
    out = ensure_gemini_last_role(contents)
    assert out is not contents  # new list, original untouched
    assert out[0] == {"role": "user", "parts": [{"text": "hi"}]}  # prior turns untouched
    assert len(out) == 2
    assert out[-1]["role"] == "user"
    assert out[-1]["parts"][0]["text"].startswith("(continue)")


def test_assistant_last_with_text_merges_text_into_continuation():
    contents = [
        {"role": "user", "parts": [{"text": "q"}]},
        {"role": "assistant", "parts": [{"text": "line1"}, {"text": "line2"}]},
    ]
    out = ensure_gemini_last_role(contents)
    assert len(out) == 2
    last = out[-1]
    assert last["role"] == "user"
    # non-text assistant parts dropped; both text parts merged in order
    assert last["parts"][0]["text"] == "(continue)\n\nline1line2"


def test_model_role_gemini_naming_gets_same_treatment():
    contents = [
        {"role": "user", "parts": [{"text": "q"}]},
        {"role": "model", "parts": [{"text": "thinking out loud"}]},
    ]
    out = ensure_gemini_last_role(contents)
    assert len(out) == 2
    assert out[-1]["role"] == "user"
    assert out[-1]["parts"][0]["text"] == "(continue)\n\nthinking out loud"


def test_user_last_returned_unchanged():
    contents = [
        {"role": "user", "parts": [{"text": "hi"}]},
        {"role": "model", "parts": [{"text": "hello"}]},
        {"role": "user", "parts": [{"text": "again"}]},
    ]
    out = ensure_gemini_last_role(contents)
    assert out == contents  # list-equal semantics: byte-identical pass-through
    assert out is contents  # same object returned (no copy needed)


def test_function_response_last_untouched():
    contents = [
        {"role": "user", "parts": [{"text": "q"}]},
        {"role": "user", "parts": [{"functionResponse": {"name": "f", "response": {"result": 1}}}]},
    ]
    out = ensure_gemini_last_role(contents)
    assert out is contents


def test_empty_and_single_user_lists_untouched():
    empty: list = []
    assert ensure_gemini_last_role(empty) is empty
    single = [{"role": "user", "parts": [{"text": "solo"}]}]
    assert ensure_gemini_last_role(single) is single


def test_garbage_input_returned_unchanged_no_raise():
    assert ensure_gemini_last_role(None) is None
    assert ensure_gemini_last_role("string") == "string"
    assert ensure_gemini_last_role(42) == 42
    bad_dicts = [
        [{"no_role": True}],
        [{"role": 123, "parts": "notalist"}],
        ["not-a-dict", 7],
        [{"role": "assistant"}],  # missing parts entirely
        [{"role": "assistant", "parts": [{"bogus": 1}]}],  # no text parts
    ]
    for gd in bad_dicts:
        assert ensure_gemini_last_role(gd) is not None  # never raises / never None
    # assistant with no text parts still gets a bare continuation turn
    out = ensure_gemini_last_role([{"role": "assistant", "parts": [{"bogus": 1}]}])
    assert out[-1]["role"] == "user"
    assert out[-1]["parts"][0]["text"] == "(continue)"
    # pure function contract: exception inside never escapes
    assert glm.ensure_gemini_last_role({"role": "assistant"}) == {"role": "assistant"}


def test_model_last_with_function_call_preserves_call_and_appends_ack():
    contents = [
        {"role": "user", "parts": [{"text": "check status"}]},
        {
            "role": "model",
            "parts": [
                {"functionCall": {"name": "read_file", "args": {"Path": "a.txt"}}},
            ],
        },
    ]
    out = ensure_gemini_last_role(contents)
    assert len(out) == 3
    assert out[1] == contents[1]
    assert out[2]["role"] == "user"
    assert out[2]["parts"][0]["functionResponse"]["name"] == "read_file"
    assert out[2]["parts"][1]["text"] == "(continue)"


def test_model_last_with_function_response_reroles_to_user_preserving_parts():
    """§8.1: Antigravity IDE can tag functionResponse turns with role='model'.
    ensure_gemini_last_role must re-role the trailing turn to 'user' in place
    without wiping out its functionResponse parts."""
    fr_part = {"functionResponse": {"name": "read_file", "response": {"output": "lines 1-10"}}}
    contents = [
        {"role": "user", "parts": [{"text": "go"}]},
        {"role": "model", "parts": [{"functionCall": {"name": "read_file", "args": {"Path": "a.py"}}}]},
        {"role": "model", "parts": [fr_part]},
    ]
    out = ensure_gemini_last_role(contents)
    assert len(out) == 3
    assert out[-1]["role"] == "user"
    assert out[-1]["parts"] == [fr_part]

