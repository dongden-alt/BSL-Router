"""Dialect-agnostic JSON-repair ladder for tool-arguments strings.

Extracted verbatim from the anthropic lane (app/middleware/anthropic_tools.py)
so every format lane (openai chat / anthropic messages / gemini / responses)
shares one ladder. This module becomes the SINGLE repair ladder for all 4
lanes in phase N2; anthropic_tools.py keeps its private copy until then.

Ladder steps (validated by json.loads before acceptance — a step that would
corrupt a valid document can never win because step 1 returns valid documents
untouched):
  1. fast path — already-valid JSON returned untouched
  2. quote unquoted bare object keys:      {Includes: x}   -> {"Includes": x}
  3. quote unquoted bare values:           {"k": *.js}     -> {"k": "*.js"}
  4. close truncation: terminate open strings, fix dangling comma/colon,
     balance unmatched {/[ in reverse open order.
  5. strip dangling commas before a closer:  {"k": 1,}      -> {"k": 1}

Pure functions, no I/O, no threads. Fail-open by contract: NEVER raises.
"""

from __future__ import annotations

import json
import re
from typing import Any, List, Optional, Tuple

__all__ = [
    "repair_json_arguments",
    "repair_tool_calls_argument_strings",
]

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.\-]*")
_NUMBER_RE = re.compile(r"^-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?$")
_JSON_LITERALS = frozenset({"true", "false", "null"})
_WS = " \t\r\n"

DEFAULT_MAX_BYTES = 262144  # 256 KiB — guard against pathological inputs


def _quote_unquoted_keys(s: str) -> str:
    """Wrap bare identifiers used as object keys: {Includes: x} → {"Includes": x}.

    Only fires when a bare identifier is DIRECTLY followed by ':' after an
    opener/comma — the only position where a key can appear — so array elements
    and bare values are never touched. String literals are skipped.
    """
    out: List[str] = []
    i = 0
    n = len(s)
    in_str = False
    esc = False
    while i < n:
        c = s[i]
        if in_str:
            out.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if c in "{,":
            out.append(c)
            i += 1
            j = i
            while j < n and s[j] in _WS:
                j += 1
            m = _IDENT_RE.match(s, j)
            if m:
                k = m.end()
                k2 = k
                while k2 < n and s[k2] in _WS:
                    k2 += 1
                if k2 < n and s[k2] == ":":
                    out.append(s[i:j])
                    out.append('"' + m.group(0) + '"')
                    i = k
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _quote_unquoted_values(s: str) -> str:
    """Wrap non-literal bare values in quotes: {"Includes": *.js} → {"Includes": "*.js"}.

    Valid JSON scalars (numbers, true/false/null), proper strings and nested
    containers pass through untouched.
    """
    out: List[str] = []
    i = 0
    n = len(s)
    in_str = False
    esc = False
    while i < n:
        c = s[i]
        if in_str:
            out.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == ":":
            out.append(c)
            i += 1
            j = i
            while j < n and s[j] in _WS:
                j += 1
            if j >= n:
                continue  # dangling colon — truncation step handles
            v = s[j]
            if v == '"' or v in "{[":
                continue  # proper string / nested container — main loop handles
            # Bare token: read until a structural terminator.
            k = j
            while k < n and s[k] not in ",}]":
                k += 1
            token = s[j:k].rstrip(_WS)
            if token and token not in _JSON_LITERALS and not _NUMBER_RE.match(token):
                esc_tok = token.replace("\\", "\\\\").replace('"', '\\"')
                out.append(s[i:j])
                out.append('"' + esc_tok + '"')
                i = j + len(token)
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _strip_trailing_commas(s: str) -> str:
    """Remove commas that sit directly before a '}' or ']' closer.

    ``{"path": "a.py",}`` -> ``{"path": "a.py"}``

    A very common model slip, and one ``_close_truncation`` cannot fix: that
    step only strips a comma at the ABSOLUTE END of the buffer, so a comma
    followed by a closer survives and the document stays invalid. Observed in
    the drop reproducer as the single unrecoverable case of seven.

    String-aware: a comma inside a string literal is never touched, so
    ``{"q": "a,}"}`` passes through unchanged. Whitespace between the comma
    and the closer is tolerated (``{"k": 1 , }``).
    """
    out: List[str] = []
    i = 0
    n = len(s)
    in_str = False
    esc = False
    while i < n:
        c = s[i]
        if in_str:
            out.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == ",":
            # Look past whitespace: a closer here means this comma is dangling.
            j = i + 1
            while j < n and s[j] in _WS:
                j += 1
            if j < n and s[j] in "}]":
                i += 1  # drop the comma, keep the whitespace/closer
                continue
        out.append(c)
        i += 1
    return "".join(out)


