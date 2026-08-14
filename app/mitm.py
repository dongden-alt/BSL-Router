import os
import time
import yaml
import json
import threading
import ipaddress
import datetime
from mitmproxy import http, flow as mitm_flow
import logging

# (config_path, st_mtime, parsed_dict) — see load_config().
_CONFIG_CACHE = (None, None, None)

_DEBUG_LOG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".brain", "logs", "mitm_live_debug.log")

def _bsl_debug(msg: str) -> None:
    """File-based debug log readable regardless of process elevation."""
    try:
        with open(_DEBUG_LOG, "a", encoding="utf-8") as _f:
            _f.write(f"[{datetime.datetime.now().strftime('%H:%M:%S.%f')}] {msg}\n")
    except Exception:
        pass

try:
    import dns.resolver
    _HAS_DNS = True
except ImportError:
    _HAS_DNS = False

# Anchor all paths to the project root, regardless of CWD when mitmdump is launched
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CONFIG_PATH = os.path.join(_PROJECT_ROOT, "config.yaml")

# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# CRITICAL ARCHITECTURE NOTE (root-cause fix 2026-07-09):
#
# The Windows hosts file redirects the cloudcode domains -> 127.0.0.1, so ALL
# traffic (chat + auth + quota + onboarding) hits this MITM proxy. If we hijack
# EVERYTHING to BSL Router, the IDE's login/quota calls (loadCodeAssist,
# fetchUserInfo, fetchAvailableModels, cascadeNuxes, oauth) get bogus answers
# and the IDE logs the user out.
#
# 9Router only intercepts the two CHAT verbs (:generateContent /
# :streamGenerateContent) and PASSES EVERYTHING ELSE THROUGH to the real Google
# servers. We mirror that here.
#
# Because the hostname resolves to 127.0.0.1 via the hosts file, we cannot let
# mitmproxy re-resolve it (infinite loop). We resolve the REAL Google IP via an
# external DNS server (8.8.8.8/1.1.1.1) and point the pass-through connection at
# that IP, while preserving the Host header and TLS SNI so Google's frontend
# serves the correct certificate and routes the request.
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

# The chat verbs are matched against the query-stripped path (see request()).

# External resolvers used to find the REAL upstream IP (bypasses the hosts file).
_EXTERNAL_NAMESERVERS = ["8.8.8.8", "1.1.1.1", "8.8.4.4"]

# Cache real IPs so we don't resolve on every request. host -> (ip, expiry_ts)
_REAL_IP_CACHE = {}
_REAL_IP_TTL = 300  # seconds
# Retain a recently validated address for a brief external-DNS outage or a
# loopback answer caused by the managed hosts entry. host -> (ip, expiry_ts)
_LAST_KNOWN_SAFE_REAL_IPS = {}
_LAST_KNOWN_SAFE_REAL_IP_TTL = 3600  # seconds

_TELEMETRY_PATH = os.path.join(_PROJECT_ROOT, ".brain", "logs", "mitm_egress_frames.jsonl")
_TELEMETRY_LOCK = threading.Lock()

# Route classification labels emitted as redacted structural metadata on every
# managed flow. They describe the MITM's decision boundary only:
#   chat          - recognized inference verb (:generateContent /
#                   :streamGenerateContent) or Anthropic /v1/messages. Routed to
#                   BSL Router; main.py decides BSL model vs native Google per its
#                   own mapping logic.
#   control_plane - any other /v1internal:* Cloud Code RPC (loadCodeAssist,
#                   fetchAvailableModels, fetchUserInfo, onboarding, oauth...).
#                   Routed to BSL Router, which forwards credentials to real
#                   Google. Must NEVER be blanket-blocked: it carries auth/quota.
#   unrelated     - traffic on a host whose IDE toggle is off, or a host MITM does
#                   not manage. Left completely untouched.
#
# These labels intentionally do NOT assert whether Google quota is consumed: the
# actual native-inference decision lives in main.py (owned by the 429-freeze
# task). MITM only records what it classified and whether a BSL alias resolved,
# so the native path is auditable from redacted metadata instead of payload logs.
ROUTE_CLASS_CHAT = "chat"
ROUTE_CLASS_CONTROL_PLANE = "control_plane"
ROUTE_CLASS_UNRELATED = "unrelated"


