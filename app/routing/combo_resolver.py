"""Combo/chain resolver - extracted from main.py _process_chat_completion.

Handles:
- Legacy alias -> combo redirect (Step -1)
- Combo chain resolution with nested expansion (Step 0)
- Combo fallback retry override (RC5/C2/C3/C5 constraints)
- Alias lookup (Step 1)
- Provider scan for bare model IDs (Step 2)
- Not-found error construction (Step 3)
- Combo chain segment resolution for logging

Design constraints preserved from original code:
- RC5: skip now-banned leaves during retry advance
- C2: use chain SNAPSHOT from _retry_state, NOT rebuilt chain
- C3: write back advanced idx to _retry_state
- C5: exhausted chain -> 502
- Round-robin mutates the passed-in round_robin_state dict in place
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from fastapi.responses import JSONResponse


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ComboResolution:
    """Result of combo resolution (Step 0)."""
    matched: bool = False
    target_model: Optional[str] = None
    provider_name: Optional[str] = None
    thinking_override: Optional[str] = None
    active_chain: Optional[list[tuple[str, str, Optional[str]]]] = None
    strategy: str = "fallback"


@dataclass
class RetryAdvance:
    """Result of combo fallback retry override."""
    exhausted: bool = False
    target_model: Optional[str] = None
    provider_name: Optional[str] = None
    thinking_override: Optional[str] = None
    active_chain: Optional[list[tuple[str, str, Optional[str]]]] = None
    idx: int = 0


# ---------------------------------------------------------------------------
# Step -1: Legacy alias -> combo redirect
# ---------------------------------------------------------------------------

def resolve_combo_alias_redirect(
    model: str,
    provider_name: Optional[str],
    config: dict,
) -> tuple[str, bool]:
    """Check if a legacy alias intentionally targets a Combo alias.

    UI selectors store Combo selections as provider=\"__combo__\" so exact MITM
    model keys (for example gemini-default) can route to configured fallback
    chains like coder-2 / GPT-5.5 / Opus-VA-Thinking.

    Returns (possibly_redirected_model, redirected: bool).
    """
    if not provider_name and model in config.get("aliases", {}):
        alias_config = config["aliases"][model]
        alias_target = alias_config.get("model", model)
        alias_provider = alias_config.get("provider")
        combo_ids = {c.get("alias") for c in config.get("combos", []) if isinstance(c, dict)}
        if alias_target in combo_ids and (not alias_provider or alias_provider == "__combo__"):
            return alias_target, True
    return model, False


# ---------------------------------------------------------------------------
# Step 0: Combo chain resolution
# ---------------------------------------------------------------------------

def _parse_chain_entry(
    chain_entry: Any,
    config: dict,
) -> tuple[str, str, Optional[str], Optional[str]]:
    """Parse a single chain entry into (model, target, provider, thinking).

    Supports three formats:
    1. dict: {"provider": "...", "model": "...", "thinking": "..."}
    2. "provider/model" string
    3. Bare model ID string
    """
    chain_model = chain_entry
    cm_target = chain_entry
    cm_provider = None
    cm_thinking = None

    if isinstance(chain_entry, dict):
        cm_provider = chain_entry.get("provider")
        cm_target = chain_entry.get("model") or chain_entry.get("id")
        cm_thinking = chain_entry.get("thinking")
        chain_model = cm_target
    elif isinstance(chain_entry, str) and "/" in chain_entry:
        maybe_provider, maybe_model = chain_entry.split("/", 1)
        if maybe_provider in config.get("providers", {}):
            cm_provider = maybe_provider
            cm_target = maybe_model
            chain_model = maybe_model

    # Legacy alias support: combo aliases stay bare by design.
    if not cm_provider and chain_model in config.get("aliases", {}):
        alias_cfg = config["aliases"][chain_model]
        cm_target = alias_cfg.get("model", chain_model)
        cm_provider = alias_cfg.get("provider")

    return chain_model, cm_target, cm_provider, cm_thinking


def _validate_provider_model(
    config: dict,
    provider: str,
    model_id: str,
) -> bool:
    """Check that a provider has the given model enabled with active connections."""
    prov_data = config.get("providers", {}).get(provider, {})
    matching_model = next(
        (m for m in prov_data.get("models", []) if m.get("id") == model_id and m.get("enabled", True)),
        None,
    )
    active_c = [c for c in prov_data.get("connections", []) if c.get("enabled", True)]
    return bool(matching_model and active_c)


def _expand_nested_combo(
    nested_entry: Any,
    config: dict,
    parent_thinking: Optional[str],
) -> list[tuple[str, str, Optional[str]]]:
    """Expand a nested combo's chain entries into concrete (model, provider, thinking) tuples."""
    results = []
    nested_model = nested_entry
    nested_provider = None
    nested_thinking = parent_thinking

    if isinstance(nested_entry, dict):
        nested_provider = nested_entry.get("provider")
        nested_model = nested_entry.get("model") or nested_entry.get("id")
        nested_thinking = parent_thinking or nested_entry.get("thinking")
    elif isinstance(nested_entry, str) and "/" in nested_entry:
        mp, mm = nested_entry.split("/", 1)
        if mp in config.get("providers", {}):
            nested_provider = mp
            nested_model = mm

    if nested_provider:
        if _validate_provider_model(config, nested_provider, nested_model):
            results.append((nested_model, nested_provider, nested_thinking))

    return results


