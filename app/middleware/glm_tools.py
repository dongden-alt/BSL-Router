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

# 5th GLM tool-call dialect: ChatML-style <tool_use>/<invoke>/<parameter>
# blocks emitted by GLM-5.3-flash-max via the chat2api guest lane. The four
# parsers above (unicode ｜tool▁calls｜, <tool_call> XML, DeepSeek inner
# markers, pseudo-XML <name>/<arguments>) all miss this dialect, so
# normalize_glm_tool_calls left tool_calls empty and the whole batch was
# dropped -- the recurring GLM-5.3 tool-batch drop bug.
#
# Shape:
#   <tool_use>                                 <-- outer wrapper
#   <invoke name=get_weather>                  <-- one or more invokes
#   <parameter name=city>Hanoi</parameter>     <-- zero or more parameters
#   </invoke>
#   </tool_use>
#
# GLM also emits the same invoke body inside the standard <tool_call> block,
# so the outer wrapper accepts <tool_use> OR <tool_call>. Attribute values may
# be quoted ("x" / 'x') or unquoted (x); whitespace/newlines are arbitrary.
_TOOL_USE_INVOKE_RE = re.compile(
    r"<(?:tool_use|tool_call)(?:\s+[^>]*)?>(.*?)</(?:tool_use|tool_call)\s*>",
    re.DOTALL | re.IGNORECASE,
)

# One <invoke ...>...</invoke> element. Attributes are captured as a blob so
# the name= attribute can sit in any position relative to others; the body
# holds zero or more <parameter> children.
_INVOKE_RE = re.compile(
    r"<invoke\b(?P<attrs>[^>]*)>(?P<body>.*?)</invoke\s*>",
    re.DOTALL | re.IGNORECASE,
)

# One <parameter ...>v</parameter> child. The value is the raw text up to the
# closer; coerced to a native JSON type by the caller when it parses.
_PARAMETER_RE = re.compile(
    r"<parameter\b(?P<attrs>[^>]*)>(?P<value>.*?)</parameter\s*>",
    re.DOTALL | re.IGNORECASE,
)

# name= attribute extractor shared by <invoke> and <parameter>: accepts double-
# quoted, single-quoted, or unquoted values. \bname avoids matching unrelated
# attributes such as displayname=.
_ATTR_NAME_RE = re.compile(
    r"\bname\s*=\s*(?:\"(?P<qname>[^\"]*)\"|'(?P<aname>[^']*)'|(?P<bname>[^\s>]*))",
    re.IGNORECASE,
)


def _attr_name(attrs: str) -> str:
    """Pull the ``name=`` attribute value from an element's attrs blob.

    Accepts double-quoted, single-quoted, or unquoted values. Returns ``""``
    when no ``name=`` attribute is present (or its value is empty), so the
    caller can skip a nameless invoke/parameter rather than emit junk.
    """
    m = _ATTR_NAME_RE.search(attrs or "")
    if not m:
        return ""
    return m.group("qname") or m.group("aname") or m.group("bname") or ""


def _coerce_param_value(raw: str) -> Any:
    """Parse a ``<parameter>`` value to its native JSON type when it IS valid
    JSON (object/array/number/bool/null); otherwise return the raw string.

    A bare word like ``Hanoi`` is not valid JSON, so it stays a string. This
    lets ``<parameter name=options>{"limit": 5}</parameter>`` yield a dict
    while ``<parameter name=city>Hanoi</parameter>`` yields a string.
    """
    if not raw:
        return ""
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return raw


def _tool_call_dict(name: Any, args: Any, index: int = 0) -> Optional[Dict[str, Any]]:
    """Build an OpenAI tool_call dict from a name plus a str-or-dict argument set.

    Returns ``None`` when there is no usable name, so callers can skip junk
    entries rather than emitting a nameless call.
    """
    if not name:
        return None
    if args is None:
        args = {}
    if not isinstance(args, str):
        args = json.dumps(args, ensure_ascii=False, sort_keys=True)
    name_s = str(name)
    return {
        "id": _make_tool_call_id(index, name_s, args),
        "type": "function",
        "function": {"name": name_s, "arguments": args},
    }


