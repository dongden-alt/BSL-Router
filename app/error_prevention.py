"""
Auto Error Prevention — Progressive softban/longban/disable for failing models.

Tracks consecutive same-error streaks per (provider, model, error_type) tuple.
Escalates through: monitoring → softban (5min) → longban (1hr) → disable + notify.

State persists in globalConfig.error_prevention_state to survive server restarts.
"""
import os
import time
import json
import tempfile
from typing import Optional, Dict, Any, Tuple

# Top-level import: app.crypto has no app.main dependency, so this cannot
# form a cycle even though app.main imports this module.
from app.crypto import encrypt_config_secrets


def _persist_config_yaml(config: Dict[str, Any]) -> None:
    """Atomic, never-wipe config.yaml write (mirrors main._persist_config_snapshot).

    Avoids a circular import with app.main. Refuses to overwrite a rich
    on-disk config with an empty/degenerate providers snapshot.
    """
    import yaml
    import copy as _copy
    providers = (config or {}).get("providers")
    if not providers:
        try:
            if os.path.exists("config.yaml") and os.path.getsize("config.yaml") > 64:
                print(
                    "[ErrorPrevention] Refusing to overwrite a non-empty config.yaml "
                    "with an empty providers snapshot — write suppressed.",
                    flush=True,
                )
                return
        except OSError:
            pass

    # --- Protection 2b: refuse massive provider-count regression -----------
    # Mirrors the 2026-08-10 fix in main._persist_config_snapshot. A
    # corrupted in-memory config with 1 provider must not overwrite a
    # 119-provider on-disk config. Refuse writes where new count < 50% of
    # existing, with a floor of 10 so small legitimate configs are safe.
    if providers:
        try:
            if os.path.exists("config.yaml"):
                with open("config.yaml", "r", encoding="utf-8") as _f:
                    _existing = yaml.safe_load(_f) or {}
                _existing_provs = _existing.get("providers") or {}
                _existing_count = len(_existing_provs)
                _new_count = len(providers)
                if _existing_count >= 10 and _new_count < _existing_count * 0.5:
                    print(
                        "[ErrorPrevention] Refusing to overwrite a "
                        f"{_existing_count}-provider config.yaml with only "
                        f"{_new_count} providers - write suppressed "
                        "(2026-08-10 wipe signature).",
                        flush=True,
                    )
                    return
        except Exception:
            pass  # Best-effort gate; never block writes on a read failure.

    tmp_fd, tmp_path = tempfile.mkstemp(prefix="config.yaml.", suffix=".tmp", dir=".")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            # P0-2: caller passes decrypted config; encrypt a copy before disk.
            enc_config = encrypt_config_secrets(_copy.deepcopy(config))
            yaml.dump(enc_config, f, default_flow_style=False, sort_keys=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, "config.yaml")
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


