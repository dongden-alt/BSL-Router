"""
Middleware.category_classifier — deterministic bilingual request category classifier.

Pure, stateless, fail-open. Operates on the internal ChatCompletionRequest.
Uses conservative EN+VI keyword/signal regexes; ambiguous or low-signal requests
fall back to ``general``.
"""

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from app.models import ChatCompletionRequest, Message
from app.middleware.request_intent import extract_current_intent


# ─── Category definitions ───────────────────────────────────────────────────

CATEGORY_TECHNICAL = "technical"
CATEGORY_LAW = "law"
CATEGORY_HEALTH = "health"
CATEGORY_BUSINESS = "business"
CATEGORY_GEOPOLITICS = "geopolitics"
CATEGORY_CREATIVE = "creative"
CATEGORY_EDUCATION = "education"
CATEGORY_FINANCE = "finance"
CATEGORY_RESEARCH = "research"
CATEGORY_SCIENCE = "science"
CATEGORY_LIFESTYLE = "lifestyle"
CATEGORY_PHILOSOPHY = "philosophy"
CATEGORY_GENERAL = "general"

# 13 locked categories (general is the fallback, not scored).
# Order matters for deterministic tie-breaking: the first category in this
# list wins when two categories have the same score.
CATEGORY_ORDER = [
    CATEGORY_TECHNICAL,
    CATEGORY_LAW,
    CATEGORY_HEALTH,
    CATEGORY_FINANCE,  # finance before business to win revenue/profit ties
    CATEGORY_BUSINESS,
    CATEGORY_GEOPOLITICS,
    CATEGORY_CREATIVE,
    CATEGORY_EDUCATION,
    CATEGORY_RESEARCH,
    CATEGORY_SCIENCE,
    CATEGORY_LIFESTYLE,
    CATEGORY_PHILOSOPHY,
]


# ─── Keyword/signal patterns ────────────────────────────────────────────────
# Each pattern is matched case-insensitively. Scoring is intentionally simple:
# one hit = +1 for that category. The leading/trailing non-word guards prevent
# matching inside unrelated tokens while still catching common inflections.

_EN_VI_WORD = r"(?<![\w-])"
_EN_VI_WORD_END = r"(?![\w-])"


def _term(*words: str) -> str:
    """Build a regex alternation with word guards for each term."""
    return "|".join(f"{_EN_VI_WORD}{re.escape(w)}{_EN_VI_WORD_END}" for w in words)


