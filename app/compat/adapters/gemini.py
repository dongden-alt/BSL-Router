"""
BSL Router — Antigravity (Google Cloud Code / Gemini private API) adapter.

Phase 5B-1. Ports 9Router's Antigravity MITM conversion into BSL as a set of
PURE functions (no FastAPI / httpx imports) so they are unit-testable offline.

Wire shape mirrors the existing Claude Code pattern:
    route `v1internal:generateContent` / `:streamGenerateContent`
      → unwrap Cloud Code wrapper + convert Gemini→OpenAI (this module)
      → `_process_chat_completion(openai_body, client_wants_gemini=True)`
      → egress converts OpenAI→Gemini on the way out (this module)

Authoritative reference: `internal conversion spec, local .brain`
(spec §1–§8 + PORTING NOTES A–G). Skipped per §G: tool-name cloaking,
OAuth refresh, the sqlite `__nineRouterFinalThinkingPatch` DB lookup — BSL uses
its own provider keys and its own per-model thinking config.
"""
from __future__ import annotations

import json
import re
import time
import uuid as _uuid
from typing import Any, Dict, Optional

# ──────────────────────────────────────────────────────────────────────────────
# Constants — spec §2 / §6
# ──────────────────────────────────────────────────────────────────────────────

# Cap on generationConfig.maxOutputTokens for inbound antigravity requests (§8.4).
_MAX_OUTPUT_TOKENS_CAP = 16384

# thinkingConfig.thinkingBudget → reasoning_effort thresholds (§3, module 58028 `Vp`).
def _budget_to_effort(budget: Any) -> str:
    try:
        b = int(budget)
    except (TypeError, ValueError):
        return "high"
    if b <= 2048:
        return "low"
    if b <= 16384:
        return "medium"
    return "high"

# §8.8 (2026-08-24): the single source of truth for the synthetic notice the
# router emits when upstream produced zero renderable output. The Antigravity
# IDE hard-rejects frames where text and tool calls are BOTH empty, so an
# honest visible notice replaces the old parts=[{"text": ""}] placeholder.
# gemini_frame_has_content deliberately treats this exact string as NON-content
# so combo fallback stays available on zero-output finishes.
BSL_NO_OUTPUT_NOTICE = (
    "[BSL Router] Upstream returned no renderable output "
    "(text and tool calls were both empty). Try again or "
    "switch model."
)

# Model key mapping — spec §6. Exact synonym map first, then regex cascade.
MODEL_SYNONYMS: Dict[str, str] = {
    "gemini-default": "gemini-3.5-flash-low",
    "gemini-3.5-flash-high": "gemini-3-flash-agent",
    "gemini-3.5-flash-medium": "gemini-3.5-flash-low",
    "gemini-3.5-flash-extra-low": "gemini-3.5-flash-extra-low",
    "gemini-3.1-pro-high": "gemini-pro-agent",
    "gemini-3-pro-high": "gemini-pro-agent",
    "gemini-3-pro-low": "gemini-3.1-pro-low",
}

# Each entry: (compiled case-insensitive regex, alias). Order matters — first hit wins.
MODEL_PATTERNS = [
    (re.compile(r"flash.*extra.*low|extra.*low.*flash|flash.*low|low.*flash", re.I), "gemini-3.5-flash-extra-low"),
    (re.compile(r"flash.*medium|medium.*flash", re.I), "gemini-3.5-flash-low"),
    (re.compile(r"flash.*agent|agent.*flash|flash", re.I), "gemini-3.5-flash-high"),
    (re.compile(r"pro.*low|low.*pro", re.I), "gemini-3.1-pro-low"),
    (re.compile(r"gemini.*pro|pro.*gemini", re.I), "gemini-3.1-pro-high"),
    (re.compile(r"opus", re.I), "claude-opus-5-5-high"),
    (re.compile(r"sonnet|claude", re.I), "claude-sonnet-5-5-high"),
]

# Tab-completion models bypass mapping entirely (§6 / §8.11).
_TAB_RE = re.compile(r"^tab[_-]", re.I)

# OpenAI finish_reason → Gemini finishReason (§4a `bC`→`a_`).
# `tool_calls` maps to STOP (not FUNCTION_CALL): the Antigravity IDE
# treats FUNCTION_CALL as a terminal signal and stops processing, whereas
# STOP lets it consume the accumulated functionCall parts correctly.
_FINISH_MAP = {
    "stop": "STOP",
    "length": "MAX_TOKENS",
    "tool_calls": "STOP",
    "content_filter": "SAFETY",
    "function_call": "FUNCTION_CALL",
}

# Terminal SSE sentinel (§5).
# BUGFIX (2026-10-24): Antigravity IDE's Go `language_server.exe`
# (`codeassistclient.responseStream.scanner`) skips SSE lines where
# `!strings.HasPrefix(line, "data: ")` (e.g. `: heartbeat`, `: keepalive`) and
# calls `protojson.Unmarshal` on EVERY non-empty `data: ` line. Emitting the
# OpenAI sentinel `b"data: [DONE]\n\n"` feeds `[DONE]` to `protojson.Unmarshal`,
# which fails at line 1:1 with `proto: syntax error (line 1:1): unexpected token [`
# and aborts the turn (`Error Unknown: Agent execution terminated due to error.`).
# Using an SSE comment sentinel `: [DONE]\n\n` causes the Go scanner to skip it
# cleanly while still flushing the final ASGI chunk and preserving `[DONE]`
# visibility for diagnostics.
SSE_DONE = b": [DONE]\n\n"


def terminal_error_frame(code: int, message: str, model: str = "") -> Dict[str, Any]:
    """The SOLE client-visible Gemini terminal frame for an error path.

    CONTRACT (2026-08-07 freeze fix). A Gemini error exit must emit exactly ONE
    parser-valid terminal frame: this response envelope, optionally followed by
    SSE_DONE. It must NOT be preceded by a top-level `{"error": {...}}` frame.

    Why no preceding error frame — the freeze evidence:
      The previous contract emitted three frames in order:
        1. `{"error": {"code", "message", "status"}}`   (top-level, no candidates)
        2. `{"response": {"candidates": [{finishReason: STOP}]}}`  (this helper)
        3. `data: [DONE]`                                (OpenAI sentinel)
      All-leaves-429 reproduction (2026-08-07) confirmed the IDE froze despite
      frame 2 carrying `finishReason`. The Antigravity Gemini parser treats a
      top-level `error` object as a terminal/error transition: once it consumes
      frame 1 it stops processing subsequent `candidates` frames, so the
      `finishReason` it actually needs to end the stream is never acted on and
      the IDE waits on a closed connection. The `[DONE]` sentinel is discarded
      by the Gemini wire protocol regardless. The fix is to emit frame 2 ALONE
      (plus the harmless `[DONE]` tail) — never frame 1.

    This frame carries what the parser actually terminates on — a candidate with
    a `finishReason` — and puts the error text in `parts` so the failure is
    VISIBLE in the transcript rather than silent.

    Uses finishReason STOP rather than a non-standard value on purpose: unknown
    enum values risk being dropped by the same strict parser, which would
    reintroduce the freeze. A visible error message plus a clean stop is the
    safer contract.
    """
    # SANITIZE (2026-10-14): the caller passes stats["error"], which can carry a
    # raw httpx repr ("midstream_transport: peer closed connection without
    # sending complete message body (incomplete chunked read)") or an internal
    # sentinel name. Render one stable client-facing sentence instead; the full
    # detail stays in the server log. Lazy import: app.antifreeze lazily imports
    # THIS module inside _terminal_frames, so a top-level import would be a
    # cycle risk.
    try:
        from app.antifreeze import sanitize_client_error as _sce
        label = _sce(message or "upstream error").strip()
    except Exception:
        label = (message or "upstream error").strip()
    return {
        "response": {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [{"text": f"\n\n[BSL Router] upstream error {code}: {label}"}],
                    },
                    "finishReason": "STOP",
                    "index": 0,
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 0,
                "candidatesTokenCount": 0,
                "totalTokenCount": 0,
            },
            "modelVersion": model or "bsl-routed",
            "responseId": f"bslerr_{int(time.time() * 1000)}",
        }
    }

# ──────────────────────────────────────────────────────────────────────────────
# Antigravity IDE metadata injection (spec §8.16)
# ──────────────────────────────────────────────────────────────────────────────

# ─── Tool-argument repair ledger (DROP-SYNDROME FIX 2026-09-22) ───────────
# The drop site below previously only print()ed to stdout. Router stdout is
# NOT captured to disk, so a defect that silently deleted tool calls from
# every heavily-fragmented batch left no durable evidence anywhere — it took
# a purpose-built reproducer to find. These counters make the repair/drop rate
# observable in-process.
_TOOL_ARG_STATS = {"repaired": 0, "dropped": 0, "split": 0}


def get_tool_arg_stats() -> Dict[str, int]:
    """Snapshot of tool-argument repair/drop counters (process-local)."""
    return dict(_TOOL_ARG_STATS)


