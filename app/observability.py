import time
import json
import os
import httpx
from datetime import datetime

# Append-only JSONL persistence paths (survive server restarts).
# In-memory lists below remain the hot read path; every new entry is also
# appended to disk, and the tail is reloaded on startup.
_USAGE_LOG_PATH = "data/usage_stats.jsonl"
_CONSOLE_LOG_PATH = "data/console_logs.jsonl"

# ── Log rotation guard ───────────────────────────────────────────────────────
# Prevents unbounded JSONL growth. When a log file exceeds _MAX_LOG_FILE_SIZE,
# it is rewritten with only the most recent _MAX_LOG_ENTRIES entries.
_MAX_LOG_FILE_SIZE = 5_000_000  # 5 MB
_MAX_LOG_ENTRIES = 5_000        # Keep last 5k entries after rotation


def _rotate_log_file(path, max_entries=_MAX_LOG_ENTRIES):
    """Rewrite a JSONL file keeping only the tail entries. Fail-open."""
    try:
        if not os.path.exists(path):
            return
        with open(path, "r", encoding="utf-8") as f:
            all_lines = f.readlines()
        if len(all_lines) <= max_entries:
            return
        tail_lines = all_lines[-max_entries:]
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(tail_lines)
        print(f"[Observability] Rotated '{path}': kept last {max_entries}/{len(all_lines)} entries.", flush=True)
    except Exception as _err:
        print(f"[Observability] rotate failed (non-blocking): {_err}", flush=True)


def _persist_entry(path, entry):
    """Append a single entry as one JSON line to a JSONL file. Fail-open.

    Includes automatic log rotation: if the file exceeds _MAX_LOG_FILE_SIZE
    after appending, it is rewritten with only the most recent entries.
    """
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        # Check if rotation is needed after append
        try:
            if os.path.getsize(path) > _MAX_LOG_FILE_SIZE:
                _rotate_log_file(path)
        except OSError:
            pass
    except Exception as _err:
        # Persistence must NEVER break request logging.
        try:
            print(f"[Observability] persist_entry failed (non-blocking): {_err}", flush=True)
        except UnicodeEncodeError:
            print(f"[Observability] persist_entry failed (non-blocking): " + str(_err).encode('ascii', 'replace').decode('ascii'), flush=True)


def _load_persisted(path, max_entries=10000):
    """Read up to `max_entries` most-recent entries from a JSONL file. Fail-open.

    Reads the tail of the file so the latest entries are kept when truncating
    to `max_entries`. Corrupt / empty lines are skipped.
    """
    try:
        if not os.path.exists(path):
            return []
        with open(path, "r", encoding="utf-8") as f:
            all_lines = f.readlines()
    except Exception as _err:
        print(f"[Observability] load_persisted failed (non-blocking): {_err}", flush=True)
        return []

    # Keep only the most recent `max_entries` lines before parsing.
    tail_lines = all_lines[-max_entries:] if len(all_lines) > max_entries else all_lines
    entries = []
    for line in tail_lines:
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except (json.JSONDecodeError, ValueError):
            # Skip corrupt lines; fail-open on a per-line basis.
            continue
    return entries


# In-memory storage for Observability Layer (hot read path for the API).
# Pre-populated from disk so usage/console history survives restarts.
error_reports = []
usage_stats = _load_persisted(_USAGE_LOG_PATH)
console_logs = _load_persisted(_CONSOLE_LOG_PATH)

# Cleanup stale 'start' events from previous runs that never got an 'end' event.
# Mutating them in memory prevents the UI from showing them as permanently pending.
_completed_reqs = {log["request_id"] for log in console_logs if log.get("event") == "end" and "request_id" in log}
for log in console_logs:
    if log.get("event") == "start" and log.get("request_id") not in _completed_reqs:
        log["event"] = "end"
        log["error"] = "Server restarted before completion (stale request)"
        log["status"] = 500
        log["ttft_ms"] = 0
        log["total_time_ms"] = 0
        log["in_tokens"] = 0
        log["out_tokens"] = 0
        log["cached_tokens"] = 0
        log["cache_write_tokens"] = 0

