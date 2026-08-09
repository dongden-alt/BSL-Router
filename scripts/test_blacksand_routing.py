#!/usr/bin/env python3
"""
Blacksand Models Live Routing Test
===================================
Sends a minimal chat-completion request to each of the 5 Blacksand models
through the running BSL Router server and reports:

  - HTTP status code
  - Response latency (ms)
  - Resolved upstream provider + model (from response headers / body)
  - First 200 chars of response content
  - Pass / Fail verdict

Usage:
    .venv\Scripts\python scripts\test_blacksand_routing.py [--url URL] [--key KEY]

Defaults:
    URL: http://localhost:6969
    KEY: (none - BSL Router doesn't require auth for local requests)
"""

import argparse
import json
import sys
import time
import traceback
from datetime import datetime

try:
    import httpx
except ImportError:
    print("ERROR: httpx not installed. Run: pip install httpx")
    sys.exit(1)

# ─── Config ───────────────────────────────────────────────────────────────────

BLACKSAND_MODELS = [
    {
        "id": "blacksand-chat",
        "name": "Blacksand Chat",
        "desc": "Category-aware smart routing (13x3 matrix)",
        "prompt": "Reply with exactly: OK",
        "max_tokens": 16,
        "test_type": "simple",
    },
    {
        "id": "blacksand-lite",
        "name": "Blacksand Lite",
        "desc": "Coding-agent single-task router (10x3 matrix)",
        "prompt": "Reply with exactly: OK",
        "max_tokens": 16,
        "test_type": "simple",
    },
    {
        "id": "blacksand-agentic",
        "name": "Blacksand Agentic",
        "desc": "Fast-tier agentic coding orchestration (Scout-first)",
        "prompt": (
            "You are a coding assistant. I need you to:\n"
            "1. Read this Python function: def add(a, b): return a + b\n"
            "2. Identify a potential bug if someone passes a string and an int\n"
            "3. Write a fix with type hints\n"
            "4. Write a unit test for the fixed function\n"
            "Output all 4 steps in order."
        ),
        "max_tokens": 1024,
        "test_type": "multi_step_coding",
    },
    {
        "id": "blacksand-agentic-ultra",
        "name": "Blacksand Agentic Ultra",
        "desc": "Balanced-tier coding orchestration with consult",
        "prompt": (
            "You are a senior code reviewer. I have this code:\n\n"
            "```python\n"
            "def process_items(items):\n"
            "    result = []\n"
            "    for i in range(len(items)):\n"
            "        if items[i] % 2 == 0:\n"
            "            result.append(items[i] * 2)\n"
            "    return result\n"
            "```\n\n"
            "Tasks:\n"
            "1. Identify 3 code quality issues\n"
            "2. Rewrite it using list comprehension\n"
            "3. Add error handling for non-integer items\n"
            "4. Write 2 test cases (one normal, one edge case)\n"
            "5. Explain whether the balanced-tier consult route would pick this as a coding task or a general query"
        ),
        "max_tokens": 2048,
        "test_type": "multi_step_coding",
    },
    {
        "id": "blacksand-agentic-max",
        "name": "Blacksand Agentic Max",
        "desc": "Multi-domain fusion (coding + chat) for Openclaw/Hermes",
        "prompt": (
            "I need help with two things:\n\n"
            "CODING: Write a Python decorator @retry(max_attempts=3) that retries a function\n"
            "on exception, with exponential backoff (1s, 2s, 4s). Include type hints.\n\n"
            "GENERAL: Then explain in 2-3 sentences when you would use this decorator\n"
            "in a production web scraper.\n\n"
            "This requires both coding and explanation - please handle both domains."
        ),
        "max_tokens": 2048,
        "test_type": "multi_step_coding",
    },
]

# ─── Helpers ──────────────────────────────────────────────────────────────────

