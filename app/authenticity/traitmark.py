"""TraitMark — the frozen official-standard schema for one model sub-version.

A TraitMark is harvested ONCE from a trusted source (aibox / mintrouter /
codex / antigravity OAuth) and stored as JSON. At test time the suspect
endpoint's extracted features are compared against the STORED standard —
the trusted source is never re-called.

Layer tags:
  🔒 WRAPPER-INVARIANT — a seller's system prompt cannot shift these without
     changing the weights. These are the real-vs-fake evidence layers.
  ⚠️  WRAPPER-SHIFTED  — surface style; a persona wrapper moves it. Low weight
     in the real/fake decision; used for modification detection instead.
  📏 LIMITS            — delivery parameters (caps/truncation). Modification
     detection only, never real-vs-fake.

Discrimination weight per layer is declared so the classifier can fuse
evidence by how hard it is to fake.
"""
from dataclasses import dataclass, field, asdict
from typing import Optional


# ── Layer discrimination weights (sum used for real/fake fusion) ──────────
# 🔒 layers dominate; ⚠️ style is low because a wrapper can shift it.
LAYER_WEIGHTS = {
    "identity": 0.095,    # 🔒 but spoofable via prompt injection → self-claim zeroing applied first
    "knowledge": 0.266,   # 🔒 strongest version discriminator
    "reasoning": 0.095,   # 🔒
    "style": 0.0475,      # ⚠️ wrapper-shifted, minimal weight
    "lexical": 0.19,      # 🔒 slop/vocab hard to fake
    "noncoding": 0.114,   # 🔒 family-level creative/EQ/humor/design taste
    "structural": 0.1425, # 🔒 tokenizer/tool/code — deepest
    "refusal": 0.05,      # 🔒 family-level alignment/refusal corroborator (never decisive)
}


@dataclass
class MetaLayer:
    model_id: str = ""
    family: str = ""
    sub_model: str = ""
    aliases: list = field(default_factory=list)
    collected_at: str = ""
    collected_from: list = field(default_factory=list)
    standard_version: int = 1
    tier: str = "canonical"          # canonical (unwrapped) | wrapped (real+persona)
    contested: bool = False          # sources disagreed at harvest → treat cautiously
    cutoff_override: str = ""
    harvest_gaps: list = field(default_factory=list)  # layers left at defaults + why
    judged_by: str = ""                # judge model that graded the T2.5 noncoding battery
    source_tag: str = ""               # provenance: aibox | zai-web1 | arena | codex | antigravity ...
    anchor_lane: str = ""              # EXACT router lane the standard was harvested from (e.g. aibox/ZHIPU/GLM-5.3)


@dataclass
class IdentityLayer:
    """🔒 Self-identification. Zeroed of prompt-injected self-claims before use."""
    self_claim_model: str = ""
    self_claim_org: str = ""
    self_keywords: list = field(default_factory=list)
    impostor_keywords: list = field(default_factory=list)
    refusal_identity: str = ""       # canonical "I am X by Y" phrasing


@dataclass
class KnowledgeLayer:
    """🔒 STRONGEST sub-version discriminator. Wrapper-invariant."""
    cutoff_year: int = 0
    cutoff_month: int = 0
    known_facts: list = field(default_factory=list)        # dated facts it SHOULD know
    post_cutoff_unknown: list = field(default_factory=list) # facts it should NOT know
    capability_boundary: dict = field(default_factory=dict) # graded math/logic/fact answers


@dataclass
class ReasoningLayer:
    """🔒 Reasoning + calibration habits. Wrapper-invariant."""
    emits_thinking: bool = False
    thinking_style: str = ""          # e.g. "extended", "none", "inline"
    uncertainty_calibration: dict = field(default_factory=dict)  # hedging vocabulary freq
    numeric_precision: dict = field(default_factory=dict)        # long-division/float behavior


@dataclass
class StyleLayer:
    """⚠️ WRAPPER-SHIFTED. Low weight for real/fake; used for modification diffs."""
    bullet_style: str = ""
    header_depth: int = 0
    avg_sentence_length: float = 0.0
    sentence_length_variance: float = 0.0
    cjk_ratio: float = 0.0
    emoji_usage: float = 0.0
    markdown_density: float = 0.0
    default_response_length: int = 0
    refusal_style: str = ""


