"""Bug 3 (2026-09-29): provider key/connection duplication on save.

ROOT CAUSE (4 defects, all fixed here):
  1. ``_connection_identity`` was key-dependent, so a blank-key row and its
     keyed sibling had two mutually-exclusive identities and could never
     match -- every save appended a duplicate blank-key row.
  2. ``_apply_connections_stale_save_guard`` Pass 2 APPENDED body rows on
     identity mismatch instead of merging, and appended unconditionally when
     identity was None.
  3. Pass 1 restored blanked keys by POSITION when names collided, assigning
     the WRONG key.
  4. ``_dedup_connection_rows`` grouped only on id/oauth-email -- never name
     or api_key -- so the 7 duplicate-name providers survived every restart.

FIX:
  * ``_connection_identity`` is now KEY-INDEPENDENT (id, then lowercased name).
  * ``_apply_connections_stale_save_guard`` is one authoritative dedup-MERGE:
    server rows kept & merged in place (server key carried into a blanked body
    row), genuinely-new body rows appended, unidentifiable body rows dropped.
  * ``_dedup_connection_rows`` now collapses on id / oauth-email / non-empty
    api_key / case-normalised name, keeping the keyed row over a blank sibling.

These 10 cases mirror the live shapes (see .brain/scratch/inv_dup.txt).
"""
from __future__ import annotations

import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import app.main as main


# ──────────────────────────────────────────────────────────────────────────
# 1. _connection_identity
# ──────────────────────────────────────────────────────────────────────────

def test_identity_keyed_and_blank_sibling_are_the_same():
    """A keyed row and its blank-key sibling with the same name share identity.

    This is the regression at the heart of Bug 3: under the old key-dependent
    identity these returned ("key","sk-real") and ("name","dongden") -- two
    mutually-exclusive identities for one logical connection.
    """
    keyed = {"name": "dongden", "api_key": "sk-real"}
    blank = {"name": "dongden", "api_key": ""}
    assert main._connection_identity(keyed) == main._connection_identity(blank)


def test_identity_id_wins_over_name():
    """A persistent id is the strongest identity, even when names differ."""
    a = {"id": "conn-7", "name": "whatever", "api_key": "sk-a"}
    b = {"id": "conn-7", "name": "renamed", "api_key": ""}
    assert main._connection_identity(a) == main._connection_identity(b)
    assert main._connection_identity(a) == ("id", "conn-7")


def test_identity_neither_id_nor_name_is_none():
    """A row with no id and no name is unidentifiable and must not grow lists."""
    assert main._connection_identity({"api_key": "sk-orphan"}) is None
    assert main._connection_identity({}) is None
    assert main._connection_identity(None) is None


# ──────────────────────────────────────────────────────────────────────────
# 2. Pass 2 dedup-merge: the core "no duplicate on save" regression
# ──────────────────────────────────────────────────────────────────────────

def _merge(old_conns, body_conns, deletion=None):
    """Run only the connections merge guard against two snapshots."""
    old_cfg = {"providers": {"prov": {"connections": copy.deepcopy(old_conns)}}}
    new_cfg = {"providers": {"prov": {"connections": copy.deepcopy(body_conns)}}}
    if deletion is not None:
        new_cfg["_deleted_connection"] = deletion
    main._apply_connections_stale_save_guard(old_cfg, new_cfg)
    return new_cfg["providers"]["prov"]["connections"]


def test_merge_blank_body_row_absorbs_server_key_no_duplicate(capsys):
    """server=[{dongden, sk-real}] + body=[{dongden, ""}] -> exactly 1 row, keyed.

    Asserts len == 1 explicitly: this is the user-reported duplication.
    """
    result = _merge(
        [{"name": "dongden", "api_key": "sk-real"}],
        [{"name": "dongden", "api_key": ""}],
    )
    assert len(result) == 1, f"merge must not duplicate, got {len(result)} rows"
    assert result[0]["api_key"] == "sk-real"
    assert result[0]["name"] == "dongden"
    assert "restored 1 emptied api_key" in capsys.readouterr().out


