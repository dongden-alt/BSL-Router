# BSL Router — Project Rules

> **Rewritten 2026-09-20** (user-approved direct edit). Main change: §4 (push verification) now
> matches the verified behavior of `scripts/pre-push-check.ps1` — the Gate 4 gitignore invariant
> is exactly **5 paths**; the previous "7-path" claim is retired (see the WARNING under Gate 4).
> §1–§3 are preserved battle-tested invariants. This file applies to every agent working in this
> repo; global user rules stack on top of it.

## 1. 🔴 HARD RULE — BSL Router restarts require an explicit user command (2026-08-28)

**NEVER restart, stop, kill, or respawn the BSL Router process (`:6969`) without an explicit
command from the user in the current conversation.**

This applies to ALL of the following, none of which may be done autonomously:

- Running `shutdown_bsl.ps1`, `START-9router.bat`, or any start/stop script
- Killing the `python.exe -m app.dualstack_serve --port 6969` process (or its supervisor lineage) via CIM / `taskkill` / `Win32_Process`
- Spawning a new supervisor instance that would take over the port
- Touching the `.brain/logs/router-6969.lock` lockfile to force a lineage change

**Why:** BSL Router is the user's live production AI backbone (Claude Code + all agents route
through it). An unsupervised restart drops every in-flight request, can trigger the WinError-64
accept-death recovery chain, and the old-process-holding-port failure mode can leave the router
serving stale config from a process that *looks* healthy.

**What to do instead when a change requires a reload:**

1. Make the change (e.g. add a provider to `config.yaml`, edit `app/main.py`)
2. Tell the user: "config changed — BSL needs a restart to pick it up, say the word"
3. WAIT. The user decides when it is safe to bounce the router.

**Code on disk ≠ live code.** A running PID keeps serving the code it was started with; edits to
`app/main.py` or config are NOT active in the running process until the user commands a restart.
Never report a router fix as "deployed/live" — it is "ready, awaiting user-commanded restart"
until the bounce actually happens.

The only exception: the user explicitly says "restart it" / "bounce it" / equivalent in the
current session.

## 2. 🔴 HARD RULE — every `.brain/logs` writer stays capped AND off the event loop (2026-08-31)

This is the bug that made Antigravity IDE shut down mid-session: `antigravity_inbound.jsonl`
reached **29.8 GB** because it was appended *synchronously on the FastAPI event loop with no
size cap*. Each append then cost seconds of disk time, stalling every in-flight stream for
minutes → upstream reads timed out → zero-output force-stop → the IDE's language servers died
with the connection. `mitm_egress_frames.jsonl` hit 4.3 GB the same way.

Invariants — do not regress any of these:

- Capture writes go through the **async queue writer** (`asyncio.to_thread`) and the queue is
  **drop-on-full**. A slow disk must NEVER back-pressure inference: drop a record, never block.
- All three loggers rotate at **50MB** — `antigravity_inbound.jsonl`, `mitm_egress_frames.jsonl`,
  `mitm_live_debug.log` — plus boot-time self-heal truncation for files already over cap.
- Inbound capture is **metadata-only by default**. Full payloads only under `BSL_CAPTURE_INBOUND=1`.
- Currently uncapped but default-OFF: `upstream_failures.jsonl` (gate `tools.conn_trace`) and
  `outbound_upstream.jsonl` (gate `tools.outbound_forensics`). Enabling either without adding a
  cap re-arms this bug.
- MITM does **not** need to be running for this protection: the cap lives on the router's `:6969`
  request path. The two MITM-side writers only log when traffic flows through `:443`.
- **Adding a new log writer? Cap it and keep it off the hot path in the same commit.**

Diagnosing a future IDE shutdown: check `.brain/logs` **sizes first**. KB/MB-scale means this fix
held — look elsewhere. Then grep `child_out.log` for clustered `content missing` /
`Client disconnected`. Full RCA: KI `ki-bslrouter-log-stall-ide-shutdown`, and
`.brain/CHANGELOG.md` entries 2026-08-30 / 2026-08-31.

## 3. 🟠 HARD RULE — launcher health gate carries NO credential (2026-08-31)

