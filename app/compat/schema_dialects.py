"""Tool JSON-schema dialect normalization for multi-upstream routing.

Canonical internal form = OpenAI JSON Schema (lowercase ``type`` strings).
Each emitter targets one motivating upstream:

  - ``openai_strict``       — OpenAI Chat Completions structured outputs (strict mode)
  - ``anthropic_passthrough`` — Anthropic Messages API tool ``input_schema``
  - ``gemini_strict``       — Gemini functionDeclaration (antigravity lane)

Pure functions, no I/O. Fail-open by contract: every function NEVER raises;
on any internal error the input schema is returned unchanged.
"""

from __future__ import annotations

from typing import Any, Callable

__all__ = [
    "gemini_strict",
    "openai_strict",
    "anthropic_passthrough",
    "canonicalize_schema",
    "emit_schema",
]

# Meta/keyword keys each motivating upstream rejects.
_ANTHROPIC_REJECT_KEYS = ("$schema", "$id", "$anchor", "$comment", "definitions")
_OPENAI_STRICT_UNSUPPORTED = ("$schema", "$id", "$anchor", "$comment", "definitions",
                              "propertyOrdering", "enumDescriptions", "default",
                              "examples", "title")
_KNOWN_DIALECTS = ("openai", "anthropic", "gemini", "responses")

# Map of (capitalized or lowercase) schema type strings → canonical lowercase.
# Gemini's own docs emit UPPERCASE (STRING/OBJECT/…); both casings tolerated.
_TYPE_MAP = {
    "object": "object", "string": "string", "number": "number", "integer": "integer",
    "boolean": "boolean", "array": "array", "null": "null",
    "OBJECT": "object", "STRING": "string", "NUMBER": "number", "INTEGER": "integer",
    "BOOLEAN": "boolean", "ARRAY": "array", "NULL": "null",
}

# Schema-value positions in a JSON Schema node: single-schema keys and
# lists-of-schemas keys, plus the name→schema mappings ($defs/definitions).
_SINGLE_SCHEMA_KEYS = ("items", "additionalProperties", "not", "if", "then",
                       "else", "propertyNames", "contains", "unevaluatedItems",
                       "unevaluatedProperties")
_SCHEMA_LIST_KEYS = ("prefixItems", "anyOf", "oneOf", "allOf")
_SCHEMA_MAP_KEYS = ("$defs", "definitions")


def _walk(schema: Any, fix: Callable[[dict], dict]) -> Any:
    """Depth-first rewrite applying ``fix`` to every schema node at its proper position."""
    if isinstance(schema, dict):
        out = fix(dict(schema))
        props = out.get("properties")
        if isinstance(props, dict):
            out["properties"] = {k: _walk(v, fix) for k, v in props.items()}
        for key in _SINGLE_SCHEMA_KEYS:
            if isinstance(out.get(key), (dict, list)):
                out[key] = _walk(out[key], fix)
        for key in _SCHEMA_LIST_KEYS:
            if isinstance(out.get(key), list):
                out[key] = [_walk(v, fix) for v in out[key]]
        for key in _SCHEMA_MAP_KEYS:
            if isinstance(out.get(key), dict):
                out[key] = {k: _walk(v, fix) for k, v in out[key].items()}
        return out
    if isinstance(schema, list):
        return [_walk(item, fix) for item in schema]
    return schema


def _lower_types(node: dict) -> dict:
    """Lowercase schema ``type`` strings (str or list form); keep everything else."""
    out: dict = {}
    for key, val in node.items():
        if key == "type" and isinstance(val, str):
            out[key] = _TYPE_MAP.get(val, val.lower())
        elif key == "type" and isinstance(val, list):
            out[key] = [_TYPE_MAP.get(v, v.lower()) if isinstance(v, str) else v for v in val]
        else:
            out[key] = val
    return out


def _fail_open(schema: Any, fn: Callable[[Any], Any]) -> Any:
    """Run ``fn(schema)``; return the schema unchanged on any internal error."""
    try:
        return fn(schema)
    except Exception:
        return schema


def gemini_strict(schema: Any) -> Any:
    """Emit a schema the Gemini functionDeclaration upstream (antigravity lane) accepts: lowercase types, drop ``additionalProperties`` (Gemini rejects it), tolerate propertyOrdering/enumDescriptions."""
    def _fix(node: dict) -> dict:
        out = _lower_types(node)
        out.pop("additionalProperties", None)
        return out  # propertyOrdering / enumDescriptions pass through untouched
    return _fail_open(schema, lambda s: _walk(s, _fix))


def openai_strict(schema: Any) -> Any:
    """Emit an OpenAI strict structured-outputs schema: additionalProperties false everywhere, every property in required, unsupported keywords dropped."""
    def _fix(node: dict) -> dict:
        out = {k: v for k, v in node.items() if k not in _OPENAI_STRICT_UNSUPPORTED}
        if out.get("type") == "object" or "properties" in out:
            out["type"] = "object"
            props = out.get("properties")
            if isinstance(props, dict):
                out["required"] = list(props.keys())
            out["additionalProperties"] = False
        return out
    return _fail_open(schema, lambda s: _walk(s, _fix))


def anthropic_passthrough(schema: Any) -> Any:
    """Emit an Anthropic Messages API input_schema: strip the meta keys Anthropic upstreams reject, else pass the schema through untouched."""
    def _fix(node: dict) -> dict:
        return {k: v for k, v in node.items() if k not in _ANTHROPIC_REJECT_KEYS}
    return _fail_open(schema, lambda s: _walk(s, _fix))


def canonicalize_schema(schema: Any, source_dialect: str) -> Any:
    """Canonicalize a schema from ``source_dialect`` (openai | anthropic | gemini | responses) into the canonical OpenAI JSON-Schema form (lowercase types, all keys kept)."""
    dialect = (source_dialect or "").lower()
    if dialect not in _KNOWN_DIALECTS:
        return schema
    # Conservative superset: only normalize type casing — never drop keys
    # (gemini's additionalProperties/propertyOrdering/enumDescriptions are
    # preserved in canonical form; emoters drop what their target rejects).
    return _fail_open(schema, lambda s: _walk(s, _lower_types))


def emit_schema(schema: Any, target_dialect: str) -> Any:
    """Emit a canonical schema for ``target_dialect`` (openai | anthropic | gemini | responses) — the inverse of canonicalize_schema."""
    dialect = (target_dialect or "").lower()
    if dialect == "gemini":
        return gemini_strict(schema)
    if dialect == "anthropic":
        return anthropic_passthrough(schema)
    if dialect == "openai" or dialect == "responses":
        # Responses API tools share the OpenAI Chat Completions tool shape.
        return openai_strict(schema)
    return schema