def color(text, code):
    """ANSI color helper."""
    codes = {"green": 32, "red": 31, "yellow": 33, "cyan": 36, "gray": 90, "bold": 1}
    return f"\033[{codes.get(code, 0)}m{text}\033[0m"

def fmt_ms(ms):
    if ms < 1000:
        return f"{ms:.0f}ms"
    return f"{ms/1000:.1f}s"

def extract_content(resp_data):
    """Extract text content from OpenAI-format or Anthropic-format response."""
    if not isinstance(resp_data, dict):
        return str(resp_data)[:200]
    # OpenAI format
    if "choices" in resp_data and resp_data["choices"]:
        choice = resp_data["choices"][0]
        msg = choice.get("message", {})
        if msg.get("content"):
            return msg["content"][:200]
        # Streaming delta format
        if choice.get("delta", {}).get("content"):
            return choice["delta"]["content"][:200]
    # Anthropic format
    if "content" in resp_data and isinstance(resp_data["content"], list):
        for block in resp_data["content"]:
            if isinstance(block, dict) and block.get("type") == "text":
                return block.get("text", "")[:200]
    return json.dumps(resp_data, ensure_ascii=False)[:200]

def extract_routing_info(resp):
    """Extract upstream provider/model from response headers."""
    info = {}
    for h in resp.headers:
        hl = h.lower()
        if hl in ("x-bsl-provider", "x-bsl-upstream-provider"):
            info["provider"] = resp.headers[h]
        if hl in ("x-bsl-model", "x-bsl-upstream-model", "x-bsl-source-model"):
            info["model"] = resp.headers[h]
        if hl in ("x-bsl-route", "x-bsl-route-name"):
            info["route"] = resp.headers[h]
        if hl == "x-bsl-antigravity-alias":
            info["alias"] = resp.headers[h]
    return info

# ─── Main Test ────────────────────────────────────────────────────────────────

def test_model(base_url, api_key, model_info):
    """Send a chat completion to one Blacksand model. Returns result dict."""
    model_id = model_info["id"]
    test_type = model_info.get("test_type", "simple")
    result = {
        "model": model_id,
        "name": model_info["name"],
        "desc": model_info["desc"],
        "test_type": test_type,
        "status": None,
        "latency_ms": 0,
        "content": "",
        "content_len": 0,
        "routing": {},
        "error": None,
        "verdict": "",
    }

    payload = {
        "model": model_id,
        "messages": [{"role": "user", "content": model_info["prompt"]}],
        "max_tokens": model_info["max_tokens"],
        "temperature": 0,
        "stream": False,
    }

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    url = f"{base_url}/v1/chat/completions"
    timeout = 300.0 if test_type == "multi_step_coding" else 120.0

    start = time.perf_counter()
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, json=payload, headers=headers)
        elapsed = time.perf_counter() - start
        result["latency_ms"] = elapsed * 1000

        result["status"] = resp.status_code
        result["routing"] = extract_routing_info(resp)

        try:
            resp_data = resp.json()
        except Exception:
            resp_data = {"raw": resp.text[:500]}

        if resp.status_code in (200, 206):
            content = extract_content(resp_data)
            result["content"] = content
            result["content_len"] = len(content)

            if test_type == "multi_step_coding":
                # Validate that the response is substantial enough for a multi-step task.
                # A response that's too short likely means the model didn't engage
                # with the multi-step prompt or the routing collapsed to a trivial answer.
                if result["content_len"] < 100:
                    result["verdict"] = f"PARTIAL (too short: {result['content_len']} chars)"
                else:
                    result["verdict"] = "PASS"
            else:
                result["verdict"] = "PASS"
        else:
            result["content"] = json.dumps(resp_data, ensure_ascii=False)[:300]
            result["verdict"] = f"FAIL (HTTP {resp.status_code})"

    except httpx.ConnectError:
        result["latency_ms"] = (time.perf_counter() - start) * 1000
        result["verdict"] = "FAIL (Connection Refused)"
        result["error"] = f"Cannot connect to {base_url}. Is the server running?"
    except httpx.ReadTimeout:
        result["latency_ms"] = (time.perf_counter() - start) * 1000
        result["verdict"] = f"FAIL (Timeout {timeout:.0f}s)"
        result["error"] = f"Server did not respond within {timeout:.0f} seconds."
    except Exception as e:
        result["latency_ms"] = (time.perf_counter() - start) * 1000
        result["verdict"] = f"FAIL (Exception: {type(e).__name__})"
        result["error"] = str(e)
        traceback.print_exc()

    return result


