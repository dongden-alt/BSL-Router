"""Dispatch-reachability tests for blacksand-agentic-max wiring.

These tests verify the five integration points that were missing when Max
was orphaned (2026-08-07 audit):

1. Alias normalization — all three spellings resolve via _BLACKSAND_MODEL_ALIASES.
2. Catalog registration — blacksand-agentic-max appears in the /v1/models loop.
3. Dispatch branch — main.py routes blacksand-agentic-max to _bsl_agentic_max_dispatch.
4. Sibling derivation — a route defined only in bsl_agentic.agent_routes
   surfaces through Max's coding domain (proves the chat+agentic fusion).
"""

from app.middleware.bsl_agentic_max_router import route_bsl_agentic_max
from app.models import ChatCompletionRequest, Message


# ─── Alias normalization ─────────────────────────────────────────────────────


def test_alias_normalization() -> None:
    """All three spellings map to the canonical blacksand-agentic-max."""
    from app.main import _BLACKSAND_MODEL_ALIASES

    for spelling in ("blacksand-agentic-max", "bsl-agentic-max", "BSL-Agentic-Max"):
        assert _BLACKSAND_MODEL_ALIASES[spelling] == "blacksand-agentic-max", (
            f"Alias '{spelling}' missing or wrong target in _BLACKSAND_MODEL_ALIASES"
        )


# ─── Catalog registration ────────────────────────────────────────────────────


def test_catalog_registration() -> None:
    """The /v1/models registration loop includes bsl_agentic_max."""
    import inspect
    from app.main import list_models

    src = inspect.getsource(list_models)
    assert '"bsl_agentic_max"' in src, (
        "bsl_agentic_max not found in list_models registration loop"
    )
    assert '"blacksand-agentic-max"' in src, (
        "blacksand-agentic-max not found in list_models registration loop"
    )


# ─── Dispatch branch ─────────────────────────────────────────────────────────


def test_dispatch_branch_exists() -> None:
    """main.py has a dispatch branch routing to _bsl_agentic_max_dispatch."""
    import app.main as main_mod

    src = inspect_module_source(main_mod)
    assert 'model == "blacksand-agentic-max"' in src, (
        "Dispatch branch for blacksand-agentic-max not found in main.py"
    )
    assert "_bsl_agentic_max_dispatch" in src, (
        "_bsl_agentic_max_dispatch function not found in main.py"
    )
    assert hasattr(main_mod, "_bsl_agentic_max_dispatch"), (
        "_bsl_agentic_max_dispatch not importable from app.main"
    )


def inspect_module_source(module) -> str:
    import inspect
    return inspect.getsource(module)


# ─── Sibling derivation (chat + agentic fusion) ──────────────────────────────


def test_sibling_derivation_coding_route() -> None:
    """A route defined only in bsl_agentic.agent_routes is reachable via Max.

    This proves the fusion reads the sibling coding matrix rather than
    requiring a self-contained bsl_agentic_max.agent_routes block.
    """
    config = {
        "bsl_models": {
            "bsl_agentic": {
                "agent_routes": {
                    "power_coder": {"primary": "sibling-power-model"},
                    "fast_coder": {"primary": "sibling-fast-model"},
                },
            },
            "bsl_chat": {
                "category_overrides": {
                    "general": {
                        "fast": {"primary": "sibling-chat-fast"},
                        "standard": {"primary": "sibling-chat-std"},
                        "deep": {"primary": "sibling-chat-deep"},
                    },
                },
            },
            "bsl_agentic_max": {
                "enabled": True,
                "merge_strategy": "confidence_weighted",
                "global_last_fallback": "GLM-5.2",
            },
        },
    }
    request = ChatCompletionRequest(
        model="blacksand-agentic-max",
        messages=[Message(role="user", content="implement a new parser function")],
    )
    d = route_bsl_agentic_max(request, config)
    # Coding signal should win; the selected model must come from the
    # sibling bsl_agentic matrix, not from any local block.
    assert d.domain == "coding"
    assert d.selected_model.startswith("sibling-"), (
        f"Expected sibling-derived model, got '{d.selected_model}'"
    )
    assert d.selected_model == "sibling-power-model"


def test_sibling_derivation_chat_route() -> None:
    """A route defined only in bsl_chat.category_overrides is reachable via Max."""
    config = {
        "bsl_models": {
            "bsl_agentic": {
                "agent_routes": {
                    "power_coder": {"primary": "sibling-power-model"},
                },
            },
            "bsl_chat": {
                "category_overrides": {
                    "general": {
                        "fast": {"primary": "sibling-chat-fast"},
                        "standard": {"primary": "sibling-chat-std"},
                        "deep": {"primary": "sibling-chat-deep"},
                    },
                },
            },
            "bsl_agentic_max": {
                "enabled": True,
                "merge_strategy": "confidence_weighted",
                "global_last_fallback": "GLM-5.2",
            },
        },
    }
    request = ChatCompletionRequest(
        model="blacksand-agentic-max",
        messages=[Message(role="user", content="hello, how are you today?")],
    )
    d = route_bsl_agentic_max(request, config)
    assert d.domain == "chat"
    assert d.selected_model.startswith("sibling-chat"), (
        f"Expected sibling-derived chat model, got '{d.selected_model}'"
    )
