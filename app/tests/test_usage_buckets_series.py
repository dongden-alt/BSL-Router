"""Tests for the Consumption-over-time bucket series payload.

Covers the _build_buckets_for_sql extension that adds per-bucket `tokens`,
`by_model` and `by_provider` series to each time bucket:

- tokens == SUM(in_cached + cache_write_tokens + in_uncached + out)
- by_model / by_provider present, each entry has name/requests/cost/tokens
- top-N capping folds the remainder into a single "other" entry
- series tokens sum reconciles with the bucket total (top-N + other)
- fail-open: a broken connection path yields empty series without raising
- backward-compat keys (requests / cost) unchanged and always present
"""
import os
import sys
import sqlite3
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import app.observability as obs


# ── Fixtures ───────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_usage_db(tmp_path):
    db_path = str(tmp_path / "usage_stats.sqlite3")
    jsonl_path = str(tmp_path / "usage_stats.jsonl")
    obs.usage_db_path = db_path
    obs._USAGE_LOG_PATH = jsonl_path
    yield db_path, tmp_path
    try:
        obs.usage_db_path = None
    except Exception:
        pass
    try:
        obs._USAGE_LOG_PATH = "data/usage_stats.jsonl"
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _reset_store(tmp_usage_db):
    db_path = tmp_usage_db[0]
    try:
        os.remove(db_path)
    except OSError:
        pass
    obs.init_usage_store()
    yield


def _entry(i, provider="openai", model="gpt-4o",
           in_cached=0, cache_write_tokens=0, in_uncached=0, out=0, cost=0.0):
    """Build a usage event pinned to 'now-ish' so it lands in the 1D buckets."""
    ts = obs.datetime.now().isoformat()
    return {
        "timestamp": ts,
        "provider": provider,
        "model": model,
        "ttft_ms": float(i * 10),
        "total_time_ms": float((i + 1) * 20),
        "in_cached": in_cached,
        "cache_write_tokens": cache_write_tokens,
        "in_uncached": in_uncached,
        "out": out,
        "cost": round(cost, 6),
        "savings": 0.0,
    }


def _entry_row(ts_epoch, provider="openai", model="gpt-4o",
               in_cached=0, cache_write_tokens=0, in_uncached=0, out=0, cost=0.0):
    """Build a raw (ts, ts_epoch, ...) tuple for direct insert at a known epoch."""
    ts = obs.datetime.fromtimestamp(ts_epoch).isoformat()
    return (ts, ts_epoch, provider, model, 0.0, 0.0,
            in_cached, cache_write_tokens, in_uncached, out, round(cost, 6), 0.0)


