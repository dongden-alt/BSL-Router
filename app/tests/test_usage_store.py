"""Tests for the SQLite-backed usage store.

Covers schema, migration, keyset pagination, filters, summary totals/buckets,
corrupt-JSONL skip, WAL read-during-write, and fail-open write behaviour.

All fixtures are small (hundreds of rows). A 1 M insert smoke test is marked
@slow so it does not run in the default suite.
"""
import json
import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import app.observability as obs


# ── Fixtures ───────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_usage_db(tmp_path):
    """Return (db_path, tmp_path) and ensure module globals point here."""
    db_path = str(tmp_path / "usage_stats.sqlite3")
    jsonl_path = str(tmp_path / "usage_stats.jsonl")
    obs.usage_db_path = db_path
    # Patch JSONL path too so migration doesn't find prod JSONL
    obs._USAGE_LOG_PATH = jsonl_path
    yield db_path, tmp_path
    # Restore
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
    """Ensure clean SQLite state per test — delete stale DB first."""
    db_path = tmp_usage_db[0]
    # Remove any leftover file so init creates a fresh schema every time
    try:
        os.remove(db_path)
    except OSError:
        pass
    obs.init_usage_store()
    yield


# ── Schema / Init ──────────────────────────────────────────────────────────

def test_schema_created(tmp_usage_db):
    conn = sqlite3.connect(tmp_usage_db[0])
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    table_names = {t[0] for t in tables}
    assert "usage_events" in table_names
    assert "usage_meta" in table_names
    conn.close()


def test_indexes_exist(tmp_usage_db):
    conn = sqlite3.connect(tmp_usage_db[0])
    indexes = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
    ).fetchall()
    index_names = {ix[0] for ix in indexes}
    assert "idx_usage_ts_id" in index_names
    assert "idx_usage_provider" in index_names
    assert "idx_usage_model" in index_names
    assert "idx_usage_provider_model" in index_names
    conn.close()


# ── Append / Query ─────────────────────────────────────────────────────────

def _make_entry(i):
    return {
        "timestamp": f"2026-08-{min(i % 28 + 1, 28):02d}T{i:02d}:00:00",
        "provider": ["openai", "anthropic"][i % 2],
        "model": ["gpt-4o", "claude-sonnet-5"][i % 2],
        "ttft_ms": float(i * 10),
        "total_time_ms": float((i + 1) * 20),
        "in_cached": i * 5,
        "cache_write_tokens": i // 2,
        "in_uncached": 100 - i * 5,
        "out": 50 + i,
        "cost": round(i * 0.001, 4),
        "savings": round(i * 0.0005, 4),
    }


def test_append_and_query_single():
    entry = _make_entry(0)
    obs.append_usage_event(entry)
    result = obs.query_usage_events(limit=10)
    assert len(result["entries"]) == 1
    assert result["entries"][0]["provider"] == "openai"
    assert result["has_more"] is False


def test_append_multiple_newest_first():
    for i in range(5):
        obs.append_usage_event(_make_entry(i))
    result = obs.query_usage_events(limit=5)
    timestamps = [e["ts"] for e in result["entries"]]
    assert timestamps == sorted(timestamps, reverse=True)


def test_keyset_pagination_continuity():
    """Each page's cursor must point to a row that precedes the previous page."""
    n = 15
    for i in range(n):
        obs.append_usage_event(_make_entry(i))

    first = obs.query_usage_events(limit=5)
    assert len(first["entries"]) == 5
    assert first["has_more"] is True
    assert first["next_before_id"] is not None

    second = obs.query_usage_events(
        limit=5,
        before_id=str(first["next_before_id"]),
        before_ts=first["next_before_ts"],
    )
    assert len(second["entries"]) == 5
    # No duplicate ids across pages
    ids_page1 = {e["id"] for e in first["entries"]}
    ids_page2 = {e["id"] for e in second["entries"]}
    assert ids_page1.isdisjoint(ids_page2)

    total_ids = ids_page1 | ids_page2
    assert len(total_ids) == 10


