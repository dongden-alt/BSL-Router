"""
Middleware.coding_category_classifier — deterministic OAC agent request classifier.

BSL-Lite targets coding agents/IDEs (Claude Code, Cursor, Aider), NOT general
chat. This classifier uses OAC agent-aligned keyword patterns to route
requests to the appropriate OAC agent lane.

Pure, stateless, fail-open. Operates on the internal ChatCompletionRequest.
Uses conservative EN+VI keyword/signal regexes; ambiguous or low-signal
requests fall back to ``scout`` (which also serves as the general fallback).

8 OAC agents (scout is the fallback, merged with general):
  scout, planner, auditor, fast_coder, power_coder, ultra_coder,
  refactor, frontend_coder
"""

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from app.models import ChatCompletionRequest, Message
from app.middleware.request_intent import extract_current_intent
# ─── Agent definitions (OAC-aligned) ────────────────────────────────────────

CATEGORY_SCOUT = "scout"
CATEGORY_PLANNER = "planner"
CATEGORY_AUDITOR = "auditor"
CATEGORY_FAST_CODER = "fast_coder"
CATEGORY_POWER_CODER = "power_coder"
CATEGORY_ULTRA_CODER = "ultra_coder"
CATEGORY_REFACTOR = "refactor"
CATEGORY_FRONTEND_CODER = "frontend_coder"

# 8 OAC agents (General is merged into Scout — no separate general fallback).
# Order matters for deterministic tie-breaking: the first category in this
# list wins when two categories have the same score.
CATEGORY_ORDER = [
    CATEGORY_AUDITOR,        # review/audit/security signals are unambiguous
    CATEGORY_REFACTOR,       # refactor/restructure signals
    CATEGORY_FRONTEND_CODER, # UI/component/frontend signals
    CATEGORY_ULTRA_CODER,    # complex/difficult/architecture change signals
    CATEGORY_POWER_CODER,    # implement/feature/multi-file signals
    CATEGORY_FAST_CODER,     # quick fix/small change/typo signals
    CATEGORY_PLANNER,        # plan/architect/design/brainstorm signals
    CATEGORY_SCOUT,          # search/find/research/look up signals (also general fallback)
]

# Scout is the fallback category (replaces "general").
CATEGORY_GENERAL = CATEGORY_SCOUT


# ─── Keyword/signal patterns ────────────────────────────────────────────────
# Each pattern is matched case-insensitively. Scoring is intentionally simple:
# one hit = +1 for that category. Word guards prevent matching inside tokens.

_EN_VI_WORD = r"(?<![\w-])"
_EN_VI_WORD_END = r"(?![\w-])"


def _term(*words: str) -> str:
    """Build a regex alternation with word guards for each term."""
    return "|".join(f"{_EN_VI_WORD}{re.escape(w)}{_EN_VI_WORD_END}" for w in words)


def _flex_phrase(*parts: str) -> str:
    """Build a phrase regex allowing ONE optional word between each part.

    Covers adjective/adverb intrusions that broke exact phrases:
    "Write a Python function" ("Python" between "a" and "function").
    The gap is whitespace plus at most one non-space token, so both
    "write a function" and "write a Python function" match.
    """
    joined = " ".join(re.escape(p) for p in parts)
    words = joined.split()
    guarded = [f"{_EN_VI_WORD}{w}{_EN_VI_WORD_END}" for w in words]
    gap = r"\s+(?:\S+\s+)?"
    return gap.join(guarded)

