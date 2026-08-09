"""
Response Format Resilience Layer

Problem: Many reverse-proxy sellers (especially "deep market" resellers) silently
strip the `response_format` parameter from API requests, even though they accept
it without error. The client believes structured output is enforced, but the model
generates free-form text. This causes silent failures in production (JSON parsing
errors downstream).

Defense Strategy (two-layer, fail-open):
1. PROMPT-LEVEL INJECTION — When response_format is present, inject a JSON
   instruction into the system prompt. Even if the proxy strips the API
   parameter, the model still sees the instruction and tends to comply.
2. DIAGNOSTIC LOGGING — When response_format was requested, log whether the
   upstream response looks like valid JSON. This builds evidence of which
   providers/proxies strip the parameter.

Design principles:
- Fail-open: Never block a request due to response_format processing.
- Non-invasive: Only inject when response_format is present.
- Reversible: The prompt instruction is prefixed with a clear BSL marker.
"""
from typing import Any, Dict, Optional


def has_response_format(payload: Dict[str, Any]) -> bool:
    """Check if the payload has a response_format parameter."""
    rf = payload.get("response_format")
    if not rf or not isinstance(rf, dict):
        return False
    rtype = rf.get("type", "")
    return rtype in ("json_object", "json_schema")


def extract_schema_hint(payload: Dict[str, Any]) -> Optional[str]:
    """
    Extract a human-readable hint about the expected JSON structure.
    Returns None if no schema is available.
    """
    rf = payload.get("response_format")
    if not rf or not isinstance(rf, dict):
        return None

    rtype = rf.get("type", "")
    if rtype == "json_object":
        return "Respond with a valid JSON object."

    if rtype == "json_schema":
        js = rf.get("json_schema", {})
        if not isinstance(js, dict):
            return "Respond with valid JSON."

        schema = js.get("schema", {})
        name = js.get("name", "")
        description = js.get("description", "")

        # Build a compact hint from the schema
        hint_parts = []
        if name:
            hint_parts.append(f'JSON schema: "{name}"')
        if description:
            hint_parts.append(f"({description})")

        # Extract top-level property names if available
        if isinstance(schema, dict):
            props = schema.get("properties", {})
            required = schema.get("required", [])
            if isinstance(props, dict) and props:
                prop_list = list(props.keys())[:10]  # Top 10 properties
                hint_parts.append(f"Top-level keys: {prop_list}")
            if isinstance(required, list) and required:
                hint_parts.append(f"Required: {required[:10]}")

        return ". ".join(hint_parts) + "."

    return None


def inject_json_instruction(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Inject a JSON instruction into the system prompt as a resilience layer.

    This is a FALLBACK — the response_format API parameter should still be sent
    to upstream. The prompt instruction ensures the model knows to produce JSON
    even if a reverse proxy strips the API parameter.

    Non-invasive: only modifies payload when response_format is present.
    Fail-open: any error returns the original payload unchanged.
    """
    try:
        if not has_response_format(payload):
            return payload

        hint = extract_schema_hint(payload)
        if not hint:
            return payload

        instruction = f"\n\n[BSL Router: Structured Output Enforced] {hint} Output ONLY valid JSON, no markdown fences, no commentary."

        messages = payload.get("messages", [])
        if not isinstance(messages, list) or not messages:
            return payload

        # Find the first system message and append to it
        for msg in messages:
            if isinstance(msg, dict) and msg.get("role") == "system":
                content = msg.get("content")
                if isinstance(content, str):
                    msg["content"] = content + instruction
                elif isinstance(content, list):
                    # Append a text block
                    content.append({"type": "text", "text": instruction})
                return payload

        # No system message found — inject one at the start
        payload["messages"] = [{
            "role": "system",
            "content": instruction.strip()
        }] + messages

        return payload
    except Exception:
        return payload


def validate_json_response(response_text: str, provider: str, model: str) -> bool:
    """
    Check if a response that should be JSON (response_format was requested)
    is actually valid JSON. Returns True if valid, False otherwise.

    Used for diagnostic logging — does not modify the response.
    """
    import json
    text = response_text.strip()
    # Strip markdown code fences if present
    if text.startswith("```"):
        lines = text.split("\n")
        if len(lines) >= 2:
            text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    try:
        json.loads(text)
        return True
    except (json.JSONDecodeError, ValueError):
        return False