class ErrorPreventionManager:
    """
    Manages progressive ban escalation for models with repeated errors.
    
    State structure (stored in globalConfig['error_prevention_state']):
    {
        "vietapi/claude-opus-4-7/timeout": {
            "streak": 3,
            "last_error_time": 1719762000.0,
            "ban_state": "softban" | "longban" | "disabled" | null,
            "ban_until": 1719762300.0,
            "ban_escalation_count": 0,
            "error_type": "timeout",
            "provider": "vietapi",
            "model": "claude-opus-4-7"
        }
    }
    """
    
    ERROR_TYPES = {
        'timeout': ['timeout', 'timed out', 'time out'],
        'auth': ['401', '403', 'unauthorized', 'forbidden', 'authentication'],
        # Upstream aggregators (especially VSLLM) mask quota exhaustion as nested
        # 400/500 bodies.  9Router surfaces these same incidents as real 429s.
        # Patterns cover: English, Chinese (速率限制/请求频率), VSLLM code 1302,
        # quota/limit wrappers, and the Python ASCII codec encoding artefact.
        'rate_limit': ['429', 'rate limit', 'too many requests', 'quota', 'exceeded', 'ascii codec', '1302', '速率限制', '请求频率', '您的账户已达到'],
        'server_error': ['500', '502', '503', '504', 'internal server error', 'bad gateway', 'service unavailable', 'gateway timeout'],
        'gateway_error': ['524', 'cloudflare', 'origin', 'upstream'],
        'not_found': ['404', 'not found', 'does not exist'],
        'model_error': ['model', 'invalid model', 'not available']
    }
    
    def __init__(self, config: Dict[str, Any]):
        """
        Args:
            config: globalConfig dict with 'error_prevention' settings
        """
        self.config = config
        self.settings = config.get('error_prevention', {})
        
        # Initialize state if missing
        if 'error_prevention_state' not in config:
            config['error_prevention_state'] = {}
        
        self.state = config['error_prevention_state']
    
    @property
    def enabled(self) -> bool:
        return self.settings.get('enabled', False)
    
    @property
    def threshold(self) -> int:
        return self.settings.get('consecutive_threshold', 3)
    
    @property
    def softban_minutes(self) -> int:
        return self.settings.get('softban_duration_minutes', 5)
    
    @property
    def longban_minutes(self) -> int:
        return self.settings.get('longban_duration_minutes', 60)
    
    @property
    def notifications_enabled(self) -> bool:
        return self.settings.get('notification_enabled', True)

    @property
    def disable_after_longban(self) -> bool:
        return self.settings.get('disable_after_longban', True)

    @property
    def rate_limit_cooldown_seconds(self) -> int:
        """Short immediate cooldown for rate-limit events (mirrors 9Router ~69s lock).

        When a rate_limit error is classified, the model is soft-banned for this
        duration on the FIRST occurrence — no need to wait for the consecutive
        threshold.  This prevents hammering an already-rate-limited upstream.
        """
        return int(self.settings.get('rate_limit_cooldown_seconds', 90))
    
    def classify_error(self, status_code: int, error_msg: Optional[str]) -> str:
        """Classify error into one of the standard types."""
        msg = (error_msg or '').lower()
        code_str = str(status_code)
        
        # Check message patterns
        for err_type, patterns in self.ERROR_TYPES.items():
            for pattern in patterns:
                if pattern in msg or pattern in code_str:
                    return err_type
        
        # Fallback based on status code ranges
        if 500 <= status_code < 600:
            return 'server_error'
        if status_code in [401, 403]:
            return 'auth'
        if status_code == 429:
            return 'rate_limit'
        if status_code == 404:
            return 'not_found'
        
        return 'unknown'
    
    def get_state_key(self, provider: str, model: str, error_type: str) -> str:
        """Generate unique key for this error streak."""
        return f"{provider}/{model}/{error_type}"
    
    def is_banned(self, provider: str, model: str) -> Tuple[bool, Optional[str], Optional[float]]:
        """
        Check if a model is currently banned.
        
        Returns:
            (is_banned, ban_type, seconds_remaining)
        """
        if not self.enabled:
            return False, None, None
        
        now = time.time()
        
        # Check all error types for this provider/model
        for key, entry in self.state.items():
            if not key.startswith(f"{provider}/{model}/"):
                continue
            
            ban_state = entry.get('ban_state')
            if not ban_state:
                continue
            
            # Disabled is permanent
            if ban_state == 'disabled':
                return True, 'disabled', None
            
            # Check if timed ban expired
            ban_until = entry.get('ban_until', 0)
            if now < ban_until:
                return True, ban_state, ban_until - now
            else:
                # Ban expired, clear it
                entry['ban_state'] = None
                entry['ban_until'] = None
        
        return False, None, None
    
    def record_success(self, provider: str, model: str):
        """Clear all error streaks for this model after a successful request."""
        if not self.enabled:
            return
        
        # Clear all error type streaks for this model
        keys_to_clear = [k for k in self.state.keys() if k.startswith(f"{provider}/{model}/")]
        for key in keys_to_clear:
            self.state[key]['streak'] = 0
    
    def record_error(self, provider: str, model: str, status_code: int, error_msg: Optional[str]) -> Optional[Dict[str, Any]]:
        """
        Record an error and apply progressive ban if threshold reached.
        
        Returns:
            Action dict if ban was applied, else None.
            {
                'action': 'softban' | 'longban' | 'disabled',
                'model': str,
                'provider': str,
                'error_type': str,
                'duration_minutes': int | None,
                'notify': bool
            }
        """
        if not self.enabled:
            return None
        
        error_type = self.classify_error(status_code, error_msg)
        key = self.get_state_key(provider, model, error_type)
        now = time.time()

        # Client payload failures and client disconnects do not describe leaf
        # health. Ignore them before creating or mutating any streak state so
        # repeated identical requests cannot escalate a healthy model.
        if status_code in (400, 422, 499):
            return None
        
        # Initialize or update state
        if key not in self.state:
            self.state[key] = {
                'streak': 0,
                'last_error_time': 0,
                'ban_state': None,
                'ban_until': None,
                'ban_escalation_count': 0,
                'error_type': error_type,
                'provider': provider,
                'model': model
            }
        
        entry = self.state[key]
        entry['streak'] += 1
        entry['last_error_time'] = now

        # ── Immediate softban for rate_limit (9Router parity) ────────
        # Rate-limit / quota errors should not wait for N consecutive failures.
        # Every 429/1302/速率限制 triggers an immediate short cooldown so the
        # combo fallback path skips this model on the next request, just like
        # 9Router's modelLock mechanism (~69s).
        if error_type == 'rate_limit':
            entry['ban_state'] = 'softban'
            entry['ban_until'] = now + self.rate_limit_cooldown_seconds
            entry['ban_escalation_count'] = max(entry.get('ban_escalation_count', 0), 1)
            entry['streak'] = 0  # Reset after applying cooldown
            print(
                f"[ErrorPrevention] IMMEDIATE rate-limit softban: "
                f"{provider}/{model} for {self.rate_limit_cooldown_seconds}s "
                f"(pattern matched in '{error_type}')",
                flush=True,
            )
            return {
                'action': 'softban',
                'model': model,
                'provider': provider,
                'error_type': error_type,
                'duration_minutes': None,  # seconds-based, not minutes
                'duration_seconds': self.rate_limit_cooldown_seconds,
                'notify': False,
                'ephemeral': True,   # Part 2b: route to sidecar, not config.yaml
            }

        # ── Immediate softban for auth (401/403) ─────────────────────
        # Auth / IP-whitelist / no-access errors are DETERMINISTIC: the same
        # provider will reject every retry within the session, so waiting for N
        # consecutive strikes just produces a wall of identical errors (observed
        # with pix4k ip_not_allowed + hcnsec-vip 403). Apply an immediate short
        # cooldown so the fallback chain skips this model right away. The cooldown
        # is deliberately SHORT (not a longban) so a transiently-broken upstream
        # node — e.g. one un-whitelisted egress IP in an aggregator pool that
        # later recovers — is skipped briefly, not killed permanently.
        if error_type == 'auth':
            _auth_cooldown = int(self.settings.get('auth_cooldown_seconds', self.rate_limit_cooldown_seconds))
            entry['ban_state'] = 'softban'
            entry['ban_until'] = now + _auth_cooldown
            entry['ban_escalation_count'] = max(entry.get('ban_escalation_count', 0), 1)
            entry['streak'] = 0  # Reset after applying cooldown
            print(
                f"[ErrorPrevention] IMMEDIATE auth softban: "
                f"{provider}/{model} for {_auth_cooldown}s "
                f"(status={status_code})",
                flush=True,
            )
            return {
                'action': 'softban',
                'model': model,
                'provider': provider,
                'error_type': error_type,
                'duration_minutes': None,  # seconds-based, not minutes
                'duration_seconds': _auth_cooldown,
                'notify': False,
                'ephemeral': True,   # Part 2b: route to sidecar, not config.yaml
            }

        # ── Immediate cooldown for ALL leaf-health errors (9router zero-strike parity) ──
        # C1: 9router (module 12557 e()) locks a leaf on the FIRST error of any
        # class — every return path is shouldFallback:true. C2: an unmatched
        # error still gets a 30s default lock (9router wf=30000). BSL now mirrors
        # both so a dead leaf is benched on strike 1, not strike 3. In a combo
        # chain the per-leaf streak resets each request, so without this these
        # classes often NEVER reached the 3-strike threshold and were re-selected
        # on every request (the dead-leaf retry loop this PR eliminates).
        #
        # EXCLUSIONS (BSL is deliberately MORE correct than 9router, which DOES
        # lock 400s):
        #   400/422 — client payload error. Fails identically on every leaf, so
        #             banning the leaf is wrong; main.py already short-circuits
        #             these without cascading.
        #   499     — client disconnect. The USER aborted, not the leaf; banning a
        #             healthy leaf for a user cancel would poison the chain.
        if status_code not in (400, 422, 499):
            _cooldown_by_type = {
                'timeout':       self.settings.get('dead_leaf_cooldown_seconds', 90),
                'server_error':  self.settings.get('dead_leaf_cooldown_seconds', 90),
                'gateway_error': self.settings.get('dead_leaf_cooldown_seconds', 90),
                'not_found':     self.settings.get('not_found_cooldown_seconds', 120),
                'model_error':   self.settings.get('not_found_cooldown_seconds', 120),
                'unknown':       self.settings.get('default_cooldown_seconds', 30),   # C2: 9router wf
            }
            if error_type in _cooldown_by_type:
                _cooldown = int(_cooldown_by_type[error_type])
                entry['ban_state'] = 'softban'
                entry['ban_until'] = now + _cooldown
                # Vestigial for flat cooldowns (timeout/unknown never read the
                # escalation ladder) — kept consistent with rate_limit/auth above.
                entry['ban_escalation_count'] = max(entry.get('ban_escalation_count', 0), 1)
                entry['streak'] = 0
                print(
                    f"[ErrorPrevention] IMMEDIATE {error_type} softban: "
                    f"{provider}/{model} for {_cooldown}s (status={status_code})",
                    flush=True,
                )
                return {
                    'action': 'softban',
                    'model': model,
                    'provider': provider,
                    'error_type': error_type,
                    'duration_minutes': None,
                    'duration_seconds': _cooldown,
                    'notify': False,
                    'ephemeral': True,   # Part 2b: route to sidecar, not config.yaml
                }

        # Check if threshold reached
        if entry['streak'] >= self.threshold:
            escalation = entry['ban_escalation_count']
            
            if escalation == 0:
                # First strike: softban
                entry['ban_state'] = 'softban'
                entry['ban_until'] = now + (self.softban_minutes * 60)
                entry['ban_escalation_count'] = 1
                entry['streak'] = 0  # Reset streak after applying ban
                
                return {
                    'action': 'softban',
                    'model': model,
                    'provider': provider,
                    'error_type': error_type,
                    'duration_minutes': self.softban_minutes,
                    'notify': False
                }
            
            elif escalation == 1:
                # Second strike: longban
                entry['ban_state'] = 'longban'
                entry['ban_until'] = now + (self.longban_minutes * 60)
                entry['ban_escalation_count'] = 2
                entry['streak'] = 0
                
                return {
                    'action': 'longban',
                    'model': model,
                    'provider': provider,
                    'error_type': error_type,
                    'duration_minutes': self.longban_minutes,
                    'notify': False
                }
            
            else:
                # Third strike after longban: disable permanently by default.
                # If disabled in settings, keep routing enabled but emit a critical warning.
                entry['streak'] = 0

                if self.disable_after_longban:
                    entry['ban_state'] = 'disabled'
                    entry['ban_until'] = None
                    action = 'disabled'
                else:
                    entry['ban_state'] = None
                    entry['ban_until'] = None
                    action = 'critical_warning'
                
                return {
                    'action': action,
                    'model': model,
                    'provider': provider,
                    'error_type': error_type,
                    'duration_minutes': None,
                    'notify': self.notifications_enabled
                }
        
        return None
    
    def clear_all_bans(self):
        """Clear all softban/longban states (does NOT re-enable disabled models)."""
        for entry in self.state.values():
            if entry.get('ban_state') in ['softban', 'longban']:
                entry['ban_state'] = None
                entry['ban_until'] = None
                entry['streak'] = 0

    def clear_temp_bans_with_count(self) -> int:
        """
        Clear softban/longban states and return the number of entries cleared.
        Does NOT touch 'disabled' state or provider connections.
        Preserves existing public clear_all_bans() behavior.
        """
        count = 0
        for entry in self.state.values():
            if entry.get('ban_state') in ['softban', 'longban']:
                entry['ban_state'] = None
                entry['ban_until'] = None
                entry['streak'] = 0
                count += 1
        return count

    def manually_enable_model(self, provider: str, model: str):
        """Re-enable a disabled model: clear all its ban state + flip config enabled flag."""
        # Clear every error-streak entry for this provider/model
        for key in [k for k in self.state.keys() if k.startswith(f"{provider}/{model}/")]:
            self.state[key]['ban_state'] = None
            self.state[key]['ban_until'] = None
            self.state[key]['streak'] = 0
            self.state[key]['ban_escalation_count'] = 0
        # Flip the model back on in the config copy we were handed.
        prov_data = self.config.get('providers', {}).get(provider, {})
        for m in prov_data.get('models', []):
            if m.get('id') == model:
                m['enabled'] = True
        # P0 fix: commit the enable to the LIVE master. self.config.providers is a
        # deep-copied clone (decrypt-on-read), so without this the flip never reaches
        # the running router — it only would have hit disk on a later persist, i.e.
        # after a restart. Route through the single sanctioned swap path.
        try:
            from app import main as _main
            _main._replace_runtime_config(self.config)
        except Exception as e:
            print(f"[ErrorPrevention] live config swap failed (enable stays disk-pending): {e}", flush=True)
        return True

    
    def get_active_bans(self) -> list:
        """Get list of all currently banned models with details."""
        now = time.time()
        bans = []
        
        for key, entry in self.state.items():
            ban_state = entry.get('ban_state')
            if not ban_state:
                continue
            
            # Skip expired timed bans
            if ban_state in ['softban', 'longban']:
                ban_until = entry.get('ban_until', 0)
                if now >= ban_until:
                    continue
                remaining = ban_until - now
            else:
                remaining = None
            
            bans.append({
                'provider': entry['provider'],
                'model': entry['model'],
                'error_type': entry['error_type'],
                'ban_state': ban_state,
                'remaining_seconds': remaining,
                'last_error_time': entry['last_error_time']
            })
        
        return bans


