"""Regression coverage for Gemini drain emission and Codex response tee."""
import asyncio
import json

import app.main as main


def test_function_call_frame_commits_transcript():
    frame = {"candidates": [{"content": {"parts": [{"functionCall": {"name": "lookup", "args": {}}}]}}]}
    assert main._gemini_frame_commits_transcript(frame, "[no output]") is True


def test_codex_response_tee_writes_only_when_enabled(monkeypatch, tmp_path):
    path = tmp_path / "tee.jsonl"
    monkeypatch.setattr(main, "_CODEX_RESPONSE_TEE_PATH", str(path))

    async def source():
        yield b'event: response.output_text.delta\ndata: {"delta":"captured-head"}\n\n'

    async def collect():
        return [chunk async for chunk in main._tee_codex_response_source(source(), "gemini", "codex-model")]

    monkeypatch.setenv("BSL_CODEX_RESPONSE_TEE", "1")
    assert asyncio.run(collect())
    record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert "captured-head" in record["bytes_head"]

    path.unlink()
    monkeypatch.delenv("BSL_CODEX_RESPONSE_TEE", raising=False)
    assert asyncio.run(collect())
    assert not path.exists()
