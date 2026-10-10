"""Logprob preflight: a lane that rejects logprobs:true must cost ONE probe,
not the whole battery (2026-10-09 -- aibox/kimi/kimi-k3 400s every lp probe:
'invalid logprobs: only false is allowed for this model').
"""
import asyncio
import json

import httpx
import pytest

from app.authenticity import harvest
from app.authenticity.probes import LOGPROB_PROBES


def _mk_transport(record, support_logprobs: bool):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        wants_lp = bool(body.get("logprobs"))
        record.append(wants_lp)
        if wants_lp and not support_logprobs:
            return httpx.Response(400, json={"error": {
                "message": "invalid logprobs: only false is allowed for this model",
                "type": "InvalidParameter"}})
        choice = {"index": 0, "message": {"role": "assistant", "content": "answer"},
                  "finish_reason": "stop"}
        if wants_lp:
            choice["logprobs"] = {"content": [
                {"token": "answer", "logprob": -0.05,
                 "top_logprobs": [{"token": "answer", "logprob": -0.05}]}]}
        return httpx.Response(200, json={"id": "x", "choices": [choice],
                                         "usage": {"prompt_tokens": 10,
                                                   "completion_tokens": 4}})
    return httpx.MockTransport(handler)


async def _noop_dict(*_a, **_k):
    return {}


def _stub_harvest(monkeypatch, record, support_logprobs):
    monkeypatch.setattr(harvest, "_make_client",
                        lambda: httpx.AsyncClient(
                            transport=_mk_transport(record, support_logprobs)))
    for name in ("run_boundary_probes", "run_stability_probe",
                 "run_timing_probe", "run_refusal_probes"):
        monkeypatch.setattr(harvest, name, _noop_dict)


CFG = {"v2": False, "probe_timeout_s": 10, "total_timeout_s": 120,
       "max_tier": 1, "backoff_gaps": (), "concurrency": 4}


def test_unsupported_lane_pays_one_preflight_only(monkeypatch):
    record = []
    _stub_harvest(monkeypatch, record, support_logprobs=False)
    tm = asyncio.run(harvest.harvest_model(
        "http://x", "k", "aibox/kimi/kimi-k3",
        include_noncoding=False, config=dict(CFG)))
    lp_calls = record.count(True)
    assert lp_calls == 1, f"expected exactly 1 preflight call, got {lp_calls}"
    assert "logprob:unsupported_by_lane" in tm.meta.harvest_gaps


def test_supported_lane_runs_full_battery_and_reuses_preflight(monkeypatch):
    record = []
    _stub_harvest(monkeypatch, record, support_logprobs=True)
    tm = asyncio.run(harvest.harvest_model(
        "http://x", "k", "codex/gpt-6-luna",
        include_noncoding=False, config=dict(CFG)))
    lp_calls = record.count(True)
    assert lp_calls == len(LOGPROB_PROBES), (
        f"preflight + remaining battery = {len(LOGPROB_PROBES)}, got {lp_calls}")
    assert "logprob:unsupported_by_lane" not in tm.meta.harvest_gaps
    assert len(tm.structural.logprob_profile) == len(LOGPROB_PROBES)
