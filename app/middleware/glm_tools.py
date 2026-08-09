"""
Middleware.glm_tools — GLM-specific efficiency and compatibility transforms

Ports two 9Router strategies that are GLM-specific:
  - GLM Tool-Call Normalizer: parses <tool_call>...</tool_call> XML blocks from
    GLM text output into structured OpenAI tool_calls arrays. Many reseller
    channels serving GLM-4.x/5.x emit tool calls as inline XML rather than
    structured tool_calls, causing parallel tool-call loss.
  - GLM Language Forcing: detects the user's natural language and instructs GLM
    to think and respond in that language, avoiding English-thinking token waste
    on non-English tasks.

Safety contract:
  1. Pure transformation only; no retries, no state.
  2. Fail-open: any exception returns the original payload unchanged.
  3. Non-invasive: language forcing only injects when GLM + non-English detected.
  4. Tool-call normalizer only activates when <tool_call> tags are present.
"""

import hashlib
import json
import re
from typing import Any, Dict, List, Optional, Tuple


# ── GLM Tool-Call Normalizer ──────────────────────────────────────────────────

# Matches <tool_call>...</tool_call> or <tool_call type="...">...</tool_call>
_TOOL_CALL_RE = re.compile(
    r"<tool_call(?:\s+[^>]*)?>(.*?)</tool_call>",
    re.DOTALL | re.IGNORECASE,
)

# GLM/DeepSeek/Qwen families emit tool-call blocks delimited by special tokens
# whose glyphs are NOT plain ASCII. The pipe may be ASCII '|' (U+007C) or the
# FULLWIDTH VERTICAL LINE '｜' (U+FF5C); the separator between "tool", "calls",
# and "begin"/"end" may be ASCII '_' (U+005F) or the LOWER ONE EIGHTH BLOCK
# '▁' (U+2581). Models are inconsistent, so we accept every combination.
_GLM_PIPE = r"[\||｜]"      # '|' or '｜'
# Combined: accept EITHER pipe or sep at any word junction. GLM is inconsistent:
# ASCII form is < tool_CALLS_begin > (underscore) but unicode is <｜tool▁calls｜begin｜>
# (sep between tool/calls, PIPE between calls/begin). Accepting both everywhere
# is the only way to catch every variant models actually emit.
#
# NOTE: use _GLM_JUNCTION at EVERY word junction, outer and inner alike. An
# earlier revision used a narrower '[_▁]' class for the inner markers only,
# which made pipe-junction inner blocks (｜tool｜call｜begin｜) fall through and
# leak raw markup into assistant text. Do not reintroduce a narrower class.
_GLM_JUNCTION = r"[_▁｜\|]"

# Fallback: GLM sometimes emits <｜tool▁calls▁begin｜> ... <｜tool▁calls▁end｜>
# Accept both the plural "tool_calls" and singular "tool_call" outer forms, any
# pipe/separator mix, and optional surrounding whitespace.
_GLM_UNICODE_TC_RE = re.compile(
    rf"<{_GLM_PIPE}\s*tool{_GLM_JUNCTION}calls?{_GLM_JUNCTION}begin{_GLM_PIPE}\s*>"
    rf"(.*?)"
    rf"<{_GLM_PIPE}\s*tool{_GLM_JUNCTION}calls?{_GLM_JUNCTION}end{_GLM_PIPE}\s*>",
    re.DOTALL | re.IGNORECASE,
)

# DeepSeek's native inner format wraps each call in its own markers inside the
# outer block, e.g.:
#   ｜tool▁call▁begin｜function｜tool▁call▁sep｜read_file
#   ```json
#   {"file": "a.txt"}
#   ```
#   ｜tool▁call▁end｜
# The standard <tool_call>...</tool_call> parser does NOT match these begin/end
# markers, so a block that only uses the DeepSeek inner format would otherwise
# yield zero tool calls. This regex finds each inner call within an outer block.
_GLM_INNER_TC_RE = re.compile(
    rf"{_GLM_PIPE}tool{_GLM_JUNCTION}call{_GLM_JUNCTION}begin{_GLM_PIPE}"
    rf"(.*?){_GLM_PIPE}tool{_GLM_JUNCTION}call{_GLM_JUNCTION}end{_GLM_PIPE}",
    re.DOTALL | re.IGNORECASE,
)


