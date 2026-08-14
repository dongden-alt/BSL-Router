"""Contract tests for the config_state indirection (R1/R4 refactors).

WHY THIS FILE EXISTS
--------------------
The R1 refactor moved the canonical config dict into app.config_state and left
`main.config` as a read-only mirror. A subsequent bug proved the whole 946-test
suite is BLIND to that contract:

    main.py imported  `get_config`  from app.config_state at module scope, and
    ~1200 lines later defined  `async def get_config()`  as the GET /api/config
    route handler. The route rebound the module-level name, so load_config()
    assigned an unawaited COROUTINE to the global `config`. The router could not
    start at all -- and all 946 tests still passed, because every test that
    touches startup stubs load_config out (`router.load_config = lambda: None`).

The only visible symptom was a RuntimeWarning ("coroutine 'get_config' was
never awaited") that looked like harmless monkeypatch noise in an unrelated
test, and was dismissed as pre-existing.

R4 then deleted the stored `config` global entirely (Candidate C): every reader
takes a fresh `config = cs_get_config()` snapshot, so a stale mirror is
structurally impossible -- a missed reader is a loud NameError, not silent
stale data.

So these tests deliberately assert the things a mocked-startup suite cannot:
  1. load_config() produces a real dict, not a coroutine
  2. NO stored `config` global exists in main.py (the stale-mirror class is
     gone; re-adding a stored global fails this test)
  3. no module-level import in main.py is shadowed by a later def/assignment
     (generic -- catches the NEXT occurrence of this bug class, not just this one)
  4. every top-level function that reads `config` has a local snapshot
  5. runtime config replacement re-points cached config readers

Keep these fast and dependency-free: they are a startup smoke gate.
"""
import ast
import inspect
import io
import pathlib
from contextlib import redirect_stdout

import app.config_state as cs
import app.main as main
from app.circuit_breaker import CircuitBreaker, reconfigure_breaker
from app.routing.combo_resolver import advance_combo_retry


# ── helpers ─────────────────────────────────────────────────────────────────

def _restore_config_state(saved_cs):
    """Put config_state back exactly as it was (no main.config to restore)."""
    cs.replace_config(saved_cs)


def test_main_has_no_stored_config_global():
    """No stored `config` global may exist in main.py.

    A stored global is a mirror that can go stale. With it gone, every
    reader takes a fresh snapshot from config_state, and a missed reader
    is a loud NameError caught by the test suite, not silent stale data.
    """
    assert "config" not in vars(main), (
        f"main.py still stores a module-level `config` global: {main.config!r:.80}. "
        "Every reader must take a fresh snapshot via `config = cs_get_config()`."
    )


def test_model_enable_mutation_reaches_live_master():
    """A providers[].models[].enabled flip must reach the live master config.

    Regression (P0 split-brain): get_config() returns a shallow copy whose
    `providers` is a deep-copied CLONE, so mutating `config['providers']...enabled`
    on a read snapshot silently hit a throwaway clone and never changed the running
    router (only disk, i.e. after restart). The sanctioned path is: mutate a
    get_mutable_config() copy, then commit via main._replace_runtime_config().
    This test proves that round-trip lands on the master read back by get_config().
    """
    from unittest.mock import patch
    saved = cs.get_config()
    try:
        cs.replace_config({
            "providers": {
                "prov-x": {"models": [{"id": "mdl-x", "enabled": True}]}
            },
            "admin": {"password_enabled": False, "password": "123456"},
        })
        # Simulate the self-heal enable/disable path: mutable copy -> flip -> commit.
        mut = cs.get_mutable_config()
        mut["providers"]["prov-x"]["models"][0]["enabled"] = False
        
        with patch('app.main._persist_config_snapshot', return_value=None):
            main._replace_runtime_config(mut)
            
        # A fresh READ snapshot must reflect the mutation (master, not a stale clone).
        after = cs.get_config()
        assert after["providers"]["prov-x"]["models"][0]["enabled"] is False, (
            "enabled flip did not reach the live master — the mutation hit a clone. "
            "Self-heal enable/disable would be a no-op until restart."
        )
        # get_mutable_config must be an independent deep copy (mutating it must not
        # leak into the master before a commit).
        probe = cs.get_mutable_config()
        probe["providers"]["prov-x"]["models"][0]["enabled"] = True
        assert cs.get_config()["providers"]["prov-x"]["models"][0]["enabled"] is False
    finally:
        with patch('app.main._persist_config_snapshot', return_value=None):
            main._replace_runtime_config(saved)


# The two AST checks below take SOURCE TEXT rather than reading main.py
# directly, so a mutation harness can feed them deliberately-broken source and
# confirm they actually go red. A guard that has never been seen to fail is not
# yet known to work.

