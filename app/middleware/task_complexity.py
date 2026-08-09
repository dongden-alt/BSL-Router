"""
Middleware.task_complexity — Request Complexity Estimator

Estimates request complexity (trivial/standard/deep) for routing bucket
classification. Used by bsl_chat_router and bsl_agentic_max_router to pick
the appropriate model tier. Pure, stateless, fail-open.

NOTE: The token-scaling apply_task_complexity_routing function was removed
2026-08-07 — it was dead code (never called). The routing-only complexity
estimation below is still active.
"""

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from app.models import ChatCompletionRequest, Message
from app.middleware.request_intent import extract_current_intent


# ─── Complexity levels ──────────────────────────────────────────────────────

COMPLEXITY_TRIVIAL = "trivial"
COMPLEXITY_STANDARD = "standard"
COMPLEXITY_DEEP = "deep"

COMPLEXITY_LEVELS = [COMPLEXITY_TRIVIAL, COMPLEXITY_STANDARD, COMPLEXITY_DEEP]


# ─── Signal patterns ────────────────────────────────────────────────────────

# Code markers: fenced code blocks, file paths, tracebacks, line refs
_CODE_BLOCK_RE = re.compile(r"```[\s\S]*?```")
_FILE_PATH_RE = re.compile(r"(?:^|[\s(])(?:/[a-zA-Z0-9_\-./]+\.[a-zA-Z]{1,8}|[A-Z]:\\[a-zA-Z0-9_\-./\\]+\.[a-zA-Z]{1,8}|(?:src|lib|app|test|pkg|cmd|internal)/[a-zA-Z0-9_\-./]+\.[a-zA-Z]{1,8})")
_TRACEBACK_RE = re.compile(r"Traceback \(most recent call last\)|Error:|Exception:|at [a-zA-Z_][a-zA-Z0-9_]*\(", re.IGNORECASE)
_LINE_REF_RE = re.compile(r"(?:line|L)\s*\d+|:\d+(?::\d+)?(?:\s|$)")
_FUNC_CLASS_RE = re.compile(r"(?:def|class|function)\s+[a-zA-Z_][a-zA-Z0-9_]*")

# Multi-step verbs: debug, refactor, audit, test, implement, etc.
_MULTI_STEP_VERBS_RE = re.compile(
    r"\b(debug|refactor|audit|test|implement|integrate|migrate|architect"
    r"|architecture|end-to-end|regression|review|optimize|benchmark"
    r"|troubleshoot|diagnose|fix\s+all|fix\s+every|rewrite|overhaul"
    r"|restructure|reorganize|port|upgrade|downgrade|deploy|rollback)\b",
    re.IGNORECASE,
)

# ── General-purpose cognitive depth signals (v2: non-coding complexity) ──
# These detect intellectual complexity in general chatbot queries that have
# nothing to do with code: essays, analysis, philosophy, research, comparison.

# Analytical depth verbs: analyze, compare, evaluate, synthesize, argue, prove...
_ANALYTICAL_VERBS_RE = re.compile(
    r"\b(analyz|analys|compar|evaluat|synthes|argu|prov|justif|critiqu"
    r"|assess|exam|investigat|explor|deconstruct|interp|contextuali"
    r"|breakdown|break\s+down|weigh|reason|deduc|infer|deriv"
    # Vietnamese analytical verbs — both diacritic and non-diacritic forms
    r"|ph[âa]n\s*t[ií]ch|so\s*s[aá]nh|đ[aá]nh\s*gi[aáà]"
    r"|lu[aậ]n|ch[ứu]ng\s*minh|gi[aả]i\s*th[ií]ch"
    r"|nghi[eê]n\s*c[ứu]u|kh[aả]o\s*s[aá]t|x[eê]m\s*x[eé]t"
    r"|b[àa]n|b[iì]nh\s*lu[aậ]n)\w*",
    re.IGNORECASE,
)

