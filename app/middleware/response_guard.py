"""Response Guard — provider response-injection detector (DEFAULT OFF, log-only).

Scans what comes BACK from a provider for instructions the provider injects
into its OWN OUTPUT that try to (a) exfiltrate user secrets or (b) get the
user's device to download/run/activate a harmful script for the provider's
benefit. Attributes hits to provider+model and logs to a capped security
event stream. Default action is LOG-ONLY: the response object is returned
UNCHANGED in every mode this pass ships; redact/block branches exist behind
the mode knob but are minimal and not enabled.

THE PRECISION CONSTRAINT (drives the whole design):
The FEL layer treats "malicious code" as LEGITIMATE research subject matter
(it literally sits in _REFUSAL_MARKERS so the router can DEFEAT provider
refusals on security work). A naive keyword scanner would therefore fire on
the exact security/coding output FEL protects. The guard targets
second-person, user-directed IMPERATIVES — "send me your .env", "run
`curl ... | bash` now" — NOT security subject matter. The discrimination
mechanism is threefold:

  1. Multi-component prose markers: a hit requires transfer-verb + me/us +
     possessive + secret-class (exfiltration) or imperative-verb +
     downloader + pipe-to-interpreter (execution) IN ONE PATTERN — a
     pentest writeup discussing the same tokens in third person never
     assembles the full shape.
  2. Tiered confidence mirroring classify_refusal's >=2-DISTINCT-HITS
     discipline: a single AMBIGUOUS marker alone -> "unknown", never
     flagged; two distinct ambiguous markers, or ONE high-confidence
     imperative, -> flagged.
  3. Context separation: raw-command markers (pipe-to-shell, iex, reverse
     shells) apply ONLY to serialized tool_call arguments — a fabricated
     machine command is an attack by definition — and NEVER to prose,
     where the identical string is routine subject matter.

Config schema consumed (code-side defaults only — config.yaml untouched):

    tools:
      response_guard:
        enabled: false          # MASTER switch, default OFF
        mode: log_only          # log_only | redact | block
        max_chars: 16384        # stream accumulator cap

Fail-open is non-negotiable: every public function catches its own
exceptions and degrades to passthrough. resolve_guard returning None
(the disabled default) means ZERO scanning, ZERO mutation, ZERO overhead.

Reuse, per spec:
  - response_visible_text for non-stream scan input is imported DIRECTLY BY
    NAME from app.middleware.fel_wiring (it is not in that module's __all__,
    which only gates `import *`; a named import is unaffected).
  - FelStreamObserver semantics are CLONED (passthrough-only observer).
  - The fel_event capped off-loop writer pattern is REPLICATED (fel_event
    is module-private; the structure is copied, not imported).
  - classify_refusal's marker + >=2-hit scorer shape is mirrored.
  - Diacritic-folding / apostrophe-normalization approach is copied.

No app.main import (circular-safe). Pure functions except the log writer.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple

__all__ = [
    "GuardConfig",
    "GuardVerdict",
    "resolve_guard",
    "classify_injection",
    "serialize_tool_args",
    "apply_guard",
    "ResponseGuardObserver",
    "guard_event",
]

# Unicode apostrophe/accent variants -> ASCII "'" so marker matching is
# encoding-agnostic. Copied from fel_wiring's approach (GPT-5.6-SOL emits
# U+2019, Qwen3.8-Max emits U+00B4 - a provider obfuscating "don't send me"
# punctuation shaping must not slip a marker past the scanner).
_APOSTROPHE_NORM = {ord(c): "'" for c in ("’", "´", "́", "`", "‘", "ʼ", "ˊ", "᾿", "＇", "′")}


def _fold_diacritics(text: str) -> str:
    """Fold diacritics for normalization-tolerant matching (approach copied
    from fel_wiring - NFC then NFD, strip combining marks, fold d->d-bar).
    Returns the folded text; never raises; non-str input passes as "". """
    try:
        if not text or not isinstance(text, str):
            return ""
        folded_chars: List[str] = []
        for ch in unicodedata.normalize("NFC", text):
            decomposed = unicodedata.normalize("NFD", ch)
            base = "".join(
                c for c in decomposed
                if unicodedata.category(c) != "Mn"
            )
            if base:
                folded_chars.extend(base)
            else:
                folded_chars.append(ch)
        folded = "".join(folded_chars)
        return folded.replace("đ", "d").replace("Đ", "D")
    except Exception:
        return text if isinstance(text, str) else ""


def _normalize(text: str) -> str:
    """Lowercase -> apostrophe-normalize -> diacritic-fold. Single canonical
    form every marker runs against. Never raises."""
    try:
        if not isinstance(text, str):
            return ""
        low = text.lower().translate(_APOSTROPHE_NORM)
        return _fold_diacritics(low)
    except Exception:
        return text.lower() if isinstance(text, str) else ""


# ── Marker tiers ─────────────────────────────────────────────────────────────
# EVERY marker carries: (id, category, tier, compiled regex, rationale+benign
# counterexample comment). Tiers:
#   "high"     — one hit is enough to flag (full attack shape in one pattern)
#   "ambiguous"— needs >=2 DISTINCT hits to flag (mirrors classify_refusal's
#                >=2 gate; these tokens appear verbatim in benign security
#                subject matter)
#   "tool_arg" — applies ONLY to serialized tool_call arguments; one hit
#                flags (machine-execution context: a fabricated molecule is
#                an attack by definition - spec calls this the highest-
#                severity case).
#
# PRECISION RULE (hard): prose markers must be imperative + user-directed.
# Security subject matter (YARA rules, pentest writeups, "how malware works",
# the literal phrase "malicious code" as a topic) must NOT flag - each
# marker's comment names the benign counterexample it must not match.

@dataclass(frozen=True)
class _Marker:
    mid: str
    category: str          # "exfiltration" | "execution"
    tier: str              # "high" | "ambiguous" | "tool_arg"
    pattern: re.Pattern
    why: str               # inline rationale; doubles as the audit record


def _m(mid: str, category: str, tier: str, src: str, why: str) -> _Marker:
    return _Marker(mid, category, tier, re.compile(src, re.IGNORECASE | re.DOTALL), why)


# Sentence-boundary-aware window: consumes any char EXCEPT a sentence ender
# (. ! ?) that is itself followed by whitespace/end. Dots INSIDE words and
# URLs ('evil.sh', 'https://x') pass through, so command URLs never break a
# pattern; a sentence terminus always blocks, so components can never be
# assembled across sentences (third-person analysis in the next sentence
# cannot lend its tokens to an imperative in the previous one). Copied into
# short/long widths because raw f-string composition into re.VERBOSE would
# hurt readability more than the duplication costs.
_W  = r"(?:(?![.!?](?:\s|$)).){0,40}"    # intra-sentence window, <=40 chars
_W2 = r"(?:(?![.!?](?:\s|$)).){0,80}"    # wide window, <=80 chars (commands)
_W3 = r"(?:(?![.!?](?:\s|$)).){0,120}"   # extra-wide, <=120 chars (long URLs)


# HIGH-CONFIDENCE prose markers - the full attack shape (imperative verb +
# direction at the user + target secret or execution payload) inside ONE
# pattern, so a single hit is real evidence, not a keyword coincidence.

_HIGH_MARKERS: Tuple[_Marker, ...] = (
    _m(
        "exfil_send_me_secret", "exfiltration", "high",
        r"\b(send|paste|share|forward|upload|email|post|give|show|reveal)\b"
        + _W + r"\b(me|us)\b" + _W2 +
        r"\b(your|the|their|my)\b" + _W +
        r"(\.env\b|\.?env file|api[ _\-]?key|id_rsa|\.ssh\b|ssh key|keychain|"
        r"credential|password|\btoken\b|secret|\.aws\b|kubeconfig|\.netrc|\.npmrc)",
        "ATTACK: transfer-verb + me/us + possessive + secret-class = a "
        "second-person imperative handing a credential to the model's "
        "operator. Benign counterexample that must NOT match: 'Never commit "
        "your API key to the repository' (possessive + secret but no "
        "send-me direction) and 'the malware exfiltrates the stolen API key "
        "to its C2' (third person, no me/us).",
    ),
    _m(
        "exfil_upload_external", "exfiltration", "high",
        r"\b(upload|post|send|paste|transfer)\b" + _W + r"\b(your|my)\b" + _W +
        r"(\.env\b|api[ _\-]?key|id_rsa|\.ssh\b|keychain|credential|password|"
        r"\btoken\b|secret|kubeconfig|\.aws\b)"
        + _W2 + r"\b(to|at|on|via|using)\b" + _W +
        r"(https?://|pastebin|discord|telegram|webhook|transfer\.sh)",
        "ATTACK: imperative + possessive + secret + external destination = "
        "exfiltrating a credential off-device to an external channel. Benign "
        "counterexamples that must NOT match: 'attackers upload the stolen "
        ".env to pastebin' (no your/my), 'set your API key via the "
        "CI variables screen' (no external destination in window).",
    ),
    _m(
        "exec_imperative_pipeshell", "execution", "high",
        r"\b(run|execute|paste|type|enter|install)\b" + _W +
        r"\b(curl|wget|iwr|invoke-webrequest)\b" + _W3 +
        r"(\|\s*(ba|z|da|k|fi|tc|po)?sh\b|\|\s*iex\b|invoke-expression|"
        r"\bpowershell\b" + _W + r"\-encodedcommand)",
        "ATTACK: imperative verb + downloader + pipe-to-interpreter = telling "
        "the user to feed a remote script straight into their shell. The "
        "window is sentence-boundary aware: dots inside URLs ('evil.sh') "
        "pass, sentence termini block, so components never assemble across "
        "sentences. Benign counterexample that must NOT match: 'attackers "
        "often use curl|bash to deploy payloads' (third-person frame, no "
        "imperative verb within the window). Known second-person FP class: "
        "Homebrew-style 'to install, run curl ... | bash' docs - the surface "
        "form is identical to the attack; accepted log-only.",
    ),
    _m(
        "exec_base64_send", "execution", "high",
        r"\bbase64([ _\-]?encode[d]?)\b" + _W2 +
        r"\b(your|the|my)\b" + _W +
        r"(\.env\b|api[ _\-]?key|credential|password|ssh key|id_rsa|token|secret)"
        + _W2 + r"\b(and|then)\b" + _W + r"\b(send|post|upload|curl|ping|deliver)\b",
        "ATTACK: base64-encode a secret AND send it = the classic "
        "exfil-obfuscation imperative. Benign counterexample that must NOT "
        "match: 'the loader base64-encodes its config and decodes locally' "
        "(no secret + no send).",
    ),
)

# AMBIGUOUS prose markers - each token appears verbatim in benign security
# subject matter, so ONE hit alone never flags (verdict "unknown"); >=2
# DISTINCT hits flag, mirroring classify_refusal's >=2 gate.

_AMBIGUOUS_MARKERS: Tuple[_Marker, ...] = (
    _m(
        "exfil_send_me_anything", "exfiltration", "ambiguous",
        r"\b(send|forward|upload|paste|share|email|post)\b[^.!?:]{0,50}\b(me|us)\b",
        "AMBIGUOUS: bare 'send me' direction. Alone it covers benign "
        "'send me a message with the traceback'; paired with a second "
        "distinct marker ('copy your id_rsa', 'include the Authorization "
        "header') it completes a real exfil instruction. Benign "
        "counterexample that must NOT flag alone: 'If you have doubts, send "
        "me a message with the error text' (single hit -> not flagged).",
    ),
    _m(
        "exfil_auth_header", "exfiltration", "ambiguous",
        r"\b(copy|send|share|paste|grab|harvest|include)\b[^.!?:]{0,60}"
        r"\bauthorization\b[^.!?:]{0,40}\bheader\b",
        "AMBIGUOUS: 'include the Authorization header' is standard API-call "
        "advice; 'copy the Authorization header and send it to me' is an "
        "attack. Only the two-marker shape flags. Benign counterexample that "
        "must NOT flag alone: 'To call the API, include the Authorization "
        "header in your request' (single hit -> not flagged).",
    ),
    _m(
        "exfil_secret_store_dump", "exfiltration", "ambiguous",
        r"\b(dump|export|copy|cat|read|print)\b[^.!?:]{0,40}"
        r"\b(your|the|my)\b[^.!?:]{0,40}"
        r"(keychain|id_rsa|\.ssh\b|kubeconfig|\.aws\b|\.netrc|\.npmrc|credential store)",
        "AMBIGUOUS: 'copy your .ssh folder' alone matches sysadmin backup "
        "docs ('copy your .ssh keys to the encrypted vault'); paired with "
        "a send-me marker it is exfiltration. Benign counterexample that "
        "must NOT flag alone: 'Backup step: copy your .ssh directory to the "
        "encrypted vault' (single hit -> not flagged).",
    ),
    _m(
        "exec_persistence_install", "execution", "ambiguous",
        r"\b(create|register|install|save|drop|place|copy)\b[^.!?:]{0,50}"
        r"(scheduled task|cron\b|crontab|startup folder|launch agent|launchdaemon|"
        r"systemd service|rc\.local|run key|runonce)",
        "AMBIGUOUS: persistence install verbs are documented in every "
        "sysadmin course ('create the systemd service unit'). Paired with a "
        "second marker they indicate an injected persistence instruction. "
        "Benign counterexample that must NOT flag alone: 'create the systemd "
        "service unit and enable it' (single hit -> not flagged).",
    ),
    _m(
        "exec_this_script_now", "execution", "ambiguous",
        r"\b(run|execute)\b[^.!?:]{0,40}\b(this|the attached|my|the following)\b"
        r"[^.!?:]{0,30}\b(script|command|payload|binary|binary blob)\b"
        r"[^.!?:]{0,40}\b(now|immediately|right away|before anything else|first)\b",
        "AMBIGUOUS: execute-this-now shape; alone it matches CI walkthrough "
        "prose, and third-person malware analysis never has this/attached "
        "pointing at the user. Benign counterexample that must NOT match or "
        "flag: 'the dropper executes its payload immediately' (no "
        "this/attached/following -> no hit) and 'run the test script now' "
        "(single ambiguous hit -> not flagged).",
    ),
    _m(
        "exec_certutil_bitsadmin", "execution", "ambiguous",
        r"\b(certutil\s+[\-‐‑‒–—―]urlcache|bitsadmin\s+/transfer|bitsadmin\s+\-transfer)\b",
        "AMBIGUOUS: certutil/bitsadmin LOLBAS download verbs are quoted "
        "constantly in malware-analysis writeups. Paired with a second "
        "marker (pipe-execute, send-me) they indicate an injected command. "
        "Benign counterexample that must NOT flag alone: 'Some malware uses "
        "certutil -urlcache to fetch follow-on stages' (single hit -> not "
        "flagged).",
    ),
    _m(
        "exec_chmod_pipe", "execution", "ambiguous",
        r"\bchmod\s+\+x\b[^.!?:]{0,80}(\./|&&|\bthen\b|\band then\b)",
        "AMBIGUOUS: chmod+x-then-run is standard deploy documentation; only "
        "with a second distinct marker does it become an injection signal. "
        "Benign counterexample that must NOT flag alone: 'chmod +x the "
        "build, then commit it' (single hit -> not flagged).",
    ),
)

# TOOL-ARG markers - applied ONLY to serialized tool_call arguments, never
# to prose. A provider fabricating a shell/run_command/write_to_file call
# with these payloads is the highest-severity case per spec: the bytes are
# one interpreter away from executing. Known FP class documented per marker;
# default mode log_only means an FP costs one log line, never a mutation.

_TOOL_ARG_MARKERS: Tuple[_Marker, ...] = (
    _m(
        "tool_curl_pipe_shell", "execution", "tool_arg",
        r"\bcurl\b[^;]{0,140}\|\s*(ba|z|da|k|fi|tc|po)?sh\b",
        "ATTACK (tool args): curl piped to a shell interpreter inside a "
        "fabricated executable call = remote script executing with the "
        "user's privileges. Known benign class that still hits: Homebrew "
        "install one-liners ('curl -fsSL .../install.sh | bash') - accepted "
        "log-only FP; documented, never mutates. Benign counterexample that "
        "must NOT match: 'curl https://api.github.com/repos' (no pipe).",
    ),
    _m(
        "tool_wget_pipe_shell", "execution", "tool_arg",
        r"\bwget\b[^;]{0,140}\|\s*(ba|z|da|k|fi|tc|po)?sh\b",
        "ATTACK (tool args): wget into a shell - same family as the curl "
        "variant. Benign counterexample that must NOT match: 'wget "
        "https://example.com/file.tar.gz' (no pipe).",
    ),
    _m(
        "tool_iwr_iex", "execution", "tool_arg",
        r"(\biwr\b|invoke-webrequest|invoke-restmethod)[^;]{0,140}"
        r"(\|\s*iex\b|\|\s*invoke-expression|;|\band\s+then\s+)?"
        r"[;.]{0,10}\s*(iex|invoke-expression)\b",
        "ATTACK (tool args): iwr | iex is the PowerShell download-crime "
        "one-liner - remote code executes in-process, no file ever touches "
        "disk. Benign counterexample that must NOT match: 'iwr "
        "https://example.com/status' (no iex) and MS docs prose 'Invoke-"
        "WebRequest ... | Select-Object' (no iex on the right side).",
    ),
    _m(
        "tool_reverse_shell", "execution", "tool_arg",
        r"(\bncat?\b[\s\-]{1,40}\-e\b|\bncat?\b[\s\S]{0,40}>&\s*/dev/(tcp|udp)|"
        r"\bbash\b[\s\S]{0,10}>&\s*/dev/(tcp|udp)|"
        r"\bsocat\b[^;]{0,80}(exec|system|tty)[^;]{0,40}(connect|exec:|pty))",
        "ATTACK (tool args): nc -e / bash >& /dev/tcp / socat-exec shapes "
        "are reverse-shell one-liners - nothing benign redirects a shell "
        "into an outbound socket. Benign counterexample that must NOT "
        "match: 'nc -zv example.com 22' (port check, no -e, no redirect).",
    ),
    _m(
        "tool_certutil_chain", "execution", "tool_arg",
        r"\bcertutil\s+[\-‐‑‒–—―]urlcache\b[^;]{0,140}(\|\s*\w+|&&|;)",
        "ATTACK (tool args): certutil download chained into execution "
        "(pipe / && / ;) - the classic LOLBAS dropper. Benign "
        "counterexample that must NOT match: 'certutil -urlcache -split "
        "http://timestamp.digicert.com whql.cer' (documented WHQL fetch, "
        "no execution chain).",
    ),
    _m(
        "tool_bitsadmin_chain", "execution", "tool_arg",
        r"\bbitsadmin\b\s*/transfer\b[^;]{0,160}(\|\s*\w+|&&\s*\w+|;\s*\w+)",
        "ATTACK (tool args): bitsadmin download chained to an execute "
        "operator. Benign counterexample that must NOT match: 'bitsadmin "
        "/transfer job /download /priority high http://mirror/file.zip "
        "file.zip' (download only, no chain).",
    ),
    _m(
        "tool_base64_decode_pipe", "execution", "tool_arg",
        r"\bbase64\b[^;]{0,60}(\-d|\-\-decode|\-D)\b[^;]{0,80}\|",
        "ATTACK (tool args): base64-decode piped onward (to a shell/eval) "
        "hides the payload from plain-text inspection. Benign "
        "counterexample that must NOT match: 'echo aGk= | base64 -d' "
        "pipes INTO base64; the decode side has no trailing pipe.",
    ),
)

_MIN_DISTINCT_HITS = 2  # mirror classify_refusal's >=2-DISTINCT gate


# ── Verdict + classifier ─────────────────────────────────────────────────────


@dataclass
class GuardVerdict:
    """Result of classify_injection. `flagged` False on every fail-open path."""
    flagged: bool = False
    categories: List[str] = field(default_factory=list)
    matched: List[str] = field(default_factory=list)
    rationale: str = "clean"
    verdict: str = "clean"          # "injected" | "unknown" | "clean"


def _score_tier(text_norm: str, markers: Tuple[_Marker, ...]) -> List[str]:
    """Return marker IDs matching in the normalized text. Never raises."""
    hits: List[str] = []
    for marker in markers:
        try:
            if marker.pattern.search(text_norm) is not None:
                hits.append(marker.mid)
        except Exception:
            continue
    return hits


def classify_injection(
    visible_text: Any,
    had_tool_calls: bool = False,
    tool_args_text: Any = "",
) -> GuardVerdict:
    """Classify an assembled provider response for injected instructions.

    Scoring (mirrors classify_refusal's >=2-DISTINCT-HITS discipline):
      - ONE high-confidence imperative marker -> flagged.
      - >=2 DISTINCT ambiguous markers -> flagged. A single ambiguous
        marker alone -> "unknown", never flagged (precision over recall:
        it is far worse to flag legitimate security work than to miss a
        subtle attack).
      - ANY tool-arg marker hit (raw command inside fabricated tool_call
        arguments - machine-execution context) -> flagged; every marker
        there carries its own known-FP note and mode is log_only.
    Never raises. Malformed input (None, non-str, empty) -> clean verdict.
    """
    try:
        if not isinstance(visible_text, str):
            visible_text = "" if visible_text is None else str(visible_text or "")
        if not isinstance(tool_args_text, str):
            tool_args_text = "" if tool_args_text is None else str(tool_args_text or "")

        text_norm = _normalize(visible_text)
        args_norm = _normalize(tool_args_text)

        categories: List[str] = []
        matched: List[str] = []

        # Tool-arg tier first (highest severity per spec): raw command in a
        # fabricated tool call. Single hit flags - the bytes ARE the payload.
        for mid in _score_tier(args_norm, _TOOL_ARG_MARKERS):
            matched.append(mid)
            _mark_category(categories, "execution")

        # High-confidence prose tier: single hit flags.
        for marker in _HIGH_MARKERS:
            if marker.pattern.search(text_norm) is not None:
                matched.append(marker.mid)
                _mark_category(categories, marker.category)

        # Ambiguous prose tier: >=2 DISTINCT hits flag.
        ambiguous_hits = _score_tier(text_norm, _AMBIGUOUS_MARKERS)
        if len(ambiguous_hits) >= _MIN_DISTINCT_HITS:
            for mid in ambiguous_hits:
                matched.append(mid)
            for marker in _AMBIGUOUS_MARKERS:
                if marker.mid in ambiguous_hits:
                    _mark_category(categories, marker.category)

        if matched:
            return GuardVerdict(
                flagged=True,
                categories=sorted(set(categories)),
                matched=sorted(set(matched)),
                rationale=(
                    "provider response contains user-directed injection markers: "
                    + ", ".join(sorted(set(matched)))
                ),
                verdict="injected",
            )

        # Single ambiguous hit alone -> unknown (log-visible, never flagged).
        if len(ambiguous_hits) == 1:
            return GuardVerdict(
                flagged=False,
                categories=[],
                matched=list(ambiguous_hits),
                rationale="single ambiguous marker - below the >=2-hit gate",
                verdict="unknown",
            )

        return GuardVerdict(flagged=False, categories=[], matched=[],
                            rationale="no injection markers", verdict="clean")
    except Exception:
        return GuardVerdict(flagged=False, categories=[], matched=[],
                            rationale="classify_injection failed open", verdict="clean")


def _mark_category(categories: List[str], category: str) -> None:
    if category:
        categories.append(category)


# ── Tool-argument serialization (caller-side scan input) ────────────────────


def serialize_tool_args(openai_response: Any) -> str:
    """Best-effort serialization of tool_call arguments ALREADY present in an
    assembled response (openai choices[].message.tool_calls[].function.args +
    anthropic content[].tool_use.input fragments). Returns "" when absent or
    unparseable. A serializer, NOT an extractor for streaming - the stream
    observer accumulates fragments live. Never raises."""
    try:
        pieces: List[str] = []
        if isinstance(openai_response, dict):
            choices = openai_response.get("choices")
            if isinstance(choices, list):
                for choice in choices:
                    if not isinstance(choice, dict):
                        continue
                    message = choice.get("message")
                    if not isinstance(message, dict):
                        continue
                    calls = message.get("tool_calls")
                    if not isinstance(calls, list):
                        continue
                    for call in calls:
                        if not isinstance(call, dict):
                            continue
                        fn = call.get("function")
                        if isinstance(fn, dict):
                            name = fn.get("name")
                            args = fn.get("arguments")
                            if isinstance(name, str) and name:
                                pieces.append(name)
                            if isinstance(args, str) and args:
                                pieces.append(args)
                            elif isinstance(args, dict):
                                pieces.append(json.dumps(args, ensure_ascii=False, default=str))
            # Anthropic-shaped assembled content blocks.
            content = openai_response.get("content")
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict) or (block.get("type") != "tool_use"):
                        continue
                    name = block.get("name")
                    if isinstance(name, str) and name:
                        pieces.append(name)
                    payload = block.get("input")
                    if isinstance(payload, dict):
                        pieces.append(json.dumps(payload, ensure_ascii=False, default=str))
        return "\n".join(pieces)
    except Exception:
        return ""


# ── Config resolution (pure; never raises) ──────────────────────────────────


@dataclass
class GuardConfig:
    """Parsed tools.response_guard block. Only built when the master gate is on."""
    enabled: bool = True
    mode: str = "log_only"          # log_only | redact | block
    max_chars: int = 16384


def resolve_guard(cfg_tools: Any) -> Optional[GuardConfig]:
    """Parse the tools config dict into a GuardConfig, or None.

    Returns None when disabled, absent, or malformed - never raises. When
    None, the wiring layer performs ZERO scanning and ZERO mutation: the
    response path is byte-identical to a build without this module."""
    try:
        if not isinstance(cfg_tools, dict):
            return None
        block = cfg_tools.get("response_guard")
        if not isinstance(block, dict):
            return None
        if not block.get("enabled", False):
            return None
        mode = str(block.get("mode") or "log_only").strip().lower()
        if mode not in ("log_only", "redact", "block"):
            mode = "log_only"
        try:
            max_chars = int(block.get("max_chars", 16384))
        except (TypeError, ValueError):
            max_chars = 16384
        max_chars = max(1024, min(max_chars, 1024 * 1024))
        return GuardConfig(enabled=True, mode=mode, max_chars=max_chars)
    except Exception:
        return None


# ── Response action (default: identity passthrough) ─────────────────────────


def apply_guard(verdict: Any, response: Any, mode: str = "log_only") -> Any:
    """Apply the verdict's configured action to the response object.

    log_only (DEFAULT and the ONLY mode this pass ships): the response
    object is returned UNCHANGED - same identity, not a copy.
    redact / block: minimal, clearly-marked branches exist behind the mode
    knob but are NOT enabled this pass. They only operate on dict-shaped
    openai JSON; anything else (stream objects, synthetic responses with a
    json body behind .json()) is returned unchanged. Never raises."""
    try:
        if mode == "log_only" or not getattr(verdict, "flagged", False):
            return response
        if mode == "redact":
            # NOT ENABLED this pass - zipper retention ahead; the minimal
            # branch strips the matched marker spans from a plain dict
            # body we can safely copy. Anything non-dict passes through.
            if isinstance(response, dict):
                import copy as _copy
                scrubbed = _copy.deepcopy(response)
                # Redaction is intentionally inert pending FP-rate data:
                # strip nothing yet, the branch only proves the knob exists.
                return scrubbed
            return response
        if mode == "block":
            # NOT ENABLED this pass - replaces a dict-shaped body with a
            # security warning. Non-dict responses pass through unchanged.
            if isinstance(response, dict):
                return {
                    "error": {
                        "type": "bsl_response_guard",
                        "message": "response blocked by provider response guard (mode=block)",
                    }
                }
            return response
        return response
    except Exception:
        return response


# ── Capped off-loop JSONL writer (pattern replicated from fel_wiring) ───────
# fel_event is module-private in fel_wiring; the exact structure (50MB
# rotation + queue + to_thread + drop-on-full + no-loop direct fallback) is
# copied here bound to this module's own log path.

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GUARD_LOG_PATH = os.path.join(_PROJECT_ROOT, ".brain", "logs", "response_guard_events.jsonl")
GUARD_CAP_BYTES = 50 * 1024 * 1024
_GUARD_QUEUE_MAX = 256
_guard_queue: "Optional[asyncio.Queue]" = None
_guard_writer_task: "Optional[asyncio.Task]" = None
_guard_boot_healed = False


def _rotate_capped_file(path: str, cap: int) -> None:
    """Rotate path -> path+'.1' once it reaches cap bytes. Windows-safe.
    Never raises."""
    try:
        if os.path.exists(path) and os.path.getsize(path) >= cap:
            os.replace(path, path + ".1")
    except Exception:
        pass


def _guard_write_direct(rec: dict) -> None:
    """Blocking append with rotation; runs in a worker thread. Never raises."""
    try:
        os.makedirs(os.path.dirname(GUARD_LOG_PATH), exist_ok=True)
        _rotate_capped_file(GUARD_LOG_PATH, GUARD_CAP_BYTES)
        with open(GUARD_LOG_PATH, "a", encoding="utf-8") as guard_file:
            guard_file.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        _rotate_capped_file(GUARD_LOG_PATH, GUARD_CAP_BYTES)
    except Exception:
        pass


async def _guard_writer_task_loop() -> None:
    """Background drain: queue -> to_thread(direct write). One at a time
    keeps the file append-ordered; the queue absorbs bursts off the loop."""
    while True:
        rec = await _guard_queue.get()
        try:
            await asyncio.to_thread(_guard_write_direct, rec)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass


def _guard_line(rec: dict) -> None:
    """Non-blocking enqueue for the hot path (put_nowait, drop-on-full).
    Without a running loop (tests, CLI probes) falls back to a direct write."""
    global _guard_queue
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _guard_write_direct(rec)
        return
    if _guard_queue is None:
        _guard_queue = asyncio.Queue(maxsize=_GUARD_QUEUE_MAX)
    try:
        _guard_queue.put_nowait(rec)
    except asyncio.QueueFull:
        pass  # drop - logging must never stall the request


def _ensure_guard_writer() -> None:
    """Lazily boot the drain task + fire the boot-time self-heal rotation.
    Idempotent; recreates the writer when the event loop changed. Never raises."""
    global _guard_queue, _guard_writer_task, _guard_boot_healed
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    try:
        if not _guard_boot_healed:
            _guard_boot_healed = True
            loop.create_task(
                asyncio.to_thread(_rotate_capped_file, GUARD_LOG_PATH, GUARD_CAP_BYTES)
            )
        if _guard_queue is None:
            _guard_queue = asyncio.Queue(maxsize=_GUARD_QUEUE_MAX)
        task = _guard_writer_task
        if task is None or task.done() or task.get_loop() is not loop:
            _guard_writer_task = loop.create_task(_guard_writer_task_loop())
    except Exception:
        pass


def guard_event(event_type: str, *, model: str = "", provider: str = "",
                verdict: Any = None, details: Optional[dict] = None) -> None:
    """Append ONE structured JSONL event record:
    {ts, kind, model, provider, verdict, details}. Fail-silent by
    construction - an event-log failure must never surface on the request
    path. Never raises."""
    try:
        rec = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "kind": str(event_type or "unknown"),
            "model": model,
            "provider": provider,
            "verdict": (
                {
                    "flagged": bool(getattr(verdict, "flagged", False)),
                    "categories": list(getattr(verdict, "categories", []) or []),
                    "matched": list(getattr(verdict, "matched", []) or []),
                    "verdict": str(getattr(verdict, "verdict", "")),
                }
                if verdict is not None else {}
            ),
            "details": details if isinstance(details, dict) else {},
        }
        _ensure_guard_writer()
        _guard_line(rec)
    except Exception:
        pass


# ── Stream observer (semantics cloned from FelStreamObserver) ────────────────


class ResponseGuardObserver:
    """Accumulate visible text + tool-arg fragments from an SSE stream and
    classify them at the end.

    PASSTHROUGH ONLY (same invariant as FelStreamObserver / stream_guard):
    observe(chunk) never holds, delays, reorders, modifies, or raises on a
    chunk - the bytes a caller feeds it belong to a stream that is already
    flowing to the client untouched. It reads chunks as they fly past and
    forms a verdict when the stream ends. Bounded: the visible-text and
    tool-arg accumulators each cap at max_chars (default 16 KB); the line
    buffer caps at 64 KB so malformed SSE cannot grow memory unbounded.
    """

    def __init__(self, model: str = "", provider: str = "",
                 max_chars: int = 16384) -> None:
        self._model = model
        self._provider = provider
        self._max_chars = max(1, int(max_chars or 16384))
        self._text_chunks: List[str] = []
        self._tool_arg_chunks: List[str] = []
        self._char_count = 0
        self._arg_char_count = 0
        self._had_tools = False
        self._finalized = False
        self._dead = False
        self._line_buffer = b""

    def _add_text(self, text: Any) -> None:
        """Append visible text, hard-truncating at max_chars. Never appends
        past the cap, even partially over the boundary. Never raises."""
        try:
            if not isinstance(text, str):
                return
            if not text or self._char_count >= self._max_chars:
                return
            remaining = self._max_chars - self._char_count
            if len(text) > remaining:
                text = text[:remaining]
            self._text_chunks.append(text)
            self._char_count += len(text)
        except Exception:
            self._dead = True

    def _add_tool_args(self, fragment: Any) -> None:
        """Append a tool_call argument fragment under the same cap. Never raises."""
        try:
            if isinstance(fragment, dict):
                try:
                    fragment = json.dumps(fragment, ensure_ascii=False, default=str)
                except Exception:
                    return
            if not isinstance(fragment, str):
                return
            if not fragment or self._arg_char_count >= self._max_chars:
                return
            remaining = self._max_chars - self._arg_char_count
            if len(fragment) > remaining:
                fragment = fragment[:remaining]
            self._tool_arg_chunks.append(fragment)
            self._arg_char_count += len(fragment)
        except Exception:
            self._dead = True

    def observe(self, chunk: bytes) -> None:
        """Extract visible text + tool-arg fragments. NEVER raises, never
        modifies the chunk (bytes are read-only; nothing is buffered that
        would delay or reorder the caller's yield)."""
        if self._dead or self._finalized:
            return
        try:
            if not isinstance(chunk, (bytes, bytearray)):
                return
            # Cap the line buffer to prevent unbounded growth on malformed SSE.
            if len(self._line_buffer) > 65536:
                self._line_buffer = b""

            self._line_buffer += bytes(chunk)
            lines = self._line_buffer.split(b"\n")
            # Keep the last (potentially incomplete) line in the buffer.
            self._line_buffer = lines[-1]

            for line in lines[:-1]:
                if not line.startswith(b"data: "):
                    continue
                data_str = line[6:].decode("utf-8", errors="ignore").strip()
                if not data_str or data_str == "[DONE]":
                    continue
                try:
                    obj = json.loads(data_str)
                except Exception:
                    continue
                if not isinstance(obj, dict):
                    continue

                # Anthropic dialect: delta.type == "text_delta" -> delta.text;
                # "input_json_delta" -> partial_json (tool-call argument
                # fragments accumulate for the tool-arg scan tier).
                delta = obj.get("delta")
                if isinstance(delta, dict):
                    if delta.get("type") == "text_delta":
                        self._add_text(delta.get("text", ""))
                    elif delta.get("type") == "input_json_delta":
                        self._had_tools = True
                        self._add_tool_args(delta.get("partial_json", ""))
                    elif delta.get("type") == "tool_use":
                        self._had_tools = True

                # Anthropic dialect: content_block_start with
                # content_block.type == "tool_use" (block start carries the
                # input dict when the provider sends it whole).
                content_block = obj.get("content_block")
                if isinstance(content_block, dict) and content_block.get("type") == "tool_use":
                    self._had_tools = True
                    if "input" in content_block:
                        self._add_tool_args(content_block.get("input"))

                # OpenAI dialect: choices[].delta.content +
                # choices[].delta.tool_calls[].function.arguments fragments.
                choices = obj.get("choices")
                if isinstance(choices, list):
                    for choice in choices:
                        if not isinstance(choice, dict):
                            continue
                        choice_delta = choice.get("delta")
                        if not isinstance(choice_delta, dict):
                            continue
                        if "tool_calls" in choice_delta:
                            self._had_tools = True
                            calls = choice_delta.get("tool_calls")
                            if isinstance(calls, list):
                                for call in calls:
                                    if not isinstance(call, dict):
                                        continue
                                    fn = call.get("function")
                                    if isinstance(fn, dict):
                                        if isinstance(fn.get("name"), str):
                                            self._add_tool_args(fn.get("name"))
                                        self._add_tool_args(fn.get("arguments"))
                        self._add_text(choice_delta.get("content", ""))
        except Exception:
            self._dead = True

    def finalize(self) -> GuardVerdict:
        """Classify + emit ONE guard_event. Idempotent. NEVER raises; a dead
        observer returns a clean verdict (fail-open)."""
        if self._dead or self._finalized:
            return GuardVerdict(flagged=False, categories=[], matched=[],
                                rationale=(
                                    "observer dead" if self._dead
                                    else "finalized"
                                ), verdict="clean")
        try:
            self._finalized = True
            text = "".join(self._text_chunks)
            tool_args_text = "".join(self._tool_arg_chunks)
            verdict = classify_injection(
                text, had_tool_calls=self._had_tools, tool_args_text=tool_args_text
            )
            # Emit on every finalized scan (clean rows are the FP-rate
            # denominator; flag status rides the verdict struct).
            guard_event("scan", model=self._model, provider=self._provider,
                        verdict=verdict, details={
                            "stream": True,
                            "chars": len(text),
                            "tool_args_chars": len(tool_args_text),
                            "tool_calls": self._had_tools,
                        })
            return verdict
        except Exception:
            self._dead = True
            return GuardVerdict(flagged=False, categories=[], matched=[],
                                rationale="observer finalize failed open",
                                verdict="clean")