`Start-App`'s idempotence probe in `scripts/bslrouter.ps1` hits **unauthenticated `/health`** on
`127.0.0.1` AND `[::1]`, each inside its **own** `try/catch`. Three traps, all previously
shipped bugs:

- **No API key in `scripts/bslrouter.ps1`, ever.** The old probe carried a hardcoded key that was
  a *typo* of the real one; it "worked" only because `/v1/models` does not enforce auth. The day
  auth lands on that route, the probe 401s → gate concludes "unhealthy" → **kills a healthy
  router and respawns** — precisely the restart loop the gate exists to prevent.
- **One `try` per stack, never a shared one.** `Invoke-WebRequest` raises *terminating* errors
  on a refused connection, so a shared `try{}` lets a dead IPv4 stack abort the block before IPv6
  is ever probed. `-ErrorAction SilentlyContinue` does NOT downgrade terminating errors.
- **Tests asserting "pattern X is absent from the launcher" MUST strip comments first.** Use
  `_powershell_code_only()` in `app/tests/test_mitm_lifecycle.py`. Otherwise the comment that
  documents a removal satisfies the assertion forbidding it — this bit twice in one session.

`/v1/models` is still unauthenticated by design; adding auth is a breaking change for every
existing client and needs an explicit user decision. The launcher no longer depends on that
route either way.

## 4. 🔴 HARD RULE — push verification: 4 gates before every `git push` (2026-09-10; rewritten 2026-09-20)

**Every `git push` to origin MUST pass all 4 gates below before the push command runs.**
No exceptions for "small" pushes, doc-only pushes, or "just one file" pushes. Born from a
2-day forensic audit that found a partial API key fragment leaked in a *comment inside the very
fix commit* that removed the key from code. If the fix commit leaks, every commit can leak.

**Canonical gate: run `scripts/pre-push-check.ps1`** (read-only; exit 0 = pass, 1 = fail) —
OR execute the 4 gates manually. Both are valid.

### Push protocol — direct to `main`

This repo pushes **direct to `main`** (no feature-branch dance — the old 7-step branch+merge
protocol was wrong for this repo and is gone):

1. Run the gates — all 4 must pass
2. `git add` the EXACT intended files — never `git add .` / `git add -A` (see *Visible-untracked* below)
3. Commit
4. `git push origin main`
5. Verify the remote: `git ls-remote origin main` — remote HEAD must equal local HEAD

### Release / tag parity — a version bump is NOT done until the tag + release are live

**Trigger:** any push that bumps `VERSION` (or otherwise ships a new release) MUST leave the
remote tag and GitHub Release in the same state as `main`. Born 2026-09-27: two fix pushes
(v1.0.9, v1.0.10) shipped code while the public Releases page stayed on v1.0.8 — the tags were
never pushed, so the tag→release automation never ran and users saw a stale version.

After the `git push origin main` step, when `VERSION` changed:

1. **Create + push the tag** (annotated, at the release commit):
   `git tag -a v$(cat VERSION) <release-commit-sha> -m "v$(cat VERSION) - <one-line>"`
   `git push origin v$(cat VERSION)`
   (Tags are pushed over SSH — `origin` is `git@bsl-router:...`; SSH needs no API token.)
2. **Confirm the release exists.** This repo auto-creates a GitHub Release when a `v*` tag is
   pushed. Verify with the **unauthenticated** endpoint (no token needed for read):
   `https://api.github.com/repos/dongden-alt/BSL-Router/releases?per_page=3` — the newest
   `tag_name` must equal `v$(cat VERSION)` with `draft=false`. If the release does NOT appear
   within ~60s of the tag push, the automation did not fire → investigate before declaring done.
3. **Release notes body** is auto-generated by the automation from the tag message / changelog.
   If a manual REST `POST /releases` is ever needed, it requires a **valid, unexpired** PAT with
   `repo` scope — the `GITHUB_TOKEN` env var has been observed expired (401); do not assume it works,
   verify with a read call first.
4. **Never report a version as "released"** until the tag exists on the remote AND the release
   object is visible. "Pushed to main" ≠ "released".

**Anti-cascade:** if a prior push shipped a version without its tag/release, the NEXT push must
first backfill the missing tag(s) + release(s) for every gap version, then proceed.

### Gate 1 — tracked-tree secret sweep

