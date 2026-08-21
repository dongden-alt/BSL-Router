"""
AgentRouter provider policy — VN content preflight.

Live probe 2026-08-20 (direct agentrouter.org /v1/messages):
  EN tip only                         -> 200
  EN tip + Gemini-style system        -> 200
  EN tip + Vietnamese file text       -> 400 content-blocked (0.6s)
  EN tip + VN diacritics              -> 400 content-blocked
  EN tip + Chinese snippet            -> 200
  EN tip + tools schema               -> 200

AR filters the *request body*, not the tip language. Soft English system
prompts cannot clear content-blocked when VN is still in messages/context.

Policy (agentrouter ONLY):
  Detect Vietnamese Latin text (diacritics or strong VN phrase signals).
  If present: do NOT call upstream. Combo chains advance to next leaf;
  direct agentrouter calls get a clear 400 explaining the skip.
"""
from __future__ import annotations

import re
from typing import Any, List, Sequence, Tuple

# Vietnamese-specific Latin letters (covers common NFC forms in source).
_VN_DIACRITIC_RE = re.compile(
    r"[àáảãạăằắẳẵặâầấẩẫậèéẻẽẹêềếểễệìíỉĩịòóỏõọôồốổỗộơờớởỡợ"
    r"ùúủũụưừứửữựỳýỷỹỵđ"
    r"ÀÁẢÃẠĂẰẮẲẴẶÂẦẤẨẪẬÈÉẺẼẸÊỀẾỂỄỆÌÍỈĨỊÒÓỎÕỌÔỒỐỔỖỘƠỜỚỞỠỢ"
    r"ÙÚỦŨỤƯỪỨỬỮỰỲÝỶỸỴĐ]"
)

# Accent-stripped multi-word phrases that almost never appear as English.
# Live P3 blocked body: "He thong quan tri bien tap tin tuc bong da..."
_VN_PHRASES = (
    "he thong",
    "quan tri",
    "bien tap",
    "tin tuc",
    "bong da",
    "tu dong",
    "quy trinh",
    "duyet bai",
    "xuat ban",
    "thanh phan",
    "chien thuat",
    "kiem dinh",
    "su kien",
    "du an",
    "ung dung",
    "nguoi dung",
    "tep tin",
    "khong duoc",
    "cac thanh",
    "phan tich",
)

_PROVIDER = "agentrouter"


def _extract_text_blobs(obj: Any, out: List[str], depth: int = 0) -> None:
    if depth > 12 or obj is None:
        return
    if isinstance(obj, str):
        if obj:
            out.append(obj)
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            kl = str(k).lower()
            if kl in {"id", "type", "role", "model", "name", "mime_type", "media_type"}:
                if isinstance(v, str) and len(v) < 64:
                    continue
            _extract_text_blobs(v, out, depth + 1)
        return
    if isinstance(obj, (list, tuple)):
        for item in obj:
            _extract_text_blobs(item, out, depth + 1)
        return
    content = getattr(obj, "content", None)
    if content is not None:
        _extract_text_blobs(content, out, depth + 1)
    text = getattr(obj, "text", None)
    if isinstance(text, str) and text:
        out.append(text)


def _message_text(messages: Sequence[Any]) -> str:
    blobs: List[str] = []
    _extract_text_blobs(list(messages or []), blobs)
    return "\n".join(blobs)


def detect_vietnamese_content(text: str) -> Tuple[bool, str]:
    """Return (hit, reason). reason is empty when no hit."""
    if not text:
        return False, ""
    m = _VN_DIACRITIC_RE.search(text)
    if m:
        return True, f"vietnamese_diacritic:{m.group(0)}"

    tokens = re.findall(r"[A-Za-z]+", text.lower())
    joined = " ".join(tokens)
    for phrase in _VN_PHRASES:
        if phrase in joined:
            return True, f"vietnamese_phrase:{phrase}"
    return False, ""


def agentrouter_should_skip_for_vietnamese(
    provider_name: str,
    messages: Sequence[Any],
) -> Tuple[bool, str]:
    """True when provider is agentrouter and outbound text looks Vietnamese."""
    if str(provider_name or "").lower() != _PROVIDER:
        return False, ""
    text = _message_text(messages)
    return detect_vietnamese_content(text)


def format_agentrouter_vn_skip_error(reason: str) -> dict:
    return {
        "error": {
            "message": (
                "AgentRouter skipped: request body contains Vietnamese text "
                f"({reason}). AgentRouter returns 400 content-blocked on VN "
                "context even when the user tip is English. Route this request "
                "through another provider, or remove VN file/history context."
            ),
            "type": "agentrouter_vietnamese_content_blocked",
            "code": "content-blocked-preflight",
            "provider": "agentrouter",
            "reason": reason,
        }
    }
