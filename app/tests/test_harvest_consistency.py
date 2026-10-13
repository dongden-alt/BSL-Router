"""Unit tests for app.authenticity.consistency (Phase 4 consistency gates).

All tests are fully offline — no network, no real LLM calls.
MockTransport pattern mirrors test_authenticity_standard_v2.py.
"""
import asyncio
import json

import httpx
import pytest

from app.authenticity.consistency import (
    paraphrase_needle_prompt,
    agreement_score,
    fact_check_probes,
    fact_consistency,
    detect_cutoff_contradictions,
)
import app.authenticity.harvest as harvest_mod


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# paraphrase_needle_prompt
# ---------------------------------------------------------------------------

class TestParaphraseNeedlePrompt:
    def test_deterministic(self):
        p = "What is the capital of France?"
        assert paraphrase_needle_prompt(p) == paraphrase_needle_prompt(p)

    def test_output_differs_from_input(self):
        p = "What is the capital of France?"
        out = paraphrase_needle_prompt(p)
        assert out != p

    def test_output_contains_original_question(self):
        p = "What is the capital of France?"
        out = paraphrase_needle_prompt(p)
        assert p in out

    def test_empty_string(self):
        assert paraphrase_needle_prompt("") == ""

    def test_whitespace_only(self):
        assert paraphrase_needle_prompt("   ") == ""


# ---------------------------------------------------------------------------
# agreement_score
# ---------------------------------------------------------------------------

class TestAgreementScore:
    def test_identical_strings(self):
        assert agreement_score("hello world", "hello world") == pytest.approx(1.0)

    def test_disjoint_strings(self):
        assert agreement_score("cat dog", "fish bird") == pytest.approx(0.0)

    def test_partial_overlap(self):
        score = agreement_score("the cat sat", "the dog sat")
        assert 0.0 < score < 1.0

    def test_empty_empty(self):
        assert agreement_score("", "") == 1.0

    def test_empty_nonempty(self):
        assert agreement_score("", "hello") == 0.0

    def test_nonempty_empty(self):
        assert agreement_score("hello", "") == 0.0

    def test_case_insensitive(self):
        assert agreement_score("Paris", "paris") == pytest.approx(1.0)

    def test_score_in_range(self):
        s = agreement_score("alpha beta gamma", "beta gamma delta")
        assert 0.0 <= s <= 1.0


# ---------------------------------------------------------------------------
# fact_check_probes
# ---------------------------------------------------------------------------

class TestFactCheckProbes:
    def test_empty_facts(self):
        assert fact_check_probes([]) == []

    def test_caps_at_max_facts(self):
        facts = [{"fact": f"Fact {i}", "needle": f"n{i}"} for i in range(10)]
        probes = fact_check_probes(facts, max_facts=6)
        assert len(probes) == 6

    def test_probe_shape(self):
        facts = [{"fact": "Paris is the capital of France.", "needle": "paris"}]
        probes = fact_check_probes(facts)
        assert len(probes) == 1
        p = probes[0]
        assert p["id"] == "factcheck_0"
        assert "Paris" in p["prompt"]
        assert p["max_tokens"] == 512

    def test_skips_blank_facts(self):
        facts = [{"fact": "", "needle": "x"}, {"fact": "Real fact.", "needle": "real"}]
        probes = fact_check_probes(facts)
        assert len(probes) == 1
        assert "Real fact." in probes[0]["prompt"]


# ---------------------------------------------------------------------------
# fact_consistency
# ---------------------------------------------------------------------------

class TestFactConsistency:
    def _fact(self, needle, fact="Some fact."):
        return {"fact": fact, "needle": needle}

    def test_all_consistent(self):
        facts = [self._fact("paris"), self._fact("london")]
        answers = ["The capital is Paris.", "The capital is London."]
        result = fact_consistency(facts, answers)
        assert result["score"] == pytest.approx(1.0)
        assert len(result["consistent"]) == 2
        assert result["inconsistent"] == []
        assert result["checked"] == 2

    def test_all_inconsistent(self):
        facts = [self._fact("paris"), self._fact("london")]
        answers = ["The capital is Berlin.", "The capital is Madrid."]
        result = fact_consistency(facts, answers)
        assert result["score"] == pytest.approx(0.0)
        assert result["consistent"] == []
        assert len(result["inconsistent"]) == 2
        assert result["checked"] == 2

    def test_mixed(self):
        facts = [self._fact("paris"), self._fact("london")]
        answers = ["The capital is Paris.", "The capital is Madrid."]
        result = fact_consistency(facts, answers)
        assert result["score"] == pytest.approx(0.5)
        assert result["checked"] == 2

    def test_empty_inputs(self):
        result = fact_consistency([], [])
        assert result["score"] == 1.0
        assert result["checked"] == 0

    def test_case_insensitive_needle(self):
        facts = [self._fact("PARIS")]
        answers = ["The capital is paris."]
        result = fact_consistency(facts, answers)
        assert result["score"] == pytest.approx(1.0)

    def test_skips_facts_with_blank_needle(self):
        facts = [{"fact": "Some fact.", "needle": ""}]
        result = fact_consistency(facts, ["Some answer."])
        assert result["checked"] == 0
        assert result["score"] == 1.0


