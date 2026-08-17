"""
Vision candidate direct probe -- tests each vision combo candidate
against a real image to verify they can actually read images.

Generates a simple test image with distinct shapes + text, encodes it
as base64 data URL, then sends it directly to each provider's upstream
endpoint using the same OpenAI multimodal format the vision scout uses.

Usage:
    python -m app.tests.test_vision_live_probe
"""

import base64
import io
import json
import time
import sys
import httpx
import yaml

# --- Test image generation (no external file needed) ---
def _make_test_image_b64() -> str:
    """Generate a simple test image: red circle + blue square + text."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        print("Pillow not available -- using a pre-encoded tiny PNG fallback.")
        # 1x1 red pixel PNG
        return "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8/5+hHgAHggJ/PchI7wAAAABJRU5ErkJggg=="

    img = Image.new("RGB", (400, 200), "white")
    draw = ImageDraw.Draw(img)
    # Red circle
    draw.ellipse([20, 50, 120, 150], fill="red", outline="darkred", width=2)
    # Blue square
    draw.rectangle([160, 50, 260, 150], fill="blue", outline="darkblue", width=2)
    # Text
    try:
        draw.text((20, 10), "BSL VISION TEST 2026", fill="black")
        draw.text((20, 170), "Red circle + Blue square", fill="gray")
    except Exception:
        pass  # Font issues -- shapes are the real test

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _load_config() -> dict:
    with open("config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _decrypt_api_key(encrypted: str) -> str:
    """Decrypt an enc: prefixed API key using the router's own crypto."""
    if not encrypted.startswith("enc:"):
        return encrypted
    from app.crypto import decrypt_value
    return decrypt_value(encrypted)


def _get_provider_connection(config: dict, provider_name: str) -> tuple[str, str]:
    """Get (base_url, api_key) for a provider's first enabled connection."""
    prov = config.get("providers", {}).get(provider_name)
    if not prov:
        return "", ""
    conns = prov.get("connections", [])
    for conn in conns:
        if conn.get("enabled", True) and conn.get("base_url"):
            key = _decrypt_api_key(conn.get("api_key", ""))
            return conn["base_url"], key
    return "", ""


def _get_provider_format(config: dict, provider_name: str) -> str:
    prov = config.get("providers", {}).get(provider_name)
    if not prov:
        return "unknown"
    return str(prov.get("format", "openai")).lower()


def _build_openai_vision_payload(image_b64: str, model: str) -> dict:
    data_url = f"data:image/png;base64,{image_b64}"
    return {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Describe this image. What shapes and colors do you see? What text is visible?"},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
        "max_tokens": 512,
        "stream": False,
    }


def _build_anthropic_vision_payload(image_b64: str, model: str) -> dict:
    return {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Describe this image. What shapes and colors do you see? What text is visible?"},
                    {"type": "image", "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": image_b64,
                    }},
                ],
            }
        ],
        "max_tokens": 512,
        "stream": False,
    }


def _probe_candidate(
    provider_name: str,
    model: str,
    base_url: str,
    api_key: str,
    fmt: str,
    image_b64: str,
    timeout_s: float = 20.0,
) -> dict:
    """Probe one candidate. Returns {status, latency_ms, description, error}."""
    base_url = base_url.rstrip("/")
    label = f"{provider_name}/{model}"

    if fmt in ("openai", "openai-responses"):
        url = f"{base_url}/v1/chat/completions"
        payload = _build_openai_vision_payload(image_b64, model)
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
    elif fmt == "anthropic":
        url = f"{base_url}/v1/messages"
        payload = _build_anthropic_vision_payload(image_b64, model)
        headers = {
            "x-api-key": api_key,
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
        }
    else:
        return {"status": "skip", "latency_ms": 0, "description": "", "error": f"unsupported format: {fmt}"}

    sw = time.monotonic()
    try:
        with httpx.Client(timeout=timeout_s) as client:
            resp = client.post(url, json=payload, headers=headers)
        latency = int((time.monotonic() - sw) * 1000)

        if resp.status_code != 200:
            return {
                "status": "http_error",
                "latency_ms": latency,
                "description": "",
                "error": f"HTTP {resp.status_code}: {resp.text[:300]}",
            }

        data = resp.json()
        # OpenAI format
        desc = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        if not desc:
            # Anthropic format
            content_blocks = data.get("content", [])
            if isinstance(content_blocks, list):
                desc = " ".join(
                    block.get("text", "") for block in content_blocks
                    if isinstance(block, dict) and block.get("type") == "text"
                )

        if desc:
            return {"status": "ok", "latency_ms": latency, "description": desc[:500], "error": ""}
        return {"status": "empty", "latency_ms": latency, "description": "", "error": "No content in response"}

    except httpx.TimeoutException:
        latency = int((time.monotonic() - sw) * 1000)
        return {"status": "timeout", "latency_ms": latency, "description": "", "error": f"Timeout after {timeout_s}s"}
    except Exception as e:
        latency = int((time.monotonic() - sw) * 1000)
        return {"status": "error", "latency_ms": latency, "description": "", "error": f"{type(e).__name__}: {str(e)[:200]}"}