# ─── Module-level dashboard notification store ──────────────────────────────
# In-memory list of notifications surfaced to the admin dashboard banner.
# Each: {id, level, title, message, timestamp, dismissed, push}
notifications = []
_notif_counter = 0


def add_notification(level: str, title: str, message: str, push: bool = False) -> dict:
    """Push a notification to the dashboard banner store. level: info|warning|critical."""
    global _notif_counter
    _notif_counter += 1
    notif = {
        'id': _notif_counter,
        'level': level,
        'title': title,
        'message': message,
        'timestamp': time.time(),
        'dismissed': False,
        'push': push,
    }
    notifications.append(notif)
    # Bound the store
    if len(notifications) > 200:
        notifications.pop(0)
    return notif


def _handle_action(action: Optional[Dict[str, Any]], config: Dict[str, Any]):
    """Translate a ban action into a dashboard notification + disable-on-config side effect."""
    if not action:
        return
    model = action['model']
    provider = action['provider']
    etype = action['error_type']
    act = action['action']

    if act == 'softban':
        dur = action.get('duration_minutes')
        if dur is None:
            secs = action.get('duration_seconds', 0)
            dur_text = f"{secs}s" if secs < 60 else f"{secs / 60:.0f} min"
        else:
            dur_text = f"{dur} min"
        add_notification(
            'warning',
            f"Model soft-banned: {model}",
            f"{model} ({provider}) hit repeated {etype} errors. Soft-banned for "
            f"{dur_text}. Requests will skip this model until the ban lifts.",
        )
    elif act == 'longban':
        dur = action.get('duration_minutes')
        if dur is None:
            secs = action.get('duration_seconds', 0)
            dur_text = f"{secs}s" if secs < 60 else f"{secs / 60:.0f} min"
        else:
            dur_text = f"{dur} min"
        add_notification(
            'warning',
            f"Model long-banned: {model}",
            f"{model} ({provider}) kept failing with {etype} after a soft-ban. "
            f"Long-banned for {dur_text}.",
        )
    elif act == 'disabled':
        # Disable the model only in the selected provider's live config.
        prov_data = config.get('providers', {}).get(provider, {})
        for m in prov_data.get('models', []):
            if m.get('id') == model:
                m['enabled'] = False
        # P0 fix: config.providers is a deep-copied clone (decrypt-on-read), so the
        # enabled=False above never reached the live master — the model stayed
        # routable until a restart. Commit it through the single sanctioned swap
        # path (persist -> replace_config -> reconfigure breaker). Fail-open: a
        # swap error must not break error reporting.
        try:
            from app import main as _main
            _main._replace_runtime_config(config)
        except Exception as e:
            print(f"[ErrorPrevention] live config swap failed (disable stays disk-pending): {e}", flush=True)
        add_notification(
            'critical',
            f"Model auto-disabled: {model}",
            f"{model} ({provider}) failed with {etype} through soft-ban and long-ban. "
            f"It has been DISABLED. Re-enable it manually once the underlying issue is fixed.",
            push=action.get('notify', False),
        )
    elif act == 'critical_warning':
        add_notification(
            'critical',
            f"Model still failing after long-ban: {model}",
            f"{model} ({provider}) failed again with {etype} after the long-ban expired. "
            f"Auto-disable is turned off, so the model remains enabled.",
            push=action.get('notify', False),
        )


