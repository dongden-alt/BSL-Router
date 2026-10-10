"""Feature extraction from probe responses."""
import datetime
import re
from dataclasses import dataclass, field


@dataclass
class FingerprintFeatures:
    """Behavioral features extracted from probe responses."""

    # Identity features (from identity_direct + identity_adversarial)
    claimed_model: str = ""
    claimed_org: str = ""
    claimed_cutoff: str = ""
    impostor_hits: list = field(default_factory=list)
    self_hits: list = field(default_factory=list)
    unknown_count: int = 0

    # Style features (from style_sample)
    response_length: int = 0
    sentence_count: int = 0
    avg_sentence_length: float = 0.0
    bullet_style: str = ""           # "-", "*", "1.", "none"
    header_depth: int = 0            # max # count in markdown headers
    code_block_tag: str = ""         # language tag in ``` blocks
    has_analogy: bool = False
    cjk_ratio: float = 0.0           # fraction of CJK characters
    question_count: int = 0          # rhetorical questions

    # Cutoff/capability features (from Tier 2)
    reported_cutoff: str = ""
    cutoff_year: int = 0
    cutoff_month: int = 0
    capability_correct: int = 0      # out of 3
    capability_total: int = 3

    # Tier 2.5 NON-CODING (deep mode): the strongest family discriminator.
    # Coding is the WEAKEST axis — clones match frontier models on code but
    # collapse on writing/EQ/humor/design. Populated by the gate's deep lane.
    # shape: {"judge": {axis:{score,why}}, "probe_count": int,
    #         "slop_score": float, "vocabulary_complexity": float,
    #         "outputs": [{axis,id,content}]}
    noncoding: dict = field(default_factory=dict)

    # V1 KNOWLEDGE-CUTOFF NEEDLES — the strongest sub-VERSION discriminator.
    # A version knows its in-cutoff facts and does NOT know post-cutoff facts.
    # Populated by the gate's V1 lane (needles drawn from candidate standards).
    # shape: {"known_hits": [facts confirmed], "known_misses": [facts it failed],
    #         "post_cutoff_claimed": [future facts it wrongly claimed to know],
    #         "post_cutoff_denied": [future facts correctly denied],
    #         "answers": [{fact, kind, answer}]}
    knowledge_needles: dict = field(default_factory=dict)

    # V5 LOGPROB TAIL — weight-level, search/injection-IMMUNE version splitter.
    # First-token logprob distribution on fixed probes (LT method). Populated by
    # the gate's V5 lane when the endpoint returns logprobs; empty otherwise.
    # shape: {probe_id: {"logprob": float, "top": {token: logprob}}}
    logprob_profile: dict = field(default_factory=dict)

    # V6a BOUNDARY SIGNATURE — stop-sequence/EOS + strict JSON-schema
    # compliance. Populated by the gate's L1 cascade lane (deep mode, always).
    # shape: {"stop_strict": bool, "stop_echo": bool, "finish_reason": str,
    #         "json_strict": bool, "json_mode": "supported"|"unsupported"|"error"}
    boundary_signature: dict = field(default_factory=dict)

    # REFUSAL/ALIGNMENT SIGNATURE — family-level corroborator (never
    # decisive). Populated by the gate's refusal lane (deep mode) from
    # neutral public-topic probes; mirrors the harvest-time refusal capture
    # stored on the standard's RefusalLayer.
    # shape: {"axes": {axis: {"en": {"verdict","template"}, "zh": {...}}},
    #         "delta": {axis: float}, "templates": {axis: str}, "captured": int}
    refusal_signature: dict = field(default_factory=dict)

    # V6b STABILITY PROFILE — temp=0 identical-probe repetition (3 samples).
    # Populated by the gate's L2 lane (only when abstaining after L1).
    # shape: {"probe_id": str, "samples": 3, "distinct_hashes": int,
    #         "hashes": [str]*3, "mode_hash": str}
    stability_profile: dict = field(default_factory=dict)

    # V7 TIMING PROFILE — streamed probe TTFT/chunk-count/chunk-size.
    # Populated by the gate's L3 lane (only when still abstaining after L2).
    # Advisory-only evidence; never vetoes, never rescues.
    # shape: {"ttft_ms": int, "chunk_count": int,
    #         "mean_chunk_chars": float, "total_ms": int}
    timing_profile: dict = field(default_factory=dict)

    # STANDARD v2 PROFILE -- judge-free, fake-resistant evidence (see
    # standard_v2.py): {"tokenizer": {...}, "capability_curve": {...},
    # "knowledge_bank": {...}}. Populated by the gate's v2 lane (deep mode).
    v2_profile: dict = field(default_factory=dict)