# Creative / long-form requests: write, essay, article, story, with length hints
_CREATIVE_LONGFORM_RE = re.compile(
    r"\b(write|draft|compos|creat|generat|produc|outlin|story|essay|article"
    r"|blog|post|report|letter|email|speech|script|novel|poem"
    # Length indicators
    r"|\d+\s*(?:word|paragraph|page|chapter|t[ừu]|đo[ạa]n|trang|ch[uương])"
    r"|long|detailed|comprehensive|thorough|in-depth|deep\s+dive"
    r"|long\s+form|long-form|full\s+length"
    # Vietnamese — diacritic + non-diacritic
    r"|vi[ếe]t|so[ạa]n|t[ạa]o|vi[ếe]t\s*b[àa]i|b[àa]i\s*vi[ếe]t"
    r"|b[aá]o\s*c[aá]o|ti[ểu]u\s*lu[aậ]n|lu[aậ]n\s*v[aă]n"
    r"|c[âa]u\s*chuy[ệe]n|b[àa]i\s*th[uơ]|chi\s*ti[ếe]t|đ[âa]y\s*đ[ủu]|to[àa]n\s*di[ệe]n)\w*",
    re.IGNORECASE,
)

# Research / evidence markers: cite, sources, evidence, according to, data-driven
_RESEARCH_MARKERS_RE = re.compile(
    r"\b(cite|citation|sources?|evidence|according\s+to|data-driven|data\s+driven"
    r"|peer-review|studies?|statistics|reference|bibliograph|literature"
    r"|fact-check|verify|verification|support(?:s|ed)?\s+by"
    r"|research(?:ed)?|investigated?|documented"
    # Vietnamese — diacritic + non-diacritic
    r"|tr[ií]ch\s*d[ẫa]n|ngu[ồo]n|b[ằa]ng\s*ch[ứu]ng|tham\s*kh[aả]o"
    r"|t[àa]i\s*li[ệe]u|ki[ểe]m\s*ch[ứu]ng"
    r"|nghi[eê]n\s*c[ứu]u|d[ữu]\s*li[ệe]u|th[ốo]ng\s*k[êe])\w*",
    re.IGNORECASE,
)

# Multi-perspective / dialectical signals: pros/cons, advantages, from X perspective
_MULTI_PERSPECTIVE_RE = re.compile(
    r"\b(pros?\s*(?:and|&|\s*\/\s*)\s*cons?|advantages?\s*(?:and|&)\s*disadvantages?"
    r"|strengths?\s*(?:and|&)\s*weaknesses?|benefits?\s*(?:and|&)\s*(?:risks?|drawbacks?)"
    r"|from\s+\w+\s+perspective|point\s+of\s+view|standpoint"
    r"|both\s+sides|multiple\s+(?:angles|perspectives|viewpoints)"
    r"|trade-?offs?|opposing|counterargument|rebuttal|devil'?s\s+advocate"
    r"|compare\s+and\s+contrast|versus|vs\.?"
    # Vietnamese — diacritic + non-diacritic
    r"|[ưuu]\s*v[àa]\s*nh[uươ]ợc|[ưuu]\s*nh[uươ]ợc\s*đi[ểe]m"
    r"|m[ặa]t\s*tr[aá]i|m[ặa]t\s*l[ợo]i|hai\s*m[ặa]t"
    r"|so\s*s[aá]nh|đ[ốo]i\s*chi[ếe]u|t[ừu]\s*g[oó]c\s*nh[iì]n)\w*",
    re.IGNORECASE,
)

# Structural complexity indicators: step-by-step, comprehensive, elaborate, thorough
_STRUCTURAL_DEPTH_RE = re.compile(
    r"\b(step\s*[\-by]+\s*step|comprehensive|elaborate|thorough|exhaustive"
    r"|in\s+detail|detailed|nuanced|sophisticated|rigorous|systematic"
    r"|framework|methodology|taxonomy|classification|categor"
    r"|underlying|fundamental|root\s+cause|first\s+principles?|axiomat"
    r"|deep(?:ly)?|profound|in-depth|insightful"
    # Academic / theoretical depth
    r"|theoretical|phd|paper|theorem|hypothesis|proof|lemma"
    r"|postulate|corollary|axiom|derivation|formalism"
    r"|hidden\s+variable|interpretation|measurement\s+problem"
    # Vietnamese — diacritic + non-diacritic
    r"|t[ừu]ng\s*b[ướu]c|to[àa]n\s*di[ệe]n|chi\s*ti[ếe]t|s[uâ]u\s*s[ắa]c"
    r"|h[ệe]\s*th[ốo]ng|c[uơ]\s*b[ảa]n|(?:g[ốo]c\s*r[ễe]|goc\s*re)|nguy[eê]n\s*b[ảa]n|n[ềe]n\s*t[ảa]ng)\w*",
    re.IGNORECASE,
)

