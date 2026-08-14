CRITICAL: Work ONLY in d:\Projects\BSL Router.
DO NOT use antigravity-bridge for task pickup.

# Task: Stream-Deadline Retry Ladder (600 → 300 → 600 → hard-stop)

## Goal

When a streaming request hits the 600s `stream_deadline` wall-clock cap, instead of
force-closing with a synthetic error frame, RETRY:

| Fire | Action | Attempt deadline |
|---|---|---|
| 1st | retry same leaf (single) or next combo entry | 300s |
| 2nd | retry same leaf (single) or next combo entry | 600s |
| 3rd | HARD STOP — protocol-correct terminal frames, no retry | — |

Combos: every fire advances to the next chain entry (fresh 3-rung ladder per entry),
bounded by the existing `CHAIN_TOTAL_BUDGET` chain budget.

**STREAM-GUARD INVARIANT (ABSOLUTE, REGRESSION TESTS EXIST):** once ANY upstream-model
bytes have reached the client, NO fallback/retry of any kind is allowed — a deadline
fire post-emission MUST hard-stop with terminal frames. `StreamEmissionState.may_fallback`
is the sole veto. Do not weaken it.

## Architecture (follow exactly — already decided)

The current 600s cap lives in `app/antifreeze.py::stream_deadline`, wrapped by
`afz_guard` at the outermost `StreamingResponse` layer — OUTSIDE every
`_ComboFallbackNeeded` catcher, so it can never retry. DO NOT try to make
`stream_deadline` itself retry. Instead:

1. **Disable the outer wall-clock deadline for chat-completion streaming** by passing
   `deadline_s=0` (treated as "no deadline" by `stream_deadline`, see antifreeze.py:167)
   to the `afz_guard(...)` calls that wrap the chat-completion egress generators.
   Sites in `app/main.py` (verify line numbers with grep before editing):
   - OpenAI-family: `afz_guard(raw_upstream_guarded(), _afz_sid)` (~7478) and
     `afz_guard(kiro_adapter.kiro_raw_to_openai_sse(raw_upstream_guarded()), _afz_sid)` (~7474)
   - Anthropic→OpenAI converter: `afz_guard(anthropic_to_openai_egress_stream_guarded(), ...)` (~7460)
   - Gemini egress: both `afz_guard(gemini_egress_stream_guarded(), _afz_sid, ...)` (~7122, ~7128)
   Find them by grepping `afz_guard(` in app/main.py; ONLY the ones whose inner
   generator is one of those 4 chat-completion egress families. Do NOT touch
   afz_guard sites wrapping error-probe generators (e.g. `_gemini_probe_error`),
   health endpoints, or non-chat streams.

2. **Add module-level ladder constants** near `CHAIN_TOTAL_BUDGET` (~line 278):
   ```python
   # Stream-deadline retry ladder (2026-08-14): attempt deadlines in seconds.
   # Attempt N uses DEADLINE_LADDER[min(N-1, len-1)]... see ladder helper.
   STREAM_DEADLINE_LADDER = (600.0, 300.0, 600.0)
   ```
   Semantics: attempt 1 gets 600s; if it stalls the WHOLE window, retry at 300s;
   if that stalls, retry at 600s; if THAT stalls, hard stop.
   Helper: `def _ladder_deadline(attempt: int) -> float: return STREAM_DEADLINE_LADDER[min(attempt, len(STREAM_DEADLINE_LADDER)-1)]`
   where attempt is 0-indexed (0→600, 1→300, 2→600).

3. **New exception** next to `class _ComboFallbackNeeded(Exception)` (~4596):
   ```python
   class _DeadlineRetryNeeded(Exception):
       """Wall-clock attempt deadline exhausted with zero bytes emitted.
       Carries the attempt count; the guarded wrapper retries the SAME leaf
       with the next ladder deadline."""
       def __init__(self, attempt: int, err: str):
           self.attempt = attempt
           self.err = err
           super().__init__(f"deadline_retry attempt={attempt}: {err}")
   ```