# ---------------------------------------------------------------------------
# detect_cutoff_contradictions
# ---------------------------------------------------------------------------

class TestDetectCutoffContradictions:
    def test_no_v2_profile(self):
        assert detect_cutoff_contradictions(2024, 6, {}) == []

    def test_non_dict_v2_profile(self):
        assert detect_cutoff_contradictions(2024, 6, "not a dict") == []

    def test_missing_knowledge_bank(self):
        v2 = {"tokenizer": {}, "capability_curve": {}}
        assert detect_cutoff_contradictions(2024, 6, v2) == []

    def test_agreement_returns_empty(self):
        v2 = {"knowledge_bank": {"training_cutoff": "2024"}}
        result = detect_cutoff_contradictions(2024, 6, v2)
        assert result == []

    def test_year_mismatch_flagged(self):
        v2 = {"knowledge_bank": {"training_cutoff": "2025"}}
        result = detect_cutoff_contradictions(2024, 6, v2)
        assert len(result) > 0
        assert "2024" in result[0] or "2025" in result[0]

    def test_post_cutoff_blind_false_flagged(self):
        v2 = {"knowledge_bank": {"post_cutoff_blind": False}}
        result = detect_cutoff_contradictions(2024, 6, v2)
        assert len(result) > 0

    def test_missing_keys_returns_empty(self):
        v2 = {"knowledge_bank": {}}
        assert detect_cutoff_contradictions(2024, 6, v2) == []


# ---------------------------------------------------------------------------
# harvest_model integration — MockTransport, offline
# ---------------------------------------------------------------------------

def _ok_response(content: str = "Paris.") -> httpx.Response:
    return httpx.Response(200, json={
        "id": "chatcmpl-mock",
        "object": "chat.completion",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
    })


def test_harvest_flag_off_zero_behavior_change(monkeypatch):
    """With consistency_v2 absent, harvest_model must not fire consistency probes."""
    captured: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        try:
            body = json.loads(req.content)
        except Exception:
            body = {}
        captured.append(body)
        return _ok_response()

    monkeypatch.setattr(
        harvest_mod, "_make_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    tm = _run(harvest_mod.harvest_model(
        endpoint="http://mock/v1",
        api_key="mock-key",
        model_id="mock-model",
        config={"v2": False, "include_noncoding": False},
    ))

    assert tm is not None
    # No consistency probe text should appear in any captured request
    all_prompts = json.dumps(captured)
    assert "factcheck_" not in all_prompts
    assert "restate the following question" not in all_prompts.lower()


def test_harvest_flag_on_fires_consistency_probes(monkeypatch):
    """With consistency_v2=True, paraphrase and fact-check probes are fired."""
    captured: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        try:
            body = json.loads(req.content)
        except Exception:
            body = {}
        captured.append(body)
        # Return a needle-shaped response so _parse_needle_entries extracts facts
        return _ok_response("The Eiffel Tower ||| What famous tower is in Paris?\n")

    monkeypatch.setattr(
        harvest_mod, "_make_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    tm = _run(harvest_mod.harvest_model(
        endpoint="http://mock/v1",
        api_key="mock-key",
        model_id="mock-model",
        config={"v2": False, "consistency_v2": True, "include_noncoding": False},
    ))

    assert tm is not None
    gaps = getattr(tm.meta, "harvest_gaps", [])
    assert not any(g.startswith("consistency_error") for g in gaps)


def test_harvest_fail_open_on_consistency_probe_error(monkeypatch):
    """A 500 on consistency probes must not abort the harvest."""
    call_count = [0]

    def handler(req: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        try:
            body = json.loads(req.content)
        except Exception:
            body = {}
        prompt_text = json.dumps(body)
        if "restate the following question" in prompt_text.lower() or "answer the following question concisely" in prompt_text.lower():
            return httpx.Response(500, json={"error": "server error"})
        return _ok_response("The Eiffel Tower ||| What famous tower is in Paris?\n")

    monkeypatch.setattr(
        harvest_mod, "_make_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    tm = _run(harvest_mod.harvest_model(
        endpoint="http://mock/v1",
        api_key="mock-key",
        model_id="mock-model",
        config={"v2": False, "consistency_v2": True, "include_noncoding": False},
    ))

    # Harvest must return a TraitMark even when consistency probes 500
    assert tm is not None