```powershell
git grep -I -n -e 'sk-bsl-LzAC' -e 'LzACIxbnpt' -e 'Q4jHjmqdyCy5' -e 'Q4jHmqdyCy5'
```

Exit non-zero (any hit) → **STOP.** Redact, commit, re-run. The fragments are the last 12 chars
of the real admin key (plus its historical typo variant). Any hit = leak, including hits inside
comments, docstrings, test fixtures, or error messages.

### Gate 2 — machine-specific path sweep

```powershell
git grep -I -n -e 'D:/Tools' -e 'D:\\Tools' -e 'd:/Projects/BSL' -e 'D:\\Projects\\BSL' -e 'C:/Users/Admin' -e 'C:\\Users\\Admin'
```

Exit non-zero → **STOP.** Replace with portable placeholders (`YOUR_TOOLS_PATH`,
`YOUR_PROJECT_PATH`). Machine paths in `.mcp.json`, scripts, or configs are PII-adjacent and
break portability. The gate script self-excludes the files that must legitimately contain these
patterns in order to sweep for them — that exemption is for gate tooling only, never product code.

### Gate 3 — unpushed-diff secret scan

Scan the **full unpushed diff** — staged AND committed-but-unpushed:

```powershell
git diff --cached --stat                    # staged changes
git diff origin/main...HEAD --stat          # committed but not yet pushed
git diff origin/main...HEAD | Select-String -Pattern 'sk-bsl-[A-Za-z0-9]{10,}' -Context 2
```

Manually scan for any string matching `sk-bsl-` followed by 10+ alphanumeric chars (the real
key pattern). Dummy placeholders (`sk-bsl-YOUR_API_KEY_HERE`) are **expected and safe** —
underscores fail the `[A-Za-z0-9]{10,}` match.

### Gate 4 — gitignore invariant: exactly 5 paths

```powershell
git check-ignore config.yaml .bsl_key .bsl_key.dpapi .venv .mcp.json
```

ALL 5 must return as ignored. If any returns nothing → **STOP.** The `.gitignore` was tampered
with or a rebase dropped a line. Fix before pushing.

**Negation invariant:** `config.example.yaml` and `.mcp.json.example` are the tracked templates —
they MUST stay tracked (never add them to `.gitignore`, never `git rm --cached` them).

> [!WARNING]
> **Retired 2026-09-20 — the old "7-path" claim was wrong for this repo.** The previous version
> of this rule demanded `.brain/` and `.agents/` also return as ignored. Verified live
> 2026-09-20: `.brain/` remains **visible-untracked** (`git status --short` shows `?? .brain/`
> despite a `.brain/` rule in `.gitignore` — negation patterns elsewhere in the file leave its
> content un-ignored), and `.agents/AGENTS.md` is a **tracked** file (tracked files are not
> affected by ignore rules). The gate therefore asserts exactly the 5 real secret/venv paths
> above. Do not "fix" Gate 4 by re-adding `.brain/` or `.agents/` to the check.

### Visible-untracked means unprotected

`git status` currently shows `?? .brain/` and `?? .venv311/`. Neither is covered by Gate 4 (by
design) nor swept by Gate 1 (untracked). One careless `git add .` stages them — `.brain/` holds
runtime logs and secrets, `.venv311/` is a full Python environment. This is why the push
protocol mandates explicit-path `git add`.

### Known-safe patterns (do NOT flag these as leaks)

| Pattern | Why it is safe |
|---|---|
| `localhost:6969` / `127.0.0.1:6969` / `[::1]:6969` | canonical dev port, expected in docs/tests |
| `sk-bsl-YOUR_API_KEY_HERE` | dummy placeholder by design |
| `REDACTED-BSL-ADMIN-KEY` | redaction placeholder, not the real key |
| `admin@chatbot.local` in commit metadata | local hostname, cosmetic only |

## 5. 🟠 Push + CI version parity (2026-09-13 lesson)

Before pushing anything that can change CI results:

- **Replicate EVERY CI matrix leg locally** — one venv per CI interpreter (e.g. `.venv311` for
  the Py3.11 leg). A Py3.10-only dev venv cannot catch Py3.11-only failures: the deadline-pump
  case shipped a full week of red CI while every local run was green, because Py3.11 aliases
  `asyncio.TimeoutError` to the builtin `TimeoutError`, silently collapsing the pump's
  except-order timeout discrimination.