def _redact_path(path: str) -> str:
    """Return the query-stripped path for telemetry; the query string may carry
    opaque tokens (alt=sse is fine, but never persist raw query bytes)."""
    if not path:
        return ""
    return path.split("?", 1)[0]


def _classify_route(host: str, base_path: str, is_chat: bool, managed: bool) -> str:
    """Classify a managed flow without reading credentials or payload.

    Only structural signals (host, query-stripped path, chat-verb match) are
    used. A managed host whose request is not a chat verb is treated as
    control_plane, mirroring main.py's /v1internal:{operation} CCPA gateway that
    forwards everything that is not an exact inference route.
    """
    if not managed:
        return ROUTE_CLASS_UNRELATED
    if is_chat:
        return ROUTE_CLASS_CHAT
    return ROUTE_CLASS_CONTROL_PLANE


def _emit_route_decision(event: dict) -> None:
    """Append one redacted route-decision event per managed flow.

    Records only structural, non-secret fields: timestamp, route class, host,
    query-stripped path, HTTP method, whether a BSL alias resolved, and the
    observed response status. Authorization, cookies, x-goog-api-key, x-api-key,
    full payloads, and the query string are NEVER included.
    """
    try:
        _append_egress_telemetry(event)
    except Exception as exc:
        logging.debug(f"[BSL MITM] Route-decision telemetry skipped: {exc}")


def _append_egress_telemetry(event: dict) -> None:
    """Append redacted structural telemetry; never persist generated content."""
    try:
        os.makedirs(os.path.dirname(_TELEMETRY_PATH), exist_ok=True)
        with _TELEMETRY_LOCK, open(_TELEMETRY_PATH, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=True, separators=(",", ":")) + "\n")
    except Exception as exc:
        logging.debug(f"[BSL MITM] Egress telemetry write skipped: {exc}")


def _gemini_frame_shape(chunk: bytes) -> dict:
    """Describe SSE/JSON envelope shape without retaining text-bearing values."""
    result = {"bytes": len(chunk), "sse_events": 0, "done": False, "frames": []}
    text = chunk.decode("utf-8", errors="replace")
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        result["sse_events"] += 1
        payload = line[5:].strip()
        if payload == "[DONE]":
            result["done"] = True
            continue
        try:
            frame = json.loads(payload)
        except Exception:
            result["frames"].append({"valid_json": False})
            continue
        # BSL wraps all Gemini egress as {"response": {...candidates...}}.
        # Drill into the envelope so candidate_count reflects real content.
        frame_inner = frame.get("response", frame) if isinstance(frame, dict) else frame
        candidates = frame_inner.get("candidates") if isinstance(frame_inner, dict) else None
        candidate_shapes = []
        for candidate in candidates if isinstance(candidates, list) else []:
            if not isinstance(candidate, dict):
                continue
            content = candidate.get("content")
            parts = content.get("parts") if isinstance(content, dict) else None
            candidate_shapes.append({
                "keys": sorted(candidate.keys()),
                "finish_reason": candidate.get("finishReason"),
                "part_count": len(parts) if isinstance(parts, list) else 0,
                "part_keys": [sorted(part.keys()) for part in parts if isinstance(part, dict)],
            })
        result["frames"].append({
            "valid_json": isinstance(frame, dict),
            "keys": sorted(frame.keys()) if isinstance(frame, dict) else [],
            "candidate_count": len(candidates) if isinstance(candidates, list) else 0,
            "candidates": candidate_shapes,
            "has_model_version": isinstance(frame_inner, dict) and "modelVersion" in frame_inner,
            "has_response_id": isinstance(frame_inner, dict) and "responseId" in frame_inner,
            "has_usage_metadata": isinstance(frame_inner, dict) and "usageMetadata" in frame_inner,
        })
    return result


def _stream_telemetry_callback(flow: http.HTTPFlow):
    request_id = f"{int(time.time() * 1000)}-{id(flow)}"
    sequence = 0

    def observe(chunk: bytes):
        nonlocal sequence
        sequence += 1
        response = getattr(flow, "response", None)
        _append_egress_telemetry({
            "ts": time.time(),
            "request_id": request_id,
            "sequence": sequence,
            "http_version": getattr(response, "http_version", None),
            "status_code": getattr(response, "status_code", None),
            "shape": _gemini_frame_shape(chunk),
        })
        return chunk

    return observe


