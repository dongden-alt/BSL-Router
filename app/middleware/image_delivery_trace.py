"""Opt-in, bounded image metadata only. Never hydrates files or remote URLs."""
import base64
import binascii
from collections import deque
from contextvars import ContextVar
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import struct
import tempfile
import threading
import time
import zlib

_ROOT = Path(__file__).resolve().parents[2] / ".brain" / "cc_tasks"
CONTROL = _ROOT / "image-delivery-trace.control.json"
OUTPUT = _ROOT / "image-delivery-trace.metadata.json"
MAX_DEPTH = 16
MAX_NODES = 2048
LATEST_NODES = 512
MAX_BYTES = 8 * 1024 * 1024
MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_PARTS = 128
MAX_MEDIA_PARTS = 128
HEADER_BYTES = 65536
_PHASES = frozenset({"ingress", "adapter_before", "adapter_after", "canonical",
                     "vision_before", "vision_after", "upstream"})
_CHILDREN = ("request", "contents", "messages", "input", "parts", "content",
             "fr", "functionResponse", "response", "result", "output", "tool_result",
             "inlineData", "inline_data", "source", "image", "image_url",
             "fileData", "file_data")
_SCHEMA = tuple(sorted(set(_CHILDREN) | {
    "functionCall", "data", "url", "type", "mimeType", "mime_type", "media_type",
    "role", "tool_calls", "fileUri", "file_uri"}))
_TYPES = frozenset({"image", "image_url", "input_image", "output_image", "tool_result",
                    "tool_use", "function_call", "function_call_output", "text",
                    "input_text", "output_text", "base64", "url"})
_MIMES = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif", "image/bmp"})
_ROLES = frozenset({"user", "assistant", "tool", "model", "system"})
_context = ContextVar("image_delivery_trace_id", default=None)
_started = ContextVar("image_delivery_trace_started", default=None)
_started_window = ContextVar("image_delivery_trace_started_window", default=None)
_lock = threading.Lock()
_records = deque()
_window = None
_deadline = 0.0
_sequence = 0
_selected = None
_selected_at = None
_armed_at = None
_frozen = False
_pinned = set()


def _control():
    global _window, _deadline, _selected, _selected_at, _frozen, _armed_at
    try:
        with CONTROL.open("rb") as stream:
            raw = stream.read(1025)
        if len(raw) > 1024:
            raise ValueError
        cfg = json.loads(raw)
        issued, expires = cfg["issued_at"], cfg["expires_at"]
        count = cfg.get("max_records", 32)
        if (cfg.get("enabled") is not True or type(issued) not in (int, float)
                or type(expires) not in (int, float) or not math.isfinite(issued)
                or not math.isfinite(expires) or type(count) is not int
                or not 1 <= count <= 32 or not 0 < expires - issued <= 900
                or not issued <= time.time() < expires):
            raise ValueError
        watch = cfg.get("watch_sha256")
        arm = cfg.get("arm_next_media", False)
        if (type(arm) is not bool or (watch is not None and
                (not isinstance(watch, str) or re.fullmatch(r"[0-9a-fA-F]{64}", watch) is None))):
            raise ValueError
        dimensions = cfg.get("expected_dimensions")
        if "expected_dimensions" in cfg and (
                not isinstance(dimensions, list) or len(dimensions) != 2
                or any(type(value) is not int or not 1 <= value <= 0x7fffffff
                       for value in dimensions)):
            raise ValueError
        dimensions = tuple(dimensions) if dimensions is not None else None
        watch = watch.lower() if watch else None
        window = (issued, expires, count, watch, arm, dimensions)
        if _window != window:
            _records.clear()
            _pinned.clear()
            _selected = _selected_at = None
            _frozen = False
            _window = window
            _armed_at = time.time()
            _deadline = time.monotonic() + min(900, expires - time.time())
        if time.monotonic() >= _deadline:
            raise ValueError
        return count, watch, arm, dimensions
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        _records.clear()
        _pinned.clear()
        _window = None
        _selected = _selected_at = None
        _frozen = False
        return None


def _bounded_json(text):
    """Reject excessive JSON structure before allocating parsed containers."""
    depth = nodes = 0
    quoted = escaped = False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
            nodes += 1
        elif char in "[{":
            depth += 1
            nodes += 1
        elif char in ",:":
            nodes += 1
        elif char in "]}":
            depth -= 1
        if depth > MAX_DEPTH or nodes > MAX_NODES:
            return None
    return json.loads(text)