- **Reproduce the red on the failing leg FIRST** before writing the fix.
- **Watch CI to its final verdict** — never assume green, never move a git tag / publish a
  release on red.
- Commit message must map to the CI legs actually tested.

## 6. Repo quick facts

| Item | Value |
|---|---|
| Service | BSL Router — dual-stack FastAPI on `:6969` |
| Entry point | `python -m app.dualstack_serve --port 6969` |
| Launcher | `scripts/bslrouter.ps1` (`Start-App`); `START-9router.bat`; `shutdown_bsl.ps1` |
| Live config | `config.yaml` (gitignored; `config.example.yaml` is the tracked template) |
| Admin key | `.bsl_key` / `.bsl_key.dpapi` (gitignored) |
| venvs | `.venv` (dev, Py3.10 — gitignored), `.venv311` (CI-parity Py3.11 leg) and `.venv312` (CI-parity Py3.12 leg) — the latter two are visible-untracked, never bulk-add |
| Tests | `app/tests/` — suite must stay green on BOTH CI legs (`.venv311` + `.venv312`); launcher-pattern assertions in `app/tests/test_mitm_lifecycle.py`; `config.yaml` is isolated out of the suite via `BSL_CONFIG_PATH` in `conftest.py` (see §7) |
| Gate script | `scripts/pre-push-check.ps1` (read-only; exit 0 = pass / 1 = fail) |
| Reference KIs | `ki-bslrouter-log-stall-ide-shutdown`, `ki-bslrouter-github-push-guideline`, `ki-ci-version-parity-push-discipline` |

## 7. 🔴 HARD RULE — the config write target is repo-root anchored; never a bare `"config.yaml"` (2026-09-29)

**THE BUG THIS PREVENTS IS THE WORST KIND: it made the verification suite destroy the thing it was verifying.**

`_persist_config_snapshot` computed its write target as the bare, CWD-relative string
`"config.yaml"`, with no repo-root anchoring and no env override anywhere in `app/`. Consequence:
**every `pytest` run launched from the repo root wrote LIVE PRODUCTION `config.yaml`** — and
rotated away the 5-slot `config.yaml.bak-*` recovery window while doing it. Proven twice with
isolated temp-CWD experiments: `_persist_config_snapshot(cfg)` created a 180-byte `config.yaml`
in the temp dir; and with a copy of live config placed in the temp CWD, `load_config()` printed
`[Dedup] antigravity: collapsed 1 duplicate connection row(s)` and rewrote that isolated file
536070 → 535034 while creating `.bak` files, leaving the repo `config.yaml` untouched.

10 test files boot the lifespan across 36+ `TestClient(main.app)` instantiations
(`test_fel_wiring.py` 13, `test_responses_thread_store.py` 9, `test_kiro_refresh_on_import.py` 3,
five files with 2 each, `conftest.py` 1, `test_antigravity_ccpa_gateway.py` 1). The write chain is
`load_config` → dedup sets `_dirty` → `if _dirty: _replace_runtime_config(config)` → persist → swap
→ reconfigure breaker. **This is why a connection-duplication bug survived two separate "fixed,
verified, done" claims: each verification run re-armed the bug.**

Invariants — do not regress any of these:

- **Never introduce a bare `"config.yaml"` string anywhere under `app/`.** Resolve through
  `app.main._CONFIG_YAML_PATH`, or `app.error_prevention._config_yaml_path()`, or
  `app.config_state.init_config(path)`.
- `_REPO_ROOT` / `_CONFIG_YAML_PATH` (`app/main.py:21-25`) are the single source of truth:
  env `BSL_CONFIG_PATH` wins, else repo-root `config.yaml`. Wired into `load_config()`,
  `_persist_config_snapshot`, and the pricing detector.
- Atomic writes must keep the temp file on the target's filesystem:
  `mkstemp(dir=dirname(_cfg_path))` + `os.replace(tmp_path, _cfg_path)`.
