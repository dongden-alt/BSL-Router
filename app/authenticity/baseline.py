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

    def load_family_standards(self, standards_dir, family: str) -> dict[str, dict]:
        """Load full frozen standards (incl. noncoding judge profile) for a family.

        Keyed to match load_family: the filename without the family_ prefix,
        so the matcher can read baseline["noncoding"]["judge"] for the deep
        non-coding family discriminator.

        ONLY canonical standards (source_tag empty) enter the match pool.
        Provider-tagged files (e.g. claude_vsllm-o-claude-opus-4-8_...) are
        reference snapshots, not canonical standards — they must not compete
        with the canonical key for the same model.
        """
        out: dict[str, dict] = {}
        sdir = Path(standards_dir)
        if not sdir.exists():
            return out
        # Pass 1: reference standards (*_reference.json, source_tag == "reference"),
        # keyed by sub-model without the trailing "_reference" suffix so variants
        # harvested with --tag reference (e.g. qwen3.7-flash, qwen3.8-flash,
        # glm-5.3-flash, kimi-k2.7-code, gpt-5.6-luna) enter the match pool.
        for path in sorted(sdir.glob(f"{family}_*_reference.json")):
            key = path.stem[len(family) + 1:]
            if key.endswith("_reference"):
                key = key[:-len("_reference")]
            try:
                std = json.loads(path.read_text(encoding="utf-8"))
                tag = (std.get("meta", {}).get("source_tag") or "").strip()
                if tag in ("", "reference"):
                    out[key] = std
            except Exception:
                continue
        # Pass 2: canonical standards (source_tag empty) take precedence.
        for path in sorted(sdir.glob(f"{family}_*.json")):
            key = path.stem[len(family) + 1:]  # strip "family_"
            try:
                std = json.loads(path.read_text(encoding="utf-8"))
                if (std.get("meta", {}).get("source_tag") or "").strip():
                    continue  # provider-tagged snapshot, not canonical
                out[key] = std
            except Exception:
                continue
        return out

    def is_stale(self, model_id: str, max_age_days: int = 30) -> bool:
        """Check if a baseline is stale (loaded more than max_age_days ago)."""
        if model_id not in self._loaded_at:
            return True
        age_days = (time.time() - self._loaded_at[model_id]) / 86400
        return age_days >= max_age_days

    def list_models(self) -> list[str]:
        """List all models with baselines."""
        return [p.stem.replace("_baseline", "") for p in self._dir.glob("*_baseline.json")]