def _parse_tool_call_block(block_text: str) -> Optional[Dict[str, Any]]:
    """Parse a single <tool_call> block into an OpenAI tool_call dict.

    Supports both JSON and pseudo-XML formats:
      {"name": "foo", "arguments": {...}}
      <name>foo</name><arguments>{...}</arguments>
    """
    text = block_text.strip()
    if not text:
        return None

    # Try JSON first (most common from GLM-5.x)
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            name = parsed.get("name") or parsed.get("function") or parsed.get("tool")
            args = parsed.get("arguments") or parsed.get("parameters") or parsed.get("args")
            if name:
                if args is None:
                    args = {}
                if not isinstance(args, str):
                    args = json.dumps(args, ensure_ascii=False, sort_keys=True)
                return {
                    "id": _make_tool_call_id(0, str(name), args),
                    "type": "function",
                    "function": {"name": str(name), "arguments": args},
                }
            # Maybe it's {"function": {"name": ..., "arguments": ...}}
            fn = parsed.get("function")
            if isinstance(fn, dict) and fn.get("name"):
                fn_args = fn.get("arguments", "")
                if not isinstance(fn_args, str):
                    fn_args = json.dumps(fn_args, ensure_ascii=False, sort_keys=True)
                return {
                    "id": _make_tool_call_id(0, str(fn["name"]), fn_args),
                    "type": "function",
                    "function": {"name": str(fn["name"]), "arguments": fn_args},
                }
    except (json.JSONDecodeError, ValueError):
        pass

    # Try pseudo-XML: <name>foo</name><arguments>{...}</arguments>
    name_match = re.search(r"<name>\s*(.*?)\s*</name>", text, re.DOTALL | re.IGNORECASE)
    if name_match:
        name = name_match.group(1).strip()
        args_match = re.search(
            r"<arguments>\s*(.*?)\s*</arguments>", text, re.DOTALL | re.IGNORECASE
        )
        args_str = args_match.group(1).strip() if args_match else "{}"
        return {
            "id": _make_tool_call_id(0, name, args_str),
            "type": "function",
            "function": {"name": name, "arguments": args_str},
        }

    return None


def _make_tool_call_id(index: int, name: str, args_str: str) -> str:
    """Build a stable, collision-free id for a rescued tool call.

    Seeded with the block ``index`` so that two IDENTICAL parallel calls (same
    name, same arguments — e.g. reading a file before and after an edit) still
    receive distinct ids; a colliding id breaks request/response correlation on
    the client.

    Uses a sha1 digest rather than :func:`hash`, whose value is randomized per
    process via ``PYTHONHASHSEED`` and therefore differs across workers and
    across restarts for the same input.
    """
    digest = hashlib.sha1(
        f"{index}:{name}:{args_str}".encode("utf-8", errors="replace")
    ).hexdigest()
    return f"call_{digest[:12]}"