def resolve_combo(
    model: str,
    provider_name: Optional[str],
    config: dict,
    round_robin_state: dict,
) -> ComboResolution:
    """Resolve a combo alias to its active chain and select the primary entry.

    Returns ComboResolution with matched=True if model is a combo alias.
    """
    if provider_name:
        return ComboResolution(matched=False)

    combo_thinking_override = None
    active_chain = None
    _combo_matched = False

    for combo in config.get("combos", []):
        if combo.get("alias") != model:
            continue

        strategy = combo.get("strategy", "fallback")
        chain = combo.get("chain", [])

        active_chain = []
        for chain_entry in chain:
            chain_model, cm_target, cm_provider, cm_thinking = _parse_chain_entry(chain_entry, config)

            # Nested combo resolution: a bare chain entry may reference another combo alias.
            if not cm_provider:
                nested_combo = next(
                    (c for c in config.get("combos", []) if c.get("alias") == chain_model),
                    None,
                )
                if nested_combo:
                    # Expand nested combo chain entries recursively into this chain.
                    for nested_entry in nested_combo.get("chain", []):
                        active_chain.extend(
                            _expand_nested_combo(nested_entry, config, cm_thinking)
                        )
                    continue

            if cm_provider:
                if _validate_provider_model(config, cm_provider, cm_target):
                    active_chain.append((cm_target, cm_provider, cm_thinking))
                continue

            # Backward compatibility for old bare model IDs.
            for prov_id, prov_data in config.get("providers", {}).items():
                for m in prov_data.get("models", []):
                    if m.get("id") == chain_model and m.get("enabled", True):
                        active_c = [c for c in prov_data.get("connections", []) if c.get("enabled", True)]
                        if active_c:
                            cm_provider = prov_id
                            break
                if cm_provider:
                    break
            if cm_provider:
                active_chain.append((cm_target, cm_provider, cm_thinking))

        if not active_chain:
            return ComboResolution(
                matched=True,
                target_model=None,
                provider_name=None,
            )

        # Pre-filter: remove chain entries currently under an Auto Error Prevention ban.
        try:
            import app.error_prevention as _ep_filter
            filtered_chain = []
            for cm_t, cm_p, cm_th in active_chain:
                banned, _, _ = _ep_filter.check_ban(config, cm_p, cm_t)
                if not banned:
                    filtered_chain.append((cm_t, cm_p, cm_th))
            if filtered_chain:
                active_chain = filtered_chain
            # If all banned, keep original list so downstream ban check surfaces 503.
        except Exception as _epf_err:
            print(f"[Combo] ban pre-filter failed (non-blocking): {_epf_err}")

        if strategy == "round_robin":
            idx = round_robin_state.get(model, 0) % len(active_chain)
            cm_target, cm_provider, combo_thinking_override = active_chain[idx]
            round_robin_state[model] = idx + 1
            target_model = cm_target
            provider_name = cm_provider
            _rr_chain_len = len(active_chain)
            # Round-robin is a selection strategy, not a fallback chain.
            # Disable downstream retry traversal to preserve RR semantics.
            active_chain = None
            print(f"[Combo] {model} > {cm_provider}/{cm_target} [rr-idx {idx}/{_rr_chain_len}, round_robin]", flush=True)
        else:
            # Fallback strategy: try the first model; on upstream failure the
            # downstream fallback loop advances to the next chain entry.
            cm_target, cm_provider, combo_thinking_override = active_chain[0]
            target_model = cm_target
            provider_name = cm_provider
            print(f"[Combo] {model} > {cm_provider}/{cm_target} [1/{len(active_chain)}, fallback-primary]", flush=True)

        _combo_matched = True
        return ComboResolution(
            matched=True,
            target_model=target_model,
            provider_name=provider_name,
            thinking_override=combo_thinking_override,
            active_chain=active_chain,
            strategy=strategy,
        )

    return ComboResolution(matched=False)


