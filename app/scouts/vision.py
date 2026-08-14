"""
Scout.vision — Vision Capability Polyfill

Intercepts incoming ChatCompletionRequests targeting text-only models.
If the payload contains image_url blocks, sends each image to a cheap
Vision model, retrieves a text description, and replaces the image_url
block with that description so the target model can "see" the image
through text.

ARCHITECTURE NOTE (2026-08-03): This scout NEVER calls BSL Router's own
HTTP endpoint. Doing so caused a hard deadlock — the self-call re-entered
the full routing pipeline (combo resolution + a 90s per-leaf header probe)
while the outer request still held its slot, exhausting the shared
connection pool and saturating the event loop. Every request on the server
froze, including native Gemini traffic.

Instead, combo aliases are expanded IN-PROCESS into a list of concrete
(connection, model, provider) candidates, which are then tried directly
against their upstream providers with per-leaf fallback.
"""

import httpx
import json
import asyncio
import hashlib
import time
from app.models import ChatCompletionRequest, MessageContentPart
from app.utils.url_normalization import build_custom_text_upstream_url


# ── Image description cache (avoids re-describing same image on IDE retries) ──
# Keyed by SHA256(image_url). TTL-based eviction prevents stale entries.
_VISION_CACHE: dict[str, tuple[float, str]] = {}
_VISION_CACHE_TTL = 300  # 5 minutes


def clear_vision_cache():
    """Clear the image description cache. Used by tests."""
    _VISION_CACHE.clear()


# Per-attempt upstream timeout. Deliberately short: the Vision Scout runs
# INLINE inside the client's request, so a long wait here stalls the IDE.
# A dead leaf must be abandoned fast so the next candidate gets a turn.
# Default lowered from 60→15s (2026-08-14): with 4 candidates at 60s each,
# only 2 fit the 120s budget. At 15s each, all 4 get tried in 60s.
DEFAULT_VISION_TIMEOUT_S = 15.0

# Hard ceiling on wall-clock time for the ENTIRE polyfill, across all images
# and all fallback attempts. This is the last line of defence against an IDE
# freeze: whatever happens upstream, the client's request resumes by now.
# Default 120→65s (2026-08-14): at 15s/attempt all 4 candidates fit
# (4 × 15s = 60s + 5s margin) while still bounding total IDE stall.
DEFAULT_VISION_TOTAL_BUDGET_S = 65.0

# Hard ceiling on how many chain leaves we will try for a single image.
# Deliberately lower than a typical combo chain length: this scout runs INLINE
# in the client's request, so every extra attempt is latency the IDE feels.
# Leaves beyond this index are unreachable by design — order the vision combo
# with its most reliable providers first.
MAX_VISION_ATTEMPTS = 4

# Wire formats this scout can speak. _build_vision_payload emits an OpenAI
# multimodal body and _describe_image_once sends Bearer auth, so candidates on
# any other format are filtered out at resolution time rather than dialed with
# the wrong protocol.
_VISION_SUPPORTED_FORMATS = frozenset({"openai", "openai-responses"})

# Guard against cyclic nested-combo definitions.
_MAX_COMBO_DEPTH = 5

# Substituted for an individual image that could not be described, when at
# least one OTHER image in the same request succeeded.
PLACEHOLDER_UNREADABLE = "[Vision Scout: this image could not be read]"


class VisionPolyfillFailed(Exception):
    """Every vision candidate failed for every image in the request.

    Raised so the caller can abort BEFORE dispatching to the (expensive)
    target model. Without this, the target model would be asked to reason
    about an error string standing in for the image, wasting a full
    generation and returning a confused answer.
    """


# Fallback vision model used when tools.vision_bridge_model is not configured.
# The VISION_CAPABLE_PATTERNS gate was intentionally removed (the bridge now
# describes images for EVERY target model, vision-capable or not), but this
# default is still required by polyfill_vision().
DEFAULT_VISION_MODEL = "gemini-2.5-flash"


