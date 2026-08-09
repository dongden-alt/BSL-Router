"""
Middleware.request_intent — single source of truth for "what is the user
actually asking right now".

Both the category classifier and the task-complexity estimator must measure the
USER'S CURRENT REQUEST, not the serialized prompt envelope. Real clients
(Chatbot, Claude Code, Antigravity) inject large scaffolding into the message
stream:

  * role=system agent instructions ("You are a coding assistant...")
  * role=user injection pairs ("[SYSTEM INSTRUCTIONS — FOLLOW PRECISELY]\n...")
  * bracketed directive blocks ([DEEP PERSONA: ...], [CONTEXT], [REGISTER LOCK])
  * angle-tag blocks (<context>...</context>)
  * fenced ``` context dumps
  * multi-turn history from prior technical exchanges

When the classifiers concatenated the ENTIRE message history and scored by raw
keyword frequency, this scaffolding made a trivial "hello" score as
technical+deep (proven: category score 32 for a one-word greeting). Repetition
of "debug/refactor/analyze" across a 35k-char envelope turned prompt LENGTH into
the routing signal.

This module isolates the current user turn and strips injected scaffolding so
downstream detectors see intent, not envelope.

Design rules:
  - Pure, stateless, deterministic.
  - Fail-open: never return empty when the input had text. Every strip step has
    a fallback to a less-aggressive representation.
"""

import re
from dataclasses import dataclass, field
from typing import List

from app.models import ChatCompletionRequest, Message


# ─── Scaffolding patterns ────────────────────────────────────────────────────

# Directive header LABELS. Matches an ALL-CAPS (or caps-dominant) bracketed
# label optionally followed by a ``: value``. Covers real client envelopes:
#   [SYSTEM INSTRUCTIONS — FOLLOW PRECISELY]
#   [DEEP PERSONA: strategist]
#   [REGISTER LOCK]
#   [CONTEXT]
# We strip ONLY the bracket token itself, never the prose that follows it. This
# is deliberate and load-bearing: the real user query often trails a directive
# label with no marker, so consuming trailing prose risks eating the actual
# request. Leaving benign scaffolding prose in place merely over-serves (fails
# safe); destroying the real query would under-serve (fails unsafe). The leading
# token must start with an uppercase letter followed by uppercase/space/punct so
# ordinary "[note]" style user text is left alone.
_DIRECTIVE_BLOCK_RE = re.compile(
    r"\[[A-Z][A-Z0-9 _\-—–/&.]{1,60}(?::[^\]]*)?\]",
    re.DOTALL,
)

# Angle-tag scaffolding blocks: <context>...</context>, <system>...</system>,
# <persona>...</persona>, <register_lock>...</register_lock>, etc.
_ANGLE_BLOCK_RE = re.compile(
    r"<([a-zA-Z_][\w\-]*)>.*?</\1>",
    re.DOTALL,
)

# Fenced code / context dumps.
_FENCE_RE = re.compile(r"```[\s\S]*?```")

# Explicit "here comes the real user question" markers. When present, everything
# BEFORE the last marker is envelope; we keep only the trailing segment.
# Bilingual (EN + VI, diacritic and non-diacritic).
_QUERY_MARKER_RE = re.compile(
    r"(?:"
    r"user(?:'s)?\s+(?:question|message|query|input|request|prompt)"
    r"|user"
    r"|human"
    r"|question"
    r"|query"
    r"|c[âa]u\s+h[ỏo]i(?:\s+c[ủu]a\s+ng[ưu][ờo]i\s+d[ùu]ng)?"
    r"|ng[ưu][ờo]i\s+d[ùu]ng"
    r"|tin\s+nh[ắa]n\s+c[ủu]a\s+ng[ưu][ờo]i\s+d[ùu]ng"
    r")\s*[:：]",
    re.IGNORECASE,
)

# Instruction-pair acknowledgement tail (Chatbot Gemini-2.5 injection pair).
_ACK_TAIL_RE = re.compile(
    r"acknowledge\s+and\s+internalize.*$",
    re.IGNORECASE | re.DOTALL,
)


# ─── Text extraction helper ──────────────────────────────────────────────────

