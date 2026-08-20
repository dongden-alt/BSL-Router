"""
Official vendor thinking-parameter parity locks (GLM + Grok).

Authoritative mappings supplied 2026-08-20 from vendor docs. These tests
pin the Coding Plan / request-vocabulary behavior so a future edit cannot
silently re-widen a version-specific coerce path.

Run:
  python -m pytest app/tests/test_official_thinking_parity.py -v
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.compat.families import resolve_thinking

_GLM53_ACCEPTED = frozenset({"low", "high", "max"})


def _payload(**extra):
    body = {"model": "x", "messages": [], "max_tokens": 8192}
    body.update(extra)
    return body


# ─────────────────────────────────────────────────────────────────────────
# GLM-5.3 — only low/high/max accepted upstream
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("f_val", [
    "glm-5.3",
    "iamhc/glm-5.3",
    "hcnsec-vip/glm-5.3-anthropic",
])
@pytest.mark.parametrize("effort,expected", [
    ("none", "low"),
    ("minimal", "low"),
    ("low", "low"),
    ("medium", "high"),
    ("high", "high"),
    ("xhigh", "max"),
    ("max", "max"),
    ("garbage", "high"),
    ("banana", "high"),
    ("32k", "high"),
])
def test_glm53_effort_mapping(f_val, effort, expected):
    out, _ = resolve_thinking(_payload(), f_val, effort)
    assert out.get("thinking") == {"type": "enabled"}
    assert out.get("reasoning_effort") == expected
    assert out["reasoning_effort"] in _GLM53_ACCEPTED


@pytest.mark.parametrize("f_val", [
    "glm-5.3",
    "iamhc/glm-5.3",
    "glm-5.3-anthropic",
])
@pytest.mark.parametrize("effort", [
    "none", "minimal", "low", "medium", "high", "xhigh", "max", "nope",
])
def test_glm53_reasoning_effort_always_in_accepted_vocab(f_val, effort):
    out, _ = resolve_thinking(_payload(), f_val, effort)
    assert out["reasoning_effort"] in _GLM53_ACCEPTED


# ─────────────────────────────────────────────────────────────────────────
# GLM-5.2 — none/minimal disable thinking; low/medium -> high; xhigh -> max
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("effort", ["none", "minimal"])
def test_glm52_none_minimal_disables_thinking(effort):
    out, _ = resolve_thinking(_payload(), "iamhc/glm-5.2", effort)
    assert out.get("thinking") == {"type": "disabled"}
    assert "reasoning_effort" not in out


@pytest.mark.parametrize("effort,expected", [
    ("low", "high"),
    ("medium", "high"),
    ("high", "high"),
    ("xhigh", "max"),
    ("max", "max"),
    ("garbage", "high"),
])
def test_glm52_on_thinking_effort_mapping(effort, expected):
    out, _ = resolve_thinking(_payload(), "f_val/glm-5.2", effort)
    assert out.get("thinking") == {"type": "enabled"}
    assert out.get("reasoning_effort") == expected


# ─────────────────────────────────────────────────────────────────────────
# Grok-4.6 — full vocab; unknown -> high; strip incompatible sampling
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh"])
def test_grok46_valid_effort_unchanged(effort):
    out, _ = resolve_thinking(_payload(), "xai/grok-4.6", effort)
    assert out.get("reasoning_effort") == effort


@pytest.mark.parametrize("effort", ["banana", "none", "off", "minimal", "32k"])
def test_grok46_unknown_effort_defaults_to_high(effort):
    out, _ = resolve_thinking(_payload(), "xai/grok-4.6", effort)
    assert out.get("reasoning_effort") == "high"


def test_grok46_strips_presence_frequency_stop():
    src = _payload(
        presence_penalty=0.5,
        frequency_penalty=0.25,
        stop=["\n"],
        temperature=0.7,
    )
    out, prov = resolve_thinking(src, "xai/grok-4.6", "high")
    assert "presence_penalty" not in out
    assert "frequency_penalty" not in out
    assert "stop" not in out
    # Thinking fields intact.
    assert out.get("reasoning_effort") == "high"
    # Unrelated sampling left alone.
    assert out.get("temperature") == 0.7
    # Provenance recorded for every stripped key.
    stripped = []
    for rec in prov.records:
        stripped.extend(rec.fields)
    for key in ("-presence_penalty", "-frequency_penalty", "-stop"):
        assert key in stripped, f"missing provenance for {key}: {prov.summary()}"


# ─────────────────────────────────────────────────────────────────────────
# Grok-4.5 — xhigh -> high; garbage -> high
# ─────────────────────────────────────────────────────────────────────────

def test_grok45_xhigh_coerces_to_high():
    out, _ = resolve_thinking(_payload(), "xai/grok-4.5", "xhigh")
    assert out.get("reasoning_effort") == "high"


def test_grok45_garbage_defaults_to_high():
    out, _ = resolve_thinking(_payload(), "xai/grok-4.5", "banana")
    assert out.get("reasoning_effort") == "high"


def test_grok45_strips_incompatible_params():
    out, _ = resolve_thinking(
        _payload(presence_penalty=1.0, frequency_penalty=1.0, stop="END"),
        "xai/grok-4.5",
        "medium",
    )
    assert "presence_penalty" not in out
    assert "frequency_penalty" not in out
    assert "stop" not in out
    assert out.get("reasoning_effort") == "medium"


# ─────────────────────────────────────────────────────────────────────────
# Grok-4.20-multi-agent — version parse (4,20) >= (4,6) keeps xhigh
# ─────────────────────────────────────────────────────────────────────────

def test_grok420_multi_agent_xhigh_retained():
    out, _ = resolve_thinking(_payload(), "xai/grok-4.20-multi-agent", "xhigh")
    assert out.get("reasoning_effort") == "xhigh"


# ─────────────────────────────────────────────────────────────────────────
# Hunyuan Hy3 — official sampling defaults (fill-when-absent)
# ─────────────────────────────────────────────────────────────────────────

def test_hunyuan_fills_official_sampling_when_absent():
    out, prov = resolve_thinking(_payload(), "tencent/hunyuan-hy3", "high")
    assert out.get("temperature") == 0.9
    assert out.get("top_p") == 1.0
    assert any(r.rule == "official_sampling_defaults" for r in prov.records), (
        f"missing official_sampling_defaults provenance: {prov.summary()}"
    )


def test_hunyuan_never_overrides_client_temperature():
    out, _ = resolve_thinking(
        _payload(temperature=0.3),
        "tencent/hunyuan-hy3",
        "high",
    )
    assert out.get("temperature") == 0.3
    # top_p still filled when absent
    assert out.get("top_p") == 1.0


# ─────────────────────────────────────────────────────────────────────────
# Muse Spark 1.1 — thinking + output_config.effort
# ─────────────────────────────────────────────────────────────────────────

_MUSE11 = "meta/muse-spark-1.1"


@pytest.mark.parametrize("effort", ["auto", "enable", ""])
def test_muse11_auto_enable_adaptive_only(effort):
    out, _ = resolve_thinking(_payload(), _MUSE11, effort)
    assert out.get("thinking") == {"type": "adaptive"}
    assert "output_config" not in out
    assert "reasoning_effort" not in out


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh"])
def test_muse11_explicit_depth_sets_output_config(effort):
    out, _ = resolve_thinking(_payload(), _MUSE11, effort)
    assert out.get("thinking") == {"type": "adaptive"}
    assert out.get("output_config") == {"effort": effort}
    assert "reasoning_effort" not in out


@pytest.mark.parametrize("effort", ["off", "none", "minimal", "disable"])
def test_muse11_off_coerced_to_low(effort):
    out, prov = resolve_thinking(_payload(), _MUSE11, effort)
    assert out.get("thinking") == {"type": "adaptive"}
    assert out.get("output_config") == {"effort": "low"}
    assert any(r.rule == "off_coerced_to_low" for r in prov.records), prov.summary()


@pytest.mark.parametrize("effort", ["max", "ultra", "garbage", "banana"])
def test_muse11_unknown_coerces_to_xhigh(effort):
    out, _ = resolve_thinking(_payload(), _MUSE11, effort)
    assert out.get("thinking") == {"type": "adaptive"}
    assert out.get("output_config") == {"effort": "xhigh"}


def test_muse11_budget_tokens_compat_with_depth():
    src = _payload(thinking={"type": "enabled", "budget_tokens": 2048})
    out, _ = resolve_thinking(src, _MUSE11, "high")
    assert out.get("thinking") == {"type": "enabled", "budget_tokens": 2048}
    assert out.get("output_config") == {"effort": "high"}


def test_muse11_display_passthrough():
    src = _payload(display="summarized")
    out, _ = resolve_thinking(src, _MUSE11, "high")
    assert out.get("display") == "summarized"
    assert out.get("thinking") == {"type": "adaptive"}
    assert out.get("output_config") == {"effort": "high"}


# ─────────────────────────────────────────────────────────────────────────
# Muse Spark 1.2 — reasoning_effort OpenAI-compatible wire
# ─────────────────────────────────────────────────────────────────────────

_MUSE12 = "meta/muse-spark-1.2"


@pytest.mark.parametrize("effort", ["auto", ""])
def test_muse12_auto_omits_reasoning_effort(effort):
    out, _ = resolve_thinking(_payload(), _MUSE12, effort)
    assert "reasoning_effort" not in out


@pytest.mark.parametrize("effort", [
    "minimal", "low", "medium", "high", "xhigh", "ultra",
])
def test_muse12_vocab_passthrough(effort):
    out, _ = resolve_thinking(_payload(), _MUSE12, effort)
    assert out.get("reasoning_effort") == effort


@pytest.mark.parametrize("effort", ["none", "off", "disable"])
def test_muse12_off_coerced_to_minimal(effort):
    out, prov = resolve_thinking(_payload(), _MUSE12, effort)
    assert out.get("reasoning_effort") == "minimal"
    assert any(r.rule == "off_coerced_to_minimal" for r in prov.records), prov.summary()


def test_muse12_max_to_xhigh():
    out, _ = resolve_thinking(_payload(), _MUSE12, "max")
    assert out.get("reasoning_effort") == "xhigh"


def test_muse12_garbage_to_xhigh():
    out, _ = resolve_thinking(_payload(), _MUSE12, "banana")
    assert out.get("reasoning_effort") == "xhigh"


# ─────────────────────────────────────────────────────────────────────────
# Unversioned muse-spark → 1.2 wire (latest default)
# ─────────────────────────────────────────────────────────────────────────

def test_muse_unversioned_behaves_as_12_none_to_minimal():
    out, _ = resolve_thinking(_payload(), "meta/muse-spark", "none")
    assert out.get("reasoning_effort") == "minimal"


def test_muse_unversioned_behaves_as_12_auto_omitted():
    out, _ = resolve_thinking(_payload(), "meta/muse-spark", "auto")
    assert "reasoning_effort" not in out


# ─────────────────────────────────────────────────────────────────────────
# Muse Spark numeric version routing (<1.1 → 1.1 wire; >1.2 → 1.2 wire)
# ─────────────────────────────────────────────────────────────────────────

def test_muse_spark_10_behaves_as_11_high_effort():
    """muse-spark-1.0 is pre-1.1 → 1.1 wire: high → thinking adaptive + effort."""
    out, _ = resolve_thinking(_payload(), "meta/muse-spark-1.0", "high")
    assert out.get("thinking") == {"type": "adaptive"}
    assert out.get("output_config") == {"effort": "high"}
    assert "reasoning_effort" not in out


def test_muse_spark_13_behaves_as_12_none_to_minimal():
    """muse-spark-1.3 is post-1.2 → 1.2 wire until contract update."""
    out, _ = resolve_thinking(_payload(), "meta/muse-spark-1.3", "none")
    assert out.get("reasoning_effort") == "minimal"
    assert "output_config" not in out


def test_muse_spark_13_behaves_as_12_auto_omitted():
    out, _ = resolve_thinking(_payload(), "meta/muse-spark-1.3", "auto")
    assert "reasoning_effort" not in out


def test_muse_spark_20_behaves_as_12_none_to_minimal():
    """muse-spark-2.0 is post-1.2 → 1.2 wire until contract update."""
    out, _ = resolve_thinking(_payload(), "meta/muse-spark-2.0", "none")
    assert out.get("reasoning_effort") == "minimal"


def test_muse_spark_20_behaves_as_12_auto_omitted():
    out, _ = resolve_thinking(_payload(), "meta/muse-spark-2.0", "auto")
    assert "reasoning_effort" not in out
