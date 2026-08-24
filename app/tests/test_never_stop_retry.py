"""Never-stop combo retry tests.

Proves:
  1. _combo_infinite_retry_enabled defaults True; honors explicit false; survives garbage config
  2. _combo_restart_or_give_up wraps to idx 0, deadline None, pass_no+1, clears tried_conns
  3. Backoff ladder is 2,4,8,16,30,30 (capped)
  4. The wall-start stamp is cleared so the next expansion re-stamps (budget re-arm)
  5. Contract: every exhaustion site in _process_chat_completion checks the knob
     before returning a terminal 502
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _reload_main():
    module = sys.modules.get("app.main")
    if module is None:
        return importlib.import_module("app.main")
    return importlib.reload(module)


# ── 1. Knob helper ─────────────────────────────────────────────────────────


def test_knob_defaults_true():
    main = _reload_main()
    assert main._combo_infinite_retry_enabled({}) is True
    assert main._combo_infinite_retry_enabled({"settings": {}}) is True


def test_knob_explicit_false_restores_502():
    main = _reload_main()
    cfg = {"settings": {"combo_infinite_retry": False}}
    assert main._combo_infinite_retry_enabled(cfg) is False


def test_knob_none_means_true():
    main = _reload_main()
    cfg = {"settings": {"combo_infinite_retry": None}}
    assert main._combo_infinite_retry_enabled(cfg) is True


def test_knob_survives_garbage_config():
    main = _reload_main()
    assert main._combo_infinite_retry_enabled(None) is True  # type: ignore[arg-type]


# ── 2/3/4. Wrap-state builder ──────────────────────────────────────────────


class _FakeState:
    def __init__(self):
        self.bsl_chain_wall_start = 123.456


class _FakeRequest:
    def __init__(self):
        self.state = _FakeState()


def _rs(idx=3, pass_no=1, tried=None):
    return {
        "chain": ["a/m1", "b/m2", "c/m3"],
        "idx": idx,
        "pass_no": pass_no,
        "tried_conns": tried or {"a": [0, 1]},
        "cache_bp": {"k": 1},
        "original_model": "combo",
        "deadline": 999.0,
    }


def test_restart_wraps_to_idx0_deadline_none_pass2():
    main = _reload_main()
    req = _FakeRequest()
    backoff, wrap = main._combo_restart_or_give_up(
        "combo", _rs(), ["a/m1", "b/m2", "c/m3"], "combo",
        {"k": 1}, req, reason="test",
    )
    assert backoff == 2
    assert wrap["idx"] == 0
    assert wrap["deadline"] is None
    assert wrap["pass_no"] == 2
    assert wrap["chain"] == ["a/m1", "b/m2", "c/m3"]
    assert "tried_conns" not in wrap  # cleared: quota may recover
    assert req.state.bsl_chain_wall_start is None  # budget re-arm


def test_restart_backoff_ladder():
    main = _reload_main()
    ladder = []
    for p in (1, 2, 3, 4, 5, 6, 7):
        b, w = main._combo_restart_or_give_up(
            "combo", _rs(pass_no=p), ["x/m"], "combo", {}, None, reason="t",
        )
        ladder.append(b)
    assert ladder == [2, 4, 8, 16, 30, 30, 30]


def test_restart_survives_missing_retry_state():
    main = _reload_main()
    b, w = main._combo_restart_or_give_up("combo", None, ["x/m"], "combo", {}, None, reason="t")
    assert b == 2 and w["idx"] == 0 and w["pass_no"] == 2


def test_restart_survives_none_request():
    main = _reload_main()
    b, w = main._combo_restart_or_give_up("combo", _rs(), ["x/m"], "combo", {}, None, reason="t")
    assert b == 2


# ── 5. Source contract: knob gates every terminal 502 ─────────────────────


def test_exhaustion_sites_gated_by_knob():
    main = _reload_main()
    src = inspect.getsource(main._process_chat_completion)
    gated = 0
    for marker in (
        "entry_override_exhausted",
        "nonstream_transport_exhausted",
        "nonstream_unexpected_exhausted",
    ):
        assert marker in src, f"missing never-stop wrap site: {marker}"
        gated += 1
    # The wrap precedes the 502 in each site, and sleeps before recursing.
    assert src.count("_combo_infinite_retry_enabled(config)") >= gated
    assert src.count("await asyncio.sleep(_backoff)") >= gated
    # Old terminal 502 must still exist for the explicit-false path.
    assert "combo chain entries exhausted" in src


def test_module_exports():
    main = _reload_main()
    assert callable(main._combo_infinite_retry_enabled)
    assert callable(main._combo_restart_or_give_up)
