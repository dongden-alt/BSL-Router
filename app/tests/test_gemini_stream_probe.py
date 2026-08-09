"""Focused regression tests for Gemini stream status probing."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import app.main as main


def test_pre_stream_probe_skips_only_gemini_streams():
    active_chain = [("model", "provider", None)]

    assert main._should_probe_stream_status(True, active_chain, False) is True
    assert main._should_probe_stream_status(True, active_chain, True) is False
    assert main._should_probe_stream_status(False, active_chain, False) is False
