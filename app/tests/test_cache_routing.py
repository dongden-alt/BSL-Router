"""D2 cache-aware routing regression tests.

Covers: tools.cache_aware_routing gate (default OFF = zero selection change),
CacheWarmthTracker EWMA math, warmth-as-FINAL-tiebreak among equal-tier
connections, LRU eviction at cap 64, prompt_cache_key passthrough on the
anthropic wire, and fail-open behavior. All offline.
"""

from types import SimpleNamespace

from app.middleware.caching import (
    CacheWarmthTracker,
    get_connection_warmth,
    pick_warmest_connection,
    propagate_prompt_cache_key,
    record_cache_warmth,
    reset_cache_warmth_tracker,
)
from app.normalizer import UniversalNormalizer


def _provider(conn_states=("enabled", "enabled"), round_robin=False, model_indexes=None):
    """Build a provider_config with 2 connections and 1 model."""
    conns = []
    for state in conn_states:
        if state == "enabled":
            conns.append({"base_url": "https://x.example", "api_key": "k", "enabled": True})
        elif state == "disabled":
            conns.append({"base_url": "https://x.example", "api_key": "k", "enabled": False})
        else:
            conns.append({"base_url": "https://x.example", "api_key": "k", "enabled": True})
    cfg = {"connections": conns, "format": "openai", "type": "openai", "round_robin": round_robin}
    if model_indexes is not None:
        cfg["models"] = [{"id": "m1", "connection_indexes": model_indexes}]
    return cfg


class _Breaker:
    """Minimal breaker double mirroring filter_healthy_connections semantics."""

    def __init__(self, enabled=True, open_indexes=()):
        self.enabled = enabled
        self.open_indexes = set(open_indexes)

    def filter_healthy_connections(self, provider_name, model_id, eligible):
        return [e for e in eligible if e["index"] not in self.open_indexes]


def _warm(provider, conn_index, cached, total):
    record_cache_warmth(provider, conn_index, {"prompt_tokens": total, "prompt_tokens_details": {"cached_tokens": cached}}, tools_config={"cache_aware_routing": True})


def setup_function(fn):
    reset_cache_warmth_tracker()


# ── 1. Gate OFF: selection identical to before ──────────────────────────────

def test_gate_off_returns_none_even_with_warmth():
    """Golden path: flag off (default) -> helper returns None, resolver pick stands."""
    cfg = _provider()
    _warm("p", 1, 900, 1000)  # conn idx 1 is warm, but the gate is OFF
    assert pick_warmest_connection(cfg, "p", "m1", tools_config={}) is None
    assert pick_warmest_connection(cfg, "p", "m1", tools_config=None) is None


def test_gate_off_explicit_false_matches_default():
    cfg = _provider()
    _warm("p", 1, 900, 1000)
    assert pick_warmest_connection(cfg, "p", "m1", tools_config={"cache_aware_routing": False}) is None


def test_gate_on_cold_tracker_returns_none():
    """No warmth signal -> no tiebreak; resolver's lowest-index pick stands."""
    cfg = _provider()
    assert pick_warmest_connection(cfg, "p", "m1", tools_config={"cache_aware_routing": True}) is None


def test_round_robin_provider_never_tiebroken():
    """Rotation owns selection for round_robin providers — warmth never overrides."""
    cfg = _provider(round_robin=True)
    _warm("p", 1, 900, 1000)
    assert pick_warmest_connection(cfg, "p", "m1", tools_config={"cache_aware_routing": True}) is None


# ── 2. Tracker EWMA math ────────────────────────────────────────────────────

def test_ewma_two_updates_formula():
    """EWMA: new = alpha*sample + (1-alpha)*prev; first sample initializes.

    alpha = 0.3. Update 1: cached=800/total=1000 -> sample1 = 0.8, warmth = 0.8.
    Update 2: cached=400/total=1000 -> sample2 = 0.4, warmth = 0.3*0.4 + 0.7*0.8 = 0.68.
    """
    tracker = CacheWarmthTracker()
    w1 = tracker.update("p", 0, {"prompt_tokens": 1000, "prompt_tokens_details": {"cached_tokens": 800}})
    assert abs(w1 - 0.8) < 1e-9
    w2 = tracker.update("p", 0, {"prompt_tokens": 1000, "prompt_tokens_details": {"cached_tokens": 400}})
    assert abs(w2 - (0.3 * 0.4 + 0.7 * 0.8)) < 1e-9
    assert abs(w2 - 0.68) < 1e-9


def test_ewma_anthropic_exclusive_input_shape():
    """Anthropic usage: input_tokens EXCLUSIVE; fold fresh + read + creation."""
    tracker = CacheWarmthTracker()
    # fresh=100, read=600, create=100 -> inclusive total=800, sample=600/800=0.75
    w = tracker.update("p", 0, {"input_tokens": 100, "cache_read_input_tokens": 600, "cache_creation_input_tokens": 100})
    assert abs(w - 0.75) < 1e-9