def _call_from_parsed_obj(parsed: Dict[str, Any], index: int = 0) -> Optional[Dict[str, Any]]:
    """Extract a tool_call dict from one already-decoded JSON object.

    Accepts the two shapes models actually emit:
      {"name": "foo", "arguments": {...}}
      {"function": {"name": "foo", "arguments": {...}}}
    """
    fn = parsed.get("function")
    if isinstance(fn, dict) and fn.get("name"):
        return _tool_call_dict(fn["name"], fn.get("arguments", ""), index)
    name = parsed.get("name") or parsed.get("tool")
    if isinstance(name, dict):
        # {"name": {"function": ...}} -- unusual, but cheap to accept.
        return _call_from_parsed_obj(name, index)
    if name:
        args = parsed.get("arguments")
        if args is None:
            args = parsed.get("parameters")
        if args is None:
            args = parsed.get("args")
        return _tool_call_dict(name, args, index)
    return None


# Every pseudo-XML <name>...</name> occurrence, in document order. A PARALLEL
# BATCH may repeat the name/arguments pair several times inside one block, so
# this must be findall -- a single re.search silently keeps only the first call
# and discards the rest of the batch.
_PXML_NAME_RE = re.compile(r"<name>\s*(.*?)\s*</name>", re.DOTALL | re.IGNORECASE)
_PXML_ARGS_RE = re.compile(r"<arguments>\s*(.*?)\s*</arguments>", re.DOTALL | re.IGNORECASE)


def _parse_pseudo_xml_calls(text: str) -> List[Dict[str, Any]]:
    """Parse repeated ``<name>x</name><arguments>{...}</arguments>`` pairs.

    Pairs are matched positionally: the i-th <name> takes the i-th <arguments>,
    falling back to ``{}`` when a call omits its argument element. Returns every
    pair found, so a batch of N yields N calls instead of 1.
    """
    names = [m.group(1).strip() for m in _PXML_NAME_RE.finditer(text)]
    if not names:
        return []
    args_all = [m.group(1).strip() for m in _PXML_ARGS_RE.finditer(text)]

    calls: List[Dict[str, Any]] = []
    for i, name in enumerate(names):
        if not name:
            continue
        args_str = args_all[i] if i < len(args_all) else "{}"
        call = _tool_call_dict(name, args_str, i)
        if call:
            calls.append(call)
    return calls


def _calls_from_invoke_block(block_text: str, base_index: int = 0) -> List[Dict[str, Any]]:
    """Parse ChatML ``<invoke>/<parameter>`` elements into OpenAI tool_call dicts.

    The 5th GLM tool-call dialect (GLM-5.3-flash-max via the chat2api guest
    lane). One dict per ``<invoke>``; 2+ invokes = a parallel batch and ALL
    members are recovered. Each invoke's ``<parameter>`` children merge into
    one JSON object argument string. A parameter value that is itself valid
    JSON (object/array/number/bool/null) parses to the native type; otherwise
    the raw string is kept. An invoke with no usable name is skipped (the None
    contract of :func:`_tool_call_dict`). Ids are built via
    :func:`_make_tool_call_id` so batch members stay distinct.

    Scans the whole ``block_text`` for ``<invoke>`` elements, so it recovers
    bare invokes with no outer ``<tool_use>`` wrapper as well as wrapped ones.
    """
    calls: List[Dict[str, Any]] = []
    for idx, m in enumerate(_INVOKE_RE.finditer(block_text or "")):
        name = _attr_name(m.group("attrs") or "")
        if not name:
            continue
        params: Dict[str, Any] = {}
        for pm in _PARAMETER_RE.finditer(m.group("body") or ""):
            pname = _attr_name(pm.group("attrs") or "")
            if not pname:
                continue
            params[pname] = _coerce_param_value((pm.group("value") or "").strip())
        call = _tool_call_dict(name, params, base_index + idx)
        if call:
            calls.append(call)
    return calls


def _split_concatenated_objects(text: str) -> List[Dict[str, Any]]:
    """Decode a run of back-to-back JSON objects: ``{...}{...}{...}``.

    A batch streamed into one block with no array wrapper and no separator
    parses as ``Extra data`` under a plain json.loads. ``raw_decode`` consumes
    one object at a time from the current offset, recovering every member
    instead of rejecting the whole span.

    Returns [] if fewer than two objects decode, so callers can fall through to
    the next strategy rather than mistaking a single object for a batch.
    """
    decoder = json.JSONDecoder()
    objs: List[Dict[str, Any]] = []
    pos = 0
    n = len(text)
    while pos < n:
        while pos < n and text[pos] in " \t\r\n,;":
            pos += 1
        if pos >= n:
            break
        if text[pos] != "{":
            break
        try:
            obj, end = decoder.raw_decode(text, pos)
        except (json.JSONDecodeError, ValueError):
            break
        if isinstance(obj, dict):
            objs.append(obj)
        pos = end
    return objs if len(objs) >= 2 else []