def _view(node):
    # Already-materialized Pydantic fields only; no properties or model_dump.
    if type(node).__module__.startswith("app.models"):
        return vars(node)
    return node


def _dimensions(data):
    """Validate bounded PNG IHDR or JPEG SOF headers; never decode pixels."""
    if (len(data) >= 33 and data[:8] == b"\x89PNG\r\n\x1a\n"
            and data[8:16] == b"\x00\x00\x00\x0dIHDR"
            and zlib.crc32(data[12:29]) & 0xffffffff == int.from_bytes(data[29:33], "big")):
        width, height, bits, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", data[16:29])
        valid_bits = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8),
                      4: (8, 16), 6: (8, 16)}
        if (0 < width <= 0x7fffffff and 0 < height <= 0x7fffffff
                and bits in valid_bits.get(color, ()) and compression == filtering == 0
                and interlace in (0, 1)):
            return {"header_format": "png", "width": width, "height": height}
    if data[:2] != b"\xff\xd8":
        return {}
    end = min(len(data), HEADER_BYTES)
    offset = 2
    while offset + 4 <= end:
        if data[offset] != 255:
            break
        offset += 1
        while offset < end and data[offset] == 255:
            offset += 1
        if offset + 3 > end:
            break
        marker = data[offset]
        offset += 1
        if marker in (0, 0xd8, 0xd9, 0xda):
            break
        if marker == 1 or 0xd0 <= marker <= 0xd7:
            continue
        length = int.from_bytes(data[offset:offset + 2], "big")
        if length < 2 or offset + length > end:
            break
        if marker in (0xc0, 0xc1, 0xc2):
            if length < 8:
                break
            bits, height, width, components = struct.unpack(">BHHB", data[offset + 2:offset + 8])
            if (bits in (8, 12) and width and height and components in (1, 3, 4)
                    and length == 8 + 3 * components):
                return {"header_format": "jpeg", "width": width, "height": height}
            break
        offset += length
    return {}


def _list_children(node, path, depth, serialized):
    # Lazy newest-first iteration: no full-history copy or pending-node queue.
    for index in range(len(node) - 1, -1, -1):
        yield node[index], f"{path}[{index}]", depth + 1, serialized


