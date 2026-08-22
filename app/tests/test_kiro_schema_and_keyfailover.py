"""Regression tests for the evidence-derived Kiro schema + key-failover resolver.

Kiro schema assertions mirror a real status=success request captured from
9Router's request log; see app/kiro_adapter.openai_to_kiro docstring.
"""
from app import kiro_adapter
from app.utils.model_resolver import resolve_active_connection


# -- Kiro request schema ------------------------------------------------------

def _body(**over):
    payload = {
        "model": "claude-sonnet-4.5",
        "messages": [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "ack"},
            {"role": "user", "content": "second"},
        ],
    }
    payload.update(over)
    return kiro_adapter.openai_to_kiro(payload, profile_arn="arn:aws:codewhisperer:x")


def test_kiro_top_level_keys_match_working_capture():
    body = _body()
    assert set(body.keys()) == {"conversationState", "model", "profileArn"}


def test_kiro_no_top_level_modelid_stream_or_maxresponsetokens():
    """These three keys caused REQUEST_BODY_INVALID from CodeWhisperer."""
    body = _body(stream=True, max_tokens=1024)
    assert "modelId" not in body
    assert "stream" not in body
    assert "maxResponseTokens" not in body


def test_kiro_inference_config_is_top_level_not_in_conversation_state():
    body = _body(max_tokens=32000, temperature=0)
    assert body["inferenceConfig"] == {"maxTokens": 32000, "temperature": 0}
    assert "inferenceConfig" not in body["conversationState"]


def test_kiro_inference_config_omitted_when_no_params():
    body = _body()
    assert "inferenceConfig" not in body


def test_kiro_current_message_carries_modelid_and_origin():
    uim = _body()["conversationState"]["currentMessage"]["userInputMessage"]
    assert uim["modelId"] == "claude-sonnet-4.5"
    assert uim["origin"] == "AI_EDITOR"
    assert uim["content"] == "second"
    assert uim["userInputMessageContext"]["tools"] == []


def test_kiro_conversation_state_has_trigger_and_conversation_id():
    cs = _body()["conversationState"]
    assert cs["chatTriggerType"] == "MANUAL"
    assert len(cs["conversationId"]) == 36  # uuid4


def test_kiro_history_user_entries_carry_modelid():
    hist = _body()["conversationState"]["history"]
    assert hist[0]["userInputMessage"]["modelId"] == "claude-sonnet-4.5"
    assert hist[1]["assistantResponseMessage"]["content"] == "ack"


def test_kiro_thinking_params_land_in_inference_config():
    body = _body(thinking={"type": "adaptive"}, output_config={"effort": "max"},
                 reasoning_effort="high")
    ic = body["inferenceConfig"]
    assert ic["thinking"] == {"type": "adaptive"}
    assert ic["output_config"] == {"effort": "max"}
    assert ic["reasoning_effort"] == "high"


# -- Key failover (exclude_indexes) -------------------------------------------

def _cfg(n=3, indexes=None, round_robin=False, enabled=None):
    conns = []
    for i in range(n):
        on = True if enabled is None else enabled[i]
        conns.append({"name": f"k{i}", "api_key": f"key{i}", "enabled": on})
    model = {"id": "m1", "name": "m1"}
    if indexes is not None:
        model["connection_indexes"] = indexes
    return {
        "providers": {
            "p": {
                "format": "openai",
                "type": "api_key",
                "round_robin": round_robin,
                "connections": conns,
                "models": [model],
            }
        }
    }


def test_failover_picks_next_key_when_first_excluded():
    _, idx = resolve_active_connection(_cfg(), "p", "m1", exclude_indexes={0})
    assert idx == 1


def test_failover_skips_multiple_tried_keys():
    _, idx = resolve_active_connection(_cfg(), "p", "m1", exclude_indexes={0, 1})
    assert idx == 2


def test_failover_returns_none_when_all_excluded():
    conn, idx = resolve_active_connection(_cfg(), "p", "m1", exclude_indexes={0, 1, 2})
    assert conn is None and idx is None


def test_failover_respects_connection_indexes_authorization():
    """A model limited to conn 0 must NOT fail over to unauthorized keys."""
    conn, idx = resolve_active_connection(
        _cfg(indexes=[0]), "p", "m1", exclude_indexes={0}
    )
    assert conn is None and idx is None


def test_failover_stays_inside_authorized_subset():
    _, idx = resolve_active_connection(
        _cfg(indexes=[1, 2]), "p", "m1", exclude_indexes={1}
    )
    assert idx == 2


def test_failover_skips_disabled_keys():
    _, idx = resolve_active_connection(
        _cfg(enabled=[True, False, True]), "p", "m1", exclude_indexes={0}
    )
    assert idx == 2


def test_no_exclude_is_unchanged_top_first():
    _, idx = resolve_active_connection(_cfg(), "p", "m1")
    assert idx == 0


def test_failover_works_with_round_robin_pool():
    _, idx = resolve_active_connection(
        _cfg(round_robin=True), "p", "m1", exclude_indexes={0}
    )
    assert idx in (1, 2)