def _parse_tool_call_block_multi(block_text: str) -> List[Dict[str, Any]]:
    """Parse ONE <tool_call> block into a LIST of OpenAI tool_call dicts.

    ── GLM BATCH-DROP FIX (2026-09-22) ────────────────────────────────────
    The previous contract was ``Optional[Dict]``: at most ONE call per block.
    GLM emits parallel batches, and several reseller channels wrap the whole
    batch in a SINGLE <tool_call> block. Every batch shape then lost calls:

      JSON ARRAY   [{...},{...}]  json.loads -> list, not dict; the isinstance
                                  check rejected it, pseudo-XML found no
                                  <name>, so the parser returned None.
                                  changed=False: the ENTIRE BATCH VANISHED
                                  silently, with no log line at all.
      CONCATENATED {...}{...}     Extra data -> None -> whole batch lost.
      PSEUDO-XML   <name>..x2     re.search matched only the FIRST pair, so a
                                  batch of N silently became 1.

    Strategies, in order, first non-empty result wins:
      1. one JSON object      (the original single-call shape)
      2. a JSON ARRAY of call objects   <-- the canonical parallel batch
      3. back-to-back JSON objects via raw_decode
      4. every repeated pseudo-XML <name>/<arguments> pair
      5. ChatML <tool_use>/<invoke>/<parameter> blocks (GLM-5.3-flash-max)
    """
    text = (block_text or "").strip()
    if not text:
        return []

    # ── 1/2. JSON: a single object OR an array of objects.
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        parsed = None

    if isinstance(parsed, dict):
        call = _call_from_parsed_obj(parsed, 0)
        if call:
            return [call]
    elif isinstance(parsed, list):
        calls: List[Dict[str, Any]] = []
        for i, item in enumerate(parsed):
            if not isinstance(item, dict):
                continue
            call = _call_from_parsed_obj(item, i)
            if call:
                calls.append(call)
        if calls:
            return calls

    # ── 3. Concatenated objects: {..}{..}{..}
    concat = _split_concatenated_objects(text)
    if concat:
        calls = []
        for i, obj in enumerate(concat):
            call = _call_from_parsed_obj(obj, i)
            if call:
                calls.append(call)
        if calls:
            return calls

    # ── 4. Repeated pseudo-XML pairs.
    pxml = _parse_pseudo_xml_calls(text)
    if pxml:
        return pxml

    # ── 5. ChatML <tool_use>/<invoke>/<parameter> dialect (GLM-5.3-flash-max).
    # Scans the whole block for <invoke> elements, so it recovers bare invokes
    # (no outer wrapper) as well as <tool_use>/<tool_call>-wrapped batches. Every
    # <invoke> in the block is returned, so a 2+ invoke batch keeps all members.
    invoke_calls = _calls_from_invoke_block(text)
    if invoke_calls:
        return invoke_calls

    return []


def _parse_tool_call_block(block_text: str) -> Optional[Dict[str, Any]]:
    """Parse a single <tool_call> block into ONE OpenAI tool_call dict.

    Retained for callers that only need the first call. Batch-aware code should
    use :func:`_parse_tool_call_block_multi`, which returns every member.

    Supports both JSON and pseudo-XML formats:
      {"name": "foo", "arguments": {...}}
      <name>foo</name><arguments>{...}</arguments>
    """
    calls = _parse_tool_call_block_multi(block_text)
    return calls[0] if calls else None


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
        # BATCH-AWARE: a unicode block holding a JSON ARRAY is a parallel batch,
        # so extend with every member rather than keeping only the first.
        return _parse_tool_call_block_multi(text)

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
# 5th dialect: ChatML <tool_use> opener (GLM-5.3-flash-max inline wire shape).
# The buffered path (normalize_glm_tool_calls) already matches this via
# _TOOL_USE_INVOKE_RE; the streaming path had no opener for it, so a
# GLM-5.3 <tool_use><invoke> block streamed as text leaked as prose.
_TOOL_USE_OPEN_RE = re.compile(r"<tool_use(?:\s+[^>]*)?>", re.IGNORECASE)

# Closer-only variants.
_TOOL_CALL_CLOSE_RE = re.compile(r"</tool_call\s*>", re.IGNORECASE)
_GLM_UNICODE_TC_CLOSE_RE = re.compile(
    rf"<{_GLM_PIPE}\s*tool{_GLM_JUNCTION}calls?{_GLM_JUNCTION}end{_GLM_PIPE}\s*>",
    re.IGNORECASE,
)
_TOOL_USE_CLOSE_RE = re.compile(r"</tool_use\s*>", re.IGNORECASE)