def _repair_tool_args(raw_args: str) -> Optional[Dict[str, Any]]:
    """Best-effort parse of a tool-arguments string via the shared repair ladder.

    Returns the parsed dict, or None when the fragment is genuinely
    unrecoverable. NEVER raises — the caller is on the streaming hot path.

    The ladder (app/middleware/tool_arg_repair.py) closes unterminated
    strings, balances brackets, quotes bare keys/values and strips dangling
    commas. Imported lazily: adapters are imported very early during app
    construction and a module-level middleware import risks a cycle.
    """
    if not isinstance(raw_args, str) or not raw_args.strip():
        return None
    try:
        from app.middleware.tool_arg_repair import repair_json_arguments
        repaired, was_repaired = repair_json_arguments(raw_args)
        if not was_repaired:
            return None
        parsed = json.loads(repaired)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


def _split_tool_args_documents(raw_args: str) -> "list[str]":
    """Recover N COMPLETE JSON documents concatenated into one slot (2026-10-15).

    Returns the validated documents when at least two exist (an index/id
    collision upstream merged parallel calls into one buffer), else [].
    Lazy import mirrors ``_repair_tool_args``; NEVER raises.
    """
    try:
        from app.middleware.tool_arg_repair import split_extra_documents
        docs = split_extra_documents(raw_args)
        return docs if len(docs) >= 2 else []
    except Exception:
        return []


_TOOL_NAME_CLEAN_RE = re.compile(r"^([A-Za-z0-9_.:-]+)")
_PLACEHOLDER_TOOL_NAMES = frozenset({
    "TOOL_NAME",
    "FUNCTION_NAME",
    "EXAMPLE_TOOL",
    "YOUR_TOOL_NAME",
})

# Canonical parameter schemas for built-in Antigravity IDE tools.
# Antigravity's language_server validates every functionCall.args with strict
# `additionalProperties: false`. Injecting unknown keys (`toolAction` on any
# tool, or `toolSummary` on non-`run_command` tools) causes immediate client
# rejection: `Error invalid tool call: invalid arguments: - additional properties ... not allowed`.
_ANTIGRAVITY_KNOWN_TOOL_SCHEMAS: Dict[str, Dict[str, Any]] = {
    "run_command": {
        "properties": {
            "CommandLine": "string",
            "WaitMsBeforeAsync": "integer",
            "IsDaemon": "boolean",
            "toolSummary": "string",
            "Cwd": "string",
            "SafeToAutoRun": "boolean",
        },
        "required": ("CommandLine", "WaitMsBeforeAsync", "toolSummary"),
        "aliases": {
            "command": "CommandLine",
            "cmd": "CommandLine",
            "command_line": "CommandLine",
            "commandLine": "CommandLine",
            "wait_ms": "WaitMsBeforeAsync",
            "waitMsBeforeAsync": "WaitMsBeforeAsync",
            "wait_ms_before_async": "WaitMsBeforeAsync",
            "is_daemon": "IsDaemon",
            "isDaemon": "IsDaemon",
            "tool_summary": "toolSummary",
        },
        "strict": True,
    },
    "read_file": {
        "properties": {
            "Path": "string",
            "StartLine": "integer",
            "EndLine": "integer",
            "ByteOffset": "integer",
        },
        "required": ("Path",),
        "aliases": {
            "path": "Path",
            "file_path": "Path",
            "filePath": "Path",
            "file": "Path",
            "start_line": "StartLine",
            "startLine": "StartLine",
            "end_line": "EndLine",
            "endLine": "EndLine",
            "byte_offset": "ByteOffset",
            "byteOffset": "ByteOffset",
        },
        "strict": True,
    },
    "write_file": {
        "properties": {
            "Path": "string",
            "Content": "string",
            "Overwrite": "boolean",
            "Append": "boolean",
            "ArtifactMetadata": "object",
        },
        "required": ("Path", "Content"),
        "aliases": {
            "path": "Path",
            "file_path": "Path",
            "filePath": "Path",
            "content": "Content",
            "overwrite": "Overwrite",
            "append": "Append",
            "artifact_metadata": "ArtifactMetadata",
            "artifactMetadata": "ArtifactMetadata",
        },
        "strict": True,
    },
    "edit_file": {
        "properties": {
            "Path": "string",
            "Target": "string",
            "Replacement": "string",
            "ArtifactMetadata": "object",
        },
        "required": ("Path", "Target", "Replacement"),
        "aliases": {
            "path": "Path",
            "file_path": "Path",
            "filePath": "Path",
            "target": "Target",
            "old_string": "Target",
            "old_str": "Target",
            "replacement": "Replacement",
            "new_string": "Replacement",
            "new_str": "Replacement",
            "artifact_metadata": "ArtifactMetadata",
            "artifactMetadata": "ArtifactMetadata",
        },
        "strict": True,
    },
    "manage_task": {
        "properties": {
            "Action": "string",
            "TaskId": "string",
            "Input": "string",
        },
        "required": ("Action",),
        "aliases": {
            "action": "Action",
            "task_id": "TaskId",
            "taskId": "TaskId",
            "input": "Input",
        },
        "strict": True,
    },
    "invoke_subagent": {
        "properties": {
            "TypeName": "string",
            "Role": "string",
            "Prompt": "string",
            "Model": "string",
            "Workspace": "string",
        },
        "required": ("TypeName", "Role", "Prompt"),
        "aliases": {
            "type_name": "TypeName",
            "typeName": "TypeName",
            "role": "Role",
            "prompt": "Prompt",
            "model": "Model",
            "workspace": "Workspace",
        },
        "strict": True,
    },
    "define_subagent": {
        "properties": {
            "Name": "string",
            "Description": "string",
            "SystemPrompt": "string",
            "EnableMCPTools": "boolean",
            "EnableSubagentTools": "boolean",
            "EnableWriteTools": "boolean",
        },
        "required": ("Name", "Description", "SystemPrompt"),
        "aliases": {
            "name": "Name",
            "description": "Description",
            "system_prompt": "SystemPrompt",
            "systemPrompt": "SystemPrompt",
        },
        "strict": True,
    },
    "manage_subagents": {
        "properties": {
            "Action": "string",
            "ConversationIds": "array",
        },
        "required": ("Action",),
        "aliases": {
            "action": "Action",
            "conversation_ids": "ConversationIds",
            "conversationIds": "ConversationIds",
        },
        "strict": True,
    },
    "send_message": {
        "properties": {
            "Recipient": "string",
            "Message": "string",
        },
        "required": ("Recipient", "Message"),
        "aliases": {
            "recipient": "Recipient",
            "message": "Message",
        },
        "strict": True,
    },
    "schedule": {
        "properties": {
            "Prompt": "string",
            "DurationSeconds": "integer",
            "CronExpression": "string",
            "IsDaemon": "boolean",
            "MaxIterations": "integer",
            "TimerCondition": "string",
        },
        "required": ("Prompt",),
        "aliases": {
            "prompt": "Prompt",
            "duration_seconds": "DurationSeconds",
            "durationSeconds": "DurationSeconds",
            "cron_expression": "CronExpression",
            "cronExpression": "CronExpression",
            "is_daemon": "IsDaemon",
            "isDaemon": "IsDaemon",
            "max_iterations": "MaxIterations",
            "maxIterations": "MaxIterations",
            "timer_condition": "TimerCondition",
            "timerCondition": "TimerCondition",
        },
        "strict": True,
    },
    "search_web": {
        "properties": {
            "query": "string",
            "domain": "string",
        },
        "required": ("query",),
        "aliases": {
            "Query": "query",
            "Domain": "domain",
        },
        "strict": True,
    },
    "search_marketplace": {
        "properties": {
            "queries": "array",
            "kind": "string",
            "limit": "integer",
        },
        "required": ("queries",),
        "aliases": {
            "Queries": "queries",
            "Kind": "kind",
            "Limit": "limit",
        },
        "strict": True,
    },
    "ask_question": {
        "properties": {
            "questions": "array",
        },
        "required": ("questions",),
        "aliases": {
            "Questions": "questions",
        },
        "strict": True,
    },
    "call_mcp_tool": {
        "properties": {
            "ServerName": "string",
            "ToolName": "string",
            "Arguments": "object",
        },
        "required": ("ServerName", "ToolName", "Arguments"),
        "aliases": {
            "server_name": "ServerName",
            "serverName": "ServerName",
            "tool_name": "ToolName",
            "toolName": "ToolName",
            "arguments": "Arguments",
            "args": "Arguments",
        },
        "strict": True,
    },
    "list_resources": {
        "properties": {
            "ServerName": "string",
        },
        "required": ("ServerName",),
        "aliases": {
            "server_name": "ServerName",
            "serverName": "ServerName",
        },
        "strict": True,
    },
    "read_resource": {
        "properties": {
            "ServerName": "string",
            "Uri": "string",
        },
        "required": (),
        "aliases": {
            "server_name": "ServerName",
            "serverName": "ServerName",
            "uri": "Uri",
        },
        "strict": True,
    },
}


