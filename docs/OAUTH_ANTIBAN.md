# OAuth Anti-Ban Protocol — BSL Router

> **Scope:** how BSL Router relays **OAuth-subscription** providers (Antigravity / Google, OpenAI Codex, Anthropic Claude Code) so upstreams classify traffic as **authentic first-party client calls, not proxy calls**.
>
> **Status:** Protocol defined + gap analysis verified against live code (2026-10-13). Implementation items are specced, **not yet built** — see §6. Doc-only; no runtime code changed by this file.
>
> **Research backing:** companion brief `oauth_antiban_research.md` (conversation artifacts) holds the evidence, sources, and reasoning. This doc is the durable "what"; that brief is the "why".

---

## 1. Governing principle — a ban is a proxy-classification, not a rate limit

Providers do not primarily ban for "too many requests." They ban when a request **classifies as a relay/proxy instead of the official CLI.** Anti-ban = **minimize proxy-detection signal on every dimension simultaneously.** A leak on ANY single dimension classifies the traffic.

| # | Dimension | Provider measures | "Authentic" looks like |
|---|-----------|-------------------|------------------------|
| 1 | **Client identity** | headers, UA, version, beta flags | byte-for-byte the official CLI |
| 2 | **Behavioral cadence** | inter-request timing, burst shape | human/agent entropy — jittered, never metronomic |
| 3 | **Network origin** | IP class, stability, ASN | one stable residential IP per account |
| 4 | **Account graph** | payment, device, IP, phone linkage | accounts look unrelated to each other |
| 5 | **Quota shape** | per-account usage over time | one account's organic use, not a farm spread |
| 6 | **Telemetry / session** | session IDs, keep-alives, version calls | the CLI's real session lifecycle |

> **Hard rule:** *Account rotation cannot fix a request that already looks like a proxy.* Rotation only helps AFTER dimensions 1–3 are authentic; rotation on top of a leaky client just gets N accounts banned instead of one.

---

## 2. Antigravity (Google) — PRIORITY 1

**Evidence that shapes the pipeline:**
- Ban vector is **third-party client identity**, not volume — the Feb 2026 mass-ban wave (`403 "disabled for violation of ToS"`) hit OpenCode + `opencode-antigravity-auth` users on personal, low-volume accounts.
- **IP-level lockout is real:** a verified case had **5 AI-Pro accounts all hit a simultaneous 167-hour lockout** via IP-level rate limit / session bleed — rotating accounts over one IP did NOT protect them.
- **Wrong quota endpoint poisons accounting:** AI-Pro consumer accounts use a 5h sliding window + weekly quota on `daily-cloudcode-pa.googleapis.com`; the legacy `cloudcode-pa.googleapis.com` data lane returns a permanently-false `remainingFraction=1.0`, so you look un-throttled and trip the real (invisible) limit.

### Current posture (verified)
- ✅ **Dedicated hosts-bypass egress** ([app/utils/google_cloudcode_egress.py](../app/utils/google_cloudcode_egress.py)) — external-DNS channel to real Google, TLS/cert-verify ON, keeps Google OAuth creds out of 9Router's MITM.
- ✅ **Correct endpoint split** — `daily-cloudcode-pa` is the native/chat base (`main.py:1092`, `main.py:1504`); legacy `cloudcode-pa` is confined to the auth/login lane (`oauth.py:65-66` loadCodeAssist/onboardUser) and the CCPA path (`main.py:15648`). The auth domain is explicitly never MITM-intercepted ([app/mitm.py:706](../app/mitm.py)).
- ❌ **No cadence/jitter on this egress path** (see §5 gap G1).

### Authentic pipeline
1. Dedicated Google egress (done).
2. **One account = one stable egress IP**; never rotate N accounts over a single IP (that is the 5-account lockout pattern). Spread across distinct residential IPs and pin account↔IP.
3. Per-account **correct endpoint lane** so quota reads true (done).
4. Official Antigravity/Gemini-CLI client identity + real session lifecycle.
5. **Jittered cadence; pace to the 5h sliding window per account** — drain one account toward its window edge, then lane-switch; do not round-robin-spray all accounts.