def _extract_image_urls(request: ChatCompletionRequest) -> list[tuple[int, int, dict]]:
    """
    Scan all messages for image content parts.
    Handles BOTH OpenAI format (image_url) and Anthropic format (image+source).
    Returns list of (message_index, part_index, image_url_dict) tuples where
    image_url_dict always has a "url" key (data URL for Anthropic format).
    """
    hits = []
    for msg_idx, msg in enumerate(request.messages):
        if msg.content is None or isinstance(msg.content, str):
            continue
        if isinstance(msg.content, list):
            for part_idx, part in enumerate(msg.content):
                # --- OpenAI format: {"type": "image_url", "image_url": {"url": ...}} ---
                if isinstance(part, dict) and part.get("type") == "image_url" and part.get("image_url"):
                    hits.append((msg_idx, part_idx, part["image_url"]))
                elif isinstance(part, MessageContentPart) and part.type == "image_url" and part.image_url:
                    hits.append((msg_idx, part_idx, part.image_url))
                # --- Anthropic format: {"type": "image", "source": {"type": "base64", "media_type": "...", "data": "..."}} ---
                elif isinstance(part, dict) and part.get("type") == "image" and part.get("source"):
                    src = part["source"]
                    if isinstance(src, dict) and src.get("type") == "base64" and src.get("data"):
                        media_type = src.get("media_type", "image/png")
                        data_url = f"data:{media_type};base64,{src['data']}"
                        hits.append((msg_idx, part_idx, {"url": data_url, "detail": "high"}))
                elif isinstance(part, MessageContentPart) and part.type == "image" and part.source:
                    src = part.source if isinstance(part.source, dict) else {}
                    if src.get("type") == "base64" and src.get("data"):
                        media_type = src.get("media_type", "image/png")
                        data_url = f"data:{media_type};base64,{src['data']}"
                        hits.append((msg_idx, part_idx, {"url": data_url, "detail": "high"}))
    return hits


# ─── Candidate resolution (in-process; never calls BSL Router itself) ─────────

def _resolve_vision_candidates(
    config: dict,
    vision_model: str,
    _depth: int = 0,
) -> list[tuple[dict, str, str]]:
    """
    Expand a vision model reference into an ordered list of concrete
    (connection, model_id, provider_name) candidates.

    Handles, in order:
      - combo alias   -> recursively expands every chain entry
      - "prov/model"  -> direct provider-qualified reference
      - legacy alias  -> config["aliases"] lookup
      - bare model id -> scan all providers for an enabled match

    Only connections that are enabled AND have a base_url are returned, so
    every candidate is directly dialable. Never returns a BSL Router
    self-reference.
    """
    if not vision_model or _depth > _MAX_COMBO_DEPTH:
        return []

    from app.utils.model_resolver import _choose_connection_for_model

    providers = config.get("providers", {})

    def _direct(prov_name: str, model_id: str) -> list[tuple[dict, str, str]]:
        """Resolve one provider+model into at most one dialable candidate."""
        prov = providers.get(prov_name)
        if not isinstance(prov, dict):
            return []
        meta = next(
            (m for m in prov.get("models", [])
             if isinstance(m, dict) and m.get("id") == model_id and m.get("enabled", True)),
            None,
        )
        if meta is None:
            return []
        conn = _choose_connection_for_model(prov, model_id)
        if not conn or not conn.get("base_url"):
            return []
        # This scout speaks OpenAI multimodal only (_build_vision_payload emits
        # an OpenAI body and _describe_image_once sends Bearer auth). Drop
        # leaves on any other wire format rather than dialing them wrongly —
        # silently mismatched formats are far harder to diagnose than a gap in
        # the chain. `format` is injected by model_resolver.
        fmt = str(conn.get("format") or "openai").lower()
        if fmt not in _VISION_SUPPORTED_FORMATS:
            print(
                f"[Vision Scout] skipping {prov_name}/{model_id}: "
                f"provider format '{fmt}' is not OpenAI-compatible",
                flush=True,
            )
            return []
        return [(conn, model_id, prov_name)]

    # ── 1. Combo alias: expand every chain entry in order ──
    for combo in config.get("combos", []):
        if not isinstance(combo, dict) or combo.get("alias") != vision_model:
            continue
        out: list[tuple[dict, str, str]] = []
        for entry in combo.get("chain", []):
            if isinstance(entry, dict):
                prov_name = entry.get("provider")
                model_id = entry.get("model") or entry.get("id")
                if prov_name and model_id:
                    out.extend(_direct(prov_name, model_id))
                elif model_id:
                    out.extend(_resolve_vision_candidates(config, model_id, _depth + 1))
            elif isinstance(entry, str):
                if "/" in entry:
                    prov_name, model_id = entry.split("/", 1)
                    if prov_name in providers:
                        out.extend(_direct(prov_name, model_id))
                        continue
                # Bare string: nested combo alias, legacy alias, or model id
                out.extend(_resolve_vision_candidates(config, entry, _depth + 1))
        return out

    # ── 2. Provider-qualified reference ──
    if "/" in vision_model:
        prov_name, model_id = vision_model.split("/", 1)
        if prov_name in providers:
            return _direct(prov_name, model_id)

    # ── 3. Legacy alias ──
    aliases = config.get("aliases", {})
    if isinstance(aliases, dict) and vision_model in aliases:
        alias_cfg = aliases[vision_model] or {}
        prov_name = alias_cfg.get("provider")
        model_id = alias_cfg.get("model", vision_model)
        if prov_name:
            return _direct(prov_name, model_id)

    # ── 4. Bare model id: first enabled match wins ──
    for prov_name, prov in providers.items():
        if not isinstance(prov, dict):
            continue
        found = _direct(prov_name, vision_model)
        if found:
            return found

    return []


