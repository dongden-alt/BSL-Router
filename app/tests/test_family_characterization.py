"""
Characterization test — the family registry must be behavior-identical to
the legacy Engine B cascade for every model in config.yaml.

This is the safety net for the family-contract refactor. It does NOT
assert that the legacy behavior is correct; it asserts the refactor did
not change it. Divergences that are deliberate bug fixes belong in
test_family_divergences.py with an explicit justification.

Run:
  .venv\\Scripts\\python -m pytest app/tests/test_family_characterization.py -q
"""
import os
import re
import sys
import copy

import pytest
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.compat.families import resolve_thinking
from app.compat.families._legacy_reference import legacy_apply_thinking


_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "config.yaml",
)

# Every thinking vocabulary that appears across the config, plus the
# switch/off forms, so each contract branch is exercised.
_EFFORT_VALUES = [
    "auto", "off", "none", "",
    "enable", "adaptive",
    "low", "medium", "high", "max", "xhigh",
    "16k", "32k", "64k", "128k",
]

# A payload shaped like a real egress body, including the sampling params
# that Kimi K3 / Qwen strip, so the strip paths are covered.
def _base_payload():
    return {
        "model": "x",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 8192,
        "temperature": 0.7,
        "top_p": 0.9,
        "presence_penalty": 0.0,
        "frequency_penalty": 0.0,
        "n": 1,
    }


def _load_model_pairs():
    """Yield (provider_name, model_id) for every model in config.yaml."""
    if not os.path.exists(_CONFIG_PATH):
        return []
    with open(_CONFIG_PATH, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}

    pairs = []
    providers = cfg.get("providers", {}) or {}
    for pname, pcfg in providers.items():
        if not isinstance(pcfg, dict):
            continue
        for m in pcfg.get("models", []) or []:
            if isinstance(m, dict) and m.get("id"):
                pairs.append((pname, str(m["id"])))
    return pairs


_MODEL_PAIRS = _load_model_pairs()


def test_config_models_discovered():
    """Guard: if config parsing silently returns nothing, the whole
    characterization suite would vacuously pass.

    config.yaml is gitignored (it holds live credentials), so a fresh clone has
    none. Skip instead of failing there: this guard protects against a config
    SCHEMA change breaking the safety net, which is only meaningful when a
    real, populated config exists.
    """
    if not os.path.exists(_CONFIG_PATH):
        pytest.skip(
            "no config.yaml in this checkout (expected on a fresh clone); "
            "copy config.example.yaml and add providers to exercise this guard"
        )
    # An existence check alone is not enough. Copying config.example.yaml to
    # config.yaml (a normal first setup step, and what a clone smoke-test does)
    # produces a VALID config with a single placeholder provider. That satisfies
    # os.path.exists but not the >50 threshold below, so the guard failed for a
    # reason that has nothing to do with a schema regression. Treat a
    # barely-populated config as "no real config" and skip.
    if len(_MODEL_PAIRS) <= 5:
        pytest.skip(
            f"config.yaml has only {len(_MODEL_PAIRS)} model(s) — looks like an "
            "unpopulated config.example.yaml copy rather than a real config; "
            "add providers to exercise this guard"
        )
    assert len(_MODEL_PAIRS) > 50, (
        f"Expected many models from config.yaml, found {len(_MODEL_PAIRS)}. "
        "Config schema may have changed — the safety net is not covering anything."
    )


@pytest.mark.parametrize("effort", _EFFORT_VALUES)
def test_registry_matches_legacy_for_all_config_models(effort):
    """For every (provider, model) x effort, new == legacy, field for field."""
    mismatches = []

    for provider_name, model_id in _MODEL_PAIRS:
        f_val = f"{provider_name}/{model_id}".lower()

        # Gemini is a DELIBERATE divergence: the legacy cascade emitted
        # Anthropic-shaped keys that Google rejects with a 400. The correct
        # shape is asserted in test_family_divergences.py.
        if "gemini" in f_val:
            continue

        # K2 (non-K3) is a DELIBERATE divergence: the legacy cascade did
        # NOT strip temperature/top_p for K2 models, but Moonshot docs say
        # K2.7-code/K2.6 don't allow temperature modification. The new
        # _sanitize_k2 strips them unconditionally (matching K3/Qwen).
        # See test_family_divergences.py::test_k2_strips_forbidden_sampling_params.
        if "kimi" in f_val and "kimi-k3" not in f_val:
            continue

        # K3 with effort='medium' is a DELIBERATE divergence: legacy
        # accepted 'medium' in its vocab list, but Moonshot docs say K3
        # REJECTS 'medium' with a 400. The new code coerces to 'max'.
        # See test_family_divergences.py::test_k3_coerces_medium_to_max.
        if "kimi-k3" in f_val and effort == "medium":
            continue

        # qwencoder is a DELIBERATE divergence: the legacy `r'qwen'` regex
        # matched the PROVIDER name `qwencoder`, so every model served by
        # that provider got Qwen's sampling-param strip + reasoning_effort
        # injection — even non-Qwen models like claude-opus, gpt-5.6, etc.
        # The new `qwen(?!coder)` pattern correctly excludes the provider.
        # See test_family_divergences.py::test_qwencoder_provider_not_matched_as_qwen.
        if "qwencoder/" in f_val:
            continue

        # Qwen is a DELIBERATE divergence: the legacy cascade injected
        # reasoning_effort (coercing anything unknown to 'max') for EVERY
        # Qwen model. Per official docs, 3.7 and older are boolean-only
        # (no effort enum), and 3.8-max's enum is {low, medium, xhigh} —
        # legacy's 'max'/'high' are 400s there. The new contract is
        # version-aware; see test_qwen_thinking_levels.py.
        if re.search(r"qwen(?!coder)", f_val):
            continue

        legacy_payload = legacy_apply_thinking(_base_payload(), f_val, effort)
        new_payload, _prov = resolve_thinking(_base_payload(), f_val, effort)

        if legacy_payload != new_payload:
            mismatches.append(
                f"\n  {f_val} (effort={effort!r})"
                f"\n    legacy: {legacy_payload}"
                f"\n    new:    {new_payload}"
            )

    assert not mismatches, (
        f"{len(mismatches)} divergence(s) from legacy cascade:"
        + "".join(mismatches[:15])
    )


