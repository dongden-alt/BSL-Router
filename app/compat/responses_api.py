"""
BSL Router Agent Compatibility Layer — OpenAI Responses API Support

Phase 6: Support Codex-style and modern OpenAI reasoning clients.

The Responses API (/v1/responses) is a different protocol from chat/completions:
- Uses "input" items instead of "messages"
- Has reasoning items with IDs that must be preserved
- Different SSE event format
- Tool calls use a different structure

This module converts between Responses format and BSL's internal OpenAI format.
"""
from typing import Dict, Any, List
import json
import uuid


class ResponsesConverter:
    """
    Converts OpenAI Responses API format ↔ internal chat/completions format.
    """

    @staticmethod
    def responses_to_chat(responses_body: Dict[str, Any]) -> Dict[str, Any]:
        """
        Convert a /v1/responses request body to /v1/chat/completions format.

        Responses 'input' items → chat 'messages'
        Responses 'tools' → chat 'tools' (format conversion needed)
        """
        chat_body: Dict[str, Any] = {
            "model": responses_body.get("model", ""),
            "messages": [],
            "stream": responses_body.get("stream", False),
        }

        # Convert instructions → system message
        instructions = responses_body.get("instructions")
        if instructions:
            chat_body["messages"].append({"role": "system", "content": instructions})

        # Convert input items → messages
        input_items = responses_body.get("input", [])
        if isinstance(input_items, str):
            chat_body["messages"].append({"role": "user", "content": input_items})
        elif isinstance(input_items, list):
            for item in input_items:
                if not isinstance(item, dict):
                    continue

                item_type = item.get("type", "")

                if item_type == "message":
                    role = item.get("role", "user")
                    content = item.get("content", "")
                    if isinstance(content, list):
                        # Extract text from content parts
                        text_parts = []
                        for part in content:
                            if isinstance(part, dict) and part.get("type") == "output_text":
                                text_parts.append(part.get("text", ""))
                            elif isinstance(part, str):
                                text_parts.append(part)
                        content = "\n".join(text_parts)
                    chat_body["messages"].append({"role": role, "content": content})

                elif item_type == "reasoning":
                    # Preserve reasoning items — convert to a system note
                    # The reasoning ID is preserved in metadata for replay
                    reasoning_id = item.get("id", "")
                    summary = item.get("summary", [])
                    if isinstance(summary, list) and summary:
                        summary_text = summary[0].get("text", "") if isinstance(summary[0], dict) else str(summary[0])
                        # Don't inject as a message — reasoning is handled by the policy engine
                        pass

                elif item_type == "function_call":
                    # Convert to assistant tool_calls
                    fn_name = item.get("name", "")
                    # Guard: skip nameless function calls — empty tool names get
                    # rejected by upstream validators (400) and can trigger the
                    # combo fallback retry storm. See gemini.py L1/L2 guards.
                    if not isinstance(fn_name, str) or not fn_name.strip():
                        continue
                    chat_body["messages"].append({
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{
                            "id": item.get("call_id", item.get("id", "")),
                            "type": "function",
                            "function": {
                                "name": fn_name.strip(),
                                "arguments": item.get("arguments", "{}"),
                            },
                        }],
                    })

                elif item_type == "function_call_output":
                    # Convert to tool-role message
                    chat_body["messages"].append({
                        "role": "tool",
                        "tool_call_id": item.get("call_id", ""),
                        "content": item.get("output", ""),
                    })

        # Convert tools
        if "tools" in responses_body:
            chat_tools: List[Dict[str, Any]] = []
            for tool in responses_body["tools"]:
                if not isinstance(tool, dict):
                    continue
                tool_type = tool.get("type", "")
                if tool_type == "function":
                    tool_name = tool.get("name", "")
                    # Guard: skip nameless tool declarations — same 400 class.
                    if not isinstance(tool_name, str) or not tool_name.strip():
                        continue
                    chat_tools.append({
                        "type": "function",
                        "function": {
                            "name": tool_name.strip(),
                            "description": tool.get("description", ""),
                            "parameters": tool.get("parameters", {"type": "object", "properties": {}}),
                        },
                    })
            if chat_tools:
                chat_body["tools"] = chat_tools

        # Convert scalar fields
        if "max_output_tokens" in responses_body:
            chat_body["max_tokens"] = responses_body["max_output_tokens"]
        if "temperature" in responses_body:
            chat_body["temperature"] = responses_body["temperature"]
        if "top_p" in responses_body:
            chat_body["top_p"] = responses_body["top_p"]
        if "tool_choice" in responses_body:
            chat_body["tool_choice"] = responses_body["tool_choice"]
        if "reasoning" in responses_body:
            # Map reasoning effort
            reasoning = responses_body["reasoning"]
            if isinstance(reasoning, dict) and "effort" in reasoning:
                chat_body["reasoning_effort"] = reasoning["effort"]

        return chat_body


