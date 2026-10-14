"""Regression tests: judge_rank.rank_candidates JSON extraction.

The judge reply may contain MORE than one JSON object (e.g. a verdict
followed by an extra object) or brace-containing prose. The old
find("{")..rfind("}") + json.loads slice spanned both objects and raised
json.JSONDecodeError("Extra data") -> fail-open None. The fix uses
json.JSONDecoder().raw_decode(content, start), which parses exactly ONE
complete object and ignores everything after it.
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.authenticity.judge_rank import rank_candidates


def _run(coro):
    """Run an async coroutine in a sync test (no pytest-asyncio needed)."""
    return asyncio.run(coro)


class _FakeJudgeResponse:
    """Minimal httpx.Response stand-in carrying a chat-completion body."""

    status_code = 200

    def __init__(self, content: str):
        self.text = content
        self._body = json.dumps(
            {"choices": [{"message": {"content": content}}]})

    def json(self):
        return json.loads(self._body)


def _rank_with_reply(reply: str, candidates: dict):
    """Call rank_candidates with httpx.AsyncClient patched to return `reply`."""
    features = SimpleNamespace()  # every getattr(...) takes its default
    with patch("app.authenticity.judge_rank.httpx.AsyncClient") as mock_ac:
        client = mock_ac.return_value
        client.__aenter__.return_value = client
        client.post = AsyncMock(return_value=_FakeJudgeResponse(reply))
        return _run(rank_candidates(
            "https://judge.example.com", "sk-test", "judge-1",
            features, candidates,
        ))


CANDIDATES = {"cand-a": {}, "cand-b": {}}


class TestRankCandidatesJsonExtraction:
    def test_trailing_second_json_object_ignored(self):
        # {verdict} {extra} — the old rfind("}") slice spanned both objects
        # and json.loads raised "Extra data" -> fail-open None.
        result = _rank_with_reply(
            '{"best": 0, "confidence": 0.8, "why": "x"} {"extra": true}',
            CANDIDATES)
        assert result == {"id": "cand-a", "confidence": 0.8, "why": "x"}

    def test_trailing_brace_prose_ignored(self):
        # {verdict} see {this} aside — the old slice ended on the aside's
        # closing brace, producing an invalid JSON document.
        result = _rank_with_reply(
            '{"best": 1, "confidence": 0.7, "why": "a"} see {this} aside',
            CANDIDATES)
        assert result == {"id": "cand-b", "confidence": 0.7, "why": "a"}

    def test_single_clean_object_unchanged(self):
        result = _rank_with_reply(
            '{"best": 0, "confidence": 0.9, "why": "ok"}', CANDIDATES)
        assert result == {"id": "cand-a", "confidence": 0.9, "why": "ok"}

    def test_no_braces_returns_none(self):
        result = _rank_with_reply("no braces here", CANDIDATES)
        assert result is None
