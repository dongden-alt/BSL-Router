"""Model scope registry — which sub-models are standardized and where their
trusted anchors live.

Only sub-models with a FROZEN standard on disk can be fully verified
(real / downgrade / substitution). Recognized families without a standard
degrade to family-only confirmation. Unknown inputs return UNVERIFIABLE,
never a false 'fake'.

Standard sources:
  gold      — aibox/* direct vendor API (GLM, Kimi, DeepSeek, Qwen)
  reference — OAuth-real wrappers (Claude via mintrouter, GPT via codex,
              Gemini via antigravity). Real weights + seller persona.
"""
from typing import Optional


# ── Trusted anchors per family ────────────────────────────────────────────
# gold = unwrapped vendor API. reference = real-weights wrapper.
TRUSTED_ANCHORS = {
    "glm":      {"gold": ["aibox/zhipu"],      "reference": []},
    "kimi":     {"gold": ["aibox/kimi"],        "reference": []},
    "deepseek": {"gold": ["aibox/ds"],          "reference": []},
    "qwen":     {"gold": ["aibox/qwen"],        "reference": []},
    "claude":   {"gold": [],                    "reference": ["mintrouter"]},
    "gpt":      {"gold": [],                    "reference": ["codex"]},
    "gemini":   {"gold": [],                    "reference": ["antigravity"]},
    # hunyuan / mimo: no trusted in-router anchor yet → deferred (see NOTE)
}


# ── Standardized scope: family → list of sub-model patterns ───────────────
# Patterns are lowercase substrings matched against the suspect model id AND
# the reference hint. More-specific patterns should appear so the narrowest
# variant can be isolated during standard harvest.
SCOPE = {
    "claude":   ["fable", "opus-4", "opus 4", "opus-5", "sonnet-4", "sonnet 4",
                 "sonnet-5", "haiku-4", "haiku 4", "haiku-5"],
    "gemini":   ["gemini-2.5", "gemini 2.5", "2.5-pro", "2.5-flash", "flash-2.5", "pro-2.5", "flash-3", "pro-3", "pro 3",
                 "gemini-3", "gemini 3", "gemini-4", "gemini 4", "gemini-4-argon", "4-argon"],
    "gpt":      ["gpt-5", "gpt5", "gpt-6", "gpt6", "gpt-6.1", "gpt6.1",
                 "-sol", "gpt-sol", "sol-", "terra", "luna", "astra", "o4"],
    "glm":      ["glm-4.6", "glm-4.7", "glm-5", "glm4.6", "glm5"],
    "kimi":     ["kimi-k2", "kimi k2", "kimi-k3", "kimi k3", "k2.5", "k2.6", "k2.7", "k3", "k3-", "k3."],
    "deepseek": ["deepseek-r1", "-r1", "r1-", "/r1", "deepseek-v4", "v4-flash", "v4-pro",
                 "v4.1", "v4-1-flash", "v4.", "v4-"],
    "qwen":     ["qwen3", "qwen-3", "3.5-397", "3.6-plus", "3.6-max",
                 "qwen3-max", "qwen3.5", "qwen3.6", "qwen3.7", "qwen3.8",
                 "3.8-max", "3.8-max-0902"],
    "mimo":     ["mimo-v2.5", "mimo v2.5", "v2.5-pro", "mimo-v2.6", "mimo v2.6",
                 "v2.6-pro", "v2.6-flash"],
    # Deferred (no trusted in-router anchor): "hunyuan"
}

# Families explicitly supported but possibly without per-sub-model standards yet
SUPPORTED_FAMILIES = set(SCOPE.keys())