def main():
    print("=" * 70)
    print("BSL Router -- Vision Candidate Direct Probe")
    print("=" * 70)

    config = _load_config()
    image_b64 = _make_test_image_b64()
    print(f"\nTest image: {len(image_b64)} bytes base64 PNG")
    print(f"Image contains: red circle, blue square, text 'BSL VISION TEST 2026'\n")

    # Find Vision combo
    vision_combo = None
    for combo in config.get("combos", []):
        if combo.get("alias") == "Vision":
            vision_combo = combo
            break

    if not vision_combo:
        print("ERROR: No combo with alias 'Vision' found in config.yaml")
        sys.exit(1)

    chain = vision_combo.get("chain", [])
    print(f"Vision combo has {len(chain)} chain entries:\n")

    results = []
    for entry in chain:
        # Handle string format: "provider/model" (e.g. "ltn-ai/xiaomi/mimo-v2.5")
        if isinstance(entry, str):
            parts = entry.split("/", 1)
            if len(parts) == 2:
                prov_name, model_id = parts[0], parts[1]
            else:
                # No slash — treat whole string as model alias
                prov_name, model_id = "", entry
        else:
            prov_name = entry.get("provider")
            model_id = entry.get("model") or entry.get("id")
        base_url, api_key = _get_provider_connection(config, prov_name)
        fmt = _get_provider_format(config, prov_name)

        print("-" * 60)
        print(f"  Candidate: {prov_name}/{model_id}")
        print(f"  Format: {fmt}")
        print(f"  Base URL: {base_url}")
        print(f"  API Key: {'***' + api_key[-8:] if api_key else 'MISSING'}")

        if not base_url or not api_key:
            print(f"  FAIL SKIP: missing base_url or api_key")
            results.append({"candidate": f"{prov_name}/{model_id}", "status": "skip", "error": "missing connection"})
            continue

        print(f"  Probing...", end="", flush=True)
        result = _probe_candidate(prov_name, model_id, base_url, api_key, fmt, image_b64)
        result["candidate"] = f"{prov_name}/{model_id}"
        results.append(result)

        status_icon = {"ok": "OK", "http_error": "FAIL", "timeout": "TIMEOUT", "empty": "FAIL", "error": "FAIL", "skip": "--"}.get(result["status"], "?")
        print(f" {status_icon} {result['status'].upper()} ({result['latency_ms']}ms)")

        if result["description"]:
            print(f"  Description: {result['description'][:300]}")
        if result["error"]:
            print(f"  Error: {result['error'][:300]}")
        print()

    # Summary
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    for r in results:
        icon = {"ok": "OK", "http_error": "FAIL", "timeout": "TIMEOUT", "empty": "FAIL", "error": "FAIL", "skip": "SKIP"}.get(r["status"], "?")
        print(f"  {icon} {r['candidate']}: {r['status']} ({r.get('latency_ms', 0)}ms)")
        if r.get("error"):
            print(f"     -> {r['error'][:150]}")
    print()

    ok_count = sum(1 for r in results if r["status"] == "ok")
    total = len(results)
    print(f"Result: {ok_count}/{total} candidates can read images")
    if ok_count == 0:
        print("WARN  NO vision candidates work -- vision bridge will always fail-open with placeholder.")
    elif ok_count < total:
        print(f"WARN  {total - ok_count} candidate(s) failed -- vision bridge will work but with reduced fallback.")
    else:
        print("OK All candidates work -- vision bridge is fully operational.")


if __name__ == "__main__":
    main()