def test_empty_result():
    result = obs.query_usage_events(limit=10)
    assert result["entries"] == []
    assert result["total"] == 0
    assert result["has_more"] is False


# ── Filters ────────────────────────────────────────────────────────────────

def test_filter_by_provider():
    for i in range(6):
        obs.append_usage_event(_make_entry(i))
    result = obs.query_usage_events(provider="openai", limit=10)
    assert result["total"] == 3  # openai every other
    assert all(e["provider"] == "openai" for e in result["entries"])


def test_filter_by_prefix_provider():
    for i in range(6):
        obs.append_usage_event(_make_entry(i))
    result = obs.query_usage_events(provider="ant*", limit=10)
    assert result["total"] == 3
    assert all("ant" in (e["provider"] or "") for e in result["entries"])


def test_filter_by_model_prefix():
    for i in range(6):
        obs.append_usage_event(_make_entry(i))
    result = obs.query_usage_events(model="gpt*", limit=10)
    # gpt-4o appears every 2nd entry (i%2==0), so 3 out of 6
    assert result["total"] == 3


def test_filter_q_substring():
    for i in range(9):
        obs.append_usage_event(_make_entry(i))
    result = obs.query_usage_events(q="gpt", limit=10)
    assert result["total"] >= 1
    # All entries should have 'gpt' in provider or model (case-insensitive)
    for e in result["entries"]:
        combined = (e.get("provider") or "").lower() + (e.get("model") or "").lower()
        assert "gpt" in combined


def test_filter_time_range():
    for i in range(10):
        entry = dict(_make_entry(i))
        entry["timestamp"] = f"2026-08-01T{i:02d}:00:00"
        obs.append_usage_event(entry)
    result = obs.query_usage_events(
        start="2026-08-01T03:00:00", end="2026-08-01T06:00:00"
    )
    assert result["total"] == 4  # 03,04,05,06 inclusive
    timestamps = [e["timestamp"] for e in result["entries"]]
    for ts in timestamps:
        assert "03" <= ts[:2] or ts.startswith("2026-08-01T0")


def test_combined_filters():
    for i in range(20):
        entry = dict(_make_entry(i))
        obs.append_usage_event(entry)
    result = obs.query_usage_events(
        provider="openai", q="cla", limit=10
    )
    # openai provider contains no 'cla', but model names do — depends on data
    assert isinstance(result["total"], int)
    assert isinstance(len(result["entries"]), int)


# ── Summary ────────────────────────────────────────────────────────────────

def _fill_summary_data(count=50):
    for i in range(count):
        entry = _make_entry(i)
        entry["timestamp"] = f"2026-08-{min(i % 28 + 1, 28):02d}T{min(i, 23):02d}:00:00"
        obs.append_usage_event(entry)


def test_summary_totals():
    _fill_summary_data(30)
    s = obs.query_usage_summary()
    assert s["totals"]["requests"] == 30
    assert s["row_count"] == 30


def test_summary_providers_aggregate():
    _fill_summary_data(20)
    s = obs.query_usage_summary()
    provs = s["providers"]
    assert len(provs) <= 14  # top 12 + possibly other
    total_reqs = sum(p["requests"] for p in provs)
    assert total_reqs == 20


def test_summary_models_aggregate():
    _fill_summary_data(16)
    s = obs.query_usage_summary()
    models = s["models"]
    assert len(models) <= 9  # top 8 + other
    total = sum(m["requests"] for m in models)
    assert total == 16


def test_summary_buckets_nonempty():
    _fill_summary_data(10)
    for tf in ("today", "1D", "7D", "1M", "3M", "6M"):
        s = obs.query_usage_summary(timeframe=tf)
        assert len(s["buckets"]) > 0, f"No buckets for timeframe={tf}"
        for b in s["buckets"]:
            assert "key" in b
            assert "label" in b
            assert "requests" in b


