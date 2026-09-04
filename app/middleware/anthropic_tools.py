"""
Middleware.anthropic_tools — same-dialect (Anthropic→Anthropic) tool-call hygiene.

WHY THIS EXISTS (2026-09-03): GLM-5.x served through Anthropic-native channels
(vsllm-a/glm-5.2-anthropic, the coder-2 combo backing Claude Code) intermittently
emits malformed tool-argument JSON — unquoted values ({"Includes": *.js}), unquoted
keys, truncated objects. Every other GLM repair layer (normalize_glm_tool_calls,
StreamNormalizer + _repair_tool_input in app/compat/stream_normalizer.py) only
runs on CROSS-DIALECT paths (OpenAI↔Anthropic conversion). The Anthropic→Anthropic
passthrough forwarded those bytes verbatim; Claude Code failed to parse them
("invalid tool call error (invalid_args)") and burned retries on every hit.

Safety contract (mirrors app/middleware/glm_tools.py):
  1. Pure transformation; no retries, no timers, no threads.
  2. Fail-open: any exception forwards the original bytes/JSON unchanged.
  3. Never swallows bytes: held events are always flushed (repaired or verbatim).
  4. Activation is gated at the call site: client=Anthropic AND upstream=
     Anthropic-fmt AND the request declares tools. Kill-switch:
     config ``tools.anthropic_tool_repair: false`` (default enabled).
  5. Bounded memory: per-block cap (256 KB default) — beyond it the block
     degrades to verbatim passthrough; never buffer unbounded.
"""

import json
import re
from typing import Any, Dict, List, Optional, Tuple


# ── JSON repair ladder ──────────────────────────────────────────────────────
# Each step is validated by json.loads before its output is accepted; a step
# that would corrupt a document can never win because step 1 already returned
# valid documents untouched. A repaired string is only returned if it PARSES.

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.\-]*")
_NUMBER_RE = re.compile(r"^-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?$")
_JSON_LITERALS = frozenset({"true", "false", "null"})
_WS = " \t\r\n"


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


def repair_tool_input_json(args_str: str) -> Optional[str]:
    """Best-effort repair of a malformed tool-arguments JSON string.

    Returns the first candidate that parses as JSON, or None when nothing
    parseable can be produced (caller must then forward the original bytes).
    NEVER raises.
    """
    try:
        if not isinstance(args_str, str) or not args_str.strip():
            return None
        # Step 1 — fast path: already valid.
        try:
            json.loads(args_str)
            return args_str
        except Exception:
            pass
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
        for cand in candidates:
            try:
                json.loads(cand)
                return cand
            except Exception:
                continue
        return None
    except Exception:
        return None


# ── Streaming guard ─────────────────────────────────────────────────────────


