"""Standards harvester — freeze official TraitMarks from TRUSTED anchors.

A TraitMark is harvested ONCE from a trusted provider (aibox / mintrouter /
codex / antigravity OAuth) and stored as JSON. At test time the suspect is
compared against the STORED standard; the trusted source is never re-called.

Fail-open per layer: a failed probe leaves that layer at defaults and records
the gap in meta.harvest_gaps. A single model's failure never aborts a batch.

Test seams: monkeypatch `_make_client` (return an httpx.AsyncClient backed by
MockTransport) and `STANDARDS_DIR` (tmp path).
"""
import asyncio
import datetime
import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Optional
from dataclasses import asdict

import httpx

from .fingerprint import extract_features
from .probes import (TIER1_PROBES, TIER2_PROBES, HARVEST_NEEDLE_PROBES,
                     LOGPROB_PROBES, run_boundary_probes, run_stability_probe,
                     run_timing_probe, run_refusal_probes, run_probe)
from .standard_v2 import run_standard_v2, call_with_backoff
from .scope import detect_family, normalize
from .knowledge_needles import is_disclaimer_fact
from .traitmark import TraitMark

logger = logging.getLogger(__name__)

# Test seam: main.py reads STANDARDS_DIR at request time so monkeypatching it
# here propagates. Never resolve config via a bare "config.yaml".
STANDARDS_DIR = Path(__file__).parent / "reference" / "standards"


def _make_client() -> httpx.AsyncClient:
    """Test seam: tests monkeypatch this to return a MockTransport client."""
    return httpx.AsyncClient()


# ── T2.5 non-coding probe battery ────────────────────────────────────────────
# Outputs are graded by the judge model on fixed axes. Needles let tests
# distinguish each probe deterministically.
NONCODING_PROBES = [
    {
        "id": "creative_story",
        "axis": "creative",
        "prompt": (
            "Write a 4-sentence micro-story about a lighthouse keeper who is "
            "afraid of the dark. No title, no preamble."
        ),
        "max_tokens": 4096,
    },
    {
        "id": "creative_metaphor",
        "axis": "creative",
        "prompt": (
            "Describe memory in three sentences. Each sentence must contain "
            "exactly one metaphor drawn from weather — no literal statements, "
            "no preamble, no title."
        ),
        "max_tokens": 4096,
    },
    {
        "id": "eq_presence",
        "axis": "eq",
        "prompt": (
            "A friend just told you they got passed over for a promotion they'd "
            "worked toward for 2 years. Respond as that friend needs to hear — "
            "not advice, just presence. 3-4 sentences."
        ),
        "max_tokens": 4096,
    },
    {
        "id": "humor_explain",
        "axis": "humor",
        "prompt": (
            "Explain why a database index is like a library card catalog, but "
            "make it gently funny. Under 150 words."
        ),
        "max_tokens": 4096,
    },
    {
        "id": "design_critique",
        "axis": "design",
        "prompt": (
            "Critique this landing page: a hero with a stock photo of a "
            "handshake, three generic feature cards, and a big blue 'Submit' "
            "button. Be specific about what is weak and what you'd change."
        ),
        "max_tokens": 4096,
    },
]

JUDGE_AXES = ("creative", "eq", "humor", "design")

JUDGE_RUBRIC = (
    "You are grading a language model's NON-CODING output quality. Score each "
    "axis from 1 to 10 and give a justification of AT MOST 12 WORDS.\n"
    "Axes:\n"
    "- creative: originality, imagery, control of voice (low = generic/clichéd)\n"
    "- eq: empathy, perspective-taking, emotional attunement (low = robotic/advice-y)\n"
    "- humor: does the humor land without being forced (low = flat/trying too hard)\n"
    "- design: taste, specificity, critique quality (low = vague/generic praise)\n"
    "Keep every 'why' to <=12 words so the JSON stays short and ALWAYS closes. "
    "Return STRICT JSON only, no prose, no markdown fences, exactly this shape:\n"
    '{"creative":{"score":int,"why":str},"eq":{"score":int,"why":str},'
    '"humor":{"score":int,"why":str},"design":{"score":int,"why":str}}'
)


# ── Judge ────────────────────────────────────────────────────────────────────

def _strip_json_fences(text: str) -> str:
    t = (text or "").strip()
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", t, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    # Leading/trailing prose around a JSON object
    start = t.find("{")
    end = t.rfind("}")
    if start != -1 and end != -1 and end > start:
        return t[start:end + 1]
    return t


# Opt-in retry schedule for judge calls on 429/5xx (the rebuild driver sets it;
# default empty keeps offline tests instant and behaviour unchanged).
JUDGE_BACKOFF_GAPS: tuple = ()