- **`app/tests/conftest.py` sets `BSL_CONFIG_PATH` BEFORE `import app.main`**, seeds a session
  temp copy, and hard-asserts `main._CONFIG_YAML_PATH == _TEST_CONFIG_PATH` so the suite REFUSES
  TO RUN if isolation failed. Never weaken, bypass, or monkeypatch that anchor. A test needing a
  config path sets `BSL_CONFIG_PATH` or patches `app.main._CONFIG_YAML_PATH` to `tmp_path`.

> [!WARNING]
> **Root-anchor footgun.** `app/main.py:14` binds `_project_root` to the **`app/` directory**, while
> `app/main.py:~4860` defines `def _project_root()` returning the **repo root** — the def REBINDS
> the name at import time. Never derive a repo-root path from `_project_root`; the value you get
> depends on whether the def has executed yet. Use `_REPO_ROOT`.

> [!CAUTION]
> **Ciphertext hashing is NOT a change oracle for `config.yaml`.** Fernet embeds a timestamp and a
> random IV, so re-encrypting *identical plaintext* yields *different ciphertext*. During this work
> the file's sha256 prefix rotated `FE80C6B285ECF358` → `B31188FC2AC8D569` → `2765BC0221544CF5`
> while its size stayed CONSTANT at 536070 bytes and its decrypted length stayed 515592 — the live
> router was simply re-saving unchanged in-memory config. An earlier "97 credentials LOST / 99 NEW"
> alarm was a FALSE ALARM caused by exactly this. Judge config drift by structural shape
> (`.brain/scratch/probe_shape.py`: providers / rows / keyed / blank / dupname_groups), or decrypt
> via `app.crypto.decrypt_value`.

Sites that were ALREADY correct and were left untouched: `app/mitm.py:44-45`,
`app/dualstack_serve.py:712-715` (read-only watchdog), `scripts/update_bsl_router.py:194`,
`scripts/bslrouter.ps1:101`, `app/tests/test_family_characterization.py:27-30`.

## 8. 🔴 HARD RULE — `_dedup_connection_rows` is a TWO-PHASE union; Phase 2's name union stays conditional (2026-09-29)

Duplicate connection names in `config.yaml` fall into two behaviourally distinct classes that MUST
be treated differently. Collapsing them is silent credential destruction.

| Class | Shape | Correct action |
|---|---|---|
| **A** | a blank-key row beside a **keyed row of the same name** | collapse — the blank is a non-functional duplicate artifact |
| **B** | TWO rows sharing a name but carrying **DIFFERENT VALID keys** | **PRESERVE both** — they are distinct credentials |
| lone blank | blank row whose keyed sibling has a **different name** | **PRESERVE** — it is not a duplicate of that sibling |

- **Phase 1** unions ONLY on hard identity: `id`, oauth `email`, non-empty `api_key`.
- **Phase 2** builds `name.lower() → [indexes]` and unions a name group **only if**
  `group_has_blank = any(not api_key.strip())`. Name is an identity signal ONLY when at least one
  member is credential-less.
- The blank-row prune is **same-name scoped**: build
  `keyed_names = {name.lower() for rows with non-empty api_key and non-empty name}`, and drop an
  id-less blank **only if** its own `name.lower()` is in that set. Never compute
  `has_keyed = any(...)` across the whole provider.

> [!CAUTION]
> **Do NOT "simplify" Phase 2 back into an unconditional name union.** The delegated implementation
> emitted `("name", nm.lower())` unconditionally, union-finding the three Class B groups. Its own
> dry-run transcript proved the loss: `dropped codex (key=enc:gAAAAA...) reason=dup-name`,
> `dropped Free ...`, `dropped Connection 14 ...`, plus `connection_indexes remap: [0] -> dropped
> (dangling)`. That would have silently deleted **three valid provider credentials** on the next
> restart. Its test suite encoded the same data loss as expected behaviour
> (`total_removed == 8`), so green tests were not evidence of correctness.

- **Class B rows are deliberately NOT renamed** — renaming would invalidate `connection_indexes`.
- **Verified invariant:** live config is `providers=78 rows=104 keyed=99 blank=5 idless_blank=5
  dupname_groups=8`, and dedup takes it to **99 rows (exactly 5 removable)**. Any change that
  removes more than 5 from that shape is destroying credentials.
- **History:** an empty-credential prune was added in `69d68ad` and **deliberately REVERTED in
  `eb76b67`**. Do not re-introduce a provider-wide version.
