"""AWS event-stream binary frame decoder for Kiro (CodeWhisperer) responses.

Kiro's /generateAssistantResponse returns application/vnd.amazon.eventstream
BINARY frames, not text SSE. Empirically confirmed 2026-08-25: direct 200
responses begin with frames like b"\\x00\\x00\\x00\\x9a...:event-type\\x07\\x00\\x16
assistantResponseEvent". BSL's text-SSE parser read zero content from them,
which the zombie guard killed as "zombie_empty_response" 504s.

Frame layout (https://docs.aws.amazon.com/transcribe/latest/dg/event-stream.html):
    total-len(4 BE) headers-len(4 BE) prelude-crc(4) headers payload crc(4)

Header entry: name-len(1) name value-type(1) value.
Value types used by Kiro: 7=string(len2+bytes), 5=int32, 4=int16, 6=int64,
8=timestamp, 2/3=bool.
"""
from __future__ import annotations

import json
import time
from typing import Iterable


def parse_eventstream_headers(blob: bytes) -> dict[str, str]:
    """Parse the header block of an AWS event-stream frame (string/int values)."""
    headers: dict[str, str] = {}
    pos = 0
    end = len(blob)
    while pos + 2 <= end:
        name_len = blob[pos]
        if name_len == 0 or pos + 1 + name_len + 1 > end:
            break
        name = blob[pos + 1: pos + 1 + name_len].decode("utf-8", errors="replace")
        vtype = blob[pos + 1 + name_len]
        vpos = pos + 2 + name_len
        if vtype == 7:  # string: len(2 BE) + bytes
            if vpos + 2 > end:
                break
            vlen = int.from_bytes(blob[vpos: vpos + 2], "big")
            if vpos + 2 + vlen > end:
                break
            headers[name] = blob[vpos + 2: vpos + 2 + vlen].decode("utf-8", errors="replace")
            pos = vpos + 2 + vlen
        elif vtype in (4, 5, 6, 8):
            width = {4: 2, 5: 4, 6: 8, 8: 8}[vtype]
            if vpos + width > end:
                break
            headers[name] = str(int.from_bytes(blob[vpos: vpos + width], "big"))
            pos = vpos + width
        elif vtype == 2:
            headers[name] = "true"
            pos = vpos
        elif vtype == 3:
            headers[name] = "false"
            pos = vpos
        else:
            break  # unsupported type - stop safely
    return headers


def parse_eventstream_frames(buf: bytes) -> tuple[list[tuple[str, bytes]], bytes]:
    """Extract complete AWS event-stream frames from buf.

    Returns ([(event_type, payload_bytes), ...], remainder). Remainder keeps
    partial-frame bytes for the next call. Malformed data returns ([], buf)
    so callers can fall back to text-SSE parsing.
    """
    events: list[tuple[str, bytes]] = []
    pos = 0
    total = len(buf)
    while pos + 16 <= total:
        frame_len = int.from_bytes(buf[pos: pos + 4], "big")
        if frame_len < 16 or frame_len > 16_000_000:
            return [], buf
        if pos + frame_len > total:
            break  # partial frame
        header_len = int.from_bytes(buf[pos + 4: pos + 8], "big")
        if header_len > frame_len - 16:
            return [], buf
        headers = parse_eventstream_headers(buf[pos + 12: pos + 12 + header_len])
        payload = buf[pos + 12 + header_len: pos + frame_len - 4]
        events.append((headers.get(":event-type", ""), payload))
        pos += frame_len
    return events, buf[pos:]


def is_eventstream(buf: bytes) -> bool:
    """Heuristic: buf begins with a plausible event-stream frame."""
    if len(buf) < 16 or buf[0] != 0:
        return False
    frame_len = int.from_bytes(buf[0:4], "big")
    header_len = int.from_bytes(buf[4:8], "big")
    return 16 <= frame_len <= 16_000_000 and header_len <= frame_len - 16


def frame_to_openai_chunk(event_type: str, payload: bytes) -> dict | None:
    """Convert one decoded event-stream frame payload to an OpenAI chunk dict."""
    try:
        data = json.loads(payload.decode("utf-8")) if payload else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return kiro_event_to_openai_chunk(event_type, data)


def eventstream_to_openai_completion(raw: bytes) -> dict | None:
    """Decode a complete binary event-stream response into ONE OpenAI completion."""
    events, _ = parse_eventstream_frames(raw)
    if not events:
        return None
    content_parts: list[str] = []
    usage: dict = {}
    model_id = "kiro"
    for event_type, payload in events:
        try:
            data = json.loads(payload.decode("utf-8")) if payload else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        et = event_type.lower()
        if "responseevent" in et:
            c = data.get("content")
            if c:
                content_parts.append(c)
            m = data.get("modelId")
            if m:
                model_id = m
        elif "metadataevent" in et:
            u = data.get("usage") or {}
            usage = {
                "prompt_tokens": u.get("inputTokens", 0),
                "completion_tokens": u.get("outputTokens", 0),
                "total_tokens": u.get("inputTokens", 0) + u.get("outputTokens", 0),
            }
    if not content_parts and not usage:
        return None
    return {
        "id": f"chatcmpl-kiro-{id(raw)}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model_id,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "".join(content_parts)},
            "finish_reason": "stop",
        }],
        "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


async def eventstream_to_openai_sse_lines(raw_iter):
    """Async generator: binary event-stream chunks -> OpenAI SSE bytes.

    Handles arbitrary byte boundaries from httpx aiter_raw (frames may split
    mid-chunk); buffers partial frames until complete.
    """
    # Imported lazily to avoid a circular import at module load.
    from app.kiro_adapter import kiro_event_to_openai_chunk

    buf = b""
    async for chunk in raw_iter:
        if not chunk:
            continue
        buf += chunk
        while True:
            events, buf = parse_eventstream_frames(buf)
            if not events:
                break
            for event_type, payload in events:
                try:
                    data = json.loads(payload.decode("utf-8")) if payload else {}
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if not isinstance(data, dict):
                    continue
                oai = kiro_event_to_openai_chunk(event_type, data)
                if oai:
                    yield f"data: {json.dumps(oai, separators=(',', ':'))}\n\n".encode("utf-8")


async def _chain_chunks(head: list[bytes], raw_iter):
    for c in head:
        yield c
    async for c in raw_iter:
        yield c


async def eventstream_to_openai_sse_lines_with_fallback(raw_iter):
    """Sniff first bytes: binary event-stream -> new decoder; else legacy text SSE."""
    from app import kiro_adapter

    head: list[bytes] = []
    async for chunk in raw_iter:
        if chunk:
            head.append(chunk)
            if sum(len(c) for c in head) >= 16:
                break
    if not head:
        return
    if is_eventstream(b"".join(head)):
        async for out in eventstream_to_openai_sse_lines(_chain_chunks(head, raw_iter)):
            yield out
    else:
        async for out in kiro_adapter.kiro_raw_to_openai_sse(_chain_chunks(head, raw_iter)):
            yield out



