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
    logprobs: dict = None      # choices[0].logprobs when requested (V5 tail)


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

        return ProbeResult(
            probe_id=probe["id"],
            tier=0,
            status_code=200,
            content=content,
            thinking=thinking,
            latency_ms=latency_ms,
            logprobs=logprobs,
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