4. **Stall-pump wrapper** inside `_process_chat_completion`, defined right after
   `_stall_watchdog` (~5900). It must be a nested closure (closes over `stats`,
   `_emit`, `bench_leaf` imports already in scope). Behavior:

   ```python
   def _deadline_stall_pump(raw_iter, attempt: int, deadline_s: float, label: str):
       """Forward upstream bytes under an attempt-level wall-clock deadline.
       On expiry with zero emitted content bytes: raise _DeadlineRetryNeeded
       (single-leaf retry) or route to combo fallback. Post-emission expiry:
       return normally so the existing zero-output/stall handlers run
       (they hard-stop correctly)."""
   ```
   Implementation sketch (async generator):
   - `loop = asyncio.get_running_loop()`, `iterator = raw_iter.__aiter__()`,
     `deadline = loop.time() + deadline_s`
   - loop: `remaining = deadline - loop.time()`; if `remaining <= 0` → fire handler → break/return
   - `chunk = await asyncio.wait_for(iterator.__anext__(), timeout=remaining)`
     (catch `StopAsyncIteration` → return; `asyncio.TimeoutError` → fire handler)
   - fire handler:
     - if `_emit.has_emitted` (check the attribute name on StreamEmissionState —
       read app/middleware/stream_guard.py; use the existing veto accessor):
       `stats["error"] = stats["error"] or f"stream_deadline_{int(deadline_s)}s"` →
       `return` (fall through to existing handlers which emit error+[DONE])
     - else set `stats["error"] = f"deadline_stall_attempt{attempt+1}_{int(deadline_s)}s"`;
       `bench_leaf(config, provider_name, target_model, 504, stats["error"], stats.get("out", 0))`
       (only inside try/except so bench failure never kills the pump)
     - combo path: `_next_idx = (_retry_state["idx"] + 1) if _retry_state else 1`;
       if `active_chain and _next_idx < len(active_chain)`:
       budget check `if _chain_budget_remaining() <= 0:` → log `[AFZ-DEADLINE] ...`
       and fall to single path; `elif _emit.may_fallback("deadline_stall"):` →
       `raise _ComboFallbackNeeded(504, stats["error"], {"chain": active_chain,
       "idx": _next_idx, "cache_bp": _cache_breakpoints,
       "original_model": original_model, "deadline": _chain_deadline})`
     - single path: `if attempt < len(STREAM_DEADLINE_LADDER) - 1:`
       `raise _DeadlineRetryNeeded(attempt, stats["error"])`
       else: `return` (existing handlers emit terminal frames)
   - `finally`: `try: await raw_iter.aclose() except BaseException: pass`
     — BUT raw_iter may be a bare async iterator without aclose; guard with
     `ac = getattr(raw_iter, "aclose", None)` and only await if callable.
   - CancelledError/GeneratorExit: re-raise untouched (never convert to retry).

