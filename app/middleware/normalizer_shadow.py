"""N2 — normalizer_v2 shadow wiring for the live request path (DEFAULT OFF).

Runs the verified normalizer_v2 round-trip (``to_canonical`` → ``from_canonical``)
on inbound inference bodies and records whether the rebuilt body is semantically
equal to the original:

  * mode "shadow" (default): log one JSONL record per request; body untouched.
  * mode "active": additionally return the rebuilt dict so the caller can swap
    the body in (still logs the record). Fail-open — any exception, any
    non-representable body, or an active-mode loss means the ORIGINAL body is
    kept and None is returned. Inference must never be blocked or delayed by
    shadowing.

Config gate (mirrors the ``tools.conn_trace`` gates in app/main.py):

    (config or {}).get("tools", {}).get("normalizer_v2", {}) →
        {"enabled": false, "mode": "shadow"|"active", "log_mismatches": true,
         "dialects": {"openai-chat": true, "responses": true, "gemini": true}}

Defaults: everything OFF (``enabled`` false; unknown/invalid mode → disabled
with ONE warn log per process). An explicitly flagged-off dialect is skipped
before any work is done.

``log_mismatches`` semantics: True (default) → log every record (the ok:true
records prove shadow coverage); False → log only ok:false divergence records.

Semantic compare (original vs rebuilt) covers, per project spec:
message count + roles, per-part text, image data+media_type(+url), document
fields, tool_use id/name/arguments (dict-equal after JSON parse), tool_result
tool_use_id+content, system text. Extras/unknown top-level field equality is
NOT required — dialect-canonical transformations of the wire shape (e.g. a
Gemini string ``functionResponse`` payload coming back as ``{"result": ...}``)
surface as ok:false; the shadow's job is to report divergence, not judge it.

Log writer (HARD RULE, mirrors app/main.py's capture logger): the JSONL append
is OFF the event loop — the hot path only does ``put_nowait`` with drop-on-full
(a slow disk can never back-pressure inference); the drain runs via
``asyncio.to_thread``; rotation caps the file at 50MB (path → path+".1") with a
boot-time self-heal truncation check on first use. The writer itself is
fail-silent.

This module is import-order safe (no app.main import — that would be circular).
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from app.normalizer_v2 import (  # verified module — read-only dependency
    DIALECT_GEMINI,
    DIALECT_OPENAI_CHAT,
    DIALECT_RESPONSES,
    from_canonical,
    to_canonical,
)

KNOWN_DIALECTS = (DIALECT_OPENAI_CHAT, DIALECT_RESPONSES, DIALECT_GEMINI)

# ── Log writer globals (module-level so tests can monkeypatch path/cap) ──────
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SHADOW_LOG_PATH = os.path.join(_PROJECT_ROOT, ".brain", "logs", "normalizer_shadow.jsonl")
_SHADOW_CAP_BYTES = 50 * 1024 * 1024
_SHADOW_QUEUE_MAX = 256
_shadow_queue: "Optional[asyncio.Queue]" = None
_shadow_writer_task: "Optional[asyncio.Task]" = None
_shadow_boot_healed = False
_invalid_mode_warned = False


# ── Endpoint → dialect mapping (wiring helper) ──────────────────────────────

def endpoint_dialect(path: Any) -> Optional[str]:
    """Route path → normalizer_v2 dialect for the shadow wiring.

    Only the mapped inference endpoints return a dialect; everything else
    (notably /v1/messages + the anthropic mirror — anthropic dialect is not
    built — and the images/videos endpoints) returns None so the caller's
    guard short-circuits before shadow_run is awaited.
    """
    if not isinstance(path, str) or not path:
        return None
    if path.endswith("/chat/completions"):
        return DIALECT_OPENAI_CHAT
    if path == "/v1/responses":
        return DIALECT_RESPONSES
    if "generatecontent" in path.lower():  # covers :generateContent + :streamGenerateContent mirrors
        return DIALECT_GEMINI
    return None


# ── Semantic compare (pure; never raises) ────────────────────────────────────

def _stable(obj: Any) -> str:
    """Ordering-stable JSON rendering for raw/argument comparison."""
    try:
        return json.dumps(obj, sort_keys=True, default=str)
    except Exception:
        return repr(obj)


def _content_semantic(content: Any) -> Tuple:
    """tool_result content → comparable form.

    A list of pure-text parts collapses to its joined string so the
    openai-chat egress join (list → str) compares equal on both sides; any
    non-text part keeps list granularity so real drops (e.g. an image in a
    tool message being text-joined away) are flagged.
    """
    if content is None:
        return ("none",)
    if isinstance(content, str):
        return ("str", content)
    if isinstance(content, list):
        parts = [_part_semantic(p) for p in content if isinstance(p, dict)]
        if parts and all(p[0] == "text" for p in parts):
            return ("str", "".join(p[1] for p in parts))
        return ("parts", tuple(parts))
    return ("raw", _stable(content))


def _part_semantic(part: Dict[str, Any]) -> Tuple:
    """Canonical part → semantic tuple (only spec-listed fields).

    N3 (2026-09-06): thinking/signature coverage. The tuples now carry the
    thinking ``signature`` and the Gemini-attached ``thought_signature``
    (canonical ``part.extras``) for text/thinking/tool_use parts — before this,
    a rebuilt body that DROPPED or REPLACED a thoughtSignature compared EQUAL
    (verified: both missing-sig and divergent-sig pairs returned no mismatch
    paths), so a real reasoning-signature loss was silently reported ok:true.
    Additive only: round-trips preserve these fields on both sides, so every
    previously-clean compare stays clean.
    """
    extras = part.get("extras")
    _sig = extras.get("thought_signature") if isinstance(extras, dict) else None
    ptype = part.get("type")
    if ptype == "text":
        return ("text", part.get("text", ""), _sig)
    if ptype == "thinking":
        return ("thinking", part.get("thinking", ""), part.get("signature"), _sig)
    if ptype == "image":
        return ("image", part.get("url"), part.get("data"), part.get("media_type"))
    if ptype == "document":
        return ("document", part.get("url"), part.get("data"), part.get("media_type"),
                part.get("filename"), part.get("file_id"))
    if ptype == "audio":
        return ("audio", part.get("data"), part.get("media_type"))
    if ptype == "tool_use":
        return ("tool_use", part.get("id"), part.get("name"), _stable(part.get("arguments")), _sig)
    if ptype == "tool_result":
        return ("tool_result", part.get("tool_use_id"), _content_semantic(part.get("content")))
    if ptype == "unknown":
        return ("unknown", _stable(part.get("raw")))
    return (ptype, _stable(part))


def _collapse_text(parts: List[Tuple]) -> List[Tuple]:
    """Merge ADJACENT text segments into one joined segment.

    The openai-chat egress joins an assistant message's text parts into a
    single content string; collapsing on both sides makes that lossless
    (["a","b"] == "ab") instead of a false part-count mismatch.
    """
    out: List[Tuple] = []
    for seg in parts:
        if seg[0] == "text" and out and out[-1][0] == "text":
            out[-1] = ("text", out[-1][1] + seg[1])
        else:
            out.append(seg)
    return out


def _top_level_system_text(dialect: str, body: Dict[str, Any]) -> str:
    """System text that lives OUTSIDE the message list in this dialect body.

    openai-chat's ingress only reads role:system/developer messages, but its
    egress emits a top-level "system" string — without this pickup every
    clean openai-chat body with a system message would flag /system. Responses
    ("instructions") and Gemini ("systemInstruction") are re-read by their
    own ingresses, so no pickup is needed there.
    """
    if dialect != DIALECT_OPENAI_CHAT:
        return ""
    sys_val = body.get("system")
    if isinstance(sys_val, str):
        return sys_val
    if isinstance(sys_val, list):
        return "".join(
            p.get("text", "") for p in sys_val
            if isinstance(p, dict) and isinstance(p.get("text"), str)
        )
    return ""


def _canon_system_text(canonical: Any) -> str:
    try:
        return "".join(
            p.get("text", "") for p in canonical.system
            if isinstance(p, dict) and p.get("type") == "text"
        )
    except Exception:
        return ""


def semantic_mismatches(dialect: str, original: Any, rebuilt: Any) -> List[str]:
    """Pure divergence detector: original vs rebuilt → JSON-pointer-ish paths.

    Compares both bodies through ``to_canonical`` on the semantic fields the
    project spec lists (message count/roles, per-part text, image data+
    media_type, document fields, tool_use id/name/arguments, tool_result
    tool_use_id+content, system text). Extras equality is NOT required.
    Never raises; a compare crash yields a single "/compare_error" path.
    """
    try:
        can_a = to_canonical(dialect, original)
        can_b = to_canonical(dialect, rebuilt)
        paths: List[str] = []

        sys_a = _canon_system_text(can_a) + _top_level_system_text(dialect, original)
        sys_b = _canon_system_text(can_b) + _top_level_system_text(dialect, rebuilt)
        if sys_a != sys_b:
            paths.append("/system")

        msgs_a = can_a.messages or []
        msgs_b = can_b.messages or []
        if len(msgs_a) != len(msgs_b):
            paths.append("/messages/count")
        for i in range(min(len(msgs_a), len(msgs_b))):
            ma, mb = msgs_a[i], msgs_b[i]
            if not isinstance(ma, dict) or not isinstance(mb, dict):
                continue
            if (ma.get("role") or "user") != (mb.get("role") or "user"):
                paths.append(f"/messages/{i}/role")
            # Collapse adjacent text segments ONLY for assistant messages: the
            # openai-chat egress is the sole path that joins an assistant's
            # text parts into a single content string (user messages, responses
            # and gemini egress never join), so collapsing everywhere would
            # mask a genuine unknown→text part flip in user content.
            collapse = (ma.get("role") or "user") == "assistant"
            parts_a = [p for p in (ma.get("parts") or []) if isinstance(p, dict)]
            parts_b = [p for p in (mb.get("parts") or []) if isinstance(p, dict)]
            if collapse:
                parts_a = _collapse_text([_part_semantic(p) for p in parts_a])
                parts_b = _collapse_text([_part_semantic(p) for p in parts_b])
            else:
                parts_a = [_part_semantic(p) for p in parts_a]
                parts_b = [_part_semantic(p) for p in parts_b]
            if len(parts_a) != len(parts_b):
                paths.append(f"/messages/{i}/parts/count")
            for j in range(min(len(parts_a), len(parts_b))):
                if parts_a[j] != parts_b[j]:
                    suffix = "/type" if parts_a[j][0] != parts_b[j][0] else ""
                    paths.append(f"/messages/{i}/parts/{j}{suffix}")
        return paths
    except Exception:
        return ["/compare_error"]


# ── Capped JSONL writer (pattern lifted from app/main.py's capture logger) ────

def _rotate_capped_file(path: str, cap: int) -> None:
    """Rotate path → path+'.1' once it reaches cap bytes. Windows-safe
    (os.replace is atomic and overwrites a stale .1). Never raises."""
    try:
        if os.path.exists(path) and os.path.getsize(path) >= cap:
            os.replace(path, path + ".1")
    except Exception:
        pass


def _shadow_write_direct(rec: dict) -> None:
    """Blocking append with rotation; runs in a worker thread, never raises.

    Rotates BOTH before the append (heals a file that already blew past the
    cap while the router was down) and after it (so the live file ends every
    write strictly under cap — a single record can never leave it at/above).
    """
    try:
        os.makedirs(os.path.dirname(_SHADOW_LOG_PATH), exist_ok=True)
        _rotate_capped_file(_SHADOW_LOG_PATH, _SHADOW_CAP_BYTES)
        with open(_SHADOW_LOG_PATH, "a", encoding="utf-8") as shadow_file:
            shadow_file.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        # Post-append rotation: the live file must end EVERY write under cap
        # (a lone record could leave it at/above). Rotation moves the live
        # file to .1 — recreate it so the log path always resolves.
        _rotate_capped_file(_SHADOW_LOG_PATH, _SHADOW_CAP_BYTES)
        if not os.path.exists(_SHADOW_LOG_PATH):
            open(_SHADOW_LOG_PATH, "a", encoding="utf-8").close()
    except Exception:
        pass


async def _shadow_writer_task() -> None:
    """Background drain: queue → to_thread(direct write). One at a time keeps
    the file append-ordered; the queue absorbs bursts off the event loop."""
    while True:
        rec = await _shadow_queue.get()
        try:
            await asyncio.to_thread(_shadow_write_direct, rec)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass


def _shadow_line(rec: dict) -> None:
    """Non-blocking enqueue for the hot path (put_nowait, drop-on-full).

    When no event loop is running (tests, CLI probes) falls back to a direct
    write so the record still lands.
    """
    global _shadow_queue
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _shadow_write_direct(rec)
        return
    if _shadow_queue is None:
        _shadow_queue = asyncio.Queue(maxsize=_SHADOW_QUEUE_MAX)
    try:
        _shadow_queue.put_nowait(rec)
    except asyncio.QueueFull:
        pass  # drop — shadowing must never stall the request


def _ensure_shadow_writer() -> None:
    """Lazily boot the drain task + fire the boot-time self-heal rotation.

    Called from the first enabled shadow_run in a running loop; cheap and
    idempotent. Recreates the writer when the loop changed (pytest runs one
    loop per test). Never raises.
    """
    global _shadow_queue, _shadow_writer_task, _shadow_boot_healed
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    try:
        if not _shadow_boot_healed:
            _shadow_boot_healed = True
            # Boot self-heal: a restart must immediately truncate a file that
            # already blew past the cap while the router was down.
            loop.create_task(asyncio.to_thread(_rotate_capped_file, _SHADOW_LOG_PATH, _SHADOW_CAP_BYTES))
        if _shadow_queue is None:
            _shadow_queue = asyncio.Queue(maxsize=_SHADOW_QUEUE_MAX)
        task = _shadow_writer_task
        if task is None or task.done() or task.get_loop() is not loop:
            _shadow_writer_task = loop.create_task(_shadow_writer_task())
    except Exception:
        pass


# ── Public API ────────────────────────────────────────────────────────────────

async def shadow_run(dialect: str, body: Any, *, endpoint: str, config: Any,
                     provider: Optional[str] = None, model: Optional[str] = None) -> Optional[dict]:
    """Run the normalizer_v2 round-trip on one inference body.

    Returns the rebuilt dict ONLY in active mode with a semantically clean,
    fully-representable dict body; otherwise returns None (shadow mode, any
    gate off, non-dict body, passthrough/lossy body, or any exception).
    NEVER raises — inference must not be blocked or delayed by shadowing.
    """
    global _invalid_mode_warned
    try:
        cfg = ((config or {}).get("tools") or {}).get("normalizer_v2") or {}
        if not cfg.get("enabled", False):
            return None
        if dialect not in KNOWN_DIALECTS:
            return None
        dialects = cfg.get("dialects") or {}
        if dialect in dialects and not dialects.get(dialect):
            return None
        mode = cfg.get("mode", "shadow")
        if mode not in ("shadow", "active"):
            if not _invalid_mode_warned:
                _invalid_mode_warned = True
                print(
                    f"[NormalizerShadow] invalid tools.normalizer_v2.mode {mode!r} "
                    f"— normalizer_v2 shadowing disabled for this process",
                    flush=True,
                )
            return None
        if not isinstance(body, dict):
            return None  # not a shadowable body — keep the original untouched

        try:
            body_bytes = len(json.dumps(body, ensure_ascii=False, default=str))
        except Exception:
            body_bytes = None

        canonical = to_canonical(dialect, body)
        passthrough = bool(canonical.extras.get("passthrough"))
        rebuilt = from_canonical(dialect, canonical)
        paths = semantic_mismatches(dialect, body, rebuilt)
        ok = (not paths) and not passthrough

        record: Dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "endpoint": endpoint,
            "dialect": dialect,
            "model": model,
            "provider": provider,
            "ok": ok,
            "mismatch_paths": paths[:20],
            "body_bytes": body_bytes,
        }
        if passthrough:
            record["passthrough"] = True
        if (not ok) or cfg.get("log_mismatches", True):
            try:
                _ensure_shadow_writer()
                _shadow_line(record)
            except Exception:
                pass  # writer failure must never surface on the request path

        if mode == "active" and ok and not passthrough and isinstance(rebuilt, dict):
            return rebuilt
        return None
    except Exception as exc:  # fail-open — log and keep the original body
        try:
            _ensure_shadow_writer()
            _shadow_line({
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "endpoint": endpoint,
                "dialect": dialect,
                "model": model,
                "provider": provider,
                "ok": False,
                "error": repr(exc)[:200],
            })
        except Exception:
            pass
        return None