# ---------------------------------------------------------------------------
# Combo Fallback Retry Override (RC5 + C2 + C3 + C5)
# ---------------------------------------------------------------------------

def advance_combo_retry(
    _retry_state: dict,
    config: dict,
    combo_alias: str = "",
) -> RetryAdvance:
    """Advance past failed/banned combo chain entries on recursive retry.

    Preserves:
    - RC5: skip now-banned leaves
    - C2: use chain SNAPSHOT from _retry_state, NOT rebuilt chain
    - C3: write back advanced idx to _retry_state
    - C5: exhausted chain -> caller returns 502

    combo_alias: optional alias name prefixed onto the "skipping banned
    leaf" log line for parity with the pre-refactor format (default ""
    keeps backward compat).

    NOTE: this function deliberately does NOT print the selected
    "[Combo] ... fallback-retry" line. The caller in main.py owns that
    print (it has the richer local context). Printing it here too emitted
    the SAME line twice per retry, which reads in the logs as two
    attempts against one leaf — the exact confusion the C2/C3 constraints
    exist to prevent.
    """
    stable_chain = _retry_state["chain"]
    idx = _retry_state["idx"]

    import app.error_prevention as _ep
    while idx < len(stable_chain):
        cand_model, cand_provider, _cand_think = stable_chain[idx]
        banned, _, _ = _ep.check_ban(config, cand_provider, cand_model)
        if not banned:
            break
        print(f"[Combo] {combo_alias} > skipping banned leaf {cand_provider}/{cand_model} [{idx+1}/{len(stable_chain)}]", flush=True)
        idx += 1

    if idx >= len(stable_chain):
        # C5: exhausted-chain -> caller returns 502
        return RetryAdvance(exhausted=True, active_chain=stable_chain)

    _retry_state["idx"] = idx  # C3: write back advanced idx
    target_model, provider_name, thinking_override = stable_chain[idx]

    return RetryAdvance(
        exhausted=False,
        target_model=target_model,
        provider_name=provider_name,
        thinking_override=thinking_override,
        active_chain=stable_chain,
        idx=idx,
    )


# ---------------------------------------------------------------------------
# Step 1: Alias lookup
# ---------------------------------------------------------------------------

def resolve_alias(
    model: str,
    provider_name: Optional[str],
    config: dict,
) -> tuple[Optional[str], Optional[str]]:
    """Look up model in config aliases. Returns (target_model, provider_name)."""
    if not provider_name and model in config.get("aliases", {}):
        alias_config = config["aliases"][model]
        target_model = alias_config.get("model", model)
        provider_name = alias_config.get("provider")
        return target_model, provider_name
    return None, provider_name