def check_ban(config: Dict[str, Any], provider: str, model: str) -> Tuple[bool, Optional[str], Optional[float]]:
    """Module-level convenience: is (provider, model) currently banned?"""
    mgr = ErrorPreventionManager(config)
    return mgr.is_banned(provider, model)


# ─── Ephemeral ban persistence sidecar (Part 2b / C4) ───────────────────────
# record_outcome() previously rewrote ALL of config.yaml on every ban. With the
# 9router zero-strike policy, ephemeral cooldowns (30–120s) fire far more often,
# so clobbering a 3000+ line config file per-ban is slow and risks corrupting
# the live config under concurrency. Instead:
#   • Ephemeral cooldowns (rate_limit/auth/timeout/unknown/…) → this JSON sidecar.
#   • Escalated longban/disabled → still written to config.yaml (rare, policy).
# On startup load_runtime_bans() merges still-live entries back into state and
# self-prunes expired ones.
_AEP_SIDECAR_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    ".brain", "state", "aep_runtime.json",
)


def _write_ephemeral_ban(state_key: str, entry: Dict[str, Any]) -> None:
    """Persist a single ephemeral ban entry to the sidecar (merge, not clobber)."""
    try:
        os.makedirs(os.path.dirname(_AEP_SIDECAR_PATH), exist_ok=True)
        existing = {}
        if os.path.exists(_AEP_SIDECAR_PATH):
            try:
                with open(_AEP_SIDECAR_PATH, "r", encoding="utf-8") as f:
                    existing = json.load(f) or {}
            except Exception:
                existing = {}
        existing[state_key] = entry
        with open(_AEP_SIDECAR_PATH, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=True, separators=(",", ":"))
    except Exception as e:
        print(f"[ErrorPrevention] Failed to persist ephemeral ban to sidecar: {e}", flush=True)