def _parse_unicode_tool_call_block(block_text: str) -> List[Dict[str, Any]]:
    """Parse a GLM/DeepSeek unicode-delimited tool-call block.

    The outer ｜tool▁calls▁begin｜...｜tool▁calls▁end｜ wrapper has already been
    stripped by the caller. The remaining content may be either:

      1. Plain JSON (or pseudo-XML) — e.g. ``{"name": "foo", ...}`` — emitted by
         GLM/Qwen. Falls through to :func:`_parse_tool_call_block`.
      2. DeepSeek's native inner format, where each call is wrapped in its own
         ``｜tool▁call▁begin｜...｜tool▁call▁end｜`` markers, optionally prefixed
         with the literal ``function`` token and a ``｜tool▁call▁sep｜`` marker,
         followed by the tool name and a ``json``` fenced argument block::

             ｜tool▁call▁begin｜function｜tool▁call▁sep｜read_file
             ```json
             {"file": "a.txt"}
             ```
             ｜tool▁call▁end｜

         One outer block may contain several such inner calls (parallel tool
         calls); each is returned as a separate entry.

    Returns a list of OpenAI tool_call dicts (possibly empty).
    """
    text = block_text.strip()
    if not text:
        return []

    inner_blocks = _GLM_INNER_TC_RE.findall(text)
    if not inner_blocks:
        # Not the DeepSeek inner format; defer to the JSON / pseudo-XML parser.
        call = _parse_tool_call_block(text)
        return [call] if call else []

    calls: List[Dict[str, Any]] = []
    for idx, inner in enumerate(inner_blocks):
        body = inner.strip()
        # Strip the optional leading "function" + "tool_call_sep" prefix that
        # DeepSeek emits before the tool name.
        body = re.sub(
            rf"^function\s*{_GLM_PIPE}tool{_GLM_JUNCTION}call{_GLM_JUNCTION}sep{_GLM_PIPE}\s*",
            "",
            body,
            flags=re.IGNORECASE,
        )
        # Prefer a ```json ... ``` fenced argument block (DeepSeek's usual form).
        fence = re.search(r"```(?:json)?\s*(.*?)```", body, re.DOTALL | re.IGNORECASE)
        if fence:
            args_str = fence.group(1).strip()
            name = body[: fence.start()].strip()
        else:
            # Bare form: "<NAME>\n{...}" or "<NAME> {...}".
            brace = body.find("{")
            if brace == -1:
                continue
            name = body[:brace].strip()
            args_str = body[brace:].strip()

        if not name:
            continue

        # Normalize the arguments to a JSON string (OpenAI format) when they
        # parse. When they do NOT parse, forward the raw text verbatim rather
        # than substituting "{}": an empty object is a plausible-but-wrong call
        # that the model cannot distinguish from success, so it never retries.
        # Forwarding keeps the failure VISIBLE — the client rejects it, the
        # error re-enters the conversation, and the model self-corrects.
        try:
            parsed_args = json.loads(args_str)
            if not isinstance(parsed_args, str):
                args_str = json.dumps(parsed_args, ensure_ascii=False, sort_keys=True)
        except (json.JSONDecodeError, ValueError):
            pass  # forward raw; let the client decide.

        calls.append(
            {
                "id": _make_tool_call_id(idx, name, args_str),
                "type": "function",
                "function": {"name": str(name), "arguments": args_str},
            }
        )

    return calls


# ── Streaming tool-call rescue (BUG A) ────────────────────────────────────────
#
# BUG A (diagnosed 2026-08-04): GLM/Opus/Sonnet sometimes emit tool calls as
# inline text-delimiter markup instead of structured tool_calls. The buffered
# path repairs this via normalize_glm_tool_calls(), but the streaming path had
# no equivalent, so the markup leaked to the IDE as prose. These helpers give
# the stream normalizer the same parsing semantics, operating incrementally:
# buffering is scoped to DELIMITERS (never timers), and anything unparseable
# fails open as text -- never worse than today's verbatim behavior.

# Opener-only variants (no closer) let the streaming rescue find where a block
# starts before its closing delimiter has arrived.
_TOOL_CALL_OPEN_RE = re.compile(r"<tool_call(?:\s+[^>]*)?>", re.IGNORECASE)
_GLM_UNICODE_TC_OPEN_RE = re.compile(
    rf"<{_GLM_PIPE}\s*tool{_GLM_JUNCTION}calls?{_GLM_JUNCTION}begin{_GLM_PIPE}\s*>",
    re.IGNORECASE,
)