async def judge_scores(
    judge_endpoint: str,
    judge_key: str,
    judge_model: str,
    probe_outputs: list,
    timeout_s: int = 180,
) -> dict:
    """Grade non-coding probe outputs with the judge model. NEVER raises.

    probe_outputs: list of {"axis", "id", "content"}.
    Returns {axis: {"score": int, "why": str}}; on any failure every axis is
    {"score": 0, "why": "judge_error"}.
    """
    err = {a: {"score": 0, "why": "judge_error"} for a in JUDGE_AXES}
    if not (judge_endpoint and judge_key and judge_model) or not probe_outputs:
        return err

    numbered = "\n\n".join(
        f"[{i}] axis={o.get('axis','?')} id={o.get('id','?')}\n{o.get('content','')}"
        for i, o in enumerate(probe_outputs)
    )
    judge_prompt = (
        f"{JUDGE_RUBRIC}\n\nHere are the model's outputs to grade:\n\n{numbered}\n\n"
        "Return ONLY the JSON object."
    )

    base = judge_endpoint.rstrip("/")
    url = f"{base}/v1/chat/completions" if not base.endswith("/v1") else f"{base}/chat/completions"
    try:
        async with _make_client() as client:
            _body = {
                "model": judge_model,
                "messages": [
                    {"role": "system", "content": "You output only valid JSON."},
                    {"role": "user", "content": judge_prompt},
                ],
                "max_tokens": 4096,
                "stream": False,
            }
            _hdrs = {
                "Authorization": f"Bearer {judge_key}",
                "Content-Type": "application/json",
            }
            resp = None
            for _gap in (*JUDGE_BACKOFF_GAPS, None):
                resp = await client.post(url, json=_body, headers=_hdrs,
                                         timeout=timeout_s)
                if (resp.status_code == 200 or _gap is None
                        or resp.status_code not in (429, 500, 502, 503, 504, 529)):
                    break
                await asyncio.sleep(_gap)
        if resp.status_code != 200:
            logger.warning("judge_scores: %s returned HTTP %s | body=%r",
                           judge_model, resp.status_code,
                           (resp.text or "")[:300])
            return err
        data = resp.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "") or ""
        parsed = _parse_judge_json(content)
        if not parsed:
            # One sloppy judge reply used to zero the whole battery. Retry.
            logger.warning("judge_scores: unparseable reply from %s | body=%r, retrying",
                           judge_model, (resp.text or "")[:300])
            return await _judge_once(judge_endpoint, judge_key, judge_model,
                                     judge_prompt, url, timeout_s)

        out = {}
        for axis in JUDGE_AXES:
            node = parsed.get(axis) or {}
            try:
                score = int(node.get("score", 0))
            except (TypeError, ValueError):
                score = 0
            score = max(0, min(10, score))
            why = str(node.get("why", ""))[:400]
            out[axis] = {"score": score, "why": why}
        return out
    except Exception as exc:
        # Log the TYPE: str(httpx.ReadTimeout()) can be '' (blank log lines).
        # Log the raw body too -- that IS the evidence needed to name the cause.
        try:
            _body = (resp.text or "")[:300] if resp is not None else "<no response>"
        except Exception:
            _body = "<unreadable>"
        logger.warning("judge_scores failed (%s): %s: %s | body=%r",
                       judge_model, type(exc).__name__, exc, _body)
        return err


def _parse_judge_json(content: str):
    """Parse the judge's score object from a possibly prose-wrapped reply.

    Balanced-brace scan for the FIRST complete object containing a known axis
    key; falls back to a regex over axis/score pairs. Returns {} on failure.
    """
    t = _strip_json_fences(content or "")
    # 1) direct parse
    try:
        obj = json.loads(t)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    # 2) balanced-brace scan — tolerates trailing prose ("Extra data") and
    #    picks the first object that actually carries an axis key.
    depth = 0
    start = -1
    for i, ch in enumerate(t):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    chunk = t[start:i + 1]
                    try:
                        obj = json.loads(chunk)
                    except Exception:
                        obj = None
                    if isinstance(obj, dict) and any(
                            a in obj for a in JUDGE_AXES):
                        return obj
                    start = -1
    # 3) regex fallback: {"axis": {"score": N ...
    out = {}
    for axis in JUDGE_AXES:
        m = re.search(r'"%s"\s*:\s*\{[^}]*?"score"\s*:\s*(\d+)' % re.escape(axis), t)
        if m:
            why = re.search(r'"%s"\s*:\s*\{[^}]*?"why"\s*:\s*"([^"]*)"'
                            % re.escape(axis), t)
            out[axis] = {"score": int(m.group(1)),
                         "why": (why.group(1) if why else "")[:400]}
    return out if out else None


