"""
Middleware.efficiency — Opus-style turn efficiency prompts

Ports the low-risk, stateless pieces of the 9Router Opus Efficiency Middleware:
  - S1: Turn Consolidation prompt injection
  - S5: Tool-call batching correction

Safety contract:
  1. Pure request transformation only; no upstream retries or stateful loops.
  2. Skips models that are already strong at multi-action turns.
  3. Duplicate-safe: never injects the same prompt twice.
  4. Fail-open callers should catch exceptions and keep the original request.
"""

import re
from typing import List, Optional

from app.models import Message


CONSOLIDATION_PROMPT_AGGRESSIVE = """## EXECUTION RULES (MANDATORY — DO NOT IGNORE)
1. Make ALL independent tool calls in the SAME message (parallel batch).
2. Do NOT stop after a single tool call if more work remains in the task.
3. Complete the ENTIRE task in as few turns as possible — ideally ONE.
4. After receiving tool results, CONTINUE working — do NOT summarize and wait.
5. If you need to read 3 files → read ALL 3 in one tool-call batch.
6. If you need to edit 2 files → edit BOTH in one message.
7. NEVER say "let me know if you want me to continue" — just continue.
8. NEVER output partial work and ask for permission to finish.
9. NEVER ask "shall I proceed?" or "would you like me to..." — just DO it.
10. Plan your ENTIRE approach first, then execute ALL steps without pausing."""

CONSOLIDATION_PROMPT_MILD = """## EFFICIENCY GUIDANCE
- When multiple tool calls are independent, make them in parallel (same message).
- Complete the full task without unnecessary pauses between steps.
- After tool results, continue to the next step immediately."""

TOOL_BATCHING_PROMPT = """⚠️ EFFICIENCY: You are making tool calls ONE AT A TIME. Make ALL remaining tool calls in ONE parallel batch NOW.

## TOOL CALL EFFICIENCY (MANDATORY)
- Read multiple files: call read_file/view_file for ALL simultaneously
- Independent searches: batch ALL search calls in one message
- Independent edits (different files): submit ALL edits in one message
- MAXIMUM 2 tool-call rounds per task:
  Round 1: Gather ALL information (parallel reads/searches)
  Round 2: Make ALL changes (parallel edits/writes)
- NEVER: read A → respond → read B → respond → edit A → respond → edit B
- CORRECT: read A+B simultaneously → edit A+B simultaneously → DONE"""

_CONSOLIDATION_MARKER = "EXECUTION RULES (MANDATORY"
_MILD_MARKER = "EFFICIENCY GUIDANCE"
_TOOL_BATCHING_MARKER = "TOOL CALL EFFICIENCY (MANDATORY)"


def _is_opus(model_id: str) -> bool:
    return bool(re.search(r"opus|claude-opus", model_id, re.I))


def _is_kimi_thinking(model_id: str) -> bool:
    return bool(re.search(r"kimi.*thinking|k2.*thinking", model_id, re.I))


def classify_for_consolidation(model_id: str) -> Optional[str]:
    """Return the turn-efficiency prompt strength for a model, or None to skip."""
    model = model_id or ""

    # Already strong at multi-action turns; avoid instruction pollution.
    if _is_opus(model) or _is_kimi_thinking(model):
        return None

    if re.search(r"gpt-?[45]|(?:^|[/\-_])o[1-9](?:[\-_.]|$)", model, re.I):
        return "aggressive"
    if re.search(r"claude|sonnet|deepseek|ds-?v4|glm-?[45]|kimi|k2|minimax|(?:^|[/\-_])m3(?:[\-_.]|$)|mimo", model, re.I):
        return "aggressive"
    if re.search(r"gemini|qwen", model, re.I):
        return "mild"

    return None


def _content_text(message: Message) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                text = part.get("text")
            else:
                text = getattr(part, "text", None)
            if text:
                parts.append(str(text))
        return "\n".join(parts)
    return ""


def _has_prompt(messages: List[Message], marker: str) -> bool:
    return any(marker in _content_text(message) for message in messages)


def inject_turn_consolidation(messages: List[Message], model_id: str) -> List[Message]:
    """Inject Opus-style single-turn execution guidance when useful."""
    profile = classify_for_consolidation(model_id)
    if profile is None or not messages:
        return list(messages or [])

    marker = _MILD_MARKER if profile == "mild" else _CONSOLIDATION_MARKER
    if _has_prompt(messages, marker):
        return list(messages)

    prompt = CONSOLIDATION_PROMPT_MILD if profile == "mild" else CONSOLIDATION_PROMPT_AGGRESSIVE
    injection = Message(role="system", content=prompt)
    new_messages = list(messages)

    system_idx = next((idx for idx, message in enumerate(new_messages) if message.role == "system"), -1)
    if system_idx >= 0:
        new_messages.insert(system_idx + 1, injection)
    else:
        new_messages.insert(0, injection)

    return new_messages


def inject_tool_batching(messages: List[Message], model_id: str) -> List[Message]:
    """Inject a corrective prompt after repeated sequential single-tool calls."""
    if classify_for_consolidation(model_id) is None or not messages:
        return list(messages or [])
    if _has_prompt(messages, _TOOL_BATCHING_MARKER):
        return list(messages)

    recent_assistant = [message for message in messages if message.role == "assistant"][-4:]
    sequential_single_tools = sum(1 for message in recent_assistant if len(message.tool_calls or []) == 1)

    if sequential_single_tools < 2:
        return list(messages)

    new_messages = list(messages)
    new_messages.append(Message(role="system", content=TOOL_BATCHING_PROMPT))
    return new_messages
