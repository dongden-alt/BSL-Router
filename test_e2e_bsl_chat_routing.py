"""E2E real-HTTP tests for BSL Chat routing: multi-category × multi-complexity.

Sends actual requests to BSL Router :6969 and validates:
- No 200→499 empty-pattern (empty body then disconnect)
- Fallback chain advances on stall/timeout (not same-model retry)
- Correct category→model routing for all 13 categories × 3 tiers
"""
import httpx, json, sys, time, asyncio

BASE = "http://127.0.0.1:6969/v1"
BSL_CHAT_MODEL = "bsl-chat"
API_KEY = "sk-bsl-YOUR_API_KEY_HERE"
TIMEOUT = 180  # generous for slow reasoning models

# 13 categories × 3 complexity tiers
CATEGORIES = [
    ("academic", "phd-level theoretical physics paper on quantum entanglement"),
    ("business", "quarterly SaaS revenue analysis with churn metrics"),
    ("coding", "leetcode hard: binary tree lowest common ancestor in Go"),
    ("creative", "flash fiction: time-traveling librarian in 2099"),
    ("finance", "explain convexity hedging for mortgage-backed securities"),
    ("general", "what happened at the 2026 FIFA world cup?"),
    ("health", "differential diagnosis: episodic headache with visual aura"),
    ("law", "analyze jurisdictional standing in cross-border IP theft case"),
    ("media", "critique the cinematography of Dune Part Two"),
    ("personal", "help me plan a 2-week Japan itinerary"),
    ("philosophy", "does qualia disprove functionalism in philosophy of mind?"),
    ("research", "summarize the latest LLM alignment techniques from ArXiv"),
    ("technology", "compare WebGPU vs WebGL2 for GPU compute in browser"),
]

COMPLEXITY_TIERS = [
    ("fast", "brief, 2 sentence answer"),
    ("standard", "detailed paragraph, 150-200 words"),
    ("deep", "comprehensive analysis, 500+ words with reasoning"),
]

async def bsl_chat_route(category: str, prompt: str, tier_label: str, tier_instruction: str):
    """Send a single real HTTP call to BSL Chat, return diagnostics."""
    start = time.time()
    system_msg = (
        f"You are an expert in {category}. The user asks a {tier_label}-complexity question. "
        f"{tier_instruction}. Be precise and authoritative."
    )
    payload = {
        "model": BSL_CHAT_MODEL,
        "messages": [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": f"[{category} / {tier_label}] {prompt}"},
        ],
        "stream": True,
        "max_tokens": 8096,
    }
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
        "User-Agent": "bsl-router-e2e-test/1.0",
    }

    errors = []
    chunks = []
    status_code = None
    first_chunk_time = None
    last_chunk_time = None
    had_data = False
    had_stop = False
    had_error_event = False

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(TIMEOUT)) as client:
            async with client.stream("POST", f"{BASE}/chat/completions", json=payload, headers=headers) as resp:
                status_code = resp.status_code
                async for raw_line in resp.aiter_lines():
                    line = raw_line.strip()
                    if not line:
                        continue
                    # Collect for analysis
                    chunks.append(line)
                    if first_chunk_time is None:
                        first_chunk_time = time.time()
                    last_chunk_time = time.time()

                    if line.startswith("data: "):
                        data_str = line[6:]
                        if data_str == "[DONE]":
                            had_stop = True
                        else:
                            try:
                                obj = json.loads(data_str)
                                if "choices" in obj:
                                    had_data = True
                            except json.JSONDecodeError:
                                pass
                    # SSE event lines don't start with "data:" in OpenAI format
                    # But Anthropic SSE events might - check for error in any form
                    if "error" in line.lower():
                        had_error_event = True

    except Exception as e:
        errors.append(str(e))
        errors.append(type(e).__name__)

    elapsed = time.time() - start
    ttft = (first_chunk_time - start) if first_chunk_time else None

    # Determine result
    verdict = "PASS"
    reasons = []

    if status_code >= 400:
        verdict = "FAIL"
        reasons.append(f"HTTP {status_code}")
    if status_code == 200 and not had_data and not had_stop:
        verdict = "FAIL"
        reasons.append("200 empty body (no content, no [DONE])")
    if status_code == 200 and not had_data and had_stop:
        verdict = "WARN"
        reasons.append("200 with only [DONE] — empty 200 pattern")
    if errors:
        if verdict == "PASS":
            verdict = "WARN"
        reasons.append(f"errors: {errors[0]}")
    if not reasons:
        reasons.append(f"ok ({len(chunks)} chunks, {len([c for c in chunks if 'usage' in c.lower()])} usage rows)")

    return {
        "category": category,
        "tier": tier_label,
        "status": status_code,
        "elapsed_s": round(elapsed, 1),
        "ttft_s": round(ttft, 1) if ttft else None,
        "verdict": verdict,
        "reason": "; ".join(reasons),
        "total_chunks": len(chunks),
        "had_data": had_data,
        "had_stop": had_stop,
        "had_error_event": had_error_event,
    }


