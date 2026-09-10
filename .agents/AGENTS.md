# BSL Router — Project Rules

## 🔴 HARD RULE — BSL Router restarts require explicit user command (2026-08-28)

**NEVER restart, stop, kill, or respawn the BSL Router process (`:6969`) without an explicit command from the user in the current conversation.**

This applies to ALL of the following, none of which may be done autonomously:
- Running `shutdown_bsl.ps1`, `START-9router.bat`, or any start/stop script
- Killing the `python.exe -m app.dualstack_serve --port 6969` process (or its supervisor lineage) via CIM/`taskkill`/Win32_Process
- Spawning a new supervisor instance that would take over the port
- Touching the `.brain/logs/router-6969.lock` lockfile to force a lineage change

**Why:** BSL Router is the user's live production AI backbone (Claude Code + all agents route through it). An unsupervised restart drops every in-flight request, can trigger the WinError-64 accept-death recovery chain, and the old-process-holding-port failure mode makes a half-executed restart leave the router in a stale-config state that *looks* healthy.

**What to do instead when a config change requires a reload:**
1. Make the config change (e.g. add provider to `config.yaml`)
2. Tell the user: "config changed — BSL needs a restart to pick it up, say the word"
3. WAIT. The user decides when it's safe to bounce the router.

The only exception: the user explicitly says "restart it" / "bounce it" / equivalent in the current session.

---

## 🔴 HARD RULE — every `.brain/logs` writer stays capped AND off the event loop (2026-08-31)

**This is the bug that made Antigravity IDE shut down mid-session.** `antigravity_inbound.jsonl`
reached **29.8 GB** because it was appended *synchronously on the FastAPI event loop with no size
cap*. Each append then cost seconds of disk time, stalling every in-flight stream for minutes →
upstream reads timed out → zero-output force-stop → the IDE's language servers died with the
connection. `mitm_egress_frames.jsonl` hit 4.3 GB the same way.

Invariants — do not regress any of these:
- Capture writes go through the **async queue writer** (`asyncio.to_thread`) and the queue is
  **drop-on-full**. A slow disk must NEVER back-pressure inference: drop a record, never block.
- All three loggers rotate at **50MB** — `antigravity_inbound.jsonl`, `mitm_egress_frames.jsonl`,
  `mitm_live_debug.log` — plus boot-time self-heal truncation for files already over cap.
- Inbound capture is **metadata-only by default**. Full payloads only under `BSL_CAPTURE_INBOUND=1`.
- **Adding a new log writer? Cap it and keep it off the hot path in the same commit.**
  Currently uncapped but default-OFF: `upstream_failures.jsonl` (gate `tools.conn_trace`) and
  `outbound_upstream.jsonl` (gate `tools.outbound_forensics`). Enabling either without adding a
  cap re-arms this bug.
- MITM does **not** need to be running for this protection: the cap lives on the router's `:6969`
  request path. The two MITM-side writers only log when traffic flows through `:443`.

Diagnosing a future IDE shutdown: check `.brain/logs` **sizes first**. KB/MB-scale means this fix
held — look elsewhere. Then grep `child_out.log` for clustered `content missing` /
`Client disconnected`. Full RCA: KI `ki-bslrouter-log-stall-ide-shutdown`, and `.brain/CHANGELOG.md`
entries 2026-08-30 / 2026-08-31.

## 🟠 HARD RULE — launcher health gate carries NO credential (2026-08-31)

`Start-App`'s idempotence probe in `scripts/bslrouter.ps1` hits **unauthenticated `/health`** on
`127.0.0.1` AND `[::1]`, each inside its **own** `try/catch`. Three traps, all previously shipped bugs:

- **No API key in `scripts/bslrouter.ps1`, ever.** The old probe carried a hardcoded key that was a
  *typo* of the real one (dropped `j`); it "worked" only because `/v1/models` does not enforce auth.
  The day auth lands on that route, the probe 401s → gate concludes "unhealthy" → **kills a healthy
  router and respawns** — precisely the restart loop the gate exists to prevent. Secondary: since
  `config.yaml` is gitignored, the launcher was the only committed copy of a live key.
