"""Tool-argument repair ladder — direct unit coverage.

This module (app/middleware/tool_arg_repair.py) had NO dedicated test file
despite being safety-critical and, as of the 2026-09-22 drop-syndrome fix,
called from the streaming hot path of the Gemini adapter — the one lane the
Antigravity IDE actually speaks.

Covers: each ladder rung in isolation, the fail-open contract (NEVER raises),
the size guard, and the invariant that matters most — a step that would
corrupt a VALID document can never win, because step 1 returns valid
documents untouched.
"""
from __future__ import annotations

import json

import pytest

from app.middleware.tool_arg_repair import (
    DEFAULT_MAX_BYTES,
    repair_json_arguments,
    repair_tool_calls_argument_strings,
)
from app.middleware.tool_arg_repair import (
    _close_truncation,
    _quote_unquoted_keys,
    _quote_unquoted_values,
    _strip_trailing_commas,
)


# ── Fast path: valid JSON is sacred ───────────────────────────────────────────

class TestValidPassthrough:
    """A valid document must survive the ladder byte-for-byte."""

    @pytest.mark.parametrize("payload", [
        '{"path": "app/main.py"}',
        '{"a": 1, "b": [1, 2, 3], "c": {"d": null}}',
        '{"esc": "quote \\" and backslash \\\\ inside"}',
        '{"unicode": "cà phê sữa đá"}',
        '{"comma_in_string": "a,}"}',
        '{"empty": {}, "arr": []}',
        '{}',
        '{"n": -12.5e3, "t": true, "f": false, "z": null}',
    ])
    def test_valid_untouched(self, payload):
        out, was_repaired = repair_json_arguments(payload)
        assert out == payload, "valid JSON must not be rewritten"
        assert was_repaired is False
        assert json.loads(out) == json.loads(payload)


# ── Rung 5: dangling comma before a closer (the drop-repro gap) ───────────────

class TestStripTrailingCommas:
    """The rung added 2026-09-22 — the 1-of-7 case the ladder used to miss."""

    @pytest.mark.parametrize("bad,expect", [
        ('{"path": "a.py",}', {"path": "a.py"}),
        ('{"a": 1, "b": 2,}', {"a": 1, "b": 2}),
        ('{"arr": [1, 2,]}', {"arr": [1, 2]}),
        ('{"k": 1 , }', {"k": 1}),
        ('{"nested": {"x": 1,},}', {"nested": {"x": 1}}),
        ('{"arr": [{"a": 1,},]}', {"arr": [{"a": 1}]}),
    ])
    def test_repairs_dangling_comma(self, bad, expect):
        out, was_repaired = repair_json_arguments(bad)
        assert was_repaired is True
        assert json.loads(out) == expect

    def test_comma_inside_string_is_never_touched(self):
        """String-awareness: a comma before '}' INSIDE a literal must survive."""
        assert _strip_trailing_commas('{"q": "a,}"}') == '{"q": "a,}"}'
        assert _strip_trailing_commas('{"q": "trailing,"}') == '{"q": "trailing,"}'
        # And end-to-end: the document is already valid, so it passes through.
        payload = '{"q": "a,}"}'
        out, was_repaired = repair_json_arguments(payload)
        assert (out, was_repaired) == (payload, False)

    def test_escaped_quote_does_not_desync_string_tracking(self):
        payload = '{"q": "he said \\" then ,}"}'
        assert _strip_trailing_commas(payload) == payload

    def test_legitimate_commas_survive(self):
        payload = '{"a": 1, "b": 2}'
        assert _strip_trailing_commas(payload) == payload


# ── Rung 4: truncation close ──────────────────────────────────────────────────

class TestCloseTruncation:
    @pytest.mark.parametrize("bad,expect", [
        ('{"pattern": "def fo', {"pattern": "def fo"}),
        ('{"path": "d.p', {"path": "d.p"}),
        ('{"pattern": "x", "opts": {"i": true', {"pattern": "x", "opts": {"i": True}}),
        ('{"arr": [1, 2', {"arr": [1, 2]}),
        ('{"a": 1,', {"a": 1}),
        ('{"a":', {"a": None}),
        ('{"deep": {"deeper": {"k": "v', {"deep": {"deeper": {"k": "v"}}}),
    ])
    def test_repairs_truncation(self, bad, expect):
        out, was_repaired = repair_json_arguments(bad)
        assert was_repaired is True
        assert json.loads(out) == expect

    def test_trailing_backslash_is_doubled_not_left_escaping_the_quote(self):
        """A buffer cut after a lone backslash must not produce '\\"' (invalid)."""
        out, was_repaired = repair_json_arguments('{"p": "C:\\\\dir\\\\')
        assert was_repaired is True
        parsed = json.loads(out)
        assert parsed["p"].startswith("C:")


# ── Rungs 2+3: bare keys and values ───────────────────────────────────────────