# A trailing segment that could still grow into an opener: a '<' followed only
# by delimiter-ish characters. Held back from emission until the next delta
# proves it is (or is not) a delimiter. Bounded by the caller so ordinary text
# containing '<' is delayed by at most a handful of characters.
_PARTIAL_OPEN_TAIL_RE = re.compile(r"<[\s|｜_▁A-Za-z]*$")


def find_earliest_opener(text: str) -> Optional[Tuple[int, int, str]]:
    """Locate the earliest tool-call opener in ``text``.

    Returns ``(start, end, mode)`` where ``end`` is the index just past
    the opener and ``mode`` is one of ``"tool_call"``, ``"unicode"``, or
    ``"tool_use"``, or ``None`` when no opener is present.
    """
    best: Optional[Tuple[int, int, str]] = None
    m = _TOOL_CALL_OPEN_RE.search(text)
    if m:
        best = (m.start(), m.end(), "tool_call")
    m = _GLM_UNICODE_TC_OPEN_RE.search(text)
    if m and (best is None or m.start() < best[0]):
        best = (m.start(), m.end(), "unicode")
    m = _TOOL_USE_OPEN_RE.search(text)
    if m and (best is None or m.start() < best[0]):
        best = (m.start(), m.end(), "tool_use")
    return best


def find_block_closer(text: str, mode: str, pos: int = 0) -> Optional[Tuple[int, int]]:
    """Locate the matching closing delimiter at or after ``pos``.

    Returns ``(start, end)`` of the closer, or ``None`` if not yet present.
    ``mode`` is the opener mode returned by :func:`find_earliest_opener`.
    """
    rx = (
        _GLM_UNICODE_TC_CLOSE_RE if mode == "unicode"
        else _TOOL_USE_CLOSE_RE if mode == "tool_use"
        else _TOOL_CALL_CLOSE_RE
    )
    m = rx.search(text, pos)
    return (m.start(), m.end()) if m else None


def parse_streamed_tool_block(inner_text: str, mode: str) -> List[Dict[str, Any]]:
    """Parse a rescued block body into OpenAI tool_call dicts (possibly empty).

    Mirrors the dispatch in :func:`normalize_glm_tool_calls`: unicode blocks may
    contain multiple DeepSeek inner calls; tool_call ASCII blocks hold a JSON
    or pseudo-XML payload; tool_use blocks hold ChatML <invoke>/<parameter>.
    Fail-open: any exception yields an empty list so the caller can emit the
    raw text instead.
    """
    try:
        if mode == "unicode":
            return _parse_unicode_tool_call_block(inner_text)
        # tool_call and tool_use both land in the batch-aware multi parser;
        # tool_use blocks are handled by the invoke scanner inside it.
        return _parse_tool_call_block_multi(inner_text)
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

            # 5th dialect: ChatML <tool_use><invoke ...>...</invoke></tool_use>.
            # GLM-5.3-flash-max (chat2api guest lane) emits tool calls in this
            # shape; the <tool_use> wrapper is not matched by the two finders
            # above, so without this path the whole batch was dropped. (When
            # GLM wraps the invoke body in the standard tool_call block,
            # _TOOL_CALL_RE above already captures it; this branch catches the
            # bare <tool_use> wrapper that the earlier finders miss.)
            if not blocks:
                blocks = _TOOL_USE_INVOKE_RE.findall(content)

            if not blocks:
                continue

            parsed_calls: List[Dict[str, Any]] = []
            for block in blocks:
                if unicode_path:
                    # The unicode block may itself contain multiple DeepSeek
                    # inner tool-call markers (parallel tool calls).
                    parsed_calls.extend(_parse_unicode_tool_call_block(block))
                else:
                    # BATCH-AWARE: one ASCII block can carry the WHOLE parallel
                    # batch (JSON array, concatenated objects, or repeated
                    # pseudo-XML pairs). The old single-call contract reduced
                    # such a block to 0 or 1 and -- because a JSON array failed
                    # the isinstance(dict) check and then found no <name> --
                    # returned None, so changed stayed False and the entire
                    # batch vanished with no log line whatsoever.
                    parsed_calls.extend(_parse_tool_call_block_multi(block))

            if not parsed_calls:
                continue

            # Strip the tool_call blocks from content
            cleaned_content = _TOOL_CALL_RE.sub("", content)
            cleaned_content = _GLM_UNICODE_TC_RE.sub("", cleaned_content)
            cleaned_content = _TOOL_USE_INVOKE_RE.sub("", cleaned_content)
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