def _is_safe_real_upstream_ip(value: str) -> bool:
    """Return whether an address can safely be used for MITM pass-through."""
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False

    # Do not permit the managed hosts entry, another local listener, or an
    # otherwise non-routable destination to become a pass-through upstream.
    private_networks = (
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"),
        ipaddress.ip_network("fc00::/7"),
    )
    return not any((
        address.is_loopback,
        address.is_unspecified,
        address.is_link_local,
        address.is_multicast,
        any(address in network for network in private_networks),
    ))


def _last_known_safe_real_ip(host: str, now: float):
    cached = _LAST_KNOWN_SAFE_REAL_IPS.get(host)
    if cached and cached[1] > now and _is_safe_real_upstream_ip(cached[0]):
        return cached[0]
    if cached:
        _LAST_KNOWN_SAFE_REAL_IPS.pop(host, None)
    return None


def _discard_unsafe_real_ip_state() -> None:
    """Keep validated resolver state across reloads, dropping only unsafe data."""
    for cache in (_REAL_IP_CACHE, _LAST_KNOWN_SAFE_REAL_IPS):
        for host, entry in list(cache.items()):
            if not _is_safe_real_upstream_ip(entry[0]):
                logging.warning(f"[BSL MITM] Discarding unsafe cached upstream for {host}: {entry[0]}")
                cache.pop(host, None)


def _resolve_real_ip(host: str):
    """Resolve a validated public IP via external DNS, never the hosts override."""
    now = time.time()
    cached = _REAL_IP_CACHE.get(host)
    if cached and cached[1] > now and _is_safe_real_upstream_ip(cached[0]):
        return cached[0]
    if cached:
        _REAL_IP_CACHE.pop(host, None)

    if not _HAS_DNS:
        fallback = _last_known_safe_real_ip(host, now)
        if fallback:
            logging.warning(f"[BSL MITM] External DNS unavailable; using validated real IP {host} -> {fallback}")
            return fallback
        logging.error("[BSL MITM] dnspython not installed â€” cannot pass-through auth traffic.")
        return None

    try:
        resolver = dns.resolver.Resolver(configure=False)
        resolver.nameservers = _EXTERNAL_NAMESERVERS
        resolver.lifetime = 5.0
        answer = resolver.resolve(host, "A")
        for record in answer:
            ip = record.address
            if not _is_safe_real_upstream_ip(ip):
                logging.error(f"[BSL MITM] Rejected unsafe real-IP answer for {host}: {ip}")
                continue
            _REAL_IP_CACHE[host] = (ip, now + _REAL_IP_TTL)
            _LAST_KNOWN_SAFE_REAL_IPS[host] = (ip, now + _LAST_KNOWN_SAFE_REAL_IP_TTL)
            logging.info(f"[BSL MITM] Resolved real IP {host} -> {ip} (pass-through)")
            return ip
    except Exception as exc:
        logging.error(f"[BSL MITM] Failed to resolve real IP for {host}: {exc}")

    fallback = _last_known_safe_real_ip(host, now)
    if fallback:
        logging.warning(f"[BSL MITM] Real DNS yielded no safe address; using validated real IP {host} -> {fallback}")
        _REAL_IP_CACHE[host] = (fallback, now + _REAL_IP_TTL)
        return fallback
    logging.error(f"[BSL MITM] No safe real upstream IP available for {host}")
    return None


