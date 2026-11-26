"""Zero-overhead Unified 4-Plane Modular Switchboard for Antigravity / Blacksand Code & Claude Code.

Targets (independently toggleable from one unified board):
  - `apply_antigravity`: Antigravity / Blacksand Code (`/v1internal:*`, `:listExperiments`, `~/.gemini/antigravity/hooks.json`)
  - `apply_claude_code`: Claude Code Desktop / CLI (`/v1/messages`, `/anthropic/v1/messages`, `~/.claude/settings.json`)

Planes:
  - Plane 1: Lifecycle Hook Interceptors (lossless disk stash/restore + PreInvocation/UserPromptSubmit wire filter)
  - Plane 2: Discovery & Experiment Gates (Antigravity `:listExperiments` wire flags + Claude Code `skillOverrides`)
  - Plane 3: System Prompt XML & Reminder Filters (0.0ms fast-path wire sanitizer)
  - Plane 4: Built-in Tool Declarations Filter (0.0ms fast-path wire filter)

Performance contract:
  - When target is unchecked OR all Plane 1/3/4 wire flags are True (Full Studio default),
    `requires_wire_filtering()` returns False in O(1) with zero JSON parsing, zero regex evaluation,
    and zero allocations.
  - On mapped `/v1internal:*` and `/v1/messages` requests, filtering mutates the already-parsed
    request dict in place (zero extra `json.loads`/`json.dumps` round-trip).
  - Never modifies `app.asar` or renames files in `~/.gemini/` (zero file-watcher storms).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Mapping

# ─── Schema & Defaults (All ON = 0.0ms Fast-Path) ─────────────────────────────

TARGET_KEYS: tuple[str, ...] = ("apply_antigravity", "apply_claude_code")

DEFAULT_ANTIGRAVITY_LS_COMPONENTS: dict[str, bool] = {
    # Target Apps
    "apply_antigravity": True,
    "apply_claude_code": True,
    # Plane 1: Lifecycle Hook Interceptors
    "hook_session_start": True,
    "hook_pre_invocation": True,
    "hook_pre_tool_use": True,
    "hook_post_tool_use": True,
    "hook_stop": True,
    # Plane 2: Discovery & Experiment Gates
    "custom_rules": True,
    "custom_skills": True,
    "custom_plugins": True,
    "custom_subagents": True,
    "enable_mcp_sidecars": True,
    # Plane 3: System Prompt XML & Reminder Filters (0ms Wire Sanitizer)
    "prompt_guidelines": True,
    "prompt_user_rules": True,
    "prompt_skills_catalog": True,
    "prompt_mcp_servers": True,
    "prompt_subagents_section": True,
    "prompt_artifacts_section": True,
    "prompt_slash_and_style": True,
    # Plane 4: Built-in Tool Declarations Filter (0ms Wire Filter)
    "tool_core_execution": True,
    "tool_ask_question": True,
    "tool_subagents_swarm": True,
    "tool_mcp_bridge": True,
    "tool_web_search": True,
    "tool_generate_image": True,
}

# Presets specify the 4-plane switches only (never clobber user's target checkboxes)
LS_COMPONENTS_PRESETS: dict[str, dict[str, bool]] = {
    "lean_coder": {
        # Plane 1
        "hook_session_start": True,
        "hook_pre_invocation": False,
        "hook_pre_tool_use": True,
        "hook_post_tool_use": True,
        "hook_stop": False,
        # Plane 2
        "custom_rules": True,
        "custom_skills": False,
        "custom_plugins": True,
        "custom_subagents": True,
        "enable_mcp_sidecars": True,
        # Plane 3
        "prompt_guidelines": False,
        "prompt_user_rules": True,
        "prompt_skills_catalog": False,
        "prompt_mcp_servers": True,
        "prompt_subagents_section": True,
        "prompt_artifacts_section": False,
        "prompt_slash_and_style": False,
        # Plane 4
        "tool_core_execution": True,
        "tool_ask_question": False,
        "tool_subagents_swarm": True,
        "tool_mcp_bridge": True,
        "tool_web_search": True,
        "tool_generate_image": False,
    },
    "raw_uncensored": {
        # Plane 1
        "hook_session_start": False,
        "hook_pre_invocation": False,
        "hook_pre_tool_use": False,
        "hook_post_tool_use": False,
        "hook_stop": False,
        # Plane 2
        "custom_rules": False,
        "custom_skills": False,
        "custom_plugins": False,
        "custom_subagents": False,
        "enable_mcp_sidecars": False,
        # Plane 3
        "prompt_guidelines": False,
        "prompt_user_rules": False,
        "prompt_skills_catalog": False,
        "prompt_mcp_servers": False,
        "prompt_subagents_section": False,
        "prompt_artifacts_section": False,
        "prompt_slash_and_style": False,
        # Plane 4
        "tool_core_execution": True,
        "tool_ask_question": False,
        "tool_subagents_swarm": False,
        "tool_mcp_bridge": False,
        "tool_web_search": True,
        "tool_generate_image": False,
    },
    "full_studio": {
        k: v for k, v in DEFAULT_ANTIGRAVITY_LS_COMPONENTS.items() if k not in TARGET_KEYS
    },
}

_WIRE_KEYS_ANTIGRAVITY: tuple[str, ...] = (
    "hook_pre_invocation",
    "prompt_guidelines",
    "prompt_user_rules",
    "prompt_skills_catalog",
    "prompt_mcp_servers",
    "prompt_subagents_section",
    "prompt_artifacts_section",
    "prompt_slash_and_style",
    "tool_core_execution",
    "tool_ask_question",
    "tool_subagents_swarm",
    "tool_mcp_bridge",
    "tool_web_search",
    "tool_generate_image",
)

_WIRE_KEYS_CLAUDE_CODE: tuple[str, ...] = (
    "hook_pre_invocation",
    "custom_rules",
    "custom_skills",
    "prompt_user_rules",
    "prompt_skills_catalog",
    "tool_core_execution",
    "tool_ask_question",
    "tool_subagents_swarm",
    "tool_mcp_bridge",
    "tool_web_search",
)

_WIRE_KEYS: tuple[str, ...] = _WIRE_KEYS_ANTIGRAVITY

_EXPERIMENT_KEYS: tuple[str, ...] = (
    "hook_session_start",
    "hook_pre_invocation",
    "hook_pre_tool_use",
    "hook_post_tool_use",
    "hook_stop",
    "custom_rules",
    "custom_skills",
    "custom_plugins",
    "custom_subagents",
    "enable_mcp_sidecars",
    "tool_subagents_swarm",
    "tool_mcp_bridge",
)

# Precompiled regexes (zero compile cost on the hot path)
_XML_SECTION_TAGS: dict[str, tuple[str, ...]] = {
    "prompt_guidelines": ("guidelines", "cyber_policy", "refusal_policy"),
    "prompt_user_rules": ("user_rules",),
    "prompt_skills_catalog": ("skills",),
    "prompt_mcp_servers": ("mcp_servers",),
    "prompt_subagents_section": ("subagents", "messaging"),
    "prompt_artifacts_section": ("artifacts",),
    "prompt_slash_and_style": ("slash_commands", "communication_style"),
}

_TAG_PATTERNS: dict[str, re.Pattern[str]] = {
    tag: re.compile(rf"<{tag}>\s*.*?\s*</{tag}>\s*", re.IGNORECASE | re.DOTALL)
    for tags in _XML_SECTION_TAGS.values()
    for tag in tags
}

_HOOK_META_RE: re.Pattern[str] = re.compile(
    r"(?:\[BSL-ROUTING-METADATA\].*?\[/BSL-ROUTING-METADATA\]\s*"
    r"|<bsl_Mode_Payload>.*?</bsl_Mode_Payload>\s*"
    r"|<bsl_Completion_Contract[^>]*/>\s*)+",
    re.IGNORECASE | re.DOTALL,
)

# Claude Code `<system-reminder>` targeted blocks (selective, never nukes unrelated system reminders)
_CC_REMINDER_BLOCK_RE: re.Pattern[str] = re.compile(
    r"<system-reminder>\s*(.*?)\s*</system-reminder>\s*",
    re.IGNORECASE | re.DOTALL,
)

# Tool declaration sets — Antigravity / Blacksand Code + Claude Code Desktop / CLI
_CORE_EXECUTION_TOOLS: frozenset[str] = frozenset({
    # Antigravity / Blacksand Code / Lite
    "read_file",
    "write_file",
    "edit_file",
    "run_command",
    "manage_task",
    "view_file",
    "view_file_outline",
    "view_code_item",
    "write_to_file",
    "replace_file_content",
    "multi_replace_file_content",
    "command_status",
    "send_command_input",
    "find_by_name",
    "grep_search",
    "list_dir",
    # Claude Code
    "Read",
    "Write",
    "Edit",
    "MultiEdit",
    "NotebookEdit",
    "Bash",
    "Glob",
    "Grep",
    "LS",
})

_ASK_QUESTION_TOOLS: frozenset[str] = frozenset({
    "ask_question",
    "AskUserQuestion",
})

_SUBAGENT_SWARM_TOOLS: frozenset[str] = frozenset({
    # Antigravity / Blacksand Code / Lite
    "invoke_subagent",
    "define_subagent",
    "manage_subagents",
    "send_message",
    "swarm",
    "schedule",
    "browser_subagent",
    # Claude Code
    "Agent",
    "Task",
    "TaskCreate",
    "TaskUpdate",
    "TaskOutput",
    "TaskStop",
    "TaskList",
    "TaskGet",
    "SendMessage",
    "TeamCreate",
    "TeamDelete",
    "CronCreate",
    "CronDelete",
    "CronList",
    "RemoteTrigger",
})

_MCP_BRIDGE_TOOLS: frozenset[str] = frozenset({
    # Antigravity / Lite
    "call_mcp_tool",
    "list_resources",
    "read_resource",
    "mcp_list_resources",
    "mcp_read_resource",
    # Claude Code
    "ToolSearch",
    "ListMcpResourcesTool",
    "ReadMcpResourceTool",
})

_WEB_SEARCH_TOOLS: frozenset[str] = frozenset({
    "search_web",
    "web_search",
    "read_url_content",
    "WebSearch",
    "WebFetch",
})

_HOOK_KEY_MAP_ANTIGRAVITY: tuple[tuple[str, str], ...] = (
    ("hook_session_start", "SessionStart"),
    ("hook_pre_invocation", "PreInvocation"),
    ("hook_pre_tool_use", "PreToolUse"),
    ("hook_post_tool_use", "PostToolUse"),
    ("hook_stop", "Stop"),
)

_HOOK_KEY_MAP_CLAUDE_CODE: tuple[tuple[str, str], ...] = (
    ("hook_session_start", "SessionStart"),
    ("hook_pre_invocation", "UserPromptSubmit"),
    ("hook_pre_tool_use", "PreToolUse"),
    ("hook_post_tool_use", "PostToolUse"),
    ("hook_stop", "Stop"),
)


# ─── Validation & Fast-Path Checks ────────────────────────────────────────────


def validate_ls_components_config(raw: Any, existing: Mapping[str, Any] | None = None) -> dict[str, bool]:
    """Return a clean, schema-complete dict of all target & 4-plane booleans."""
    if raw is None:
        return dict(DEFAULT_ANTIGRAVITY_LS_COMPONENTS)
    if not isinstance(raw, Mapping):
        raise ValueError("antigravity_integration.ls_components must be an object.")
    preset = raw.get("preset")
    base = dict(DEFAULT_ANTIGRAVITY_LS_COMPONENTS)
    if isinstance(existing, Mapping):
        for tk in TARGET_KEYS:
            if tk in existing and existing[tk] is not None:
                base[tk] = bool(existing[tk])
    if isinstance(preset, str) and preset.strip().lower() in LS_COMPONENTS_PRESETS:
        base.update(LS_COMPONENTS_PRESETS[preset.strip().lower()])
    out: dict[str, bool] = {}
    for key, default_val in base.items():
        if key in raw and raw[key] is not None:
            out[key] = bool(raw[key])
        else:
            out[key] = default_val
    return out


def get_ls_components_from_config(config_data: Any) -> dict[str, bool]:
    """Fast O(1) lookup from the in-memory runtime config dict."""
    if not isinstance(config_data, Mapping):
        return DEFAULT_ANTIGRAVITY_LS_COMPONENTS
    ag = config_data.get("antigravity_integration")
    if not isinstance(ag, Mapping):
        return DEFAULT_ANTIGRAVITY_LS_COMPONENTS
    lsc = ag.get("ls_components")
    if not isinstance(lsc, Mapping):
        return DEFAULT_ANTIGRAVITY_LS_COMPONENTS
    return lsc  # type: ignore[return-value]


def requires_wire_filtering(cfg: Mapping[str, Any] | None, target: str = "antigravity") -> bool:
    """O(1) fast-path gate: False when target app is unchecked OR all applicable wire flags are ON."""
    if not cfg:
        return False
    is_cc = target == "claude_code"
    target_key = "apply_claude_code" if is_cc else "apply_antigravity"
    if cfg.get(target_key, True) is False:
        return False
    keys = _WIRE_KEYS_CLAUDE_CODE if is_cc else _WIRE_KEYS_ANTIGRAVITY
    for k in keys:
        if cfg.get(k, True) is False:
            return True
    return False


def requires_experiment_filtering(cfg: Mapping[str, Any] | None) -> bool:
    """O(1) fast-path gate for Antigravity `/v1internal:listExperiments`."""
    if not cfg or cfg.get("apply_antigravity", True) is False:
        return False
    for k in _EXPERIMENT_KEYS:
        if cfg.get(k, True) is False:
            return True
    return False


# ─── Plane 1 + Plane 3 + Plane 4 Live Wire Filters ────────────────────────────


def _filter_system_prompt_xml_sections(text: str, cfg: Mapping[str, Any]) -> str | None:
    tags_to_strip: list[str] = []
    for key, tags in _XML_SECTION_TAGS.items():
        if cfg.get(key, True) is False:
            tags_to_strip.extend(tags)
    if not tags_to_strip:
        return None
    low = text.lower()
    if not any(f"<{tag}>" in low for tag in tags_to_strip):
        return None
    out = text
    changed = False
    for tag in tags_to_strip:
        if f"<{tag}>" in low:
            pat = _TAG_PATTERNS[tag]
            new_out, count = pat.subn("", out)
            if count:
                out = new_out
                changed = True
    return out if changed else None


def _strip_pre_invocation_hook_metadata(text: str) -> str | None:
    if (
        "[BSL-ROUTING-METADATA]" not in text
        and "<bsl_Completion_Contract" not in text
        and "<bsl_Mode_Payload>" not in text
    ):
        return None
    new_out, count = _HOOK_META_RE.subn("", text)
    return new_out if count else None


def _filter_claude_code_system_reminders(text: str, cfg: Mapping[str, Any]) -> str | None:
    """Selectively strip disabled Claude Code `<system-reminder>` blocks."""
    if "<system-reminder>" not in text.lower():
        return None
    strip_hooks = cfg.get("hook_pre_invocation", True) is False
    strip_rules = cfg.get("prompt_user_rules", True) is False or cfg.get("custom_rules", True) is False
    strip_skills = cfg.get("prompt_skills_catalog", True) is False or cfg.get("custom_skills", True) is False
    if not (strip_hooks or strip_rules or strip_skills):
        return None

    changed = False

    def _repl(match: re.Match[str]) -> str:
        nonlocal changed
        inner = match.group(1)
        low = inner.lower()
        if strip_hooks and (
            "userpromptsubmit hook" in low
            or "[bsl-routing-metadata]" in low
            or "<bsl_completion_contract" in low
            or "<bsl_mode_payload>" in low
        ):
            changed = True
            return ""
        if strip_rules and ("claude.md" in low and ("contents of" in low or "claudemd" in low or "project instructions" in low)):
            changed = True
            return ""
        if strip_skills and ("following skills are available" in low or "available skills:" in low):
            changed = True
            return ""
        return match.group(0)

    out = _CC_REMINDER_BLOCK_RE.sub(_repl, text)
    return out if changed else None


def is_tool_allowed_by_switchboard(name: str, cfg: Mapping[str, Any]) -> bool:
    if not name:
        return True
    if cfg.get("tool_core_execution", True) is False and name in _CORE_EXECUTION_TOOLS:
        return False
    if cfg.get("tool_ask_question", True) is False and name in _ASK_QUESTION_TOOLS:
        return False
    if cfg.get("tool_subagents_swarm", True) is False and name in _SUBAGENT_SWARM_TOOLS:
        return False
    if cfg.get("tool_mcp_bridge", True) is False and (
        name in _MCP_BRIDGE_TOOLS or name.startswith("mcp_") or name.startswith("mcp__")
    ):
        return False
    if cfg.get("tool_web_search", True) is False and name in _WEB_SEARCH_TOOLS:
        return False
    if cfg.get("tool_generate_image", True) is False and name == "generate_image":
        return False
    return True


def apply_ls_components_to_gemini_inner(inner: dict[str, Any], cfg: Mapping[str, Any] | None) -> bool:
    """In-place filter on an unwrapped Gemini `inner` request dict (Antigravity / Blacksand Code).

    Returns True iff `inner` was modified. Fail-open on malformed fields.
    """
    if not isinstance(inner, dict) or not requires_wire_filtering(cfg, target="antigravity"):
        return False
    assert cfg is not None
    changed = False

    # Plane 3: System Prompt XML Section Filters
    si = inner.get("systemInstruction")
    if isinstance(si, str):
        rep = _filter_system_prompt_xml_sections(si, cfg)
        if rep is not None:
            inner["systemInstruction"] = rep
            changed = True
    elif isinstance(si, dict):
        parts = si.get("parts")
        if isinstance(parts, list):
            for pt in parts:
                if isinstance(pt, dict) and isinstance(pt.get("text"), str):
                    rep = _filter_system_prompt_xml_sections(pt["text"], cfg)
                    if rep is not None:
                        pt["text"] = rep
                        changed = True

    # Plane 1 (`!hook_pre_invocation`): Strip hook-injected metadata from user turns
    if cfg.get("hook_pre_invocation", True) is False:
        contents = inner.get("contents")
        if isinstance(contents, list):
            for msg in contents:
                if not isinstance(msg, dict):
                    continue
                role = str(msg.get("role") or "user").lower()
                if role != "user":
                    continue
                parts = msg.get("parts")
                if isinstance(parts, list):
                    for pt in parts:
                        if isinstance(pt, dict) and isinstance(pt.get("text"), str):
                            rep = _strip_pre_invocation_hook_metadata(pt["text"])
                            if rep is not None:
                                pt["text"] = rep
                                changed = True

    # Plane 4: Built-in Tool Declarations Filter
    tools = inner.get("tools")
    if isinstance(tools, list):
        new_tools = []
        tools_changed = False
        for tg in tools:
            if not isinstance(tg, dict):
                new_tools.append(tg)
                continue
            fds = tg.get("functionDeclarations")
            if isinstance(fds, list):
                kept = [
                    fd for fd in fds
                    if not isinstance(fd, dict)
                    or is_tool_allowed_by_switchboard(str(fd.get("name") or ""), cfg)
                ]
                if len(kept) != len(fds):
                    tools_changed = True
                if kept:
                    tg["functionDeclarations"] = kept
                    new_tools.append(tg)
                else:
                    tools_changed = True
            else:
                new_tools.append(tg)
        if tools_changed:
            changed = True
            if new_tools:
                inner["tools"] = new_tools
            else:
                inner.pop("tools", None)
                inner.pop("toolConfig", None)

    return changed


def apply_ls_components_to_raw_v1internal(raw_body: bytes, cfg: Mapping[str, Any] | None) -> bytes:
    """Apply Plane 1/3/4 wire filters to raw `/v1internal:*` JSON bytes (native passthrough).

    Zero-overhead fast-path: returns `raw_body` unchanged when `requires_wire_filtering(cfg)` is False.
    """
    if not raw_body or not requires_wire_filtering(cfg, target="antigravity"):
        return raw_body
    try:
        payload = json.loads(raw_body)
        if not isinstance(payload, dict):
            return raw_body
        inner = payload.get("request")
        target = inner if isinstance(inner, dict) else payload
        if not apply_ls_components_to_gemini_inner(target, cfg):
            return raw_body
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")
    except Exception:
        return raw_body


def apply_ls_components_to_anthropic_body(body: dict[str, Any], cfg: Mapping[str, Any] | None) -> bool:
    """In-place filter on an Anthropic `/v1/messages` request dict (Claude Code Desktop / CLI).

    Zero-overhead fast-path: exits in O(1) when `apply_claude_code` is False or all wire flags are ON.
    """
    if not isinstance(body, dict) or not requires_wire_filtering(cfg, target="claude_code"):
        return False
    assert cfg is not None
    changed = False

    def _sanitize_system_text(txt: str) -> str | None:
        cur = txt
        local_changed = False
        rep = _filter_system_prompt_xml_sections(cur, cfg)
        if rep is not None:
            cur = rep
            local_changed = True
        rep2 = _filter_claude_code_system_reminders(cur, cfg)
        if rep2 is not None:
            cur = rep2
            local_changed = True
        return cur if local_changed else None

    # Plane 3: Anthropic `system` field (str or list of text blocks)
    sys_val = body.get("system")
    if isinstance(sys_val, str):
        rep = _sanitize_system_text(sys_val)
        if rep is not None:
            body["system"] = rep
            changed = True
    elif isinstance(sys_val, list):
        for blk in sys_val:
            if isinstance(blk, dict) and blk.get("type", "text") == "text" and isinstance(blk.get("text"), str):
                rep = _sanitize_system_text(blk["text"])
                if rep is not None:
                    blk["text"] = rep
                    changed = True

    # Plane 1 + Plane 3 reminders inside user messages
    messages = body.get("messages")
    if isinstance(messages, list):
        strip_hook_meta = cfg.get("hook_pre_invocation", True) is False
        for msg in messages:
            if not isinstance(msg, dict) or str(msg.get("role") or "").lower() != "user":
                continue
            content = msg.get("content")
            if isinstance(content, str):
                cur = content
                c_mod = False
                if strip_hook_meta:
                    r1 = _strip_pre_invocation_hook_metadata(cur)
                    if r1 is not None:
                        cur = r1
                        c_mod = True
                r2 = _filter_claude_code_system_reminders(cur, cfg)
                if r2 is not None:
                    cur = r2
                    c_mod = True
                if c_mod:
                    msg["content"] = cur
                    changed = True
            elif isinstance(content, list):
                for blk in content:
                    if not isinstance(blk, dict) or blk.get("type") != "text" or not isinstance(blk.get("text"), str):
                        continue
                    cur = blk["text"]
                    c_mod = False
                    if strip_hook_meta:
                        r1 = _strip_pre_invocation_hook_metadata(cur)
                        if r1 is not None:
                            cur = r1
                            c_mod = True
                    r2 = _filter_claude_code_system_reminders(cur, cfg)
                    if r2 is not None:
                        cur = r2
                        c_mod = True
                    if c_mod:
                        blk["text"] = cur
                        changed = True

    # Plane 4: Anthropic `tools` list (`[{"name": "AskUserQuestion", ...}]`)
    tools = body.get("tools")
    if isinstance(tools, list):
        kept = [
            t for t in tools
            if not isinstance(t, dict)
            or is_tool_allowed_by_switchboard(str(t.get("name") or ""), cfg)
        ]
        if len(kept) != len(tools):
            changed = True
            if kept:
                body["tools"] = kept
            else:
                body.pop("tools", None)
                body.pop("tool_choice", None)

    return changed


# ─── Plane 2: Antigravity `:listExperiments` Wire Flag Filter ─────────────────


def _experiment_flag_overrides(cfg: Mapping[str, Any]) -> tuple[dict[str, bool], bool]:
    hooks_on = any(
        cfg.get(k, True) is not False
        for k in ("hook_session_start", "hook_pre_invocation", "hook_pre_tool_use", "hook_post_tool_use", "hook_stop")
    )
    subagents_on = (cfg.get("custom_subagents", True) is not False) and (cfg.get("tool_subagents_swarm", True) is not False)
    mcp_on = (cfg.get("enable_mcp_sidecars", True) is not False) and (cfg.get("tool_mcp_bridge", True) is not False)
    plugins_on = cfg.get("custom_plugins", True) is not False
    rules_on = cfg.get("custom_rules", True) is not False
    skills_on = cfg.get("custom_skills", True) is not False

    overrides: dict[str, bool] = {}
    if not subagents_on:
        for flag in (
            "enable-native-swarm",
            "enable-swarm-overview",
            "enable-swarm-graph-tab",
            "enable-swarm-raw-tab",
            "enable-swarm-monitor",
            "enable-swarm-narrator",
            "enable-customization-kind-agent",
            "enable-subagent-user-messaging",
            "send-subagent-initial-prompt-as-message",
        ):
            overrides[flag] = False
    if not mcp_on:
        for flag in (
            "enable-sidecar-sdk",
            "enable-sidecars",
            "enable-ui-sidecars",
            "sidecar-gateway",
            "sidecar-iframe-prewarm",
            "jetski-sidecar-declared-permissions",
            "enable-customization-kind-mcp",
        ):
            overrides[flag] = False
    if not plugins_on:
        for flag in (
            "enable-plugins",
            "enable-agent-plugins-spec",
            "enable-plugin-context-providers",
            "enable-customization-kind-plugin",
            "enable-customization-kind-workflow",
        ):
            overrides[flag] = False
    if not rules_on:
        overrides["enable-customization-kind-rule"] = False
    if not skills_on:
        overrides["enable-customization-kind-skill"] = False
    if not hooks_on:
        overrides["enable-customization-kind-hooks"] = False
        overrides["enable-hook-status"] = False
    return overrides, subagents_on


def filter_list_experiments_bytes(raw_body: bytes, cfg: Mapping[str, Any] | None) -> bytes:
    """Patch disabled Plane 2 experiment flags inside `/v1internal:listExperiments` JSON."""
    if not raw_body or not requires_experiment_filtering(cfg):
        return raw_body
    assert cfg is not None
    overrides, subagents_on = _experiment_flag_overrides(cfg)
    if not overrides and subagents_on:
        return raw_body
    try:
        data = json.loads(raw_body)
        if not isinstance(data, dict) or not isinstance(data.get("flags"), list):
            return raw_body
        flags = data["flags"]
        seen: set[str] = set()
        changed = False
        for entry in flags:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "")
            if name in overrides:
                seen.add(name)
                if entry.get("boolValue") is not overrides[name]:
                    entry["boolValue"] = overrides[name]
                    changed = True
            elif name == "invoke-subagent-config" and not subagents_on:
                seen.add(name)
                target_str = '{"enabled": false, "always_inherit_model": false}'
                if entry.get("stringValue") != target_str:
                    entry["stringValue"] = target_str
                    changed = True
        for name, val in overrides.items():
            if name not in seen:
                flags.append({"name": name, "boolValue": val})
                changed = True
        if not subagents_on and "invoke-subagent-config" not in seen:
            flags.append({
                "name": "invoke-subagent-config",
                "stringValue": '{"enabled": false, "always_inherit_model": false}',
            })
            changed = True
        if not changed:
            return raw_body
        return json.dumps(data, ensure_ascii=False).encode("utf-8")
    except Exception:
        return raw_body


# ─── Plane 1 & 2: Lossless Disk Sync (Only on Explicit Settings Save) ────────


def _default_antigravity_hooks_paths(home_dir: Path | None = None) -> list[Path]:
    home = home_dir if home_dir is not None else Path.home()
    candidates = [
        home / ".gemini" / "antigravity" / "hooks.json",
        home / ".gemini" / "antigravity-ide" / "hooks.json",
        home / ".gemini" / "blacksand-studio" / "hooks.json",
        home / ".gemini" / "config" / "hooks.json",
    ]
    out: list[Path] = []
    seen_resolved: set[str] = set()
    for p in candidates:
        if not p.exists():
            continue
        try:
            rp = str(p.resolve()).lower()
        except Exception:
            rp = str(p).lower()
        if rp not in seen_resolved:
            seen_resolved.add(rp)
            out.append(p)
    return out


def sync_antigravity_hooks_file(
    cfg: Mapping[str, Any],
    hooks_paths: list[Path] | None = None,
) -> dict[str, Any]:
    """Losslessly enable/disable hook lifecycle phases in Antigravity `hooks.json`.

    - Runs ONLY on explicit save (never on inference hot path).
    - If `apply_antigravity` is False, restores any stashed hooks back to `hooks.json`.
    - Writes disk ONLY when actual JSON content changes (avoids file-watcher churn).
    """
    from app.antigravity_patcher import _atomic_write

    apply_ag = cfg.get("apply_antigravity", True) is not False
    paths = hooks_paths if hooks_paths is not None else _default_antigravity_hooks_paths()
    updated_files: list[str] = []
    skipped_files: list[str] = []

    for path in paths:
        try:
            if not path.exists():
                continue
            raw_text = path.read_text(encoding="utf-8-sig")
            doc = json.loads(raw_text)
            if not isinstance(doc, dict):
                continue

            stash_path = path.with_name(path.name + ".bsl-stash.json")
            stash: dict[str, Any] = {}
            if stash_path.exists():
                try:
                    loaded_stash = json.loads(stash_path.read_text(encoding="utf-8-sig"))
                    if isinstance(loaded_stash, dict):
                        stash = loaded_stash
                except Exception:
                    stash = {}

            doc_changed = False
            stash_changed = False

            groups: list[tuple[str, dict[str, Any]]] = []
            phase_names = {phase for _, phase in _HOOK_KEY_MAP_ANTIGRAVITY}
            if any(k in doc for k in phase_names) or "__root__" in stash:
                groups.append(("__root__", doc))
            else:
                seen_groups: set[str] = set()
                for gname, gval in doc.items():
                    if isinstance(gval, dict):
                        groups.append((gname, gval))
                        seen_groups.add(gname)
                for gname, gstash_val in stash.items():
                    if gname != "__root__" and gname not in seen_groups and isinstance(gstash_val, dict):
                        new_group: dict[str, Any] = {}
                        doc[gname] = new_group
                        groups.append((gname, new_group))
                        seen_groups.add(gname)

            for gname, group in groups:
                g_stash = stash.get(gname)
                if not isinstance(g_stash, dict):
                    g_stash = {}
                for cfg_key, phase in _HOOK_KEY_MAP_ANTIGRAVITY:
                    want_on = True if not apply_ag else (cfg.get(cfg_key, True) is not False)
                    if not want_on:
                        if phase in group:
                            g_stash[phase] = group.pop(phase)
                            doc_changed = True
                            stash_changed = True
                    else:
                        if phase not in group and phase in g_stash:
                            group[phase] = g_stash.pop(phase)
                            doc_changed = True
                            stash_changed = True
                if g_stash:
                    stash[gname] = g_stash
                elif gname in stash:
                    stash.pop(gname, None)
                    stash_changed = True

            if stash_changed:
                if stash:
                    _atomic_write(stash_path, (json.dumps(stash, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
                elif stash_path.exists():
                    stash_path.unlink()

            if doc_changed:
                _atomic_write(path, (json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
                updated_files.append(str(path))
            else:
                skipped_files.append(str(path))
        except Exception:
            skipped_files.append(str(path))

    return {
        "updated": updated_files,
        "unchanged": skipped_files,
    }


def sync_claude_code_hooks_file(
    cfg: Mapping[str, Any],
    settings_path: Path | None = None,
) -> dict[str, Any]:
    """Losslessly enable/disable Claude Code hooks & `skillOverrides` in `~/.claude/settings.json`.

    - Runs ONLY on explicit save (never on inference hot path).
    - If `apply_claude_code` is False, restores any stashed hooks/statusLine/skillOverrides.
    - Writes disk ONLY when actual JSON content changes.
    """
    from app.antigravity_patcher import _atomic_write

    apply_cc = cfg.get("apply_claude_code", True) is not False
    path = settings_path if settings_path is not None else (Path.home() / ".claude" / "settings.json")
    if not path.exists():
        return {"updated": [], "unchanged": []}

    try:
        doc = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(doc, dict):
            return {"updated": [], "unchanged": [str(path)]}

        stash_path = path.with_name(path.name + ".bsl-stash.json")
        stash: dict[str, Any] = {}
        if stash_path.exists():
            try:
                loaded = json.loads(stash_path.read_text(encoding="utf-8-sig"))
                if isinstance(loaded, dict):
                    stash = loaded
            except Exception:
                stash = {}

        doc_changed = False
        stash_changed = False

        # Plane 1: `hooks` phases (`SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `Stop`)
        hooks_obj = doc.get("hooks")
        hooks_stash = stash.get("hooks")
        if not isinstance(hooks_stash, dict):
            hooks_stash = {}
        if isinstance(hooks_obj, dict) or hooks_stash:
            if not isinstance(hooks_obj, dict):
                hooks_obj = {}
                doc["hooks"] = hooks_obj
            for cfg_key, phase in _HOOK_KEY_MAP_CLAUDE_CODE:
                want_on = True if not apply_cc else (cfg.get(cfg_key, True) is not False)
                if not want_on:
                    if phase in hooks_obj:
                        hooks_stash[phase] = hooks_obj.pop(phase)
                        doc_changed = True
                        stash_changed = True
                else:
                    if phase not in hooks_obj and phase in hooks_stash:
                        hooks_obj[phase] = hooks_stash.pop(phase)
                        doc_changed = True
                        stash_changed = True
        if hooks_stash:
            stash["hooks"] = hooks_stash
        elif "hooks" in stash:
            stash.pop("hooks", None)
            stash_changed = True

        # Plane 1 (`hook_session_start`): `statusLine` bootstrap hook if present
        want_statusline = True if not apply_cc else (cfg.get("hook_session_start", True) is not False)
        if not want_statusline:
            if "statusLine" in doc:
                stash["statusLine"] = doc.pop("statusLine")
                doc_changed = True
                stash_changed = True
        else:
            if "statusLine" not in doc and "statusLine" in stash:
                doc["statusLine"] = stash.pop("statusLine")
                doc_changed = True
                stash_changed = True

        # Plane 2 (`custom_skills`): `skillOverrides`
        want_skills = True if not apply_cc else (cfg.get("custom_skills", True) is not False)
        if not want_skills:
            if isinstance(doc.get("skillOverrides"), dict) and any(v != "off" for v in doc["skillOverrides"].values()):
                if "skillOverrides" not in stash:
                    stash["skillOverrides"] = dict(doc["skillOverrides"])
                    stash_changed = True
                doc["skillOverrides"] = {k: "off" for k in doc["skillOverrides"]}
                doc_changed = True
        else:
            if "skillOverrides" in stash:
                doc["skillOverrides"] = stash.pop("skillOverrides")
                doc_changed = True
                stash_changed = True

        if stash_changed:
            if stash:
                _atomic_write(stash_path, (json.dumps(stash, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
            elif stash_path.exists():
                stash_path.unlink()

        if doc_changed:
            _atomic_write(path, (json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
            return {"updated": [str(path)], "unchanged": []}
        return {"updated": [], "unchanged": [str(path)]}
    except Exception:
        return {"updated": [], "unchanged": [str(path)]}


# ─── Environment Pairing & Per-Switch Applicability Detector ──────────────────

_BSL_HOOK_MARKERS: tuple[str, ...] = (
    "bsl-routing",
    "bsl_completion",
    "bsl-studio",
    "blacksand",
    "pre_invocation",
    "stop_contract",
)

# Which switch keys are Antigravity-only vs shared across Antigravity + Claude Code
_ANTIGRAVITY_ONLY_KEYS: frozenset[str] = frozenset({
    "custom_plugins",
    "custom_subagents",
    "enable_mcp_sidecars",
    "prompt_guidelines",
    "prompt_mcp_servers",
    "prompt_subagents_section",
    "prompt_artifacts_section",
    "prompt_slash_and_style",
    "tool_generate_image",
})


def _collect_hook_phases_from_doc(doc: Any, phase_names: set[str]) -> tuple[set[str], bool]:
    """Return (present_phases, has_bsl_markers) from a hooks.json / stash dict."""
    found: set[str] = set()
    has_bsl = False
    if not isinstance(doc, dict):
        return found, has_bsl
    try:
        serialized = json.dumps(doc, ensure_ascii=False).lower()
        has_bsl = any(m in serialized for m in _BSL_HOOK_MARKERS)
    except Exception:
        pass
    if any(k in doc for k in phase_names):
        for p in phase_names:
            if p in doc:
                found.add(p)
    else:
        for gval in doc.values():
            if isinstance(gval, dict):
                for p in phase_names:
                    if p in gval:
                        found.add(p)
    return found, has_bsl


def detect_ls_components_environment(
    cfg: Mapping[str, Any] | None = None,
    *,
    home_dir: Path | None = None,
    appdata_dir: Path | None = None,
    localappdata_dir: Path | None = None,
) -> dict[str, Any]:
    """Read-only environment pairing probe for the Unified 4-Plane Switchboard.

    Detects which host apps (original Antigravity App/IDE, Blacksand Code,
    Claude Code Desktop/CLI) and which Plane 1 hook phases exist on this machine,
    returning per-switch applicability so the UI can blur/annotate inapplicable
    options cleanly for both vanilla GitHub users and Blacksand Studio users.
    """
    eff_cfg = validate_ls_components_config(cfg) if cfg is not None else dict(DEFAULT_ANTIGRAVITY_LS_COMPONENTS)
    home = home_dir if home_dir is not None else Path.home()
    roaming = appdata_dir if appdata_dir is not None else Path(os.environ.get("APPDATA") or (home / "AppData" / "Roaming"))
    local = localappdata_dir if localappdata_dir is not None else Path(os.environ.get("LOCALAPPDATA") or (home / "AppData" / "Local"))

    ag_original_installed = any(p.exists() for p in (
        local / "Programs" / "antigravity" / "resources" / "app.asar",
        roaming / "Antigravity IDE",
        roaming / "Antigravity",
        home / ".gemini" / "antigravity",
        home / ".gemini" / "antigravity-ide",
    ))
    bsl_code_env_asar = any(
        bool(os.environ.get(env_k, "").strip()) and Path(os.environ.get(env_k, "").strip()).exists()
        for env_k in ("BSL_BLACKSAND_STUDIO_ASAR", "BSL_BLACKSAND_CODE_ASAR")
    )
    blacksand_code_installed = (
        bsl_code_env_asar
        or (home / ".gemini" / "blacksand-studio").exists()
        or (home / ".gemini" / "blacksand-code-lite").exists()
        or (home / ".gemini" / "blacksand-code-dev").exists()
    )
    antigravity_family_installed = ag_original_installed or blacksand_code_installed

    cc_settings_path = home / ".claude" / "settings.json"
    claude_code_installed = cc_settings_path.exists() or (home / ".claude").exists()

    # Inspect Plane 1 hook phases across live files AND .bsl-stash.json so
    # pausing a hook never falsely reports it as "No hook configured".
    ag_phase_names = {phase for _, phase in _HOOK_KEY_MAP_ANTIGRAVITY}
    ag_phases_present: set[str] = set()
    has_bsl_hooks = False
    for hp in _default_antigravity_hooks_paths(home_dir=home):
        for candidate in (hp, hp.with_name(hp.name + ".bsl-stash.json")):
            if not candidate.exists():
                continue
            try:
                doc = json.loads(candidate.read_text(encoding="utf-8-sig"))
                phases, bsl_flag = _collect_hook_phases_from_doc(doc, ag_phase_names)
                ag_phases_present.update(phases)
                has_bsl_hooks = has_bsl_hooks or bsl_flag
            except Exception:
                pass

    cc_phase_names = {phase for _, phase in _HOOK_KEY_MAP_CLAUDE_CODE}
    cc_phases_present: set[str] = set()
    cc_has_statusline = False
    cc_has_skill_overrides = False
    for candidate in (cc_settings_path, cc_settings_path.with_name(cc_settings_path.name + ".bsl-stash.json")):
        if not candidate.exists():
            continue
        try:
            doc = json.loads(candidate.read_text(encoding="utf-8-sig"))
            if not isinstance(doc, dict):
                continue
            serialized = json.dumps(doc, ensure_ascii=False).lower()
            if any(m in serialized for m in _BSL_HOOK_MARKERS):
                has_bsl_hooks = True
            hooks_obj = doc.get("hooks")
            if isinstance(hooks_obj, dict):
                for p in cc_phase_names:
                    if p in hooks_obj:
                        cc_phases_present.add(p)
            if "statusLine" in doc:
                cc_has_statusline = True
            if isinstance(doc.get("skillOverrides"), dict) and doc["skillOverrides"]:
                cc_has_skill_overrides = True
        except Exception:
            pass

    ag_target_on = eff_cfg.get("apply_antigravity", True) is not False
    cc_target_on = eff_cfg.get("apply_claude_code", True) is not False
    ag_usable = ag_target_on and antigravity_family_installed
    cc_usable = cc_target_on and claude_code_installed

    ag_label = "Antigravity / Blacksand Code" if blacksand_code_installed else "Antigravity App / IDE"

    applicability: dict[str, dict[str, Any]] = {}
    for key in DEFAULT_ANTIGRAVITY_LS_COMPONENTS:
        if key in TARGET_KEYS:
            continue
        # Both target checkboxes unchecked
        if not ag_target_on and not cc_target_on:
            applicability[key] = {"applicable": False, "reason": "Enable an app target above", "targets": []}
            continue

        # Plane 1: Lifecycle Hook Interceptors
        if key.startswith("hook_"):
            ag_phase = dict(_HOOK_KEY_MAP_ANTIGRAVITY).get(key, "")
            cc_phase = dict(_HOOK_KEY_MAP_CLAUDE_CODE).get(key, "")
            ag_has = ag_usable and (ag_phase in ag_phases_present)
            cc_has = cc_usable and (
                (cc_phase in cc_phases_present)
                or (key == "hook_session_start" and cc_has_statusline)
                or (key == "hook_pre_invocation" and has_bsl_hooks)
            )
            matched_targets: list[str] = []
            if ag_has:
                matched_targets.append("antigravity")
            if cc_has:
                matched_targets.append("claude_code")
            if matched_targets:
                applicability[key] = {"applicable": True, "reason": "", "targets": matched_targets}
            elif not ag_usable and not cc_usable:
                applicability[key] = {"applicable": False, "reason": "Target app not detected", "targets": []}
            else:
                applicability[key] = {"applicable": False, "reason": "No hook configured", "targets": []}
            continue

        # Planes 2, 3, 4
        if key in _ANTIGRAVITY_ONLY_KEYS:
            if ag_usable:
                applicability[key] = {"applicable": True, "reason": "", "targets": ["antigravity"]}
            elif not ag_target_on:
                applicability[key] = {"applicable": False, "reason": f"{ag_label} only", "targets": []}
            else:
                applicability[key] = {"applicable": False, "reason": f"{ag_label} not detected", "targets": []}
        else:
            # Shared across Antigravity + Claude Code
            matched_targets = []
            if ag_usable:
                matched_targets.append("antigravity")
            if cc_usable:
                matched_targets.append("claude_code")
            if matched_targets:
                applicability[key] = {"applicable": True, "reason": "", "targets": matched_targets}
            else:
                applicability[key] = {"applicable": False, "reason": "Target app not detected", "targets": []}

    return {
        "apps": {
            "antigravity_installed": antigravity_family_installed,
            "antigravity_original_installed": ag_original_installed,
            "blacksand_code_installed": blacksand_code_installed,
            "claude_code_installed": claude_code_installed,
            "has_bsl_hooks": has_bsl_hooks,
            "cc_has_skill_overrides": cc_has_skill_overrides,
            "antigravity_label": ag_label,
        },
        "detected_hooks": {
            "antigravity": sorted(ag_phases_present),
            "claude_code": sorted(cc_phases_present | ({"statusLine"} if cc_has_statusline else set())),
        },
        "applicability": applicability,
    }