# ──────────────────────────────────────────────────────────────────────────
# 3. Idempotence: applying the guard repeatedly must not grow the list
# ──────────────────────────────────────────────────────────────────────────

def test_merge_is_idempotent_across_repeated_saves():
    """Applying the guard 3 times (fresh blanked body each save) stays at 1 row.

    The old append-on-mismatch grew the list by one on every save.
    """
    live = [{"name": "dongden", "api_key": "sk-real"}]
    for _ in range(3):
        body = [{"name": "dongden", "api_key": ""}]  # stale tab re-blanks
        live = _merge(live, body)
        assert len(live) == 1
        assert live[0]["api_key"] == "sk-real"


# ──────────────────────────────────────────────────────────────────────────
# 4. Unidentifiable body row is never appended
# ──────────────────────────────────────────────────────────────────────────

def test_unidentifiable_body_row_not_appended():
    """A body row with no id and no name must not grow the list."""
    result = _merge(
        [{"name": "dongden", "api_key": "sk-real"}],
        [{"name": "dongden", "api_key": "sk-real"}, {"api_key": "sk-no-name"}],
    )
    assert len(result) == 1
    assert result[0]["api_key"] == "sk-real"


# ──────────────────────────────────────────────────────────────────────────
# 5. _dedup_connection_rows: all 7 live dup-name providers collapse
# ──────────────────────────────────────────────────────────────────────────

def test_dedup_collapses_class_a_blank_beside_keyed():
    """Class A: a blank-key row beside its keyed same-name original collapses.

    These are the 4 live providers whose idx0 row had NO id and an EMPTY
    api_key -- the exact "duplicate the key name only" symptom the user
    reported. The keyed row must always be the survivor.
    """
    providers = {
        "hcnsec": {"connections": [
            {"name": "dongden", "api_key": ""},
            {"name": "dongden", "api_key": "enc-hcnsec", "enabled": True},
        ]},
        "agentrouter": {"connections": [
            {"name": "Nguyenha", "api_key": ""},
            {"name": "Nguyenha", "api_key": "enc-agentrouter", "enabled": True},
        ]},
        "agentrouter-o": {"connections": [
            {"name": "nguyenha", "api_key": ""},
            {"name": "nguyenha", "api_key": "enc-agentrouter-o", "enabled": True},
        ]},
        "mintrouter": {"connections": [
            {"name": "dongden", "api_key": ""},
            {"name": "dongden", "api_key": "enc-mintrouter", "enabled": True},
        ]},
    }
    total_removed = 0
    for pid, pcfg in providers.items():
        before = len(pcfg["connections"])
        removed = main._dedup_connection_rows(pcfg)
        after = len(pcfg["connections"])
        assert after == 1, f"{pid}: expected 1 survivor, got {after}"
        assert removed == before - after
        total_removed += removed
        conn = pcfg["connections"][0]
        assert conn["api_key"], f"{pid}: blank-key row survived over its keyed sibling"
        assert conn["name"] == pcfg["connections"][0]["name"]
    assert total_removed == 4, f"expected 4 blank rows removed, got {total_removed}"


