"""
Labeled regression fixture for the D1-D4 complexity funnel.

Each case is (text, expected_tier, note) where note documents the routing
rationale. ~40 EN+VI cases across trivial/standard/deep.

Continuation cases requiring prior messages are in CONTINUATION_ROOTS dict.
"""

from app.middleware.task_complexity import COMPLEXITY_TRIVIAL, COMPLEXITY_STANDARD, COMPLEXITY_DEEP

CASES = [
    # ── Trivial (10) ───────────────────────────────────────────────────────────
    ("hi", COMPLEXITY_TRIVIAL, "bare greeting"),
    ("hello", COMPLEXITY_TRIVIAL, "hello"),
    ("thanks", COMPLEXITY_TRIVIAL, "thanks after essay -> trivial"),
    ("thank you", COMPLEXITY_TRIVIAL, "thank you"),
    ("ok", COMPLEXITY_TRIVIAL, "ok"),
    ("got it", COMPLEXITY_TRIVIAL, "acknowledgment"),
    ("bye", COMPLEXITY_TRIVIAL, "bye"),
    ("cảm ơn", COMPLEXITY_TRIVIAL, "VI thanks"),
    ("What is 2+2? Just the number.", COMPLEXITY_STANDARD, "tiny factual -> standard (no deep signals, not a greeting)"),
    ("yes", COMPLEXITY_TRIVIAL, "yes"),
    # ── Standard (16) ──────────────────────────────────────────────────────────
    (
        "Explain how JWT works",
        COMPLEXITY_STANDARD,
        "single analytical verb, no density/multi-domain -> standard",
    ),
    (
        "What is the capital of France?",
        COMPLEXITY_STANDARD,
        "simple factual Q -> standard (no deep signals)",
    ),
    (
        "summarize this 30000-word report",
        COMPLEXITY_STANDARD,
        "size != capability; low-reasoning head 'summarize' caps at standard",
    ),
    (
        "review this file",
        COMPLEXITY_STANDARD,
        "low-reasoning cap: review head verb -> standard",
    ),
    (
        "format the output as JSON",
        COMPLEXITY_STANDARD,
        "low-reasoning cap: format head verb -> standard",
    ),
    (
        "translate this to French",
        COMPLEXITY_STANDARD,
        "low-reasoning cap: translate head verb -> standard",
    ),
    (
        "convert the data to CSV",
        COMPLEXITY_STANDARD,
        "low-reasoning cap: convert head verb -> standard",
    ),
    (
        "lint this Python file for style issues",
        COMPLEXITY_STANDARD,
        "low-reasoning cap: lint head verb -> standard",
    ),
    (
        "rename the variables to follow PEP 8",
        COMPLEXITY_STANDARD,
        "low-reasoning cap: rename head verb -> standard",
    ),
    (
        "review and fix this code",
        COMPLEXITY_STANDARD,
        "low-reasoning cap: 'review' head verb -> standard despite companion 'fix'",
    ),
    (
        "scaffolding word comprehensive only",
        COMPLEXITY_STANDARD,
        "injection resistance: just 'comprehensive' with no depth -> standard",
    ),
    (
        "Write a Python function to sort a list",
        COMPLEXITY_STANDARD,
        "simple code request, no density/workflow depth -> standard",
    ),
    (
        "What is the difference between TCP and UDP?",
        COMPLEXITY_STANDARD,
        "analytical verb + domain density, but no structural density -> standard",
    ),
    (
        "Write a 400-word essay on climate change",
        COMPLEXITY_STANDARD,
        "D1 scope fired (400 words) but D3 density not met (text <120 chars) -> standard",
    ),
    (
        "comprehensive analysis and evaluation of multiple perspectives with supporting evidence",
        COMPLEXITY_STANDARD,
        "keyword salad without connectors/delimiter chain -> standard (not deep)",
    ),
    (
        "Analyze the ethics of utilitarianism vs deontology from multiple perspectives and cite sources",
        COMPLEXITY_STANDARD,
        "analytical + multi-perspective but no D3 density (text under 120 chars) -> standard",
    ),
    # ── Deep (14) ──────────────────────────────────────────────────────────────
    (
        "design a billing system with 6 requirements, avoid X, in style Y, and document each module, and incorporate feedback from stakeholders",
        COMPLEXITY_DEEP,
        "D1 scope (6 deliverables as delimiter chain >=3 items >3 words) + D3 density from connectors -> deep",
    ),
    (
        "design a billing system: 6 requirements, avoid X, style Y, document each module, add tests, and write API docs",
        COMPLEXITY_DEEP,
        "D1 (delimiter chain) + D3 (multi-segment delimiter + connector 'and') -> deep",
    ),
    (
        "Write a comprehensive 2000-word report with detailed analysis, comparison of multiple approaches, and evidence-based recommendations",
        COMPLEXITY_DEEP,
        "D1 saturated (2000 words >=800) -> deep via saturation",
    ),
    (
        "debug and refactor the auth module, integrate end-to-end",
        COMPLEXITY_DEEP,
        "D4 saturated: debug+refactor+integrate = 3 distinct workflow verbs",
    ),
    (
        "Debug the login module at app/auth/login.py line 42. Refactor the middleware and integrate end-to-end testing. Also audit the models for security.",
        COMPLEXITY_DEEP,
        "D4 saturated (debug+refactor+integrate+audit = 4 distinct) + code markers -> deep",
    ),
    (
        "Analyze and compare the ethics of utilitarianism vs deontology from multiple perspectives. Write a thorough report discussing pros and cons, cite sources from the literature, and reason from first principles about the underlying framework.",
        COMPLEXITY_DEEP,
        "D1 (longform noun 'report') + D3 density (connectors, delimiter chain, 120+ chars) -> deep",
    ),
    (
        "Conduct a comprehensive literature review. Cite sources, gather evidence and statistics, and synthesize the findings with rigorous methodology. Write a detailed analysis of the results.",
        COMPLEXITY_DEEP,
        "D1 (longform noun 'review') + D3 (connectors, delimiter chain, 120+ chars) -> deep",
    ),
    (
        "I need to debug and refactor the entire authentication module. The issue is in app/auth/login.py at line 42 where the token validation fails. Please review and fix the traceback. Also audit app/auth/middleware.py and app/auth/models.py and integrate the fix end-to-end with regression tests.",
        COMPLEXITY_DEEP,
        "D4 saturated + code markers + traceback + multi-file -> deep",
    ),
    (
        "Implement a complete REST API with 6 features: JWT-based authentication, PostgreSQL database integration, Redis rate limiting, request logging middleware, comprehensive error handling with custom exceptions, and request payload validation",
        COMPLEXITY_DEEP,
        "D1 scope (6 features >=3 sections) + D3 delimiter chain (6 items >3 words, 220+ chars) -> deep",
    ),
    (
        "migrate and integrate the database, refactor the query layer, and deploy the updated schema",
        COMPLEXITY_DEEP,
        "D4 saturated: migrate+integrate+refactor+deploy = >=3 distinct workflow verbs",
    ),
    (
        "I need a comprehensive design document with 5 sections covering architecture, security, scalability, deployment, monitoring, and testing. Include detailed API specs and performance benchmarks.",
        COMPLEXITY_DEEP,
        "D1 scope (5 sections >=3 sections) + D3 (delimiter chain >=3 items >3 words, 120+ chars) -> deep",
    ),
    (
        "Viết báo cáo thiết kế database multi-tenant với phân vùng dữ liệu, bảo mật hàng-level dựa trên RBAC, audit log cho mọi thao tác, soft delete để tránh mất dữ liệu, và tích hợp thanh toán qua Stripe",
        COMPLEXITY_DEEP,
        "VI deep: D1 longform noun 'báo cáo' + D3 delimiter chain (5 items >3 words, 140+ chars) + connector 'và'",
    ),
    (
        "Hay viet bao cao phan tich va so sanh triet hoc dao duc tu nhieu goc nhin. Xet xem uu va nhuoc diem, trich dan nguon, va luan tu goc re cua van de.",
        COMPLEXITY_DEEP,
        "VI non-diacritic: D1 longform noun 'bao cao' + D3 connectors + delimiter chain -> deep",
    ),
    (
        "Phân tích và so sánh triết học đạo đức từ nhiều góc nhìn. Viết báo cáo chi tiết về ưu và nhược điểm, trích dẫn nguồn, và luận từ gốc rễ của vấn đề.",
        COMPLEXITY_DEEP,
        "VI diacritic: D1 longform noun 'báo cáo' + D3 connectors + delimiter chain -> deep",
    ),
]

# Continuation cases: current_text -> (root_text, expected_tier)
# These need prior messages to trigger root-turn inheritance.
CONTINUATION_CASES = {
    "make it longer": (
        "Write a 5000-word thesis on quantum computing with detailed explanations of entanglement, superposition, and quantum gates",
        COMPLEXITY_DEEP,
        "continuation after deep root -> inherits deep (floor)",
    ),
    "now add auth": (
        "design a billing system: 6 requirements, avoid X, style Y, document each module, and add tests",
        COMPLEXITY_DEEP,
        "continuation after deep root -> inherits deep",
    ),
}