async def _judge_once(judge_endpoint, judge_key, judge_model, judge_prompt,
                      url, timeout_s):
    """One retry attempt at the judge call (same prompt). NEVER raises."""
    err = {a: {"score": 0, "why": "judge_error"} for a in JUDGE_AXES}
    resp = None
    try:
        async with _make_client() as client:
            resp = await client.post(
                url,
                json={
                    "model": judge_model,
                    "messages": [
                        {"role": "system", "content": "You output only valid JSON."},
                        {"role": "user", "content": judge_prompt},
                    ],
                    "max_tokens": 3072,
                    "stream": False,
                },
                headers={"Authorization": f"Bearer {judge_key}",
                         "Content-Type": "application/json"},
                timeout=timeout_s,
            )
        if resp.status_code != 200:
            logger.warning("judge_scores retry: %s returned HTTP %s | body=%r",
                           judge_model, resp.status_code,
                           (resp.text or "")[:300])
            return err
        data = resp.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "") or ""
        parsed = _parse_judge_json(content)
        if not parsed:
            logger.warning("judge_scores retry: %s reply still unparseable | body=%r",
                           judge_model, (resp.text or "")[:300])
            return err
        out = {}
        for axis in JUDGE_AXES:
            node = parsed.get(axis) or {}
            try:
                score = int(node.get("score", 0))
            except (TypeError, ValueError):
                score = 0
            out[axis] = {"score": max(0, min(10, score)),
                         "why": str(node.get("why", ""))[:400]}
        return out
    except Exception as exc:
        try:
            _body = (resp.text or "")[:300] if resp is not None else "<no response>"
        except Exception:
            _body = "<unreadable>"
        logger.warning("judge_scores retry failed (%s): %s: %s | body=%r",
                       judge_model, type(exc).__name__, exc, _body)
        return err


# ── Harvest ──────────────────────────────────────────────────────────────────

_PERSONA_NAMES = ("mintrouter", "aibox", "antigravity", "vsllm",
                  "openrouter", "router", "relay", "proxy")
_PERSONA_RE = re.compile(
    r"(?i)\b(?:" + "|".join(_PERSONA_NAMES) + r")\b[\w.\-]*|"
    r"\b(?:running on|powered by|served by|via|through)\b")


def _strip_persona(text: str) -> str:
    """Remove provider/relay persona wording from a self-claim so the frozen
    identity reflects the MODEL, not whichever wrapper the anchor lane adds."""
    if not text:
        return ""
    cleaned = _PERSONA_RE.sub(" ", text)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" ,.;:-")
    return cleaned


def _parse_needle_entries(text: str) -> list:
    """Parse anchor self-reported needle text into {fact, needle} entries.

    Expected line format: 'detail ||| event' (known-edge) or a plain line
    (post-cutoff). Each entry carries fact (question text) + needle (the
    distinctive token a correct answer must contain)."""
    out = []
    for raw in (text or "").splitlines():
        line = raw.strip().lstrip("0123456789.-) ").strip()
        if not line or len(line) < 8:
            continue
        if "|||" in line:
            needle, _, fact = line.partition("|||")
            fact, needle = fact.strip(), needle.strip()
        else:
            fact, needle = line, ""
        if fact and is_disclaimer_fact(fact):
            continue  # audit C8: disclaimers are not facts
        if fact and fact not in [e["fact"] for e in out]:
            out.append({"fact": fact, "needle": (needle or fact)[:80]})
    return out[:8]


def _family_ref(model_id: str) -> dict:
    """Minimal family reference for extract_features (keywords)."""
    fam = detect_family(model_id) or ""
    # extract_features only needs self/impostor keyword lists; pull from a
    # families.json if present, else synthesize from the detected family.
    families_path = Path(__file__).parent / "reference" / "families.json"
    if families_path.exists():
        try:
            families = json.loads(families_path.read_text(encoding="utf-8"))
            if fam in families:
                return families[fam]
        except Exception:
            pass
    return {
        "family": fam,
        "self_keywords": [fam] if fam else [],
        "impostor_keywords": [k for k in ("glm", "kimi", "qwen", "deepseek",
                                          "claude", "gpt", "gemini", "mimo",
                                          "hunyuan") if k != fam],
    }


