CRITICAL: Work ONLY in d:\Projects\BSL Router.
ALLOWED WRITE PATHS: app/mitm.py, .brain/CHANGELOG.md, .brain/logs/* (verification output only).
READ-ONLY PATHS: everything else in the repo; do NOT modify config.yaml, app/main.py, or any other source file.
DO NOT use antigravity-bridge for task pickup.

## Goal
Fix the ~30-60s Antigravity IDE startup-auth delay caused by `load_config()` in `app/mitm.py` parsing the 230KB `config.yaml` with pure-Python YAML synchronously inside mitmproxy's single event loop, twice per connection.

## Root cause (already diagnosed — do NOT re-investigate)
- `load_config()` docstring claims mtime-caching but has NO cache: every call re-reads + `yaml.safe_load`s the whole 230KB file (measured 1.1-1.4s/call; explicit `yaml.CSafeLoader` = 181ms).
- Called twice per managed connection: `request()` (~line 393) and `server_connect()` (~line 700).
- Live probe: direct :6969 = 47ms; through MITM :443 = 16,328ms mean.

## Changes Required (app/mitm.py ONLY)
1. Add a module-level cache for the parsed config:
   - `_CONFIG_CACHE` holding `(config_path, st_mtime, parsed_dict)`.
   - In `load_config()`: resolve path, `os.stat` it; if cache matches path+mtime -> return cached dict immediately.
   - Otherwise parse with `yaml.load(f, Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))` and update the cache.
   - Preserve the function's return contract exactly (same dict shape; same behavior when the file is missing — read the current code first and keep its exact missing-file behavior).
   - If `_last_config_path` / `_last_mtime` module vars become unused, remove or keep them as needed, but check for other references first (grep the file).
2. Update the `load_config()` docstring to accurately describe the mtime-keyed cache + C loader.
3. Do NOT change routing logic, hijack decisions, debug-log behavior, or anything else in the file. Keep all unrelated comments intact.

## Verification (you MUST run these from repo root d:\Projects\BSL Router with .venv\Scripts\python.exe)
- `.venv\Scripts\python.exe -c "import ast; ast.parse(open(r'app/mitm.py',encoding='utf-8').read())"` — syntax OK.
- Benchmark the cached path, e.g.:
  `.venv\Scripts\python.exe -c "import time; import app.mitm as m; c=m.load_config(); print(type(c), len(c)); t0=time.perf_counter(); m.load_config(); print(f'cached call: {(time.perf_counter()-t0)*1000:.3f}ms')"`
  Second call MUST be <1ms. If importing app.mitm has heavy side effects or fails, report that and instead verify via a minimal harness that execs the load_config function in isolation — but try the real import first.
- Confirm the Loader used is yaml.CSafeLoader when available (you can print it once during verification).

## Output Contract
- Run verification commands, then:
- Print `FINAL_GIT_STATUS` followed by `git status --short`
- Print `FINAL_DIFF_FILES` followed by `git diff --name-only`
- Summarize files changed.
- Explicitly state if app/mitm.py did NOT change.
- Exit when done.