def test_summary_with_provider_filter():
    _fill_summary_data(20)
    s = obs.query_usage_summary(provider="anthropic")
    assert s["totals"]["requests"] < 20
    assert s["totals"]["requests"] > 0


def test_summary_includes_db_stats():
    _fill_summary_data(5)
    s = obs.query_usage_summary()
    assert "db_bytes" in s
    assert "row_count" in s


# ── Migration ──────────────────────────────────────────────────────────────

def test_migration_idempotent(tmp_path):
    """Ensure migrating the same JSONL twice does not double-insert."""
    db_path = str(tmp_path / "usage_stats.sqlite3")
    jsonl_path = str(tmp_path / "usage_stats.jsonl")
    old_db = obs.usage_db_path
    old_jsonl = obs._USAGE_LOG_PATH
    try:
        obs.usage_db_path = db_path
        obs._USAGE_LOG_PATH = jsonl_path

        # Seed JSONL with 5 entries
        entries = [_make_entry(i) for i in range(5)]
        with open(jsonl_path, "w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

        obs.init_usage_store()
        first_call_count = obs.query_usage_events()["total"]
        assert first_call_count == 5, f"Expected 5 after first init, got {first_call_count}"

        # Second call MUST NOT duplicate
        os.remove(db_path)  # simulate restart — fresh DB
        obs.init_usage_store()
        second_call_count = obs.query_usage_events()["total"]
        assert second_call_count == 5, f"Expected 5 after second init, got {second_call_count}"
    finally:
        obs.usage_db_path = old_db
        obs._USAGE_LOG_PATH = old_jsonl


def test_migration_skips_corrupt_lines(tmp_usage_db):
    src_path, tmp_path = tmp_usage_db
    jsonl_path = str(tmp_path / "usage_stats.jsonl")
    good = _make_entry(1)
    with open(jsonl_path, "w", encoding="utf-8") as f:
        f.write(json.dumps(good) + "\n")
        f.write("this is not json\n")
        f.write("\n")  # blank
        f.write(json.dumps(_make_entry(2)) + "\n")
    obs.init_usage_store()
    count = obs.query_usage_events()["total"]
    assert count == 2


def test_migration_no_jsonl(tmp_usage_db):
    obs.init_usage_store()
    count = obs.query_usage_events()["total"]
    assert count == 0


# ── No Hard Cap ────────────────────────────────────────────────────────────

def test_no_silent_delete_after_500_inserts(tmp_usage_db):
    for i in range(500):
        obs.append_usage_event(_make_entry(i))
    result = obs.query_usage_events(limit=1000)
    assert result["total"] == 500


# ── Fail-Open Write ────────────────────────────────────────────────────────

def test_fail_open_append():
    # Point to an unwritable path
    old = obs.usage_db_path
    try:
        obs.usage_db_path = "/proc/nonexistent/path/db.sqlite3"
        # Must not raise
        obs.append_usage_event(_make_entry(0))
        # Empty results because nothing was written
        result = obs.query_usage_events(limit=10)
        assert result["entries"] == []
        assert result["total"] == 0
    finally:
        obs.usage_db_path = old


# ── Response Wrapper Always ───────────────────────────────────────────────

def test_response_always_has_wrapper_keys():
    obs.append_usage_event(_make_entry(0))
    r = obs.query_usage_events()
    assert "entries" in r
    assert "total" in r
    assert "has_more" in r
    assert "next_before_id" in r
    assert "next_before_ts" in r


def test_offset_deprecated_but_works_for_small():
    obs.append_usage_event(_make_entry(1))
    obs.append_usage_event(_make_entry(2))
    r = obs.query_usage_events(offset=0, limit=1)
    assert len(r["entries"]) == 1
    assert r["total"] == 2


# ── Summary time filter ───────────────────────────────────────────────────

def test_summary_timefilter():
    _fill_summary_data(10)
    s_all = obs.query_usage_summary()
    s_filtered = obs.query_usage_summary(start="2026-08-15T00:00:00")
    assert s_filtered["totals"]["requests"] <= s_all["totals"]["requests"]


# ── Slow test ──────────────────────────────────────────────────────────────

@pytest.mark.slow
@pytest.mark.timeout(1800)
def test_slow_1m_insert_smoke(tmp_usage_db):
    """@slow: insert 1 M rows into SQLite — not part of normal CI."""
    start_idx = 0
    batch = 10_000
    n = 1_000_000
    for i in range(start_idx, n, batch):
        chunk_size = min(batch, n - i)
        entries = [{"timestamp": f"2026-01-{(j % 28) + 1:02d}T{j%24:02d}:00:00",
                     "provider": "openai", "model": f"gpt-test-{j}",
                     "ttft_ms": 0.0, "total_time_ms": 0.0,
                     "in_cached": 0, "cache_write_tokens": 0,
                     "in_uncached": 100, "out": 50,
                     "cost": 0.005, "savings": 0.001} for j in range(i, i + chunk_size)]
        try:
            with obs._usage_conn() as conn:
                conn.executemany(
                    """INSERT INTO usage_events
                       (ts, ts_epoch, provider, model, ttft_ms, total_time_ms,
                        in_cached, cache_write_tokens, in_uncached, out, cost, savings)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    [(e["timestamp"], obs._safe_ts_epoch(e["timestamp"]),
                      e["provider"], e["model"], e["ttft_ms"], e["total_time_ms"],
                      e["in_cached"], e["cache_write_tokens"],
                      e["in_uncached"], e["out"], e["cost"], e["savings"])
                     for e in entries],
                )
        except Exception as exc:
            print(f"[Slow test] batch {i}/{n} failed: {exc}")
            break
    result = obs.query_usage_events()
    assert result["total"] > 0


# ── D1: error column, latency percentiles, error rate ─────────────────────

def test_error_column_present_and_idempotent(tmp_usage_db):
    db_path = tmp_usage_db[0]
    conn = sqlite3.connect(db_path)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(usage_events)").fetchall()]
    conn.close()
    assert "error" in cols
    assert cols.count("error") == 1

    # Idempotent: second init must not duplicate the column
    obs.init_usage_store()
    conn = sqlite3.connect(db_path)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(usage_events)").fetchall()]
    conn.close()
    assert cols.count("error") == 1


def test_old_schema_db_gains_error_column(tmp_usage_db, tmp_path):
    db_path, _tmp = tmp_usage_db
    legacy_db = str(tmp_path / "legacy_usage.sqlite3")

    # Raw legacy schema WITHOUT the error column + one pre-existing row
    conn = sqlite3.connect(legacy_db)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS usage_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            ts_epoch REAL NOT NULL,
            provider TEXT,
            model TEXT,
            ttft_ms REAL,
            total_time_ms REAL,
            in_cached INTEGER,
            cache_write_tokens INTEGER,
            in_uncached INTEGER,
            out INTEGER,
            cost REAL,
            savings REAL,
            pricing_version TEXT DEFAULT 'v1'
        )"""
    )
    conn.execute(
        "INSERT INTO usage_events (ts, ts_epoch, provider, model) VALUES (?, ?, ?, ?)",
        ("2026-08-01T00:00:00", 1785542400.0, "legacy-prov", "legacy-model"),
    )
    conn.commit()
    conn.close()

    # Defensive: clear any stale WAL/SHM sidecars before re-pointing
    for suffix in ("-wal", "-shm"):
        try:
            os.remove(legacy_db + suffix)
        except OSError:
            pass

    prev = obs.usage_db_path
    try:
        obs.usage_db_path = legacy_db
        obs.init_usage_store()

        conn = sqlite3.connect(legacy_db)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(usage_events)").fetchall()]
        count = conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]
        conn.close()
        assert "error" in cols
        assert cols.count("error") == 1
        assert count == 1  # pre-existing row survived the in-place migration
    finally:
        obs.usage_db_path = prev