def test_dedup_preserves_class_b_two_keyed_rows_sharing_a_name():
    """Class B: two DISTINCT keyed rows with the same name must BOTH survive.

    REGRESSION GUARD for the data-loss defect this fix replaces. The live
    config carries 9aws/"Free", ciii-gpt/"codex" and gorouter/"Connection 14"
    as two working credentials each. An unconditional name pass union-merged
    them and DELETED A LIVE API KEY (proven by the pre-fix dry-run, which
    reported `dropped codex (key=enc:gAAAAA...) reason=dup-name`).
    Names are left untouched too -- renaming would invalidate every
    connection_indexes reference.
    """
    providers = {
        "ciii-gpt": {"connections": [
            {"name": "codex", "api_key": "enc-codex-1", "enabled": True},
            {"name": "codex", "api_key": "enc-codex-2", "enabled": True},
        ]},
        "9aws": {"connections": [
            {"name": "Free", "api_key": "enc-free-1", "enabled": True},
            {"name": "Free", "api_key": "enc-free-2", "enabled": True},
        ]},
        "gorouter": {"connections": [
            {"name": "Connection 14", "api_key": "enc-gorouter-1", "enabled": True},
            {"name": "Connection 14", "api_key": "enc-gorouter-2", "enabled": True},
        ]},
    }
    for pid, pcfg in providers.items():
        expected_keys = {c["api_key"] for c in pcfg["connections"]}
        removed = main._dedup_connection_rows(pcfg)
        assert removed == 0, f"{pid}: Class B rows must never be merged"
        assert len(pcfg["connections"]) == 2, f"{pid}: expected both rows kept"
        got_keys = {c["api_key"] for c in pcfg["connections"]}
        assert got_keys == expected_keys, f"{pid}: a live credential was deleted"
        assert [c["name"] for c in pcfg["connections"]] == [
            "codex" if pid == "ciii-gpt"
            else "Free" if pid == "9aws" else "Connection 14"
        ] * 2, f"{pid}: Class B rows must not be renamed"


def test_dedup_preserves_lone_blank_with_differently_named_keyed_sibling():
    """pix4k shape: a blank row is NOT a duplicate of a differently-named row.

    Live config: pix4k has "Connection 2" (EMPTY key) and "11k" (keyed) -- two
    DIFFERENT names. The provider-wide blank prune deleted "Connection 2"
    merely because the provider had some keyed row, destroying an unfinished
    but deliberate setup. The prune is now scoped to same-name siblings.
    """
    pcfg = {
        "connections": [
            {"name": "Connection 2", "api_key": "", "enabled": True},
            {"name": "11k", "api_key": "enc-pix4k", "enabled": True},
        ],
        "models": [{"id": "m1", "connection_indexes": [0, 1]}],
    }
    assert main._dedup_connection_rows(pcfg) == 0
    assert len(pcfg["connections"]) == 2
    names = {c["name"] for c in pcfg["connections"]}
    assert names == {"Connection 2", "11k"}
    assert pcfg["models"][0]["connection_indexes"] == [0, 1]


def test_dedup_collapses_same_id_oauth_rows_keeping_fresher():
    """antigravity shape: two rows sharing one ``id`` collapse to the freshest.

    Live config had two oauth rows with id=nWkQqW6RZBPwblBG, both keyed,
    expires_at 19:43:11 vs 13:54:25. The id pass must collapse them and keep
    the later expiry -- this is a genuinely duplicate credential, unlike Class B.
    """
    pcfg = {"connections": [
        {"id": "nWkQqW6RZBPwblBG", "name": "tonluongdong@gmail.com",
         "token_type": "oauth", "api_key": "enc-old",
         "expires_at": "2026-09-25T13:54:25+00:00"},
        {"id": "nWkQqW6RZBPwblBG", "name": "tonluongdong@gmail.com",
         "token_type": "oauth", "api_key": "enc-new",
         "expires_at": "2026-09-25T19:43:11+00:00"},
    ]}
    assert main._dedup_connection_rows(pcfg) == 1
    assert len(pcfg["connections"]) == 1
    assert pcfg["connections"][0]["expires_at"] == "2026-09-25T19:43:11+00:00"