def test_warmth_read_default_zero_and_record_gated():
    assert get_connection_warmth("p", 0) == 0.0
    # Gated OFF record -> still zero.
    record_cache_warmth("p", 0, {"prompt_tokens": 10, "prompt_tokens_details": {"cached_tokens": 5}}, tools_config={})
    assert get_connection_warmth("p", 0) == 0.0
    # Gated ON record -> sampled.
    record_cache_warmth("p", 0, {"prompt_tokens": 10, "prompt_tokens_details": {"cached_tokens": 5}}, tools_config={"cache_aware_routing": True})
    assert abs(get_connection_warmth("p", 0) - 0.5) < 1e-9


# ── 3. Tiebreak: equal-tier vs unequal-tier ────────────────────────────────

def test_tiebreak_warmer_member_wins_at_equal_tier():
    """Both conns eligible (same tier); idx 1 warmer -> idx 1 replaces idx 0."""
    cfg = _provider()
    _warm("p", 1, 900, 1000)  # idx 1 warm, idx 0 cold
    pick = pick_warmest_connection(cfg, "p", "m1", tools_config={"cache_aware_routing": True})
    assert pick is not None
    conn, idx = pick
    assert idx == 1
    # Enrichment mirrors resolver: provider-level format/type attached.
    assert conn["format"] == "openai" and conn["type"] == "openai"
    assert conn["base_url"] == "https://x.example"


def test_tiebreak_unequal_tier_existing_order_wins_regardless_of_warmth():
    """Authorization (connection_indexes) is tier ordering: a warm connection
    OUTSIDE the model's authorized indexes must NEVER win."""
    cfg = _provider(conn_states=("enabled", "enabled"), model_indexes=[0])
    _warm("p", 1, 900, 1000)
    # Only idx 0 is eligible -> <=1 candidate -> None (resolver pick stands).
    assert pick_warmest_connection(cfg, "p", "m1", tools_config={"cache_aware_routing": True}) is None


def test_tiebreak_disabled_connection_never_wins_despite_warmth():
    cfg = _provider(conn_states=("enabled", "disabled"))
    _warm("p", 1, 900, 1000)
    assert pick_warmest_connection(cfg, "p", "m1", tools_config={"cache_aware_routing": True}) is None


def test_tiebreak_breaker_open_connection_never_wins_despite_warmth():
    """Health ordering is never overridden: OPEN breaker -> not a candidate."""
    cfg = _provider()
    _warm("p", 1, 900, 1000)
    pick = pick_warmest_connection(cfg, "p", "m1", breaker=_Breaker(enabled=True, open_indexes={1}), tools_config={"cache_aware_routing": True})
    assert pick is None


def test_tiebreak_key_failover_exclusion_respected():
    """Request-scoped tried-set exclusion: an excluded (already-failed) warm
    connection must never be re-picked."""
    cfg = _provider()
    _warm("p", 1, 900, 1000)
    pick = pick_warmest_connection(cfg, "p", "m1", exclude_indexes={1}, tools_config={"cache_aware_routing": True})
    assert pick is None


def test_tiebreak_equal_warmth_keeps_lowest_index():
    """Equal warmth (both cold OR both warm) -> existing lowest-index rule."""
    cfg = _provider()
    _warm("p", 0, 500, 1000)
    _warm("p", 1, 500, 1000)
    assert pick_warmest_connection(cfg, "p", "m1", tools_config={"cache_aware_routing": True}) is None


def test_tiebreak_per_provider_warmth_isolation():
    """Warmth tracked for provider 'other' must not affect provider 'p'."""
    cfg = _provider()
    _warm("other", 1, 900, 1000)
    assert pick_warmest_connection(cfg, "p", "m1", tools_config={"cache_aware_routing": True}) is None


# ── 4. LRU eviction at cap 64 ───────────────────────────────────────────────

def test_lru_eviction_at_capacity_64():
    tracker = CacheWarmthTracker(capacity=64)
    for i in range(64):
        tracker.update("p", i, {"prompt_tokens": 10, "prompt_tokens_details": {"cached_tokens": 5}})
    assert len(tracker._state) == 64
    # All 64 present.
    for i in range(64):
        assert abs(tracker.warmth("p", i) - 0.5) < 1e-9
    # 65th distinct key evicts the LEAST-recently-UPDATED (idx 0).
    tracker.update("p", 64, {"prompt_tokens": 10, "prompt_tokens_details": {"cached_tokens": 5}})
    assert len(tracker._state) == 64
    assert tracker.warmth("p", 0) == 0.0
    assert abs(tracker.warmth("p", 64) - 0.5) < 1e-9


