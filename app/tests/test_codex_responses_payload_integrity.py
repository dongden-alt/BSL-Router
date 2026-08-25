"""Codex Responses payload integrity — the intent-driven block must never
leave a `messages` key on a codex Responses payload.

Codex speaks the Responses wire (top-level `input` / `instructions`); a
`messages` key triggers HTTP 400 "Unsupported parameter: messages".
"""
import inspect
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parent.parent / "main.py"
SOURCE = APP.read_text(encoding="utf-8")

# Pull the helpers out of main.py so we can unit-test them directly without
# booting the full FastAPI app / config stack.
import app.main as _main

_fold = _main._fold_top_level_system_into_messages
_inject = _main._inject_intent_format_block


# ── 1. Gate parity + functional: codex skips the intent block ─────────


def test_intent_block_gate_excludes_codex() -> None:
    """Source parity: the output_intent_driven gate must contain the codex
    exclusion (same string-parity pattern as test_provider_modal_key_guard).
    """
    # The gate lives on a single line; locate it by its unique leading tokens.
    gate_line = None
    for line in SOURCE.splitlines():
        stripped = line.strip()
        if stripped.startswith("if t_cfg.get(\"output_intent_driven\"") and "client_wants_gemini" in stripped:
            gate_line = stripped
            break
    assert gate_line is not None, "output_intent_driven gate not found"
    assert "provider_name != 'codex'" in gate_line, (
        f"codex exclusion missing from gate: {gate_line}"
    )


def test_inject_intent_block_would_corrupt_codex_payload() -> None:
    """Sanity: _inject_intent_format_block, if it ran on a codex Responses
    payload, would create a `messages` key (no top-level `system` present →
    it folds into messages). This is exactly what the codex gate prevents.
    """
    payload = {"input": [{"role": "user", "content": "hi"}], "model": "gpt-5.6-terra"}
    out = _inject(payload, "json")
    assert "messages" in out, "expected inject to create messages on a Responses payload"
    assert out["messages"][0]["role"] == "system"


# ── 2. Fold helper — Responses wire folds into instructions ───────────


def test_fold_responses_wire_merges_instructions() -> None:
    """Payload on the Responses wire (has `input`, no `messages`) + a
    top-level `system` key → result has NO `messages`, system text merged
    into `instructions` (existing instructions first).
    """
    payload = {
        "input": [{"role": "user", "content": "hi"}],
        "instructions": "Base instruction.",
        "system": "Detected format: JSON.",
        "model": "gpt-5.6-terra",
    }
    out = _fold(payload)
    assert "messages" not in out, f"messages leaked onto Responses payload: {out}"
    assert "system" not in out, "top-level system key should be popped"
    assert out["instructions"] == "Base instruction.\nDetected format: JSON."
    assert out["input"] == [{"role": "user", "content": "hi"}]


def test_fold_responses_wire_instructions_only_system() -> None:
    """Responses wire with no pre-existing instructions — system becomes
    instructions verbatim.
    """
    payload = {
        "input": [{"role": "user", "content": "hi"}],
        "system": "Detected format: JSON.",
    }
    out = _fold(payload)
    assert "messages" not in out
    assert out["instructions"] == "Detected format: JSON."


# ── 3. Fold helper — Chat wire still creates messages ─────────────────


def test_fold_chat_wire_still_creates_messages() -> None:
    """Payload NOT on the Responses wire (no `input` key) + top-level
    `system` → unchanged behaviour: creates a `messages` list with a
    system entry.
    """
    payload = {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": "hi"}],
        "system": "Detected format: JSON.",
    }
    out = _fold(payload)
    assert "system" not in out
    assert out["messages"][0]["role"] == "system"
    assert out["messages"][0]["content"] == "Detected format: JSON."


def test_fold_chat_wire_no_messages_yet_creates_messages() -> None:
    """Chat wire with no `messages` key and no `input` key — still falls
    back to creating a messages list (the legacy path is preserved).
    """
    payload = {"model": "gpt-4o", "system": "Be concise."}
    out = _fold(payload)
    assert "messages" in out
    assert out["messages"][0]["role"] == "system"


# ── 4. End-to-end: codex Responses output survives a fold ─────────────


def test_codex_responses_payload_stays_messages_free_after_fold() -> None:
    """Functional regression: run a codex-converted payload (output of
    openai_to_responses) through _fold and assert `messages` never appears.
    This is the exact shape that previously hit the 400.
    """
    from app.codex_adapter import openai_to_responses

    chat = {
        "model": "gpt-5.6-terra",
        "messages": [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Return the answer as a JSON object."},
        ],
    }
    responses = openai_to_responses(chat)
    assert "messages" not in responses
    assert "input" in responses

    # Simulate what the intent block leaves behind: a top-level system key.
    responses["system"] = "Detected format: JSON."
    out = _fold(responses)

    assert "messages" not in out, (
        f"REGRESSION: messages key created on codex Responses payload: {out}"
    )
    assert "system" not in out


# ── 5. inject_json_instruction is codex-safe (no messages write) ──────


def test_inject_json_instruction_does_not_write_messages():
    """Defence-in-depth check (Change 3): inject_json_instruction mutates
    the system/messages of a Chat payload only when response_format is set.
    Codex strips response_format in openai_to_responses (codex_adapter.py
    STRIP_KEYS), so this path cannot fire for codex. Confirm it also never
    touches a Responses-shaped payload.
    """
    from app.middleware.response_format_guard import inject_json_instruction

    responses_payload = {
        "input": [{"role": "user", "content": "hi"}],
        "instructions": "Base.",
        "response_format": {"type": "json_object"},
    }
    out = inject_json_instruction(responses_payload)
    # It operates on Chat-wire messages; on a Responses payload it must not
    # create a messages key.
    assert "messages" not in out