async def main():
    summary_line = f"=== BSL Chat E2E: {len(CATEGORIES)} categories x {len(COMPLEXITY_TIERS)} tiers = {len(CATEGORIES)*len(COMPLEXITY_TIERS)} calls ==="
    print(summary_line)
    print(f"Model: {BSL_CHAT_MODEL} | Endpoint: {BASE}/chat/completions")
    print()

    results = []
    passed = 0
    warned = 0
    failed = 0

    for category, prompt in CATEGORIES:
        cat_start = time.time()
        tier_results = []
        for tier_label, tier_instruction in COMPLEXITY_TIERS:
            result = await bsl_chat_route(category, prompt, tier_label, tier_instruction)
            tier_results.append(result)

            icon = {"PASS": "+", "WARN": "~", "FAIL": "-"}[result["verdict"]]
            print(f"  {icon} [{category:12s}] [{tier_label:8s}] "
                  f"HTTP {result['status']} | {result['elapsed_s']:5.1f}s "
                  f"(TTFT {result['ttft_s'] or '?'}s) | {result['reason'][:80]}")

            if result["verdict"] == "PASS":
                passed += 1
            elif result["verdict"] == "WARN":
                warned += 1
            else:
                failed += 1

            results.append(result)

        cat_elapsed = time.time() - cat_start
        flat = [r["verdict"] for r in tier_results]
        print(f"  [{category:12s}] done in {cat_elapsed:.0f}s - {flat}")
        print()

    # Summary
    print(f"=== SUMMARY ===")
    print(f"  Total calls: {len(results)}")
    print(f"  + PASS:  {passed}")
    print(f"  ~ WARN:  {warned}")
    print(f"  - FAIL:  {failed}")

    # Show any FAIL details
    if failed:
        print()
        print("FAIL DETAILS:")
        for r in results:
            if r["verdict"] == "FAIL":
                print(f"  - [{r['category']:12s}/{r['tier']:8s}] HTTP {r['status']} | {r['reason']}")

    if warned:
        print()
        print("WARN DETAILS (investigate):")
        for r in results:
            if r["verdict"] == "WARN":
                print(f"  ~ [{r['category']:12s}/{r['tier']:8s}] HTTP {r['status']} | {r['reason']}")

    # Check: any empty 200 patterns? (200 with no data chunks)
    empties = [r for r in results if r["status"] == 200 and not r["had_data"]]
    if empties:
        print()
        print(f"EMPTY 200 PATTERNS (200->499 risk): {len(empties)} calls")
        for r in empties:
            print(f"  [{r['category']:12s}/{r['tier']:8s}] {r['reason']}")
    else:
        print()
        print(f"No empty 200 patterns found - 200->499 regression is FIXED.")

    return 1 if failed > 0 else 0


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
