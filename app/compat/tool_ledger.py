"""
BSL Router Agent Compatibility Layer — Tool Ledger

Phase 1 (remaining): Per-request tool call/result validation.

Prevents protocol corruption across turns by tracking:
- tool_use IDs emitted by assistant
- tool_result IDs returned by user
- Orphaned results (no matching tool_use)
- Missing results (tool_use with no tool_result)

For Claude Code, the invariant is:
  every tool_result.tool_use_id must match exactly one previous assistant tool_use.id
"""
from typing import Dict, List, Set, Optional, Any
from dataclasses import dataclass
import hashlib
import json


@dataclass
class ToolLedgerEntry:
    tool_use_id: str
    tool_name: str
    input_hash: str
    status: str = "open"  # open | resolved | errored | orphaned
    result_hash: Optional[str] = None


class ToolLedger:
    """
    Request-local tool ledger. Validates tool_use/tool_result pairing.

    Usage:
        ledger = ToolLedger()
        ledger.scan_inbound_messages(messages)
        issues = ledger.validate()
        if issues:
            # log or reject
    """

    def __init__(self):
        self.entries: Dict[str, ToolLedgerEntry] = {}  # tool_use_id -> entry
        self._resolved_ids: Set[str] = set()

    def register_tool_use(self, tool_use_id: str, tool_name: str, tool_input: Any) -> None:
        """Register a tool_use block emitted by the assistant."""
        input_str = json.dumps(tool_input, sort_keys=True) if tool_input else ""
        input_hash = hashlib.md5(input_str.encode()).hexdigest()[:16]

        self.entries[tool_use_id] = ToolLedgerEntry(
            tool_use_id=tool_use_id,
            tool_name=tool_name,
            input_hash=input_hash,
            status="open",
        )

    def register_tool_result(self, tool_use_id: str, result_content: Any) -> None:
        """Register a tool_result block returned by the user."""
        result_str = str(result_content) if result_content else ""
        result_hash = hashlib.md5(result_str.encode()).hexdigest()[:16]

        if tool_use_id in self.entries:
            entry = self.entries[tool_use_id]
            entry.status = "resolved"
            entry.result_hash = result_hash
            self._resolved_ids.add(tool_use_id)
        else:
            # Orphaned tool_result — no matching tool_use
            self.entries[f"orphan_{tool_use_id}"] = ToolLedgerEntry(
                tool_use_id=tool_use_id,
                tool_name="unknown_orphan",
                input_hash="",
                status="orphaned",
                result_hash=result_hash,
            )

    def scan_inbound_messages(self, messages: List[Dict[str, Any]]) -> None:
        """
        Scan inbound messages and register all tool_use/tool_result blocks.

        Handles both Anthropic content-block format and OpenAI tool-call format.
        """
        for msg in messages:
            if not isinstance(msg, dict):
                continue

            role = msg.get("role", "")
            content = msg.get("content")

            # Anthropic format: assistant with content blocks
            if role == "assistant" and isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use":
                        self.register_tool_use(
                            block.get("id", ""),
                            block.get("name", ""),
                            block.get("input", {}),
                        )

            # Anthropic format: user with tool_result blocks
            if role == "user" and isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_result":
                        self.register_tool_result(
                            block.get("tool_use_id", ""),
                            block.get("content", ""),
                        )

            # OpenAI format: assistant with tool_calls
            if role == "assistant" and "tool_calls" in msg:
                for tc in msg.get("tool_calls", []):
                    if not isinstance(tc, dict):
                        continue
                    tc_id = tc.get("id", "")
                    function = tc.get("function", {})
                    try:
                        args = json.loads(function.get("arguments", "{}"))
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                    self.register_tool_use(tc_id, function.get("name", ""), args)

            # OpenAI format: tool role message
            if role == "tool":
                self.register_tool_result(
                    msg.get("tool_call_id", ""),
                    msg.get("content", ""),
                )

    def validate(self) -> List[str]:
        """
        Validate the ledger. Returns a list of issue descriptions.
        Empty list = no issues.
        """
        issues = []

        for entry in self.entries.values():
            if entry.status == "orphaned":
                issues.append(
                    f"Orphaned tool_result: tool_use_id '{entry.tool_use_id}' "
                    f"has no matching tool_use in conversation history"
                )

        return issues

    def has_open_tool_uses(self) -> bool:
        """Check if there are any unresolved tool_use entries."""
        return any(e.status == "open" for e in self.entries.values())

    def summary(self) -> Dict[str, Any]:
        """Return a summary of the ledger state for logging."""
        return {
            "total_entries": len(self.entries),
            "open": sum(1 for e in self.entries.values() if e.status == "open"),
            "resolved": sum(1 for e in self.entries.values() if e.status == "resolved"),
            "orphaned": sum(1 for e in self.entries.values() if e.status == "orphaned"),
        }
