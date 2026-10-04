"""Universal model authenticity verification module.

Verifies that an upstream provider actually serves the model it claims
by probing the endpoint and comparing behavioral fingerprints against
pre-built baselines.

Public API:
    authenticity_check() — main entry point (fail-open)
    AuthenticityVerdict — verdict dataclass
    AuthenticityCache — TTL cache with anomaly invalidation
    BaselineStore — baseline loading and freshness tracking
"""

from .gate import authenticity_check
from .scoring import (
    RealityVerdict,
    Modification,
    compute_reality,
    confidence_level,
    CONFIDENCE_LEVELS,
)
from .cache import AuthenticityCache
from .baseline import BaselineStore
from .traitmark import TraitMark, LAYER_WEIGHTS
from .scope import in_scope, detect_family, TRUSTED_ANCHORS, SCOPE

__all__ = [
    "authenticity_check",
    "RealityVerdict",
    "Modification",
    "compute_reality",
    "confidence_level",
    "CONFIDENCE_LEVELS",
    "AuthenticityCache",
    "BaselineStore",
    "TraitMark",
    "LAYER_WEIGHTS",
    "in_scope",
    "detect_family",
    "TRUSTED_ANCHORS",
    "SCOPE",
]