async def harvest_model(
    endpoint: str,
    api_key: str,
    model_id: str,
    *,
    judge_endpoint: Optional[str] = None,
    judge_key: Optional[str] = None,
    judge_model: Optional[str] = None,
    include_noncoding: bool = True,
    source_tag: str = "",
    config: Optional[dict] = None,
) -> TraitMark:
    """Probe a trusted anchor and build its TraitMark. NEVER raises."""
    cfg = config or {}
    probe_timeout = int(cfg.get("probe_timeout_s", 90))
    total_timeout = int(cfg.get("total_timeout_s", 300))
    max_tier = int(cfg.get("max_tier", 2))

    tm = TraitMark()
    tm.meta.model_id = model_id
    tm.meta.family = detect_family(model_id) or ""
    _sub = normalize(model_id)
    if not (source_tag or "").strip():
        # CANONICAL harvest: a provider-prefixed sub_model ('codex/gpt-5.6-sol')
        # is private-infrastructure-tagged and gets filtered OUT of both the
        # canonical and public testable lists by the _PRIV regex. Strip the
        # provider/ prefix so the bare standard ('gpt-5.6-sol') surfaces.
        # Source-tagged harvests keep the full id (private calibration refs).
        _sub = _sub.split("/")[-1]
    tm.meta.sub_model = _sub
    tm.meta.tier = "canonical"
    tm.meta.collected_at = datetime.datetime.now(
        datetime.timezone.utc).isoformat()
    tm.meta.collected_from = [endpoint]
    tm.meta.source_tag = (source_tag or "").strip()
    tm.meta.anchor_lane = model_id
    if not hasattr(tm.meta, "harvest_gaps"):
        # MetaLayer gained harvest_gaps; from_dict filters unknown fields, so
        # setting it here is forward/backward compatible either way.
        try:
            tm.meta.harvest_gaps = []
        except Exception:
            pass

    gaps = []
    t0 = time.monotonic()
    family_ref = _family_ref(model_id)
    noncoding_outputs = []
    needle_known_text = ""
    needle_post_text = ""
    logprob_profile = {}
    v2_profile = {}
    _consistency_paraphrase_text = ""
    _consistency_fact_answers = []

    try:
        async with _make_client() as client:

            _sem = asyncio.Semaphore(int(cfg.get("concurrency", 64)))
            _gaps = tuple(cfg.get("backoff_gaps", ()))

            async def _probe(probe, tier):
                if (time.monotonic() - t0) > total_timeout:
                    gaps.append(f"timeout_before:{probe['id']}")
                    return None
                async with _sem:
                    r = await call_with_backoff(client, endpoint, api_key, model_id,
                                                probe, probe_timeout, gaps=_gaps)
                if r is not None:
                    r.tier = tier
                    if r.error:
                        gaps.append(f"probe_error:{probe['id']}")
                return r

            async def _probe_lp(probe):
                if (time.monotonic() - t0) > total_timeout:
                    gaps.append(f"timeout_before:{probe['id']}")
                    return None
                async with _sem:
                    r = await call_with_backoff(
                        client, endpoint, api_key, model_id, probe, probe_timeout,
                        extra={"logprobs": True, "top_logprobs": 5,
                               "temperature": 0}, gaps=_gaps)
                if r is not None:
                    r.tier = 2
                return r

            async def _probe_lp_once(probe):
                # Preflight MUST NOT retry: the router surfaces kimi's 400
                # logprob rejection as a 502, which backoff classifies as
                # retryable, firing the probe len(gaps)+1 times. One shot.
                if (time.monotonic() - t0) > total_timeout:
                    gaps.append(f"timeout_before:{probe['id']}")
                    return None
                async with _sem:
                    r = await run_probe(
                        client, endpoint, api_key, model_id, probe, probe_timeout,
                        extra={"logprobs": True, "top_logprobs": 5,
                               "temperature": 0}, retries=0)
                if r is not None:
                    r.tier = 2
                return r

            # Tier 1 + Tier 2 (parallel): reasoning probes take ~20s each, so
            # sequential execution blows the total battery timeout. Gather runs
            # them concurrently over the shared client.
            tier_jobs = [(_probe(p, 1)) for p in TIER1_PROBES]
            if max_tier >= 2:
                tier_jobs += [(_probe(p, 2)) for p in TIER2_PROBES]
            # Version-discrimination capture: needles (self-reported) + logprobs.
            tier_jobs += [_probe(p, 2) for p in HARVEST_NEEDLE_PROBES]
            # Logprob preflight (2026-10-09): some lanes (aibox/kimi) reject
            # logprobs:true with 400/502 -- every lp probe is then a PAID,
            # guaranteed-failed call. Probe support ONCE with LOGPROB_PROBES[0];
            # the battery only runs when the preflight PROVES support (200 +
            # actual logprobs payload), and the preflight result doubles as the
            # first sample. An unsupported lane costs exactly one probe, not N.
            _lp_pf = await _probe_lp_once(LOGPROB_PROBES[0]) if LOGPROB_PROBES else None
            _lp_supported = (_lp_pf is not None and _lp_pf.status_code == 200
                             and _lp_pf.logprobs is not None)
            if _lp_supported:
                tier_jobs += [_probe_lp(p) for p in LOGPROB_PROBES[1:]]
            elif LOGPROB_PROBES:
                gaps.append("logprob:unsupported_by_lane")
            tier_results = [r for r in await asyncio.gather(*tier_jobs) if r]
            if _lp_pf is not None and not _lp_pf.error:
                tier_results.append(_lp_pf)

            # Split needle + logprob results back out by probe id.
            for r in tier_results:
                if r.probe_id == "needle_known_edge" and not r.error:
                    needle_known_text = r.content or ""
                elif r.probe_id == "needle_post_cutoff" and not r.error:
                    needle_post_text = r.content or ""
                elif r.probe_id.startswith("lp_tok_") and r.logprobs:
                    first = (r.logprobs.get("content") or [{}])[0]
                    logprob_profile[r.probe_id] = {
                        "logprob": first.get("logprob"),
                        "top": {t.get("token"): t.get("logprob")
                                for t in (first.get("top_logprobs") or [])},
                    }

            # Phase 4 consistency gates (opt-in, <= 7 extra paid calls).
            if cfg.get("consistency_v2", False):
                try:
                    from .consistency import (
                        paraphrase_needle_prompt,
                        fact_check_probes,
                    )
                    # 1. Paraphrase the needle_known_edge prompt and re-ask.
                    _nk_probe = next(
                        (p for p in HARVEST_NEEDLE_PROBES
                         if p.get("id") == "needle_known_edge"), None)
                    if _nk_probe and needle_known_text:
                        _para_probe = {
                            "id": "consistency_paraphrase",
                            "prompt": paraphrase_needle_prompt(
                                _nk_probe.get("prompt", "")),
                            "max_tokens": _nk_probe.get("max_tokens", 1024),
                        }
                        _r_para = await _probe(_para_probe, 2)
                        if _r_para and not _r_para.error:
                            _consistency_paraphrase_text = _r_para.content or ""
                    # 2. Fact-check: re-ask up to 6 harvested known-facts.
                    _con_facts = _parse_needle_entries(needle_known_text)
                    _fc_probes = fact_check_probes(_con_facts)
                    if _fc_probes:
                        _fc_results = await asyncio.gather(
                            *[_probe(p, 2) for p in _fc_probes])
                        _consistency_fact_answers = [
                            (r.content or "") if (r and not r.error) else ""
                            for r in _fc_results
                        ]
                except Exception as exc:
                    gaps.append(f"consistency_error:{str(exc)[:120]}")

            # Tier 2.5 non-coding (parallel; outputs kept for the judge)
            if include_noncoding:
                nc_results = await asyncio.gather(*[_probe(p, 3) for p in NONCODING_PROBES])
                for probe, r in zip(NONCODING_PROBES, nc_results):
                    if r and not r.error:
                        noncoding_outputs.append(
                            {"axis": probe["axis"], "id": probe["id"],
                             "content": r.content})

            # V6a/V6b/V7 cascade captures (harvest is offline: L1/L2/L3 ALL
            # run — baselines must be complete, no abstain trigger needed).
            # Router-only: every helper goes through the endpoint key above.
            boundary_signature = {}
            stability_profile = {}
            timing_profile = {}
            try:
                if (time.monotonic() - t0) < total_timeout:
                    boundary_signature = await run_boundary_probes(
                        client, endpoint, api_key, model_id, probe_timeout)
            except Exception as exc:
                gaps.append(f"boundary_capture_error:{str(exc)[:120]}")
            try:
                if (time.monotonic() - t0) < total_timeout:
                    stability_profile = await run_stability_probe(
                        client, endpoint, api_key, model_id, probe_timeout)
            except Exception as exc:
                gaps.append(f"stability_capture_error:{str(exc)[:120]}")
            try:
                if (time.monotonic() - t0) < total_timeout:
                    timing_profile = await run_timing_probe(
                        client, endpoint, api_key, model_id, probe_timeout)
            except Exception as exc:
                gaps.append(f"timing_capture_error:{str(exc)[:120]}")
            refusal_signature = {}
            try:
                if (time.monotonic() - t0) < total_timeout:
                    refusal_signature = await run_refusal_probes(
                        client, endpoint, api_key, model_id, probe_timeout)
            except Exception as exc:
                gaps.append(f"refusal_capture_error:{str(exc)[:120]}")
            # Standard v2 (tokenizer deltas / capability curve / knowledge bank).
            # Own budget: the full bank would never fit the 300s battery budget.
            v2_profile = {}
            if cfg.get("v2", True):
                try:
                    # hard_series judged half (2026-10-20): the harvest path is
                    # the only caller with judge creds; the gate's live pass
                    # passes judge=None and stays judge-free (math half only).
                    v2_profile = await asyncio.wait_for(
                        run_standard_v2(client, endpoint, api_key, model_id,
                                        int(cfg.get("v2_probe_timeout_s", 180)),
                                        mode="harvest",
                                        concurrency=int(cfg.get("v2_concurrency", 4)),
                                        gaps=tuple(cfg.get("backoff_gaps", ())),
                                        judge=(judge_endpoint, judge_key, judge_model)),
                        timeout=float(cfg.get("v2_total_timeout_s", 1500)))
                except Exception as exc:
                    gaps.append(f"v2_capture_error:{type(exc).__name__}:{str(exc)[:100]}")
    except Exception as exc:
        gaps.append(f"probe_batch_error:{str(exc)[:120]}")

    # Populate layers from extracted features (best-effort).
    try:
        feats = extract_features(tier_results, family_ref)
        k = tm.knowledge
        k.cutoff_year = int(getattr(feats, "cutoff_year", 0) or 0)
        k.cutoff_month = int(getattr(feats, "cutoff_month", 0) or 0)
        k.capability_boundary = {
            "correct": int(getattr(feats, "capability_correct", 0) or 0)}
        # V1 needles: self-reported cutoff-edge knowns + admitted post-cutoff unknowns.
        k.known_facts = _parse_needle_entries(needle_known_text)
        k.post_cutoff_unknown = _parse_needle_entries(needle_post_text)
        # V5 logprob tail (weight-level). Captured via the ROUTER endpoint —
        # the router passes logprobs through its SSE buffer, so no upstream
        # bypass or provider keys are needed (harvest HARD RULE: router only).
        tm.structural.logprob_profile = logprob_profile
        if not tm.structural.logprob_profile:
            gaps.append("logprobs:unavailable")
        if not k.known_facts and not k.post_cutoff_unknown:
            gaps.append("needles:no_self_report")

        # V6a/V6b/V7 cascade profiles into the structural layer. The
        # StructuralLayer dataclass (read-only traitmark.py) has no fields for
        # these, so they nest inside tokenizer_signature, whose dict shape the
        # matcher already reads through (V5 logprob_profile precedent).
        _struct = tm.structural.tokenizer_signature
        if isinstance(boundary_signature, dict) and boundary_signature:
            _struct["boundary_signature"] = boundary_signature
        if isinstance(stability_profile, dict) and stability_profile.get("mode_hash"):
            _struct["stability_profile"] = stability_profile
        if isinstance(timing_profile, dict) and timing_profile.get("chunk_count"):
            _struct["timing_profile"] = timing_profile
        if cfg.get("v2", True):
            if isinstance(v2_profile, dict) and (v2_profile.get("tokenizer")
                                                  or v2_profile.get("capability_curve")
                                                  or v2_profile.get("knowledge_bank")
                                                  or v2_profile.get("discriminator")
                                                  or v2_profile.get("spatial_curve")
                                                  or v2_profile.get("hard_series")):
                _struct["v2"] = v2_profile
            for _lane in ("tokenizer", "capability_curve", "knowledge_bank",
                          "discriminator", "spatial_curve", "hard_series"):
                if not (isinstance(v2_profile, dict) and v2_profile.get(_lane)):
                    gaps.append(f"v2_{_lane}:unavailable")
        # Phase 4 consistency scoring (opt-in).
        if cfg.get("consistency_v2", False):
            try:
                from .consistency import (
                    agreement_score,
                    fact_consistency,
                    detect_cutoff_contradictions,
                )
                _con: dict = {}
                if _consistency_paraphrase_text and needle_known_text:
                    _con["agreement_score"] = agreement_score(
                        needle_known_text, _consistency_paraphrase_text)
                _con_facts = _parse_needle_entries(needle_known_text)
                if _con_facts and _consistency_fact_answers:
                    _con["fact_consistency"] = fact_consistency(
                        _con_facts, _consistency_fact_answers)
                _con["cutoff_contradictions"] = detect_cutoff_contradictions(
                    k.cutoff_year, k.cutoff_month, v2_profile)
                _struct["consistency_v2"] = _con
            except Exception as exc:
                gaps.append(f"consistency_score_error:{str(exc)[:120]}")
        if not _struct.get("boundary_signature"):
            gaps.append("boundary:unavailable")
        if not _struct.get("stability_profile"):
            gaps.append("stability:unavailable")
        # Refusal/alignment signature into the dedicated RefusalLayer.
        if isinstance(refusal_signature, dict) and refusal_signature.get("axes"):
            tm.refusal.axes = refusal_signature.get("axes", {})
            tm.refusal.delta_signature = refusal_signature.get("delta", {})
            tm.refusal.templates = refusal_signature.get("templates", {})
            tm.refusal.captured = int(refusal_signature.get("captured", 0))
        else:
            gaps.append("refusal:unavailable")
        if not _struct.get("timing_profile"):
            gaps.append("timing:unavailable")

        s = tm.style
        s.bullet_style = getattr(feats, "bullet_style", "") or ""
        s.header_depth = int(getattr(feats, "header_depth", 0) or 0)
        s.avg_sentence_length = float(getattr(feats, "avg_sentence_length", 0.0) or 0.0)
        s.cjk_ratio = float(getattr(feats, "cjk_ratio", 0.0) or 0.0)
        s.default_response_length = int(getattr(feats, "response_length", 0) or 0)

        r = tm.reasoning
        r.emits_thinking = bool(
            any((getattr(pr, "thinking", "") or "") for pr in tier_results))

        idn = tm.identity
        idn.self_claim_model = _strip_persona(getattr(feats, "claimed_model", "") or "")
        idn.self_claim_org = _strip_persona(getattr(feats, "claimed_org", "") or "")
        idn.self_keywords = list(family_ref.get("self_keywords", []))
        idn.impostor_keywords = list(family_ref.get("impostor_keywords", []))
    except Exception as exc:
        gaps.append(f"feature_extract_error:{str(exc)[:120]}")

    # Judge the non-coding outputs (only with judge creds + outputs).
    if include_noncoding and noncoding_outputs and \
            judge_endpoint and judge_key and judge_model:
        scores = await judge_scores(judge_endpoint, judge_key, judge_model,
                                    noncoding_outputs)
        for axis in JUDGE_AXES:
            setattr(tm.noncoding, axis, scores.get(
                axis, {"score": 0, "why": "judge_error"}))
    elif include_noncoding:
        gaps.append("noncoding:no_judge_or_outputs")

    # Lexical layer (slop + vocabulary). Computed from the harvested non-coding
    # outputs — mirrors gate.py's live formula (same wordlist + TTR) so the
    # frozen standard and the live suspect are comparable. Never gated on the
    # judge: this is deterministic signal, independent of judge availability.
    if include_noncoding and noncoding_outputs:
        try:
            joined = " ".join(o["content"] for o in noncoding_outputs if o.get("content"))
            words = re.findall(r"[A-Za-z']+", joined)
            ttr = (len(set(w.lower() for w in words)) / len(words)) if words else 0.0
            slop = sum(joined.lower().count(w) for w in
                       ("delve", "tapestry", "landscape", "testament", "navigate",
                        "crucial", "vibrant", "furthermore", "moreover", "elevate"))
            tm.lexical.slop_score = round(slop / max(len(words), 1), 4)
            tm.lexical.vocabulary_complexity = round(ttr, 4)
        except Exception as exc:
            gaps.append(f"lexical_compute_error:{str(exc)[:120]}")

    try:
        tm.meta.judged_by = judge_model if (judge_endpoint and judge_key and judge_model) else ""
        tm.meta.harvest_gaps = gaps
    except Exception:
        pass
    return tm


