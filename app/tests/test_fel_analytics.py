"""FEL-3 — refusal analytics tests for the SQLite usage store.

Covers the two new usage_events columns (fel, refusal_class), the guarded
migration for legacy DBs, the note_fel_context → log_request attach plumbing,
and the fel aggregate block in query_usage_summary. Mirrors the test_usage_store
fixture style (temp DB per test, fail-open module globals).
"""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import app.observability as obs


LEGACY_SCHEMA = """\
CREATE TABLE IF NOT EXISTS usage_events (
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
    pricing_version TEXT DEFAULT 'v1',
    error TEXT
);
CREATE TABLE IF NOT EXISTS usage_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@pytest.fixture
def tmp_usage_db(tmp_path):
    db_path = str(tmp_path / "usage_stats.sqlite3")
    obs.usage_db_path = db_path
    obs._USAGE_LOG_PATH = str(tmp_path / "usage_stats.jsonl")
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
    # Fresh context registry per test so cross-test leakage can't flatter us.
    obs._FEL_REQUEST_CONTEXT.clear()
    obs.init_usage_store()
    yield
    obs._FEL_REQUEST_CONTEXT.clear()


def _entry(i=0, model="glm-5.2", provider="zai", fel=None, refusal_class=None):
    e = {
        "timestamp": "2026-09-07T12:00:%02d" % i,
        "provider": provider,
        "model": model,
        "ttft_ms": 100 + i,
        "total_time_ms": 1000 + i,
        "in_cached": 0,
        "cache_write_tokens": 0,
        "in_uncached": 10,
        "out": 20,
        "cost": 0.001,
        "savings": 0.0,
    }
    if fel is not None:
        e["fel"] = fel
    if refusal_class is not None:
        e["refusal_class"] = refusal_class
    return e


# ── Schema / migration ─────────────────────────────────────────────────────

def test_fresh_schema_has_fel_columns(tmp_usage_db):
    conn = sqlite3.connect(tmp_usage_db[0])
    cols = [r[1] for r in conn.execute("PRAGMA table_info(usage_events)").fetchall()]
    conn.close()
    assert "fel" in cols
    assert "refusal_class" in cols


def test_legacy_db_migrated_in_place(tmp_usage_db):
    """A DB created before FEL-3 (no fel/refusal_class) gains both columns."""
    db_path = tmp_usage_db[0]
    os.remove(db_path)
    legacy = sqlite3.connect(db_path)
    legacy.executescript(LEGACY_SCHEMA)
    legacy.execute(
        "INSERT INTO usage_events (ts, ts_epoch, provider, model) VALUES ('t', 1.0, 'p', 'm')"
    )
    legacy.commit()
    legacy.close()

    obs.init_usage_store()

    conn = sqlite3.connect(db_path)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(usage_events)").fetchall()]
    conn.close()
    assert "fel" in cols and "refusal_class" in cols
    # init is idempotent — run it again, no error, columns intact.
    obs.init_usage_store()
    conn = sqlite3.connect(db_path)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(usage_events)").fetchall()]
    conn.close()
    assert "fel" in cols and "refusal_class" in cols


# ── Persistence ────────────────────────────────────────────────────────────

def test_append_persists_fel_fields(tmp_usage_db):
    obs.append_usage_event(_entry(0, fel="cn", refusal_class="recovered"))
    obs.append_usage_event(_entry(1))  # absent keys → NULL
    page = obs.query_usage_events(limit=10)
    rows = {r["model"]: r for r in page["entries"]}
    assert rows["glm-5.2"]["fel"] == "cn"
    assert rows["glm-5.2"]["refusal_class"] == "recovered"
    assert rows["glm-5.2"]["fel"] == "cn"  # first row shape
    assert rows.get("glm-5.2") and page["total"] == 2
    other = [r for r in page["entries"] if r["model"] == "glm-5.2"]
    # Second entry (i=1) also glm-5.2 — distinguish via refusal_class NULL.
    nulls = [r for r in page["entries"] if r["refusal_class"] is None]
    assert len(nulls) == 1 and nulls[0]["fel"] is None


def test_append_usage_event_fail_open_on_garbage(tmp_usage_db):
    obs.append_usage_event({"nope": True})
    assert obs.query_usage_events(limit=5)["total"] == 1


# ── note_fel_context → log_request plumbing ────────────────────────────────

def test_note_fel_context_attaches_to_usage_row(tmp_usage_db):
    obs.note_fel_context("req-fel-1", fel="cn", refusal_class="recovered")
    obs.log_request(
        provider="zai", model="glm-5.2", status=200, ttft=0.1, in_tokens=10,
        out_tokens=20, cached_tokens=0, config={}, request_id="req-fel-1",
        total_time=1.0,
    )
    rows = obs.query_usage_events(model="glm-5.2", limit=10)["entries"]
    assert len(rows) == 1
    assert rows[0]["fel"] == "cn"
    assert rows[0]["refusal_class"] == "recovered"
    # Registry consumed on attach.
    assert "req-fel-1" not in obs._FEL_REQUEST_CONTEXT


def test_note_fel_context_attaches_to_error_row(tmp_usage_db):
    obs.note_fel_context("req-fel-2", fel="gpt", refusal_class="refusal")
    obs.log_request(
        provider="openai", model="gpt-5.5", status=500, ttft=0.0, in_tokens=0,
        out_tokens=0, cached_tokens=0, config={}, request_id="req-fel-2",
        error_msg="upstream down", total_time=0.5,
    )
    rows = obs.query_usage_events(model="gpt-5.5", limit=10)["entries"]
    assert len(rows) == 1
    assert rows[0]["fel"] == "gpt"
    assert rows[0]["refusal_class"] == "refusal"


def test_log_request_without_fel_context_stays_null(tmp_usage_db):
    obs.log_request(
        provider="anthropic", model="claude-opus-5", status=200, ttft=0.1,
        in_tokens=5, out_tokens=5, cached_tokens=0, config={},
        request_id="req-plain", total_time=0.5,
    )
    rows = obs.query_usage_events(limit=5)["entries"]
    assert len(rows) == 1
    assert rows[0]["fel"] is None
    assert rows[0]["refusal_class"] is None


def test_note_fel_context_fail_open_and_bounded():
    # Bad input never raises.
    obs.note_fel_context(None)
    obs.note_fel_context("", fel=123, refusal_class=None)
    obs.note_fel_context("rid", fel=None, refusal_class=None)
    # Bounded registry.
    for i in range(obs._FEL_CONTEXT_MAX + 200):
        obs.note_fel_context(f"r{i}", fel="cn")
    assert len(obs._FEL_REQUEST_CONTEXT) <= obs._FEL_CONTEXT_MAX


# ── query_usage_summary fel aggregate ──────────────────────────────────────

def test_summary_fel_empty_store_is_zero_shape(tmp_usage_db):
    s = obs.query_usage_summary()
    assert s["fel"] == {
        "applied": 0, "refusals": {}, "recovered": 0,
        "by_family": {}, "by_model": {},
    }


def test_summary_fel_aggregates(tmp_usage_db):
    # cn family: 3 requests — 1 refusal, 1 recovered, 1 clean.
    obs.append_usage_event(_entry(0, model="glm-5.2", provider="zai", fel="cn", refusal_class="refusal"))
    obs.append_usage_event(_entry(1, model="glm-5.2", provider="zai", fel="cn", refusal_class="recovered"))
    obs.append_usage_event(_entry(2, model="glm-5.2", provider="zai", fel="cn", refusal_class="clean"))
    # gpt family: 1 tos_lecture.
    obs.append_usage_event(_entry(3, model="gpt-5.5", provider="openai", fel="gpt", refusal_class="tos_lecture"))
    # filter_block without directives applied (fel null).
    obs.append_usage_event(_entry(4, model="kimi-k2", provider="moonshot", refusal_class="filter_block"))
    # Non-FEL row must not count.
    obs.append_usage_event(_entry(5, model="claude-opus-5", provider="anthropic"))

    s = obs.query_usage_summary()
    fel = s["fel"]
    assert fel["applied"] == 4  # 3 cn + 1 gpt
    assert fel["refusals"]["refusal"] == 1
    assert fel["refusals"]["tos_lecture"] == 1
    assert fel["refusals"]["filter_block"] == 1
    assert fel["refusals"]["clean"] == 1
    assert fel["recovered"] == 1

    assert fel["by_family"]["cn"] == {"requests": 3, "refusals": 1, "recovered": 1}
    assert fel["by_family"]["gpt"] == {"requests": 1, "refusals": 1, "recovered": 0}
    assert "claude" not in fel["by_family"]

    # by_model: only models with hard refusals (refusal/tos_lecture), top 8.
    assert fel["by_model"]["glm-5.2"]["refusals"] == 1
    assert fel["by_model"]["glm-5.2"]["requests"] == 3
    assert fel["by_model"]["glm-5.2"]["recovered"] == 1
    assert fel["by_model"]["gpt-5.5"]["refusals"] == 1
    assert "kimi-k2" not in fel["by_model"]  # filter_block ≠ hard refusal


def test_summary_fel_respects_filters(tmp_usage_db):
    obs.append_usage_event(_entry(0, model="glm-5.2", provider="zai", fel="cn", refusal_class="refusal"))
    obs.append_usage_event(_entry(1, model="gpt-5.5", provider="openai", fel="gpt", refusal_class="recovered"))
    s = obs.query_usage_summary(provider="openai")
    assert s["fel"]["applied"] == 1
    assert "cn" not in s["fel"]["by_family"]
    assert s["fel"]["by_family"]["gpt"]["recovered"] == 1
    assert s["fel"]["recovered"] == 1


def test_summary_fel_by_model_top8_cap(tmp_usage_db):
    for i in range(10):
        obs.append_usage_event(_entry(
            i, model=f"model-{i}", provider="p", fel="cn", refusal_class="refusal",
        ))
    s = obs.query_usage_summary()
    assert len(s["fel"]["by_model"]) == 8


# ── Route passthrough (surface contract, no HTTP server needed) ────────────

def test_summary_result_serializes_fel_key(tmp_usage_db):
    import json as _json
    s = obs.query_usage_summary()
    # The usage-summary route returns query_usage_summary verbatim; the fel
    # key must be plain-JSON so JSONResponse needs no encoder support.
    payload = _json.dumps(s)
    assert '"fel"' in payload
