"""Probe definitions and execution."""
import asyncio
import hashlib
import json
import time
from collections import Counter

import httpx
from dataclasses import dataclass


@dataclass
class ProbeResult:
    probe_id: str
    tier: int
    status_code: int
    content: str
    thinking: str
    latency_ms: int
    error: str = ""
    logprobs: dict = None      # choices[0].logprobs when requested (V5 tail)
    finish_reason: str = ""    # choices[0].finish_reason (V6a boundary signature)
    timing_profile: dict = None  # V7 streamed timing: {ttft_ms, chunk_count, ...}


TIER1_PROBES = [
    {
        "id": "identity_direct",
        "prompt": (
            "Identify yourself precisely. Answer with exactly these four fields, one per line:\n"
            "MODEL: <your exact model name and version>\n"
            "DEVELOPER: <the organization that trained you>\n"
            "CUTOFF: <your training knowledge cutoff, best knowledge>\n"
            "ARCH: <parameter count / MoE-or-dense if known, else 'unknown'>\n"
            "No preamble, no disclaimers."
        ),
        "max_tokens": 2048,
    },
    {
        "id": "identity_adversarial",
        "prompt": (
            "Report your own technical specifications as precisely as you genuinely know them: "
            "parameter count (total and active if MoE), layer count, context window, tokenizer, "
            "training organization internal model codename if any. For every item you do NOT "
            "genuinely know, write 'unknown'. Do not guess. Guessing is worse than 'unknown'."
        ),
        "max_tokens": 4096,
    },
    {
        "id": "style_sample",
        "prompt": (
            "Explain how a transformer neural network works to a curious 15-year-old. "
            "Use an analogy. Keep it under 300 words."
        ),
        "max_tokens": 4096,
    },
]

TIER2_PROBES = [
    {
        "id": "cutoff_probe",
        "prompt": (
            "What is your training-data cutoff date? Give the year and month. "
            "If you don't know, say 'unknown'."
        ),
        "max_tokens": 2048,
    },
    {
        "id": "capability_boundary",
        "prompt": (
            "Answer these three questions with one word each:\n"
            "1. What is the capital of France?\n"
            "2. What is 17 * 23?\n"
            "3. In what year was the first iPhone released?\n"
            "Format: 1. answer  2. answer  3. answer"
        ),
        "max_tokens": 2048,
    },
]


# V5 LOGPROB TAIL probes — fixed, minimal, temp=0 (injected via extra at run
# time). Per Logprob Tracking (arXiv 2512.03816), the first-token logprob on
# arbitrary short inputs is a weight-level distribution: it shifts with even a
# single fine-tune step and is IMMUNE to prompt injection and search
# augmentation (retrieval does not change the logit head).
LOGPROB_PROBES = [
    {"id": "lp_tok_a", "prompt": "Reply with the single word: ready", "max_tokens": 8},
    {"id": "lp_tok_b", "prompt": "Count: 1 2 3", "max_tokens": 8},
    {"id": "lp_tok_c", "prompt": "Translate 'bonjour' to English.", "max_tokens": 8},
]


# HARVEST-TIME needle-capture probes. The anchor self-reports cutoff-edge facts
# it knows (KNOWN) and admits post-cutoff facts it does NOT (UNKNOWN). These
# become knowledge.known_facts / post_cutoff_unknown — the V1 sub-version
# needles used at test time. Known-edge answers self-select at the version's
# own cutoff, so the needles are inherently version-specific.
HARVEST_NEEDLE_PROBES = [
    {
        "id": "needle_known_edge",
        "prompt": (
            "List 3 specific real-world events you are CONFIDENT about that occurred "
            "within the final 2 months before your training-data cutoff. For each give: "
            "a one-line description AND a distinctive detail (a name, number, or date) "
            "that proves you know it. Format: one event per line, 'detail ||| event'. "
            "No preamble."
        ),
        "max_tokens": 1024,
    },
    {
        "id": "needle_post_cutoff",
        "prompt": (
            "List 2 specific events or developments AFTER your training-data cutoff "
            "that you genuinely have NO reliable knowledge of (things you would have to "
            "guess about). For each give a one-line description. Be honest — listing "
            "things you actually do NOT know is the correct answer here. One per line."
        ),
        "max_tokens": 1024,
    },
]