CATEGORY_PATTERNS = {
    CATEGORY_TECHNICAL: re.compile(
        _term(
            "code", "coding", "programmer", "developer", "debug", "debugger",
            "bug", "error", "traceback", "exception", "stack trace", "api",
            "fastapi", "python", "javascript", "typescript", "node", "nodejs",
            "router", "middleware", "deploy", "deployment", "refactor",
            "rewrite", "function", "class", "module", "library", "framework",
            "database", "sql", "query", "lập trình", "lập trình viên", "kiểm lỗi",
            "sửa bug", "sửa lỗi", "mã nguồn", "hàm", "biến", "class", "thư viện",
            "hệ thống", "server", "cơ sở dữ liệu", "triển khai",
            # Protocol & networking
            "HTTP", "HTTPS", "HTTP/2", "HTTP/1.1", "multiplexing", "pipelining",
            "protocol", "WebSocket", "gRPC", "GraphQL", "REST", "SOAP",
            "TCP", "UDP", "TLS", "SSL", "certificate", "OAuth", "JWT", "SAML",
            # Frontend & rendering
            "WebGPU", "WebGL", "WebGL2", "shader", "rendering", "browser",
            "CSS", "HTML", "DOM", "canvas", "WebRTC",
            # Distributed systems
            "distributed", "CRDT", "consistency model", "partition tolerance",
            "consensus", "Byzantine", "Raft", "Paxos", "microservice",
            "concurrent", "parallel", "async", "synchronization",
            # Infrastructure & DevOps
            "container", "Kubernetes", "Docker", "CI/CD", "devops",
            "infrastructure", "scalability", "load balancing", "reverse proxy",
            "nginx", "CDN", "edge computing", "serverless",
            # Data & ML
            "NoSQL", "Redis", "PostgreSQL", "MySQL", "MongoDB",
            "neural", "transformer", "attention", "embedding",
            "machine learning", "deep learning",
        ),
        re.IGNORECASE,
    ),
    CATEGORY_LAW: re.compile(
        _term(
            "legal", "law", "lawyer", "contract", "agreement", "regulation",
            "compliance", "court", "lawsuit", "litigation", "statute", "clause",
            "pháp lý", "luật", "luật sư", "hợp đồng", "tòa án", "kiện",
            "tranh chấp", "quy định", "tuân thủ", "điều khoản", "pháp luật",
            # Jurisdictional & procedural
            "jurisdictional", "jurisdiction", "standing", "cross-border",
            "discovery", "deposition", "tort", "class action",
            "antitrust", "arbitration", "mediation", "injunction",
            "appeal", "appellate", "verdict", "ruling",
            # IP & corporate
            "intellectual property", "patent", "copyright", "trademark",
            "infringement", "trade secret", "NDA", "non-compete",
            "merger", "acquisition", "due diligence",
            "FTC", "SEC", "DOJ", "GDPR", "HIPAA",
        ),
        re.IGNORECASE,
    ),
    CATEGORY_HEALTH: re.compile(
        _term(
            "medical", "medicine", "health", "healthcare", "diagnosis",
            "symptom", "symptoms", "treatment", "doctor", "physician", "patient",
            "thuốc", "bệnh", "triệu chứng", "bác sĩ", "điều trị", "chẩn đoán",
            "sức khỏe", "y tế", "bệnh nhân",
        ),
        re.IGNORECASE,
    ),
    CATEGORY_BUSINESS: re.compile(
        _term(
            "strategy", "strategic", "market", "marketing", "customer", "client",
            "startup", "go-to-market", "gtm", "sales", "business",
            "product", "launch",
            "chiến lược", "thị trường", "khách hàng",
            "khởi nghiệp", "doanh nghiệp", "bán hàng", "sản phẩm",
            "ra mắt sản phẩm",
            # SaaS & subscription metrics
            "SaaS", "B2B", "B2C", "churn", "cohort", "retention",
            "ARR", "MRR", "CAC", "LTV", "PLG", "freemium",
            "subscription", "onboarding", "activation",
            # Growth & metrics
            "quarterly", "annual", "revenue", "metrics", "KPI", "OKR",
            "growth", "acquisition", "funnel", "conversion", "engagement",
            "stripe", "sales-led", "marketing-led",
            # Operations
            "operations", "supply chain", "logistics", "vendor",
            "stakeholder", "shareholder", "board", "investor",
            "pitch", "pitch deck", "burn rate", "runway",
        ),
        re.IGNORECASE,
    ),
    CATEGORY_GEOPOLITICS: re.compile(
        _term(
            "geopolitics", "geopolitical", "diplomacy", "diplomatic", "sanctions",
            "war", "election", "foreign policy", "international relations",
            "conflict", "nation", "government", "địa chính trị", "ngoại giao",
            "trừng phạt", "chiến tranh", "bầu cử", "chính sách đối ngoại",
            "quan hệ quốc tế", "xung đột", "chính phủ",
        ),
        re.IGNORECASE,
    ),
    CATEGORY_CREATIVE: re.compile(
        _term(
            "story", "poem", "poetry", "screenplay", "script", "novel",
            "brand voice", "creative", "write", "writing", "fiction",
            "thiết kế", "truyện", "thơ", "kịch bản", "sáng tạo",
            "tiểu thuyết", "tác phẩm", "phong cách thương hiệu",
            # Flash fiction & short form
            "flash fiction", "short story", "micro fiction", "drabble",
            "time-traveling", "time travel", "librarian",
            # Narrative craft
            "character", "plot", "narrative", "protagonist", "antagonist",
            "setting", "worldbuilding", "dialogue", "monologue",
            "scene", "act", "chapter", "verse", "stanza",
            # Screenwriting & publishing
            "spec script", "treatment", "logline", "query letter",
            "manuscript", "draft", "outline", "premise",
            # Creative direction
            "mood board", "storyboard", "concept art", "illustration",
            "typography", "color palette", "visual identity",
        ),
        re.IGNORECASE,
    ),
    CATEGORY_EDUCATION: re.compile(
        _term(
            "teach", "teaching", "explain", "explanation", "lesson", "curriculum",
            "course", "tutorial", "learn", "learning", "study",
            "học", "dạy", "giải thích", "bài học", "giáo trình", "khóa học",
            "hướng dẫn", "giáo dục",
        ),
        re.IGNORECASE,
    ),
    CATEGORY_FINANCE: re.compile(
        _term(
            "investment", "invest", "investing", "stock", "stocks", "crypto",
            "cryptocurrency", "valuation", "finance", "financial", "budget",
            "revenue", "profit", "fund", "trading", "forex",
            "tài chính", "đầu tư", "cổ phiếu", "định giá", "tiền số",
            "tiền điện tử", "ngân sách", "lợi nhuận", "giao dịch",
            "doanh thu",  # finance-primary; removed from business to prevent collision
            # Fixed income & bonds
            "convexity", "hedging", "hedge", "mortgage-backed", "MBS",
            "securities", "bonds", "fixed income", "duration", "yield curve",
            "spread", "treasury", "corporate bond", "municipal bond",
            # Derivatives
            "options", "futures", "derivatives", "swaps", "forwards",
            "strike price", "expiration", "implied volatility",
            # Portfolio & risk
            "portfolio", "risk management", "VaR", "Sharpe", "alpha", "beta",
            "correlation", "volatility", "leverage", "margin",
            "diversification", "asset allocation", "rebalancing",
        ),
        re.IGNORECASE,
    ),
    CATEGORY_RESEARCH: re.compile(
        _term(
            "research", "study", "studies", "survey", "literature review",
            "hypothesis", "methodology", "data analysis", "findings",
            "nghiên cứu", "khảo sát", "tổng quan tài liệu", "giả thuyết",
            "phương pháp luận", "phân tích dữ liệu",
        ),
        re.IGNORECASE,
    ),
    CATEGORY_SCIENCE: re.compile(
        _term(
            "physics", "chemistry", "biology", "science", "scientific",
            "experiment", "hypothesis", "theory", "quantum", "molecule",
            "vật lý", "hóa học", "sinh học", "khoa học", "thí nghiệm",
            "lý thuyết", "phân tử",
        ),
        re.IGNORECASE,
    ),
    CATEGORY_LIFESTYLE: re.compile(
        _term(
            "travel", "food", "recipe", "cooking", "fitness", "wellness",
            "fashion", "hobby", "gardening", "home decor", "self-care",
            "du lịch", "ẩm thực", "nấu ăn", "thể hình", "thời trang",
            "sở thích", "trang trí nhà",
        ),
        re.IGNORECASE,
    ),
    CATEGORY_PHILOSOPHY: re.compile(
        _term(
            "philosophy", "ethics", "morality", "existence", "consciousness",
            "meaning", "purpose", "free will", "determinism", "metaphysics",
            "triết học", "đạo đức", "tồn tại", "ý thức", "ý nghĩa",
            "mục đích", "ý chí tự do",
        ),
        re.IGNORECASE,
    ),
}