def _close_truncation(s: str) -> str:
    """Close an unterminated JSON document: terminate open strings, strip a
    dangling trailing comma (or complete a dangling colon with null), then
    balance unmatched {/[ in reverse open order."""
    stack: List[str] = []
    in_str = False
    esc = False
    for c in s:
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c in "{[":
                stack.append(c)
            elif c in "}]":
                if stack:
                    stack.pop()
    out = s
    if in_str:
        # A trailing backslash would escape the closing quote ("\ " is an
        # invalid JSON escape). DOUBLE it so it becomes a valid escaped
        # backslash, then close the string.
        if out.endswith("\\"):
            out += "\\"
        out += '"'
    stripped = out.rstrip()
    if stripped.endswith(","):
        out = stripped[:-1]
    elif stripped.endswith(":"):
        out = stripped + "null"
    closers = "".join("}" if c == "{" else "]" for c in reversed(stack))
    return out + closers


def _ladder_candidates(args_str: str) -> List[str]:
    """Build the ordered candidate list (quoted forms + truncation-closed forms)."""
    candidates: List[str] = []
    # Steps 2+3 — quote unquoted keys, then unquoted values.
    s2 = _quote_unquoted_keys(args_str)
    if s2 != args_str:
        candidates.append(s2)
        s3 = _quote_unquoted_values(s2)
        if s3 != s2:
            candidates.append(s3)
    else:
        s3 = _quote_unquoted_values(args_str)
        if s3 != args_str:
            candidates.append(s3)
    # Step 4 — truncation close, applied both to the original and to the
    # best quoted form (a truncated fragment may need BOTH repairs).
    s4_orig = _close_truncation(args_str)
    if s4_orig != args_str:
        candidates.append(s4_orig)
    base = candidates[-1] if candidates else args_str
    s4 = _close_truncation(base)
    if s4 != base:
        candidates.append(s4)
    # Step 5 — dangling comma before a closer. Applied to the original AND to
    # every candidate produced so far: a fragment can need quoting, closing
    # AND comma-stripping together (e.g. '{path: "a",}'), and each earlier
    # rung leaves the dangling comma untouched.
    for cand in [args_str] + list(candidates):
        s5 = _strip_trailing_commas(cand)
        if s5 != cand and s5 not in candidates:
            candidates.append(s5)
    return candidates


def repair_json_arguments(s: str, max_bytes: int = DEFAULT_MAX_BYTES) -> Tuple[str, bool]:
    """Repair a malformed tool-arguments JSON string via the ladder.

    Returns ``(repaired, was_repaired)``. Valid input passes through with
    ``was_repaired=False``. Fail-open: any exception, non-str input, empty
    input, oversize input (> ``max_bytes``) or unrepairable input yields
    ``(s, False)``. NEVER raises.
    """
    try:
        if not isinstance(s, str):
            return s, False  # type: ignore[return-value]
        if max_bytes is not None and max_bytes > 0 and len(s.encode("utf-8", errors="replace")) > max_bytes:
            return s, False
        if not s.strip():
            return s, False
        # Step 1 — fast path: already valid.
        try:
            json.loads(s)
            return s, False
        except Exception:
            pass
        for cand in _ladder_candidates(s):
            try:
                json.loads(cand)
                return cand, True
            except Exception:
                continue
        return s, False
    except Exception:
        return s, False  # type: ignore[return-value]


def repair_tool_calls_argument_strings(tool_calls: List[Any]) -> int:
    """Repair each OpenAI-shaped tool_call's ``function.arguments`` string in place.

    Walks ``tool_calls`` (a list of dicts shaped ``{"function": {"arguments": "<str>"}}``),
    replaces malformed ``arguments`` strings with their repaired form, and
    returns the number of arguments actually repaired. Fail-open: malformed
    container shapes and unrepairable strings are left untouched. NEVER raises.
    """
    try:
        if not isinstance(tool_calls, list):
            return 0
        count = 0
        for tc in tool_calls:
            try:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function")
                if not isinstance(fn, dict):
                    continue
                args = fn.get("arguments")
                if not isinstance(args, str):
                    continue
                repaired, was_repaired = repair_json_arguments(args)
                if was_repaired:
                    fn["arguments"] = repaired
                    count += 1
            except Exception:
                continue
        return count
    except Exception:
        return 0