@pytest.mark.parametrize("effort", ["max", "high", "32k", "enable", "adaptive", "auto"])
@pytest.mark.parametrize(
    "f_val",
    [
        # Explicit spot-checks for each contract, including the ordering
        # traps that the legacy elif chain resolved only by line position.
        "vsllm-gpt/gpt-5.5",
        "vsllm-gpt/gpt-5.6-terra",
        "openrouter/anthropic-claude",
        # gemini-* deliberately omitted — see test_family_divergences.py
        # kimi-k2.* deliberately omitted — see test_family_divergences.py
        "pix4k/claude-opus-4.8-thinking",
        "vsllm-a/claude-opus-4-6-antigravity",
        "pix4k/claude-sonnet-5-thinking",
        "anthropic/claude-3-5-sonnet-20241022",
        "pix4k/fable-5",
        "pix4k/mythos-5",
        "xai/grok-4.5",
        "xai/grok-4-non-reasoning",
        "iamhc/DeepSeek-V4-Pro",
        "moonshot/kimi-k3",
        # hcnsec-vip/Qwen3.7-Max deliberately omitted — the two-axis Qwen
        # revision diverges from legacy; see test_qwen_thinking_levels.py.
        "iamhc/glm-5.2",
        "hcnsec-vip/glm-5.1",
        "vsllm-gpt/glm-5.2-anthropic",
        "iamhc/Minimax-M3",
    ],
)
def test_registry_matches_legacy_spot_checks(f_val, effort):
    legacy_payload = legacy_apply_thinking(_base_payload(), f_val, effort)
    new_payload, _prov = resolve_thinking(_base_payload(), f_val, effort)
    assert new_payload == legacy_payload, (
        f"Divergence for {f_val} effort={effort!r}\n"
        f"  legacy: {legacy_payload}\n"
        f"  new:    {new_payload}"
    )


def test_provenance_is_recorded_for_every_write():
    """A write with no attribution defeats the purpose of the refactor."""
    payload, prov = resolve_thinking(_base_payload(), "iamhc/glm-5.2", "max")
    assert prov.records, "GLM-5.2 wrote thinking fields but recorded no provenance"
    rec = prov.records[0]
    assert rec.contract_id == "glm"
    assert rec.source == "families/glm.py"
    assert rec.rule == "graded_effort"


def test_provenance_names_the_file_to_edit():
    """The whole diagnosis-speed premise: the log names the source file."""
    _payload, prov = resolve_thinking(_base_payload(), "moonshot/kimi-k3", "max")
    sources = {r.source for r in prov.records}
    assert sources == {"families/kimi.py"}


def test_no_contract_writes_outside_owned_keys():
    """The resolver must not write reasoning fields it does not own.

    max_tokens is the one deliberate exception: the Claude budget paths
    raise it to leave room for the reasoning budget.
    """
    from app.compat.families._base import THINKING_PAYLOAD_KEYS

    allowed = set(THINKING_PAYLOAD_KEYS) | {"max_tokens", "includeThoughts"}
    base = _base_payload()

    for provider_name, model_id in _MODEL_PAIRS:
        f_val = f"{provider_name}/{model_id}".lower()
        for effort in ("max", "enable", "32k"):
            before = _base_payload()
            after, _prov = resolve_thinking(_base_payload(), f_val, effort)
            changed = {
                k for k in set(before) | set(after)
                if before.get(k) != after.get(k)
            }
            # Sampling-param strips are removals, not writes — allow them.
            removed = {k for k in changed if k not in after}
            written = changed - removed
            unexpected = written - allowed
            assert not unexpected, (
                f"{f_val} (effort={effort}) wrote unowned keys: {unexpected}"
            )