def main():
    parser = argparse.ArgumentParser(description="Test all 5 Blacksand model routes")
    parser.add_argument("--url", default="http://localhost:6969", help="BSL Router base URL")
    parser.add_argument("--key", default="", help="API key (if required)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    print(f"\n{'=' * 62}")
    print(f"  Blacksand Models Live Routing Test")
    print(f"{'=' * 62}")
    print(f"  Server:  {args.url}")
    print(f"  Time:    {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Models:  {len(BLACKSAND_MODELS)}")
    print()

    # Pre-flight: check server is up
    try:
        with httpx.Client(timeout=5.0) as c:
            r = c.get(f"{args.url}/v1/models")
            if r.status_code != 200:
                print(f"  WARNING: Server responded with HTTP {r.status_code} on /v1/models")
            else:
                data = r.json()
                model_count = len(data.get("data", []))
                print(f"  OK - Server up - {model_count} models in catalog")
    except Exception as e:
        print(f"  FAIL - SERVER DOWN: {e}")
        print(f"  Start it with:  .venv\\Scripts\\python -m app.main")
        sys.exit(1)

    print()
    results = []

    for i, model_info in enumerate(BLACKSAND_MODELS, 1):
        model_id = model_info["id"]
        print(f"  [{i}/{len(BLACKSAND_MODELS)}] {model_id} - {model_info['desc']}")
        print(f"       Type: {model_info.get('test_type', 'simple')} | Sending request...", end=" ", flush=True)

        result = test_model(args.url, args.key, model_info)
        results.append(result)

        # Print result
        verdict = result.get("verdict", "UNKNOWN")
        if "PASS" in verdict:
            v_color = "green"
            icon = "OK"
        elif "PARTIAL" in verdict:
            v_color = "yellow"
            icon = "WARN"
        else:
            v_color = "red"
            icon = "FAIL"

        print(f"{icon} {verdict} ({fmt_ms(result['latency_ms'])})")

        if result.get("routing"):
            parts = []
            for k, v in result["routing"].items():
                parts.append(f"{k}={v}")
            print(f"       Route: {' | '.join(parts)}")

        if result.get("content"):
            content_preview = result['content'][:200]
            print(f"       Response ({result.get('content_len', 0)} chars): {content_preview}")

        if result.get("error"):
            print(f"       Error: {result['error']}")

        print()

    # ─── Summary ──────────────────────────────────────────────────────────────
    passed = sum(1 for r in results if "PASS" in r.get("verdict", ""))
    failed = len(results) - passed

    print(f"  {'=' * 60}")
    print(f"  Results: {passed} passed | {failed} failed | {len(results)} total")
    print()

    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False, default=str))
    else:
        # Table summary
        print(f"  {'Model':<30} {'Type':<18} {'Verdict':<25} {'Latency':<10} {'Chars':<7} {'Provider'}")
        print(f"  {'-'*30} {'-'*18} {'-'*25} {'-'*10} {'-'*7} {'-'*15}")
        for r in results:
            v = r.get("verdict", "?")
            tt = r.get("test_type", "?")
            lat = fmt_ms(r.get("latency_ms", 0))
            chars = r.get("content_len", 0)
            prov = r.get("routing", {}).get("provider", "-")
            print(f"  {r['model']:<30} {tt:<18} {v:<25} {lat:<10} {chars:<7} {prov}")

    print()
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
