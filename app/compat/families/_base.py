"""
BSL Router — Family Contract Base

A *contract generation* is the unit of model behavior, NOT a company.
Kimi K2 and Kimi K3 are the same company with incompatible parameter
contracts (K3 forbids temperature/top_p and requires reasoning_effort;
K2 needs a top-level enable_thinking boolean). Claude legacy/modern/next
are three contracts under one vendor. So the registry keys on contract,
and each family module may declare several.

Two properties this file exists to guarantee:

1. SINGLE WRITER. Exactly one component writes the thinking/reasoning
   fields listed in THINKING_PAYLOAD_KEYS. Previously main.py ran a
   220-line regex cascade that silently overwrote whatever
   reasoning_policy.py had already written, so a fix required checking
   both and knowing which won.

2. PROVENANCE. Every write records which contract and which rule
   produced it. A request log then names the file to edit, replacing
   "grep glm across the repo and read a 13-branch elif chain".

Ordering safety: each Contract owns its own `detect` predicate and an
explicit integer `priority`. Previously correctness depended on the
physical line order of an elif chain — `is_kimi` only beat
`is_chinese_m` because it sat 8 lines earlier in the file.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Pattern
import re


# Authoritative set of payload keys that reasoning resolution owns.
# Mirrors app/middleware/thinking_fallback.THINKING_PAYLOAD_KEYS — that
# module strips exactly these on a degrade-and-retry, so the writer must
# not exceed this set or a retry would leave orphaned fields behind.
THINKING_PAYLOAD_KEYS = (
    "thinking",           # Anthropic / DeepSeek / Chinese-model {type: ...}
    "reasoning",          # OpenRouter {effort, exclude}
    "reasoning_effort",   # OpenAI / DeepSeek scalar
    "output_config",      # Claude modern / DeepSeek effort container
    "thinking_config",    # legacy Gemini 2.5 (Anthropic-shaped, now stripped)
    "thinkingLevel",      # legacy Gemini 3.x (top-level, now nested)
    "enable_thinking",    # Kimi K2 / MiMo boolean
    "generationConfig",   # Gemini native: thinkingConfig container (2.5 + 3.x)
    "includeThoughts",    # Gemini 3.x multi-turn thought-signature flag
)

# Values that mean "the operator did not pick a thinking level".
OFF_VALUES = ("auto", "none", "off", "")


@dataclass
class ThinkingContext:
    """Everything a contract needs to resolve. Pure data, no I/O.

    `f_val` is the legacy match target "provider/model" lowercased, kept
    because every existing detection regex is written against it (some
    match on the provider segment, e.g. 'antigravity' in the Opus 4.6
    budget rule, so model_id alone is insufficient).
    """
    f_val: str
    model_id: str = ""
    provider_name: str = ""
    effort: str = "auto"
    reasoning_mode: Optional[str] = None
    reasoning_context: Optional[str] = None
    # Upstream transport: "openai" | "anthropic" | "gemini" | "openai-responses".
    # A logical setting like "think hard" has a DIFFERENT payload shape per
    # transport, and one model family can be served over several. Gemini is
    # the clearest case: the same model is reachable as native gemini, as an
    # OpenAI-compatible gateway, and as an anthropic-shaped endpoint, and
    # sending the native generationConfig over the other two is a 400.
    wire_format: str = "openai"

    @property
    def effort_is_explicit(self) -> bool:
        return str(self.effort or "").lower() not in OFF_VALUES


@dataclass
class ProvenanceRecord:
    """One attributed write. Rendered into the request log."""
    contract_id: str
    source: str          # "families/glm.py"
    rule: str            # which branch inside the contract fired
    fields: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "contract": self.contract_id,
            "set_by": self.source,
            "rule": self.rule,
            "fields": list(self.fields),
        }


class Provenance:
    """Collects attributed writes for one request.

    Wraps payload mutation so a field can never be written without an
    accompanying attribution.
    """

    def __init__(self) -> None:
        self.records: List[ProvenanceRecord] = []

    def apply(
        self,
        payload: Dict[str, Any],
        contract: "Contract",
        rule: str,
        values: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Write `values` onto payload, attributed to contract+rule.

        A None value means "remove this key" — expressing a forbidden
        field as an explicit, attributed deletion rather than a silent
        pop somewhere further down the request path.
        """
        touched: List[str] = []
        for key, val in values.items():
            if val is None:
                if key in payload:
                    payload.pop(key, None)
                    touched.append(f"-{key}")
            else:
                payload[key] = val
                touched.append(key)

        if touched:
            self.records.append(
                ProvenanceRecord(
                    contract_id=contract.id,
                    source=contract.source,
                    rule=rule,
                    fields=touched,
                )
            )
        return payload

    def as_list(self) -> List[Dict[str, Any]]:
        return [r.as_dict() for r in self.records]

    def summary(self) -> str:
        """Compact one-line form for console logs."""
        if not self.records:
            return "none"
        return "; ".join(
            f"{r.contract_id}:{r.rule}[{','.join(r.fields)}]" for r in self.records
        )


# A contract's apply function: (payload, ctx, provenance, contract) -> payload
ApplyFn = Callable[[Dict[str, Any], ThinkingContext, Provenance, "Contract"], Dict[str, Any]]


@dataclass
class Contract:
    """One model contract generation.

    Attributes:
        id:        Stable identifier surfaced in logs, e.g. "glm-5.2".
        source:    Owning file, surfaced in logs so a bug report names
                   the file to edit.
        priority:  Higher wins when several contracts match. Replaces
                   elif ordering with an explicit, greppable number.
        pattern:   Regex matched against ThinkingContext.f_val.
        exclude:   Regex that disqualifies a match (e.g. Kimi K3 must not
                   be captured by the K2 pattern).
        apply:     Writes thinking fields via Provenance.apply.
        sanitize:  Unconditional cleanup that runs even when thinking is
                   off — Kimi K3 and Qwen reject sampling params outright,
                   so those removals cannot be gated on effort.
        always_applies: When True, `apply` runs even for effort=auto/off.
                   GPT-5 needs this: explicit reasoning_mode/context
                   metadata must still be sent with no effort selected.
    """
    id: str
    source: str
    priority: int
    pattern: str
    exclude: Optional[str] = None
    apply: Optional[ApplyFn] = None
    sanitize: Optional[ApplyFn] = None
    always_applies: bool = False

    _compiled: Optional[Pattern] = field(default=None, init=False, repr=False)
    _compiled_exclude: Optional[Pattern] = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self._compiled = re.compile(self.pattern, re.IGNORECASE)
        if self.exclude:
            self._compiled_exclude = re.compile(self.exclude, re.IGNORECASE)

    def matches(self, ctx: ThinkingContext) -> bool:
        target = ctx.f_val or ""
        if not self._compiled.search(target):
            return False
        if self._compiled_exclude is not None and self._compiled_exclude.search(target):
            return False
        return True
