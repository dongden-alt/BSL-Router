"""
BSL Router — Legacy Engine B reference implementation.

VERBATIM extraction of the regex cascade that lived at
app/main.py:4283-4535 before the family-contract refactor.

This exists ONLY as a characterization oracle. The test suite asserts
that the new family registry produces byte-identical output to this
function for every model in config.yaml, which is what makes the
refactor provably behavior-preserving.

Do NOT fix bugs here. Bugs found in this cascade are documented as
intentional divergences in the family modules and locked by a dedicated
test. Changing this file silently weakens the safety net.

Extraction notes (faithfulness):
  - `h` and `i` are the shared output_config / reasoning dicts read back
    off the payload exactly as the original did, so pre-existing values
    are preserved with the same aliasing semantics.
  - The `is_*` flags keep their original definitions AND their original
    evaluation order, since the cascade's correctness depended on it.
  - The Kimi-K3 and Qwen sanitize blocks run unconditionally after the
    cascade, exactly as in the original.
  - max_tokens / thinking-squeeze / Kiro logic is NOT included: those
    concerns are outside the thinking-resolution boundary and remain in
    main.py untouched.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional

from app.compat.families._effort import (
    apply_gpt5_reasoning_controls,
    claude_modern_thinking,
    coerce_effort,
)


def legacy_apply_thinking(
    upstream_payload: Dict[str, Any],
    f_val: str,
    thinking_suffix: str,
    reasoning_mode: Optional[str] = None,
    reasoning_context: Optional[str] = None,
) -> Dict[str, Any]:
    """Reproduce main.py's Engine B thinking cascade exactly.

    Args:
        upstream_payload: payload dict (mutated and returned, as original)
        f_val: "provider/model" lowercased
        thinking_suffix: configured thinking value, lowercased
        reasoning_mode: optional GPT-5.6 / Fable-Mythos mode
        reasoning_context: optional GPT-5.6 reasoning context
    """
    f_val = (f_val or "").lower()
    thinking_suffix = str(thinking_suffix or "auto").lower()

    # ── flag definitions (original order preserved) ──
    _claude_match = re.search(r'(claude|opus|sonnet).*?(\d+)(?:[.-](\d+))?', f_val)
    _claude_major = int(_claude_match.group(2)) if _claude_match else 0
    is_claude_next = bool(re.search(r'fable|mythos', f_val))
    is_claude_legacy = bool(_claude_match) and _claude_major == 3 and not is_claude_next
    is_claude_modern = bool(_claude_match) and _claude_major >= 4 and not is_claude_next
    is_gpt_5 = bool(re.search(r'gpt-?5', f_val))
    is_grok = bool(re.search(r'grok|xai', f_val)) and not bool(re.search(r'non-reasoning', f_val))
    is_deepseek_v4 = bool(re.search(r'deepseek-v4', f_val))
    is_kimi_k3 = bool(re.search(r'kimi-k3', f_val))
    is_kimi = bool(re.search(r'kimi|k2\.|moonshot', f_val)) and not is_kimi_k3
    is_qwen = bool(re.search(r'qwen', f_val))
    is_chinese_m = bool(re.search(r'glm-|kimi|minimax|mimo|qwen', f_val)) and not is_kimi_k3
    is_gemini_3 = bool(re.search(r'gemini.*3', f_val))
    is_gemini = bool(re.search(r'gemini', f_val))
    is_openrouter = bool(re.search(r'openrouter|open-router', f_val))

    _has_explicit_gpt5_metadata = is_gpt_5 and (
        reasoning_mode in ("standard", "pro")
        or reasoning_context in ("auto", "current_turn", "all_turns")
    )

    if (thinking_suffix and thinking_suffix not in ("auto", "none", "off", "")) or _has_explicit_gpt5_metadata:
        h = upstream_payload.get("output_config", {})
        if not isinstance(h, dict):
            h = {}
        i = upstream_payload.get("reasoning", {})
        if not isinstance(i, dict):
            i = {}

        if is_gpt_5:
            upstream_payload = apply_gpt5_reasoning_controls(
                upstream_payload, thinking_suffix, reasoning_mode, reasoning_context
            )
        elif is_openrouter:
            i["effort"] = coerce_effort(thinking_suffix)
            i["exclude"] = bool(i.get("exclude", False))
            upstream_payload["reasoning"] = i
        elif is_gemini_3:
            upstream_payload["thinkingLevel"] = thinking_suffix
            upstream_payload["includeThoughts"] = True
        elif is_gemini:
            b = {"16k": 16384, "32k": 32768, "64k": 65536, "128k": 131072}.get(thinking_suffix, 0)
            if b > 0:
                upstream_payload["thinking_config"] = {"budget_tokens": b}
        elif is_claude_modern:
            _think, _effort_level, _budget = claude_modern_thinking(f_val, thinking_suffix)
            upstream_payload["thinking"] = _think
            if _budget is not None:
                upstream_payload["max_tokens"] = max(
                    int(upstream_payload.get("max_tokens", 0) or 0), _budget + 32768
                )
                upstream_payload.pop("output_config", None)
            else:
                h["effort"] = _effort_level
                upstream_payload["output_config"] = h
        elif is_claude_next:
            _mode = reasoning_mode if reasoning_mode in ("adaptive", "enabled") else "adaptive"
            upstream_payload["thinking"] = {"type": _mode}
            h["effort"] = coerce_effort(thinking_suffix)
            upstream_payload["output_config"] = h
        elif is_claude_legacy:
            b = 0
            if thinking_suffix == "16k":
                b = 16384
            elif thinking_suffix == "32k":
                b = 32768
            elif thinking_suffix == "64k":
                b = 65536
            elif thinking_suffix == "128k":
                b = 131072
            if b > 0:
                upstream_payload["thinking"] = {"type": "enabled", "budget_tokens": b}
                upstream_payload["max_tokens"] = max(
                    int(upstream_payload.get("max_tokens", 0) or 0), b + 32768
                )
            else:
                upstream_payload["thinking"] = {"type": "adaptive"}
        elif is_grok:
            upstream_payload["reasoning_effort"] = thinking_suffix
        elif is_deepseek_v4:
            upstream_payload["thinking"] = {"type": "enabled"}
            upstream_payload["reasoning_effort"] = thinking_suffix
            h["effort"] = coerce_effort(thinking_suffix)
            upstream_payload["output_config"] = h
        elif is_kimi_k3:
            upstream_payload["reasoning_effort"] = (
                thinking_suffix if thinking_suffix in ("low", "medium", "high", "max") else "max"
            )
            upstream_payload.pop("thinking", None)
        elif is_qwen:
            upstream_payload["reasoning_effort"] = (
                thinking_suffix if thinking_suffix in ("low", "medium", "high", "max") else "max"
            )
            upstream_payload.pop("thinking", None)
            upstream_payload.pop("output_config", None)
            upstream_payload.pop("reasoning", None)
        elif is_kimi:
            upstream_payload["enable_thinking"] = True
            upstream_payload.pop("thinking", None)
        elif is_chinese_m:
            if re.search(r'glm-5\.2', f_val) and thinking_suffix in ("low", "medium", "high", "max"):
                upstream_payload["thinking"] = {"type": "enabled"}
                upstream_payload["reasoning_effort"] = thinking_suffix
            elif thinking_suffix == "enable":
                upstream_payload["thinking"] = {"type": "enabled"}
            elif thinking_suffix == "adaptive":
                upstream_payload["thinking"] = {"type": "adaptive"}
            else:
                upstream_payload["thinking"] = {"type": "enabled"}
                h["effort"] = coerce_effort(thinking_suffix)
                upstream_payload["output_config"] = h

    # ── unconditional sanitization (runs even when thinking is off) ──
    if is_kimi_k3:
        upstream_payload.pop("temperature", None)
        upstream_payload.pop("top_p", None)
        upstream_payload.pop("presence_penalty", None)
        upstream_payload.pop("frequency_penalty", None)
        upstream_payload.pop("n", None)
        upstream_payload.pop("thinking", None)
        if "reasoning_effort" not in upstream_payload:
            upstream_payload["reasoning_effort"] = (
                thinking_suffix if thinking_suffix in ("low", "medium", "high", "max") else "max"
            )

    if is_qwen:
        upstream_payload.pop("temperature", None)
        upstream_payload.pop("top_p", None)
        upstream_payload.pop("presence_penalty", None)
        upstream_payload.pop("frequency_penalty", None)
        upstream_payload.pop("n", None)
        upstream_payload.pop("thinking", None)
        upstream_payload.pop("output_config", None)
        upstream_payload.pop("reasoning", None)
        if "reasoning_effort" not in upstream_payload:
            upstream_payload["reasoning_effort"] = (
                thinking_suffix if thinking_suffix in ("low", "medium", "high", "max") else "max"
            )

    return upstream_payload