# Greeting / tiny single-turn patterns
_TRIVIAL_RE = re.compile(
    r"^(hi|hello|hey|yo|sup|thanks|thank you|ok|okay|yes|no|bye|goodbye"
    r"|got it|sure|cool|nice|great|agreed|done|lol|brb|ttyl|hmm|hm|idk"
    r"|chào|xin\s+chào|cảm\s+ơn|ok|okie|ừ|đúng|sai|được|không)\s*[!.?]*$",
    re.IGNORECASE,
)

# ── D1-D4 funnel detectors ────────────────────────────────────────────────
# D1 — explicit output magnitude (scope)
_WORD_COUNT_RE = re.compile(
    r"(\d+)[\s-]*(?:words?|t[ừu]|paragraphs?|đo[ạa]n|pages?|trang|chapters?|ch[uương]|requirements?|sections?|deliverables?|features?)",
    re.IGNORECASE,
)
_LONGFORM_NOUN_RE = re.compile(
    r"\b(essay|report|review|thesis|whitepaper|dissertation|ti[ểu]u\s+lu[aậ]n|b[aá]o\s*c[aá]o|lu[aậ]n\s+v[aă]n|lu[aậ]n\s+[aá]n)\b",
    re.IGNORECASE,
)
# D3 — structural density (connectors + delimiter chains)
_CONNECTOR_RE = re.compile(
    r"\b(and|or|but|then|v[àa]|ho[ặa]c|nh[uư]ng|r[ồo]i|khi)\b",
    re.IGNORECASE,
)
# D4 — multi-step engineering workflow verbs (distinct co-occurrence)
_WORKFLOW_DEPTH_RE = re.compile(
    r"\b(debug|refactor|audit|migrate|integrate|implement|optimize|restructure|overhaul|troubleshoot)\b",
    re.IGNORECASE,
)
# D8 — low-reasoning head verbs (cap at standard unless compound)
_LOW_REASONING_RE = re.compile(
    r"^\s*(review|format|convert|translate|lint|rename|summarize|proofread)\b",
    re.IGNORECASE,
)
# Continuation detection
_CONTINUATION_RE = re.compile(
    r"^\s*(make it|now|also|and then|continue|more|longer|shorter|expand|shrink|again|ti[ếe]p|n[ữu]a|d[àa]i|ng[ắa]n|th[êe]m)\b",
    re.IGNORECASE,
)

# ── D1-D4 helper functions ────────────────────────────────────────────────

def _detect_scope(text: str) -> tuple[bool, bool]:
    """D1. Returns (scope_fired, scope_saturated).
    scope_fired: explicit magnitude requested (>=400 words OR a longform noun
    OR a conjunction-list of >=3 deliverables).
    scope_saturated: magnitude alone decisive (>=800 words OR >=5 sections)."""
    if not text:
        return False, False
    for m in _WORD_COUNT_RE.finditer(text):
        try:
            n = int(m.group(1))
        except ValueError:
            continue
        unit = m.group(0).lower()
        if "word" in unit or "từ" in unit:
            if n >= 800:
                return True, True
            if n >= 400:
                return True, False
        else:  # paragraphs/pages/chapters count as sections
            if n >= 5:
                return True, True
            if n >= 3:
                return True, False
    if _LONGFORM_NOUN_RE.search(text):
        return True, False
    return False, False


def _detect_density(text: str) -> bool:
    """D3. True when structurally complex: >=3 connectors OR a delimiter chain
    of >=3 items, AND intent is substantive (>= ~30 tokens ~ 120 chars)."""
    if not text or len(text) < 120:
        return False
    connectors = len(_CONNECTOR_RE.findall(text))
    # delimiter chain: >=3 comma-separated items each >3 words
    items = [seg.strip() for seg in text.split(",") if len(seg.split()) > 3]
    delimiter_chain = len(items) >= 3
    return connectors >= 3 or delimiter_chain


def _detect_workflow_depth(text: str) -> tuple[bool, bool]:
    """D4. Returns (fired, saturated). fired: >=2 DISTINCT multi-step
    engineering verbs; saturated: >=3 distinct."""
    if not text:
        return False, False
    distinct = {m.group(0).lower() for m in _WORKFLOW_DEPTH_RE.finditer(text)}
    return len(distinct) >= 2, len(distinct) >= 3