def _build_vision_payload(url: str, prompt: str, model: str, max_tokens: int) -> dict:
    """Build an OpenAI-compatible vision request body."""
    return {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": url}},
                ],
            }
        ],
        "max_tokens": max_tokens,
        "stream": False,
    }


def _parse_vision_response(raw_text: str, label: str) -> str:
    """
    Extract the description text from an upstream response body.

    Tolerates providers that return SSE frames despite stream=False.
    Returns "" when no usable content could be extracted, which signals
    the caller to advance to the next candidate.
    """
    if not raw_text or not raw_text.strip():
        print(f"[Vision Scout] Empty response body from {label}", flush=True)
        return ""

    description = ""
    data = None

    # ── SSE-formatted response (some providers stream despite stream=False) ──
    if raw_text.lstrip().startswith("data:"):
        print(
            f"[Vision Scout] {label} returned SSE despite stream=False — parsing SSE frames.",
            flush=True,
        )
        content_parts = []
        for line in raw_text.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                chunk = json.loads(payload)
                delta = chunk.get("choices", [{}])[0].get("delta", {})
                content_part = delta.get("content", "")
                if content_part:
                    content_parts.append(content_part)
            except (json.JSONDecodeError, IndexError, KeyError):
                continue
        if content_parts:
            description = "".join(content_parts)
        else:
            # SSE frames had no content deltas — try full JSON parse as fallback
            try:
                data = json.loads(raw_text)
            except json.JSONDecodeError:
                pass
    else:
        # ── Standard JSON response ──
        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError as e:
            print(
                f"[Vision Scout] Non-JSON response from {label}: {e} "
                f"— raw[:200]={raw_text[:200]!r}",
                flush=True,
            )
            return ""

    if data is not None:
        description = data.get("choices", [{}])[0].get("message", {}).get("content", "") or ""

    return description


