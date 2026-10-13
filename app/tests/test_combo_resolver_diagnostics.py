"""Unit tests for combo-resolution drop diagnostics.

Background (2026-10-13): the Vision combo collapsed 6 -> 1 entries at
resolution (5 leaves disabled/missing upstream), but the only log line was
``[Combo] Vision > antigravity/gemini-pro-agent [1/1, fallback-primary]`` —
the drops were completely silent, so a hollowed-out chain was invisible until
a 404 surfaced. resolve_combo now prints:

- ``[Combo] <alias>: dropped N invalid chain entr(y|ies) at resolution -> ...``
- ``[Combo] <alias>: ban pre-filter dropped N entr(y|ies) -> ...``

These tests pin that behavior so a future refactor cannot re-silence it.
"""

import logging

import pytest

from app.routing.combo_resolver import resolve_combo


def _config_with_combo(chain):
    return {
        "providers": {
            "provok": {
                "models": [{"id": "model-ok", "enabled": True}],
                "connections": [{"enabled": True, "base_url": "http://x", "api_key": "k"}],
            },
            "provdead": {
                "models": [{"id": "model-dead", "enabled": False}],
                "connections": [{"enabled": True, "base_url": "http://x", "api_key": "k"}],
            },
            "provnoc": {
                "models": [{"id": "model-noc", "enabled": True}],
                "connections": [],
            },
        },
        "combos": [
            {
                "alias": "TestCombo",
                "strategy": "fallback",
                "chain": chain,
            }
        ],
        "aliases": {},
    }


def test_resolve_combo_logs_dropped_invalid_entries(caplog):
    config = _config_with_combo(
        [
            {"provider": "provok", "model": "model-ok"},
            {"provider": "provdead", "model": "model-dead"},  # disabled
            {"provider": "provnoc", "model": "model-noc"},  # no active conn
            {"provider": "provmissing", "model": "model-x"},  # provider absent
        ]
    )
    with caplog.at_level(logging.INFO):
        with pytest.MonkeyPatch.context() as mp:
            # resolve_combo logs via print(); capture stdout instead.
            import io
            import sys

            buf = io.StringIO()
            mp.setattr(sys, "stdout", buf)
            res = resolve_combo("TestCombo", None, config, {})
    out = buf.getvalue()
    assert res.matched is True
    # Healthy leaf survives and becomes the primary.
    assert res.provider_name == "provok"
    assert res.target_model == "model-ok"
    assert len(res.active_chain) == 1
    # Drop diagnostic names every invalid entry.
    assert "dropped 3 invalid chain entries at resolution" in out
    assert "provdead/model-dead" in out
    assert "provnoc/model-noc" in out
    assert "provmissing/model-x" in out


def test_resolve_combo_no_drop_line_when_all_valid(caplog):
    config = _config_with_combo([{"provider": "provok", "model": "model-ok"}])
    import io
    import sys

    buf = io.StringIO()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sys, "stdout", buf)
        res = resolve_combo("TestCombo", None, config, {})
    out = buf.getvalue()
    assert res.matched is True
    assert "dropped" not in out
    assert "fallback-primary" in out  # normal primary-selection line intact


def test_resolve_combo_ban_prefilter_logs_drop():
    config = _config_with_combo(
        [
            {"provider": "provdead", "model": "model-dead"},  # dropped at validation
            {"provider": "provok", "model": "model-ok"},  # banned by pre-filter
        ]
    )

    import app.error_prevention as ep

    orig = ep.check_ban
    ep.check_ban = lambda cfg, p, m: (
        (True, "soft", 60) if (p, m) == ("provok", "model-ok") else (False, None, None)
    )
    try:
        import io
        import sys

        buf = io.StringIO()
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(sys, "stdout", buf)
            res = resolve_combo("TestCombo", None, config, {})
    finally:
        ep.check_ban = orig

    out = buf.getvalue()
    assert res.matched is True
    # provok was the only valid leaf; ban filter empties the chain, so the
    # resolver keeps the original (all-banned) list for the downstream 503.
    assert "ban pre-filter dropped 1 entry" in out
    assert "provok/model-ok" in out