@dataclass
class LexicalLayer:
    """🔒 Slop + vocabulary. Hard to fake — a weight-level signature."""
    slop_score: float = 0.0                  # AI-cliché density (lower = Claude-like)
    vocabulary_complexity: float = 0.0       # type-token ratio + rare-word fraction
    signature_phrases: list = field(default_factory=list)  # family-specific tics
    forbidden_words: list = field(default_factory=list)    # words this family ~never uses


@dataclass
class NonCodingLayer:
    """🔒 The T2.5 layer: creative/EQ/humor/design taste. Family discriminator."""
    creative: dict = field(default_factory=dict)   # metaphor_density, imagery, dialogue_naturalness, slop
    eq: dict = field(default_factory=dict)         # hedging_ratio, perspective_taking, emotional_range
    humor: dict = field(default_factory=dict)      # pun_rate, irony_markers, wordplay_success
    design: dict = field(default_factory=dict)     # taste_vocabulary[], critique_specificity


@dataclass
class StructuralLayer:
    """🔒 Deepest: tokenizer / tool-use / code formatting. Wrapper-invariant."""
    tokenizer_signature: dict = field(default_factory=dict)  # special-token + char-count behavior
    tool_use_schema: dict = field(default_factory=dict)      # argument-schema style
    code_formatting: dict = field(default_factory=dict)      # indentation, comment style
    logprob_profile: dict = field(default_factory=dict)      # V5: {probe_id: {logprob, top}} first-token tail


@dataclass
class RefusalLayer:
    """Family-level alignment/refusal fingerprint. Corroborator, never decisive."""
    axes: dict = field(default_factory=dict)
    delta_signature: dict = field(default_factory=dict)
    templates: dict = field(default_factory=dict)
    captured: int = 0


@dataclass
class LimitsLayer:
    """📏 Delivery parameters. Modification detection only — never real/fake."""
    max_output_tokens: int = 0
    context_window: int = 0
    thinking_budget_behavior: str = ""
    temperature_entropy: float = 0.0     # output variance across N samples at temp>0


@dataclass
class TraitMark:
    """Complete frozen standard for one model sub-version."""
    meta: MetaLayer = field(default_factory=MetaLayer)
    identity: IdentityLayer = field(default_factory=IdentityLayer)
    knowledge: KnowledgeLayer = field(default_factory=KnowledgeLayer)
    reasoning: ReasoningLayer = field(default_factory=ReasoningLayer)
    style: StyleLayer = field(default_factory=StyleLayer)
    lexical: LexicalLayer = field(default_factory=LexicalLayer)
    noncoding: NonCodingLayer = field(default_factory=NonCodingLayer)
    structural: StructuralLayer = field(default_factory=StructuralLayer)
    refusal: RefusalLayer = field(default_factory=RefusalLayer)
    limits: LimitsLayer = field(default_factory=LimitsLayer)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "TraitMark":
        """Rebuild a TraitMark from stored JSON. Missing layers → defaults."""
        layers = {
            "meta": MetaLayer,
            "identity": IdentityLayer,
            "knowledge": KnowledgeLayer,
            "reasoning": ReasoningLayer,
            "style": StyleLayer,
            "lexical": LexicalLayer,
            "noncoding": NonCodingLayer,
            "structural": StructuralLayer,
            "refusal": RefusalLayer,
            "limits": LimitsLayer,
        }
        kwargs = {}
        for name, klass in layers.items():
            sub = data.get(name) or {}
            if isinstance(sub, dict):
                # Only pass fields the dataclass actually declares (forward-compat)
                valid = {f: v for f, v in sub.items() if f in klass.__dataclass_fields__}
                kwargs[name] = klass(**valid)
            else:
                kwargs[name] = klass()
        return cls(**kwargs)

    def is_stale(self, max_age_days: int = 30, now_ts: Optional[float] = None) -> bool:
        """True if collected_at is older than max_age_days (or unparseable)."""
        import datetime
        import time
        if not self.meta.collected_at:
            return True
        try:
            collected = datetime.datetime.fromisoformat(
                self.meta.collected_at.replace("Z", "+00:00")
            )
        except (ValueError, AttributeError):
            return True
        now = now_ts if now_ts is not None else time.time()
        age_days = (now - collected.timestamp()) / 86400
        return age_days >= max_age_days
