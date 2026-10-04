"""TTL cache for authenticity verdicts."""
import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class CacheEntry:
    verdict: "RealityVerdict"
    stored_at: float


class AuthenticityCache:
    def __init__(self, ttl_seconds: int = 1800):
        self._cache: dict[str, CacheEntry] = {}
        self._ttl = ttl_seconds

    def _key(self, endpoint_base: str, model_id: str) -> str:
        return f"{endpoint_base}::{model_id}"

    def get(self, endpoint_base: str, model_id: str) -> Optional["RealityVerdict"]:
        key = self._key(endpoint_base, model_id)
        entry = self._cache.get(key)
        if entry is None:
            return None
        if (time.time() - entry.stored_at) >= self._ttl:
            del self._cache[key]
            return None
        return entry.verdict

    def put(self, endpoint_base: str, model_id: str, verdict: "RealityVerdict") -> None:
        # Only cache stable outcomes. Transient/ambiguous verdicts
        # (unverifiable, error) are re-probed next time.
        if verdict.verdict in ("unverifiable", "error"):
            return
        self._cache[self._key(endpoint_base, model_id)] = CacheEntry(
            verdict=verdict, stored_at=time.time()
        )

    def invalidate(self, endpoint_base: str, model_id: str) -> None:
        self._cache.pop(self._key(endpoint_base, model_id), None)

    def invalidate_all(self) -> None:
        self._cache.clear()


# Module-level singleton
_default_cache: Optional[AuthenticityCache] = None


def get_cache(ttl_seconds: int = 1800) -> AuthenticityCache:
    global _default_cache
    if _default_cache is None or _default_cache._ttl != ttl_seconds:
        _default_cache = AuthenticityCache(ttl_seconds=ttl_seconds)
    return _default_cache


def reset_cache() -> None:
    """Reset the singleton cache (for tests)."""
    global _default_cache
    _default_cache = None


# Import at bottom to avoid circular import
from .scoring import RealityVerdict  # noqa: E402