def load_runtime_bans(config: Dict[str, Any]) -> int:
    """Load persisted ephemeral bans from the sidecar into config state.

    Called once at startup (after load_config/init_breaker). Entries whose
    ban_until has already passed are dropped. Returns the number of live
    entries merged. Safe to call when the sidecar is absent.
    """
    if not os.path.exists(_AEP_SIDECAR_PATH):
        return 0
    try:
        with open(_AEP_SIDECAR_PATH, "r", encoding="utf-8") as f:
            saved = json.load(f) or {}
    except Exception as e:
        print(f"[ErrorPrevention] Could not read sidecar: {e}", flush=True)
        return 0

    now = time.time()
    state = config.setdefault("error_prevention_state", {})
    surviving = {}
    for key, entry in saved.items():
        if not isinstance(entry, dict):
            continue
        ban_until = entry.get("ban_until")
        # Keep only still-active timed cooldowns; drop anything already expired.
        if ban_until and ban_until > now:
            state[key] = entry
            surviving[key] = entry
    # Rewrite the sidecar with only the survivors (self-pruning).
    try:
        os.makedirs(os.path.dirname(_AEP_SIDECAR_PATH), exist_ok=True)
        with open(_AEP_SIDECAR_PATH, "w", encoding="utf-8") as f:
            json.dump(surviving, f, ensure_ascii=True, separators=(",", ":"))
    except Exception:
        pass
    if surviving:
        print(f"[ErrorPrevention] Restored {len(surviving)} live ephemeral ban(s) from sidecar.", flush=True)
    return len(surviving)