# ── Persistence ──────────────────────────────────────────────────────────────

def _standard_filename(tm: TraitMark) -> str:
    fam = re.sub(r"[^a-z0-9_-]+", "-", (tm.meta.family or "unknown").lower())
    sub = re.sub(r"[^a-z0-9_-]+", "-", (tm.meta.sub_model or tm.meta.model_id
                                        or "model").lower())
    src = re.sub(r"[^a-z0-9_-]+", "-", (tm.meta.source_tag or "").lower())
    return f"{fam}_{sub}_{src}.json" if src else f"{fam}_{sub}.json"


def standard_filename(family: str, sub_model: str, source_tag: str = "") -> str:
    """Filename for a standard, computed from raw strings.

    Single source of truth for the on-disk layout: `_standard_filename` (TraitMark
    in), `delete_standard`, and main.py's persist-delete all resolve through the
    same sanitisation + suffix rule, so an unlink and a `git rm` can never
    disagree about the path.
    """
    fam = re.sub(r"[^a-z0-9_-]+", "-", (family or "").lower())
    sub = re.sub(r"[^a-z0-9_-]+", "-", (sub_model or "").lower())
    src = re.sub(r"[^a-z0-9_-]+", "-", (source_tag or "").lower())
    return f"{fam}_{sub}_{src}.json" if src else f"{fam}_{sub}.json"


