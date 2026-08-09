"""
Shared model-to-connection resolver for Tool scouts.

Resolves a model ID or alias (including Combo aliases) to an active
provider connection. Combos live in config["combos"] as a list of
{alias, chain: [{provider, model, ...}, ...], strategy} dicts.

Resolution order:
  1. Combo alias match → take first chain entry → resolve provider+model
  2. Legacy alias match (config["aliases"]) → resolve provider+model
  3. Direct model ID scan across all providers

Returns (connection_dict_or_None, resolved_model_id_str).

The returned connection is a shallow copy carrying the owning provider's
`format` and `type`, because callers that dial upstreams directly need to
know which wire protocol to speak and `format` lives on the provider, not
on the connection.
"""

from typing import Dict, Any, Tuple, Optional, List
import random


# ─── Connection selection helpers ────────────────────────────────────────────

def _get_model_meta(provider_config: dict, model_id: str) -> Optional[dict]:
    """Return the model metadata dict for model_id in provider_config, or None."""
    for m in provider_config.get("models", []):
        if isinstance(m, dict) and m.get("id") == model_id:
            return m
    return None


def _with_provider_meta(conn: dict, provider_config: dict) -> dict:
    """Shallow-copy conn and attach the provider-level format/type.

    A connection dict on its own cannot tell a caller which wire format to
    speak — `format` is declared one level up, on the provider. Tool scouts
    dial upstreams themselves (they cannot re-enter the router without
    deadlocking), so they need that format to build the right endpoint and
    auth headers.

    Copies rather than mutates so the live config object is never touched.
    `setdefault` lets an explicit per-connection override win.
    """
    enriched = dict(conn)
    enriched.setdefault("format", provider_config.get("format"))
    enriched.setdefault("type", provider_config.get("type"))
    return enriched


def _pick_connection(
    provider_config: dict,
    model_id: str,
    provider_name: str = "",
    breaker=None,
) -> Tuple[Optional[dict], Optional[int]]:
    """Pick (enriched_conn, original_index) honoring connection_indexes + breaker.

    - Enumerates connections with their original index, keeps only enabled ones.
    - If model metadata has a non-empty list[int] connection_indexes:
        · filters the enabled connections to only those whose original index is in the list.
        · if none qualify (all disabled), returns None.
    - If metadata is missing or connection_indexes is missing/invalid/empty:
        · legacy behavior — random choice among all enabled connections.
    - When breaker is provided and enabled, filters OPEN connections first.
      Fail-open: an exception inside the breaker never blocks selection.
    - Returns (None, None) when nothing qualifies.
    """
    connections: List[dict] = provider_config.get("connections", [])

    # Build list of {"index": original_index, "conn": conn} — the shape
    # CircuitBreaker.filter_healthy_connections expects.
    enabled: List[Dict[str, Any]] = [
        {"index": i, "conn": c}
        for i, c in enumerate(connections)
        if isinstance(c, dict) and c.get("enabled", True)
    ]

    if not enabled:
        return None, None

    meta = _get_model_meta(provider_config, model_id)

    # Determine whether metadata contains a valid, non-empty list of ints
    indexes = None
    if meta is not None:
        raw = meta.get("connection_indexes")
        if isinstance(raw, list) and len(raw) > 0 and all(isinstance(x, int) for x in raw):
            indexes = set(raw)

    eligible = [e for e in enabled if e["index"] in indexes] if indexes is not None else enabled
    if not eligible:
        return None, None

    if breaker is not None:
        try:
            if getattr(breaker, "enabled", False):
                eligible = breaker.filter_healthy_connections(provider_name, model_id, eligible)
        except Exception:
            pass  # fail-open: the breaker is an optimization, never a gate

    if not eligible:
        return None, None

    picked = random.choice(eligible)
    return _with_provider_meta(picked["conn"], provider_config), picked["index"]


def _choose_connection_for_model(
    provider_config: dict,
    model_id: str,
    provider_name: str = "",
    breaker=None,
) -> Optional[dict]:
    """
    Choose a connection for (provider_config, model_id) respecting connection_indexes.

    Backward-compatible wrapper over _pick_connection returning just the
    connection dict (or None). Optional breaker filters OPEN connections.
    """
    conn, _ = _pick_connection(provider_config, model_id, provider_name, breaker)
    return conn


def resolve_active_connection(
    config: Dict[str, Any],
    provider_name: str,
    model_id: str,
    breaker=None,
) -> Tuple[Optional[dict], Optional[int]]:
    """Resolve to (connection_dict, original_index) for main dispatch paths.

    1. Get provider_config from config["providers"][provider_name]
    2. Enumerate enabled connections with their original index
    3. Filter by model metadata connection_indexes if present
    4. Filter by circuit breaker if breaker is provided and enabled
    5. Random choice among remaining
    6. Return (enriched_connection_dict, original_index) or (None, None)

    The connection dict is enriched with provider-level format/type via
    _with_provider_meta. The original index lets callers track per-connection
    health in the circuit breaker after the request completes.
    """
    provider_config = config.get("providers", {}).get(provider_name)
    if not isinstance(provider_config, dict):
        return None, None
    return _pick_connection(provider_config, model_id, provider_name, breaker)