# Each category has STRONG terms (specific, unambiguous — weight 2) and WEAK
# terms (generic verbs/nouns that recur across many intents — weight 1). This
# two-tier weighting is the core fix for the flat-scoring ties that misrouted
# requests: a specific phrase like "quick fix" must outrank a generic "add".
#
# Ordering WITHIN each list matters: shorter/base terms are listed BEFORE the
# longer phrases that contain them, because regex alternation is first-match-
# wins. This preserves the original single-match-per-site scoring behavior.
CATEGORY_TERMS = {
    CATEGORY_SCOUT: {
        "strong": [
            "search", "find", "look up", "lookup", "grep", "locate",
            "where is", "explore", "scan", "discover", "investigate",
            "gather info",
            "tìm kiếm", "tra cứu", "nghiên cứu", "khám phá",
        ],
        "weak": [
            "research", "docs", "read the", "show me", "what is",
            "explain how", "how does", "tell me about",
            "tìm", "đọc", "giải thích", "thế nào", "là gì",
        ],
    },
    CATEGORY_PLANNER: {
        "strong": [
            "plan", "planning", "architect", "architecture", "design system",
            "system design", "brainstorm", "roadmap", "milestone",
            "tech stack", "technology selection",
            "kiến trúc", "thiết kế hệ thống", "lập kế hoạch", "kế hoạch",
            "chiến lược", "đường lối",
        ],
        "weak": [
            "strategy", "trade-off", "tradeoff", "scalability",
            "maintainability",
        ],
    },
    CATEGORY_AUDITOR: {
        "strong": [
            "review", "code review", "peer review", "pr review",
            "pull request review", "audit", "security audit", "quality gate",
            "lint", "linter", "eslint", "pylint", "flake8", "mypy",
            "typescript check", "tsc", "vulnerability", "security issue",
            "code smell", "technical debt", "cyclomatic",
            "review this", "review the", "audit this", "audit the",
            "kiểm tra code", "review code", "thanh tra", "audit bảo mật",
            "chất lượng code", "nợ kỹ thuật", "độ phức tạp",
        ],
        # "complexity" is generic (overlaps refactor/ultra intent) — demote to
        # WEAK so "refactor ... to reduce complexity" no longer ties to auditor.
        "weak": [
            "complexity",
        ],
    },
    CATEGORY_FAST_CODER: {
        "strong": [
            "quick fix", "small change", "tiny fix", "typo", "rename",
            "one-liner", "simple fix", "minor change", "trivial fix",
            "fast change", "hotfix", "patch", "small task",
            "add a comment", "add comment", "add a docstring", "docstring",
            "sửa nhanh", "đổi tên", "vặt vãnh",
        ],
        "weak": [
            "nhỏ",
        ],
    },
    CATEGORY_POWER_CODER: {
        "strong": [
            "implement", "multi-file", "multifile", "scaffold",
            "write a function", "write a class", "write a script",
            "write a module", "write a component",
            "implement a", "implement the", "scaffold a", "scaffold the",
            "viết hàm", "viết class", "triển khai", "viết code", "sinh code",
            "fix the bug", "fix a bug", "fix this bug", "bug fix",
            "debug", "debugging", "error handling",
        ],
        # Generic CRUD verbs are WEAK so they never dominate a specific signal.
        "weak": [
            "feature", "add", "create", "build", "develop", "generate",
            "create a", "create the", "generate a", "generate the",
            "viết", "tạo", "tạo hàm", "tạo class", "tính năng", "xây dựng",
            "bug", "fix", "lỗi", "sửa",
        ],
    },
    CATEGORY_ULTRA_CODER: {
        "strong": [
            "hard problem", "architecture change", "refactor system",
            "algorithm", "redesign", "overhaul", "rewrite",
            "migrate", "migration", "deep refactor", "system-wide",
            "thay đổi kiến trúc", "viết lại", "di cư", "toàn hệ thống",
        ],
        "weak": [
            "complex", "difficult", "optimize", "optimization", "performance",
            "phức tạp", "khó", "tối ưu",
        ],
    },
    CATEGORY_REFACTOR: {
        # NOTE: "đổi tên" (rename) intentionally NOT here — rename is a trivial
        # op owned by fast_coder. Keeping it in both taxonomies caused a tie.
        "strong": [
            "refactor", "refactoring", "restructure", "reorganize",
            "extract method", "extract function", "extract class",
            "inline", "move class", "move function",
            "split function", "split class", "merge class",
            "design pattern", "pattern migration", "modernize",
            "tái cấu trúc", "tái tổ chức", "tách hàm", "tách class",
            "hiện đại hóa",
        ],
        "weak": [
            "simplify", "clean up", "cleanup", "gộp", "đơn giản hóa",
            "làm sạch code",
        ],
    },
    CATEGORY_FRONTEND_CODER: {
        "strong": [
            "ui", "component", "css", "layout", "frontend",
            "react", "vue", "angular", "svelte", "tailwind",
            "bootstrap", "design-to-code", "figma to code",
            "web component", "html", "svg", "canvas",
            "responsive", "accessibility", "a11y", "dark mode",
            "giao diện", "thành phần", "mặt trước",
        ],
        "weak": [],
    },
}