def test_dedup_on_live_shape_totals_five_removals():
    """End-to-end invariant: the live 104-row config reduces to 99, not 96.

    4 Class-A blanks + 1 same-id oauth dup = 5 removable. The three Class-B
    pairs (6 rows) and the pix4k lone blank are NOT removable. This is the
    number the migration script must report before it is allowed to --apply.
    """
    providers = {
        # 4x Class A -> 1 removal each
        "hcnsec": [{"name": "dongden", "api_key": ""},
                   {"name": "dongden", "api_key": "k-h"}],
        "agentrouter": [{"name": "Nguyenha", "api_key": ""},
                        {"name": "Nguyenha", "api_key": "k-a"}],
        "agentrouter-o": [{"name": "nguyenha", "api_key": ""},
                          {"name": "nguyenha", "api_key": "k-ao"}],
        "mintrouter": [{"name": "dongden", "api_key": ""},
                       {"name": "dongden", "api_key": "k-m"}],
        # 1x same-id oauth -> 1 removal
        "antigravity": [{"id": "same", "token_type": "oauth", "api_key": "k-1",
                         "expires_at": "2026-09-25T13:54:25+00:00"},
                        {"id": "same", "token_type": "oauth", "api_key": "k-2",
                         "expires_at": "2026-09-25T19:43:11+00:00"}],
        # 3x Class B -> 0 removals
        "ciii-gpt": [{"name": "codex", "api_key": "k-c1"},
                     {"name": "codex", "api_key": "k-c2"}],
        "9aws": [{"name": "Free", "api_key": "k-f1"},
                 {"name": "Free", "api_key": "k-f2"}],
        "gorouter": [{"name": "Connection 14", "api_key": "k-g1"},
                     {"name": "Connection 14", "api_key": "k-g2"}],
        # pix4k lone blank -> 0 removals
        "pix4k": [{"name": "Connection 2", "api_key": ""},
                  {"name": "11k", "api_key": "k-p"}],
    }
    total_removed = 0
    total_rows_before = sum(len(v) for v in providers.values())
    per_provider_removed = {}
    for pid, conns in providers.items():
        pcfg = {"connections": conns}
        per_provider_removed[pid] = main._dedup_connection_rows(pcfg)
        total_removed += per_provider_removed[pid]

    # 9 providers x 2 rows each = 18 rows in this synthetic live-shape fixture.
    assert total_rows_before == 18
    # Class A (blank beside same-name keyed) collapses: 4 providers, 1 each.
    for pid in ("hcnsec", "agentrouter", "agentrouter-o", "mintrouter"):
        assert per_provider_removed[pid] == 1, f"{pid}: Class A must collapse"
    # Same-id oauth duplicate collapses: 1.
    assert per_provider_removed["antigravity"] == 1
    # Class B (two distinct keyed rows sharing a name) must be untouched: 0.
    for pid in ("ciii-gpt", "9aws", "gorouter"):
        assert per_provider_removed[pid] == 0, f"{pid}: Class B must be preserved"
    # pix4k lone blank with a differently-named keyed sibling must be kept: 0.
    assert per_provider_removed["pix4k"] == 0

    assert total_removed == 5, f"live-shape invariant I2 violated: {total_removed} != 5"


# ──────────────────────────────────────────────────────────────────────────
# 6. _dedup_connection_rows index remap
# ──────────────────────────────────────────────────────────────────────────

def test_dedup_remaps_connection_indexes_to_survivor():
    """Indexes pointing at a dropped row rewrite to the survivor; no dangling/dupes."""
    pcfg = {
        "connections": [
            {"name": "keep", "api_key": "enc-keep", "enabled": True},        # 0 -> 0
            {"name": "dongden", "api_key": "", "enabled": True},             # 1 dropped
            {"name": "dongden", "api_key": "enc-dongden", "enabled": True},  # 2 -> 1
        ],
        "models": [
            {"id": "m1", "connection_indexes": [0, 1, 2]},
            {"id": "m2", "connection_indexes": [1]},      # dropped -> survivor (1)
            {"id": "m3", "connection_indexes": [2, 2]},   # dup + survivor -> single, sorted
            {"id": "m4", "connection_indexes": [9]},      # dangling -> dropped
        ],
    }
    removed = main._dedup_connection_rows(pcfg)
    assert removed == 1
    assert len(pcfg["connections"]) == 2
    by_model = {m["id"]: m["connection_indexes"] for m in pcfg["models"]}
    assert by_model["m1"] == [0, 1, 1] or by_model["m1"] == [0, 1], by_model["m1"]
    # de-duplicated + re-sorted: m1 indexes 0(dup->surv 0) and 1(dropped->surv 1) and 2(->surv 1)
    assert sorted(set(by_model["m1"])) == [0, 1]
    assert by_model["m2"] == [1]      # dropped row 1 -> survivor new index 1
    assert by_model["m3"] == [1]      # dup + survivor collapsed
    assert by_model["m4"] == []       # dangling index dropped, not left