class AnthropicStreamToolGuard:
    """Incremental SSE filter for the Anthropic→Anthropic streaming passthrough.

    Holds each tool_use block's ``input_json_delta`` events from
    ``content_block_start`` until ``content_block_stop``; at stop time the
    accumulated ``partial_json`` string is validated. Valid → the original
    events are flushed byte-exact. Invalid → one repaired replacement delta is
    emitted (when repair succeeds), else the originals are flushed verbatim
    (fail-open). All non-tool events pass through immediately, so text/thinking
    latency is unaffected and cross-event ordering is preserved.

    Framing is delimiter-driven (no timers). Feed() may return [] while a block
    is held; flush() at stream end releases anything still held, verbatim.
    """

    def __init__(self, tools_in_request: bool, max_block_bytes: int = 262144):
        self._active = bool(tools_in_request)
        self._max = max_block_bytes
        self._buf = bytearray()
        # block index -> {"events": [bytes...], "fragments": str, "bytes": int}
        self._held: Dict[int, Dict[str, Any]] = {}
        # Indices flushed due to the memory cap — never held again.
        self._capped: set = set()

    # -- public API ----------------------------------------------------------

    def feed(self, chunk: bytes) -> List[bytes]:
        if not self._active:
            return [chunk]
        try:
            self._buf.extend(chunk)
            return self._drain(final=False)
        except Exception:
            # Fail-open: dump the buffer verbatim and deactivate holding.
            self._active = False
            out = [bytes(self._buf)]
            self._buf.clear()
            return out

    def flush(self) -> List[bytes]:
        if not self._active:
            rest = bytes(self._buf)
            self._buf.clear()
            return [rest] if rest else []
        try:
            out = self._drain(final=True)
            # Blocks that never saw content_block_stop: fail-open verbatim.
            for idx in sorted(self._held.keys()):
                blk = self._held.pop(idx)
                out.extend(blk["events"])
            if self._buf:
                out.append(bytes(self._buf))
                self._buf.clear()
            return out
        except Exception:
            out: List[bytes] = []
            for blk in self._held.values():
                out.extend(blk["events"])
            self._held.clear()
            if self._buf:
                out.append(bytes(self._buf))
                self._buf.clear()
            return out

    # -- internals -----------------------------------------------------------

    def _drain(self, final: bool) -> List[bytes]:
        out: List[bytes] = []
        lines = self._buf.split(b"\n")
        self._buf = bytearray(lines.pop())  # incomplete tail stays buffered
        i = 0
        while i < len(lines):
            j = i
            while j < len(lines) and lines[j].strip(b"\r") != b"":
                j += 1
            if j >= len(lines):
                # No blank terminator yet — incomplete event. Push back (only
                # possible at the very end of the line list).
                if not final:
                    self._buf = bytearray(
                        b"\n".join(lines[i:]) + b"\n" + bytes(self._buf)
                    )
                    break
                group = lines[i:]
                if group:
                    out.extend(self._handle_event(group))
                break
            group = lines[i:j] + [lines[j]]  # include the blank terminator line
            out.extend(self._handle_event(group))
            i = j + 1
        return out

    def _handle_event(self, group: List[bytes]) -> List[bytes]:
        # Byte-exact reconstruction: every split line consumed one '\n'.
        event_bytes = b"".join(line + b"\n" for line in group)
        # Cheap prefilter — do NOT json-parse the hot path (every text/thinking
        # delta must pass through this class with zero parsing overhead).
        if not (
            b"tool_use" in event_bytes
            or b"input_json_delta" in event_bytes
            or b"content_block_stop" in event_bytes
        ):
            return [event_bytes]
        data_line = None
        for line in group:
            if line.startswith(b"data:"):
                data_line = line
                break
        if data_line is None:
            return [event_bytes]
        try:
            ev = json.loads(data_line[5:].strip().decode("utf-8"))
        except Exception:
            return [event_bytes]  # not JSON — passthrough
        if not isinstance(ev, dict):
            return [event_bytes]
        etype = ev.get("type", "")
        idx = ev.get("index")

        if etype == "content_block_start":
            blk = ev.get("content_block")
            if (
                isinstance(blk, dict)
                and blk.get("type") == "tool_use"
                and isinstance(idx, int)
                and idx not in self._capped
            ):
                self._held[idx] = {
                    "events": [event_bytes],
                    "fragments": "",
                    "bytes": len(event_bytes),
                }
                return []  # held
            return [event_bytes]

        if etype == "content_block_delta":
            delta = ev.get("delta")
            if isinstance(delta, dict) and delta.get("type") == "input_json_delta":
                if isinstance(idx, int) and idx in self._held:
                    blk = self._held[idx]
                    pj = delta.get("partial_json", "") or ""
                    if isinstance(pj, str):
                        blk["fragments"] += pj
                    blk["events"].append(event_bytes)
                    blk["bytes"] += len(event_bytes)
                    if blk["bytes"] > self._max:
                        self._capped.add(idx)
                        flushed = blk["events"]
                        del self._held[idx]
                        print(
                            f"[AnthropicToolRepair] block {idx} exceeded "
                            f"{self._max}B cap — passthrough",
                            flush=True,
                        )
                        return flushed
                    return []
            return [event_bytes]

        if etype == "content_block_stop":
            if isinstance(idx, int) and idx in self._held:
                blk = self._held.pop(idx)
                combined = blk["fragments"]
                repaired: Optional[str] = None
                try:
                    json.loads(combined)
                except Exception:
                    repaired = repair_tool_input_json(combined)
                    if repaired is not None:
                        try:
                            json.loads(repaired)
                        except Exception:
                            repaired = None
                if repaired is not None and repaired != combined:
                    # Emit the block's start event, ONE replacement delta with
                    # the full repaired JSON, then the stop event verbatim.
                    out = [blk["events"][0]]
                    repl = {
                        "type": "content_block_delta",
                        "index": idx,
                        "delta": {
                            "type": "input_json_delta",
                            "partial_json": repaired,
                        },
                    }
                    out.append(
                        (
                            "event: content_block_delta\ndata: "
                            + json.dumps(repl, ensure_ascii=False)
                            + "\n\n"
                        ).encode("utf-8")
                    )
                    out.append(event_bytes)
                    print(
                        f"[AnthropicToolRepair] stream repaired tool_use block {idx}",
                        flush=True,
                    )
                    return out
                # Valid already, or unrepairable — verbatim fail-open.
                return blk["events"] + [event_bytes]
            return [event_bytes]

        return [event_bytes]


# ── Non-streaming repair ────────────────────────────────────────────────────


def repair_anthropic_response_tool_uses(resp: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
    """Repair malformed ``tool_use.input`` in a non-streaming Anthropic response.

    ``input`` given as a string is parsed (repaired when needed) into the dict
    the Anthropic schema requires. Dict inputs are untouched. Returns
    ``(resp, mutated)``; NEVER raises.
    """
    try:
        content = resp.get("content")
        if not isinstance(content, list):
            return resp, False
        mutated = False
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            inp = block.get("input")
            if not isinstance(inp, str):
                continue
            try:
                block["input"] = json.loads(inp)
                mutated = True
            except Exception:
                repaired = repair_tool_input_json(inp)
                if repaired is not None:
                    try:
                        block["input"] = json.loads(repaired)
                        mutated = True
                    except Exception:
                        pass
        return resp, mutated
    except Exception:
        return resp, False


# ── Config gate ─────────────────────────────────────────────────────────────


def anthropic_tool_repair_enabled(config: Optional[Dict[str, Any]]) -> bool:
    """Kill-switch: ``tools.anthropic_tool_repair: false`` in config.yaml.
    Absent key (or any error reading it) → enabled (fail-open to the fix)."""
    try:
        tools_cfg = (config or {}).get("tools") or {}
        return bool(tools_cfg.get("anthropic_tool_repair", True))
    except Exception:
        return True
