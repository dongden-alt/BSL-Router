"""Empty-credential row prune (2026-09-18).

Two rows in the live config held an empty api_key and could never authenticate:

    openrouter connections[0]  name='Primary Connection'  api_key empty
    pix4k      connections[0]  name='Primary Connection'  api_key empty

These are leftovers from abandoned setup flows (a placeholder created before a
key was pasted). _dedup_connection_rows skips them because they carry neither
an `id` nor oauth+email, so its grouping never sees them.

IMPORTANT: Same-named rows routinely hold DIFFERENT credentials (verified by
hash across the live config). Collapsing by name would destroy working accounts.
"""

from app.main import _conn_has_credential, _prune_empty_connection_rows


# ── Credential detection ────────────────────────────────────────────────────


def test_row_with_api_key_is_kept():
    assert _conn_has_credential({"api_key": "sk-test-1234"})


def test_row_with_whitespace_api_key_is_dead():
    assert not _conn_has_credential({"api_key": "   "})


def test_row_with_empty_string_api_key_is_dead():
    assert not _conn_has_credential({"api_key": ""})


def test_row_with_access_token_is_kept():
    assert _conn_has_credential({"access_token": "bearer-xyz"})


def test_row_with_refresh_token_is_kept():
    assert _conn_has_credential({"refresh_token": "rt-abc123"})


def test_oauth_row_with_only_refresh_token_is_kept():
    """OAuth rows hold only a refresh_token until the first exchange."""
    assert _conn_has_credential(
        {"token_type": "oauth", "email": "user@example.com", "refresh_token": "rt-xyz"}
    )


def test_row_with_provider_data_is_kept():
    assert _conn_has_credential({"provider_data": {"session": "abc"}})


def test_row_with_all_empty_credentials_is_dead():
    assert not _conn_has_credential(
        {"api_key": "", "access_token": None, "refresh_token": "", "provider_data": None}
    )


def test_non_dict_is_never_pruned():
    """Defensive: if the shape is wrong, do not prune what we do not understand."""
    assert _conn_has_credential(None)
    assert _conn_has_credential("not a dict")
    assert _conn_has_credential([1, 2, 3])


def test_non_string_credential_is_kept():
    """Defensive: if a credential is not a string but is truthy, keep it."""
    assert _conn_has_credential({"api_key": 12345})


# ── Pruning ─────────────────────────────────────────────────────────────────


def test_all_good_rows_are_untouched():
    pcfg = {
        "connections": [
            {"name": "c1", "api_key": "sk-test-1"},
            {"name": "c2", "api_key": "sk-test-2"},
        ]
    }
    removed = _prune_empty_connection_rows(pcfg)
    assert removed == 0
    assert len(pcfg["connections"]) == 2


def test_one_empty_row_is_removed():
    pcfg = {
        "connections": [
            {"name": "Primary Connection", "api_key": ""},
            {"name": "real", "api_key": "sk-test-1"},
        ]
    }
    removed = _prune_empty_connection_rows(pcfg)
    assert removed == 1
    assert len(pcfg["connections"]) == 1
    assert pcfg["connections"][0]["name"] == "real"


def test_multiple_empty_rows_are_removed():
    pcfg = {
        "connections": [
            {"name": "placeholder1", "api_key": ""},
            {"name": "real", "api_key": "sk-live"},
            {"name": "placeholder2", "api_key": None},
        ]
    }
    removed = _prune_empty_connection_rows(pcfg)
    assert removed == 2
    assert len(pcfg["connections"]) == 1


def test_provider_with_no_connections_is_untouched():
    pcfg = {"connections": []}
    removed = _prune_empty_connection_rows(pcfg)
    assert removed == 0
    assert pcfg["connections"] == []


def test_provider_with_all_empty_rows_is_left_intact():
    """If every row is empty, leave the list untouched so the admin UI can show it."""
    pcfg = {
        "connections": [
            {"name": "placeholder1", "api_key": ""},
            {"name": "placeholder2", "api_key": None},
        ]
    }
    removed = _prune_empty_connection_rows(pcfg)
    assert removed == 0
    assert len(pcfg["connections"]) == 2


# ── connection_indexes remapping ────────────────────────────────────────────


def test_connection_indexes_are_remapped_after_prune():
    pcfg = {
        "connections": [
            {"name": "empty", "api_key": ""},
            {"name": "c1", "api_key": "sk-1"},
            {"name": "c2", "api_key": "sk-2"},
        ],
        "models": [{"id": "m1", "connection_indexes": [1, 2]}],
    }
    _prune_empty_connection_rows(pcfg)
    assert pcfg["models"][0]["connection_indexes"] == [0, 1]


def test_index_pointing_at_removed_row_is_dropped():
    pcfg = {
        "connections": [
            {"name": "empty", "api_key": ""},
            {"name": "real", "api_key": "sk-1"},
        ],
        "models": [{"id": "m1", "connection_indexes": [0, 1]}],
    }
    _prune_empty_connection_rows(pcfg)
    assert pcfg["models"][0]["connection_indexes"] == [0]


def test_duplicate_indexes_after_remap_are_deduplicated():
    """If two old indexes collapse to the same new index, keep one."""
    pcfg = {
        "connections": [
            {"name": "real", "api_key": "sk-1"},
            {"name": "empty1", "api_key": ""},
            {"name": "empty2", "api_key": None},
        ],
        "models": [{"id": "m1", "connection_indexes": [0, 1, 2]}],
    }
    _prune_empty_connection_rows(pcfg)
    assert pcfg["models"][0]["connection_indexes"] == [0]


def test_models_with_no_connection_indexes_are_untouched():
    pcfg = {
        "connections": [
            {"name": "empty", "api_key": ""},
            {"name": "real", "api_key": "sk-1"},
        ],
        "models": [{"id": "m1"}],
    }
    _prune_empty_connection_rows(pcfg)
    assert "connection_indexes" not in pcfg["models"][0]


def test_non_dict_models_do_not_crash():
    pcfg = {
        "connections": [
            {"name": "empty", "api_key": ""},
            {"name": "real", "api_key": "sk-1"},
        ],
        "models": [None, "not a dict", {"id": "m1", "connection_indexes": [1]}],
    }
    removed = _prune_empty_connection_rows(pcfg)
    assert removed == 1
    assert pcfg["models"][2]["connection_indexes"] == [0]


# ── Same-name rows with different credentials are NOT collapsed ─────────────


def test_same_name_different_keys_are_kept():
    """The trap: 'cairn' appears twice in iamhc, but the rows hold DIFFERENT keys."""
    pcfg = {
        "connections": [
            {"name": "cairn", "api_key": "sk-key-1"},
            {"name": "cairn", "api_key": "sk-key-2"},
        ]
    }
    removed = _prune_empty_connection_rows(pcfg)
    assert removed == 0
    assert len(pcfg["connections"]) == 2


def test_same_name_one_empty_only_empty_is_removed():
    pcfg = {
        "connections": [
            {"name": "Primary Connection", "api_key": ""},
            {"name": "Primary Connection", "api_key": "sk-live"},
        ]
    }
    removed = _prune_empty_connection_rows(pcfg)
    assert removed == 1
    assert len(pcfg["connections"]) == 1
    assert pcfg["connections"][0]["api_key"] == "sk-live"
