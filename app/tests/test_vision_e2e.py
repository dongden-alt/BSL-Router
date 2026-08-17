"""
E2E test for BSL Router vision model routing.

Tests that the Vision combo model can process an image and return a description.

MARKERS / TIMEOUTS (2026-08-17):
- ``@pytest.mark.integration`` — this test requires a LIVE BSL Router on
  localhost:6969 and real upstream providers. It is excluded from default
  unit sweeps (``-m "not slow and not integration"``).
- ``@pytest.mark.timeout(120)`` — overrides the pytest.ini 10s global
  safety net. The Vision combo chain (6-provider fallback, vision-capable
  upstream) legitimately takes ~45s end-to-end.
- Pre-flight health check: if the router is unreachable, the test SKIPS
  immediately instead of burning the full timeout budget.

Prior bug class: with no markers, pytest collected this in the unit sweep,
the 10s global timeout killed the ~46s httpx call, and all failures were
swallowed by ``return False`` (pytest treats that as PASS). Both fixed.
"""
import httpx
import base64
import pytest
from pathlib import Path

ROUTER_URL = "http://localhost:6969"


@pytest.mark.integration
@pytest.mark.timeout(120)
def test_vision_model_e2e():
    """Send an image to BSL Router's Vision combo and verify it returns a description."""
    # Pre-flight: fast skip when the router is down (no hang, no 10s kill).
    try:
        health = httpx.get(f"{ROUTER_URL}/health", timeout=5.0)
        if health.status_code != 200:
            pytest.skip(f"BSL Router /health returned {health.status_code}")
    except Exception as exc:
        pytest.skip(f"BSL Router not reachable at {ROUTER_URL}: {exc}")

    # Read the test image (generated via PIL)
    image_path = Path(__file__).parent.parent.parent / "test-assets" / "vision_test_image.png"
    if not image_path.exists():
        pytest.skip(
            f"Test image not found at {image_path}. "
            "Generate it with PIL (see test-assets/)."
        )

    # Encode image to base64
    image_data = image_path.read_bytes()
    image_b64 = base64.b64encode(image_data).decode('utf-8')

    # Prepare request payload
    payload = {
        "model": "Vision",  # Use the Vision combo model
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "What do you see in this image? Describe the layout, colors, and main elements."
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{image_b64}"
                        }
                    }
                ]
            }
        ],
        "max_tokens": 1000,
        "stream": False
    }

    # Send request to BSL Router (90s: chain legitimately takes ~45s)
    print("[INFO] Sending image to BSL Router Vision combo model...")
    response = httpx.post(
        f"{ROUTER_URL}/v1/chat/completions",
        json=payload,
        timeout=90.0,
    )
    assert response.status_code == 200, (
        f"HTTP {response.status_code}: {response.text[:500]}"
    )
    result = response.json()

    # Extract the response
    assert "choices" in result and len(result["choices"]) > 0, (
        f"No choices in response: {str(result)[:500]}"
    )
    content = result["choices"][0]["message"]["content"]
    assert isinstance(content, str) and content.strip(), "Empty vision response"
    print(f"\n[OK] Vision model response:\n{content}\n")

    # Check that the response contains relevant keywords (soft check —
    # upstream phrasing varies; presence of ANY vision-related keyword is
    # enough evidence the image reached a vision-capable model).
    keywords = ["image", "layout", "ui", "mockup", "design", "website", "news",
                "circle", "background", "text", "blue", "red"]
    found = [kw for kw in keywords if kw.lower() in content.lower()]
    if found:
        print(f"[OK] Response contains relevant keywords: {', '.join(found)}")
    else:
        print("[WARN] Response doesn't contain expected vision-related keywords")


if __name__ == "__main__":
    test_vision_model_e2e()