_combo_registry: dict = {}

# Request-id -> index of the authoritative in-memory END event. Streaming
# generators can finalize through both an inner `finally` and an outer
# cancellation guard; this registry prevents contradictory 200/499 rows.
_terminal_end_registry: dict = {}

# Ensure artifacts directory exists
os.makedirs("artifacts/error_reports", exist_ok=True)

def _load_model_costs(config: dict) -> dict:
    """Extract $/1M token rates: config.yaml first, canonical registry fallback.

    Two-layer resolution ensures cost/savings compute correctly even when
    config.yaml models lack explicit cost_in/cost_out/cost_cache fields.
    The registry (data/model_pricing_registry.json) uses canonical keys like
    'zhipu:glm-5.2'; we fuzzy-match provider model IDs (e.g. 'glm-5.2-anthropic')
    by checking if any registry pattern is a substring of the model ID.
    """
    rates = {}

    # Layer 1: Explicit operator overrides from config.yaml
    for prov_id, prov_data in config.get("providers", {}).items():
        for m in prov_data.get("models", []):
            model_id = m.get("id", "")
            if not model_id:
                continue
            base_cost = float(m.get("cost_per_1m", 0.0))
            cost_in = float(m.get("cost_in", base_cost))
            cost_out = float(m.get("cost_out", base_cost))
            default_cache = cost_in * 0.1 if ("claude" in model_id or "deepseek" in model_id) else cost_in * 0.5
            cost_cache = float(m.get("cost_cache", default_cache))
            rates[model_id] = {"in": cost_in, "out": cost_out, "cache": cost_cache}

    # Layer 2: Canonical registry fallback (fail-open)
    try:
        registry = _load_pricing_registry()
        if registry:
            family_index = []
            for fam_key, fam_data in registry.items():
                cin = float(fam_data.get("input_per_1m") or 0)
                cout = float(fam_data.get("output_per_1m") or 0)
                if cin == 0 and cout == 0:
                    continue
                ccache = float(fam_data.get("cache_hit_per_1m") or 0)
                patterns = list(fam_data.get("patterns") or [])
                canonical = fam_data.get("canonical_model") or ""
                if canonical:
                    patterns.append(canonical)
                family_index.append((set(patterns), cin, cout, ccache))

            for prov_id, prov_data in config.get("providers", {}).items():
                for m in prov_data.get("models", []):
                    mid = m.get("id", "")
                    if not mid:
                        continue
                    existing = rates.get(mid, {})
                    if existing.get("in", 0) > 0 or existing.get("out", 0) > 0:
                        continue
                    mid_l = mid.lower()
                    mid_dash = mid_l.replace(".", "-")
                    for patterns, cin, cout, ccache in family_index:
                        for pat in patterns:
                            if not pat:
                                continue
                            pat_l = pat.lower()
                            pat_dash = pat_l.replace(".", "-")
                            if pat_l in mid_l or pat_dash in mid_dash:
                                rates[mid] = {"in": cin, "out": cout, "cache": ccache}
                                break
                        if rates.get(mid, {}).get("in", 0) > 0:
                            break
    except Exception as _e:
        print(f"[PricingRegistry] Fallback load failed (non-blocking): {_e}", flush=True)

    return rates


