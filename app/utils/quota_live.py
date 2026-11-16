"""Cache-first quota snapshots with account-scoped background singleflight."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time
from typing import Any


def account_key(conn: dict, index: int = 0) -> str:
    """Use configured identity; digest credentials only for anonymous connections."""
    pd = conn.get("provider_data") or {}
    principal = (conn.get("id") or pd.get("account_id") or
                 pd.get("chatgptAccountId") or conn.get("email") or pd.get("email"))
    credential = conn.get("refresh_token") or (
        None if principal else conn.get("api_key") or conn.get("access_token")
    )
    identity = [principal, credential, index if not principal and not credential else None]
    return hashlib.sha256(json.dumps(identity, sort_keys=True, default=str).encode()).hexdigest()


class QuotaCache:
    def __init__(self):
        self.rows: dict[tuple[str, str], dict] = {}
        self.tasks: dict[tuple[str, str], asyncio.Task] = {}
        self.attempts: dict[tuple[str, str], float] = {}
        self.failures: dict[tuple[str, str], int] = {}

    def read(self, provider: str, conn: dict, index: int, probe, blocked: str | None = None,
             force: bool = False) -> dict:
        key = (provider, account_key(conn, index))
        now = time.time()
        row = copy.deepcopy(self.rows.get(key) or {
            "remaining_pct": None, "source": "unavailable", "observed_at": None,
            "last_attempt_at": None, "stale": False,
        })
        observed = row.get("observed_at")
        row["stale"] = bool(observed and (row.get("error") or now - observed >= 15))
        if blocked:
            row.update(error=blocked, refreshing=False, stale=bool(observed))
            return row
        task = self.tasks.get(key)
        failures = self.failures.get(key, 0)
        interval = min(300, 30 * 2 ** min(failures - 1, 4)) if failures else (10 if force else 15)
        elapsed = now - self.attempts.get(key, 0)
        if force and (task is None or task.done()) and elapsed < interval:
            row["throttled"] = True
            row["next_refresh_in_s"] = round(max(0.0, interval - elapsed), 1)
        if (task is None or task.done()) and elapsed >= interval:
            self.attempts[key] = now

            async def refresh():
                try:
                    result = await probe()
                    result = copy.deepcopy(result)
                    result.setdefault("last_attempt_at", now)
                    if result.get("error"):
                        self.failures[key] = failures + 1
                        previous = self.rows.get(key)
                        if previous and previous.get("observed_at"):
                            merged = copy.deepcopy(previous)
                            for field in ("error", "status", "last_attempt_at", "credential_present", "token_expired", "field_names"):
                                if field in result:
                                    merged[field] = result[field]
                            merged["stale"] = True
                            merged["last_good_source"] = merged.get("source")
                            result = merged
                    else:
                        self.failures.pop(key, None)
                        previous = self.rows.get(key) or {}
                        if (previous.get("observed_at") or 0) > (result.get("observed_at") or 0):
                            return
                    self.rows[key] = result
                except asyncio.CancelledError:
                    raise
                except Exception:
                    result = copy.deepcopy(self.rows.get(key) or row)
                    result.update(error="probe_exception", stale=bool(result.get("observed_at")), last_attempt_at=now)
                    if result.get("observed_at"):
                        result["last_good_source"] = result.get("source")
                    result.pop("ambiguous", None)
                    self.rows[key] = result
                    self.failures[key] = failures + 1
                finally:
                    self.tasks.pop(key, None)

            self.tasks[key] = asyncio.create_task(refresh())
        active_task = self.tasks.get(key)
        row["refreshing"] = bool(active_task is not None and not active_task.done())
        return row

    async def close(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()