async def _describe_image_once(
    url: str,
    prompt: str,
    http_client: httpx.AsyncClient,
    conn: dict,
    model: str,
    provider: str,
    max_tokens: int,
    timeout_s: float,
) -> str:
    """
    Single attempt against one concrete candidate.

    Returns the description, or "" if this candidate produced no usable
    content. Raises on transport/HTTP failure so the caller can advance.
    """
    base_url = (conn.get("base_url") or "").rstrip("/")
    label = f"{provider}/{model}"

    headers = {
        "Authorization": f"Bearer {conn.get('api_key', '')}",
        "Content-Type": "application/json",
    }

    # Endpoint construction is delegated to the same builder main.py uses, so
    # this scout cannot drift from the primary routing path.
    endpoint = build_custom_text_upstream_url(base_url, "openai")
    payload = _build_vision_payload(url, prompt, model, max_tokens)

    resp = await http_client.post(
        endpoint, json=payload, headers=headers, timeout=timeout_s
    )
    resp.raise_for_status()

    return _parse_vision_response(resp.text, label)


async def _describe_image(
    image_url: dict,
    http_client: httpx.AsyncClient,
    candidates: list[tuple[dict, str, str]],
    max_tokens: int,
    ui_ux_override: bool,
    timeout_s: float = DEFAULT_VISION_TIMEOUT_S,
) -> str | None:
    """
    Describe an image, walking the candidate chain with per-leaf fallback.

    Returns the description, or None if every candidate was exhausted
    without producing usable content.

    Uses an in-memory cache to avoid re-describing the same image when the
    IDE retries the request (common when TTFT exceeds client timeout).
    """
    url = image_url.get("url", "")

    # ── Cache lookup ──
    cache_key = hashlib.sha256(url.encode("utf-8", errors="replace")).hexdigest()
    now = time.monotonic()
    cached = _VISION_CACHE.get(cache_key)
    if cached:
        cached_ts, cached_desc = cached
        if now - cached_ts < _VISION_CACHE_TTL:
            print(f"[Vision Scout] Cache HIT for image (key={cache_key[:12]})", flush=True)
            return cached_desc
        del _VISION_CACHE[cache_key]

    if not candidates:
        return None

    if ui_ux_override:
        prompt = (
            "Describe this UI layout in exhaustive detail: components, spacing, typography, "
            "color palette, interactive states, and accessibility considerations."
        )
    else:
        detail_level = image_url.get("detail", "high")
        prompt = (
            f"Describe this image in detail. Detail level: {detail_level}. "
            "Provide a thorough description covering all visual elements, "
            "text visible in the image, colors, layout, and any notable features."
        )

    attempts = candidates[:MAX_VISION_ATTEMPTS]
    total = len(attempts)
    last_error = "no candidates produced content"

    for idx, (conn, model, provider) in enumerate(attempts):
        label = f"{provider}/{model}"
        try:
            print(
                f"[Vision Scout] Describing image via {label} [{idx + 1}/{total}]",
                flush=True,
            )
            description = await _describe_image_once(
                url, prompt, http_client, conn, model, provider, max_tokens, timeout_s
            )
            if description:
                _VISION_CACHE[cache_key] = (time.monotonic(), description)
                print(
                    f"[Vision Scout] OK via {label} — cached (key={cache_key[:12]})",
                    flush=True,
                )
                return description
            last_error = f"{label} returned no content"
            print(f"[Vision Scout] {last_error} — advancing", flush=True)
        except httpx.HTTPStatusError as e:
            last_error = f"{label} HTTP {e.response.status_code}"
            print(f"[Vision Scout] {last_error} — advancing", flush=True)
        except Exception as e:
            last_error = f"{label} {type(e).__name__}: {str(e)[:120]}"
            print(f"[Vision Scout] {last_error} — advancing", flush=True)

    print(f"[Vision Scout] All {total} candidate(s) failed. Last: {last_error}", flush=True)
    return None