def save_standard(tm: TraitMark, standards_dir) -> Path:
    """Atomic write: temp file on same filesystem + os.replace."""
    d = Path(standards_dir)
    d.mkdir(parents=True, exist_ok=True)
    target = d / _standard_filename(tm)
    fd, tmp_path = tempfile.mkstemp(dir=str(d), prefix=".tmp_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(tm.to_dict(), fh, indent=2, ensure_ascii=False)
        os.replace(tmp_path, target)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return target


def _layer_nonempty(layer) -> bool:
    """True if a dataclass layer carries ANY non-default behavioral signal."""
    try:
        d = asdict(layer)
    except Exception:
        return False
    for v in d.values():
        if isinstance(v, bool):
            if v:
                return True
        elif isinstance(v, (int, float)):
            if v not in (0, 0.0):
                return True
        elif isinstance(v, str):
            if v.strip():
                return True
        elif isinstance(v, (list, dict)):
            if v:
                return True
    return False


def standard_quality(tm: TraitMark) -> dict:
    """Judge-independent quality grade of a frozen standard.

    full          -> judge-graded T2.5 noncoding battery captured (no judge gap) AND
                     >=4 behavioral layers populated. High-confidence verification.
    family_only   -> some signal but the noncoding battery is missing/hollow
                     (the 'no_judge_or_outputs' gap) -> family classification only.
    hollow        -> almost no behavioral signal captured; do NOT trust Tier-2.
    """
    gaps = list(getattr(tm.meta, "harvest_gaps", []) or [])
    layers = {
        "identity": _layer_nonempty(tm.identity),
        "knowledge": _layer_nonempty(tm.knowledge),
        "reasoning": _layer_nonempty(tm.reasoning),
        "style": _layer_nonempty(tm.style),
        "lexical": _layer_nonempty(tm.lexical),
        "noncoding": _layer_nonempty(tm.noncoding),
        "structural": _layer_nonempty(tm.structural),
    }
    populated = sum(1 for v in layers.values() if v)
    judge_gap = any("no_judge" in g for g in gaps)
    noncoding_ok = layers["noncoding"] and not judge_gap
    if populated < 3:
        grade = "hollow"
    elif noncoding_ok and populated >= 4:
        grade = "full"
    else:
        grade = "family_only"
    return {
        "grade": grade,
        "populated_layers": populated,
        "layers": layers,
        "judge_gap": judge_gap,
        "high_confidence": grade == "full",
        "gaps": gaps,
    }


# Judge suggestion: a judge must be a DIFFERENT family from the suspect, else
# creative/EQ/design scores are inflated by family resemblance. Default = claude-opus-5-5.
# Generic judge defaults — name the FAMILY only, never a provider id. The judge
# is chosen from the user's own enabled models at runtime; these are fallbacks.
# (The old mintrouter/codex ids leaked private infrastructure into a public repo.)
JUDGE_DEFAULT = "claude"
JUDGE_SUGGESTIONS = {
    "claude":   "gpt",
    "gpt":      "claude",
    "gemini":   "claude",
    "glm":      "claude",
    "kimi":     "claude",
    "deepseek": "claude",
    "qwen":     "claude",
    "mimo":     "claude",
}

# Vendor display names for the public testable-models database (family -> vendor).
_FAMILY_VENDOR = {
    "claude": "Anthropic", "gpt": "OpenAI", "gemini": "Google", "glm": "Zhipu",
    "qwen": "Alibaba", "deepseek": "DeepSeek", "kimi": "Moonshot", "mimo": "Xiaomi",
}


def suggest_judge(family: str) -> str:
    """Recommended judge for a suspect of `family` (always a different family)."""
    return JUDGE_SUGGESTIONS.get((family or "").lower(), JUDGE_DEFAULT)


def list_standards(standards_dir) -> list:
    """Summaries for the admin UI. Never raises."""
    out = []
    d = Path(standards_dir)
    if not d.exists():
        return out
    for path in sorted(d.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            tm = TraitMark.from_dict(data)
            q = standard_quality(tm)
            out.append({
                "family": tm.meta.family,
                "sub_model": tm.meta.sub_model,
                "collected_at": tm.meta.collected_at,
                "tier": tm.meta.tier,
                "stale": tm.is_stale(),
                "file": path.name,
                "gaps": getattr(tm.meta, "harvest_gaps", []),
                "judged_by": getattr(tm.meta, "judged_by", ""),
                "source_tag": getattr(tm.meta, "source_tag", ""),
                "suggested_judge": suggest_judge(tm.meta.family),
                "quality": q["grade"],
                "populated_layers": q["populated_layers"],
                "high_confidence": q["high_confidence"],
            })
        except Exception:
            continue
    return out


def list_testable_standards(standards_dir) -> dict:
    """Public testable-models database, grouped by vendor.

    Only CANONICAL standards are listed (source_tag empty) — provider-tagged
    local references are private infrastructure and never surface in the
    public list. Each entry's sub_model is display-normalised (family prefix
    stripped) so the UI renders "claude-fable-5-1" under "Anthropic".
    """
    standards = list_standards(standards_dir)
    # Public/private split: a standard is PUBLIC only if its sub_model carries no
    # provider token ANYWHERE — slash-prefixed ('codex/gpt-5.5'), dash-prefixed
    # ('mintrouter-claude-...'), or bare ('workbuddy'). Provider-tagged local
    # references are private infrastructure and never surface in the public
    # testable list — matching the gitignore whitelist.
    import re as _re
    _PRIV = _re.compile(r'(^|[/\-_])(mintrouter|vsllm-o|vsllm-r|pix4k|a6api|sharellm|'
                        r'workbuddy|zai-web\d|chat2api|antigravity|arena-web|'
                        r'deepseek-web|qwen-web|mimo-web|codex|ds|zhipu)([/\-_]|$)', _re.I)
    canonical = [s for s in standards
                 if not (s.get("source_tag") or "").strip()
                 and not _PRIV.search(s.get("sub_model", ""))]
    full = [s for s in canonical if s.get("high_confidence")]
    partial = [s for s in canonical if not s.get("high_confidence")]

    def _display(s):
        sub = s.get("sub_model", "")
        fam = s.get("family", "")
        # strip family prefix: 'claude-claude-fable-5-1' -> 'fable-5-1' only if doubled
        if sub.lower().startswith(fam.lower() + "-"):
            sub = sub[len(fam) + 1:]
        return sub

    def _group(entries, ok):
        groups = {}
        for s in entries:
            fam = s.get("family", "")
            vendor = _FAMILY_VENDOR.get(fam.lower(), fam.title())
            groups.setdefault(vendor, []).append({
                "model": _display(s),
                "quality": s.get("quality", ""),
                "confidence": "high" if ok else "family-only",
                "suggested_judge": s.get("suggested_judge", JUDGE_DEFAULT),
            })
        for vendor in groups:
            groups[vendor].sort(key=lambda x: x["model"])
        return dict(sorted(groups.items()))

    return {
        "vendors": _group(full, True),
        "family_only_vendors": _group(partial, False),
        "count_full": len(full),
        "count_family_only": len(partial),
    }


def delete_standard(standards_dir, family: str, sub_model: str, source_tag: str = "") -> bool:
    d = Path(standards_dir)
    target = d / standard_filename(family, sub_model, source_tag)
    if target.exists():
        target.unlink()
        return True
    return False