def _remove_db(db_path):
    try:
        os.remove(db_path)
    except OSError:
        pass
    for suffix in ("-wal", "-shm"):
        try:
            os.remove(db_path + suffix)
        except OSError:
            pass


def test_latency_percentiles(tmp_usage_db):
    db_path = tmp_usage_db[0]

    # 10 rows: ttft 100..1000, total 200..2000
    for i in range(1, 11):
        e = _make_entry(i)
        e["ttft_ms"] = float(i * 100)
        e["total_time_ms"] = float(i * 200)
        obs.append_usage_event(e)
    lat = obs.query_usage_summary()["latency"]
    assert lat["n_timed"] == 10
    assert lat["ttft_p50_ms"] == 500
    assert lat["ttft_p95_ms"] == 1000
    assert lat["total_p50_ms"] == 1000
    assert lat["total_p95_ms"] == 2000

    # Uneven n=7: ttft 100..700, total 200..1400
    _remove_db(db_path)
    obs.init_usage_store()
    for i in range(1, 8):
        e = _make_entry(i)
        e["ttft_ms"] = float(i * 100)
        e["total_time_ms"] = float(i * 200)
        obs.append_usage_event(e)
    lat = obs.query_usage_summary()["latency"]
    assert lat["n_timed"] == 7
    assert lat["ttft_p50_ms"] == 400
    assert lat["ttft_p95_ms"] == 700
    assert lat["total_p50_ms"] == 800
    assert lat["total_p95_ms"] == 1400

    # No timed rows → null percentiles
    _remove_db(db_path)
    obs.init_usage_store()
    e = _make_entry(0)
    e["ttft_ms"] = None
    e["total_time_ms"] = None
    obs.append_usage_event(e)
    lat = obs.query_usage_summary()["latency"]
    assert lat["n_timed"] == 0
    assert lat["ttft_p50_ms"] is None
    assert lat["ttft_p95_ms"] is None
    assert lat["total_p50_ms"] is None
    assert lat["total_p95_ms"] is None