5. **Wrap the 4 pump sites** (replace `_stall_watchdog(X)` with
   `_deadline_stall_pump(X, _attempt, _ladder_deadline(_attempt), <label>)`):
   - `raw_upstream()` main pump (~5978): `_stall_watchdog(resp.aiter_raw())`
   - the S3/S6 continuation pump (~6052) — same treatment (continuation bytes are
     still upstream model output; same veto applies)
   - find the Gemini egress pump and the anthropic_to_openai egress pump by
     grepping `_stall_watchdog(` in app/main.py — wrap those too (labels
     "gemini", "anthropic-egress").
   Leave `_stall_watchdog` itself unchanged (it's a pure passthrough + docs).

6. **Attempt counter + retry loop in `raw_upstream_guarded()`** (~6157). Restructure:
   ```python
   async def raw_upstream_guarded():
       _attempt = 0
       while True:
           _raw_source = raw_upstream(_attempt)   # see step 7
           try:
               async for _c in _raw_source:
                   yield _c
               return  # clean completion
           except _DeadlineRetryNeeded as _dr:
               # zero-emission guaranteed by the pump's veto check
               print(f"[Deadline Ladder] '{model}' attempt {_dr.attempt+1} stalled "
                     f"({stats.get('error')}) — retrying same leaf "
                     f"{target_model}/{provider_name} at "
                     f"{_ladder_deadline(_dr.attempt+1)}s", flush=True)
               _attempt = _dr.attempt + 1
               # reset per-attempt stats so the next attempt isn't poisoned
               stats["ttft"] = 0.0; stats["out"] = 0; stats["status"] = 500; stats["error"] = None
               continue
           except _ComboFallbackNeeded as _cf_raw:
               ... existing fallback handling, then RETURN (do not continue the loop) ...
           except (GeneratorExit, asyncio.CancelledError):
               raise
           except Exception as _raw_guarded_err:
               ... existing error+[DONE] handling ...
               return
           finally:
               try: await _raw_source.aclose() except BaseException: pass
   ```
   IMPORTANT: the `finally` aclose must run on every loop iteration — keep it inside
   the `while True` body structure (wrap each iteration's try/finally), and ensure the
   `continue` path does not leak the source generator. A clean pattern:
   ```python
   while True:
       _raw_source = raw_upstream(_attempt)
       _retry_again = False
       try:
           async for _c in _raw_source: yield _c
       except _DeadlineRetryNeeded as _dr:
           _attempt = _dr.attempt + 1; _retry_again = True
           ... log + stats reset ...
       except _ComboFallbackNeeded as _cf_raw:
           ... existing ...
           return
       except (GeneratorExit, asyncio.CancelledError):
           raise
       except Exception as _e:
           ... existing error frames ...
           return
       finally:
           try: await _raw_source.aclose() except BaseException: pass
       if not _retry_again:
           return
   ```
   Cap ladder abuse: `if _attempt >= len(STREAM_DEADLINE_LADDER): return` defensively.
   Note: obs.log_request in raw_upstream()'s finally already fires per attempt —
   acceptable (each attempt is one upstream call). Keep it.

7. **Thread the attempt through `raw_upstream()`**: change signature to
   `async def raw_upstream(_attempt: int = 0):` and pass `_attempt` into the pump
   wrappers at the sites from step 5. The zero-output handler (~6062) and stall
   handler (~6083) inside raw_upstream already emit error+[DONE] — after the ladder
   exhausts, the pump returns with stats["error"]="deadline_stall_attempt3_600s";
   make sure those handlers don't swallow it: the existing `except Exception` /
   zero-output paths will emit terminal frames; verify by reading that a deadline
   error results in error+[DONE] (not a silent return). If the zero-output combo
   raise would fire on a deadline_stall error for a combo, let it — advancing the
   chain on zero output is correct.

8. **Gemini + anthropic-egress families**: their guarded wrappers
   (`gemini_egress_stream_guarded`, `anthropic_to_openai_egress_stream_guarded`)
   have their own `except _ComboFallbackNeeded` handlers already. Apply the SAME
   pattern as step 6 but MINIMAL: wrap their pump sites (step 5), add attempt loop
   in their guarded wrappers IF they have a structure like raw_upstream_guarded.
   If a family's guarded wrapper is too structurally different, fallback to:
   wrap the pump so deadline expiry pre-emission raises _ComboFallbackNeeded for
   combos and _DeadlineRetryNeeded for singles, and implement the attempt loop in
   that family's guarded wrapper. Do NOT leave any family where a deadline fire
   post-emission could retry.

## Constraints (HARD)
- Post-emission: NEVER retry. Only terminal frames.
- `_DeadlineRetryNeeded` must never escape to the client: every guarded wrapper
  catches it. Add a final `except _DeadlineRetryNeeded` fallback in each wrapper
  that converts to error+[DONE] defensively.
- Do NOT change `stream_deadline`/`afz_guard` signatures or defaults — only pass
  `deadline_s=0` at the specific chat-egress call sites.
- Do NOT touch non-streaming paths, the vision pipeline, or tests other than
  adding the new test file below.
- Preserve all existing comments/docstrings you don't directly replace.

## Verification
1. `python -m py_compile app/main.py app/antifreeze.py`
2. `python -m pytest app/tests/test_stream_guard_invariant.py app/tests/test_chain_deadline.py app/tests/test_stream_guard_coverage.py -x -q`
3. New test file `app/tests/test_deadline_ladder.py` (write it):
   - ladder helper returns (600, 300, 600) for attempts 0,1,2 and 600 for 3+
   - monkeypatch STREAM_HARD_DEADLINE irrelevant (we pass deadlines explicitly)
   - unit test the pump: an iterator yielding nothing + tiny deadline (0.05s) with a
     fake `_emit` that has NOT emitted → raises _DeadlineRetryNeeded for single
     (no active_chain); raises _ComboFallbackNeeded when active_chain has 2 entries;
     returns normally when the fake _emit HAS emitted (no raise)
   - keep tests fast: all deadlines < 0.2s, no sleeps beyond pump timeouts.
4. Run the new tests: `python -m pytest app/tests/test_deadline_ladder.py -x -q`
5. `python -m pytest app/tests/ -x -q` (full suite; if pre-existing unrelated
   failures exist, list them explicitly and do NOT "fix" unrelated code)

## Finish protocol (REQUIRED)
- Run the verification commands above.
- Print `FINAL_GIT_STATUS` then `git status --short`.
- Print `FINAL_DIFF_FILES` then `git diff --name-only`.
- Summarize files changed and the test results.
- Explicitly state if any expected source file did NOT change.
- Exit when done.
