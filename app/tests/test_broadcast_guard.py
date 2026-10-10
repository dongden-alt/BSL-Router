# test_broadcast_guard.py — Step-2 bare-model broadcast guard (2026-10-06).
#
# The router used to pick the FIRST provider serving a bare model id
# (find_provider_for_model) and silently retry-broadcast on failure across every
# provider that serves that id — the kiro/xpiki 400/402/502 quota-burn flood.
# The fix: a bare id served by >1 provider is now AMBIGUOUS and rejected (404,
# code ambiguous_model) instead of broadcasting. Exact 'provider/model' lanes,
# namespaced ids, aliases and fuzzy hits are untouched (they set provider_name
# before Step 2, so the guard never runs for them).
#
# These tests hit the pure resolver helpers directly — no router boot, no config
# write, no upstream call. Synthetic config only.
import json

import pytest

from app.routing.combo_resolver import (
    find_provider_for_model,
    find_providers_for_model,
    build_not_found_error,
    build_ambiguous_model_error,
)


def _config(providers: dict) -> dict:
    """Synthetic config: {prov_id: [model_id, ...]} -> providers shape."""
    return {
        "providers": {
            pid: {"models": [{"id": mid} for mid in mids]}
            for pid, mids in providers.items()
        },
        "aliases": {},
        "combos": [],
    }


# --- find_providers_for_model: enumerate EVERY serving provider ------------

def test_multi_provider_bare_id_enumerates_all():
    cfg = _config({
        "kiro": ["claude-opus-4-8"],
        "xpiki": ["claude-opus-4-8"],
        "mintrouter": ["claude-opus-4-8", "claude-fable-5"],
    })
    assert sorted(find_providers_for_model("claude-opus-4-8", cfg)) == \
        ["kiro", "mintrouter", "xpiki"]


def test_single_provider_bare_id_returns_one():
    cfg = _config({"mintrouter": ["claude-fable-5"], "kiro": ["claude-opus-4-8"]})
    assert find_providers_for_model("claude-fable-5", cfg) == ["mintrouter"]


def test_unknown_model_returns_empty():
    cfg = _config({"mintrouter": ["claude-fable-5"]})
    assert find_providers_for_model("does-not-exist", cfg) == []


def test_legacy_finder_still_returns_first_match():
    """find_provider_for_model is unchanged (still used elsewhere); the new
    guard does not alter its contract."""
    cfg = _config({"kiro": ["m"], "xpiki": ["m"]})
    assert find_provider_for_model("m", cfg) in ("kiro", "xpiki")


# --- the guard's decision boundary ----------------------------------------

@pytest.mark.parametrize("providers,ambiguous", [
    ({"kiro": ["m"], "xpiki": ["m"]}, True),          # >1 -> broadcast -> reject
    ({"mintrouter": ["m"]}, False),                    # 1 -> safe single match
    ({"a": ["m"], "b": ["m"], "c": ["m"]}, True),      # 3 -> reject
])
def test_ambiguity_decision(providers, ambiguous):
    cfg = _config(providers)
    matches = find_providers_for_model("m", cfg)
    assert (len(matches) > 1) is ambiguous


# --- error builders --------------------------------------------------------

def test_ambiguous_error_shape():
    resp = build_ambiguous_model_error("claude-opus-4-8", ["kiro", "xpiki", "mintrouter"])
    assert resp.status_code == 404
    body = json.loads(resp.body)
    err = body["error"]
    assert err["code"] == "ambiguous_model"
    assert err["type"] == "invalid_request_error"
    assert err["param"] == "model"
    # every candidate lane is listed so the caller can qualify explicitly
    for lane in ("kiro/claude-opus-4-8", "xpiki/claude-opus-4-8", "mintrouter/claude-opus-4-8"):
        assert lane in err["message"]
    assert "Broadcast is disabled" in err["message"]


def test_not_found_error_shape_unchanged():
    cfg = _config({"mintrouter": ["claude-fable-5"]})
    resp = build_not_found_error("ghost-model", cfg)
    assert resp.status_code == 404
    body = json.loads(resp.body)
    assert "ghost-model" in body["error"]
    assert "claude-fable-5" in body["error"]  # known-models hint still present