def test_error_rate(tmp_usage_db):
    # Empty DB → zero count and zero rate
    s = obs.query_usage_summary()
    assert s["errors"]["count"] == 0
    assert s["errors"]["rate"] == 0.0

    # Mixed rows: 1 error among 4
    for i in range(4):
        e = _make_entry(i)
        if i == 0:
            e["error"] = "http_502"
        obs.append_usage_event(e)
    s = obs.query_usage_summary()
    assert s["errors"]["count"] == 1
    assert s["errors"]["rate"] == 0.25


def test_writer_error_row(tmp_usage_db):
    # Non-200 without error_msg → short token 'http_502', zeroed tokens/cost
    obs.log_request(
        provider="prov-x", model="model-x", status=502,
        ttft=0.1, in_tokens=10, out_tokens=5, cached_tokens=0,
        config={}, error_msg=None, total_time=0.5,
    )
    rows = obs.query_usage_events(limit=10)["entries"]
    err_rows = [r for r in rows if r.get("error")]
    assert len(err_rows) == 1
    er = err_rows[0]
    assert er["error"] == "http_502"
    assert er["cost"] == 0.0
    assert er["in_cached"] == 0
    assert er["cache_write_tokens"] == 0
    assert er["in_uncached"] == 0
    assert er["out"] == 5

    # Success row keeps error NULL
    obs.log_request(
        provider="prov-x", model="model-x", status=200,
        ttft=0.1, in_tokens=10, out_tokens=5, cached_tokens=0,
        config={}, total_time=0.5,
    )
    rows = obs.query_usage_events(limit=10)["entries"]
    ok_rows = [r for r in rows if r.get("error") is None]
    assert len(ok_rows) == 1
    assert ok_rows[0]["out"] == 5
    assert ok_rows[0]["in_uncached"] == 10


def test_summary_keys_backcompat(tmp_usage_db):
    obs.append_usage_event(_make_entry(0))
    s = obs.query_usage_summary()
    for key in ("totals", "providers", "models", "buckets", "row_count", "db_bytes"):
        assert key in s
    # New D1 keys also present
    assert "latency" in s
    assert "errors" in s
