"""Shared antigravity model-name synonym table.

Both mitm.py and main.py must resolve IDE-internal model names to the same
config mapping key.  This module is the single source of truth — if a new
synonym is needed, add it here and both layers pick it up automatically.

Keys are IDE-internal names (what the Antigravity IDE sends in the request).
Values are lists of config mapping keys to try (in order).
"""

ANTIGRAVITY_REVERSE_SYNONYMS = {
    "gemini-3-flash-agent": ["gemini-3.5-flash-high"],
    "gemini-3.5-flash-low": ["gemini-3.5-flash-medium"],
    "gemini-pro-agent": ["gemini-3.1-pro-high", "gemini-3-pro-high"],
    "gemini-3.1-pro-low": ["gemini-3-pro-low"],
}