# ── Researched per-variant traits (2026-09 sources) ────────────────────────
# These notes tell the standard harvester WHICH variants must be treated as
# distinct TraitMarks because their weights/tuning differ. A seller swapping a
# cheaper variant for a dearer one is exactly what we detect.
VARIANT_NOTES = {
    "gpt": {
        # GPT-5.6 generation (tiers Sol/Terra/Luna are DISTINCT models)
        "5.6/sol":   "flagship; ~68% fewer factual errors vs 5.5 Instant",
        "5.6/terra": "mid/balanced, competitive with GPT-5.5",
        "5.6/luna":  "fast/low-cost; free-tier default + Think button",
        "5.6/astra": "separate variant outside the Sol/Terra/Luna ladder",
        # GPT-6 generation (researched 2026-09)
        "6/astra":    "flagship, 2026-09-03; $10/$50 per Mtok; Critical-cyber "
                      "rated; ChatGPT label 'GPT-6 Pro' (Pro/Business/Enterprise)",
        "6/sol":      "mid tier, 2026-09-22; $2/$10; superseded by 6.1 Sol",
        "6/luna":     "budget tier, 2026-09-22; $0.10/$0.50; high-volume; "
                      "below Critical; cheaper than 5.6 Sol",
        "6.1/sol":    "mid tier upgrade, 2026-09-29; $2/$10; ~matches Astra on "
                      "agentic coding/computer-use; 1.05M ctx (922K in), 128K "
                      "out, KNOWLEDGE CUTOFF 2026-04-30 (hard discriminator)",
        "_note": "Each tier is a distinct model, not a quality knob. Split by "
                 "(a) knowledge cutoff (6.1 Sol = 2026-04-30), (b) capability "
                 "ceiling + factual-error rate, (c) context/output caps, "
                 "(d) price-tier behavior. Astra is the Critical-rated flagship; "
                 "selling Astra but serving Sol/Luna is a downgrade.",
    },
    "qwen": {
        "3.8-max":      "2.4T MoE, 95B active, 1M ctx, GA 2026-08-03",
        "3.8-max-0902": "IN-PLACE upgrade of 3.8-max (2026-09-01), post-trained "
                        "on coding/cowork. TerminalBench 11.3->29.0, "
                        "ProgramBench 10.5->28.0, WorkArena Elo 1348->1468. "
                        "Same weights-family, sharper agentic tuning.",
        "3.8-max-prime":"higher-throughput SKU, same base",
        "_note": "3.8-max vs 3.8-max-0902 differ mainly in agentic/coding "
                 "benchmarks; use capability-boundary + code probes to split.",
    },
    "deepseek": {
        "r1":        "reasoning model, distinct from V4 line",
        "v4-flash":  "284B total / 13B active; cheap tier",
        "v4-pro":    "1.6T total / 49B active; flagship; build 0813",
        "v4.1-flash":"552B backbone, 8B read / 16B gen active; 2026-09-10. "
                     "NOTE: V4-Pro traffic now routes to V4.1-Flash at flash "
                     "prices — a 'v4-pro' seller may be serving V4.1-Flash.",
        "_note": "Active-parameter count + reasoning modes + cutoff split "
                 "Flash vs Pro. Watch for Pro->V4.1-Flash silent reroute.",
    },
    "mimo": {
        "v2.5":      "310B, full-modal, 1M ctx, 2026-04-22",
        "v2.5-pro":  "1.02T total / 42B active; flagship reasoning",
        "v2.6-flash":"310B / 15B active, full-modality, 2026-09-21",
        "v2.6-pro":  "1.02T / 42B active, omni-modal, 2026-09-21; beat Claude "
                     "Opus 5 on automation benchmarks",
        "_note": "V2.6 series is current. Differentiate flash vs pro by "
                 "active-param fingerprint + reasoning depth.",
    },
}


def normalize(text: str) -> str:
    return (text or "").lower().replace("_", "-").strip()


def detect_family(model_name: str) -> Optional[str]:
    """Best-effort family detection from a model name / hint substring.

    Returns the family key or None if nothing recognized. This is a
    *prior* only — the probe evidence can override it.
    """
    n = normalize(model_name)
    if not n:
        return None
    # Explicit family names take precedence over shared version substrings.
    for family in SCOPE:
        if family in n:
            return family
    for family, patterns in SCOPE.items():
        if any(p in n for p in patterns):
            return family
    return None


def in_scope(model_name: str, hint: str = "") -> dict:
    """Decide how much the framework can say about this suspect.

    Returns a dict:
      verdict_scope: "full"     — recognized + (hint or name) maps to a sub-model
                     "family"   — family recognized, no specific sub-model standard
                     "unknown"  — nothing recognized → UNVERIFIABLE
      family:        detected family key or None
      focus:         the narrowed sub-model hint (normalized) or None
      reason:        human-readable explanation
    """
    name_n = normalize(model_name)
    hint_n = normalize(hint)

    fam_from_hint = detect_family(hint_n) if hint_n else None
    fam_from_name = detect_family(name_n)

    family = fam_from_hint or fam_from_name
    if family is None:
        return {
            "verdict_scope": "unknown",
            "family": None,
            "focus": None,
            "reason": f"'{model_name}' not in supported mid/top-tier scope; "
                      f"returning UNVERIFIABLE (not 'fake').",
        }

    focus = hint_n if hint_n else None
    scope = "full" if (hint_n or fam_from_name) else "family"
    if fam_from_hint and fam_from_name and fam_from_hint != fam_from_name:
        # hint and name disagree → flag but proceed, bias to hint
        return {
            "verdict_scope": "full",
            "family": fam_from_hint,
            "focus": focus,
            "reason": f"hint '{hint}' implies {fam_from_hint} but name "
                      f"'{model_name}' implies {fam_from_name}; "
                      f"testing against hint first (hint is a hypothesis, "
                      f"not evidence).",
        }

    return {
        "verdict_scope": scope,
        "family": family,
        "focus": focus,
        "reason": (f"narrowed to {family}"
                   + (f" / focus '{focus}'" if focus else "")
                   + ("." if scope == "full"
                      else " (family-only; no specific sub-model standard).")),
    }