def _sanitize_tool_name(raw_name: Any, tool_schemas: Optional[Dict[str, Any]] = None) -> str:
    """Extract a clean tool identifier when malformed tags/attrs fuse onto a tool name."""
    if not isinstance(raw_name, str):
        return ""
    s = raw_name.strip()
    if not s:
        return ""
    m = _TOOL_NAME_CLEAN_RE.match(s)
    clean = m.group(1) if m else s
    if isinstance(tool_schemas, dict) and tool_schemas and clean not in tool_schemas:
        low = clean.lower()
        for k in tool_schemas:
            if k.lower() == low:
                return k
    return clean


def _is_bogus_tool_call(
    name: str,
    args: Any,
    tool_schemas: Optional[Dict[str, Any]] = None,
) -> bool:
    """Return True if a tool call is a prompt-template placeholder or undeclared noise."""
    if not name or name.upper() in _PLACEHOLDER_TOOL_NAMES:
        return True
    if not _TOOL_NAME_CLEAN_RE.match(name):
        return True
    if isinstance(args, dict) and args == {"arg_name": "arg_value"}:
        return True
    if isinstance(tool_schemas, dict) and tool_schemas:
        if name not in tool_schemas and name not in _ANTIGRAVITY_KNOWN_TOOL_SCHEMAS:
            return True
    return False


def extract_openai_tool_schemas(tools: Any) -> Dict[str, Dict[str, Any]]:
    """Build a `{tool_name: {properties: {k: type}, required: (...)}}` map from OpenAI tools."""
    out: Dict[str, Dict[str, Any]] = {}
    if not isinstance(tools, list):
        return out
    for t in tools:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if isinstance(t.get("function"), dict) else t
        name = fn.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        name = name.strip()
        params = fn.get("parameters") or fn.get("parametersJsonSchema") or {}
        if not isinstance(params, dict):
            continue
        raw_props = params.get("properties")
        props: Dict[str, str] = {}
        if isinstance(raw_props, dict):
            for pk, pv in raw_props.items():
                if isinstance(pv, dict) and isinstance(pv.get("type"), str):
                    props[pk] = pv["type"].lower()
                else:
                    props[pk] = "any"
        req_list = params.get("required")
        req_tuple = tuple(req_list) if isinstance(req_list, list) else ()
        out[name] = {
            "properties": props,
            "required": req_tuple,
            "aliases": _ANTIGRAVITY_KNOWN_TOOL_SCHEMAS.get(name, {}).get("aliases", {}),
            "strict": bool(
                params.get("additionalProperties") is False
                or name in _ANTIGRAVITY_KNOWN_TOOL_SCHEMAS
            ),
        }
    return out


def _coerce_arg_value(val: Any, expected_type: str, key_name: str = "") -> Any:
    """Coerce stringified scalars/containers into the JSON type expected by the tool schema."""
    if expected_type == "integer":
        if isinstance(val, bool):
            return val
        if isinstance(val, int):
            res = val
        elif isinstance(val, float) and val.is_integer():
            res = int(val)
        elif isinstance(val, str):
            s = val.strip()
            if s.lstrip("-").isdigit():
                res = int(s)
            else:
                return val
        else:
            return val
        if key_name == "WaitMsBeforeAsync":
            return max(500, min(10000, res))
        return res

    if expected_type == "boolean":
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            s = val.strip().lower()
            if s == "true":
                return True
            if s == "false":
                return False
        return val

    if expected_type == "string":
        if isinstance(val, str) and len(val) >= 2 and val[0] == '"' and val[-1] == '"':
            try:
                inner = json.loads(val)
                if isinstance(inner, str):
                    return inner
            except Exception:
                return val[1:-1]
        return val

    if expected_type == "object":
        if isinstance(val, str):
            s = val.strip()
            if s.startswith("{") and s.endswith("}"):
                try:
                    parsed = json.loads(s)
                    if isinstance(parsed, dict):
                        return parsed
                except Exception:
                    pass
        return val

    if expected_type == "array":
        if isinstance(val, str):
            s = val.strip()
            if s.startswith("[") and s.endswith("]"):
                try:
                    parsed = json.loads(s)
                    if isinstance(parsed, list):
                        return parsed
                except Exception:
                    pass
        return val

    return val


