"""Fuzzy model-ID normalization for BSL Router.

Fixes the dash-vs-dot / token-order mismatch class of 404s: requesting
``gpt-5-6-terra`` or ``gpt-terra-5-6`` must resolve to a registered
``gpt-5.6-terra``.

Design constraints (2026-08-24):
- EXACT MATCH ALWAYS WINS. ``fuzzy_resolve_model`` returns None when the
  requested name is already exactly known to combos, aliases, or any
  provider's model list — fuzzy correction never rewrites a valid name.
- Pure functions over the config dict — no I/O, no global state, so the
  resolver is trivially testable and safe to call anywhere in the routing
  ladder.
- Canonical key is order-insensitive between text tokens and numbers:
  consecutive numeric tokens fold into one version tuple (``5-6`` ==
  ``5.6`` == ``5_6``), alpha tokens are collected as a sorted tuple.
"""

from __future__ import annotations

import re
from typing import Optional

_TOKEN_SPLIT_RE = re.compile(r"[^0-9a-z]+")
_NUMERIC_RE = re.compile(r"^[0-9]+$")


def canonical_key(model_id: str):
    """Return a canonical, order-insensitive key for a model ID.

    Lowercase, split on every non-alphanumeric character, then fold runs of
    consecutive numeric tokens into a version tuple. Multiple numeric runs
    are kept as separate tuples so ``gpt-5.6-preview`` never collides with
    ``gpt-5.6-preview-2024``. The key shape is always
    ``((alpha tokens sorted), (version tuples...))`` — comparing two keys of
    the same shape never mixes ints with tuples.

    'gpt-5.6-terra' -> (('gpt', 'terra'), ((5, 6),))
    'gpt-5-6-terra' -> (('gpt', 'terra'), ((5, 6),))
    'gpt-terra-5-6' -> (('gpt', 'terra'), ((5, 6),))
    """
    if not model_id:
        return ((), ())
    tokens = [t for t in _TOKEN_SPLIT_RE.split(model_id.lower()) if t]

    alpha_tokens: list[str] = []
    version_runs: list[tuple] = []
    cur_run: list[int] = []
    for t in tokens:
        if _NUMERIC_RE.match(t):
            cur_run.append(int(t))
            continue
        if cur_run:
            version_runs.append(tuple(cur_run))
            cur_run = []
        alpha_tokens.append(t)
    if cur_run:
        version_runs.append(tuple(cur_run))

    return (tuple(sorted(alpha_tokens)), tuple(version_runs))


def _iter_combo_aliases(config: dict):
    for c in config.get("combos", []) or []:
        alias = c.get("alias")
        if alias:
            yield alias


def build_fuzzy_index(config: dict) -> dict:
    """Index every known model identifier by canonical key.

    Insertion order mirrors the routing ladder: combos first, then aliases,
    then provider model IDs — so when several identifiers share a canonical
    key, the candidate the ladder would have checked first also comes first.
    """
    index: dict = {}
    for alias in _iter_combo_aliases(config):
        index.setdefault(canonical_key(alias), []).append(
            {"kind": "combo", "name": alias, "provider": None}
        )
    for alias in (config.get("aliases", {}) or {}):
        index.setdefault(canonical_key(alias), []).append(
            {"kind": "alias", "name": alias, "provider": None}
        )
    for prov_id, prov_data in (config.get("providers", {}) or {}).items():
        for m in (prov_data.get("models", []) or []):
            mid = m.get("id")
            if mid:
                index.setdefault(canonical_key(mid), []).append(
                    {"kind": "model", "name": mid, "provider": prov_id}
                )
    return index


def exact_model_known(model: str, config: dict, provider_hint: Optional[str] = None) -> bool:
    """True when ``model`` is exactly known as a combo alias, alias, or a
    provider model ID (optionally scoped to ``provider_hint``)."""
    if not model:
        return False
    if not provider_hint:
        if model in set(_iter_combo_aliases(config)):
            return True
        if model in (config.get("aliases", {}) or {}):
            return True
    providers = config.get("providers", {}) or {}
    if provider_hint:
        if provider_hint not in providers:
            return False
        providers = {provider_hint: providers[provider_hint]}
    for prov_data in providers.values():
        for m in (prov_data.get("models", []) or []):
            if m.get("id") == model:
                return True
    return False


def fuzzy_resolve_model(
    model: str,
    config: dict,
    provider_hint: Optional[str] = None,
) -> Optional[dict]:
    """Resolve an unknown model ID to a registered one.

    Returns the first candidate dict (with a ``candidates`` list attached for
    logging) or None. Exact-known names always return None — a valid name is
    never rewritten. With ``provider_hint``, model candidates are restricted
    to that provider when any match.
    """
    if exact_model_known(model, config, provider_hint):
        return None
    cands = build_fuzzy_index(config).get(canonical_key(model)) or []
    if not cands:
        return None
    if provider_hint:
        scoped = [
            c for c in cands
            if c.get("kind") == "model" and c.get("provider") == provider_hint
        ]
        if scoped:
            cands = scoped
    first = dict(cands[0])
    first["candidates"] = list(cands)
    return first


def maybe_fuzzy_normalize_model(
    model: str,
    config: dict,
    provider_hint: Optional[str] = None,
):
    """Return ``(possibly-corrected model, note)``.

    ``note`` is a human-readable log line when a correction was applied,
    else None. Caller routes by ``note`` — no correction happens when the
    original name is already exactly valid.
    """
    hit = fuzzy_resolve_model(model, config, provider_hint)
    if not hit:
        return model, None
    provider_part = "@%s" % hit["provider"] if hit.get("provider") else ""
    note = (
        "[ModelFuzzy] '%s' -> %s (%s%s, %d candidate(s))"
        % (model, hit["name"], hit["kind"], provider_part, len(hit["candidates"]))
    )
    return hit["name"], note
