"""
GLM Parallel Tool Guard — router-level fix for GLM-5.x dropping tool args
when emitting multi-tool_use batches on the Anthropic wire format.

Injects tool_choice.disable_parallel_tool_use: true into the FINAL Anthropic
upstream payload (after normalize_to_anthropic) when the resolved target model
is GLM-family. Covers Claude Code CLI, OAC/Blacksand Code, and any
Anthropic-SDK client — none of which can rely on client-side fixes.
"""

from __future__ import annotations


def is_glm_model(model_id: str) -> bool:
    """Case-insensitive substring check for GLM-family model ids."""
    return "glm" in (model_id or "").lower()


def inject_disable_parallel_tool_use(payload: dict) -> dict:
    """Pure, non-mutating, idempotent surgery on an Anthropic-format payload.

    Adds ``tool_choice.disable_parallel_tool_use: True`` so GLM-5.x emits
    tool_use blocks sequentially instead of dropping arguments in parallel
    batches. Applies only when tools are present.
    """
    if not isinstance(payload, dict) or "tools" not in payload or not payload.get("tools"):
        return payload

    tc = payload.get("tool_choice")

    if isinstance(tc, dict):
        if tc.get("disable_parallel_tool_use") is True:
            return payload  # already flagged — idempotent
        return {**payload, "tool_choice": {**tc, "disable_parallel_tool_use": True}}

    # Missing / string / any other scalar: normalize to object form so the
    # flag has somewhere to live (mirrors Blacksand Code's serializer).
    if isinstance(tc, str) and tc and tc != "auto":
        return {**payload, "tool_choice": {"type": tc, "disable_parallel_tool_use": True}}
    return {**payload, "tool_choice": {"type": "auto", "disable_parallel_tool_use": True}}


def apply_glm_parallel_guard(payload: dict, target_model: str, tools_cfg: dict) -> dict:
    """Config- + model-gated entry point for the final Anthropic upstream payload."""
    if not tools_cfg.get("glm_no_parallel_tools", True):
        return payload  # config OFF → no-op (default ON)
    if not is_glm_model(target_model):
        return payload
    guarded = inject_disable_parallel_tool_use(payload)
    if guarded is not payload:
        print(f"[ParallelGuard] disabled parallel tool use for {target_model}", flush=True)
    return guarded
