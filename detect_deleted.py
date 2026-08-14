"""Definitive deleted-provider detector.

Logic: a provider in config.yaml is DELETED if it is absent from ALL code registries:
  1. backend PROVIDER_DEFAULT_URLS (app/main.py)
  2. backend OAUTH_PROVIDERS (app/oauth.py)
  3. frontend KNOWN_PROVIDERS (app/static/app.js)

`type: custom` providers are excluded from the check entirely (user-owned proxies).
"""
import re
import sys
import yaml
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from app.main import PROVIDER_DEFAULT_URLS
from app.oauth import OAUTH_PROVIDERS

with open('config.yaml', encoding='utf-8') as f:
    cfg = yaml.safe_load(f)
providers = cfg.get('providers', {})

# --- Parse frontend KNOWN_PROVIDERS from app.js ---
js = (ROOT / 'app' / 'static' / 'app.js').read_text(encoding='utf-8')
known_section = js[js.index('const KNOWN_PROVIDERS'):js.index('const BLACKSAND_PROVIDER_ID')]
fe_ids = set(re.findall(r"id:\s*'([^']+)'", known_section))

backend_urls = set(PROVIDER_DEFAULT_URLS.keys())
oauth_ids = set(OAUTH_PROVIDERS.keys())

# Extra built-in virtuals defined in app.js (not in KNOWN_PROVIDERS)
fe_ids.add('blacksand')

all_known = backend_urls | oauth_ids | fe_ids

print(f"backend PROVIDER_DEFAULT_URLS: {len(backend_urls)}")
print(f"oauth OAUTH_PROVIDERS:         {len(oauth_ids)}")
print(f"frontend KNOWN_PROVIDERS:      {len(fe_ids)}")
print(f"UNION all-known:               {len(all_known)}\n")

deleted = []
active_known = []
custom = []

for name in sorted(providers):
    p = providers[name]
    ptype = p.get('type')
    nm = len(p.get('models') or [])
    # User-owned custom providers never appear in code registries by design.
    # This covers BOTH 'custom' and 'image_custom' (e.g. vsllm-i has a live base_url).
    if ptype and ptype.endswith('custom'):
        custom.append((name, nm))
        continue
    if name in all_known:
        active_known.append((name, nm))
    else:
        deleted.append((name, ptype, nm))

print(f"===== CUSTOM (skip): {len(custom)} =====")
print(f"===== KNOWN + active in code: {len(active_known)} =====")
for n, nm in active_known:
    print(f"  OK    {n:22s} models={nm}")

print(f"\n===== DELETED (in config, absent from ALL code registries): {len(deleted)} =====")
total_models = 0
for n, t, nm in deleted:
    total_models += nm
    print(f"  DEL   {n:22s} type={t or 'NONE':12s} models={nm}")
print(f"\nTotal naked models to remove: {total_models}")

# --- Cross-check: opencode-zen vs opencode-go model overlap ---
print("\n===== opencode-zen vs opencode-go model comparison =====")
zen_models = [m.get('id') for m in (providers.get('opencode-zen', {}).get('models') or [])]
go_models = [m.get('id') for m in (providers.get('opencode-go', {}).get('models') or [])]
print(f"  opencode-zen models: {zen_models}")
print(f"  opencode-go  models: {go_models}")
if zen_models and zen_models == go_models:
    print("  -> IDENTICAL: opencode-zen is a pure duplicate of opencode-go")
else:
    only_zen = set(zen_models) - set(go_models)
    print(f"  -> only in opencode-zen: {sorted(only_zen)}")

# --- Cross-check: are deletion candidates referenced as aliases in code? ---
import re as _re
main_src = (ROOT / 'app' / 'main.py').read_text(encoding='utf-8', errors='ignore')
norm_src = (ROOT / 'app' / 'normalizer.py').read_text(encoding='utf-8', errors='ignore') if (ROOT / 'app' / 'normalizer.py').exists() else ''
print("\n===== deletion candidates referenced in main.py/normalizer.py? =====")
for n, t, nm in deleted:
    hits_main = main_src.count(f"'{n}'") + main_src.count(f'"{n}"')
    hits_norm = norm_src.count(f"'{n}'") + norm_src.count(f'"{n}"')
    if hits_main or hits_norm:
        print(f"  !! {n:22s} main.py={hits_main} normalizer.py={hits_norm}")
print("  (done)")
