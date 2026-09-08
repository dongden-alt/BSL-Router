"""ERE-R5: Gemini generateContent last-role guard (2026-09-06).

Gemini's generateContent API requires the LAST content role to be 'user' or a
pending functionResponse context — an OpenAI-style body ending in an assistant
turn is rejected upstream. `ensure_gemini_last_role` appends a minimal user
continuation turn ONLY when the trailing role would be invalid, merging a
trailing assistant text turn into the appended continuation when safe. Valid
bodies pass through byte-identical. Pure function; never raises.
"""
from typing import Any


def ensure_gemini_last_role(contents: Any) -> Any:
    try:
        if not isinstance(contents, list) or not contents:
            return contents
        last = contents[-1]
        if not isinstance(last, dict):
            return contents
        role = str(last.get("role") or "").lower()
        if role not in ("assistant", "model"):
            return contents  # user/functionResponse/tool endings are valid
        parts = last.get("parts")
        text = ""
        if isinstance(parts, list):
            for p in parts:
                if isinstance(p, dict) and isinstance(p.get("text"), str):
                    text += p["text"]
        merged = {"role": "user", "parts": [{"text": "(continue)" + ("\n\n" + text if text else "")}]}
        return contents[:-1] + [merged]
    except Exception:
        return contents
