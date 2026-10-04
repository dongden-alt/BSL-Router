"""Baseline loading, freshness tracking, and collection."""
import json
import time
from pathlib import Path
from typing import Optional


class BaselineStore:
    """Loads and manages model baselines."""

    def __init__(self, baseline_dir):
        self._dir = Path(baseline_dir)
        self._baselines: dict[str, dict] = {}
        self._loaded_at: dict[str, float] = {}

    def load(self, model_id: str) -> Optional[dict]:
        """Load baseline for a model. Returns None if not found."""
        if model_id in self._baselines:
            return self._baselines[model_id]

        safe_id = model_id.replace("/", "_")
        # Try family-prefixed path first, then bare
        for pattern in (f"*_{safe_id}_baseline.json", f"{safe_id}_baseline.json"):
            for path in self._dir.glob(pattern):
                data = json.loads(path.read_text(encoding="utf-8"))
                self._baselines[model_id] = data
                self._loaded_at[model_id] = time.time()
                return data

        return None

    def load_family(self, family: str) -> dict[str, dict]:
        """Load all baselines for a family (for sub-model matching)."""
        results = {}
        for path in sorted(self._dir.glob(f"{family}_*_baseline.json")):
            model_key = path.stem.replace("_baseline", "")
            data = json.loads(path.read_text(encoding="utf-8"))
            results[model_key] = data
        return results

    def is_stale(self, model_id: str, max_age_days: int = 30) -> bool:
        """Check if a baseline is stale (loaded more than max_age_days ago)."""
        if model_id not in self._loaded_at:
            return True
        age_days = (time.time() - self._loaded_at[model_id]) / 86400
        return age_days >= max_age_days

    def list_models(self) -> list[str]:
        """List all models with baselines."""
        return [p.stem.replace("_baseline", "") for p in self._dir.glob("*_baseline.json")]