def _detect_multidomain(category_scores: Optional[Dict[str, int]]) -> bool:
    """D2. >=2 categories each clear CATEGORY_SCORE_THRESHOLD (2)."""
    if not category_scores:
        return False
    return sum(1 for v in category_scores.values() if v >= 2) >= 2


def _is_low_reasoning(text: str) -> bool:
    """D8. Head verb is a low-reasoning op (review/format/convert/translate)."""
    return bool(_LOW_REASONING_RE.match(text)) if text else False


def _is_continuation(text: str) -> bool:
    """True if text looks like a continuation/short follow-up turn."""
    t = (text or "").strip()
    return bool(t) and len(t) <= 40 and bool(_CONTINUATION_RE.match(t))


def _root_turn_text(messages: List[Message], current_text: str) -> Optional[str]:
    """If current turn is a continuation, walk back past continuation turns to the
    last substantive user turn and return its text. Else None."""
    if not _is_continuation(current_text):
        return None
    user_texts = []
    for m in messages:
        if m.role == "user":
            txt = _msg_text(m).strip()
            if txt:
                user_texts.append(txt)
    # Exclude the current (last) turn; find the most recent non-continuation.
    for prev in reversed(user_texts[:-1]):
        if not _is_continuation(prev):
            return prev
    return None


_TIER_ORDER = [COMPLEXITY_TRIVIAL, COMPLEXITY_STANDARD, COMPLEXITY_DEEP]


def _max_tier(a: str, b: str) -> str:
    """Return the higher of two tiers."""
    return a if _TIER_ORDER.index(a) >= _TIER_ORDER.index(b) else b


def _capability_tier(scoring_text: str, category_scores: Optional[Dict[str, int]],
                     cognitive_score: int, workflow_score: int,
                     trivial_detected: bool) -> tuple[str, Dict[str, object]]:
    """Two-gate deep funnel. DEEP requires (D1 v D2 v D4) AND D3, OR D1/D4 saturated.
    Trivial fast-path first; low-reasoning cap last. Returns (tier, feature_vector)."""
    if trivial_detected:
        return COMPLEXITY_TRIVIAL, {"gate": "trivial_fastpath"}
    scope, scope_sat = _detect_scope(scoring_text)
    multidomain = _detect_multidomain(category_scores)
    density = _detect_density(scoring_text)
    wf, wf_sat = _detect_workflow_depth(scoring_text)
    fv: Dict[str, object] = {
        "D1_scope": scope, "D1_sat": scope_sat,
        "D2_multidomain": multidomain,
        "D3_density": density,
        "D4_workflow": wf, "D4_sat": wf_sat,
        "cognitive": cognitive_score, "workflow": workflow_score,
    }
    # Two-gate: (D1 v D2 v D4) AND D3, OR explicit saturation
    deep = ((scope or multidomain or wf) and density) or scope_sat or wf_sat
    tier = COMPLEXITY_DEEP if deep else COMPLEXITY_STANDARD
    # D8 low-reasoning cap: review/format/convert/translate head -> standard unless compound (wf=True)
    if tier == COMPLEXITY_DEEP and _is_low_reasoning(scoring_text) and not wf:
        tier = COMPLEXITY_STANDARD
        fv["capped"] = "low_reasoning_head"
    return tier, fv

# Domain-density signal: detects technical/domain-specific terminology that
# indicates a substantive query even without explicit cognitive verbs. This
# catches queries like "HTTP/2 multiplexing" or "convexity hedging" that
# would otherwise score 0 on cognitive/workflow signals.
_DOMAIN_DENSITY_RE = re.compile(
    r"\b(HTTP|HTTPS|HTTP\/2|HTTP\/1\.1|API|REST|GraphQL|gRPC|WebSocket"
    r"|JWT|OAuth|SAML|TLS|SSL"
    r"|SQL|NoSQL|Redis|PostgreSQL|MySQL|MongoDB"
    r"|React|Vue|Angular|Svelte|Next\.js|Nuxt"
    r"|Docker|Kubernetes|Terraform|Ansible"
    r"|CRDT|Raft|Paxos|Byzantine|consensus"
    r"|convexity|hedging|duration|yield|MBS|securities"
    r"|jurisdiction|standing|tort|arbitration|antitrust"
    r"|SaaS|B2B|churn|cohort|retention|ARR|MRR"
    r"|quantum|entanglement|Bell|theorem|relativity"
    r"|neural|transformer|attention|embedding|LLM"
    r"|multiplexing|pipelining|protocol"
    r"|shader|WebGPU|WebGL|rendering"
    r"|distributed|consistency|partition"
    r"|microservice|container|serverless"
    r")\b",
    re.IGNORECASE,
)