# Minimum score required to accept a non-general category.
CATEGORY_SCORE_THRESHOLD = 2


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
    directive blocks, context dumps, prior history). This is the single source
    of truth for "what is the user actually asking" — without it, client
    scaffolding in user-role messages inflates trivial messages like 'hello'
    to technical+deep by sheer keyword repetition.
    """
    return extract_current_intent(request).text


# ─── Decision dataclass ─────────────────────────────────────────────────────

@dataclass
class CategoryDecision:
    category: str = CATEGORY_GENERAL
    confidence: float = 0.0
    score: int = 0
    reasons: List[str] = field(default_factory=list)
    # Phase 2 (D2 multi-domain prerequisite): full per-category score vector.
    # Populated by classify_request_category; empty dict on the empty-text path.
    scores: Dict[str, int] = field(default_factory=dict)
    # Second-highest scoring category (deterministic tie-break by CATEGORY_ORDER).
    # None when fewer than 2 categories scored > 0.
    runner_up: Optional[str] = None
    # True when >= 2 categories each meet CATEGORY_SCORE_THRESHOLD.
    multi_domain: bool = False


# ─── Main API ─────────────────────────────────────────────────────────────────

def classify_request_category(request: ChatCompletionRequest) -> CategoryDecision:
    """Deterministically classify a chat request into one category.

    Conservative: returns ``general`` when signals are weak or ambiguous.
    Ties are deterministic and resolved by ``CATEGORY_ORDER`` so behavior stays
    stable across runs.
    """
    text = _extract_request_text(request)
    if not text or not text.strip():
        return CategoryDecision(
            category=CATEGORY_GENERAL,
            confidence=0.0,
            score=0,
            reasons=["empty request text"],
        )

    scores = {}
    reasons = []
    for category in CATEGORY_ORDER:
        pattern = CATEGORY_PATTERNS[category]
        matches = pattern.findall(text)
        # Score by DISTINCT normalized matches, not raw frequency. Repetition of
        # the same keyword (common in injected scaffolding) must not amplify the
        # score — otherwise prompt length becomes the routing signal.
        distinct = {
            (m if isinstance(m, str) else " ".join(m)).lower().strip()
            for m in matches
        }
        distinct.discard("")
        score = len(distinct)
        scores[category] = score
        if score:
            reasons.append(f"{category}={score}")

    best_category = CATEGORY_GENERAL
    best_score = 0
    for category in CATEGORY_ORDER:
        if scores[category] > best_score:
            best_score = scores[category]
            best_category = category

    if best_score >= CATEGORY_SCORE_THRESHOLD:
        category = best_category
    else:
        category = CATEGORY_GENERAL

    # Coarse deterministic confidence — NOT a real probability.
    # 0.0 = no signal, 0.5 = weak (single hit), 1.0 = confident (2+ hits).
    # This replaces the previous fake-precision score/10.0 float.
    if best_score >= 2:
        confidence = 1.0
    elif best_score == 1:
        confidence = 0.5
    else:
        confidence = 0.0

    # Phase 2: derive multi-domain + runner-up from the (already computed) scores.
    # multi_domain: >= 2 categories each clearing the acceptance threshold.
    qualifying = [c for c in CATEGORY_ORDER if scores.get(c, 0) >= CATEGORY_SCORE_THRESHOLD]
    multi_domain = len(qualifying) >= 2
    # runner_up: 2nd-highest scoring category, deterministic tie-break by CATEGORY_ORDER.
    # Iterate in CATEGORY_ORDER (already the tie-break order), rank by (score desc).
    ranked = sorted(
        (c for c in CATEGORY_ORDER if scores.get(c, 0) > 0),
        key=lambda c: (-scores[c], CATEGORY_ORDER.index(c)),
    )
    runner_up = ranked[1] if len(ranked) >= 2 else None

    return CategoryDecision(
        category=category,
        confidence=confidence,
        score=best_score,
        reasons=reasons or ["no strong category signals"],
        scores=dict(scores),
        runner_up=runner_up,
        multi_domain=multi_domain,
    )
