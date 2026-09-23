"""Middleware.dedup_viewfile — In-Window view_file Duplicate Pair Stripper

Request-local middleware that strips redundant `view_file` tool pairs from a
Gemini request's `contents` array BEFORE it is converted to OpenAI format.

Motivation (transcript analysis of a 4850-step agent conversation):
    - 928 total view_file calls; 725 (76.2%) re-read a path already read earlier.
    - At a 128k-token window, 632 of those duplicates (87.2%) happened while the
      earlier copy was STILL resident in the model's context window.
    - ~2.33 MB of pure redundancy resident on heavy-tool models such as glm-5.3.

This is a MITIGATION of the model re-reading behavior, not a root-cause fix: the
model still issues the duplicate calls, dedup just makes them cheaper by dropping
the redundant tool pair from the request sent upstream.

CRITICAL INVARIANT — PAIRED REMOVAL IS ATOMIC
    Gemini wire format pairs functionCall <-> functionResponse BY POSITION; there
    is no per-call id on the wire. `app/compat/adapters/gemini.py` mints ids with
    a FIFO queue (`_pending_calls`, see L407-513): each functionResponse is paired
    to the oldest pending functionCall of the same name.

    Therefore removing a functionResponse while leaving its functionCall (or vice
    versa) desynchronizes the FIFO and corrupts every downstream tool_call_id.
    Removal MUST happen for BOTH halves of a pair. A unit test proves no orphan
    half is ever produced.

PART-LEVEL OPERATION
    A Gemini `content` entry carries `role` + `parts[]`. Several parts may share
    one content entry (e.g. text + functionCall, or multiple functionCalls).
    Removal operates at the PART level; a content entry is dropped only after
    part removal leaves it with zero parts.

FAIL-OPEN
    The whole transform is wrapped in try/except. ANY exception returns the
    ORIGINAL request unmodified with zeroed stats. A dedup bug must never break
    an inbound request.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Tuple

__all__ = ["dedup_viewfile_pairs"]

# The IDE tool name for reading a whole file. Matched defensively and
# case-insensitively against the functionCall `name`.
_VIEW_FILE_TOOL = "view_file"

# Argument keys on the view_file functionCall. The Antigravity IDE tool carries
# the target path under `AbsolutePath`. Optional line-range bounds are carried
# under `StartLine` / `EndLine` (whole-file reads omit both).
_PATH_ARG = "AbsolutePath"
_START_LINE_ARG = "StartLine"
_END_LINE_ARG = "EndLine"


def _zero_stats() -> Dict[str, Any]:
    return {"duplicates_removed": 0, "bytes_saved_est": 0, "paths_seen": 0}


def _is_view_file_call(name: str) -> bool:
    """Case-insensitive exact match on the resolved tool name."""
    return isinstance(name, str) and name.strip().lower() == _VIEW_FILE_TOOL


def _normalize_path(raw: Any) -> str:
    """Normalize an AbsolutePath argument value to a canonical comparison key.

    Handles JSON-string encodings (the IDE sometimes passes the path as a JSON
    string rather than a bare value), strips surrounding quotes / backslash
    escapes, and lowercases for Windows case-insensitivity.
    """
    if raw is None:
        return ""
    # If the caller passed a JSON string (e.g. '"D:\\\\a.py"'), decode it first.
    if isinstance(raw, str):
        s = raw.strip()
        # Try json.loads only when it looks quoted/escaped; bare paths (no
        # quotes, no backslashes) are taken verbatim to avoid mangling them.
        if (s.startswith('"') and s.endswith('"')) or "\\\\" in s:
            try:
                decoded = json.loads(s)
                if isinstance(decoded, str):
                    s = decoded
            except (ValueError, TypeError):
                pass
        # Strip any residual surrounding quotes.
        s = s.strip().strip('"').strip("'")
        return s.lower()
    # Non-string scalars: coerce defensively.
    try:
        return str(raw).strip().strip('"').strip("'").lower()
    except Exception:
        return ""


def _extract_path(fc: Dict[str, Any]) -> str:
    """Pull the AbsolutePath out of a functionCall's args, tolerating shape
    variants (dict args, JSON-string args, bare string)."""
    args = fc.get("args")
    if args is None:
        args = fc.get("arguments")
    if args is None:
        return ""

    # args may arrive as a JSON string ({"AbsolutePath": ...}) — parse once.
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (ValueError, TypeError):
            # A bare path string with no wrapping object: treat as the path.
            return _normalize_path(args)

    if isinstance(args, dict):
        return _normalize_path(args.get(_PATH_ARG))

    return _normalize_path(args)


def _range_key(fc: Dict[str, Any]) -> Tuple[Any, Any]:
    """Return the (StartLine, EndLine) bounds used to decide whether two reads
    of the SAME path are actually distinct reads.

    Reads are deduplicated ONLY when their line ranges are equivalent:
      - both unbounded (whole-file reads) -> same range key
      - identical StartLine/EndLine        -> same range key
      - anything else                      -> distinct (kept separately)

    Missing bounds are normalized to None so two whole-file reads collapse.
    """
    args = fc.get("args")
    if args is None:
        args = fc.get("arguments")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (ValueError, TypeError):
            args = {}
    if not isinstance(args, dict):
        args = {}

    def _bound(key: str) -> Any:
        if key not in args:
            return None
        val = args[key]
        if val is None or val == "":
            return None
        return val

    return (_bound(_START_LINE_ARG), _bound(_END_LINE_ARG))


def _response_name(fr: Dict[str, Any]) -> str:
    name = fr.get("name")
    return name.strip() if isinstance(name, str) else ""


def _est_bytes(part: Dict[str, Any]) -> int:
    """Estimate the wire byte cost of a part so stats can report savings.

    Uses the serialized JSON length as a conservative proxy. The exact byte
    count is not load-bearing — only used for the telemetry log line.
    """
    try:
        return len(json.dumps(part, ensure_ascii=True))
    except (TypeError, ValueError):
        return 0


def dedup_viewfile_pairs(
    gemini_request: Dict[str, Any]
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Strip duplicate view_file tool pairs from a Gemini request's contents.

    Walks `contents` in order, part by part, tracking seen normalized
    AbsolutePath values from view_file functionCall parts. The first occurrence
    of a (path, range) is kept and recorded; any later occurrence of the SAME
    (path, range) marks BOTH the functionCall part and its matching
    functionResponse part for atomic removal.

    Request-local and stateless: each inbound request gets an independent
    seen-path set. No session state is retained.

    Args:
        gemini_request: A Gemini (Cloud Code) request dict with a ``contents``
            array. Operates on the same traversal as
            ``gemini_request_to_openai``.

    Returns:
        ``(modified_request, stats_dict)`` where stats_dict has keys
        ``duplicates_removed``, ``bytes_saved_est``, ``paths_seen``. On any
        exception the ORIGINAL request is returned unchanged with zeroed stats
        (fail-open).
    """
    try:
        if not isinstance(gemini_request, dict):
            return gemini_request, _zero_stats()

        contents = gemini_request.get("contents")
        if not isinstance(contents, list) or not contents:
            return gemini_request, _zero_stats()

        # Fast pre-check: if the request declares no tools, there can be no
        # functionCall/functionResponse parts to dedup. (Kept is a soft check —
        # a malformed request with raw parts but no tools array still fails open
        # safely below.)
        tools = gemini_request.get("tools")
        if tools is None and not any(
            isinstance(c, dict) and any(
                isinstance(p, dict) and ("functionCall" in p or "functionResponse" in p)
                for p in (c.get("parts") or [])
            )
            for c in contents
        ):
            return gemini_request, _zero_stats()

        # seen tracks the first occurrence of each (path, range) we KEEP.
        seen: set = set()
        paths_seen = 0

        # pending_removal is a FIFO of tool names whose functionCall was marked
        # for removal and whose matching functionResponse is still outstanding.
        # Mirrors the FIFO discipline of gemini.py _pending_calls (L416-513):
        # a functionResponse pairs to the OLDEST pending call of the same name.
        pending_removal: list = []

        duplicates_removed = 0
        bytes_saved_est = 0

        new_contents: list = []
        for content in contents:
            if not isinstance(content, dict):
                new_contents.append(content)
                continue
            parts = content.get("parts")
            if not isinstance(parts, list):
                new_contents.append(content)
                continue

            new_parts: list = []
            for part in parts:
                if not isinstance(part, dict):
                    new_parts.append(part)
                    continue

                # ── functionCall half ───────────────────────────────────────
                if "functionCall" in part:
                    fc = part.get("functionCall") or {}
                    name = fc.get("name") if isinstance(fc, dict) else None
                    if isinstance(fc, dict) and _is_view_file_call(name):
                        path = _extract_path(fc)
                        rkey = _range_key(fc)
                        dedup_key = (path, rkey) if path else None
                        if dedup_key is not None and dedup_key in seen:
                            # Duplicate read of a path already in-window.
                            # Mark BOTH halves for removal. The functionCall is
                            # dropped now; push its name onto the pending-removal
                            # FIFO so the next matching functionResponse is
                            # removed too (atomic per pair).
                            pending_removal.append((name.strip().lower(),))
                            duplicates_removed += 1
                            bytes_saved_est += _est_bytes(part)
                            # Do NOT append this part -> dropped.
                            continue
                        # First occurrence (or unkeyable): KEEP and record.
                        if dedup_key is not None:
                            seen.add(dedup_key)
                            paths_seen += 1
                    new_parts.append(part)
                    continue

                # ── functionResponse half ───────────────────────────────────
                if "functionResponse" in part:
                    fr = part.get("functionResponse") or {}
                    rname = _response_name(fr).lower()
                    # Pair to the OLDEST pending-removal call of the same name,
                    # exactly as gemini.py pairs functionResponse to functionCall.
                    matched_idx = None
                    for qi, entry in enumerate(pending_removal):
                        if entry[0] == rname:
                            matched_idx = qi
                            break
                    if matched_idx is not None and _is_view_file_call(rname):
                        pending_removal.pop(matched_idx)
                        bytes_saved_est += _est_bytes(part)
                        # Do NOT append -> dropped (paired removal complete).
                        continue
                    # No pending removal matched: this response pairs with a KEPT
                    # call (or is an orphan). Keep it — never orphan a response.
                    new_parts.append(part)
                    continue

                # Any other part type (text, inlineData, fileData, thought):
                # keep verbatim.
                new_parts.append(part)

            # Drop the content entry only if it became empty after part removal.
            if new_parts:
                rebuilt_content = dict(content)
                rebuilt_content["parts"] = new_parts
                new_contents.append(rebuilt_content)
            # else: content had every part removed -> drop the whole entry.

        if duplicates_removed == 0:
            # No mutation occurred; return the original object unchanged so we
            # never replace a request with a structurally-equal copy.
            return gemini_request, {
                "duplicates_removed": 0,
                "bytes_saved_est": 0,
                "paths_seen": paths_seen,
            }

        rebuilt = dict(gemini_request)
        rebuilt["contents"] = new_contents
        return rebuilt, {
            "duplicates_removed": duplicates_removed,
            "bytes_saved_est": bytes_saved_est,
            "paths_seen": paths_seen,
        }
    except Exception:
        # FAIL-OPEN: any error returns the original request untouched. A dedup
        # bug must never break an inbound request.
        try:
            return gemini_request, _zero_stats()
        except Exception:
            return gemini_request, _zero_stats()