def _seed_rows(rows, db_path):
    """Insert raw rows directly (avoids log_request cost recompute)."""
    conn = sqlite3.connect(db_path)
    conn.executemany(
        """INSERT INTO usage_events
           (ts, ts_epoch, provider, model, ttft_ms, total_time_ms,
            in_cached, cache_write_tokens, in_uncached, out, cost, savings)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    conn.commit()
    conn.close()


def _now_epoch():
    return time.time()


# ── tokens sum ─────────────────────────────────────────────────────────────

def test_bucket_tokens_equal_sum_of_token_columns(tmp_usage_db):
    db_path = tmp_usage_db[0]
    now = _now_epoch()
    rows = [
        _entry_row(now - 60, provider="openai", model="gpt-4o",
                   in_cached=100, cache_write_tokens=10, in_uncached=50, out=200, cost=0.01),
        _entry_row(now - 30, provider="anthropic", model="claude-sonnet-5",
                   in_cached=5, cache_write_tokens=2, in_uncached=3, out=40, cost=0.02),
    ]
    _seed_rows(rows, db_path)

    s = obs.query_usage_summary(timeframe="1D")
    buckets = s["buckets"]
    # At least one bucket must contain both rows (they're <1D old).
    total_tokens = sum(int(b.get("tokens", 0)) for b in buckets)
    expected = (100 + 10 + 50 + 200) + (5 + 2 + 3 + 40)
    assert total_tokens == expected, f"bucket tokens sum {total_tokens} != {expected}"

    # Every bucket carries the tokens key.
    for b in buckets:
        assert "tokens" in b


def test_bucket_tokens_match_totals_row(tmp_usage_db):
    db_path = tmp_usage_db[0]
    now = _now_epoch()
    rows = [
        _entry_row(now - 600, provider="openai", model="gpt-4o",
                   in_cached=7, cache_write_tokens=3, in_uncached=11, out=19, cost=0.0),
        _entry_row(now - 300, provider="openai", model="gpt-4o",
                   in_cached=0, cache_write_tokens=0, in_uncached=5, out=5, cost=0.0),
    ]
    _seed_rows(rows, db_path)
    s = obs.query_usage_summary(timeframe="1D")
    bucket_tokens = sum(int(b.get("tokens", 0)) for b in s["buckets"])
    t = s["totals"]
    totals_tokens = int(t["in_cached"]) + int(t["cache_write_tokens"]) + \
        int(t["in_uncached"]) + int(t["out"])
    assert bucket_tokens == totals_tokens


# ── series shape ──────────────────────────────────────────────────────────

def test_series_present_and_shaped(tmp_usage_db):
    db_path = tmp_usage_db[0]
    now = _now_epoch()
    rows = [
        _entry_row(now - 60, provider="openai", model="gpt-4o",
                   in_cached=10, out=10, cost=0.01),
        _entry_row(now - 60, provider="anthropic", model="claude-sonnet-5",
                   in_cached=20, out=20, cost=0.02),
    ]
    _seed_rows(rows, db_path)
    s = obs.query_usage_summary(timeframe="1D")
    nonempty = [b for b in s["buckets"] if int(b.get("tokens", 0)) > 0]
    assert nonempty, "no bucket captured the seeded rows"
    b = nonempty[0]
    assert "by_model" in b and "by_provider" in b
    assert isinstance(b["by_model"], list)
    assert isinstance(b["by_provider"], list)
    # Entries carry the documented keys.
    for key in ("by_model", "by_provider"):
        for e in b[key]:
            assert set(e.keys()) == {"name", "requests", "cost", "tokens"}
            assert isinstance(e["name"], str)
            assert isinstance(e["requests"], int)
            assert isinstance(e["cost"], float)
            assert isinstance(e["tokens"], int)
    # The two distinct models / providers show up as their own series.
    model_names = {e["name"] for e in b["by_model"]}
    prov_names = {e["name"] for e in b["by_provider"]}
    assert {"gpt-4o", "claude-sonnet-5"}.issubset(model_names)
    assert {"openai", "anthropic"}.issubset(prov_names)


# ── top-N fold ─────────────────────────────────────────────────────────────

def test_by_model_top8_fold_into_other(tmp_usage_db):
    db_path = tmp_usage_db[0]
    now = _now_epoch()
    # 11 distinct models — top-8 kept, remainder folded into "other".
    rows = []
    for i in range(11):
        rows.append(_entry_row(
            now - (i + 1) * 10, provider="openai", model=f"model-{i:02d}",
            in_cached=0, cache_write_tokens=0, in_uncached=0,
            out=(11 - i) * 100, cost=0.0,
        ))
    _seed_rows(rows, db_path)
    s = obs.query_usage_summary(timeframe="1D")
    nonempty = [b for b in s["buckets"] if int(b.get("tokens", 0)) > 0]
    assert nonempty
    b = nonempty[0]
    # At most 8 named series + 1 "other".
    assert len(b["by_model"]) <= 9
    names = [e["name"] for e in b["by_model"]]
    # The remainder is folded into exactly one "other" entry.
    assert names.count("other") <= 1
    # The union of kept + other covers every model's tokens.
    series_tok = sum(e["tokens"] for e in b["by_model"])
    assert series_tok == b["tokens"], (
        f"series tokens {series_tok} != bucket tokens {b['tokens']}"
    )


def test_by_provider_top10_fold_into_other(tmp_usage_db):
    db_path = tmp_usage_db[0]
    now = _now_epoch()
    rows = []
    for i in range(12):
        rows.append(_entry_row(
            now - (i + 1) * 10, provider=f"prov-{i:02d}", model="shared-model",
            in_cached=0, cache_write_tokens=0, in_uncached=0,
            out=(12 - i) * 50, cost=0.0,
        ))
    _seed_rows(rows, db_path)
    s = obs.query_usage_summary(timeframe="1D")
    nonempty = [b for b in s["buckets"] if int(b.get("tokens", 0)) > 0]
    assert nonempty
    b = nonempty[0]
    assert len(b["by_provider"]) <= 11
    series_tok = sum(e["tokens"] for e in b["by_provider"])
    assert series_tok == b["tokens"]


def test_no_other_when_under_top_n(tmp_usage_db):
    db_path = tmp_usage_db[0]
    now = _now_epoch()
    rows = [
        _entry_row(now - 60, provider="openai", model="gpt-4o", out=10, cost=0.0),
        _entry_row(now - 50, provider="anthropic", model="claude-sonnet-5", out=20, cost=0.0),
    ]
    _seed_rows(rows, db_path)
    s = obs.query_usage_summary(timeframe="1D")
    nonempty = [b for b in s["buckets"] if int(b.get("tokens", 0)) > 0]
    assert nonempty
    b = nonempty[0]
    assert "other" not in [e["name"] for e in b["by_model"]]


# ── series tokens sum <= bucket tokens (+ other) ───────────────────────────

def test_series_sum_reconciles_bucket_tokens(tmp_usage_db):
    db_path = tmp_usage_db[0]
    now = _now_epoch()
    rows = []
    for i in range(20):
        rows.append(_entry_row(
            now - (i + 1) * 5, provider=f"p{i % 5}", model=f"m{i % 7}",
            in_cached=i, cache_write_tokens=1, in_uncached=2, out=i + 3, cost=0.001,
        ))
    _seed_rows(rows, db_path)
    s = obs.query_usage_summary(timeframe="1D")
    for b in s["buckets"]:
        if int(b.get("tokens", 0)) == 0:
            assert b["by_model"] == [] and b["by_provider"] == []
            continue
        by_model_tok = sum(e["tokens"] for e in b["by_model"])
        by_prov_tok = sum(e["tokens"] for e in b["by_provider"])
        # With the "other" fold, the per-bucket series MUST sum exactly to the
        # bucket total (every row is accounted for exactly once).
        assert by_model_tok == b["tokens"], (
            f"by_model {by_model_tok} != bucket {b['tokens']}"
        )
        assert by_prov_tok == b["tokens"], (
            f"by_provider {by_prov_tok} != bucket {b['tokens']}"
        )
        # Series tokens never exceed the bucket total (other-fold invariant).
        assert by_model_tok <= b["tokens"] + 0  # equality expected here
        assert by_prov_tok <= b["tokens"] + 0


# ── fail-open ──────────────────────────────────────────────────────────────

def test_bucket_series_fail_open_on_bad_conn():
    """A broken connection object must yield empty series without raising.

    _build_buckets_for_sql is called with a live conn in production, but the
    series helper must be defensive: any exception returns [] so the chart
    still renders with cost/requests.
    """
    class BadConn:
        def execute(self, *a, **k):
            raise sqlite3.OperationalError("simulated broken connection")

    bad = BadConn()
    # count + cost queries run directly on conn and will raise on BadConn —
    # _build_buckets_for_sql has no try/except around count/cost, so wrap the
    # call at the summary boundary instead and confirm the series helper alone
    # is fail-open.
    by_model = obs._bucket_series(bad, "1=1", [], 0, 1, "model", top_n=8)
    by_provider = obs._bucket_series(bad, "1=1", [], 0, 1, "provider", top_n=10)
    assert by_model == []
    assert by_provider == []


def test_summary_fail_open_on_missing_db(tmp_usage_db):
    """A non-existent DB path must still return a well-formed summary."""
    obs.usage_db_path = "/proc/nonexistent/path/missing.sqlite3"
    try:
        s = obs.query_usage_summary(timeframe="1D")
        assert "buckets" in s
        assert "totals" in s
        # Buckets degrade gracefully to the empty/zeros shape (fail-open).
        for b in s["buckets"]:
            assert b["requests"] == 0
            assert b["tokens"] == 0
            assert b["by_model"] == []
            assert b["by_provider"] == []
    finally:
        obs.usage_db_path = None


# ── backward-compat keys ──────────────────────────────────────────────────

def test_bucket_backward_compat_keys_unchanged(tmp_usage_db):
    db_path = tmp_usage_db[0]
    now = _now_epoch()
    rows = [
        _entry_row(now - 60, provider="openai", model="gpt-4o",
                   in_cached=1, cache_write_tokens=1, in_uncached=1, out=1, cost=0.005),
    ]
    _seed_rows(rows, db_path)
    s = obs.query_usage_summary(timeframe="1D")
    for b in s["buckets"]:
        # Legacy keys still present and correctly typed.
        for k in ("key", "label", "start", "end", "requests", "cost"):
            assert k in b, f"missing legacy key {k}"
        assert isinstance(b["requests"], int)
        assert isinstance(b["cost"], float)
        # New keys always present (even on empty buckets).
        for k in ("tokens", "by_model", "by_provider"):
            assert k in b
    # requests/cost still match the row count / cost sum for the populated bucket.
    nonempty = [b for b in s["buckets"] if b["requests"] > 0]
    assert nonempty
    b = nonempty[0]
    assert b["requests"] == 1
    assert b["cost"] == round(0.005, 6)


def test_summary_payload_has_no_raw_rows(tmp_usage_db):
    """The summary must never leak raw usage rows — only aggregates."""
    db_path = tmp_usage_db[0]
    now = _now_epoch()
    _seed_rows([_entry_row(now - 60, provider="openai", model="gpt-4o",
                           in_cached=1, out=1, cost=0.001)], db_path)
    s = obs.query_usage_summary(timeframe="1D")
    text = repr(s)
    # No raw column tuples / row ids should surface anywhere in the payload.
    assert "ts_epoch" not in text or "ts_epoch" not in str(s.get("buckets"))
    for b in s["buckets"]:
        assert isinstance(b.get("by_model"), list)
        assert isinstance(b.get("by_provider"), list)
        for e in b["by_model"] + b["by_provider"]:
            assert set(e.keys()) == {"name", "requests", "cost", "tokens"}


def test_buckets_are_chronological_oldest_to_newest(tmp_usage_db):
    """Buckets must be emitted oldest -> newest (ascending start) so the
    chart x-axis flows left-to-right chronologically (regression: the axis
    previously rendered reversed, newest-first)."""
    db_path = tmp_usage_db[0]
    now = _now_epoch()
    _seed_rows([_entry_row(now - 60, provider="openai", model="gpt-4o",
                           in_cached=1, out=1, cost=0.001)], db_path)
    for tf in ("today", "1D", "7D", "1M", "3M", "6M"):
        s = obs.query_usage_summary(timeframe=tf)
        buckets = s["buckets"]
        assert len(buckets) >= 2, f"{tf}: expected multiple buckets"
        starts = [b["start"] for b in buckets]
        assert starts == sorted(starts), (
            f"{tf}: buckets not in ascending time order (oldest first). "
            f"starts={starts}"
        )
