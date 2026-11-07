"""Unit tests for Claude Supply-Lane Classification (Bedrock vs Kiro vs Claude Max vs Anthropic API)."""
import asyncio
import json
import pytest

from app.authenticity.supply_lane import (
    classify_claude_supply_lane,
    classify_message_id,
    inspect_provider_supply_hints,
)
from app.compat.stream_normalizer import StreamNormalizer


def test_classify_message_id_variants():
    # Native AWS Bedrock (~35 chars)
    bdrk_native = "msg_bdrk_011CgdA1b2c3d4e5f6g7h8i9j0"
    r1 = classify_message_id(bdrk_native)
    assert r1["kind"] == "bedrock_native"
    assert r1["is_bedrock"] is True
    assert r1["is_anthropic_ulid"] is False

    # Gateway-wrapped AWS Bedrock (~65 chars, e.g. pix4k)
    bdrk_wrapped = "msg_bdrk_011CgdA1b2c3d4e5f6g7h8i9j0a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"
    r2 = classify_message_id(bdrk_wrapped)
    assert r2["kind"] == "bedrock_gateway_wrapped"
    assert r2["is_bedrock"] is True
    assert r2["is_anthropic_ulid"] is False

    # Direct Anthropic / Claude Max ULID (~28 chars)
    anth_id = "msg_011CgdA1b2c3d4e5f6g7h8i9"
    r3 = classify_message_id(anth_id)
    assert r3["kind"] == "anthropic_ulid"
    assert r3["is_bedrock"] is False
    assert r3["is_anthropic_ulid"] is True

    # Synthesized BSL Router ID
    synth_id = "chatcmpl-bsl-1234567890abcdef"
    r4 = classify_message_id(synth_id)
    assert r4["kind"] == "router_synthesized"
    assert r4["is_bedrock"] is False
    assert r4["is_anthropic_ulid"] is False


def test_classify_supply_lane_aws_bedrock():
    res = classify_claude_supply_lane(
        response_ids=[
            "msg_bdrk_011CgdA1b2c3d4e5f6g7h8i9j0",
            "msg_bdrk_011CgdB1b2c3d4e5f6g7h8i9j0",
        ],
        active_probes={
            "kiro_waf_triggered": False,
            "claude_max_system_prompt_detected": False,
        },
        config_hints={},
    )
    assert res["supply_lane"] == "aws-bedrock"
    assert "AWS Bedrock" in res["supply_lane_label"]
    assert res["confidence"] >= 0.90


def test_classify_supply_lane_aws_kiro_via_waf():
    res = classify_claude_supply_lane(
        response_ids=["msg_bdrk_011CgdA1b2c3d4e5f6g7h8i9j0"],
        active_probes={
            "kiro_waf_triggered": True,
            "kiro_waf_reason": "finish_reason=content_filter",
            "claude_max_system_prompt_detected": False,
        },
        config_hints={},
    )
    assert res["supply_lane"] == "aws-kiro"
    assert "Kiro" in res["supply_lane_label"]
    assert res["confidence"] >= 0.90


def test_classify_supply_lane_claude_max_oauth():
    res = classify_claude_supply_lane(
        response_ids=["msg_011CgdA1b2c3d4e5f6g7h8i9"],
        active_probes={
            "kiro_waf_triggered": False,
            "claude_max_system_prompt_detected": True,
            "claude_max_reply": "YES",
        },
        config_hints={},
    )
    assert res["supply_lane"] == "claude-max"
    assert "Claude Max" in res["supply_lane_label"]
    assert res["confidence"] >= 0.90


def test_classify_supply_lane_direct_anthropic_api():
    res = classify_claude_supply_lane(
        response_ids=["msg_011CgdA1b2c3d4e5f6g7h8i9"],
        active_probes={
            "kiro_waf_triggered": False,
            "claude_max_system_prompt_detected": False,
            "claude_max_reply": "NO",
        },
        config_hints={},
    )
    assert res["supply_lane"] == "anthropic-api"
    assert "Anthropic API" in res["supply_lane_label"]


def test_classify_supply_lane_hybrid_bedrock_anthropic():
    res = classify_claude_supply_lane(
        response_ids=[
            "msg_bdrk_011CgdA1b2c3d4e5f6g7h8i9j0a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",
            "msg_011CgdA1b2c3d4e5f6g7h8i9",
        ],
        active_probes={
            "kiro_waf_triggered": False,
            "claude_max_system_prompt_detected": False,
        },
        config_hints={},
    )
    assert res["supply_lane"] == "hybrid-bedrock-anthropic"
    assert "Hybrid Pool" in res["supply_lane_label"]
    assert res["bedrock_id_count"] == 1
    assert res["anthropic_ulid_count"] == 1


def test_stream_normalizer_preserves_upstream_anthropic_message_id():
    async def _fake_sse():
        yield b'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_bdrk_011CgdTestBedrockId999","model":"claude-opus-5-5","usage":{"input_tokens":12}}}\n\n'
        yield b'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
        yield b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"OK"}}\n\n'
        yield b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":1}}\n\n'
        yield b'event: message_stop\ndata: {"type":"message_stop"}\n\n'

    async def _run():
        normalizer = StreamNormalizer("anthropic_sse", "openai_sse", model_name="claude-opus-5-5")
        chunks = []
        async for raw in normalizer.convert_anthropic_to_openai(_fake_sse()):
            line = raw.decode("utf-8") if isinstance(raw, bytes) else raw
            for part in line.splitlines():
                if part.startswith("data: ") and part.strip() != "data: [DONE]":
                    chunks.append(json.loads(part[6:]))
        return chunks

    chunks = asyncio.run(_run())
    assert len(chunks) >= 2
    assert all(c["id"] == "msg_bdrk_011CgdTestBedrockId999" for c in chunks)