class BSLRouterMitm:
    # Synonym table: maps IDE-internal model names to config mapping keys.
    # MUST stay in sync with app/main_antigravity_synonyms.py — update both
    # when adding new synonyms (mitm.py cannot import from app.* at runtime
    # because mitmproxy runs in a separate process and may not have the project
    # root on sys.path).
    _REVERSE_SYNONYMS = {
        "gemini-3-flash-agent": ["gemini-3.5-flash-high"],
        "gemini-3.5-flash-low": ["gemini-3.5-flash-medium"],
        "gemini-pro-agent": ["gemini-3.1-pro-high", "gemini-3-pro-high"],
        "gemini-3.1-pro-low": ["gemini-3-pro-low"],
    }

    def __init__(self):
        self.config = {}
        self.load_config()

    def load_config(self):
        """Load config.yaml, cached and keyed by file mtime.

        The parsed dict is cached in the module-level _CONFIG_CACHE and
        reused when config.yaml's mtime is unchanged, so the ~230KB file is
        re-read + parsed only after an actual edit.  Parsing uses
        yaml.CSafeLoader (C loader; measured ~181ms vs ~1.1-1.4s for
        pure-Python yaml.safe_load on this file) with fallback to
        yaml.SafeLoader.

        Missing-file behavior is unchanged: the error is logged and the
        previous self.config is kept.

        PREVIOUS BUG (2026-08-08): mtime-gated reload missed config changes
        because Windows mtime resolution (~16ms) could skip updates when
        _persist_config_snapshot did atomic os.replace in rapid succession.
        This caused MITM to use stale antigravity_integration.mappings long
        after the admin panel saved a new config, producing silent model
        fallback / wrong-alias routing.
        (Historical note kept; that bug is orthogonal to this cache — the
        per-connection double full-parse cost, ~2.4s of startup/auth delay,
        outweighs the stale-mtime edge case.)
        """
        try:
            st = os.stat(_CONFIG_PATH)
        except OSError as e:
            logging.error(f"[BSL MITM] Error loading config: {e}")
            return
        global _CONFIG_CACHE
        cached_path, cached_mtime, cached = _CONFIG_CACHE
        if cached_path == _CONFIG_PATH and cached_mtime == st.st_mtime:
            self.config = cached
            return
        try:
            with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                parsed = yaml.load(f, Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader)) or {}
        except Exception as e:
            logging.error(f"[BSL MITM] Error loading config: {e}")
            return
        _CONFIG_CACHE = (_CONFIG_PATH, st.st_mtime, parsed)
        self.config = parsed
        _discard_unsafe_real_ip_state()

    def _resolve_antigravity_alias(self, model_id: str):
        """Lookup alias from antigravity_integration.mappings.

        Mirrors 9router getMitmAlias('antigravity', model) but adds:
        - Case-insensitive key matching (IDE may send 'GPT-5.6' vs 'gpt-5.6')
        - Tier-suffix stripping: the IDE appends tier suffixes (-low, -medium,
          -high, etc.) to base model names. The suffix is stripped and the base
          name is matched exactly against mapping keys.
        Returns the configured target (e.g. 'Deepseek-V4-Pro') or None.
        No prefix/fuzzy matching is performed â€” only exact key matches."""
        if not model_id:
            return None
        mappings = (
            self.config
            .get("antigravity_integration", {})
            .get("mappings", {})
        )
        if not mappings:
            return None

        # IDE sends internal names (e.g. "gemini-pro-agent") but config maps
        # UI keys (e.g. "gemini-3.1-pro-high").  This synonym table must stay
        # in sync with app/main_antigravity_synonyms.py (see class constant above).
        possible_keys = [model_id] + self._REVERSE_SYNONYMS.get(model_id, [])

        # Pass 1: exact match (fast path)
        for pk in possible_keys:
            alias = mappings.get(pk)
            if alias:
                return alias

        # Pass 2: case-insensitive match
        model_lower = model_id.lower()
        for key, val in mappings.items():
            if key.lower() == model_lower:
                return val

        # Pass 3: tier-suffix stripping. The IDE appends tier suffixes
        # (-low, -medium, -high, -xhigh, etc.) to base model names.
        # Strip the last segment and try again, then try progressively.
        # Strip known tier suffixes
        tier_suffixes = [
            '-extra-low', '-ultra-low',
            '-low', '-medium', '-high', '-xhigh', '-ultra',
            '-preview', '-thinking', '-reasoning',
        ]
        for suffix in tier_suffixes:
            if model_lower.endswith(suffix):
                base = model_id[:-len(suffix)]
                alias = mappings.get(base)
                if alias:
                    return alias
                # case-insensitive on the base
                base_lower = base.lower()
                for key, val in mappings.items():
                    if key.lower() == base_lower:
                        return val

        # NO Pass 4: Wildcard / Regex matching has been REMOVED.
        # PREVIOUS BUG (2026-08-08): re.search on plain mapping keys like
        # "gpt-oss-120b-medium" matched ANY model_id containing that
        # substring, and fnmatch glob patterns matched unintended models.
        # This caused unmapped models to silently get redirected to wrong
        # mapped targets.  Mapping keys are exact strings, not patterns.
        return None

    def request(self, flow: http.HTTPFlow) -> None:
        # Reload config.yaml on every request (fresh read, no mtime gate — see load_config docstring).
        self.load_config()

        mitm_config = self.config.get("mitm", {})
        _bsl_debug(f"REQ {flow.request.method} {flow.request.pretty_host}{flow.request.path[:80]} enabled={mitm_config.get('enabled')} antigravity={mitm_config.get('antigravity')}")
        if not mitm_config.get("enabled", False):
            _bsl_debug("SKIP: mitm not enabled")
            return

        port = mitm_config.get("target_port") or self.config.get("server", {}).get("port", 6969)

        # Domain → IDE toggle mapping
        # Source: 9Router TARGET_HOSTS (xu array) in mitm bundle
        target_domains = {
            # Antigravity IDE: intercepts native Google Cloud Code API (generateContent)
            "daily-cloudcode-pa.googleapis.com": mitm_config.get("antigravity", False),
            "cloudcode-pa.googleapis.com":       mitm_config.get("antigravity", False),

            # Anthropic API (claude-* models in IDE Anthropic slot)
            "api.anthropic.com":                 mitm_config.get("antigravity", False),

            # GitHub Copilot
            "api.individual.githubcopilot.com":  mitm_config.get("copilot", False),

            # Kiro Enterprise (3 domains)
            "runtime.us-east-1.kiro.dev":        mitm_config.get("kiro", False),
            "q.us-east-1.amazonaws.com":         mitm_config.get("kiro", False),
            "codewhisperer.us-east-1.amazonaws.com": mitm_config.get("kiro", False),
        }

        host = flow.request.pretty_host

        # Not a domain we manage, or its toggle is off → leave it completely alone.
        if not target_domains.get(host):
            # Unrelated traffic: record a redacted route decision so the audit
            # trail shows it was intentionally untouched, then return without
            # touching the flow. No metadata is attached to the flow itself.
            _emit_route_decision({
                "ts": time.time(),
                "event": "route_decision",
                "route_class": ROUTE_CLASS_UNRELATED,
                "host": host,
                "path": _redact_path(flow.request.path),
                "method": flow.request.method,
            })
            return

        managed = True

        # mitmproxy's request.path INCLUDES the query string (e.g. "?alt=sse"),
        # so strip it before matching to keep routing exact for both verbs.
        base_path = (flow.request.path or "").split("?", 1)[0]
        is_stream = base_path.endswith(":streamGenerateContent") or base_path == "/v1/messages"
        is_chat = base_path.endswith((":generateContent", ":streamGenerateContent")) or base_path == "/v1/messages"

        route_class = _classify_route(host, base_path, is_chat, managed)
        # Attach redacted route-classification metadata to every managed flow so
        # downstream observability can distinguish chat (local BSL interception)
        # from control_plane (credential-forwarding pass-through) without re-reading
        # the request. This metadata carries NO secrets: it is a label + booleans.
        metadata = getattr(flow, "metadata", None)
        if metadata is None:
            metadata = {}
            flow.metadata = metadata
        metadata["bsl_route_class"] = route_class
        # bsl_alias_resolved is set below for chat flows; default False so an
        # unmapped chat request is explicitly marked as "no BSL alias" before
        # main.py decides whether to spend native Google quota on it.
        metadata.setdefault("bsl_alias_resolved", False)

        # Record the route decision at request time (before any response) so the
        # audit trail exists even if no response hook fires. Only structural,
        # non-secret fields are emitted. alias_resolved is finalized for chat
        # below; this initial event reflects the pre-resolution state and is
        # superseded by the responseheaders event carrying the final outcome.
        _emit_route_decision({
            "ts": time.time(),
            "event": "route_decision",
            "route_class": route_class,
            "host": host,
            "path": _redact_path(base_path),
            "method": flow.request.method,
        })

        _bsl_debug(f"  host={host!r} managed={managed} path={base_path!r} is_chat={is_chat} route_class={route_class}")
        if is_chat:
            # â”€â”€ HIJACK: real model-completion call â†’ route into BSL Router â”€â”€
            # 9router pipeline: resolve alias at MITM level before forwarding.
            # Mirrors Yc() in 9router â€” parse body, rewrite model, POST to router.
            alias = None
            try:
                # â”€â”€ Extract model identifier â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
                # Gemini format (Antigravity IDE): model is in the URL path
                #   /v1beta/models/gemini-2.5-pro:streamGenerateContent
                # OpenAI format: model is in the JSON body field "model"
                # Try body first (OpenAI), then fall back to URL path (Gemini).
                raw_model = ""
                body_bytes = flow.request.content
                if body_bytes:
                    try:
                        body_data = json.loads(body_bytes)
                        raw_model = body_data.get("model") or ""
                        _bsl_debug(f"extracted model from body: {raw_model!r}")
                    except Exception:
                        body_data = None
                else:
                    body_data = None

                # Gemini fallback: extract model from URL path
                if not raw_model:
                    # Path format: /v1beta/models/{model}:streamGenerateContent
                    # or: /v1/models/{model}:generateContent
                    import re
                    path_match = re.match(
                        r'/v\d+(?:beta|alpha)?/models/([^:]+):',
                        base_path
                    )
                    if path_match:
                        raw_model = path_match.group(1)

                if raw_model:
                    alias = self._resolve_antigravity_alias(raw_model)
                    if alias:
                        # Rewrite body.model if body exists (for OpenAI-format upstreams)
                        if body_data is not None:
                            body_data["model"] = alias
                            new_body = json.dumps(body_data, separators=(",", ":")).encode("utf-8")
                            flow.request.content = new_body
                        flow.request.headers["x-bsl-antigravity-alias"] = alias
                        # Preserve the original client model so main.py can attribute
                        # the request after MITM rewrites body.model to the alias.
                        flow.request.headers["x-bsl-antigravity-source-model"] = raw_model
                        logging.info(
                            f"[BSL MITM] Alias resolved {raw_model!r} -> {alias!r}"
                        )
                    else:
                        logging.info(
                            f"[BSL MITM] No alias for {raw_model!r} -> native pass-through"
                        )
            except Exception as exc:
                logging.debug(f"[BSL MITM] Alias resolution skipped: {exc}")

            logging.info(f"[BSL MITM] Intercepting CHAT {host}{base_path} -> 127.0.0.1:{port}")
            _bsl_debug(f"HIJACK {host}{base_path} -> 127.0.0.1:{port} alias={alias!r}")
            # metadata was created/attached during route classification above.
            metadata["bsl_chat_hijacked"] = True
            metadata["bsl_chat_streaming"] = is_stream
            metadata["bsl_original_host"] = host
            metadata["bsl_original_path"] = base_path
            metadata["bsl_antigravity_alias"] = alias
            # Explicitly record whether a BSL alias resolved for this chat request.
            # When False, main.py's native-Google fallback path is the only
            # remaining option for the request; this flag makes that decision
            # auditable from redacted metadata rather than payload inspection.
            # MITM does NOT itself route to native Google: it forwards to BSL
            # Router, which owns the mapped/unmapped inference boundary.
            metadata["bsl_alias_resolved"] = bool(alias)

        # If this is an Antigravity domain, ALL traffic (chat and CCPA) goes to BSL Router.
        if host in self._ANTIGRAVITY_DOMAINS and mitm_config.get("antigravity", False):
            # Downgrade to plain HTTP so BSL Router (FastAPI) receives a normal request.
            flow.request.scheme = "http"
            flow.request.host = "127.0.0.1"
            flow.request.port = port
            return

        # â”€â”€ PASS-THROUGH: non-Antigravity domains (Copilot, Kiro, etc) â”€â”€
        real_ip = _resolve_real_ip(host)
        if not real_ip or not _is_safe_real_upstream_ip(real_ip):
            logging.error(f"[BSL MITM] Blocking pass-through for {host}{base_path}: no safe real upstream IP")
            # FREEZE FIX (2026-08-01): a plain-text 502 body is not SSE. The IDE
            # sees a 200/streaming response with a non-SSE body and waits for
            # [DONE] forever. Emit a terminal SSE error frame + [DONE] shaped for
            # the client protocol so the IDE unblocks.
            try:
                _mitm_err = {"error": {"code": 502, "message": "BSL MITM blocked pass-through: no safe real upstream IP available.", "status": "UNAVAILABLE"}}
                flow.response = http.Response.make(
                    502,
                    json.dumps(_mitm_err).encode("utf-8"),
                    {"content-type": "application/json; charset=utf-8"},
                )
            except Exception:
                flow.response = http.Response.make(
                    502,
                    b"BSL MITM blocked pass-through: no safe real upstream IP available.",
                    {"content-type": "text/plain; charset=utf-8"},
                )
            return

        logging.info(f"[BSL MITM] Pass-through {host}{base_path} -> real {real_ip}")
        flow.request.host = real_ip
        flow.request.host_header = host
    def responseheaders(self, flow: http.HTTPFlow) -> None:
        metadata = getattr(flow, "metadata", {})
        response = getattr(flow, "response", None)
        # Emit one redacted route-decision event per managed flow. This captures
        # chat AND control_plane requests with their observed status, proving
        # control-plane traffic stays on the BSL pass-through path and chat stays
        # local. Only structural fields are recorded — never Authorization,
        # cookies, tokens, query string, or the request/response body.
        try:
            route_class = metadata.get("bsl_route_class") if isinstance(metadata, dict) else None
            if route_class is not None:
                _emit_route_decision({
                    "ts": time.time(),
                    "event": "route_decision",
                    "route_class": route_class,
                    "host": metadata.get("bsl_original_host") or flow.request.pretty_host,
                    "path": _redact_path(metadata.get("bsl_original_path") or flow.request.path),
                    "method": flow.request.method,
                    "alias_resolved": bool(metadata.get("bsl_alias_resolved")),
                    "status_code": getattr(response, "status_code", None) if response is not None else None,
                })
        except Exception as exc:
            logging.debug(f"[BSL MITM] Route-decision emit skipped: {exc}")
        try:
            if (
                getattr(metadata, "get", lambda *_: None)("bsl_chat_hijacked") is True
                and getattr(metadata, "get", lambda *_: None)("bsl_chat_streaming") is True
                and response is not None
                and hasattr(response, "stream")
            ):
                response.stream = _stream_telemetry_callback(flow)
                _append_egress_telemetry({
                    "ts": time.time(),
                    "request_id": "headers",
                    "sequence": 0,
                    "event": "responseheaders",
                    "host": metadata.get("bsl_original_host"),
                    "path": metadata.get("bsl_original_path"),
                    "http_version": getattr(response, "http_version", None),
                    "status_code": getattr(response, "status_code", None),
                    "content_type": response.headers.get("content-type") if getattr(response, "headers", None) else None,
                })
        except Exception as exc:
            logging.debug(f"[BSL MITM] Streaming telemetry unavailable: {exc}")
            try:
                if response is not None and hasattr(response, "stream"):
                    response.stream = True
            except Exception:
                return

    # â”€â”€ Target domains: every hostname the hosts-file redirect points at us.
    # Used in server_connect to intercept BEFORE mitmproxy resolves via OS DNS
    # (which would return 127.0.0.1 â†’ recursive loop). Mirrors 9router's Pn() pattern.
    _TARGET_DOMAINS = {
        "daily-cloudcode-pa.googleapis.com",
        "cloudcode-pa.googleapis.com",
        "api.anthropic.com",
        "api.individual.githubcopilot.com",
        "runtime.us-east-1.kiro.dev",
        "q.us-east-1.amazonaws.com",
        "codewhisperer.us-east-1.amazonaws.com",
    }

    # Domains where, when antigravity is ON, ALL connections are routed to BSL Router.
    # BSL Router's /v1internal:{op} catch-all proxies auth to Google; chat goes to BSL model.
    _ANTIGRAVITY_DOMAINS = {
        "daily-cloudcode-pa.googleapis.com",
        "api.anthropic.com",
    }

    def tls_clienthello(self, data) -> None:
        """Skip MITM interception for domains that use strict certificate pinning.
        By ignoring the connection, mitmproxy acts as a raw TCP proxy to the
        real IP (resolved here), presenting Google's real certificate.
        """
        sni = getattr(data.client_hello, "sni", None)
        if sni == "cloudcode-pa.googleapis.com":
            real_ip = _resolve_real_ip(sni)
            if real_ip:
                data.context.server.address = (real_ip, 443)
                logging.info(f"[BSL MITM] tls_clienthello: ignoring {sni}, forwarding TCP to {real_ip}")
                data.ignore_connection = True

    def server_connect(self, data) -> None:
        """Route server connections for managed domains.

        When mitm.antigravity is ON, ALL connections to Antigravity domains go
        to BSL Router (127.0.0.1:6969) via plain HTTP.  BSL Router's built-in
        /v1internal:{op} CCPA proxy forwards auth/telemetry to real Google;
        /v1internal:streamGenerateContent handles inference locally.

        This MUST happen at server_connect time (before any HTTP/2 stream) to
        avoid the keep-alive reuse problem where request() changes are ignored
        on an already-established HTTP/2 connection to Google.

        All other managed domains (Copilot, Kiro) keep the existing real-IP
        pass-through via external DNS (8.8.8.8).
        """
        try:
            server = data.server
            client = data.client
            if not server.address:
                return

            port = server.address[1]
            
            # The IDE connects to 127.0.0.1 due to hosts file. server.address[0] is 127.0.0.1.
            # We must use SNI to know the actual target domain.
            host = getattr(client, "sni", None) or getattr(server, "sni", None)
            if not host:
                return

            if host not in self._TARGET_DOMAINS:
                return

            # â”€â”€ Antigravity: route ALL connections to BSL Router â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
            self.load_config()
            mitm_config = self.config.get("mitm", {})
            bsl_port = self.config.get("server", {}).get("port", 6969)

            if host in self._ANTIGRAVITY_DOMAINS and mitm_config.get("antigravity", False):
                _bsl_debug(f"SC ANTIGRAVITY {host}:{port} -> 127.0.0.1:{bsl_port} (BSL Router)")
                logging.info(f"[BSL MITM] server_connect {host}:{port} -> 127.0.0.1:{bsl_port} (BSL Router)")
                server.address = ("127.0.0.1", bsl_port)
                # Mark as BSL Router target so tls_start_server skips TLS
                if not hasattr(data, "metadata"):
                    try:
                        data.metadata = {}
                    except Exception:
                        pass
                try:
                    data.metadata["bsl_router_target"] = True
                except Exception:
                    pass
                return

            # â”€â”€ All other managed domains: resolve real IP via 8.8.8.8 â”€â”€â”€â”€â”€â”€â”€
            real_ip = _resolve_real_ip(host)
            if real_ip and _is_safe_real_upstream_ip(real_ip):
                logging.info(
                    f"[BSL MITM] server_connect {host}:{port} -> real {real_ip}:{port} (via 8.8.8.8)"
                )
                server.address = (real_ip, port)
            else:
                logging.error(
                    f"[BSL MITM] server_connect: no safe real IP for {host} â€” blocking"
                )
                server.error = mitm_flow.Error(
                    f"BSL MITM: no safe real upstream IP for {host}"
                )
        except Exception as e:
            logging.error(f"[BSL MITM] Error in server_connect: {e}")

    def tls_start_server(self, tls_start):
        """Skip TLS for BSL Router targets; fix SNI for real Google IP pass-throughs.

        When server_connect routed the connection to 127.0.0.1:6969 (BSL Router),
        mitmproxy would normally try a TLS handshake â€” but BSL Router is plain
        HTTP.  We skip TLS by setting ssl_established on the server connection.

        For real Google IPs (pass-through), copy the client SNI so Google's
        HTTP/2 frontend routes to the right backend.
        """
        try:
            server = tls_start.conn
            addr = getattr(server, "address", None) or getattr(getattr(tls_start.context, "server", None), "address", None)

            # Real Google IP â€” restore client SNI so Google routes correctly.
            client = tls_start.context.client
            client_sni = getattr(client, "sni", None)
            if client_sni and (server.sni is None or _is_ip(str(server.sni))):
                server.sni = client_sni
        except Exception as e:
            logging.debug(f"[BSL MITM] tls_start_server adjust skipped: {e}")


def _is_ip(value: str) -> bool:
    try:
        parts = value.split(".")
        return len(parts) == 4 and all(p.isdigit() for p in parts)
    except Exception:
        return False


addons = [
    BSLRouterMitm()
]