# Closer-only variants.
_TOOL_CALL_CLOSE_RE = re.compile(r"</tool_call\s*>", re.IGNORECASE)
_GLM_UNICODE_TC_CLOSE_RE = re.compile(
    rf"<{_GLM_PIPE}\s*tool{_GLM_JUNCTION}calls?{_GLM_JUNCTION}end{_GLM_PIPE}\s*>",
    re.IGNORECASE,
)

# A trailing segment that could still grow into an opener: a '<' followed only
# by delimiter-ish characters. Held back from emission until the next delta
# proves it is (or is not) a delimiter. Bounded by the caller so ordinary text
# containing '<' is delayed by at most a handful of characters.
_PARTIAL_OPEN_TAIL_RE = re.compile(r"<[\s|｜_▁A-Za-z]*$")


def find_earliest_opener(text: str) -> Optional[Tuple[int, int, bool]]:
    """Locate the earliest tool-call opener in ``text``.

    Returns ``(start, end, unicode_path)`` where ``end`` is the index just past
    the opener, or ``None`` when no opener is present.
    """
    best: Optional[Tuple[int, int, bool]] = None
    m = _TOOL_CALL_OPEN_RE.search(text)
    if m:
        best = (m.start(), m.end(), False)
    m = _GLM_UNICODE_TC_OPEN_RE.search(text)
    if m and (best is None or m.start() < best[0]):
        best = (m.start(), m.end(), True)
    return best


def find_block_closer(text: str, unicode_path: bool, pos: int = 0) -> Optional[Tuple[int, int]]:
    """Locate the matching closing delimiter at or after ``pos``.

    Returns ``(start, end)`` of the closer, or ``None`` if not yet present.
    """
    rx = _GLM_UNICODE_TC_CLOSE_RE if unicode_path else _TOOL_CALL_CLOSE_RE
    m = rx.search(text, pos)
    return (m.start(), m.end()) if m else None


def parse_streamed_tool_block(inner_text: str, unicode_path: bool) -> List[Dict[str, Any]]:
    """Parse a rescued block body into OpenAI tool_call dicts (possibly empty).

    Mirrors the dispatch in :func:`normalize_glm_tool_calls`: unicode blocks may
    contain multiple DeepSeek inner calls; ASCII blocks hold a single JSON or
    pseudo-XML payload. Fail-open: any exception yields an empty list so the
    caller can emit the raw text instead.
    """
    try:
        if unicode_path:
            return _parse_unicode_tool_call_block(inner_text)
        call = _parse_tool_call_block(inner_text)
        return [call] if call else []
    except Exception as e:  # Fail-open, same contract as normalize_glm_tool_calls.
        print(f"[StreamToolRescue] parse failed (fail-open): {e}", flush=True)
        return []


def find_hold_tail(text: str) -> int:
    """Return the index from which ``text`` should be held back from emission.

    Everything before the returned index is proven safe to emit as text.
    Everything from the index onward could still grow into a tool-call opener
    on the next delta. Returns ``len(text)`` when nothing needs holding.
    """
    m = _PARTIAL_OPEN_TAIL_RE.search(text)
    return m.start() if m else len(text)