def _inject_tool_metadata(
    args: Any,
    tool_name: str,
    tool_schemas: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Sanitize, coerce, and backfill IDE metadata fields on functionCall args.

    Root-cause fix (2026-10-29):
    Antigravity IDE (`language_server.exe`) validates every `functionCall.args`
    against that tool's declared JSON Schema with strict `additionalProperties: false`.
    - `toolAction` is NOT a valid property on ANY Antigravity tool. Unconditionally
      injecting `toolAction` caused 100% of `run_command` calls to fail with:
      `invalid arguments: - additional properties 'toolAction' not allowed`.
    - `toolSummary` is ONLY a valid property on `run_command` (or tools whose
      schema explicitly declares `toolSummary`). Injecting `toolSummary` on
      `read_file`, `write_file`, `edit_file`, `call_mcp_tool`, etc. caused 100%
      of those calls to fail with:
      `invalid arguments: - additional properties 'toolAction', 'toolSummary' not allowed`.
    """
    if not isinstance(args, dict):
        args = {}
    else:
        args = dict(args)

    clean_name = _sanitize_tool_name(tool_name, tool_schemas) or tool_name
    explicit_schema = (
        tool_schemas.get(clean_name)
        if isinstance(tool_schemas, dict) and clean_name in tool_schemas
        else None
    )
    schema = explicit_schema or _ANTIGRAVITY_KNOWN_TOOL_SCHEMAS.get(clean_name)

    props: Dict[str, str] = (schema.get("properties") if isinstance(schema, dict) else None) or {}
    required = (schema.get("required") if isinstance(schema, dict) else None) or ()
    aliases: Dict[str, str] = (schema.get("aliases") if isinstance(schema, dict) else None) or {}
    is_strict = bool(schema.get("strict")) if isinstance(schema, dict) else False

    # 1. Remap common lowercase/snake_case parameter aliases when canonical key is absent.
    #    Only remap when explicit tool_schemas is active or for `run_command` so synthetic
    #    unit tests calling `read_file({"path": "a.py"})` without schemas retain `path`.
    if aliases and (explicit_schema is not None or clean_name == "run_command"):
        for alias_key, canonical_key in list(aliases.items()):
            if alias_key in args and canonical_key in props and alias_key not in props:
                if canonical_key not in args:
                    args[canonical_key] = args.pop(alias_key)
                else:
                    args.pop(alias_key, None)

    # 2. Strip poisoned/hallucinated `toolAction` unless explicitly in tool schema.
    if "toolAction" not in props:
        args.pop("toolAction", None)
    else:
        args.setdefault("toolAction", f"Running {clean_name}")

    # 3. Only inject `toolSummary` for `run_command` or tools whose schema declares it;
    #    strip `toolSummary` from tools whose schema does not allow it.
    if clean_name == "run_command" or "toolSummary" in props:
        if not isinstance(args.get("toolSummary"), str) or not args["toolSummary"].strip():
            args["toolSummary"] = f"Executing {clean_name}"
    else:
        args.pop("toolSummary", None)

    # 4. Backfill required `run_command` defaults when omitted by non-native models.
    if clean_name == "run_command" or "WaitMsBeforeAsync" in required:
        if "WaitMsBeforeAsync" not in args or args["WaitMsBeforeAsync"] is None:
            args["WaitMsBeforeAsync"] = 5000

    # 5. Strip unknown additional properties when explicit strict schema is provided.
    if is_strict and props and explicit_schema is not None:
        for k in list(args.keys()):
            if k not in props:
                args.pop(k, None)

    # 6. Coerce argument value types to match schema expectations.
    if props:
        for k, v in list(args.items()):
            expected_t = props.get(k)
            if expected_t:
                args[k] = _coerce_arg_value(v, expected_t, key_name=k)

    return args


# ──────────────────────────────────────────────────────────────────────────────
# SSE wire helpers — spec §5
# ──────────────────────────────────────────────────────────────────────────────

def sse_data(obj: Dict[str, Any]) -> bytes:
    """Encode a Gemini chunk object as a `data: <json>\n\n` SSE frame (no event: prefix)."""
    return b"data: " + json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n\n"


def build_response_headers(stream: bool) -> Dict[str, str]:
    """Response headers for the antigravity egress (spec PORTING NOTES B.7)."""
    base = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "Access-Control-Allow-Origin": "*",
    }
    if stream:
        base["Content-Type"] = "text/event-stream"
    else:
        base["Content-Type"] = "application/json"
    return base


# ──────────────────────────────────────────────────────────────────────────────
# Ingress: unwrap + detect + model normalize + Gemini→OpenAI (spec §2, §3, §6)
# ──────────────────────────────────────────────────────────────────────────────

def unwrap_request(body: Dict[str, Any]) -> Dict[str, Any]:
    """Unwrap the Cloud Code envelope. Returns the inner `request` object, or
    `body` itself when there is no wrapper (bare Gemini request)."""
    inner = body.get("request") if isinstance(body, dict) else None
    if isinstance(inner, dict):
        return inner
    return body if isinstance(body, dict) else {}


def is_antigravity(body: Dict[str, Any]) -> bool:
    """Detect an antigravity (Cloud Code) request: userAgent=="antigravity" with
    contents present, falling back to a bare contents array (§1)."""
    if not isinstance(body, dict):
        return False
    inner = body.get("request")
    target = inner if isinstance(inner, dict) else body
    if body.get("userAgent") == "antigravity" and target.get("contents"):
        return True
    # Fall back to bare contents (no wrapper but Gemini-shaped body).
    if isinstance(inner, dict) and inner.get("contents"):
        return False
    return bool(body.get("contents")) and body.get("userAgent") == "antigravity"


def normalize_model(antigravity_id: str) -> str:
    """Map a Google-Cloud-Code model id to a BSL canonical id (§6).

    Synonym map → regex cascade → passthrough. `tab[_-]*` ids bypass mapping.
    BSL's own routing/aliases resolve the returned id further.
    """
    if not antigravity_id:
        return antigravity_id
    if _TAB_RE.match(antigravity_id):
        return antigravity_id
    if antigravity_id in MODEL_SYNONYMS:
        return MODEL_SYNONYMS[antigravity_id]
    for pattern, alias in MODEL_PATTERNS:
        if pattern.search(antigravity_id):
            return alias
    return antigravity_id


def _join_system_text(system_instruction: Any) -> str:
    """Join the text parts of a Gemini systemInstruction (may be dict or str)."""
    if isinstance(system_instruction, str):
        return system_instruction
    if isinstance(system_instruction, dict):
        parts = system_instruction.get("parts") or []
        return "".join(
            p.get("text", "") for p in parts if isinstance(p, dict)
        )
    return ""


MAX_TOOL_CALL_ID_LEN = 64


def bound_tool_call_id(call_id: Any, max_len: int = MAX_TOOL_CALL_ID_LEN) -> Any:
    """Deterministically bound a tool_call_id / call_id to ``<= max_len`` (64) chars.

    OpenAI Chat Completions and Responses API (`codex/*`, `openai/*`, etc.) reject
    any `call_id` / `tool_call_id` longer than 64 characters with:
      `400 Invalid 'input[N].call_id': string too long. Expected a string with
       maximum length 64, but got a string with length 70 instead (string_above_max_length)`.
    Using a deterministic prefix + 12-char SHA-1 suffix ensures that matching
    assistant `tool_calls[].id` (`function_call.call_id`) and tool-role
    `tool_call_id` (`function_call_output.call_id`) always map to the exact same
    64-char ID without collisions.
    """
    if not isinstance(call_id, str) or len(call_id) <= max_len:
        return call_id
    import hashlib
    digest = hashlib.sha1(call_id.encode("utf-8", errors="ignore")).hexdigest()[:12]
    keep = max(1, max_len - 1 - len(digest))
    return f"{call_id[:keep]}_{digest}"


def _mint_tool_call_id(name: str, seq: int, max_len: int = MAX_TOOL_CALL_ID_LEN) -> str:
    """Mint a unique `call_<name>_<seq>` ID guaranteed to be `<= max_len` (64) chars."""
    raw = f"call_{name}_{seq}"
    if len(raw) <= max_len:
        return raw
    return bound_tool_call_id(raw, max_len=max_len)


_SCHEMA_INT_KEYS = frozenset({
    "minLength", "maxLength",
    "minItems", "maxItems",
    "minProperties", "maxProperties",
    "minContains", "maxContains",
})

_SCHEMA_NUM_KEYS = frozenset({
    "minimum", "maximum",
    "exclusiveMinimum", "exclusiveMaximum",
    "multipleOf",
})

_SCHEMA_MAP_CONTAINERS = frozenset({
    "properties", "$defs", "definitions", "patternProperties", "dependentSchemas",
})


def _coerce_schema_int(val: Any) -> Any:
    if isinstance(val, bool):
        return val
    if isinstance(val, int):
        return val
    if isinstance(val, float) and val.is_integer():
        return int(val)
    if isinstance(val, str):
        s = val.strip()
        if s and (s.isdigit() or (s[0] in "+-" and s[1:].isdigit())):
            try:
                return int(s)
            except ValueError:
                return val
    return val


def _coerce_schema_num(val: Any) -> Any:
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return val
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return val
        if s.isdigit() or (s[0] in "+-" and s[1:].isdigit()):
            try:
                return int(s)
            except ValueError:
                pass
        try:
            f = float(s)
            return int(f) if f.is_integer() and "." not in s and "e" not in s.lower() else f
        except ValueError:
            return val
    return val


def coerce_json_schema_numerics(value: Any) -> Any:
    """Recursively coerce ProtoJSON-stringified integer/number keywords in a JSON Schema
    (e.g. `"minLength": "1"` -> `1`) without mutating input or altering other keys."""
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for key, child in value.items():
            if key in _SCHEMA_MAP_CONTAINERS and isinstance(child, dict):
                out[key] = {
                    prop_name: coerce_json_schema_numerics(prop_schema)
                    for prop_name, prop_schema in child.items()
                }
            elif key in _SCHEMA_INT_KEYS:
                out[key] = _coerce_schema_int(child)
            elif key in _SCHEMA_NUM_KEYS:
                out[key] = _coerce_schema_num(child)
            else:
                out[key] = coerce_json_schema_numerics(child)
        return out
    if isinstance(value, list):
        return [coerce_json_schema_numerics(item) for item in value]
    return value


def _sanitize_parameters(params: Any) -> Dict[str, Any]:
    """Sanitize a functionDeclaration `parameters` schema for OpenAI tools:
    lowercase schema `type` strings recursively, drop `enumDescriptions`, and
    synthesize a reason-explanation schema when none is present (§3, §8.7)."""
    if not isinstance(params, dict) or not params:
        return {
            "type": "object",
            "properties": {
                "reason": {"type": "string", "description": "Brief explanation"}
            },
            "required": ["reason"],
        }

    def _sanitize_schema(value: Any) -> Any:
        if isinstance(value, dict):
            sanitized = {}
            for key, child in value.items():
                if key in _SCHEMA_MAP_CONTAINERS and isinstance(child, dict):
                    sanitized[key] = {
                        prop_name: _sanitize_schema(prop_schema)
                        for prop_name, prop_schema in child.items()
                    }
                    continue
                if key in _SCHEMA_INT_KEYS:
                    sanitized[key] = _coerce_schema_int(child)
                    continue
                if key in _SCHEMA_NUM_KEYS:
                    sanitized[key] = _coerce_schema_num(child)
                    continue
                if key == "enumDescriptions":
                    continue
                if key == "type" and isinstance(child, str):
                    sanitized[key] = child.lower()
                elif key == "type" and isinstance(child, list):
                    sanitized[key] = [
                        item.lower() if isinstance(item, str) else _sanitize_schema(item)
                        for item in child
                    ]
                else:
                    sanitized[key] = _sanitize_schema(child)
            return sanitized
        if isinstance(value, list):
            return [_sanitize_schema(item) for item in value]
        return value

    return _sanitize_schema(params)


def gemini_request_to_openai(req: Dict[str, Any], model: str) -> Dict[str, Any]:
    """Translate a Gemini (Cloud Code) request into an OpenAI ChatCompletion dict.

    Implements spec §3 table + §8 edge cases. The returned dict is consumed by
    `_process_chat_completion`; BSL's own thinking patch re-applies per-provider
    thinking downstream, so the `reasoning_effort` hint here is advisory.
    """
    req = req if isinstance(req, dict) else {}
    openai_body: Dict[str, Any] = {
        "model": model,
        "messages": [],
        "stream": True,  # antigravity streams by default (§8.12); route overrides
    }

    # ── systemInstruction → system message ──────────────────────────────
    sys_text = _join_system_text(req.get("systemInstruction"))
    if sys_text:
        openai_body["messages"].append({"role": "system", "content": sys_text})

    # ── generationConfig → scalars ──────────────────────────────────────
    gc = req.get("generationConfig") or {}
    if isinstance(gc, dict):
        if "maxOutputTokens" in gc:
            try:
                openai_body["max_tokens"] = min(int(gc["maxOutputTokens"]), _MAX_OUTPUT_TOKENS_CAP)
            except (TypeError, ValueError):
                pass
        if "temperature" in gc and gc["temperature"] is not None:
            openai_body["temperature"] = gc["temperature"]
        if "topP" in gc and gc["topP"] is not None:
            openai_body["top_p"] = gc["topP"]
        if "topK" in gc and gc["topK"] is not None:
            openai_body["top_k"] = gc["topK"]  # non-standard but preserved (§3)
        tc = gc.get("thinkingConfig")
        if isinstance(tc, dict) and "thinkingBudget" in tc:
            openai_body["reasoning_effort"] = _budget_to_effort(tc["thinkingBudget"])

    # ── tools → OpenAI tools + VALIDATED toolConfig ─────────────────────
    openai_tools: list = []
    for tool in (req.get("tools") or []):
        if not isinstance(tool, dict):
            continue
        for decl in (tool.get("functionDeclarations") or []):
            if not isinstance(decl, dict):
                continue
            _tool_name = decl.get("name")
            if not isinstance(_tool_name, str) or not _tool_name.strip():
                continue
            openai_tools.append({
                "type": "function",
                "function": {
                    "name": _tool_name.strip(),
                    "description": decl.get("description", ""),
                    "parameters": _sanitize_parameters(
                        decl.get("parameters") if decl.get("parameters") is not None else decl.get("parametersJsonSchema")
                    ),
                },
            })
    if openai_tools:
        openai_body["tools"] = openai_tools
        # §8.6: VALIDATED mode only asserted when tools are present.
        tc = req.get("toolConfig")
        mode = None
        if isinstance(tc, dict):
            fcc = tc.get("functionCallingConfig")
            if isinstance(fcc, dict):
                mode = fcc.get("mode")
        openai_body["tool_choice"] = "auto"  # OpenAI-side equivalent; mode VALIDATED recorded below
        # Carry the validated-mode intent so callers/tests can assert it.
        openai_body["x_gemini_tool_mode"] = mode or "VALIDATED"
    # safetySettings are intentionally stripped (§8.5).

    # ── Tool-call id uniqueness (root-cause fix for pix4k 502 / bare-origin 400) ──
    # The Gemini wire carries NO per-call id: functionCall/functionResponse pair
    # by position+name. Minting ids as f"call_{name}" collapses every invocation
    # of the same tool onto ONE id, so a long agent session emits e.g. 30 tool_use
    # blocks all id="call_run_command". Anthropic REQUIRES globally-unique
    # tool_use.id; the duplicate ids make the /v1/messages body malformed, which
    # Cloudflare-fronted pix4k returns as a 502 HTML page and bare origins (Grok/
    # Qwen) return as a 400. Mint a unique id per call and positionally pair each
    # functionResponse to it via this FIFO queue.
    _tool_call_seq = 0
    _pending_calls: list = []  # FIFO of (name, call_id) awaiting a functionResponse
    _req_tool_schemas = extract_openai_tool_schemas(openai_tools)

    def _flush_orphaned_pending_calls() -> None:
        while _pending_calls:
            _orphan_name, _orphan_id = _pending_calls.pop(0)
            openai_body["messages"].append({
                "role": "tool",
                "tool_call_id": _orphan_id,
                "content": json.dumps({
                    "error": f"Tool call '{_orphan_name}' was skipped or failed client validation."
                }),
            })

    # ── contents → messages ─────────────────────────────────────────────
    _raw_contents = [c for c in (req.get("contents") or []) if isinstance(c, dict)]
    for _ci, content in enumerate(_raw_contents):
        role = content.get("role", "user")
        # §8.1: a content carrying functionResponse is re-roled to user.
        parts = content.get("parts") or []
        has_fn_response = any(
            isinstance(p, dict) and "functionResponse" in p for p in parts
        )
        if has_fn_response:
            openai_role = "user"
        elif role == "model":
            openai_role = "assistant"
        else:
            openai_role = role  # "user" → user, else passthrough

        # If a previous model turn emitted functionCall(s) that were never paired
        # with a functionResponse (e.g. parallel batch aborted on client validation
        # error), synthesize tool-role error responses before the next turn starts
        # so strict upstreams (Codex Responses, Anthropic, OpenAI) never reject the
        # history with `400 No tool output found for function call ...`.
        if not has_fn_response and _pending_calls:
            _flush_orphaned_pending_calls()

        msg: Dict[str, Any] = {"role": openai_role}
        text_parts: list = []
        image_parts: list = []
        tool_calls: list = []
        tool_results: list = []
        reasoning_parts: list = []

        for part in parts:
            if not isinstance(part, dict):
                continue

            if "functionCall" in part:
                fc = part["functionCall"] or {}
                name = fc.get("name") if isinstance(fc, dict) else None
                if not isinstance(name, str) or not name.strip():
                    continue
                name = _sanitize_tool_name(name.strip(), _req_tool_schemas)
                if not name:
                    continue
                args = fc.get("args") or fc.get("arguments") or {}
                if isinstance(args, dict):
                    args = _inject_tool_metadata(args, name, _req_tool_schemas)
                # Preserve explicit non-empty functionCall.id (or part.id) when
                # present, bounded to <=64 chars; otherwise mint a globally
                # unique id per invocation.
                _explicit_fc_id = (
                    (fc.get("id") if isinstance(fc, dict) else None)
                    or part.get("id")
                )
                if isinstance(_explicit_fc_id, str) and _explicit_fc_id.strip():
                    _call_id = bound_tool_call_id(_explicit_fc_id.strip())
                else:
                    _tool_call_seq += 1
                    _call_id = _mint_tool_call_id(name, _tool_call_seq)
                _tc: Dict[str, Any] = {
                    "id": _call_id,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(args),
                    },
                }
                # thoughtSignature capture (Gemini 3.1-Pro 400 fix, 2026-09-04):
                # Gemini validates that echoed functionCall parts carry their
                # original signature. Carry it inline on the OpenAI tool_call so
                # the antigravity egress builder can re-emit it upstream; the
                # egress pop zone strips it on non-antigravity lanes.
                _sig = part.get("thoughtSignature")
                if isinstance(_sig, str) and _sig:
                    _tc["thought_signature"] = _sig
                tool_calls.append(_tc)
                _pending_calls.append((name, _call_id))
                continue

            # functionResponse → OpenAI tool-role message (§3)
            if "functionResponse" in part:
                fr = part["functionResponse"] or {}
                name = fr.get("name", "") if isinstance(fr, dict) else ""
                if isinstance(name, str) and name.strip():
                    name = _sanitize_tool_name(name.strip(), _req_tool_schemas) or name.strip()
                resp = fr.get("response") if isinstance(fr, dict) else None
                result = resp.get("result", resp) if isinstance(resp, dict) else resp
                # Pair by explicit functionResponse.id (or part.id) FIRST when present,
                # then by NAME (oldest pending call with the same tool name),
                # then fall back to global FIFO, then to a fresh unique id.
                _explicit_fr_id = (
                    (fr.get("id") if isinstance(fr, dict) else None)
                    or part.get("id")
                )
                _matched_id = None
                if isinstance(_explicit_fr_id, str) and _explicit_fr_id.strip():
                    _bounded_fr_id = bound_tool_call_id(_explicit_fr_id.strip())
                    for _qi, (_qname, _qid) in enumerate(_pending_calls):
                        if _qid == _bounded_fr_id:
                            _matched_id = _qid
                            _pending_calls.pop(_qi)
                            break
                    if _matched_id is None:
                        _matched_id = _bounded_fr_id
                if _matched_id is None:
                    for _qi, (_qname, _qid) in enumerate(_pending_calls):
                        if _qname == name:
                            _matched_id = _qid
                            _pending_calls.pop(_qi)
                            break
                if _matched_id is None and _pending_calls:
                    _matched_id = _pending_calls.pop(0)[1]
                if _matched_id is None:
                    _tool_call_seq += 1
                    _matched_id = _mint_tool_call_id(name, _tool_call_seq)
                _tr: Dict[str, Any] = {
                    "role": "tool",
                    "tool_call_id": _matched_id,
                    "content": json.dumps(result),
                }
                # thoughtSignature capture on functionResponse (Gemini 3.1-Pro
                # 400 fix, 2026-09-04): the IDE can echo signatures on response
                # parts too. Carry it inline on the tool-role message so the
                # antigravity egress builder can re-emit it; other lanes strip it.
                _rsig = part.get("thoughtSignature")
                if isinstance(_rsig, str) and _rsig:
                    _tr["thought_signature"] = _rsig
                tool_results.append(_tr)
                continue

            # inlineData → image_url data URI (§3)
            if "inlineData" in part:
                idata = part["inlineData"] or {}
                mime = idata.get("mimeType") or "image/png"
                data = idata.get("data", "")
                image_parts.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{data}"},
                })
                continue

            # fileData → image_url remote URI (§3; GAP fix 2026-08-24).
            # Remote files (gs://, https://) were silently DROPPED before —
            # Vision requests with remote URIs reached upstream image-less.
            if "fileData" in part:
                fdata = part["fileData"] or {}
                uri = fdata.get("fileUri") or fdata.get("uri") or ""
                if isinstance(uri, str) and uri:
                    image_parts.append({
                        "type": "image_url",
                        "image_url": {"url": uri},
                    })
                continue

            # thought:true (+ text) → reasoning_content channel (§3)
            if part.get("thought") is True:
                # §8.2: pure-thought parts (no text/fn) are dropped on request.
                if part.get("text"):
                    reasoning_parts.append(part.get("text"))
                # thoughtSignature dropped unless with fn/text (handled above/below).
                continue

            # plain text / thoughtSignature-with-text (§8.3: keep text)
            if "text" in part and part["text"] is not None:
                text_parts.append(part["text"])
                continue

        # §Ordering: tool-result messages MUST follow the assistant message that
        # issued the call.  The Gemini wire puts functionCall in model-role and
        # functionResponse in the NEXT user-role content block, so tool_results
        # only ever accumulates on a pure-functionResponse block (has_fn_response=True).
        # In that case we skip the trailing empty message and only emit the tool
        # messages — which naturally come after the previously-appended assistant turn.
        if has_fn_response:
            for tr in tool_results:
                openai_body["messages"].append(tr)
            # If the next content block does NOT carry additional functionResponse
            # parts for the same batch, any remaining _pending_calls were skipped
            # by the IDE (e.g. when the first parallel call failed schema validation).
            _next_has_fn_resp = (
                (_ci + 1 < len(_raw_contents))
                and any(
                    isinstance(p, dict) and "functionResponse" in p
                    for p in (_raw_contents[_ci + 1].get("parts") or [])
                )
            )
            if not _next_has_fn_resp and _pending_calls:
                _flush_orphaned_pending_calls()
            # If the client packed follow-up user text or images into the same
            # content block after functionResponse, preserve them as a user message.
            if text_parts or image_parts:
                _follow_msg: Dict[str, Any] = {"role": "user"}
                if image_parts:
                    _clist = []
                    if text_parts:
                        _clist.append({"type": "text", "text": "\n".join(text_parts)})
                    _clist.extend(image_parts)
                    _follow_msg["content"] = _clist
                else:
                    _follow_msg["content"] = "\n".join(text_parts)
                openai_body["messages"].append(_follow_msg)
            continue

        # Assemble the main message for this content block.
        if openai_role == "tool":
            # A bare tool-role content (rare) — fold text into content.
            msg["content"] = "\n".join(text_parts) if text_parts else ""
            openai_body["messages"].append(msg)
            continue

        if image_parts:
            content_list = []
            if text_parts:
                content_list.append({"type": "text", "text": "\n".join(text_parts)})
            content_list.extend(image_parts)
            msg["content"] = content_list
        else:
            # GAP-4 FIX: use "" not None — Anthropic-format providers reject
            # content:null on assistant messages (1210 parameter error).
            msg["content"] = "\n".join(text_parts) if text_parts else ""

        if reasoning_parts:
            msg["reasoning_content"] = "\n".join(reasoning_parts)
        if tool_calls:
            msg["tool_calls"] = tool_calls
            # An assistant turn that is purely a tool call may have no text;
            # content is already "" from above.
        openai_body["messages"].append(msg)

    return openai_body


# ──────────────────────────────────────────────────────────────────────────────
# Egress: OpenAI → Gemini (spec §4)
# ──────────────────────────────────────────────────────────────────────────────

def _usage_metadata(usage: Dict[str, Any]) -> Dict[str, Any]:
    """Map OpenAI/Anthropic usage → Gemini usageMetadata (§4a, §8.14)."""
    usage = usage or {}
    p = int(usage.get("prompt_tokens") or 0)
    if not p:
        p = (
            int(usage.get("input_tokens") or 0)
            + int(usage.get("cache_read_input_tokens") or 0)
            + int(usage.get("cache_creation_input_tokens") or 0)
        )
    q = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    meta: Dict[str, Any] = {
        "promptTokenCount": p,
        "candidatesTokenCount": q,
        "totalTokenCount": (p or 0) + (q or 0),
    }
    # §8.14: forward reasoning/cached counts when present.
    details = usage.get("completion_tokens_details") or {}
    if isinstance(details, dict) and details.get("reasoning_tokens"):
        meta["thoughtsTokenCount"] = details["reasoning_tokens"]
    pdetails = usage.get("prompt_tokens_details") if isinstance(usage.get("prompt_tokens_details"), dict) else {}
    idetails = usage.get("input_tokens_details") if isinstance(usage.get("input_tokens_details"), dict) else {}
    cached = int(
        pdetails.get("cached_tokens")
        or idetails.get("cached_tokens")
        or usage.get("cache_read_input_tokens")
        or usage.get("cachedContentTokenCount")
        or usage.get("prompt_cache_hit_tokens")
        or 0
    )
    if cached > 0:
        meta["cachedContentTokenCount"] = cached
    return meta


def _new_state() -> Dict[str, Any]:
    """Fresh per-stream translation state for openai_chunk_to_gemini."""
    return {
        # slot key -> {"id","name","args"}. Keys are homogeneous (str,int)
        # tuples so the finish flush can sorted() them without TypeError.
        "toolCallAccum": {},
        # call id -> slot key, for index-less streams.
        "toolCallIdKeys": {},
        # next sequence number for index-less slot allocation.
        "toolCallSeq": 0,
        # slot key currently receiving argument fragments.
        "toolCallLastKey": None,
        "responseId": None,
        "modelVersion": None,
        "usage": None,
    }


def openai_chunk_to_gemini(chunk: Dict[str, Any], state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Translate one OpenAI `chat.completion.chunk` into a Gemini chunk (§4a).

    Returns `{"response": <gemini_obj>}` or `None` for empty mid-stream deltas
    (translator emits nothing when there are no parts and no finish). The caller
    wraps `response` into a `data:` SSE frame via `sse_data`.

    Maintains per-stream `state` for tool-call accumulation and the
    responseId/modelVersion/usage carried until the finish chunk.
    """
    if not isinstance(chunk, dict):
        return None

    # Lazily initialize per-stream state so callers may pass a bare `{}`.
    if "toolCallAccum" not in state:
        state["toolCallAccum"] = {}
        state.setdefault("toolCallIdKeys", {})
        state.setdefault("toolCallSeq", 0)
        state.setdefault("toolCallLastKey", None)
        state.setdefault("responseId", None)
        state.setdefault("modelVersion", None)
        state.setdefault("usage", None)

    # Track stable ids for the whole response.
    if not state.get("responseId"):
        state["responseId"] = chunk.get("id") or f"resp_{int(time.time() * 1000)}"
    if not state.get("modelVersion"):
        state["modelVersion"] = chunk.get("model")

    usage = chunk.get("usage")
    if usage:
        state["usage"] = usage

    choices = chunk.get("choices") or []
    if not choices:
        # A usage-only chunk (no choices) — emit usageMetadata WITHOUT any
        # parts (2026-08-24): an empty text part trips the IDE's
        # "text and tool calls cannot both be empty" validator.
        if state.get("usage"):
            obj = _base_gemini_response(state, parts=[], finish_reason=None)
            if obj["candidates"]:
                obj["candidates"][0]["content"]["parts"] = []
            obj["usageMetadata"] = _usage_metadata(state["usage"])
            state["usage"] = None
            return {"response": obj}
        return None

    choice = choices[0] if isinstance(choices[0], dict) else {}
    delta = choice.get("delta") or {}
    finish = choice.get("finish_reason")

    parts: list = []

    # reasoning_content → {thought:true, text} (§4a)
    # Keep thought:true for Antigravity too — IDE expects the thought part shape.
    rc = delta.get("reasoning_content")
    if rc:
        parts.append({"thought": True, "text": rc})
        # Any tool_call slot already accumulated BEFORE subsequent reasoning_content
        # deltas was scraped out of the middle of `<thinking>` by upstream toolbridge
        # while the model was still thinking (quoting a prior failed call or template).
        for _existing_slot in state["toolCallAccum"].values():
            _existing_slot["from_reasoning"] = True

    # content → {text} (§4a)
    content = delta.get("content")
    if content:
        parts.append({"text": content})
        state["hasVisibleContent"] = True

    # tool_calls accumulate by index across chunks (§4a)
    #
    # ── INDEX-COLLAPSE FIX (2026-09-22) ─────────────────────────────────
    # The old key was `tc.get("index", 0)`. When upstream omits `index`
    # entirely, EVERY call in a parallel batch resolved to key 0, so
    # `slot["args"] += fn["arguments"]` concatenated N independent argument
    # objects into one buffer:  {"a":1}{"b":2}{"c":3}
    # That buffer then fails json.loads with "Extra data", the whole slot is
    # dropped, and a batch of N becomes 0 calls. This is the exact signature
    # of the "Extra data" drops seen in production logs.
    #
    # Keying precedence, first match wins:
    #   1. an explicit integer `index`          -> ("i", index)
    #   2. the call `id` (opening delta)        -> ("s", seq) allocated once
    #   3. neither (pure argument continuation) -> the slot opened most
    #                                             recently
    # Branch 3 is what keeps fragment streams coherent when upstream sends
    # only argument deltas after the opening frame.
    for tc in (delta.get("tool_calls") or []):
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        raw_idx = tc.get("index")
        if isinstance(raw_idx, int) and not isinstance(raw_idx, bool):
            existing = state["toolCallAccum"].get(("i", raw_idx))
            tc_id = tc.get("id")
            # INDEX-COLLISION GUARD (2026-10-15): an upstream that stamps the
            # SAME explicit index on every parallel call (the codex adapter
            # did exactly this pre-fix) merges N complete calls into one slot
            # here -- their arguments concatenate and the flush drops the
            # batch with "Extra data". When the slot is already claimed by a
            # DIFFERENT call id, allocate a fresh sequence key for the
            # newcomer instead of concatenating.
            if (
                existing is not None
                and tc_id
                and existing.get("id")
                and existing["id"] != tc_id
            ):
                key: Any = ("s", state.get("toolCallSeq", 0))
                state["toolCallSeq"] = state.get("toolCallSeq", 0) + 1
                state.setdefault("toolCallIdKeys", {})[tc_id] = key
            else:
                key = ("i", raw_idx)
            state["toolCallLastKey"] = key
        else:
            tc_id = tc.get("id")
            id_keys = state.setdefault("toolCallIdKeys", {})
            if tc_id and tc_id in id_keys:
                prior_slot = state["toolCallAccum"].get(id_keys[tc_id])
                # ID-COLLISION GUARD (2026-10-15): two DISTINCT calls sharing
                # one id (upstream id fallback) also concatenate here. A
                # different name on an args-bearing slot means a NEW call --
                # re-key it instead of appending.
                if (
                    prior_slot is not None
                    and prior_slot.get("args")
                    and fn.get("name")
                    and prior_slot.get("name")
                    and prior_slot["name"] != fn["name"]
                ):
                    key = ("s", state.get("toolCallSeq", 0))
                    state["toolCallSeq"] = state.get("toolCallSeq", 0) + 1
                    id_keys[tc_id] = key
                else:
                    key = id_keys[tc_id]
                state["toolCallLastKey"] = key
            elif tc_id:
                key = ("s", state.get("toolCallSeq", 0))
                state["toolCallSeq"] = state.get("toolCallSeq", 0) + 1
                id_keys[tc_id] = key
                state["toolCallLastKey"] = key
            else:
                # Argument-only continuation: attach to the open slot. Never
                # fall back to a shared default key -- that is what collapsed
                # the batch.
                key = state.get("toolCallLastKey")
                if key is None:
                    key = ("s", state.get("toolCallSeq", 0))
                    state["toolCallSeq"] = state.get("toolCallSeq", 0) + 1
                    state["toolCallLastKey"] = key

        slot = state["toolCallAccum"].get(key)
        if slot is None:
            slot = {"id": tc.get("id", ""), "name": fn.get("name", ""), "args": ""}
            state["toolCallAccum"][key] = slot
        else:
            if tc.get("id"):
                slot["id"] = tc["id"]
            if fn.get("name"):
                slot["name"] = fn["name"]
        if fn.get("arguments"):
            slot["args"] += fn["arguments"]
        # thought_signature carrier (Gemini 3.1-Pro 400 fix): snapshot the
        # inline signature so the finish flush below can re-emit it as a
        # camelCase sibling of functionCall on the Gemini frame.
        _tsig = tc.get("thought_signature")
        if isinstance(_tsig, str) and _tsig:
            slot["sig"] = _tsig

    # On finish: flush accumulated tool calls to functionCall parts.
    truncated_tool_call = False
    if finish:
        _tool_schemas = state.get("tool_schemas")
        _dedup_seen: set = set()
        _has_post_reasoning_calls = any(
            not _s.get("from_reasoning")
            and not _is_bogus_tool_call(
                _sanitize_tool_name(_s.get("name"), _tool_schemas) or "tool",
                {},
                _tool_schemas,
            )
            for _s in state["toolCallAccum"].values()
        )
        for idx in sorted(state["toolCallAccum"].keys()):
            slot = state["toolCallAccum"][idx]
            # If upstream toolbridge scraped a quoted `<tool_use>` out of mid-`<thinking>`
            # and the model subsequently emitted real post-thinking tool calls (or visible
            # content), discard the mid-thinking phantom call.
            if slot.get("from_reasoning") and (_has_post_reasoning_calls or state.get("hasVisibleContent")):
                continue
            name = _sanitize_tool_name(slot["name"], _tool_schemas) or "tool"
            raw_args = slot["args"]
            try:
                args = json.loads(raw_args) if raw_args else {}
            except (json.JSONDecodeError, TypeError) as e:
                # ── DROP-SYNDROME FIX (2026-09-22) ────────────────────────
                # ROOT CAUSE of "GLM loses tool calls from a batch". A model
                # streams each call's arguments as many small deltas; this
                # function reassembles them and parses once at finish. If the
                # reassembled buffer does not parse, the old code `continue`d
                # — DELETING that call from the batch. The IDE then received
                # N-1 functionCall parts plus a forced MAX_TOKENS, i.e. a
                # batch missing members and a model reporting truncation.
                #
                # It looked model-specific because it scales with argument
                # fragmentation: GLM averages 8.6 arg-deltas per call vs
                # Qwen's 4.5 (measured), so GLM presents ~2x the reassembly
                # surface. Severity ordering (GLM > Sonnet/Opus > Qwen/Kimi)
                # tracks fragmentation density, not model capability.
                #
                # It went undetected because the ANTHROPIC lane has repaired
                # this since day one (stream_normalizer.py::_repair_tool_input)
                # while this Gemini lane — the only one the Antigravity IDE
                # actually speaks — never called any repair.
                #
                # Repair first; DROP only what is truly unrecoverable, so
                # MAX_TOKENS remains an honest signal and we never invent a
                # call the model did not make.
                repaired_args = _repair_tool_args(raw_args)
                if repaired_args is None:
                    # EXTRA-DATA SPLIT (2026-10-15): when parallel calls were
                    # merged into one slot by an index/id collision upstream
                    # (see the accumulator guards), the buffer holds N
                    # COMPLETE JSON documents concatenated -- "Extra data"
                    # fails exactly at the first document's end. The ladder
                    # cannot express that (each document is valid JSON), so
                    # split instead of drop: the batch survives and MAX_TOKENS
                    # stays reserved for genuine truncation. All split parts
                    # carry the slot's name -- the merged-away members' names
                    # are unrecoverable, but dropping recoverable arguments
                    # is what error-looped the IDE (2026-10-15 codex report).
                    split_docs = _split_tool_args_documents(raw_args)
                    if split_docs:
                        _TOOL_ARG_STATS["split"] += 1
                        print(
                            f"[BSL ROUTER] Tool args SPLIT for {name}: "
                            f"{len(split_docs)} concatenated documents recovered "
                            f"(was: {e}). Calls preserved instead of dropped.",
                            flush=True,
                        )
                        for _doc in split_docs:
                            _doc_parsed = json.loads(_doc)
                            if _is_bogus_tool_call(name, _doc_parsed, _tool_schemas):
                                continue
                            _doc_args = _inject_tool_metadata(_doc_parsed, name, _tool_schemas)
                            _fc_part_split: Dict[str, Any] = {
                                "functionCall": {"name": name, "args": _doc_args}
                            }
                            _tsig_split = slot.get("sig")
                            if isinstance(_tsig_split, str) and _tsig_split:
                                _fc_part_split["thoughtSignature"] = _tsig_split
                            parts.append(_fc_part_split)
                        continue
                    _TOOL_ARG_STATS["dropped"] += 1
                    print(
                        f"[BSL ROUTER] Tool args UNRECOVERABLE for {name}: {e}. "
                        f"Dropping malformed call, signaling MAX_TOKENS. "
                        f"Raw args ({len(raw_args)} chars): {raw_args[:200]!r}",
                        flush=True,
                    )
                    truncated_tool_call = True
                    continue
                _TOOL_ARG_STATS["repaired"] += 1
                print(
                    f"[BSL ROUTER] Tool args REPAIRED for {name} "
                    f"({len(raw_args)} chars, {e.__class__.__name__}): call preserved "
                    f"instead of dropped from the batch.",
                    flush=True,
                )
                args = repaired_args
            if _is_bogus_tool_call(name, args, _tool_schemas):
                continue
            args = _inject_tool_metadata(args, name, _tool_schemas)
            if state.get("is_antigravity") or _tool_schemas:
                try:
                    _dedup_key = (name, json.dumps(args, sort_keys=True))
                except Exception:
                    _dedup_key = None
                if _dedup_key is not None:
                    if _dedup_key in _dedup_seen:
                        continue
                    _dedup_seen.add(_dedup_key)
            _fc_part: Dict[str, Any] = {"functionCall": {"name": name, "args": args}}
            _tsig = slot.get("sig")
            if isinstance(_tsig, str) and _tsig:
                # Re-emit the captured signature: the IDE stores the signed
                # call in history, so its next-turn functionCall echo passes
                # Gemini 3.1-Pro's signature validation (400 fix).
                _fc_part["thoughtSignature"] = _tsig
            parts.append(_fc_part)
        # §8.8 (FORCE-STOP FIX 2026-08-24, v3): a parts-less finish must NOT
        # carry the notice text downstream (that shipped a fake finishReason=STOP
        # whose only content was the notice — rendered as force-stop while combo
        # fallback never fired). But it also must NOT return None — finish frames
        # carry finishReason and usageMetadata that the client needs. Emit a
        # minimal empty-text part: valid Gemini protocol, but gemini_frame_has_content
        # returns False for empty text, so combo fallback still fires correctly.
        if not parts:
            parts.append({"text": ""})

    # §4a: emit nothing for empty mid-stream deltas (no parts, no finish).
    if not parts and not finish:
        return None

    finish_reason = _FINISH_MAP.get(finish, "STOP") if finish else None
    # A truncated tool call must surface as MAX_TOKENS regardless of the upstream
    # finish_reason (which may falsely claim tool_calls/stop), so the IDE does not
    # try to execute an argument-less call and instead treats it as a token-limit cutoff.
    if truncated_tool_call:
        finish_reason = "MAX_TOKENS"
    obj = _base_gemini_response(state, parts=parts, finish_reason=finish_reason)

    # Usage rides on whichever chunk carries it — usually the finish chunk.
    if state.get("usage"):
        obj["usageMetadata"] = _usage_metadata(state["usage"])
        state["usage"] = None

    return {"response": obj}


def gemini_frame_has_content(frame: Dict[str, Any]) -> bool:
    """True only if a Gemini frame carries something the CLIENT CAN RENDER.

    BUG L (2026-08-04). This exists to answer one specific question correctly:
    "if we now fail over to another provider, would the user see a corrupted
    transcript?" Only rendered content can be corrupted, so only rendered
    content may disable fallback.

    NOT content (returns False):
      * usage-only chunks - `openai_chunk_to_gemini` emits parts without any
        text part purely to carry `usageMetadata` (see the `not choices`
        branch above).
      * a finish chunk whose tool calls were all dropped - §8.8 forces the
        synthetic notice part to keep `parts` non-empty.
      * the §8.8 synthetic no-output notice itself - it is router-generated
        text, not model output. Counting it would block combo fallback on
        exactly the zero-output finishes we must fail over from.
      * empty-string text or thought deltas.

    IS content (returns True): non-empty text (except the synthetic notice),
    non-empty thought text, functionCall, inlineData/fileData.

    A `finishReason` alone is deliberately NOT content: it ends the stream but
    renders nothing, and treating it as content would re-block fallback on
    exactly the zero-output finishes we need to fail over from.
    """
    if not isinstance(frame, dict):
        return False
    inner = frame.get("response") if "response" in frame else frame
    if not isinstance(inner, dict):
        return False
    for cand in inner.get("candidates") or []:
        if not isinstance(cand, dict):
            continue
        content = cand.get("content")
        if not isinstance(content, dict):
            continue
        for part in content.get("parts") or []:
            if not isinstance(part, dict):
                continue
            # Any non-empty text counts, whether or not it is a thought: the
            # IDE renders thoughts in the reasoning pane, so splicing a second
            # provider after one would still be visible corruption. The
            # synthetic §8.8 notice is the one deliberate exception.
            if isinstance(part.get("text"), str) and part["text"] != "":
                if part["text"] == BSL_NO_OUTPUT_NOTICE:
                    continue
                return True
            if part.get("functionCall") or part.get("inlineData") or part.get("fileData"):
                return True
    return False


def gemini_frame_is_thought_only(frame: Dict[str, Any]) -> bool:
    """True only if EVERY part of the frame is a thought (reasoning) part.

    BUG N (2026-08-13). Pairs with `gemini_frame_has_content` at the Gemini
    egress pre-render buffer (main.py). A thought-only frame renders in the
    IDE's reasoning pane but nothing into the transcript body. While the
    stream has produced ONLY such frames (plus scaffolding), abandoning the
    leaf and failing over leaves the client with an incomplete reasoning pane
    and no body text — recoverable. The moment a visible-text or tool frame
    exists, the transcript is committed and fallback is forbidden.

    Classification is conservative in the SAFE direction: anything that is not
    provably a thought part (text, functionCall, inlineData, usage metadata,
    finish frames, malformed shapes) returns False so the buffer flushes and
    commits. A misclassified thought frame merely commits one fallback earlier
    than optimal; a misclassified visible frame would permit the transcript
    corruption this module exists to prevent.

    Frames with no content at all (usage-only, finish-with-empty-parts) return
    False: they are not thoughts, and the buffer only ever holds thought frames
    — they pass straight through unbuffered and uncommitted.
    """
    if not isinstance(frame, dict):
        return False
    inner = frame.get("response") if "response" in frame else frame
    if not isinstance(inner, dict):
        return False
    found = False
    for cand in inner.get("candidates") or []:
        if not isinstance(cand, dict):
            return False
        content = cand.get("content")
        if not isinstance(content, dict):
            return False
        for part in content.get("parts") or []:
            if not isinstance(part, dict):
                return False
            found = True
            if part.get("thought") is not True:
                return False
            if not isinstance(part.get("text"), str) or part["text"] == "":
                return False
            if part.get("functionCall") or part.get("inlineData") or part.get("fileData"):
                return False
    return found


def _base_gemini_response(state: Dict[str, Any], parts: list, finish_reason: Optional[str]) -> Dict[str, Any]:
    """Build the inner Gemini chunk object (§4a). Caller wraps as {"response": obj}."""
    candidate: Dict[str, Any] = {
        "content": {"role": "model", "parts": parts},
    }
    if finish_reason:
        candidate["finishReason"] = finish_reason
    obj: Dict[str, Any] = {
        "candidates": [candidate],
        "modelVersion": state.get("modelVersion"),
        "responseId": state.get("responseId"),
    }
    return obj


def openai_response_to_gemini(
    openai_resp: Dict[str, Any],
    model: str,
    tool_schemas: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Render a non-streaming OpenAI completion as a single Gemini response (§4b).

    Returns the wrapped `{"response": {candidates, usageMetadata, modelVersion, responseId}}`.
    """
    openai_resp = openai_resp or {}
    choices = openai_resp.get("choices") or []
    choice = choices[0] if choices and isinstance(choices[0], dict) else {}
    message = choice.get("message") or {} if isinstance(choice, dict) else {}

    parts: list = []
    text = message.get("content")
    if text:
        parts.append({"text": text})

    # Tool calls → functionCall parts (§4b note).
    for tc in (message.get("tool_calls") or []):
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        fn_name = fn.get("name") if isinstance(fn, dict) else None
        # Guard: skip nameless calls — a functionCall with an empty name is
        # rejected by Gemini-side validators (mirror of L1/L2 guards above).
        if not isinstance(fn_name, str) or not fn_name.strip():
            continue
        fn_name = _sanitize_tool_name(fn_name.strip(), tool_schemas)
        if not fn_name:
            continue
        raw_args = fn.get("arguments", "{}")
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
        except (json.JSONDecodeError, TypeError):
            # DROP-SYNDROME FIX (2026-09-22), non-streaming twin. Same missing
            # repair as the streaming flush, different symptom: substituting
            # args={} ships a call with no arguments, which the IDE reports as
            # TOOL_CALL_INCOMPLETE. Repair via the shared ladder first; fall
            # back to {} only when the fragment is unrecoverable.
            repaired_args = _repair_tool_args(raw_args if isinstance(raw_args, str) else "")
            if repaired_args is not None:
                _TOOL_ARG_STATS["repaired"] += 1
                args = repaired_args
            else:
                _TOOL_ARG_STATS["dropped"] += 1
                args = {}
        if _is_bogus_tool_call(fn_name, args, tool_schemas):
            continue
        args = _inject_tool_metadata(args, fn_name, tool_schemas)
        _fc_part: Dict[str, Any] = {"functionCall": {"name": fn_name, "args": args}}
        # thought_signature twin (Gemini 3.1-Pro 400 fix): re-emit the inline
        # signature as a camelCase sibling on the non-stream part as well.
        _tsig = tc.get("thought_signature")
        if isinstance(_tsig, str) and _tsig:
            _fc_part["thoughtSignature"] = _tsig
        parts.append(_fc_part)

    if not parts:
        # §8.8 (2026-08-24) non-stream twin: the IDE hard-rejects
        # empty text + empty tool calls ("...these cannot both be empty").
        parts.append({"text": BSL_NO_OUTPUT_NOTICE})

    finish = choice.get("finish_reason") if isinstance(choice, dict) else None
    finish_reason = _FINISH_MAP.get(finish, "STOP") if finish else "STOP"

    candidate = {
        "content": {"role": "model", "parts": parts},
        "finishReason": finish_reason,
        "index": 0,
    }

    obj: Dict[str, Any] = {
        "candidates": [candidate],
        "usageMetadata": _usage_metadata(openai_resp.get("usage") or {}),
        "modelVersion": model or openai_resp.get("model", ""),
        "responseId": openai_resp.get("id") or f"resp_{int(time.time() * 1000)}",
    }
    return {"response": obj}


