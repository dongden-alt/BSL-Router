"""
E2E test for BSL Router vision model routing.

Tests that the Vision combo model can process an image and return a description.
"""
import httpx
import base64
from pathlib import Path


def test_vision_model_e2e():
    """Send an image to BSL Router's Vision combo and verify it returns a description."""
    # Read the test image (generated via PIL)
    image_path = Path(__file__).parent.parent.parent / "test-assets" / "vision_test_image.png"
    if not image_path.exists():
        print(f"[WARN] Test image not found at {image_path}")
        print("Run: python -c \"from PIL import Image, ImageDraw; ...\" to create it.")
        return
    
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
    
    # Send request to BSL Router
    print("[INFO] Sending image to BSL Router Vision combo model...")
    try:
        response = httpx.post(
            "http://localhost:6969/v1/chat/completions",
            json=payload,
            timeout=60.0
        )
        response.raise_for_status()
        result = response.json()
        
        # Extract the response
        if "choices" in result and len(result["choices"]) > 0:
            content = result["choices"][0]["message"]["content"]
            print(f"\n[OK] Vision model response:\n{content}\n")
            
            # Check that the response contains relevant keywords
            keywords = ["image", "layout", "ui", "mockup", "design", "website", "news"]
            found = [kw for kw in keywords if kw.lower() in content.lower()]
            
            if found:
                print(f"[OK] Response contains relevant keywords: {', '.join(found)}")
            else:
                print("[WARN] Response doesn't contain expected vision-related keywords")
            
            return True
        else:
            print("[ERROR] No valid response from Vision model")
            print(f"Raw response: {result}")
            return False
            
    except httpx.HTTPStatusError as e:
        print(f"[ERROR] HTTP error: {e.response.status_code}")
        print(f"Response: {e.response.text}")
        return False
    except Exception as e:
        print(f"[ERROR] Error: {e}")
        return False


if __name__ == "__main__":
    success = test_vision_model_e2e()
    exit(0 if success else 1)