---

## 3. OpenAI Codex — PRIORITY 2

**Evidence:**
- The `codex-429-autoban` problem is real: aggressive/un-paced Codex CLI loops get accounts flagged.
- **IP classification alone triggers zero-warning bans** — an 18-month Pro subscriber was banned purely for exiting a datacenter/shared-corporate IP scored as a bot-farm node. No automation, no ToS breach — just a bad IP class.
- Named risk patterns: request loops, retry bugs, machine-steady frequency, one account hammered from many contexts.

### Current posture (verified)
- ✅ **Client identity hardcoded correctly:** `originator: codex`, `OpenAI-Beta: codex-1`, `ChatGPT-Account-ID` (from `provider_data.chatgptAccountId`), `codex` header_profile pins `User-Agent: codex_cli_rs/0.61.0 (...)`.
- ❌ **No cadence/jitter on this egress path** (§5 G1); no adaptive 429 backoff (§5 G2).

### Authentic pipeline
1. `codex` header_profile on every Codex connection (done).
2. **`ChatGPT-Account-ID` always accompanies the token, never crosses accounts** — token + wrong/absent account-ID is an instant proxy tell.
3. **Stable residential IP per account; avoid datacenter egress entirely for Codex** (that single signal has caused zero-warning bans on its own).
4. **Per-account cadence governor**: jittered min-interval, no metronomic bursts, exponential backoff on 429 — **never retry-storm** (a retry loop is the exact autoban pattern).
5. Rotate accounts only to spread quota shape (dimension 5), after 1–4 hold.

---

## 4. Claude Code (Anthropic) — PRIORITY 3 (most hostile)

**Evidence:**
- **ToS explicitly bans third-party OAuth-token use.** 2026 timeline: Jan 9 server-side restriction of subscription OAuth tokens to the official client; Feb 19 formal ban on Claude OAuth tokens "in any other product, tool, or service"; Apr 4 enforcement cut off OpenClaw/OpenCode/NanoClaw. (Partial May 2026 reinstatement only via a paid programmatic-credit lane.)
- **Detection is telemetry-based, not just header-based** (Anthropic staff): third-party harnesses "generate unusual traffic patterns **without any of the usual telemetry that the Claude Code harness provides.**" Spoofing `User-Agent: claude-cli/...` is necessary but NOT sufficient — the session/telemetry rhythm is what's read.
- **Fingerprinting:** client telemetry/headers, residential-vs-datacenter IP + rapid-IP-change scrutiny, inhuman speed / repetitive request structure.
- **Aggressive, fast account linking** via shared payment / IP / device / phone. A second account after a ban is often re-banned "within hours."

### Current posture (verified)
- ✅ `claude_code` header_profile: `User-Agent: claude-cli/2.0.1 (external, cli)`, `anthropic-version: 2023-06-01`, `anthropic-beta: oauth-2025-04-20,interleaved-thinking-2025-05-14`, `x-app: cli`.
- ❌ **No session/telemetry-lifecycle mimicry** (§5 G4); no cadence/jitter (§5 G1).

### Authentic pipeline
1. `claude_code` header_profile (done).
2. **Mimic the CLI session lifecycle, not just headers:** keep-alive rhythm, version/telemetry surface, realistic think-time between turns. This is the dimension Claude weights most and the hardest to fake.
3. **Stable residential IP per account; never rapid IP-switch.**
4. **Fewest accounts, most human cadence** — because linking is aggressive, Claude is where you lean hardest on per-account quality over account quantity.
5. Human-like pacing, generous jitter, hard backoff on 429.

---

## 5. Gap analysis — verified against live code (2026-10-13)