- **One `try` per stack, never a shared one.** `Invoke-WebRequest` raises *terminating* errors on a
  refused connection, so a shared `try{}` lets a dead IPv4 stack abort the block before IPv6 is ever
  probed — the "dual-stack" check could only ever report IPv4. `-ErrorAction SilentlyContinue` does
  NOT downgrade terminating errors.
- **Tests asserting "pattern X is absent from the launcher" MUST strip comments first.** Use
  `_powershell_code_only()` in `app/tests/test_mitm_lifecycle.py`. Otherwise the comment that
  documents a removal satisfies the assertion forbidding it — this bit twice in one session.

`/v1/models` is still unauthenticated by design; adding auth is a breaking change for every existing
client and needs an explicit user decision. The launcher no longer depends on that route either way.

---

## 🔴 HARD RULE — pre-push secret verification (2026-09-10)

**Every `git push` to origin MUST pass all 4 gates below before the push command runs.**
No exceptions for "small" pushes, doc-only pushes, or "just one file" pushes.
This rule was written after a 2-day forensic audit found a partial API key fragment
(`Q4jHjmqdyCy5`) leaked in a *comment inside the very fix commit* that removed the
key from code — commit `042a77a` fixed the launcher but documented the real key tail
in its explanatory comment. If the fix commit leaks, every commit can leak.

**Run `scripts/pre-push-check.ps1` OR execute the 4 gates manually — both are valid.**

### Gate 1 — tracked-tree secret sweep
```powershell
git grep -I -n -e 'sk-bsl-LzAC' -e 'LzACIxbnpt' -e 'Q4jHjmqdyCy5' -e 'Q4jHmqdyCy5'
```
Exit non-zero (find anything) → **STOP.** Redact, commit, re-run. The partial-key
fragments are the last 12 chars of the real admin key. Any hit = leak, including
hits inside comments, docstrings, test fixtures, or error messages.

### Gate 2 — machine-specific path sweep
```powershell
git grep -I -n -e 'D:/Tools' -e 'D:\\Tools' -e 'd:/Projects/BSL' -e 'D:\\Projects\\BSL' -e 'C:/Users/Admin' -e 'C:\\Users\\Admin'
```
Exit non-zero → **STOP.** Replace with portable placeholders (`YOUR_TOOLS_PATH`,
`YOUR_PROJECT_PATH`). Machine paths in `.mcp.json`, scripts, or configs are PII-adjacent
and break portability.

### Gate 3 — staged-diff review
```powershell
git diff --cached --stat   # or git diff origin/main...HEAD --stat if already committed
git diff --cached | Select-String -Pattern 'sk-bsl-[A-Za-z0-9]{10,}' -Context 2
```
Manually scan the staged diff for any string matching `sk-bsl-` followed by 10+
alphanumeric chars (the real key pattern). Dummy placeholders (`sk-bsl-YOUR_API_KEY_HERE`)
are **expected and safe** — they fail this regex because `YOUR_API_KEY_HERE` has
underscores, not the `[A-Za-z0-9]{10,}` pattern.

### Gate 4 — gitignore invariant
```powershell
git check-ignore config.yaml .bsl_key .bsl_key.dpapi .brain/ .venv/ .agents/
```
All 7 paths MUST return as ignored. If any returns nothing → **STOP.** The `.gitignore`
was tampered with or a rebase dropped a line. Fix before pushing.

### After push — verify remote
```powershell
git ls-remote origin <branch>   # confirm remote HEAD = local HEAD
```

**Known-safe patterns (do NOT flag these as leaks):**
- `localhost:6969` / `127.0.0.1:6969` / `[::1]:6969` — canonical dev port, expected in docs/tests
- `sk-bsl-YOUR_API_KEY_HERE` — dummy placeholder by design
- `admin@chatbot.local` in commit metadata — local hostname, not a secret (cosmetic only)
- `REDACTED-BSL-ADMIN-KEY` — redaction placeholder, not the real key

**Rationale:** the 2026-09-10 audit proved that "code works" ≠ "repo is clean."
The partial key fragment survived 4+ commits across 2 days because nobody ran a
fragment-level grep. This gate makes the check mechanical — 30 seconds, zero ambiguity.