def normalize_glm_tool_calls(openai_json: Dict[str, Any], model: str = "") -> Tuple[Dict[str, Any], bool]:
    """Post-process an OpenAI-format response to extract inline <tool_call> blocks.

    If the assistant's content contains <tool_call>...</tool_call> XML blocks,
    parse them into structured tool_calls and strip the blocks from content.
    This rescues parallel tool calls that GLM emits as text instead of structured output.

    Returns:
        A tuple of (normalized_json, changed). ``changed`` is True only when the
        response was actually modified (tool calls extracted). Fail-open: any
        exception returns ``(original_json, False)``.
    """
    try:
        choices = openai_json.get("choices")
        if not isinstance(choices, list) or not choices:
            return openai_json, False

        modified = False
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message")
            if not isinstance(message, dict):
                continue

            content = message.get("content")
            if not isinstance(content, str) or not content:
                continue

            # Skip if already has structured tool_calls
            existing_tc = message.get("tool_calls")
            if isinstance(existing_tc, list) and existing_tc:
                continue

            # Find all <tool_call> blocks (try standard then unicode-delimiter)
            blocks = _TOOL_CALL_RE.findall(content)
            unicode_path = False
            if not blocks:
                blocks = _GLM_UNICODE_TC_RE.findall(content)
                unicode_path = True

            if not blocks:
                continue

            parsed_calls: List[Dict[str, Any]] = []
            for block in blocks:
                if unicode_path:
                    # The unicode block may itself contain multiple DeepSeek
                    # inner tool-call markers (parallel tool calls).
                    parsed_calls.extend(_parse_unicode_tool_call_block(block))
                else:
                    call = _parse_tool_call_block(block)
                    if call:
                        parsed_calls.append(call)

            if not parsed_calls:
                continue

            # Strip the tool_call blocks from content
            cleaned_content = _TOOL_CALL_RE.sub("", content)
            cleaned_content = _GLM_UNICODE_TC_RE.sub("", cleaned_content)
            cleaned_content = re.sub(r"\n{3,}", "\n\n", cleaned_content).strip()

            message["tool_calls"] = parsed_calls
            message["content"] = cleaned_content if cleaned_content else None
            modified = True

            print(
                f"[GLM-Normalize] Extracted {len(parsed_calls)} tool_call(s) from "
                f"content for {model}",
                flush=True,
            )

        if modified:
            # Ensure finish_reason reflects tool calls
            for choice in choices:
                if isinstance(choice, dict) and choice.get("finish_reason") == "stop":
                    msg = choice.get("message", {})
                    if isinstance(msg, dict) and msg.get("tool_calls"):
                        choice["finish_reason"] = "tool_calls"

        return openai_json, modified
    except Exception as e:
        print(f"[GLM-Normalize] Failed (fail-open): {e}", flush=True)
        return openai_json, False


# ── GLM Language Forcing ──────────────────────────────────────────────────────

# Vietnamese diacritic characters (à-ỹ, ă, â, ê, ô, ơ, ư, đ)
_VI_CHARS = set("ăâêôơưđÀÁÂÃÈÉÊÌÍÒÓÔÕÙÚÝàáâãèéêìíòóôõùúýĂăÂâÊêÔôƠơƯưĐđăăăăă")
# Vietnamese tone + letter combinations that are unmistakably Vietnamese
_VI_PATTERN = re.compile(r"[àáạảãâầấậẩẫăằắặẳẵèéẹẻẽêềếệểễìíịỉĩòóọỏõôồốộổỗơờớợởỡùúụủũưừứựửữỳýỵỷỹđ]", re.IGNORECASE)
# CJK Unified Ideographs (Chinese/Japanese shared)
_CJK_PATTERN = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
# Hiragana + Katakana (Japanese)
_JP_PATTERN = re.compile(r"[\u3040-\u309f\u30a0-\u30ff]")
# Korean Hangul
_KR_PATTERN = re.compile(r"[\uac00-\ud7af]")