def _load_pricing_registry() -> dict:
    """Load canonical pricing registry from disk (fail-open)."""
    registry_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "data", "model_pricing_registry.json"
    )
    if not os.path.exists(registry_path):
        return {}
    with open(registry_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("canonical_models", {})

def _short_request_id() -> str:
    """Generate a compact trace id for correlating START/END console lines."""
    return f"req_{int(time.time() * 1000):x}_{os.urandom(2).hex()}"


def _safe_trace_value(value, max_len: int = 160):
    """Keep trace values compact and avoid accidental secret dumps."""
    if value is None:
        return None
    text = str(value)
    secret_markers = ("sk-", "api_key", "authorization", "bearer ")
    if any(marker in text.lower() for marker in secret_markers):
        return "[redacted]"
    return text if len(text) <= max_len else text[:max_len] + "…"


def log_request_start(
    provider: str,
    model: str,
    config: dict,
    stream: bool = False,
    client: str = None,
    upstream_url: str = None,
    request_id: str = None,
    thinking: dict = None,
    combo: str = None,
) -> str:
    """Log the beginning of a proxied request to dashboard memory and stdout."""
    request_id = request_id or _short_request_id()
    now = datetime.now()
    entry = {
        "timestamp": now.isoformat(),
        "event": "start",
        "request_id": request_id,
        "provider": provider,
        "model": model,
        "stream": bool(stream),
    }
    if combo and combo != model:
        entry["combo"] = combo
        # Store in registry so END log_request can pick it up without
        # needing a combo= param at every call site.
        _combo_registry[request_id] = combo
        if len(_combo_registry) > 5000:
            # Evict oldest 200 entries to keep memory bounded.
            for _old in list(_combo_registry.keys())[:200]:
                _combo_registry.pop(_old, None)
    if client:
        entry["client"] = client
    if upstream_url:
        entry["upstream_url"] = _safe_trace_value(upstream_url)
    if thinking:
        # Reasoning/thinking knobs actually applied to the upstream payload
        # (e.g. {"effort": "max", "reasoning_mode": "pro"}). Fail-open, never
        # required, and values are scrubbed like any other trace field.
        try:
            entry["thinking"] = {
                str(k): _safe_trace_value(v) for k, v in thinking.items() if v is not None
            }
        except Exception:
            pass

    console_logs.append(entry)
    if len(console_logs) > 10000:
        console_logs.pop(0)
    _persist_entry(_CONSOLE_LOG_PATH, entry)

    _thinking_str = ""
    if entry.get("thinking"):
        try:
            _thinking_str = " thinking=" + ",".join(
                f"{k}:{v}" for k, v in entry["thinking"].items()
            )
        except Exception:
            _thinking_str = ""

    msg = (
        f"[BSL][{request_id}] START client={client or 'openai'} "
        f"provider={provider} model={model} stream={bool(stream)} "
        f"upstream={_safe_trace_value(upstream_url) or '-'}{_thinking_str}"
    )
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        print(msg.encode('ascii', 'replace').decode('ascii'), flush=True)

    return request_id


def log_request(
    provider: str,
    model: str,
    status: int,
    ttft: float,
    in_tokens: int,
    out_tokens: int,
    cached_tokens: int,
    config: dict,
    error_msg: str = None,
    total_time: float = None,
    request_id: str = None,
    event: str = "end",
    client: str = None,
    stream: bool = None,
    upstream_url: str = None,
    conn_index: int = None,
    thinking: dict = None,
    cache_write_tokens: int = 0,
    combo: str = None,
):
    """Log a request to Usage tracker, dashboard console, and stdout trace."""
    now = datetime.now()
    timestamp = now.isoformat()
    ttft_ms = round(ttft * 1000, 2) if ttft else 0.0
    total_ms = round(total_time * 1000, 2) if total_time is not None else None

    # 1. Console Log entry
    log_entry = {
        "timestamp": timestamp,
        "event": event,
        "provider": provider,
        "model": model,
        "status": status,
        "ttft_ms": ttft_ms,
        "total_time_ms": total_ms,
        "in_tokens": in_tokens,
        "out_tokens": out_tokens,
        "cached_tokens": cached_tokens,
        "cache_write_tokens": cache_write_tokens,
    }
    if combo and combo != model:
        log_entry["combo"] = combo
    # Auto-pick combo from registry when not explicitly supplied.
    # Use get() not pop() — the same request_id may appear in multiple
    # log_request calls (probe failures, stream finally blocks, etc.).
    # The registry is bounded and evicted in log_request_start.
    elif request_id and request_id in _combo_registry:
        _resolved_combo = _combo_registry.get(request_id)
        if _resolved_combo and _resolved_combo != model:
            log_entry["combo"] = _resolved_combo
    if request_id:
        log_entry["request_id"] = request_id
    if client:
        log_entry["client"] = client
    if stream is not None:
        log_entry["stream"] = bool(stream)
    if upstream_url:
        log_entry["upstream_url"] = _safe_trace_value(upstream_url)
    if error_msg:
        log_entry["error"] = _safe_trace_value(error_msg, max_len=500)
    if thinking:
        try:
            log_entry["thinking"] = {
                str(k): _safe_trace_value(v) for k, v in thinking.items() if v is not None
            }
        except Exception:
            pass

    # A streaming request can reach log_request from both an inner generator
    # finally block and an outer cancellation guard. Keep one visible END row.
    # A non-200 result is more authoritative than a provisional 200, so replace
    # the in-memory row when the later event reports a disconnect/error.
    _skip_side_effects = False
    if event == "end" and request_id and request_id in _terminal_end_registry:
        _existing_idx = _terminal_end_registry[request_id]
        _existing = console_logs[_existing_idx] if 0 <= _existing_idx < len(console_logs) else None
        if _existing is not None:
            _existing_status = _existing.get("status")
            _replace_existing = _existing_status == 200 and status != 200
            if _replace_existing:
                console_logs[_existing_idx] = log_entry
                _persist_entry(_CONSOLE_LOG_PATH, {**log_entry, "event": "end_correction"})
                # The provisional 200 was already recorded as a success by
                # ep.record_outcome / CircuitBreaker.record_outcome in the
                # first pass. Re-record with the corrected (non-200) status so
                # error prevention and circuit breaker see the real outcome.
                try:
                    import app.error_prevention as ep
                    ep.record_outcome(config, provider, model, status, error_msg, out_tokens=out_tokens)
                except Exception as _ep_err:
                    print(f"[ErrorPrevention] correction record_outcome failed (non-blocking): {_ep_err}", flush=True)
                try:
                    from app.circuit_breaker import get_breaker
                    _breaker = get_breaker()
                    if _breaker and _breaker.enabled and conn_index is not None:
                        _breaker.record_outcome(
                            provider, model, conn_index, status, error_msg, out_tokens=out_tokens,
                        )
                except Exception as _cb_err:
                    print(f"[CircuitBreaker] correction record_outcome failed (non-blocking): {_cb_err}", flush=True)
            _skip_side_effects = True
    # Clean up the registry entry once the terminal event has been processed
    # (either replaced or appended) so it doesn't leak for the server lifetime.
    if event == "end" and request_id and request_id in _terminal_end_registry and _skip_side_effects:
        del _terminal_end_registry[request_id]
    
    if not _skip_side_effects:
        console_logs.append(log_entry)
        if event == "end" and request_id:
            _terminal_end_registry[request_id] = len(console_logs) - 1
        _persist_entry(_CONSOLE_LOG_PATH, log_entry)

        if len(console_logs) > 10000:
            console_logs.pop(0)
            # Decrement all indices by 1 instead of clearing, so in-flight
            # streaming requests retain their dedup tracking.
            _to_keep = {k: v - 1 for k, v in _terminal_end_registry.items() if v > 0}
            _terminal_end_registry.clear()
            _terminal_end_registry.update(_to_keep)

    trace_id = request_id or "noid"
    _thinking_str = ""
    if log_entry.get("thinking"):
        try:
            _thinking_str = " thinking=" + ",".join(
                f"{k}:{v}" for k, v in log_entry["thinking"].items()
            )
        except Exception:
            _thinking_str = ""
    _combo_str = f" combo={log_entry['combo']}" if log_entry.get("combo") else ""
    msg = (
        f"[BSL][{trace_id}] {event.upper()} status={status} "
        f"client={client or '-'} provider={provider} model={model}{_combo_str} "
        f"ttft={ttft_ms}ms total={total_ms if total_ms is not None else '-'}ms "
        f"in={in_tokens} out={out_tokens} cached={cached_tokens} "
        f"error={_safe_trace_value(error_msg, max_len=180) if error_msg else '-'}{_thinking_str}"
    )
    if not _skip_side_effects:
        try:
            print(msg, flush=True)
        except UnicodeEncodeError:
            print(msg.encode('ascii', 'replace').decode('ascii'), flush=True)

    # Auto Error Prevention — record outcome (may softban/longban/disable model).
    # Fail-open: prevention logic must never break request logging.
    if not _skip_side_effects:
        try:
            import app.error_prevention as ep
            ep.record_outcome(config, provider, model, status, error_msg, out_tokens=out_tokens)
        except Exception as _ep_err:
            print(f"[ErrorPrevention] record_outcome failed (non-blocking): {_ep_err}", flush=True)

        # Circuit Breaker — record per-connection outcome (may OPEN a bad connection).
        # Fail-open: breaker logic must never break request logging.
        try:
            from app.circuit_breaker import get_breaker
            _breaker = get_breaker()
            if _breaker and _breaker.enabled and conn_index is not None:
                _breaker.record_outcome(
                    provider, model, conn_index, status, error_msg, out_tokens=out_tokens,
                )
        except Exception as _cb_err:
            print(f"[CircuitBreaker] record_outcome failed (non-blocking): {_cb_err}", flush=True)

    # 2. Usage / Cost Tracking
    if status == 200:
        rates_map = _load_model_costs(config)
        m_rates = rates_map.get(model, {"in": 0.0, "out": 0.0, "cache": 0.0})
        
        in_write = cache_write_tokens
        in_uncached = max(0, in_tokens - cached_tokens - in_write)
        
        # Precise cost formula
        cost_uncached_in = (in_uncached / 1_000_000) * m_rates["in"]
        cost_write_in = (in_write / 1_000_000) * (m_rates["in"] * 1.25)
        cost_cached_in = (cached_tokens / 1_000_000) * m_rates["cache"]
        cost_out_total = (out_tokens / 1_000_000) * m_rates["out"]
        
        cost = cost_uncached_in + cost_write_in + cost_cached_in + cost_out_total
        
        # Savings = What we WOULD have paid (in_tokens * in_rate) minus what we DID pay (in_uncached * in_rate + cached * cache_rate)
        # Simplified: cached_tokens * (in_rate - cache_rate)
        cache_savings = (cached_tokens / 1_000_000) * (m_rates["in"] - m_rates["cache"]) if cached_tokens else 0.0
        
        usage_entry = {
            "timestamp": timestamp,
            "provider": provider,
            "model": model,
            "ttft_ms": ttft_ms,
            "total_time_ms": total_ms,
            "in_cached": cached_tokens,
            "cache_write_tokens": cache_write_tokens,
            "in_uncached": in_uncached,
            "out": out_tokens,
            "cost": cost,
            "savings": cache_savings
        }
        usage_stats.append(usage_entry)
        if len(usage_stats) > 10000:
            usage_stats.pop(0)
        _persist_entry(_USAGE_LOG_PATH, usage_entry)


def recompute_usage_costs(config: dict):
    """Retroactively recalculate cost/savings for ALL historical usage entries
    using the current pricing registry. Fixes entries logged with cost=0 when
    the pricing registry was not yet loaded or was incomplete at request time.

    Mutates usage_stats entries in-place so the API returns corrected values.
    """
    try:
        rates_map = _load_model_costs(config)
        if not rates_map:
            return
        for entry in usage_stats:
            model = entry.get("model", "")
            m_rates = rates_map.get(model)
            if not m_rates:
                continue
            cached = entry.get("in_cached", 0) or 0
            write_tokens = entry.get("cache_write_tokens", 0) or 0
            uncached = entry.get("in_uncached", 0) or 0
            out = entry.get("out", 0) or 0

            cost_uncached_in = (uncached / 1_000_000) * m_rates["in"]
            cost_write_in = (write_tokens / 1_000_000) * (m_rates["in"] * 1.25)
            cost_cached_in = (cached / 1_000_000) * m_rates["cache"]
            cost_out_total = (out / 1_000_000) * m_rates["out"]
            entry["cost"] = round(cost_uncached_in + cost_write_in + cost_cached_in + cost_out_total, 6)
            entry["savings"] = round(
                (cached / 1_000_000) * (m_rates["in"] - m_rates["cache"]), 6
            ) if cached else 0.0
    except Exception as _e:
        print(f"[Observability] recompute_usage_costs failed (non-blocking): {_e}", flush=True)

async def run_error_analysis(http_client: httpx.AsyncClient, config: dict):
    """
    Extract errors from console_logs and use Routing Manager AP to analyze them.
    Saves an artifact file.
    """
    global console_logs
    # 1. Extract errors
    errors = [log for log in console_logs if log.get("error")]
    if not errors:
        return  # Nothing to analyze
        
    # 2. Get Routing Manager AP Profile
    ap_profile_path = "artifacts/bsl_router_ap_profile.json"
    ap_instructions = ""
    try:
        with open(ap_profile_path, "r", encoding="utf-8") as f:
            ap_data = json.load(f)
            # Flatten the AP DNA into instructions
            dna = ap_data.get("voiceDNA", {})
            ap_instructions = (
                f"Role: {dna.get('archetype', 'Routing Manager')}\n"
                f"Core Philosophy: {dna.get('corePhilosophy', '')}\n"
                f"Voice: {', '.join(dna.get('voiceQualities', []))}\n"
                f"Avoid: {', '.join(dna.get('bannedElements', []))}\n"
            )
    except Exception:
        ap_instructions = "You are the Routing Manager. Analyze these errors technically and decisively."

    error_summary = json.dumps(errors, indent=2)
    
    prompt = (
        f"{ap_instructions}\n\n"
        f"You must analyze the following proxy errors extracted from the BSL Router console log. "
        f"Generate a structured Markdown report with:\n"
        f"1. Error classification\n"
        f"2. Root cause hypothesis\n"
        f"3. Impact severity\n"
        f"4. Recommended action\n\n"
        f"Errors:\n{error_summary}"
    )

    # 3. Use the cheapest/fastest model available for analysis (usually gpt-4o-mini or gemini-flash)
    analysis_model = "gpt-4o-mini"
    t = config.get("tools", {})
    if t.get("docs_summary_model"):
        analysis_model = t["docs_summary_model"]
        
    # Resolve connection
    provider_name = None
    if analysis_model in config.get("aliases", {}):
        provider_name = config["aliases"][analysis_model].get("provider")
        analysis_model = config["aliases"][analysis_model].get("model", analysis_model)
        
    if not provider_name:
        for prov_id, prov_data in config.get("providers", {}).items():
            for m in prov_data.get("models", []):
                if m.get("id") == analysis_model:
                    provider_name = prov_id
                    break
            if provider_name:
                break
                
    if not provider_name:
        return
        
    provider_config = config.get("providers", {}).get(provider_name, {})
    active_connections = [c for c in provider_config.get("connections", []) if c.get("enabled", True)]
    if not active_connections:
        return
        
    import random
    active_conn = random.choice(active_connections)
    
    payload = {
        "model": analysis_model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.40,
        "top_p": 0.85,
        "max_tokens": 4096,
    }
    
    headers = {
        "Authorization": f"Bearer {active_conn['api_key']}",
        "Content-Type": "application/json"
    }

    base_url = active_conn.get("base_url", "").rstrip("/")
    
    try:
        resp = await http_client.post(f"{base_url}/chat/completions", json=payload, headers=headers, timeout=300.0)
        resp.raise_for_status()
        report_text = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "")
        
        # Save Artifact
        now = datetime.now()
        filename = f"error_report_{now.strftime('%Y%m%d_%H%M%S')}.md"
        filepath = os.path.join("artifacts/error_reports", filename)
        
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(report_text)
            
        error_reports.append({
            "timestamp": now.isoformat(),
            "filename": filename,
            "filepath": filepath,
            "error_count": len(errors)
        })
        
        # Clear errors from console_logs after analysis
        console_logs = [log for log in console_logs if not log.get("error")]
        
    except Exception as e:
        print(f"[Observability] Error analysis failed: {e}")