def _walk(payload, node_limit, path="$", present=True):
    report = {"media_count": 0, "serialized_media_count": 0, "reference_count": 0,
              "invalid_count": 0, "truncated": False, "nodes": 0, "parts": [],
              "schema_counts": {}}
    budget = MAX_BYTES
    stack = [iter(((payload, path, 0, False),))] if present else []
    seen = {}
    details = structures = 0

    def part(path, kind, media=False, **fields):
        nonlocal details, structures
        if media:
            if details >= MAX_MEDIA_PARTS:
                report["truncated"] = True
                return
            details += 1
        else:
            if structures >= MAX_PARTS:
                report["truncated"] = True
                return
            structures += 1
        report["parts"].append({"path": path, "kind": kind, **fields})

    def image(data, mime, path, serialized):
        nonlocal budget
        report["serialized_media_count" if serialized else "media_count"] += 1
        fields = {"serialized": serialized}
        if isinstance(mime, str) and mime in _MIMES:
            fields["mime"] = mime
        else:
            report["invalid_count"] += 1
            part(path, "image", media=True, status="invalid_mime", **fields)
            return
        if not isinstance(data, str):
            report["invalid_count"] += 1
            part(path, "image", media=True, status="invalid_data", **fields)
            return
        size = len(data)
        if size > 4 * ((MAX_IMAGE_BYTES + 2) // 3) or size > budget:
            report["truncated"] = True
            part(path, "image", media=True, status="oversize", **fields)
            return
        budget -= size
        try:
            decoded = base64.b64decode(data, validate=True)
            if len(decoded) > MAX_IMAGE_BYTES:
                raise ValueError
        except (ValueError, binascii.Error):
            report["invalid_count"] += 1
            part(path, "image", media=True, status="invalid_base64", **fields)
            return
        part(path, "image", media=True, status="decoded", decoded_bytes=len(decoded),
             sha256=hashlib.sha256(decoded).hexdigest(), **_dimensions(decoded), **fields)

    while stack:
        try:
            node, path, depth, serialized = next(stack[-1])
        except StopIteration:
            stack.pop()
            continue
        if report["nodes"] >= node_limit or budget <= 0:
            report["truncated"] = True
            break
        report["nodes"] += 1
        if depth > MAX_DEPTH:
            report["truncated"] = True
            continue
        node = _view(node)
        if isinstance(node, (dict, list)):
            if id(node) in seen:
                continue
            seen[id(node)] = node
        if isinstance(node, dict):
            if node.get("role") in ("system", "developer"):
                continue
            for key in _SCHEMA:
                if key in node:
                    report["schema_counts"][key] = report["schema_counts"].get(key, 0) + 1
            kind = node.get("type")
            if isinstance(kind, str) and kind in _TYPES:
                part(path, kind)
            role = node.get("role")
            if isinstance(role, str) and role in _ROLES:
                part(path, "role", role=role)
            if "data" in node and ("mimeType" in node or "mime_type" in node):
                image(node["data"], node.get("mimeType", node.get("mime_type")), path, serialized)
            elif kind == "base64" and "data" in node:
                image(node["data"], node.get("media_type"), path, serialized)
            for key in ("url", "image_url", "fileUri", "file_uri"):
                value = node.get(key)
                if isinstance(value, str):
                    if value.startswith("data:"):
                        if len(value) > budget or len(value) > 3 * MAX_IMAGE_BYTES:
                            report["truncated"] = True
                            continue
                        header, sep, data = value.partition(",")
                        mime = header[5:-7] if header.endswith(";base64") else None
                        image(data if sep else None, mime, path, serialized)
                    else:
                        report["reference_count"] += 1
                        part(path, "reference", media=True)
            # Only a fixed number of allowlisted children can be materialized.
            stack.append(iter(tuple((node[key], f"{path}.{key}", depth + 1, serialized)
                                    for key in _CHILDREN if key in node and
                                    not (key == "image_url" and isinstance(node[key], str)))))
        elif isinstance(node, list):
            stack.append(_list_children(node, path, depth, serialized))
        elif isinstance(node, str):
            size = len(node)
            if size > budget:
                report["truncated"] = True
                continue
            budget -= size
            if size <= 3 * MAX_IMAGE_BYTES and node.startswith(("{", "[")):
                try:
                    parsed = _bounded_json(node)
                except (ValueError, RecursionError):
                    report["truncated"] = True
                    continue
                if parsed is None:
                    report["truncated"] = True
                    continue
                # Keep parsed containers alive while their identities are tracked.
                stack.append(iter(((parsed, path + ".json", depth + 1, True),)))
    report["complete"] = not report["truncated"]
    return report


def _latest(payload):
    node, path = _view(payload), "$"
    for _ in range(MAX_DEPTH):
        if not isinstance(node, dict):
            break
        request = _view(node.get("request"))
        if isinstance(request, dict):
            node, path = request, path + ".request"
            continue
        for key in ("contents", "messages", "input"):
            items = node.get(key)
            if isinstance(items, list) and items:
                return items[-1], f"{path}.{key}[{len(items) - 1}]", True
        break
    if isinstance(node, list) and node:
        return node[-1], f"{path}[{len(node) - 1}]", True
    return None, path, False


def summarize(payload):
    """Recent-first history plus a separately bounded latest message subtree."""
    latest, path, present = _latest(payload)
    report = _walk(payload, MAX_NODES)
    recent = _walk(latest, LATEST_NODES, path, present)
    recent["present"] = present
    recent["scope"] = "latest_message_subtree"
    recent["path"] = path
    # Local assertion only: never means whole-request absence.
    recent["no_media_in_inspected_subtree"] = bool(
        present and recent["complete"] and not recent["invalid_count"]
        and not recent["reference_count"] and not recent["media_count"]
        and not recent["serialized_media_count"])
    view = _view(latest)
    recent["tool_result"] = bool(isinstance(view, dict) and (
        view.get("role") == "tool" or view.get("type") in
        ("tool_result", "function_call_output") or
        recent["schema_counts"].get("functionResponse", 0) or
        recent["schema_counts"].get("fr", 0) or any(
            p["kind"] in ("tool_result", "function_call_output") for p in recent["parts"])))
    report["latest"] = recent
    return report


def _has_media(report):
    return bool(report["media_count"] or report["serialized_media_count"]
                or report["reference_count"])


def _atomic_write(document):
    if OUTPUT.is_symlink():
        raise OSError("diagnostic destination unavailable")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=OUTPUT.parent,
                                         prefix=OUTPUT.name + ".", suffix=".tmp",
                                         delete=False) as stream:
            temporary = stream.name
            json.dump(document, stream, separators=(",", ":"))
            stream.flush()
        os.replace(temporary, OUTPUT)
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def trace(phase, payload, *, begin=False):
    """Default-disabled opaque correlation capture; never changes inference data."""
    global _sequence, _selected, _selected_at, _frozen
    try:
        if phase not in _PHASES:
            return
        with _lock:
            control = _control()
            if control is None:
                if begin:
                    _context.set(None)
                    _started.set(None)
                    _started_window.set(None)
                return
            limit, watch, arm, dimensions = control
            if _frozen:
                return
            correlation = _context.get()
            if begin or correlation is None:
                correlation = secrets.token_hex(16)
                _context.set(correlation)
                _started.set(time.time() if begin else None)
                _started_window.set(_window if begin else None)
            if arm and _selected is None and (
                    _started.get() is None or _started_window.get() != _window
                    or _started.get() < _armed_at):
                return
            if arm and _selected is not None and correlation != _selected:
                return
            report = summarize(payload)
            timestamp = time.time()
            if arm and _selected is None:
                if watch or dimensions:
                    if not (report["latest"]["present"] and any(
                            p["kind"] == "image" and p.get("status") == "decoded" and (
                                (watch is not None and p.get("sha256") == watch) or
                                (dimensions is not None and
                                 (p.get("width"), p.get("height")) == dimensions))
                            for p in report["latest"]["parts"])):
                        return
                elif not (_has_media(report) or _has_media(report["latest"])
                          or report["latest"]["tool_result"]):
                    return
                _selected, _selected_at = correlation, timestamp
                _records.clear()
                _pinned.clear()
            if watch and any(p.get("sha256") == watch for section in
                             (report, report["latest"]) for p in section["parts"]):
                _pinned.add(correlation)
            _sequence += 1
            record = {"phase": phase, "correlation": correlation,
                      "timestamp": timestamp, "sequence": _sequence, **report}
            # Preserve the latest observation of each phase in protected requests.
            if correlation == _selected or correlation in _pinned:
                for old in tuple(_records):
                    if old["correlation"] == correlation and old["phase"] == phase:
                        _records.remove(old)
            _records.append(record)
            while len(_records) > limit:
                protected = _pinned | ({_selected} if _selected else set())
                media_ids = {r["correlation"] for r in _records if _has_media(r)}
                victim = next((r for r in _records if r["correlation"] not in protected
                               and r["correlation"] not in media_ids), None)
                if victim is None:
                    victim = next((r for r in _records if r["correlation"] not in protected), None)
                if victim is None:
                    victim = _records[0] if correlation in protected else record
                _records.remove(victim)
            _pinned.intersection_update(r["correlation"] for r in _records)
            freeze = bool(arm and correlation == _selected and phase == "upstream")
            groups = []
            for r in _records:
                group = next((g for g in groups if g["correlation"] == r["correlation"]), None)
                if group is None:
                    group = {"correlation": r["correlation"], "phases": [],
                             "first_timestamp": r["timestamp"], "last_timestamp": r["timestamp"],
                             "has_media": False, "watch_match": r["correlation"] in _pinned}
                    groups.append(group)
                group["phases"].append({"phase": r["phase"], "sequence": r["sequence"]})
                group["last_timestamp"] = r["timestamp"]
                group["has_media"] |= _has_media(r)
            _atomic_write({"schema_version": 2, "records": list(_records), "groups": groups,
                           "capture": {"armed": arm, "armed_at": _armed_at,
                                       "selected_correlation": _selected,
                                       "selected_at": _selected_at, "frozen": freeze,
                                       "expires_at": _window[1]}})
            _frozen = freeze
    except Exception:
        # Diagnostics must never affect inference or disclose exception values.
        return