def detect_user_language(messages: List[Any]) -> str:
    """Detect the dominant non-English language from user messages.

    Returns one of: 'vietnamese', 'chinese', 'japanese', 'korean', 'english', 'unknown'.
    Scans up to the last 6 user messages for efficiency.
    """
    try:
        user_texts: List[str] = []
        for msg in reversed(messages or []):
            role = getattr(msg, "role", None) or (msg.get("role") if isinstance(msg, dict) else None)
            if role != "user":
                continue
            content = getattr(msg, "content", None) or (msg.get("content") if isinstance(msg, dict) else None)
            if isinstance(content, str):
                user_texts.append(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict):
                        t = part.get("text", "")
                    else:
                        t = getattr(part, "text", "")
                    if isinstance(t, str):
                        user_texts.append(t)
            if len(user_texts) >= 6:
                break

        if not user_texts:
            return "unknown"

        combined = " ".join(user_texts)

        vi_count = len(_VI_PATTERN.findall(combined))
        cjk_count = len(_CJK_PATTERN.findall(combined))
        jp_count = len(_JP_PATTERN.findall(combined))
        kr_count = len(_KR_PATTERN.findall(combined))

        # Japanese: needs kana to disambiguate from Chinese
        if jp_count >= 3 and jp_count >= kr_count:
            return "japanese"
        if kr_count >= 5:
            return "korean"
        if vi_count >= 3 and vi_count > cjk_count:
            return "vietnamese"
        if cjk_count >= 5 and jp_count < 3:
            return "chinese"

        return "english"
    except Exception:
        return "unknown"


_LANGUAGE_INSTRUCTIONS = {
    "vietnamese": "CRITICAL: Think and reason internally in Vietnamese. Respond in Vietnamese. Do NOT translate to English between steps — this wastes tokens and degrades quality.",
    "chinese": "CRITICAL: Think and reason internally in Chinese (中文). Respond in Chinese. Do NOT translate to English between steps.",
    "japanese": "CRITICAL: Think and reason internally in Japanese (日本語). Respond in Japanese. Do NOT translate to English between steps.",
    "korean": "CRITICAL: Think and reason internally in Korean (한국어). Respond in Korean. Do NOT translate to English between steps.",
}

_LANGUAGE_MARKER = "[BSL GLM Language Forcing]"


def is_glm_model(model_id: str) -> bool:
    """Check if the model is a GLM variant that benefits from language forcing."""
    return bool(re.search(r"glm", model_id or "", re.IGNORECASE))


def inject_glm_language_forcing(messages: List[Any], model_id: str) -> List[Any]:
    """Inject a language-matching instruction for GLM models.

    GLM models default to English internal reasoning even on non-English tasks,
    wasting tokens on translation overhead. This forces GLM to think in the
    user's detected language.

    Only activates when:
      1. Model is GLM
      2. Detected language is non-English
      3. No existing language forcing marker

    Returns a new list; does not mutate the original.
    """
    try:
        if not is_glm_model(model_id):
            return list(messages or [])

        lang = detect_user_language(messages)
        instruction = _LANGUAGE_INSTRUCTIONS.get(lang)
        if not instruction:
            return list(messages or [])

        # Check for existing marker (duplicate-safe)
        for msg in (messages or []):
            content = getattr(msg, "content", None) or (msg.get("content") if isinstance(msg, dict) else None)
            if isinstance(content, str) and _LANGUAGE_MARKER in content:
                return list(messages or [])
            elif isinstance(content, list):
                for part in content:
                    t = part.get("text", "") if isinstance(part, dict) else getattr(part, "text", "")
                    if isinstance(t, str) and _LANGUAGE_MARKER in t:
                        return list(messages or [])

        # Build the injection
        from app.models import Message

        full_instruction = f"{_LANGUAGE_MARKER} {instruction}"
        injection = Message(role="system", content=full_instruction)

        new_messages = list(messages or [])
        # Insert after the first system message, or at the start
        system_idx = -1
        for idx, msg in enumerate(new_messages):
            role = getattr(msg, "role", None) or (msg.get("role") if isinstance(msg, dict) else None)
            if role == "system":
                system_idx = idx
                break

        if system_idx >= 0:
            new_messages.insert(system_idx + 1, injection)
        else:
            new_messages.insert(0, injection)

        print(f"[GLM-LangForce] Injected '{lang}' forcing for {model_id}", flush=True)
        return new_messages
    except Exception as e:
        print(f"[GLM-LangForce] Failed (fail-open): {e}", flush=True)
        return list(messages or [])