# ─── Token estimation ───────────────────────────────────────────────────────

CHARS_PER_TOKEN = 4


def _approx_tokens(text: str) -> int:
    """Rough 4-chars-per-token estimate. Fast, good enough for threshold logic."""
    if not text:
        return 0
    return max(1, len(text) // CHARS_PER_TOKEN)


def _msg_text(msg: Message) -> str:
    """Extract plain text from a message for token estimation."""
    if isinstance(msg.content, str):
        return msg.content or ""
    if isinstance(msg.content, list):
        parts = []
        for p in msg.content:
            if isinstance(p, dict):
                parts.append(p.get("text") or p.get("content") or "")
            else:
                parts.append(getattr(p, "text", "") or "")
        return " ".join(filter(None, parts))
    return ""


def _count_input_tokens(messages: List[Message]) -> int:
    """Estimate total input tokens across all messages."""
    return sum(_approx_tokens(_msg_text(m)) for m in messages)


# ─── Signal extraction ──────────────────────────────────────────────────────

def _has_tool_context(messages: List[Message]) -> bool:
    """Check if messages contain tool_calls or tool result messages."""
    for msg in messages:
        if msg.tool_calls:
            return True
        if msg.tool_call_id:
            return True
        if msg.role == "tool":
            return True
        # Anthropic-style tool_use / tool_result content blocks
        if isinstance(msg.content, list):
            for part in msg.content:
                if isinstance(part, dict) and part.get("type") in ("tool_use", "tool_result"):
                    return True
    return False


def _count_code_markers(text: str) -> int:
    """Count code-related signal occurrences in text."""
    score = 0
    score += len(_CODE_BLOCK_RE.findall(text))
    score += len(_FILE_PATH_RE.findall(text))
    score += min(len(_TRACEBACK_RE.findall(text)), 3)  # cap at 3
    score += min(len(_LINE_REF_RE.findall(text)), 5)   # cap at 5
    score += min(len(_FUNC_CLASS_RE.findall(text)), 3)  # cap at 3
    return score


def _count_multi_step_verbs(text: str) -> int:
    """Count multi-step verb occurrences in text."""
    return len(_MULTI_STEP_VERBS_RE.findall(text))


def _count_distinct_matches(pattern: re.Pattern, text: str) -> int:
    """Count unique normalized regex matches, not repeated occurrences.

    Capture-group tolerant. Uses finditer() rather than findall() so the
    return shape never depends on the number of capturing groups:
      - pattern with >=1 capture group -> normalize on group(1), preserving
        the stem-based counting these cognitive signal patterns rely on;
      - pattern with 0 capture groups   -> normalize on the full match.
    This immunizes the helper against the findall() tuple hazard, where a
    pattern carrying 2+ capture groups returns list[tuple] and crashes the
    downstream string ops (the exact regression this module was built to fix).
    """
    distinct = set()
    for match in pattern.finditer(text):
        value = match.group(1) if pattern.groups else match.group(0)
        normalized = value.lower().strip() if value else ""
        if normalized:
            distinct.add(normalized)
    return len(distinct)


# ─── Decision dataclass ─────────────────────────────────────────────────────

@dataclass
class ComplexityDecision:
    """Result of complexity estimation for a request.

    level: capability tier (trivial/standard/deep) — from the D1-D4 funnel.
    budget_max_tokens: max_tokens ceiling, decoupled from level (flat 65536).
    feature_vector: D10 diagnostic vector for offline replay-calibration.
    """
    level: str = COMPLEXITY_STANDARD
    budget_max_tokens: int = 65536
    score: int = 0
    estimated_input_tokens: int = 0
    target_max_tokens: int = 65536
    old_max_tokens: Optional[int] = None
    reasons: List[str] = field(default_factory=list)
    changed: bool = False
    feature_vector: Dict[str, object] = field(default_factory=dict)


# ─── Main API ────────────────────────────────────────────────────────────────

def estimate_request_complexity(
    request: ChatCompletionRequest,
    category_scores: Optional[Dict[str, int]] = None,
) -> ComplexityDecision:
    """Estimate the complexity level of a ChatCompletionRequest.

    Uses deterministic heuristics: token count, code markers, multi-step
    verbs, tool context, and long-context thresholds. Returns a
    ComplexityDecision with level, score, and reasoning.

    The capability tier (level) is computed by the D1-D4 deep funnel,
    decoupled from the budget (max_tokens ceiling). category_scores is an
    optional Phase 2A per-category score vector for D2 multi-domain detection;
    callers that pass None (or don't pass it) get D2=false.
    """
    messages = request.messages or []
    # Token estimation: ALL messages (system prompt counts toward request size)
    input_tokens = _count_input_tokens(messages)
    # Scoring text: the ISOLATED current-turn intent, with injected scaffolding
    # stripped (see request_intent). System prompts, client-injected
    # persona/context blocks, and multi-turn history are full of technical
    # keywords (code, debug, analyze, router) that would inflate complexity
    # and route simple messages like 'hello' to deep. Isolating the current
    # turn is the fix.
    _intent = extract_current_intent(request)
    scoring_text = _intent.text

    load_score = 0
    workflow_score = 0
    cognitive_score = 0
    reasons = []

    # Signal 1: Input token estimate (load_score — capped at 3, not 6)
    if input_tokens < 50:
        pass  # +0
    elif input_tokens < 200:
        load_score += 1
        reasons.append(f"short input (~{input_tokens} tokens)")
    elif input_tokens < 1000:
        load_score += 2
        reasons.append(f"medium input (~{input_tokens} tokens)")
    elif input_tokens < 4000:
        load_score += 3
        reasons.append(f"long input (~{input_tokens} tokens)")
    else:
        load_score += 3
        reasons.append(f"very long input (~{input_tokens} tokens)")

    # Signal 2: Code markers (workflow_score)
    code_markers = _count_code_markers(scoring_text)
    if code_markers >= 1:
        workflow_score += min(code_markers, 6)
        reasons.append(f"code markers ({code_markers})")

    # Signal 3: Multi-step verbs (workflow_score — raw count, capped)
    multi_step = _count_multi_step_verbs(scoring_text)
    if multi_step >= 1:
        workflow_score += min(multi_step * 2, 6)
        reasons.append(f"multi-step verbs ({multi_step})")

    # Signal 3b: Analytical depth verbs (cognitive_score — distinct count)
    analytical = _count_distinct_matches(_ANALYTICAL_VERBS_RE, scoring_text)
    if analytical >= 1:
        cognitive_score += min(analytical, 4)
        reasons.append(f"analytical verbs ({analytical} distinct)")

    # Signal 3c: Creative / long-form requests (cognitive_score — distinct count)
    creative = _count_distinct_matches(_CREATIVE_LONGFORM_RE, scoring_text)
    if creative >= 1:
        cognitive_score += min(creative, 4)
        reasons.append(f"creative/long-form ({creative} distinct)")

    # Signal 3d: Research / evidence markers (cognitive_score — distinct count)
    research = _count_distinct_matches(_RESEARCH_MARKERS_RE, scoring_text)
    if research >= 1:
        cognitive_score += min(research, 4)
        reasons.append(f"research markers ({research} distinct)")

    # Signal 3e: Multi-perspective / dialectical (cognitive_score — distinct count)
    multi_persp = _count_distinct_matches(_MULTI_PERSPECTIVE_RE, scoring_text)
    if multi_persp >= 1:
        cognitive_score += min(multi_persp, 3)
        reasons.append(f"multi-perspective ({multi_persp} distinct)")

    # Signal 3f: Structural complexity indicators (cognitive_score — distinct count)
    structural = _count_distinct_matches(_STRUCTURAL_DEPTH_RE, scoring_text)
    if structural >= 1:
        cognitive_score += min(structural, 4)
        reasons.append(f"structural depth ({structural} distinct)")

    # Signal 3g: Domain-density (cognitive_score - domain-specific terminology)
    # Detects technical/domain terms that indicate a substantive query even
    # without explicit cognitive verbs. Catches queries like "HTTP/2 multiplexing"
    # or "convexity hedging" that would otherwise score 0 on cognitive signals.
    domain_hits = _count_distinct_matches(_DOMAIN_DENSITY_RE, scoring_text)
    if domain_hits >= 1:
        cognitive_score += min(domain_hits, 3)
        reasons.append(f"domain density ({domain_hits} distinct)")

    # Signal 4: Tool context (workflow_score)
    if _has_tool_context(messages):
        workflow_score += 3
        reasons.append("tool context present")

    # Signal 5: Trivial detection (greeting / tiny request on the CURRENT turn)
    # Uses the ISOLATED current-turn intent from extract_current_intent, not the
    # raw last message. This lets a trivial follow-up ('thanks', 'ok') route fast
    # even after a prior technical exchange, and catches trivial turns wrapped
    # in scaffolding. Guard on zero cognitive/workflow signal from the CURRENT
    # turn's scoring_text to ensure real tasks are never trivialized.
    # Length guard: a trivial turn must be genuinely short (≤ 50 chars). Long
    # text with no cognitive signal still needs the length guard so it isn't
    # mislabeled trivial.
    _trivial_detected = False
    if cognitive_score == 0 and workflow_score == 0:
        current_turn_text = (_intent.text or "").strip()
        if (
            current_turn_text
            and len(current_turn_text) <= 50
            and _TRIVIAL_RE.match(current_turn_text)
        ):
            # cognitive_score and workflow_score are already 0 by the guard
            # condition above; only load_score can be nonzero here.
            load_score = 0
            _trivial_detected = True
            reasons.clear()
            reasons.append("trivial greeting/tiny request")

    # Signal 6: Many messages (conversation depth - load_score)
    # SKIP when trivial was detected - a greeting in a long conversation
    # is still a greeting, not a standard-complexity request.
    if not _trivial_detected:
        if len(messages) > 10:
            load_score += 2
            reasons.append(f"deep conversation ({len(messages)} messages)")
        elif len(messages) > 6:
            load_score += 1
            reasons.append(f"moderate conversation ({len(messages)} messages)")

    # Combine additive score (LEGACY: kept for backward-compat with existing
    # readers; the tier is now determined by the funnel below).
    base_score = cognitive_score + workflow_score
    if base_score >= 2:
        score = base_score + load_score
    else:
        score = base_score + min(load_score, 2)

    # D1-D4 deep funnel: capability tier independent of input size.
    direct_tier, fv = _capability_tier(
        scoring_text, category_scores, cognitive_score, workflow_score, _trivial_detected,
    )

    # Continuation inheritance: if the current turn is a short follow-up,
    # recompute funnel on the root turn and take the max tier (floor).
    root = _root_turn_text(messages, scoring_text)
    if root:
        root_tier, root_fv = _capability_tier(
            root, category_scores, cognitive_score, workflow_score, False,
        )
        fv["root_tier"] = root_tier
        fv["direct_tier"] = direct_tier
        final_tier = _max_tier(direct_tier, root_tier)
        if final_tier != direct_tier:
            reasons.append(f"continuation floor: inherited {root_tier} from root")
        level = final_tier
    else:
        level = direct_tier

    return ComplexityDecision(
        level=level,
        budget_max_tokens=65536,
        score=score,
        estimated_input_tokens=input_tokens,
        reasons=reasons or ["default classification"],
        feature_vector=fv,
    )


def apply_task_complexity_routing(
    request: ChatCompletionRequest,
    config: dict,
    model_id: str = "",
) -> ChatCompletionRequest:
    """Apply the configured output ceiling; fail open on configuration errors."""
    tools_cfg = (config or {}).get("tools", {})
    if not tools_cfg.get("task_complexity_router", False):
        return request
    try:
        minimum = int(tools_cfg.get("task_complexity_min_tokens", 1024))
        ceiling = int(tools_cfg.get("task_complexity_max_tokens", 65536))
        ceiling = max(minimum, ceiling)
        old_max = request.max_tokens
        if old_max is None or old_max < ceiling or (
            tools_cfg.get("task_complexity_allow_lowering", False) and old_max > ceiling
        ):
            request.max_tokens = ceiling
        return request
    except Exception:
        return request