# Flexible phrases: multi-word coding verbs with optional adjective/adverb
# intrusions ("write a Python function"). Matched text differs from the
# literal strong terms, so these are scored STRONG explicitly in
# ``score_categories`` via ``CATEGORY_FLEX_PATTERNS``.
CATEGORY_FLEX = {
    CATEGORY_POWER_CODER: [
        ("write", "a", "function"),
        ("write", "a", "class"),
        ("write", "a", "script"),
        ("write", "a", "module"),
        ("write", "a", "component"),
    ],
}


def _norm(term: str) -> str:
    """Normalize a term/match for set membership: lowercased, stripped."""
    return term.lower().strip()


# Compile one pattern per category from strong+weak terms (strong listed first,
# ordering within tiers preserved for match stability), plus a strong-term set
# used to weight each distinct match at scoring time.
CATEGORY_PATTERNS = {}
CATEGORY_STRONG_SET = {}
CATEGORY_FLEX_PATTERNS = {}
for _cat, _tiers in CATEGORY_TERMS.items():
    _strong_terms = _tiers.get("strong", [])
    _weak_terms = _tiers.get("weak", [])
    _flex_specs = CATEGORY_FLEX.get(_cat, [])
    _flex_pats = [
        (spec, re.compile(_flex_phrase(*spec), re.IGNORECASE))
        for spec in _flex_specs
    ]
    CATEGORY_FLEX_PATTERNS[_cat] = _flex_pats
    _extra = "|".join(p.pattern for _, p in _flex_pats)
    CATEGORY_PATTERNS[_cat] = re.compile(
        _term(*_strong_terms, *_weak_terms) + (f"|{_extra}" if _extra else ""),
        re.IGNORECASE,
    )
    CATEGORY_STRONG_SET[_cat] = {_norm(t) for t in _strong_terms}

# Term weights: a STRONG (specific) hit counts double a WEAK (generic) hit.
WEIGHT_STRONG = 2
WEIGHT_WEAK = 1

# Minimum weighted score to accept a non-fallback category (1 = one weak hit).
CATEGORY_SCORE_THRESHOLD = 1


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _msg_text(msg: Message) -> str:
    """Extract plain text from a message for classification."""
    if msg.content is None:
        return ""
    if isinstance(msg.content, str):
        return msg.content
    if isinstance(msg.content, list):
        parts = []
        for part in msg.content:
            if isinstance(part, dict):
                parts.append(part.get("text") or part.get("content") or "")
            else:
                parts.append(getattr(part, "text", "") or "")
        return " ".join(filter(None, parts))
    return ""


def _extract_request_text(request: ChatCompletionRequest) -> str:
    """Extract the user's CURRENT request text for classification.

    Delegates to ``request_intent.extract_current_intent`` which isolates the
    current user turn and strips injected scaffolding (agent instructions,
    directive blocks, context dumps, prior history). System prompts and
    coding-agent envelopes (e.g. "You are Claude Code. Write functions...")
    do NOT represent the user's actual task; classifying them routes trivial
    messages like 'hello' to the wrong agent lane.
    """
    return extract_current_intent(request).text


# ─── Decision dataclass ─────────────────────────────────────────────────────

@dataclass
class CodingCategoryDecision:
    category: str = CATEGORY_GENERAL
    confidence: float = 0.0
    score: int = 0
    reasons: List[str] = field(default_factory=list)
    # Observability (mirrors the Phase 2A chat CategoryDecision). Additive:
    # the empty-text path returns {} / None / 0 so no consumer breaks.
    scores: Dict[str, int] = field(default_factory=dict)
    runner_up: Optional[str] = None
    margin: int = 0


# ─── Main API ─────────────────────────────────────────────────────────────────