def test_lru_update_refreshes_recency():
    tracker = CacheWarmthTracker(capacity=2)
    tracker.update("p", 0, {"prompt_tokens": 10, "prompt_tokens_details": {"cached_tokens": 5}})
    tracker.update("p", 1, {"prompt_tokens": 10, "prompt_tokens_details": {"cached_tokens": 5}})
    tracker.update("p", 0, {"prompt_tokens": 10, "prompt_tokens_details": {"cached_tokens": 5}})  # refresh 0
    tracker.update("p", 2, {"prompt_tokens": 10, "prompt_tokens_details": {"cached_tokens": 5}})  # evicts 1
    assert tracker.warmth("p", 0) > 0.0
    assert tracker.warmth("p", 1) == 0.0
    assert tracker.warmth("p", 2) > 0.0


# ── 5. prompt_cache_key passthrough ─────────────────────────────────────────

def test_anthropic_ingress_drops_prompt_cache_key_then_handler_preserves():
    """Proof of drop: normalize_to_openai_from_anthropic loses the key.
    main._preserve_anthropic_prompt_cache_key restores it (passthrough only)."""
    import app.main as main

    body = {"model": "claude-sonnet-5", "max_tokens": 64, "prompt_cache_key": "cc-key-123",
            "messages": [{"role": "user", "content": "hi"}]}
    openai_body = UniversalNormalizer.normalize_to_openai_from_anthropic(body)
    assert "prompt_cache_key" not in openai_body  # converter drops it

    openai_body = main._preserve_anthropic_prompt_cache_key(body, openai_body)
    assert openai_body["prompt_cache_key"] == "cc-key-123"


def test_propagate_prompt_cache_key_from_pydantic_extra_to_anthropic_payload():
    """Egress: normalize_to_anthropic builds from scratch (key dropped);
    propagate_prompt_cache_key restores it from the internal request."""
    from app.models import ChatCompletionRequest

    req = ChatCompletionRequest.model_validate({
        "model": "m", "max_tokens": 10,
        "messages": [{"role": "user", "content": "hi"}],
        "prompt_cache_key": "cc-key-123",
    })
    anthropic_payload = UniversalNormalizer.normalize_to_anthropic(req)
    assert "prompt_cache_key" not in anthropic_payload  # built from scratch

    anthropic_payload = propagate_prompt_cache_key(anthropic_payload, req)
    assert anthropic_payload["prompt_cache_key"] == "cc-key-123"


def test_propagate_noop_when_absent_or_already_set():
    from app.models import ChatCompletionRequest

    req = ChatCompletionRequest.model_validate({
        "model": "m", "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}]
    })
    payload = {"model": "m", "messages": []}
    assert propagate_prompt_cache_key(payload, req) is payload
    assert "prompt_cache_key" not in payload  # absent -> no invented key

    payload2 = {"model": "m", "messages": [], "prompt_cache_key": "upstream-key"}
    assert propagate_prompt_cache_key(payload2, req) is payload2
    assert payload2["prompt_cache_key"] == "upstream-key"  # never overwritten


# ── 6. Fail-open ────────────────────────────────────────────────────────────

def test_fail_open_record_with_garbage_input():
    """Tracker update with malformed usage must never raise."""
    record_cache_warmth("p", 0, None, tools_config={"cache_aware_routing": True})
    record_cache_warmth("p", 0, {"prompt_tokens": "garbage"}, tools_config={"cache_aware_routing": True})
    record_cache_warmth(None, None, {"input_tokens": 5}, tools_config={"cache_aware_routing": True})
    record_cache_warmth("p", 0, [], tools_config={"cache_aware_routing": True})
    assert get_connection_warmth("p", 0) == 0.0


def test_fail_open_pick_with_malformed_config():
    assert pick_warmest_connection(None, "p", "m1", tools_config={"cache_aware_routing": True}) is None
    assert pick_warmest_connection({}, "p", "m1", tools_config={"cache_aware_routing": True}) is None
    assert pick_warmest_connection({"connections": None}, "p", "m1", tools_config={"cache_aware_routing": True}) is None
    # Breaker exploding must degrade to no-breaker behavior, not raise.
    class _Boom:
        enabled = True

        def filter_healthy_connections(self, *a, **k):
            raise RuntimeError("boom")
    cfg = _provider()
    _warm("p", 1, 900, 1000)
    pick = pick_warmest_connection(cfg, "p", "m1", breaker=_Boom(), tools_config={"cache_aware_routing": True})
    assert pick is not None and pick[1] == 1  # breaker failed open -> tiebreak still applies


def test_fail_open_propagate_with_garbage():
    assert propagate_prompt_cache_key(None, None) is None
    assert propagate_prompt_cache_key({"model": "m"}, None) == {"model": "m"}
    assert propagate_prompt_cache_key({"model": "m"}, SimpleNamespace(prompt_cache_key=123)) == {"model": "m"}  # non-str ignored