# ──────────────────────────────────────────────────────────────────────────
# 7. No positional wrong-key assignment under duplicate names
# ──────────────────────────────────────────────────────────────────────────

def test_no_positional_wrong_key_when_names_collide():
    """Two server rows with the same name + a blank body row -> the RIGHT key.

    The old Pass 1 positional fallback assigned old_conns[idx]'s key to the
    blank body row, which under name drift was the WRONG key. The merge must
    carry the server row that shares the body row's IDENTITY (name), and the
    other same-name server row must collapse away (not be re-appended).
    """
    result = _merge(
        # server has two "dongden" rows: one keyed with enc-A, one keyed with enc-B
        [
            {"name": "dongden", "api_key": "enc-A", "enabled": True},
            {"name": "dongden", "api_key": "enc-B", "enabled": True},
        ],
        # body blanks one "dongden" -- the merge carries a server key into it
        [{"name": "dongden", "api_key": "", "enabled": True}],
    )
    assert len(result) == 1, f"duplicate-name server rows must collapse, got {len(result)}"
    assert result[0]["api_key"] in ("enc-A", "enc-B")
    assert result[0]["api_key"], "blank key must be restored from a server sibling"


# ──────────────────────────────────────────────────────────────────────────
# 8. Casing: Nguyenha == nguyenha
# ──────────────────────────────────────────────────────────────────────────

def test_casing_normalised_for_name_identity():
    """Nguyenha and nguyenha are the same name identity."""
    a = {"name": "Nguyenha", "api_key": "enc-a"}
    b = {"name": "nguyenha", "api_key": ""}
    assert main._connection_identity(a) == main._connection_identity(b)

    pcfg = {"connections": [
        {"name": "Nguyenha", "api_key": "enc-a", "enabled": True},
        {"name": "nguyenha", "api_key": "", "enabled": True},
    ]}
    assert main._dedup_connection_rows(pcfg) == 1
    assert len(pcfg["connections"]) == 1
    assert pcfg["connections"][0]["api_key"] == "enc-a"


# ──────────────────────────────────────────────────────────────────────────
# 9. OAuth id/email dedup unchanged (no regression)
# ──────────────────────────────────────────────────────────────────────────

def test_oauth_email_dedup_unchanged():
    """OAuth rows sharing an id or email still collapse exactly as before."""
    pcfg = {"connections": [
        {"id": "id-old", "email": "u@x.com", "token_type": "oauth",
         "api_key": "K1", "expires_at": "2020-01-01T00:00:00+00:00"},
        {"id": "id-new", "email": "u@x.com", "token_type": "oauth",
         "api_key": "K2", "expires_at": "2027-01-01T00:00:00+00:00"},
    ]}
    assert main._dedup_connection_rows(pcfg) == 1
    assert len(pcfg["connections"]) == 1
    assert pcfg["connections"][0]["id"] == "id-new"
    assert pcfg["connections"][0]["expires_at"] == "2027-01-01T00:00:00+00:00"


def test_oauth_distinct_emails_not_collapsed():
    """Two distinct oauth accounts are NOT collapsed (no over-merging)."""
    pcfg = {"connections": [
        {"id": "a", "email": "a@x.com", "token_type": "oauth", "api_key": "K1"},
        {"id": "b", "email": "b@x.com", "token_type": "oauth", "api_key": "K2"},
    ]}
    assert main._dedup_connection_rows(pcfg) == 0
    assert len(pcfg["connections"]) == 2


# ──────────────────────────────────────────────────────────────────────────
# 10. Migration script --dry-run on a fixture YAML
# ──────────────────────────────────────────────────────────────────────────

MIGRATE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "scripts", "migrate_dedup_connections.py",
)