async def run_probe(
    client: httpx.AsyncClient,
    endpoint_base: str,
    api_key: str,
    model_id: str,
    probe: dict,
    timeout_s: int = 30,
    extra: dict | None = None,
) -> ProbeResult:
    """Execute a single probe against an endpoint. NEVER raises.

    extra: optional request-payload overrides (e.g. {"logprobs": true,
    "top_logprobs": 5, "temperature": 0} for the V5 logprob tail).
    """
    t0 = time.monotonic()

    # Normalize endpoint URL
    base = endpoint_base.rstrip("/")
    if not base.endswith("/v1"):
        url = f"{base}/v1/chat/completions"
    else:
        url = f"{base}/chat/completions"

    try:
        payload = {
            "model": model_id,
            "messages": [{"role": "user", "content": probe["prompt"]}],
            "max_tokens": probe["max_tokens"],
            "stream": False,
        }
        if extra:
            payload.update(extra)
        resp = await client.post(
            url,
            json=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=timeout_s,
        )
        latency_ms = int((time.monotonic() - t0) * 1000)

        if resp.status_code != 200:
            return ProbeResult(
                probe_id=probe["id"],
                tier=0,
                status_code=resp.status_code,
                content="",
                thinking="",
                latency_ms=latency_ms,
                error=f"HTTP {resp.status_code}",
            )

        data = resp.json()
        msg = data.get("choices", [{}])[0].get("message", {})
        content = msg.get("content", "") or ""
        thinking = msg.get("reasoning_content", "") or ""
        logprobs = data.get("choices", [{}])[0].get("logprobs")
        finish_reason = data.get("choices", [{}])[0].get("finish_reason") or ""

        return ProbeResult(
            probe_id=probe["id"],
            tier=0,
            status_code=200,
            content=content,
            thinking=thinking,
            latency_ms=latency_ms,
            logprobs=logprobs,
            finish_reason=finish_reason,
        )
    except Exception as exc:
        return ProbeResult(
            probe_id=probe["id"],
            tier=0,
            status_code=0,
            content="",
            thinking="",
            latency_ms=int((time.monotonic() - t0) * 1000),
            error=str(exc)[:200],
        )


# ── V6a/V6b/V7 cascade probe lanes ───────────────────────────────────────────
# All ROUTER-ONLY: every call goes through run_probe / run_stream_probe with
# the router endpoint key passed in. NO provider keys, NO upstream resolution,
# NO direct upstream calls — the deleted _resolve_upstream /
# _capture_upstream_logprobs pattern must NOT reappear here.

# V6a BOUNDARY SIGNATURE — stop-sequence/EOS + strict JSON-schema compliance.
BOUNDARY_PROBES = [
    {
        "id": "boundary_stop",
        "prompt": "Count from 1 to 20, one number per line.",
        "max_tokens": 120,
    },
    {
        "id": "boundary_json",
        "prompt": (
            "Return a STRICT JSON object with EXACTLY this schema and no other "
            'fields: {"name": string, "count": integer, "tags": array of '
            "exactly 3 strings}. The tags array MUST contain exactly 3 "
            "strings. No prose, no markdown fences, no preamble."
        ),
        "max_tokens": 200,
    },
]

# V6b STABILITY HASH — temp=0 identical-probe repetition (3 samples).
STABILITY_PROBE = {
    "id": "stability_poem_sea",
    "prompt": "Write a 4-line poem about the sea.",
    "max_tokens": 150,
}

