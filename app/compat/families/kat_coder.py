"""
KwaiPilot Kat-Coder family contract / Hợp đồng gia đình KwaiPilot Kat-Coder.

Kat-Coder Pro V2.5 is an agentic coding model from Kuaishou's KwaiKAT
team, served via OpenRouter, Blackbox AI, and Atlas Cloud. It supports
reasoning (confirmed on Blackbox AI model page) but the specific
`reasoning_effort` vocabulary is not publicly documented.

Strategy: PASS THROUGH. Send the operator's `reasoning_effort` value
as-is without coercion. If the upstream rejects an unsupported value,
the operator will see the error and can configure accordingly.

This is safer than guessing a vocabulary (the earlier K3-lineage claim
was unverified and has been removed).

Kat-Coder Pro V2.5 là model coding agentic từ team KwaiKAT của Kuaishou,
được phục vụ qua OpenRouter, Blackbox AI và Atlas Cloud. Model hỗ trợ
reasoning (xác nhận trên trang Blackbox AI) nhưng bộ từ vựng
`reasoning_effort` cụ thể không được tài liệu hóa công khai.

Chiến lược: PASS THROUGH — gửi giá trị `reasoning_effort` nguyên văn
không chuyển đổi. Nếu upstream từ chối giá trị không hỗ trợ, operator
sẽ thấy lỗi và có thể cấu hình lại.
"""
from __future__ import annotations

from typing import Any, Dict

from app.compat.families._base import Contract, Provenance, ThinkingContext

SOURCE = "families/kat_coder.py"


def _apply(
    payload: Dict[str, Any],
    ctx: ThinkingContext,
    prov: Provenance,
    contract: Contract,
) -> Dict[str, Any]:
    # Pass through: no coercion. Kat-Coder's effort vocabulary is
    # undocumented, so we trust the operator's value and let the
    # upstream validate.
    # Pass through: không chuyển đổi. Bộ từ vựng effort của Kat-Coder
    # không được tài liệu hóa, gửi nguyên văn giá trị operator chọn.
    return prov.apply(
        payload,
        contract,
        "reasoning_effort_passthrough",
        {"reasoning_effort": ctx.effort},
    )


CONTRACTS = [
    Contract(
        id="kat-coder",
        source=SOURCE,
        priority=52,
        pattern=r"kat-coder|kwaipilot",
        apply=_apply,
    ),
]