- `scripts/migrate_dedup_connections.py` imports `_dedup_connection_rows` from `app.main` — single
  implementation, no fork. `--dry-run` is the default. **`--apply` is UNSAFE while the router holds
  `config.yaml`**: the running process overwrites the result on its next periodic save. The live
  config self-heals on the next user-commanded restart instead.

## 9. 🟠 HARD RULE — CC delegation prompts go over STDIN, never as an argv (2026-09-29)

Windows PowerShell 5.1 does **not** escape embedded double quotes when it builds a native command
line. Both bug prompts contain quoted phrases (`"empty delivery -> no backfill"`,
`"tool call invalid"`), so passing `-p $prompt` terminated the argument at the first quote and
spilled the remainder as extra argv tokens — `->` landed as a bare token and `claude.exe` rejected
it: `error: unknown option '->'`, **exit 1 after 1.3 seconds**, before CC ever connected to
anything. No debug log was created.

Proven in `.brain/scratch/quote_escape_experiment.txt`: naive `-p $raw` → `ARGV_COUNT=7` with
`ARG4=[->]`; escaping `"` as `\"` → `ARGV_COUNT=3`, string intact. And in
`.brain/scratch/stdin_fidelity.txt`: a 396-char multi-line prompt containing double quotes, `->` and
`[brackets]` piped over stdin round-tripped intact, `EXIT=0`.

- **REQUIRED shape:** `$prompt | & 'D:\npm\npm-global\claude.ps1' -p --flags...` — `-p` takes **NO**
  prompt argument; the prompt arrives on stdin. The npm shim branches on
  `$MyInvocation.ExpectingInput` and forwards `$input`, so piping is the supported path.
- Escaping would also work but is a **per-prompt landmine**: the next author who types a quote
  re-arms the identical failure. Prefer removing argv from the equation.
- **A dead CC must abort the chain.** The only gate in the original script was a compile check,
  which trivially passes when CC wrote nothing — so bug1 exited 1 and the chain advanced to bug2,
  which died identically and produced 0 bytes. Required gates, in order:
  `claude` exit ≠ 0 → ABORT; `compileall` ≠ 0 → ABORT; exit 0 but **tree unchanged** →
  classify FAILED/NO-OP, never success.
- Snapshot `git diff --name-only` + `git ls-files --others --exclude-standard` BEFORE and AFTER each
  task to detect the no-op case. CC saying "done" is not evidence.
- **Tests/assertions that check a pattern is ABSENT from a script MUST strip comments first** — the
  same gotcha as §3. A comment documenting the removal otherwise satisfies the assertion forbidding
  it, and today's own verification flagged the `-p $prompt` phrase inside the explanatory comment
  as a regression.

## 10. 🔴 HARD RULE — never full-re-harvest to fix ONE missing probe/axis/lane (2026-10-09)

A full harvest costs ~50-70 paid API calls (probes + judge + 3 v2 lanes). Re-running it
to fix a SINGLE missing piece wastes account credit AND destroys already-good axes
(2026-10-09: re-running full harvest for a missing capability_curve overwrote kimi's
creative=6 with 0). This is the SAME class of mistake as the wasteful terra full-lane
re-run earlier that session. Burned credit twice for nothing.

**Invariants — zero exceptions:**
- Diagnose the EXACT missing piece FIRST. Name it: judge axis? one v2 lane? knowledge_bank?
- Judge axis zero/missing (e.g. creative=0 while eq/humor/design have real `why`) -> judge-only
  backfill (`.brain/scratch/rejudge_creative.py`): ~3 calls, patches in place, NEVER re-runs probes.
- One v2 lane missing (tokenizer / capability_curve / knowledge_bank) -> targeted lane-only
  backfill `scripts/backfill_v2_lane.py --lane-name <lane>`: merges into the existing file,
  leaves judge axes + other v2 lanes untouched. Skips if already present.
- A full re-harvest is the LAST resort and requires the user to explicitly say "full re-harvest"
  in the current message. Default is always the smallest targeted fix.
- Before ANY harvest/backfill run: state the per-model call estimate and get the smallest-scope
  confirmation. "Fix X" means fix X, not re-run everything that includes X.