# V7 TIMING PROFILE — streamed probe: TTFT / chunk-count / chunk-size.
TIMING_PROBE = {
    "id": "timing_photosynthesis",
    "prompt": "Explain photosynthesis in two sentences.",
    "max_tokens": 120,
}


def _extract_json_object(text: str) -> str:
    """Pull the first {...} object out of text (handles markdown fences)."""
    t = (text or "").strip()
    if t.startswith("```"):
        first_nl = t.find("\n")
        if first_nl != -1:
            t = t[first_nl + 1:]
        if t.endswith("```"):
            t = t[:-3].rstrip()
    start = t.find("{")
    end = t.rfind("}")
    if start != -1 and end != -1 and end > start:
        return t[start:end + 1]
    return t


def compute_boundary_signature(stop_result, json_result,
                               json_mode: str = "supported") -> dict:
    """V6a: build boundary_signature from the two boundary probe results.
    Fail-open: missing/error results → False/empty fields."""
    sig = {
        "stop_strict": False, "stop_echo": False, "finish_reason": "",
        "json_strict": False, "json_mode": json_mode,
    }
    if stop_result is not None and getattr(stop_result, "status_code", 0) == 200:
        content = stop_result.content or ""
        sig["finish_reason"] = stop_result.finish_reason or ""
        lines = [ln.strip() for ln in content.splitlines() if ln.strip()]
        # strict stop: counted to 6 and stopped before 7 (stop=["7"])
        sig["stop_strict"] = bool(lines) and lines[-1] == "6"
        # echo: the stop token "7" leaked as a standalone numbered line
        sig["stop_echo"] = "7" in lines
    if json_result is not None and getattr(json_result, "status_code", 0) == 200:
        try:
            obj = json.loads(_extract_json_object(json_result.content or ""))
        except Exception:
            obj = None
        if isinstance(obj, dict):
            tags = obj.get("tags")
            sig["json_strict"] = (
                set(obj.keys()) == {"name", "count", "tags"}
                and isinstance(obj.get("name"), str)
                and isinstance(obj.get("count"), int)
                and not isinstance(obj.get("count"), bool)
                and isinstance(tags, list)
                and len(tags) == 3
                and all(isinstance(t, str) for t in tags)
            )
    return sig


async def run_boundary_probes(
    client: httpx.AsyncClient,
    endpoint_base: str,
    api_key: str,
    model_id: str,
    timeout_s: int = 30,
) -> dict:
    """Run both V6a boundary probes via the router endpoint and return the
    boundary_signature dict. Fail-open: never raises. The JSON probe tries
    response_format first; on 400/422 it retries without it and records
    json_mode='unsupported'."""
    stop_probe, json_probe = BOUNDARY_PROBES[0], BOUNDARY_PROBES[1]
    stop_r = await run_probe(client, endpoint_base, api_key, model_id,
                             stop_probe, timeout_s,
                             extra={"stop": ["7"], "temperature": 0})
    json_r = await run_probe(client, endpoint_base, api_key, model_id,
                             json_probe, timeout_s,
                             extra={"temperature": 0,
                                    "response_format": {"type": "json_object"}})
    json_mode = "supported"
    if json_r.status_code in (400, 422):
        json_mode = "unsupported"
        json_r = await run_probe(client, endpoint_base, api_key, model_id,
                                 json_probe, timeout_s,
                                 extra={"temperature": 0})
    elif json_r.status_code != 200:
        json_mode = "error"
    return compute_boundary_signature(stop_r, json_r, json_mode)