# ─── Internal helpers ─────────────────────────────────────────────────────────

def _get_combo_aliases(config: Dict[str, Any]) -> set:
    """Return a set of all combo alias names for quick membership check."""
    result = set()
    for combo in config.get("combos", []):
        if isinstance(combo, dict) and combo.get("alias"):
            result.add(combo["alias"])
    return result


def _resolve_chain_entry(
    entry: Any,
    config: Dict[str, Any],
) -> Optional[Tuple[dict, str]]:
    """
    Resolve a single combo chain entry to (connection, model).

    Chain entries can be:
      - dict: {"provider": "vsllm-a", "model": "glm-5.2", ...}
      - str (combo alias): "GLM-5.1" -> resolve_model_conn recursively
      - str (provider/model): "vietapi-a/kimi-k2.6"
    """
    # Dict entry: direct provider+model
    if isinstance(entry, dict):
        prov_name = entry.get("provider")
        real_model = entry.get("model", "")
        if prov_name and real_model:
            prov = config.get("providers", {}).get(prov_name, {})
            conn = _choose_connection_for_model(prov, real_model)
            if conn is not None:
                return conn, real_model
        return None

    # String entry: could be combo alias or provider/model shorthand
    if isinstance(entry, str):
        # Try as combo alias first (recursive)
        if entry in _get_combo_aliases(config):
            return resolve_model_conn(config, entry)

        # Try as provider/model shorthand
        if "/" in entry:
            parts = entry.split("/", 1)
            prov_name, real_model = parts[0], parts[1]
            prov = config.get("providers", {}).get(prov_name, {})
            conn = _choose_connection_for_model(prov, real_model)
            if conn is not None:
                return conn, real_model
            return None

        # Try as legacy alias
        aliases = config.get("aliases", {})
        if isinstance(aliases, dict) and entry in aliases:
            alias_cfg = aliases[entry]
            real_model = alias_cfg.get("model", entry)
            prov_name = alias_cfg.get("provider")
            if prov_name:
                prov = config.get("providers", {}).get(prov_name, {})
                conn = _choose_connection_for_model(prov, real_model)
                if conn is not None:
                    return conn, real_model

        # Try as direct model ID scan
        for prov_id, prov_data in config.get("providers", {}).items():
            if not isinstance(prov_data, dict):
                continue
            for m in prov_data.get("models", []):
                if isinstance(m, dict) and m.get("id") == entry and m.get("enabled", True):
                    conn = _choose_connection_for_model(prov_data, entry)
                    if conn is not None:
                        return conn, entry

    return None


def resolve_model_conn(
    config: Dict[str, Any],
    model_or_alias: str,
) -> Tuple[Optional[dict], str]:
    """
    Resolve a model ID, legacy alias, or Combo alias to an active connection.

    Args:
        config: The full config dict (must contain "providers", optionally "combos"/"aliases").
        model_or_alias: The model ID, alias, or combo alias string.

    Returns:
        (connection_dict, resolved_model_id) or (None, model_or_alias) if not found.
    """
    if not model_or_alias:
        return None, model_or_alias

    # ─── 1. Combo alias resolution ───────────────────────────────────
    combos = config.get("combos", [])
    if isinstance(combos, list):
        for combo in combos:
            if not isinstance(combo, dict):
                continue
            if combo.get("alias") == model_or_alias:
                chain = combo.get("chain", [])
                if chain and len(chain) > 0:
                    # Try each chain entry until we find an active connection
                    for entry in chain:
                        resolved = _resolve_chain_entry(entry, config)
                        if resolved:
                            return resolved
                    # All chain entries exhausted — continue to other resolution methods
                    break

    # ─── 2. Legacy alias resolution ──────────────────────────────────
    aliases = config.get("aliases", {})
    if isinstance(aliases, dict) and model_or_alias in aliases:
        alias_cfg = aliases[model_or_alias]
        real_model = alias_cfg.get("model", model_or_alias)
        prov_name = alias_cfg.get("provider")
        if prov_name:
            prov = config.get("providers", {}).get(prov_name, {})
            conn = _choose_connection_for_model(prov, real_model)
            if conn is not None:
                return conn, real_model
        # Alias exists but provider/connection not found — fall through to scan
        model_or_alias = real_model

    # ─── 3. Direct model ID scan ─────────────────────────────────────
    for prov_id, prov_data in config.get("providers", {}).items():
        if not isinstance(prov_data, dict):
            continue
        for m in prov_data.get("models", []):
            if isinstance(m, dict) and m.get("id") == model_or_alias and m.get("enabled", True):
                conn = _choose_connection_for_model(prov_data, model_or_alias)
                if conn is not None:
                    return conn, model_or_alias

    return None, model_or_alias
