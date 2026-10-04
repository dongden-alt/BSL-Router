"""Probe definitions and execution."""
import time

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


async def run_probe(
    client: httpx.AsyncClient,
    endpoint_base: str,
    api_key: str,
    model_id: str,
    probe: dict,
    timeout_s: int = 30,
) -> ProbeResult:
    """Execute a single probe against an endpoint. NEVER raises."""
    t0 = time.monotonic()

    # Normalize endpoint URL
    base = endpoint_base.rstrip("/")
    if not base.endswith("/v1"):
        url = f"{base}/v1/chat/completions"
    else:
        url = f"{base}/chat/completions"

    try:
        resp = await client.post(
            url,
            json={
                "model": model_id,
                "messages": [{"role": "user", "content": probe["prompt"]}],
                "max_tokens": probe["max_tokens"],
                "stream": False,
            },
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

        return ProbeResult(
            probe_id=probe["id"],
            tier=0,
            status_code=200,
            content=content,
            thinking=thinking,
            latency_ms=latency_ms,
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