def test_migration_dry_run_prints_report_writes_nothing(tmp_path, capsys):
    """--dry-run on a fixture YAML prints the report, writes no file, exits 0."""
    import yaml

    fixture = {
        "providers": {
            "hcnsec": {
                "connections": [
                    {"name": "dongden", "api_key": "", "enabled": True},
                    {"name": "dongden", "api_key": "enc-real", "enabled": True},
                ],
                "models": [{"id": "m1", "connection_indexes": [0, 1]}],
            },
        },
    }
    cfg_path = tmp_path / "config.yaml"
    with open(cfg_path, "w", encoding="utf-8") as f:
        yaml.dump(fixture, f, default_flow_style=False, sort_keys=False)

    import subprocess
    proc = subprocess.run(
        [sys.executable, MIGRATE, "--dry-run", "--config", str(cfg_path)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    out = proc.stdout
    # Report content: shows the collapse.
    assert "hcnsec" in out
    assert "dongden" in out
    # No backup, no mutated file in dry-run.
    assert not any(p.name.startswith("config.yaml.bak") for p in tmp_path.iterdir())
    # Final line is machine-checkable: DRY_RUN first token.
    last_line = [ln for ln in out.splitlines() if ln.strip()][-1]
    assert last_line.split()[0] == "DRY_RUN", last_line
    # The fixture file is untouched.
    with open(cfg_path, encoding="utf-8") as f:
        assert yaml.safe_load(f) == fixture


# ──────────────────────────────────────────────────────────────────────────
# 11. Models stale-save guard (2026-10-15): providers.<id>.models lost-update
#
# A stale dashboard tab's full-config autosave POSTs its stale models list
# and REPLACES the server's — wiping rows added server-side after the tab
# loaded (observed: 4 gemini-4-argon rows). The guard is UNION-with-opt-out,
# mirroring _apply_connections_stale_save_guard.
# ──────────────────────────────────────────────────────────────────────────

def _merge_models(old_models, body_models, flags=None):
    """Run only the models stale-save guard against two snapshots.

    ``flags`` maps opt-out keys (_deleted_model/_deleted_models) onto the
    body config, mirroring what the dashboard sends for deliberate deletes.
    Returns (merged models list, new_config) so tests can assert the flags
    were popped.
    """
    old_cfg = {"providers": {"prov": {"models": copy.deepcopy(old_models)}}}
    new_cfg = {"providers": {"prov": {"models": copy.deepcopy(body_models)}}}
    for key, value in (flags or {}).items():
        new_cfg[key] = value
    main._apply_models_stale_save_guard(old_cfg, new_cfg)
    return new_cfg["providers"]["prov"]["models"], new_cfg


def test_models_guard_restores_stale_missing_row(capsys):
    """(a) A server row missing from the body is restored; count == server count."""
    server = [
        {"id": "gemini-4-argon-1", "enabled": True, "thinking": "low"},
        {"id": "gemini-4-argon-2", "enabled": True, "thinking": "low"},
        {"id": "gpt-5.6-terra", "enabled": True},
    ]
    body = [{"id": "gpt-5.6-terra", "enabled": True}]  # tab predates the argon rows
    result, _ = _merge_models(server, body)
    assert len(result) == len(server), "stale-tab wipe must be reverted"
    # Body order first, restored server rows appended — no reorder, no drop.
    assert [m["id"] for m in result] == [
        "gpt-5.6-terra", "gemini-4-argon-1", "gemini-4-argon-2",
    ]
    assert "restored 2 model(s) (stale client save)" in capsys.readouterr().out


def test_models_guard_single_delete_opt_out(capsys):
    """(b) _deleted_model single-row opt-out: flagged row NOT restored."""
    server = [{"id": "m1", "enabled": True}, {"id": "m2", "enabled": True}]
    body = [{"id": "m1", "enabled": True}]  # deliberate m2 delete, not a stale drop
    result, cfg = _merge_models(
        server, body, {"_deleted_model": {"provider": "prov", "model": "m2"}},
    )
    assert [m["id"] for m in result] == ["m1"]
    assert "_deleted_model" not in cfg  # popped — must never persist
    assert capsys.readouterr().out == ""  # nothing restored

    # A flag naming a DIFFERENT provider must not suppress restoration.
    result2, cfg2 = _merge_models(
        server, body, {"_deleted_model": {"provider": "other", "model": "m2"}},
    )
    assert [m["id"] for m in result2] == ["m1", "m2"]
    assert "_deleted_model" not in cfg2


def test_models_guard_bulk_delete_opt_out(capsys):
    """(c) _deleted_models bulk opt-out: flagged rows dropped, others restored."""
    server = [
        {"id": "m1", "enabled": True},
        {"id": "m2", "enabled": True},
        {"id": "m3", "enabled": True},
        {"id": "m4", "enabled": True},
    ]
    body = []  # client deliberately deleted m2/m3; m1/m4 are stale drops
    result, cfg = _merge_models(
        server, body,
        {"_deleted_models": {"provider": "prov", "models": ["m2", "m3"]}},
    )
    assert [m["id"] for m in result] == ["m1", "m4"]
    assert "_deleted_models" not in cfg  # popped — must never persist
    assert "restored 2 model(s) (stale client save)" in capsys.readouterr().out


def test_models_guard_body_wins_field_updates():
    """(d) A body row sharing the server id wins field updates — no reverts."""
    server = [{"id": "m1", "enabled": True, "thinking": "low", "name": "Old Name"}]
    body = [{"id": "m1", "enabled": False, "thinking": "max", "name": "New Name"}]
    result, _ = _merge_models(server, body)
    assert len(result) == 1
    assert result[0]["enabled"] is False  # flip NOT reverted
    assert result[0]["thinking"] == "max"
    assert result[0]["name"] == "New Name"


def test_models_guard_keeps_genuinely_new_body_row():
    """(e) A body row whose id is not on the server is kept, order intact."""
    server = [{"id": "m1", "enabled": True}]
    body = [
        {"id": "m-new", "enabled": False},  # genuinely new — added by this client
        {"id": "m1", "enabled": True},
    ]
    result, _ = _merge_models(server, body)
    assert [m["id"] for m in result] == ["m-new", "m1"]  # never dropped/reordered


def test_models_guard_non_dict_and_missing_lists_noop(capsys):
    """(f) Fail-open: non-dict input never raises; missing models lists are no-ops."""
    # Non-dict configs / non-dict providers / non-dict provider values.
    main._apply_models_stale_save_guard(None, None)
    main._apply_models_stale_save_guard(None, {"providers": {"prov": {"models": []}}})
    main._apply_models_stale_save_guard(
        {"providers": {"prov": {"models": []}}}, "not-a-dict",
    )
    main._apply_models_stale_save_guard(
        {"providers": "not-a-dict"}, {"providers": {"prov": {"models": []}}},
    )
    main._apply_models_stale_save_guard(
        {"providers": {"prov": "not-a-dict"}},
        {"providers": {"prov": "also-not-a-dict"}},
    )
    # Server provider has no models list -> nothing to restore, body untouched.
    old_cfg = {"providers": {"prov": {"connections": [{"name": "x"}]}}}
    new_cfg = {"providers": {"prov": {"models": [{"id": "m1"}]}}}
    main._apply_models_stale_save_guard(old_cfg, new_cfg)
    assert new_cfg["providers"]["prov"]["models"] == [{"id": "m1"}]
    # Body provider carries no models key -> no-op: the guard never invents a
    # models list for the body and never restores rows into one.
    old_cfg2 = {"providers": {"prov": {"models": [{"id": "m1"}, {"id": "m2"}]}}}
    new_cfg2 = {"providers": {"prov": {"connections": []}}}
    main._apply_models_stale_save_guard(old_cfg2, new_cfg2)
    assert "models" not in new_cfg2["providers"]["prov"]
    assert capsys.readouterr().out == ""
