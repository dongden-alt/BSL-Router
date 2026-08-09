"""
Vision model test using a LOCAL image encoded as base64 data URL.
Bypasses the 403/download issue where upstream providers can't fetch public URLs.
"""

import httpx
import json
import sys
import time
import os
import base64
import pathlib

os.environ["PYTHONIOENCODING"] = "utf-8"
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.stderr.reconfigure(encoding='utf-8', errors='replace')

BASE_URL = "http://localhost:6969"
API_KEY = "sk-bsl-YOUR_API_KEY_HERE"

# Load local image and convert to base64 data URL
IMAGE_PATH = pathlib.Path(__file__).parent / "fixtures" / "vision_test.png"
RAW_BYTES = IMAGE_PATH.read_bytes()
B64 = base64.b64encode(RAW_BYTES).decode()
DATA_URL = f"data:image/png;base64,{B64}"

print(f"Loaded image: {IMAGE_PATH} ({len(RAW_BYTES)} bytes, {len(B64)} base64 chars)")


def test_model(model_name: str, label: str, timeout: float = 120.0):
    print(f"\n{'='*60}")
    print(f"TEST: {label}")
    print(f"Model: {model_name}")
    print(f"{'='*60}")

    payload = {
        "model": model_name,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "What do you see in this image? Describe it briefly."},
                    {"type": "image_url", "image_url": {"url": DATA_URL}},
                ],
            }
        ],
        "max_tokens": 512,
        "temperature": 0.3,
        "stream": False,
    }

    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }

    start = time.time()
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(
                f"{BASE_URL}/v1/chat/completions",
                json=payload,
                headers=headers,
            )
        elapsed = time.time() - start

        print(f"Status: {resp.status_code} ({elapsed:.1f}s)")

        if resp.status_code != 200:
            print(f"ERROR: {resp.text[:1000]}")
            return False

        data = resp.json()
        choices = data.get("choices", [])
        if not choices:
            print("ERROR: No choices in response")
            print(json.dumps(data, indent=2)[:1000])
            return False

        content = choices[0].get("message", {}).get("content", "")
        print(f"\n--- MODEL RESPONSE ---")
        print(content[:1500] if content else "(empty)")
        print(f"--- END ---")

        if not content or len(content) < 20:
            print("[WARN] Response too short")
            return False

        lower = content.lower()
        if "i cannot see" in lower or "i can't see" in lower or "unable to see" in lower:
            print("[WARN] Model claims it cannot see the image")
            return False

        print("[PASS] Model appears to have read the image")
        return True

    except httpx.TimeoutException:
        print(f"[FAIL] TIMEOUT after {time.time() - start:.1f}s")
        return False
    except Exception as e:
        print(f"[FAIL] EXCEPTION: {type(e).__name__}: {e}")
        return False


if __name__ == "__main__":
    print(f"\nBSL Router Vision Test (base64 data URL)")
    print(f"Endpoint: {BASE_URL}")
    print(f"Time: {time.strftime('%Y-%m-%d %H:%M:%S')}")

    results = []
    results.append(("Vision combo", test_model("Vision", "Vision combo chain", timeout=120.0)))
    results.append(("Kimi-K2.6", test_model("Kimi-K2.6", "Kimi K2.6 direct", timeout=120.0)))
    results.append(("Qwen-3.7-Plus", test_model("Qwen-3.7-Plus", "Qwen 3.7 Plus direct", timeout=120.0)))

    print(f"\n\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    for name, passed in results:
        print(f"  {name}: {'[PASS]' if passed else '[FAIL]'}")

    overall = all(r[1] for r in results)
    print(f"\nOverall: {'ALL PASS' if overall else 'SOME FAILED'}")
    sys.exit(0 if overall else 1)