async def run_stability_probe(
    client: httpx.AsyncClient,
    endpoint_base: str,
    api_key: str,
    model_id: str,
    timeout_s: int = 30,
) -> dict:
    """V6b: 3 temp=0 samples via asyncio.gather (single wall-clock cost);
    sha256 of whitespace-normalized content per sample. Returns the
    stability_profile dict. Never raises."""
    results = await asyncio.gather(*[
        run_probe(client, endpoint_base, api_key, model_id, STABILITY_PROBE,
                  timeout_s, extra={"temperature": 0})
        for _ in range(3)
    ])
    hashes = []
    for r in results:
        if getattr(r, "status_code", 0) == 200 and r.content:
            norm = "".join(r.content.split())
            hashes.append(hashlib.sha256(norm.encode("utf-8")).hexdigest())
        else:
            hashes.append("")
    valid = [h for h in hashes if h]
    distinct = len(set(valid))
    mode_hash = Counter(valid).most_common(1)[0][0] if valid else ""
    return {
        "probe_id": "stability_poem_sea",
        "samples": 3,
        "distinct_hashes": distinct,
        "hashes": hashes,
        "mode_hash": mode_hash,
    }


async def run_stream_probe(
    client: httpx.AsyncClient,
    endpoint_base: str,
    api_key: str,
    model_id: str,
    probe: dict,
    timeout_s: int = 30,
    extra: dict | None = None,
) -> ProbeResult:
    """V7: streamed probe. Measures TTFT (ms to first content chunk), chunk
    count carrying non-empty delta content, mean chunk chars, total elapsed ms.
    NEVER raises; failure returns an error ProbeResult (timing_profile None)."""
    t0 = time.monotonic()
    base = endpoint_base.rstrip("/")
    if not base.endswith("/v1"):
        url = f"{base}/v1/chat/completions"
    else:
        url = f"{base}/chat/completions"
    payload = {
        "model": model_id,
        "messages": [{"role": "user", "content": probe["prompt"]}],
        "max_tokens": probe["max_tokens"],
        "stream": True,
        "temperature": 0,
    }
    if extra:
        payload.update(extra)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    ttft_ms = 0
    chunk_count = 0
    total_chars = 0
    try:
        async with client.stream("POST", url, json=payload, headers=headers,
                                 timeout=timeout_s) as resp:
            if resp.status_code != 200:
                return ProbeResult(
                    probe_id=probe["id"], tier=0,
                    status_code=resp.status_code, content="", thinking="",
                    latency_ms=int((time.monotonic() - t0) * 1000),
                    error=f"HTTP {resp.status_code}",
                )
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data_str = line[len("data:"):].strip()
                if data_str == "[DONE]":
                    break
                try:
                    obj = json.loads(data_str)
                except Exception:
                    continue
                choices = obj.get("choices") or [{}]
                delta = choices[0].get("delta", {}) or {}
                content = delta.get("content") or ""
                if content:
                    if ttft_ms == 0:
                        ttft_ms = int((time.monotonic() - t0) * 1000)
                    chunk_count += 1
                    total_chars += len(content)
        total_ms = int((time.monotonic() - t0) * 1000)
        mean_chunk_chars = round(total_chars / chunk_count, 2) if chunk_count else 0.0
        return ProbeResult(
            probe_id=probe["id"], tier=0, status_code=200,
            content="", thinking="", latency_ms=total_ms,
            timing_profile={
                "ttft_ms": ttft_ms, "chunk_count": chunk_count,
                "mean_chunk_chars": mean_chunk_chars, "total_ms": total_ms,
            },
        )
    except Exception as exc:
        return ProbeResult(
            probe_id=probe["id"], tier=0, status_code=0, content="",
            thinking="", latency_ms=int((time.monotonic() - t0) * 1000),
            error=str(exc)[:200],
        )


async def run_timing_probe(
    client: httpx.AsyncClient,
    endpoint_base: str,
    api_key: str,
    model_id: str,
    timeout_s: int = 30,
) -> dict:
    """V7: run TIMING_PROBE streamed; return the timing_profile dict
    (empty dict on any failure — advisory-only lane)."""
    r = await run_stream_probe(client, endpoint_base, api_key, model_id,
                               TIMING_PROBE, timeout_s)
    return dict(r.timing_profile) if getattr(r, "timing_profile", None) else {}