async def polyfill_vision(
    request: ChatCompletionRequest,
    http_client: httpx.AsyncClient,
    config: dict,
) -> ChatCompletionRequest:
    """
    Main entry point for the Vision Scout.
    """
    t = config.get("tools", {})
    if not t.get("vision_bridge_enabled", False):
        return request

    image_hits = _extract_image_urls(request)
    if not image_hits:
        return request

    vision_model = t.get("vision_bridge_model", DEFAULT_VISION_MODEL)

    # Expand to concrete, directly-dialable candidates. Combo aliases are
    # resolved IN-PROCESS — we never call BSL Router's own endpoint, which
    # would re-enter the routing pipeline and deadlock the event loop.
    candidates = _resolve_vision_candidates(config, vision_model)

    if not candidates:
        print(
            f"[Vision Scout] No dialable connection for vision model "
            f"'{vision_model}' — skipping vision polyfill.",
            flush=True,
        )
        return request

    print(
        f"[Vision Scout] '{vision_model}' resolved to {len(candidates)} candidate(s): "
        + ", ".join(f"{p}/{m}" for _, m, p in candidates[:MAX_VISION_ATTEMPTS]),
        flush=True,
    )

    ui_ux_override = t.get("vision_ui_ux_override", False)
    max_tokens = 2048 if ui_ux_override else t.get("vision_max_tokens", 1024)
    try:
        timeout_s = float(t.get("vision_timeout_s", DEFAULT_VISION_TIMEOUT_S))
    except (TypeError, ValueError):
        timeout_s = DEFAULT_VISION_TIMEOUT_S
    try:
        total_budget_s = float(
            t.get("vision_total_budget_s", DEFAULT_VISION_TOTAL_BUDGET_S)
        )
    except (TypeError, ValueError):
        total_budget_s = DEFAULT_VISION_TOTAL_BUDGET_S

    # Process images concurrently
    async def fetch_desc(msg_idx, part_idx, image_url_dict):
        try:
            desc = await _describe_image(
                image_url_dict, http_client, candidates, max_tokens, ui_ux_override, timeout_s
            )
            return (msg_idx, part_idx), desc
        except Exception as e:
            print(f"[Vision Scout] Unexpected error describing image: {e}", flush=True)
            return (msg_idx, part_idx), None

    tasks = [fetch_desc(m, p, i) for m, p, i in image_hits]

    # Hard wall-clock ceiling. The scout runs inline inside the client's
    # request, so it must never be able to hold that request open.
    try:
        results = await asyncio.wait_for(
            asyncio.gather(*tasks), timeout=total_budget_s
        )
        descriptions = dict(results)
    except asyncio.TimeoutError:
        print(
            f"[Vision Scout] Total budget of {total_budget_s}s exceeded.",
            flush=True,
        )
        raise VisionPolyfillFailed(
            f"vision description exceeded the {total_budget_s}s budget"
        )

    # FAIL-OPEN: If every image failed, substitute a placeholder and let the
    # request continue to the target model. The previous behavior raised
    # VisionPolyfillFailed and returned a 502, which blocked ALL responses
    # (even for requests where the image was ancillary, not essential). The
    # target model can still answer "I couldn't see the image" — that's far
    # better than a hard 502 that prevents any response at all.
    if descriptions and all(d is None for d in descriptions.values()):
        failed_count = len(descriptions)
        candidate_count = min(len(candidates), MAX_VISION_ATTEMPTS)
        print(
            f"[Vision Scout] All {failed_count} image(s) failed across "
            f"{candidate_count} candidate(s) — failing open with placeholder.",
            flush=True,
        )

    # Rewrite message content, replacing image_url parts with text descriptions
    for msg_idx, msg in enumerate(request.messages):
        if msg.content is None or isinstance(msg.content, str):
            continue
        if isinstance(msg.content, list):
            new_parts = []
            for part_idx, part in enumerate(msg.content):
                key = (msg_idx, part_idx)
                if key in descriptions:
                    desc = descriptions[key]
                    if desc is None:
                        # Partial failure: at least one sibling image succeeded,
                        # so the request is still worth serving.
                        text = PLACEHOLDER_UNREADABLE
                    else:
                        label = "UI Mockup" if ui_ux_override else "Image"
                        text = f"[{label} Description]: {desc}"
                    new_parts.append({"type": "text", "text": text})
                else:
                    new_parts.append(part)
            msg.content = new_parts

    return request