| # | Capability | Status | Where / note |
|---|------------|--------|--------------|
| — | Codex / Claude client-identity headers | ✅ Done | `header_profile` codex + claude_code, `app/main.py` |
| — | Google dedicated egress (origin integrity) | ✅ Done | `app/utils/google_cloudcode_egress.py` |
| — | Antigravity correct quota-endpoint lane | ✅ Done | `daily-cloudcode-pa` native base; legacy `cloudcode-pa` auth-only (`main.py:1092/1504/15648`, `oauth.py:65`) |
| — | Per-connection circuit breaker / error taxonomy | ✅ Done | `app/circuit_breaker.py` — stops hammering a sick lane |
| — | Account selection spread (round-robin + warm-cache pick) | ✅ Done | `combo_resolver.py:348`, `model_resolver.py:25`, `caching.py:351 pick_warmest_connection` |
| **G1** | **Cadence / jitter / min-interval on the OAuth egress path** | ❌ **Missing in BSL Router** | zero `jitter/backoff/min_interval` in `app/`; lives only in Chat2API `cadence.py`, **not** on this path. **Single biggest gap.** |
| **G2** | **Adaptive 429 backoff (no retry-storm)** | ⚠️ Partial | breaker trips a sick lane, but no upstream-call pacing/jittered backoff |
| **G3** | **Session-lane pinning (account↔IP stickiness)** | ❌ Missing | the IP-level-lockout countermeasure; needs multi-egress-IP support first |
| **G4** | **Telemetry/session-lifecycle mimicry (Claude)** | ❌ Missing | headers spoofed, rhythm not |
| **G5** | **Graded authenticity pre-send check (bot→suspicious→human)** | ❌ Missing | scored 6-dimension check before dispatch |
| **G6** | **Per-account usage-budget tracker (Antigravity 5h/weekly; Codex/Claude windows)** | ❌ Missing | pace to the window edge instead of discovering it via 429 |

---

## 6. Implementation spec (priority order)

> Delegation is the default per project rules — these are specs for a delegated build, pending your sign-off on scope.

- **P0 — G1 Cadence governor on the OAuth egress path.** Port the proven Chat2API `cadence.py` model (per-account `asyncio.Lock` + jittered `BSL_CADENCE_MIN_INTERVAL_S`) into BSL Router's upstream dispatch for the three OAuth providers. *This is the highest-leverage fix — it directly removes the "machine-steady bot cadence" signal on every provider at once.*
- **P1 — G2 Adaptive 429 backoff.** Read `Retry-After`, exponential backoff with jitter, hard no-retry-storm, per provider; integrate with the existing circuit breaker so backoff and lane-trip agree.
- **P1 — G3 Session-lane pinning.** Bind account → stable egress identity (IP/profile). Prerequisite for safe multi-account Antigravity; depends on multi-egress-IP availability.
- **P2 — G4 Claude session mimicry.** Keep-alive + think-time rhythm that matches the official CLI.
- **P2 — G5 Graded authenticity pre-send check.** Score each outgoing request on the 6 dimensions; block/warn below threshold.
- **P2 — G6 Per-account usage-budget tracker** for Antigravity 5h/weekly and Codex/Claude windows.

---

## 7. Threat model & honesty box

- You cannot make a third-party relay *fully* indistinguishable to a provider that reads **telemetry rhythm** (Claude especially). The achievable goal is to **sit below the automated ban threshold** — look like a slightly-heavy but genuine first-party user.
- The two highest-leverage, lowest-effort wins are **(a) correct client identity + endpoint per provider** (done) and **(b) cadence/jitter on the egress path + stable IP-per-account** (G1 + G3). Chasing exotic fingerprint spoofing before these are nailed is wasted effort.
- Account quantity is a *diminishing, sometimes negative* lever: more accounts over shared IP/payment/device signals **raises** graph-linkage risk (Claude most, then Google, then OpenAI).