def record_outcome(
    config: Dict[str, Any],
    provider: str,
    model: str,
    status_code: int,
    error_msg: Optional[str],
    out_tokens: int = 0,
):
    """
    Module-level convenience called from obs.log_request.
    On success: clears streaks. On error: increments streak, may apply a ban + notify.

    out_tokens > 0 means the upstream actually delivered output before any tail
    error (e.g. a slow stream whose final read timed out). Such a request DID
    succeed from the user's perspective, so it must NOT count toward a ban —
    otherwise a healthy-but-slow channel gets softbanned out of rotation.
    """
    mgr = ErrorPreventionManager(config)
    if not mgr.enabled:
        return
    # Treat any token-producing request as a success for ban accounting, even if
    # a tail read-timeout attached an error_msg while status stayed 200.
    if (status_code == 200 and not error_msg) or (out_tokens and out_tokens > 0):
        mgr.record_success(provider, model)
        return
    action = mgr.record_error(provider, model, status_code, error_msg)
    _handle_action(action, config)
    
    # Persist ban state to survive server restarts.
    # Part 2b: ephemeral cooldowns go to the small JSON sidecar; only escalated
    # policy bans (longban/disabled/critical_warning) warrant a config.yaml write.
    if action:
        if action.get('ephemeral'):
            state_key = mgr.get_state_key(provider, model, action.get('error_type', 'unknown'))
            entry = mgr.state.get(state_key)
            if entry is not None:
                _write_ephemeral_ban(state_key, entry)
        else:
            try:
                _persist_config_yaml(config)
            except Exception as e:
                print(f"[ErrorPrevention] Failed to persist state to config.yaml: {e}")