class TestQuoting:
    @pytest.mark.parametrize("bad,expect", [
        ('{path: "unquoted-key.py"}', {"path": "unquoted-key.py"}),
        ('{"path": app/main.py}', {"path": "app/main.py"}),
        ('{"Includes": *.js}', {"Includes": "*.js"}),
        ('{Query: foo}', {"Query": "foo"}),
    ])
    def test_repairs_quoting(self, bad, expect):
        out, was_repaired = repair_json_arguments(bad)
        assert was_repaired is True
        assert json.loads(out) == expect

    def test_json_literals_and_numbers_are_not_stringified(self):
        for payload in ('{"t": true}', '{"f": false}', '{"z": null}', '{"n": 42}'):
            out, was_repaired = repair_json_arguments(payload)
            assert (out, was_repaired) == (payload, False)

    def test_combined_quoting_and_comma(self):
        """A fragment needing MULTIPLE rungs at once."""
        out, was_repaired = repair_json_arguments('{path: "a.py",}')
        assert was_repaired is True
        assert json.loads(out) == {"path": "a.py"}


# ── Fail-open contract ────────────────────────────────────────────────────────

class TestFailOpen:
    @pytest.mark.parametrize("junk", [None, 123, [], {}, object()])
    def test_non_str_returns_input_unchanged(self, junk):
        out, was_repaired = repair_json_arguments(junk)  # type: ignore[arg-type]
        assert out is junk
        assert was_repaired is False

    @pytest.mark.parametrize("blank", ["", "   ", "\n\t "])
    def test_blank_passthrough(self, blank):
        assert repair_json_arguments(blank) == (blank, False)

    def test_oversize_input_is_refused_not_processed(self):
        big = '{"k": "' + ("x" * (DEFAULT_MAX_BYTES + 10))
        out, was_repaired = repair_json_arguments(big)
        assert (out, was_repaired) == (big, False)

    def test_unrepairable_returns_original(self):
        """Genuine garbage must NOT be forced into validity."""
        for junk in ('not json at all', '}{', '{"a": "b"} trailing garbage'):
            out, was_repaired = repair_json_arguments(junk)
            assert out == junk
            assert was_repaired is False

    def test_unbalanced_openers_are_legitimately_balanced(self):
        """'[[[[' IS repairable -> '[[[[]]]]' (nested empty arrays), which is
        valid JSON. Correct ladder behaviour, documented so it is not mistaken
        for over-eagerness. The ADAPTER layer is what refuses a non-object
        result for tool arguments (see _repair_tool_args), not the ladder."""
        out, was_repaired = repair_json_arguments('[[[[')
        assert was_repaired is True
        assert json.loads(out) == [[[[]]]]

    @pytest.mark.parametrize("hostile", [
        '{"a": "\\', '{{{{{{', '","', '{"a":,}', '\x00\x01',
        '{"' + 'k' * 500 + '": "' + 'v' * 500,
        '[' * 200, '{"a": [' * 50,
    ])
    def test_never_raises_on_hostile_input(self, hostile):
        out, was_repaired = repair_json_arguments(hostile)
        assert isinstance(out, str)
        assert isinstance(was_repaired, bool)
        if was_repaired:
            json.loads(out)  # a claimed repair MUST parse

    def test_repair_claim_is_always_truthful(self):
        """was_repaired=True implies the output parses. No exceptions."""
        samples = [
            '{"a": 1', '{a: 1}', '{"a": b}', '{"a": 1,}', '{"a": "x',
            'garbage', '', '{"valid": true}', '{"a": [1,', '{"a": {"b":',
        ]
        for s in samples:
            out, was_repaired = repair_json_arguments(s)
            if was_repaired:
                json.loads(out)


# ── Container-level helper ────────────────────────────────────────────────────

class TestRepairToolCallsArgumentStrings:
    def test_repairs_in_place_and_counts(self):
        tool_calls = [
            {"function": {"name": "a", "arguments": '{"path": "ok.py"}'}},
            {"function": {"name": "b", "arguments": '{"path": "cut'}},
            {"function": {"name": "c", "arguments": '{"p": 1,}'}},
        ]
        assert repair_tool_calls_argument_strings(tool_calls) == 2
        for tc in tool_calls:
            json.loads(tc["function"]["arguments"])

    @pytest.mark.parametrize("junk", [None, "string", 42, {}])
    def test_malformed_container_returns_zero(self, junk):
        assert repair_tool_calls_argument_strings(junk) == 0  # type: ignore[arg-type]

    def test_skips_malformed_entries_without_raising(self):
        tool_calls = [
            None,
            "not a dict",
            {"no_function": True},
            {"function": "not a dict"},
            {"function": {"arguments": 123}},
            {"function": {"name": "ok", "arguments": '{"a": 1'}},
        ]
        assert repair_tool_calls_argument_strings(tool_calls) == 1  # type: ignore[arg-type]


# ── Rung purity: helpers must not corrupt what is already valid ───────────────

class TestRungPurity:
    @pytest.mark.parametrize("fn", [
        _quote_unquoted_keys, _quote_unquoted_values,
        _close_truncation, _strip_trailing_commas,
    ])
    @pytest.mark.parametrize("payload", [
        '{"path": "app/main.py"}',
        '{"a": 1, "b": [1, 2], "c": {"d": null}}',
        '{"s": "brace } and bracket ] and comma , inside"}',
        '{"t": true, "n": -1.5}',
    ])
    def test_rung_is_identity_on_valid_json(self, fn, payload):
        """No rung may alter a valid document. This is what makes the ladder
        safe to run on every parse failure without risking correct calls."""
        assert fn(payload) == payload
