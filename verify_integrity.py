import glob
import json

import yaml

backup = sorted(glob.glob('config.backup.*.yaml'))[-1]
with open(backup, encoding='utf-8') as f:
    old = yaml.safe_load(f)
with open('config.yaml', encoding='utf-8') as f:
    new = yaml.safe_load(f)

assert set(old.keys()) == set(new.keys()), 'TOP-LEVEL KEYS CHANGED!'
for k in old['providers']:
    if k in new['providers']:
        assert json.dumps(old['providers'][k], sort_keys=True) == json.dumps(new['providers'][k], sort_keys=True), f'provider {k} mutated!'
for k in old:
    if k != 'providers':
        assert json.dumps(old[k], sort_keys=True) == json.dumps(new[k], sort_keys=True), f'section {k} mutated!'

old_models = sum(len(p.get('models') or []) for p in old['providers'].values())
new_models = sum(len(p.get('models') or []) for p in new['providers'].values())
print(f"providers: {len(old['providers'])} -> {len(new['providers'])}")
print(f"models:    {old_models} -> {new_models}  (naked models removed: {old_models - new_models})")
print("STRUCTURAL INTEGRITY: OK")
