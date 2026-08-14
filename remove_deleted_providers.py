"""Strip deleted provider blocks from config.yaml using YAML round-trip.

Deletion set = providers present in config but absent from ALL code registries
(backend PROVIDER_DEFAULT_URLS + oauth OAUTH_PROVIDERS + frontend KNOWN_PROVIDERS),
plus 'cerebras' (user-confirmed deleted despite lingering in the backend URL map).

Never touches:
  - type=custom / type=image_custom (user-owned proxies, 26 providers)
  - any provider present in the code registries
"""
import shutil
import sys
from datetime import datetime
from pathlib import Path

import yaml
from ruamel.yaml import YAML

ROOT = Path(__file__).parent
CONFIG = ROOT / 'config.yaml'

DELETED = {
    'alibaba', 'alibaba_intl', 'azure_openai', 'bytedance-img',
    'cerebras',  # user-confirmed deleted (still in backend URL map = code debt)
    'claude_code', 'clinepass', 'cloudflare', 'command_code',
    'gemini-cli', 'glm_china', 'glm_coding', 'google-veo-oauth',
    'iflow', 'kilo', 'kling-video', 'luma-video', 'mimo_free',
    'minimax_china', 'minimax_coding', 'nvidia_nim', 'ollama_cloud',
    'ollama_local', 'openai_codex', 'opencode', 'opencode-zen',
    'opencode_go', 'qwen', 'vercel_gateway', 'vertex_ai',
    'vertex_partner', 'volcengine', 'xai_grok', 'xiaomi_mimo',
    'xiaomi_mimo_token',
}

# 1. Backup
ts = datetime.now().strftime('%Y%m%d_%H%M%S')
backup = CONFIG.with_name(f'config.backup.{ts}.yaml')
shutil.copy2(CONFIG, backup)
print(f"Backup -> {backup.name}")

# 2. ruamel round-trip (preserves comments + formatting)
ry = YAML()
ry.preserve_quotes = True
ry.width = 4096  # prevent line re-wrapping of long api_key lines
with open(CONFIG, encoding='utf-8') as f:
    data = ry.load(f)

providers = data['providers']
removed = []
skipped_missing = []
for name in sorted(DELETED):
    if name in providers:
        del providers[name]
        removed.append(name)
    else:
        skipped_missing.append(name)

with open(CONFIG, 'w', encoding='utf-8', newline='\n') as f:
    ry.dump(data, f)

print(f"\nRemoved {len(removed)} provider blocks:")
for n in removed:
    print(f"  - {n}")
if skipped_missing:
    print(f"\nNot found (already absent): {skipped_missing}")

# 3. Re-verify with plain yaml
with open(CONFIG, encoding='utf-8') as f:
    cfg = yaml.safe_load(f)
remaining = set(cfg.get('providers', {}).keys())
leftover = remaining & DELETED
print(f"\nRemaining providers: {len(remaining)}")
print(f"Leftover deleted names: {leftover or 'NONE'}")

# Count custom providers survived
custom_survived = [p for p, v in cfg['providers'].items()
                   if (v.get('type') or '').endswith('custom')]
print(f"Custom providers survived: {len(custom_survived)} (expected 26)")
assert not leftover, "Leftover deleted providers!"
assert len(custom_survived) == 26, "Custom provider count changed!"
print("\nVERIFIED OK")