def score_categories(text: str) -> Dict[str, int]:
    """Return the weighted per-category score vector for ``text``.

    Public so tests and diagnostics reuse the EXACT production scoring. A STRONG
    term hit contributes ``WEIGHT_STRONG``; a WEAK term hit ``WEIGHT_WEAK``.
    Scored by DISTINCT normalized matches so repeated scaffolding keywords never
    amplify a category.
    """
    scores: Dict[str, int] = {}
    for category in CATEGORY_ORDER:
        pattern = CATEGORY_PATTERNS[category]
        strong_set = CATEGORY_STRONG_SET[category]
        distinct = {
            _norm(m if isinstance(m, str) else " ".join(m))
            for m in pattern.findall(text)
        }
        distinct.discard("")
        # Flex-phrase matches ("write a Python function") are STRONG even
        # though their text is not a literal strong term.
        flex_hits = set()
        for _, fp in CATEGORY_FLEX_PATTERNS.get(category, []):
            for m in fp.findall(text):
                flex_hits.add(_norm(m if isinstance(m, str) else " ".join(m)))
        scores[category] = sum(
            WEIGHT_STRONG if (term in strong_set or term in flex_hits) else WEIGHT_WEAK
            for term in distinct
        )

    # A specific quick-edit phrase owns the route when the competing power
    # signal contains only generic verbs ("add", "fix", "bug"). Do not hide
    # real implementation intent: strong power terms still compete normally.
    #
    # DEMOTE, never zero. Zeroing removes power_coder from the ranked list
    # entirely, which drops ``runner_up`` to None and deletes the cross-domain
    # fallback route that agentic-max appends. Clamping to one below the
    # fast_coder score loses the primary decision while keeping the route
    # visible as the runner-up.
    fast_score = scores.get(CATEGORY_FAST_CODER, 0)
    power_score = scores.get(CATEGORY_POWER_CODER, 0)
    if fast_score >= WEIGHT_STRONG and power_score >= fast_score:
        power_terms = {
            _norm(m if isinstance(m, str) else " ".join(m))
            for m in CATEGORY_PATTERNS[CATEGORY_POWER_CODER].findall(text)
        }
        if not (power_terms & CATEGORY_STRONG_SET[CATEGORY_POWER_CODER]):
            scores[CATEGORY_POWER_CODER] = fast_score - 1
    return scores


def classify_coding_request_category(request: ChatCompletionRequest) -> CodingCategoryDecision:
    """Deterministically classify a coding-agent request into one category.

    Conservative: returns ``general`` (scout) when signals are weak or absent.
    Scoring is specificity-weighted (STRONG=2 / WEAK=1) so a specific phrase
    beats a generic verb outright. True ties (equal weighted score) remain
    deterministic, resolved by ``CATEGORY_ORDER``; the tie is recorded in
    ``reasons`` and softens ``confidence`` so downstream sees the ambiguity.
    """
    text = _extract_request_text(request)
    if not text or not text.strip():
        return CodingCategoryDecision(
            category=CATEGORY_GENERAL,
            confidence=0.0,
            score=0,
            reasons=["empty request text"],
            scores={},
            runner_up=None,
            margin=0,
        )

    scores = score_categories(text)
    reasons = [f"{c}={scores[c]}" for c in CATEGORY_ORDER if scores[c]]

    # Rank scoring categories by (weighted score desc, CATEGORY_ORDER index).
    # CATEGORY_ORDER is the deterministic tie-break for equal scores.
    ranked = sorted(
        (c for c in CATEGORY_ORDER if scores[c] > 0),
        key=lambda c: (-scores[c], CATEGORY_ORDER.index(c)),
    )
    best_category = ranked[0] if ranked else CATEGORY_GENERAL
    best_score = scores[best_category] if ranked else 0
    runner_up = ranked[1] if len(ranked) >= 2 else None
    runner_up_score = scores[runner_up] if runner_up else 0
    margin = best_score - runner_up_score

    category = best_category if best_score >= CATEGORY_SCORE_THRESHOLD else CATEGORY_GENERAL

    # Coarse deterministic confidence — NOT a real probability.
    # 0.0 = no signal, 0.5 = single weak hit, 1.0 = strong/2+ weighted signal.
    if best_score >= 2:
        confidence = 1.0
    elif best_score == 1:
        confidence = 0.5
    else:
        confidence = 0.0

    # A dead-heat (margin 0 against a real runner-up) is genuinely ambiguous:
    # it was resolved only by CATEGORY_ORDER. Surface that in confidence+reasons.
    if runner_up is not None and margin == 0 and confidence >= 1.0:
        confidence = 0.75
        reasons.append(f"tie {best_category}~{runner_up} (order-resolved)")

    return CodingCategoryDecision(
        category=category,
        confidence=confidence,
        score=best_score,
        reasons=reasons or ["no strong category signals"],
        scores=scores,
        runner_up=runner_up,
        margin=margin,
    )