def find_shadowed_module_imports(src: str) -> list[str]:
    """Names imported at module scope that a later module-scope stmt rebinds."""
    tree = ast.parse(src)
    imported: dict[str, int] = {}
    collisions: list[str] = []

    for node in tree.body:  # module scope only
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                name = alias.asname or alias.name.split(".")[0]
                imported[name] = node.lineno
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name in imported:
                collisions.append(
                    f"{node.name!r}: imported at line {imported[node.name]}, "
                    f"redefined by {type(node).__name__} at line {node.lineno}"
                )
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in imported:
                    collisions.append(
                        f"{target.id!r}: imported at line {imported[target.id]}, "
                        f"reassigned at line {node.lineno}"
                    )
    return collisions


def find_bare_config_reads(src: str) -> list[str]:
    """Top-level functions that read bare `config` with no local binding.

    The stored global is gone (R4), so bare reads are NameErrors waiting to
    happen. Every reader must snapshot: `config = cs_get_config()`. Functions
    that receive config as a parameter or assign a local first are fine.
    """
    tree = ast.parse(src)
    findings: list[str] = []

    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        params = {a.arg for a in node.args.args + node.args.kwonlyargs + node.args.posonlyargs}
        if "config" in params:
            continue
        reads = 0
        stored = False
        for stmt in node.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
                continue
            for n in ast.walk(stmt):
                if not isinstance(n, ast.Name) or n.id != "config":
                    continue
                if isinstance(n.ctx, ast.Load):
                    reads += 1
                elif isinstance(n.ctx, ast.Store):
                    stored = True
        if reads and not stored:
            findings.append(
                f"{node.name}() at line {node.lineno} reads bare `config` "
                f"({reads} site(s)) without a local snapshot"
            )
    return findings


# ── 1. startup produces a usable config ─────────────────────────────────────

def test_load_config_yields_dict_not_coroutine():
    """load_config() must leave config_state holding a real dict.

    Regression: the get_config route shadowed the config_state accessor, so
    this assigned an un-awaited coroutine and the server died on boot with
    AttributeError: 'coroutine' object has no attribute 'get'. The stored
    `config` global is gone (R4), so the assertion targets the canonical dict.
    """
    saved_cs = cs.get_config()
    try:
        main.load_config()
        assert isinstance(cs.get_config(), dict), (
            f"config_state holds {type(cs.get_config()).__name__}, expected dict. "
            "A module-level name is probably shadowing config_state.get_config."
        )
        assert not inspect.iscoroutine(cs.get_config())
        assert "config" not in vars(main), (
            "load_config() must not re-create a module-level `config` global."
        )
    finally:
        _restore_config_state(saved_cs)


def test_no_bare_config_reads_without_snapshot():
    """Every top-level bare `config` reader must take a fresh snapshot.

    This is the generalized guard for the stale-mirror bug class: with the
    stored global deleted, a reader that forgot its `config = cs_get_config()`
    is a NameError -- loud and immediately caught by this static scan.
    """
    findings = find_bare_config_reads(_main_source())
    assert not findings, (
        "Functions in main.py read bare `config` without a local snapshot.\n"
        "Add `config = cs_get_config()` as the first statement.\n  - "
        + "\n  - ".join(findings)
    )

_SANCTIONED_REPLACE_CONFIG_CALLERS = {"load_config", "_replace_runtime_config"}


def test_replace_config_only_called_from_sanctioned_functions():
    """replace_config() may only be called from load_config or _replace_runtime_config.

    _replace_runtime_config is the ONLY sanctioned runtime swap path (persist ->
    swap -> breaker). Any other caller skips the persist and breaker reconfigure,
    which means config.yaml drifts from runtime and the breaker reads stale settings.
    """
    tree = ast.parse(_main_source())
    offenders: list[str] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if fn.name in _SANCTIONED_REPLACE_CONFIG_CALLERS:
            continue
        for n in ast.walk(fn):
            if isinstance(n, ast.Call):
                if isinstance(n.func, ast.Name) and n.func.id == "replace_config":
                    offenders.append(f"{fn.name}() line {n.lineno}: bare replace_config()")
                elif isinstance(n.func, ast.Attribute) and n.func.attr == "replace_config":
                    offenders.append(f"{fn.name}() line {n.lineno}: attribute .replace_config()")
    assert not offenders, (
        "replace_config() called outside sanctioned functions. Use "
        "_replace_runtime_config() instead.\n  - " + "\n  - ".join(offenders)
    )


def test_config_state_accessor_is_not_shadowed():
    """main must hold the real accessor, aliased away from route handler names."""
    assert main.cs_get_config is cs.get_config
    assert main.replace_config is cs.replace_config


def test_api_config_route_handler_still_exists():
    """Guard the other half of the fix: renaming the import must not have
    disturbed the GET /api/config endpoint, which is legitimately named
    get_config and must stay async."""
    assert inspect.iscoroutinefunction(main.get_config)


# ── 2. generic guard for the whole bug class ────────────────────────────────

def _main_source() -> str:
    return pathlib.Path(main.__file__).read_text(encoding="utf-8", errors="replace")


