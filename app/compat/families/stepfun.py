"""
StepFun step-5 family contract / Hợp đồng gia đình StepFun step-5.

Official StepFun spec: `step-5-preview` Chat Completions API (OpenAI wire)
accepts a top-level `reasoning_effort` with vocabulary `low|medium|high`.
The Messages / anthropic-shaped API uses `output_config.effort` with the
SAME vocabulary. Both wires accept exactly low|medium|high — xhigh/max are
NOT documented, so they collapse to `high` (the highest documented level)
rather than being sent as an upstream-rejected value.

Pattern scope: `step-?5` matches step-5 / step5 MODEL ids only. The
provider segment `stepfun` alone must NOT match — the step-3.x-flash ids
served under stepfun providers were never verified to accept
reasoning_effort, and legacy emitted nothing for them (parity locked in
test_family_divergences.py::test_step3_flash_models_remain_legacy_untouched).

Local normalization map (NO shared coerce helper exists — verified):
  - low/medium/high pass through as-is
  - xhigh/max -> high      (above the documented ceiling)
  - budget style (32k etc.) -> high (treat any budget as "max effort")
  - enable/adaptive/unknown -> high (CLAMPED, never passed through — the
    vocabulary IS documented, and a strict gateway 400s unknown values;
    house precedent is clamping: Grok test_grok_unknown_effort_defaults_
    to_high. Kat-coder is the only passthrough family, and only because
    its vocab is undocumented.)
  - empty -> no write (the resolver's effort gate already drops it)

Wire branching mirrors openai.py L40-60 (gpt-5 anthropic-strip precedent):
  - anthropic wire -> {output_config: {effort: lvl}, reasoning_effort: None}
  - openai wire -> {reasoning_effort: lvl, output_config: None}
  - gemini / openai-responses -> return payload unchanged (undocumented shapes)

The `None` values are explicit attributed removals: a strict gateway (DeepSeek
2026-08-20 evidence) 400s on unknown top-level fields, so an inherited
container from another contract must be stripped, not left stale.

---
Spec chính thức StepFun: `step-5-preview` qua Chat Completions API (OpenAI
wire) nhận `reasoning_effort` top-level với bộ từ vựng `low|medium|high`.
API Messages / dạng anthropic dùng `output_config.effort` cùng bộ từ vựng.
Cả hai wire chỉ chấp nhận low|medium|high; xhigh/max không được tài liệu hóa
nên gập về `high` (mức cao nhất đã ghi nhận) thay vì gửi giá trị bị upstream
từ chối.

Bản đồ chuẩn hóa nội bộ (KHÔNG có helper coerce dùng chung — đã kiểm chứng):
enable/adaptive/giá trị lạ gập về `high` (từ vựng ĐÃ tài liệu hóa, gateway
nghiêm ngặt trả 400 — theo tiền lệ clamp của Grok). Pattern chỉ khớp id
model `step-?5`; riêng phân đoạn provider `stepfun` không khớp, các id
step-3.x-flash giữ nguyên hành vi legacy (không phát reasoning key nào).
Phân nhánh wire theo tiền lệ openai.py L40-60. Giá trị `None` là xóa có ghi
nguồn: gateway nghiêm ngặt (bằng chứng DeepSeek 2026-08-20) trả 400 với các
trường top-level lạ, nên container thừa kế từ contract khác phải bị strip.
"""
from __future__ import annotations

import re
from typing import Any, Dict

from app.compat.families._base import Contract, Provenance, ThinkingContext

SOURCE = "families/stepfun.py"

# Documented vocabulary: low|medium|high. xhigh/max collapse to `high`
# (the highest documented level). Budget style also maps to high.
# Bộ từ vựng đã tài liệu hóa: low|medium|high. xhigh/max gập về `high`.
_EFFORT_MAP = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "high",
    "max": "high",
}
_BUDGET_RE = re.compile(r"^\d+[km]?$")


def _normalize_effort(raw: Any) -> str | None:
    """Normalize an inbound effort value into StepFun's documented vocabulary.

    low/medium/high pass through; xhigh/max -> high; budget style (32k, 8m...)
    -> high; EVERYTHING else (enable, adaptive, unknown words like ultra)
    clamps to `high`. The vocabulary is documented low|medium|high and a
    strict gateway 400s unknown values, so clamping — not passthrough — is
    correct here (house precedent: Grok test_grok_unknown_effort_defaults_
    to_high; Kat-coder passes through only because its vocab is
    undocumented). Empty/None -> None (the resolver's effort gate already
    dropped it).
    """
    v = str(raw or "").strip().lower()
    if not v:
        return None
    if v in _EFFORT_MAP:
        return _EFFORT_MAP[v]
    if _BUDGET_RE.match(v):
        return "high"          # budget style -> highest documented
    # enable/adaptive/unknown: clamp to the documented ceiling rather than
    # send a value the gateway rejects with a 400.
    return "high"


def _apply(
    payload: Dict[str, Any],
    ctx: ThinkingContext,
    prov: Provenance,
    contract: Contract,
) -> Dict[str, Any]:
    lvl = _normalize_effort(ctx.effort)
    if not lvl:
        return payload

    if ctx.wire_format == "anthropic":
        # Messages/anthropic-shaped wire: output_config.effort is the slot.
        # Strip any inherited OpenAI reasoning_effort (strict gateways 400
        # on unknown top-level fields — DeepSeek 2026-08-20 evidence).
        return prov.apply(
            payload,
            contract,
            "output_config_effort",
            {"output_config": {"effort": lvl}, "reasoning_effort": None},
        )
    elif ctx.wire_format == "openai":
        # Chat Completions wire: top-level reasoning_effort. Strip any
        # inherited output_config container for the same strict-gateway reason.
        return prov.apply(
            payload,
            contract,
            "reasoning_effort_openai",
            {"reasoning_effort": lvl, "output_config": None},
        )
    # gemini / openai-responses: undocumented shapes — do not guess.
    return payload


CONTRACTS = [
    Contract(
        id="step-5",
        source=SOURCE,
        priority=58,
        # step-5 / step5 model ids ONLY. The `stepfun` provider segment must
        # not match: step-3.x-flash ids are out of spec (legacy parity).
        pattern=r"step-?5",
        apply=_apply,
    ),
]