def _msg_text(msg: Message) -> str:
    """Extract plain text from a message (str or content-block list)."""
    content = getattr(msg, "content", None)
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                # Only text blocks carry intent; skip image/audio/tool blocks.
                if part.get("type") in (None, "text"):
                    parts.append(part.get("text") or part.get("content") or "")
                elif "text" in part:
                    parts.append(part.get("text") or "")
            else:
                parts.append(getattr(part, "text", "") or "")
        return " ".join(filter(None, parts))
    return ""


# ─── Result dataclass ────────────────────────────────────────────────────────

@dataclass
class CurrentIntent:
    """Isolated representation of the user's current request."""
    text: str = ""                # scaffolding-stripped current-turn text
    raw_last_user: str = ""       # unmodified last user message
    had_marker: bool = False      # an explicit query marker was found
    had_scaffolding: bool = False  # scaffolding was stripped
    reasons: List[str] = field(default_factory=list)


# ─── Core primitives ─────────────────────────────────────────────────────────

def _last_user_text(request: ChatCompletionRequest) -> str:
    """Return the text of the last role=user message (the current turn)."""
    for msg in reversed(request.messages or []):
        if getattr(msg, "role", "") == "user":
            return _msg_text(msg)
    return ""


def _strip_scaffolding(text: str) -> tuple:
    """Remove injected scaffolding blocks from a single message's text.

    Returns (stripped_text, had_scaffolding).
    """
    if not text:
        return "", False

    original = text
    # 1) Drop the Chatbot instruction-pair acknowledgement tail.
    text = _ACK_TAIL_RE.sub(" ", text)
    # 2) Remove angle-tag blocks (<context>...</context>).
    text = _ANGLE_BLOCK_RE.sub(" ", text)
    # 3) Remove fenced code / context dumps.
    text = _FENCE_RE.sub(" ", text)
    # 4) Remove ALL-CAPS directive blocks and their trailing content.
    text = _DIRECTIVE_BLOCK_RE.sub(" ", text)

    text = re.sub(r"\s+", " ", text).strip()
    had_scaffolding = text != original.strip()
    return text, had_scaffolding


def extract_current_intent(request: ChatCompletionRequest) -> CurrentIntent:
    """Isolate the user's current request from the message envelope.

    Pipeline:
      1. Take the last role=user message (current turn only — ignore history).
      2. If explicit query marker(s) exist, keep only the trailing segment
         after the LAST marker (everything before is envelope).
      3. Strip injected scaffolding blocks (directive/angle/fenced).
      4. Fail-open: if any step empties the text, fall back to the previous,
         less-aggressive representation. Never return empty for non-empty input.
    """
    raw_last = _last_user_text(request)
    reasons: List[str] = []

    if not raw_last or not raw_last.strip():
        # No user turn at all — fall back to the whole-history join so callers
        # that relied on prior behavior still get *something* to classify.
        whole = "\n".join(
            _msg_text(m) for m in (request.messages or []) if m.role != "system"
        )
        return CurrentIntent(
            text=whole.strip(),
            raw_last_user="",
            reasons=["no user turn; fell back to non-system history"],
        )

    working = raw_last
    had_marker = False

    # Step 2: honor explicit query markers.
    markers = list(_QUERY_MARKER_RE.finditer(working))
    if markers:
        candidate = working[markers[-1].end():].strip()
        if candidate:
            working = candidate
            had_marker = True
            reasons.append("isolated trailing segment after query marker")

    # Step 3: strip scaffolding.
    stripped, had_scaffolding = _strip_scaffolding(working)
    if had_scaffolding:
        reasons.append("stripped injected scaffolding blocks")

    # Step 4: fail-open fallbacks.
    final = stripped
    if not final:
        # Stripping removed everything; fall back to pre-strip working text.
        final = working.strip()
        reasons.append("strip emptied text; fell back to pre-strip turn")
    if not final:
        # Still empty; fall back to the raw last user message.
        final = raw_last.strip()
        reasons.append("fell back to raw last user message")

    return CurrentIntent(
        text=final,
        raw_last_user=raw_last.strip(),
        had_marker=had_marker,
        had_scaffolding=had_scaffolding,
        reasons=reasons or ["used raw current turn"],
    )
