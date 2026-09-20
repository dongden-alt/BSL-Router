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
| venvs | `.venv` (dev, Py3.10 — gitignored) and `.venv311` (CI-parity Py3.11 leg — visible-untracked, never bulk-add) |
| Tests | `app/tests/` — suite must stay green; launcher-pattern assertions in `app/tests/test_mitm_lifecycle.py` |
| Gate script | `scripts/pre-push-check.ps1` (read-only; exit 0 = pass / 1 = fail) |
| Reference KIs | `ki-bslrouter-log-stall-ide-shutdown`, `ki-bslrouter-github-push-guideline`, `ki-ci-version-parity-push-discipline` |