# Self-identification phrases to strip for behavior-column classification
SELF_CLAIM_PATTERNS = [
    r"(?i)i am (?:a |an )?[\w\s.\-]+(?:model|assistant|ai|llm)",
    r"(?i)i(?:'m| am) (?:developed|trained|built|created|made) by [\w\s.\-]+",
    r"(?i)my (?:name|model|version) is [\w\s.\-]+",
    r"(?i)(?:model|developer|arch|cutoff)\s*:\s*[\w\s./\-]+",
    r"(?i)as (?:a |an )?[\w\s.\-]+(?:model|assistant|ai)",
    r"(?i)i(?:'m| am) \w+",
]


def strip_self_claims(text: str) -> str:
    """Remove self-identification phrases from text.

    The remaining text carries the model's style fingerprint without
    the influence of what it claims to be. This is the key technique
    from BazaarLink's Layer 1 — adversaries rewrite *what the model
    says it is* far more often than they rewrite *how it says things*.
    """
    cleaned = text
    for pattern in SELF_CLAIM_PATTERNS:
        cleaned = re.sub(pattern, "", cleaned)
    return cleaned.strip()


def extract_features(results: list, family_ref: dict) -> FingerprintFeatures:
    """Extract behavioral features from probe results.

    Args:
        results: list of ProbeResult from probe execution
        family_ref: family reference data (self_keywords, impostor_keywords)

    Returns:
        FingerprintFeatures with all extracted features
    """
    features = FingerprintFeatures()

    for result in results:
        if result.status_code != 200 or not result.content:
            continue

        content = result.content
        lower = content.lower()

        if result.probe_id == "identity_direct":
            # Extract MODEL/DEVELOPER/CUTOFF fields
            for line in content.split("\n"):
                line_stripped = line.strip()
                if line_stripped.upper().startswith("MODEL:"):
                    features.claimed_model = line_stripped[6:].strip()
                elif line_stripped.upper().startswith("DEVELOPER:"):
                    features.claimed_org = line_stripped[10:].strip()
                elif line_stripped.upper().startswith("CUTOFF:"):
                    features.claimed_cutoff = line_stripped[7:].strip()

            # Check impostor/self keywords
            for kw in family_ref.get("impostor_keywords", []):
                if kw.lower() in lower:
                    features.impostor_hits.append(kw)
            for kw in family_ref.get("self_keywords", []):
                if kw.lower() in lower:
                    features.self_hits.append(kw)

        elif result.probe_id == "identity_adversarial":
            features.unknown_count = lower.count("unknown")
            for kw in family_ref.get("impostor_keywords", []):
                if kw.lower() in lower:
                    features.impostor_hits.append(kw)
            for kw in family_ref.get("self_keywords", []):
                if kw.lower() in lower:
                    features.self_hits.append(kw)

        elif result.probe_id == "style_sample":
            features.response_length = len(content)
            sentences = re.split(r'[.!?]+', content)
            sentences = [s.strip() for s in sentences if s.strip()]
            features.sentence_count = len(sentences)
            if sentences:
                features.avg_sentence_length = (
                    sum(len(s) for s in sentences) / len(sentences)
                )

            # Bullet style
            if re.search(r'^\s*-\s', content, re.MULTILINE):
                features.bullet_style = "-"
            elif re.search(r'^\s*\*\s', content, re.MULTILINE):
                features.bullet_style = "*"
            elif re.search(r'^\s*\d+\.\s', content, re.MULTILINE):
                features.bullet_style = "1."
            else:
                features.bullet_style = "none"

            # Header depth
            headers = re.findall(r'^(#{1,6})\s', content, re.MULTILINE)
            features.header_depth = max(len(h) for h in headers) if headers else 0

            # Code block tag
            code_tags = re.findall(r'```(\w+)', content)
            features.code_block_tag = code_tags[0] if code_tags else ""

            # Analogy detection
            analogy_words = [
                "like", "imagine", "think of", "similar to", "as if",
                "好比", "就像", "想象",
            ]
            features.has_analogy = any(w in lower for w in analogy_words)

            # CJK ratio
            cjk_chars = sum(
                1 for c in content
                if '\u4e00' <= c <= '\u9fff'
                or '\u3040' <= c <= '\u309f'
                or '\uac00' <= c <= '\ud7af'
            )
            features.cjk_ratio = cjk_chars / max(len(content), 1)

            # Question count — PROSE ONLY. '?' inside fenced code, inline
            # code spans or URLs (query strings, ternaries, regexes) is not a
            # rhetorical question; counting it floored every response at 1+
            # and made the question-count ratio a free +1 for every family.
            _prose = re.sub(r"```.*?```", " ", content, flags=re.DOTALL)
            _prose = re.sub(r"`[^`]*`", " ", _prose)
            _prose = re.sub(r"https?://\S+", " ", _prose)
            features.question_count = _prose.count("?") + _prose.count("？")

        elif result.probe_id == "cutoff_probe":
            features.reported_cutoff = content.strip()
            # Range-gate: a plausible model knowledge cutoff is 2019..now+1.
            # The old code took the FIRST 4-digit number anywhere in the reply,
            # which froze garbage (6647-2, 2007-0) into standards.
            _now = datetime.datetime.now().year
            _lo, _hi = 2019, _now + 1
            _CUTOFF_CTX = ("cutoff", "cut-off", "training data", "trained",
                           "knowledge", "up to", "as of", "until")
            # Prefer a year that appears NEAR cutoff vocabulary; the reply may
            # mention unrelated years (token counts, dates in an example).
            _cands = []
            for m in re.finditer(r"\d{4}", content):
                y = int(m.group(0))
                if not (_lo <= y <= _hi):
                    continue
                _w = content[max(0, m.start() - 90):m.start()].lower()
                _ctx = any(k in _w for k in _CUTOFF_CTX)
                _cands.append((_ctx, y, m.start()))
            _cands.sort(key=lambda c: (not c[0], c[2]))  # context first, then earliest
            if _cands:
                features.cutoff_year = _cands[0][1]
            month_names = {
                "january": 1, "february": 2, "march": 3, "april": 4,
                "may": 5, "june": 6, "july": 7, "august": 8,
                "september": 9, "october": 10, "november": 11, "december": 12,
            }
            for name, num in month_names.items():
                if name in lower:
                    features.cutoff_month = num
                    break
            # Also try numeric month (YYYY-MM / YYYY/MM).
            month_match = re.search(r"(\d{4})[-/](\d{1,2})", content)
            if month_match:
                _y = int(month_match.group(1))
                _m = int(month_match.group(2))
                if _lo <= _y <= _hi:
                    features.cutoff_year = _y
                    if 1 <= _m <= 12:
                        features.cutoff_month = _m
            # A month with no year is not a usable cutoff: drop it rather than
            # freeze a half-signal (the audit showed rows like 2007-0 / 0-2).
            if features.cutoff_month and not features.cutoff_year:
                features.cutoff_month = 0

        elif result.probe_id == "capability_boundary":
            correct = 0
            if "paris" in lower:
                correct += 1
            if "391" in lower:
                correct += 1
            if "2007" in lower:
                correct += 1
            features.capability_correct = correct

    return features