# ---------------------------------------------------------------------------
# Step 2: Provider scan for bare model IDs
# ---------------------------------------------------------------------------

def find_provider_for_model(model: str, config: dict) -> Optional[str]:
    """Scan all providers' model lists for a match. Returns provider_id or None."""
    for prov_id, prov_data in config.get("providers", {}).items():
        for m in prov_data.get("models", []):
            if m.get("id") == model:
                return prov_id
    return None


# ---------------------------------------------------------------------------
# Step 3: Not-found error construction
# ---------------------------------------------------------------------------

def build_not_found_error(model: str, config: dict) -> JSONResponse:
    """Construct the 'Model not found' error response with known models list."""
    known_models = [
        m.get("id")
        for pv in config.get("providers", {}).values()
        for m in pv.get("models", [])
    ]
    alias_ids = list(config.get("aliases", {}).keys())
    combo_ids = [c.get("alias") for c in config.get("combos", [])]
    return JSONResponse(
        {
            "error": f"Model '{model}' not found. "
                     f"Known models: {known_models}. "
                     f"Known aliases: {alias_ids}. "
                     f"Known combos: {combo_ids}. "
                     "Please register this model under a provider in the admin panel."
        },
        status_code=404,
    )


# ---------------------------------------------------------------------------
# Combo chain segment resolution for logging
# ---------------------------------------------------------------------------

def resolve_combo_chain_segment(
    entry: str,
    config: dict,
    route_registry=None,
    _depth: int = 0,
) -> tuple[list[str], str]:
    """Recursively resolve a combo alias to its full chain segment for logging.

    Returns (chain_labels, final_model_id) where:
      - chain_labels is the list of intermediate alias names (for the > log)
      - final_model_id is the first concrete provider/model string

    Example: entry=\"coder-2\" with nested combos returns:
      ([\"Qwen3.7-Max\"], \"qwencoder/qwen3.7-max\")
    so the log shows: coder-2 > Qwen3.7-Max > qwencoder/qwen3.7-max
    """
    if _depth > 4:
        return [], entry

    _combos = config.get("combos", [])
    _combo_aliases = {c.get("alias"): c for c in _combos if c.get("alias")}

    labels: list[str] = []

    if entry not in _combo_aliases:
        # entry is a concrete model - resolve via route registry if available
        model_id = entry
        if route_registry:
            try:
                from app.middleware.route_registry import resolve_canonical_chain
                _resolved = resolve_canonical_chain(route_registry, [entry], enabled_only=True)
                if _resolved:
                    _, _rp, _rm = _resolved
                    if _rm:
                        model_id = f"{_rp}/{_rm}" if _rp else _rm
            except Exception:
                pass
        return labels, model_id

    # entry IS a combo alias - drill into its chain
    _combo_def = _combo_aliases[entry]
    _combo_chain = _combo_def.get("chain", [])
    if not _combo_chain:
        return labels, entry

    _first = _combo_chain[0]

    if isinstance(_first, dict):
        # Direct provider/model entry - terminal
        _p = _first.get("provider", "")
        _m = _first.get("model", "")
        if _p and _m:
            return labels, f"{_p}/{_m}"
        elif _m:
            return labels, _m
        return labels, entry

    elif isinstance(_first, str):
        # String entry - could be another combo alias, a concrete model,
        # or a "provider/model" direct reference
        if "/" in _first and _first not in _combo_aliases:
            # Already a concrete provider/model - terminal, don't add as label
            return labels, _first
        labels.append(_first)
        _sub_labels, _sub_model = resolve_combo_chain_segment(
            _first, config, route_registry, _depth + 1
        )
        labels.extend(_sub_labels)
        return labels, _sub_model

    return labels, entry