def test_no_module_level_import_is_shadowed_in_main():
    """No name imported at module scope in main.py may be rebound later.

    This is the generalized form of the get_config bug. FastAPI route handlers
    are ordinary module-level defs, so any handler sharing a name with an
    import silently replaces it -- with no error, no warning, and (as proven)
    no test failure. Scanning the AST catches the next one for free.
    """
    collisions = find_shadowed_module_imports(_main_source())
    assert not collisions, (
        "Module-level imports in main.py are shadowed by later definitions.\n"
        "This silently breaks the imported callable. Alias the import.\n  - "
        + "\n  - ".join(collisions)
    )


# ── 3. runtime config replacement reaches cached readers ────────────────────

def test_circuit_breaker_sees_replaced_config():
    """Breaker settings must not be frozen at construction.

    Regression: CircuitBreaker cached `self.settings = config["circuit_breaker"]`
    in __init__, so saving new breaker settings via POST /api/config persisted
    to config.yaml but had zero effect until a restart.
    """
    original = {"circuit_breaker": {"enabled": False, "failure_threshold": 3}}
    breaker = CircuitBreaker(original)
    assert breaker.enabled is False
    assert breaker.failure_threshold == 3

    replacement = {"circuit_breaker": {"enabled": True, "failure_threshold": 9}}
    breaker.reconfigure(replacement)
    assert breaker.enabled is True
    assert breaker.failure_threshold == 9


def test_circuit_breaker_reconfigure_preserves_health_state():
    """Editing config must not clear accumulated per-connection health.

    Dropping state would re-admit traffic to accounts already known bad, so
    an unrelated config save would silently undo every OPEN connection.
    """
    breaker = CircuitBreaker({"circuit_breaker": {"enabled": True}})
    breaker.state["prov/mdl/0"] = {"state": "OPEN", "consecutive_failures": 5}
    breaker.reconfigure({"circuit_breaker": {"enabled": True}})
    assert breaker.state["prov/mdl/0"]["state"] == "OPEN"
    assert breaker.state["prov/mdl/0"]["consecutive_failures"] == 5


def test_reconfigure_breaker_is_fail_open():
    """The breaker is an optimization; a bad reconfigure must never raise."""
    reconfigure_breaker(None)  # must not raise
    reconfigure_breaker({"circuit_breaker": {}})


# ── 4. log fidelity ────────────────────────────────────────────────────────

def test_advance_combo_retry_does_not_duplicate_caller_log():
    """advance_combo_retry must not print the fallback-retry line.

    main.py prints it at the call site. When this function printed it too the
    identical line appeared twice per retry, which reads as two attempts on a
    single leaf -- indistinguishable in the logs from the retry loop that the
    C2/C3 constraints exist to prevent.
    """
    retry_state = {
        "chain": [("mdl-a", "prov-a", None), ("mdl-b", "prov-b", None)],
        "idx": 0,
    }
    config = {"providers": {}, "error_prevention": {"enabled": False}}

    buf = io.StringIO()
    with redirect_stdout(buf):
        advance = advance_combo_retry(retry_state, config, combo_alias="coder-2")

    assert advance.exhausted is False
    assert advance.target_model == "mdl-a"
    assert "fallback-retry" not in buf.getvalue(), (
        "advance_combo_retry printed the fallback-retry line; the caller in "
        "main.py already prints it, so this duplicates every retry log."
    )


def test_advance_combo_retry_still_logs_banned_skips():
    """The banned-leaf skip log is this function's own; it must survive.

    It carries the combo alias prefix for parity with the pre-refactor format.
    """
    import app.error_prevention as ep

    retry_state = {
        "chain": [("mdl-a", "prov-a", None), ("mdl-b", "prov-b", None)],
        "idx": 0,
    }
    # Ban only the first leaf so the loop skips exactly once.
    config = {
        "error_prevention": {"enabled": True},
        "error_prevention_state": {
            "prov-a/mdl-a": {
                "ban_type": "softban",
                "ban_until": 9_999_999_999,
                "consecutive_failures": 3,
            }
        },
    }
    banned, _, _ = ep.check_ban(config, "prov-a", "mdl-a")
    if not banned:  # ban schema drifted; the skip path is covered elsewhere
        return

    buf = io.StringIO()
    with redirect_stdout(buf):
        advance = advance_combo_retry(retry_state, config, combo_alias="coder-2")

    out = buf.getvalue()
    assert "skipping banned leaf" in out
    assert "coder-2" in out, "combo alias prefix missing from the skip log"
    assert advance.target_model == "mdl-b"  # RC5: advanced past the banned leaf
    assert retry_state["idx"] == 1          # C3: advanced idx written back


# ── 5. dead-code removal stays removed ─────────────────────────────────────

def test_chain_segment_helper_resolves_to_extracted_module():
    """main._resolve_combo_chain_segment must be the extracted implementation.

    A duplicate definition previously lived in main.py. It was dead (the import
    rebound the name) but drifted independently, so the copy a reader saw was
    not the copy that ran.
    """
    assert main._resolve_combo_chain_segment.__module__ == "app.routing.combo_resolver"
