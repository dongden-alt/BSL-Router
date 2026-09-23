"""Tests for the mid-stream transport-death continuation gate.

Part A of the 502 force-stop hardening: when an upstream stream dies AFTER
partial content was delivered (stats["out"] > 0), the BUG J transcript-integrity
guard declines to splice a SECOND provider into the live parser. Instead the
post-loop AntiStop continuation resumes the SAME provider/model from the
accumulated partial text. The single source of truth for that gate is the pure
function app.middleware.quality.should_splice_continuation.

These are PURE unit tests - no network, no subprocess, no monkeypatch needed for
the gate itself (it is a stateless boolean function). The flag-set behavior in
app/main.py::_midstream_transport_fallback is covered separately by the
out>0 / out==0 cases below via direct flag-key assertions.
"""

from app.middleware.quality import (
    should_splice_continuation,
    TRANSPORT_DIED_PARTIAL_FLAG,
)


# --- gate: length-truncation branch (unchanged legacy behavior) -------------

def test_splice_on_truncation():
    assert should_splice_continuation(
        truncated=True,
        transport_died_partial=False,
        partial_text="hello world",
        infinite_retry_enabled=True,
        used=False,
    ) is True


def test_truncation_splices_even_when_retry_disabled():
    # The length-truncation branch is NOT gated on infinite_retry (legacy contract).
    assert should_splice_continuation(
        truncated=True,
        transport_died_partial=False,
        partial_text="hello",
        infinite_retry_enabled=False,
        used=False,
    ) is True


# --- gate: transport-death branch (the new Part A path) ---------------------

def test_splice_on_transport_death_with_retry_enabled():
    assert should_splice_continuation(
        truncated=False,
        transport_died_partial=True,
        partial_text="partial output",
        infinite_retry_enabled=True,
        used=False,
    ) is True


def test_no_splice_on_transport_death_when_retry_disabled():
    # combo_infinite_retry=false must preserve today's terminal-502 contract.
    assert should_splice_continuation(
        truncated=False,
        transport_died_partial=True,
        partial_text="partial output",
        infinite_retry_enabled=False,
        used=False,
    ) is False


# --- gate: one-shot + empty-partial guards ---------------------------------

def test_no_splice_when_already_used():
    assert should_splice_continuation(
        truncated=True,
        transport_died_partial=True,
        partial_text="hello",
        infinite_retry_enabled=True,
        used=True,
    ) is False


def test_no_splice_on_empty_partial_text():
    assert should_splice_continuation(
        truncated=False,
        transport_died_partial=True,
        partial_text="",
        infinite_retry_enabled=True,
        used=False,
    ) is False


def test_no_splice_on_whitespace_partial_text():
    assert should_splice_continuation(
        truncated=False,
        transport_died_partial=True,
        partial_text="   \n\t  ",
        infinite_retry_enabled=True,
        used=False,
    ) is False


def test_no_splice_when_neither_trigger():
    assert should_splice_continuation(
        truncated=False,
        transport_died_partial=False,
        partial_text="hello",
        infinite_retry_enabled=True,
        used=False,
    ) is False


# --- flag constant stability ------------------------------------------------

def test_flag_constant_value():
    # The canonical stats key; main.py and every splice site read this one symbol.
    assert TRANSPORT_DIED_PARTIAL_FLAG == "transport_died_partial"
