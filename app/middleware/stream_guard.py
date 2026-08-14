"""
Middleware.stream_guard — The pre-first-byte fallback invariant.

THE INVARIANT
-------------
    No byte sent to client yet  ->  combo fallback is SAFE.
    Any byte already sent       ->  combo fallback is FORBIDDEN, permanently.
                                    Emit a terminal frame and stop.

WHY THIS EXISTS
---------------
Every IDE freeze in this project traces to one operation: abandoning an
in-flight upstream response and starting a different one while the client is
already parsing the first. The client's SSE parser receives a second stream's
frames interleaved into the first stream's state and waits forever for an end
that never comes in a shape it can recognise.

Changelog history, all the same root cause:
  2026-07-22  raise after data yielded          -> freeze
  2026-07-25  499 return without [DONE]         -> freeze
  2026-08-02  recursive retry, new SSE stream   -> freeze
  2026-08-03  vision self-call, pool starvation -> freeze (whole server)

Each was fixed at ITS OWN SITE. That is why the class keeps recurring: the rule
was a convention honored at some of ~88 combo-advance sites, not an invariant.
This module makes it structural, so a new advance site cannot silently omit it.

COMPARISON: 9router's MITM proxy almost never freezes despite having ~1 timeout
to our ~273. It has no multi-model failover, so it never starts a second stream.
Its stability comes from stream DISCIPLINE, not from having fewer guardrails.
We keep failover (it is the product) and adopt the discipline.

NOT A TIMEOUT MODULE. Deadlines remain a last-resort backstop elsewhere. With
this invariant enforced, budget exhaustion should become rare — and therefore
informative when it does happen, instead of routine background noise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


# Cap for the retained sample of first-emitted bytes (diagnostics only).
# 256B is enough to recognise a frame shape (message_start / reasoning_content
# delta / text delta) without growing per-request memory meaningfully.
_DIAG_CAP = 256


@dataclass
class StreamEmissionState:
    """Tracks whether any byte has reached the client for one response.

    One instance per client-facing stream. Passed to every fallback decision so
    the answer to "may I retry?" comes from a single place.
    """

    # True once ANY byte has been yielded downstream. Once True, never False:
    # the client's parser cannot be un-fed.
    emitted: bool = False
    # Bytes emitted, for diagnostics.
    byte_count: int = 0
    # Capped sample of the FIRST bytes marked as emitted, for forensics.
    # The GPT-5.6-SOL case (2026-08-13): a transport death after 11204B was
    # refused fallback while out=0 -- judging whether that veto was correct
    # requires SEEING what those bytes were (reasoning scaffolding vs real
    # content). This sample answers that from the refusal log alone. It does
    # NOT influence the veto decision itself.
    first_emitted: Optional[bytes] = None
    # Fallback attempts refused because emission had already begun. A non-zero
    # value here is the signal that a leaf died mid-stream, which is exactly the
    # scenario that used to freeze the IDE.
    refused_fallbacks: int = 0
    refusal_reasons: List[str] = field(default_factory=list)

    def mark_emitted(self, chunk: Optional[bytes] = None) -> None:
        """Record that a chunk was sent to the client. Call BEFORE the yield.

        Marking before rather than after matters: if the consumer cancels the
        generator at the yield point, the bytes may still have been buffered
        downstream. Assuming emission is the safe error.
        """
        self.emitted = True
        if chunk:
            self.byte_count += len(chunk)
            # Retain a capped sample of the first emitted bytes for forensics.
            if len(self.first_emitted or b"") < _DIAG_CAP:
                _have = self.first_emitted or b""
                self.first_emitted = _have + chunk[: _DIAG_CAP - len(_have)]

    def first_emitted_preview(self, limit: int = 300) -> str:
        """Human-readable repr of the retained first-bytes sample.

        Empty when nothing was captured (e.g. mark_emitted() was called with
        no payload). Used by refusal_log() only; never gates the veto.
        """
        if not self.first_emitted:
            return ""
        return repr(self.first_emitted)[:limit]

    def mark_emitted_if_content(self, chunk: Optional[bytes]) -> bool:
        """Mark emission only if `chunk` carries RENDERABLE content.

        BUG M (2026-08-04). Raw SSE passthrough pumps forwarded upstream bytes
        and called `mark_emitted(chunk)` on EVERY one. But a stream opens with
        frames that render nothing:

            event: message_start        {"role":"assistant","content":[]}
            event: ping                 {"type":"ping"}
            event: content_block_start  {"content_block":{"text":""}}

        Those flipped `emitted=True` before any text existed, so a leaf that
        then produced ZERO output could not fail over: `may_fallback()` refused,
        and the user got a dead stream while healthy chain entries went untried.
        Same defect class as BUG L, different pump.

        DIRECTION OF SAFETY. The dangerous error is a FALSE NEGATIVE: declaring
        real content non-renderable would permit a second stream to splice into
        a transcript the user is already reading -- the freeze this whole module
        exists to prevent. A false positive merely forgoes one fallback. So this
        marks emission by DEFAULT and withholds it only when the chunk is
        PROVABLY scaffolding-only. Anything unrecognised counts as content.

        Returns whether emission was marked, for callers tracking their own flag.
        """
        if not chunk:
            return False
        if _is_scaffolding_only(chunk):
            # Bytes still went out; count them so `byte_count` stays honest.
            # Only the fallback veto is withheld.
            self.byte_count += len(chunk)
            return False
        self.mark_emitted(chunk)
        return True

    def may_fallback(self, reason: str = "") -> bool:
        """Return True only if combo fallback is still safe.

        Every combo-advance site MUST consult this before raising its fallback
        signal. Returns False once any byte has been emitted, and records the
        refusal for logging.
        """
        if self.emitted:
            self.refused_fallbacks += 1
            if reason:
                self.refusal_reasons.append(reason)
            return False
        return True

    def refusal_log(self, model: str = "", provider: str = "") -> str:
        """Human-readable line explaining a refusal. Empty when none occurred."""
        if not self.refused_fallbacks:
            return ""
        label = f"{provider}/{model}" if (provider or model) else "upstream"
        reasons = ", ".join(self.refusal_reasons[-3:]) or "unspecified"
        preview = self.first_emitted_preview()
        tail = f" | first-bytes: {preview}" if preview else ""
        return (
            f"[STREAM-GUARD] {label}: refused {self.refused_fallbacks} "
            f"post-emission fallback(s) after {self.byte_count}B "
            f"[{reasons}] — emitting terminal frame instead of a second stream{tail}"
        )


def may_fallback(state: Optional[StreamEmissionState], reason: str = "") -> bool:
    """Module-level guard tolerant of a missing state.

    A caller with no state threaded yet is treated as pre-emission (True), which
    preserves today's behavior at not-yet-migrated sites rather than silently
    disabling their fallback. Migration is therefore incremental and safe; the
    accompanying test asserts the sites that matter are threaded.
    """
    if state is None:
        return True
    return state.may_fallback(reason)


# Frame types that are pure protocol scaffolding: they advance the SSE state
# machine but put NOTHING on screen. Deliberately an ALLOWLIST of types proven
# non-renderable, never a denylist -- an unknown frame must count as content.
_SCAFFOLDING_TYPES = (
    b'"type":"ping"',
    b'"type": "ping"',
    b'"type":"message_start"',
    b'"type": "message_start"',
    b'"type":"content_block_start"',
    b'"type": "content_block_start"',
)

# Markers proving a chunk DOES carry renderable output. Checked first, so a
# batched chunk containing both scaffolding and real text counts as content.
_CONTENT_MARKERS = (
    b'"text_delta"',
    b'"thinking_delta"',
    b'"input_json_delta"',
    b'"tool_use"',
    b'"functionCall"',
    b'"inlineData"',
)


def _is_scaffolding_only(chunk: bytes) -> bool:
    """True only if `chunk` provably contains NO renderable content.

    BUG M helper. Conservative by construction: returns True only for chunks
    that match a known-scaffolding type AND contain no content marker. Every
    ambiguous or unrecognised chunk returns False (treated as content), because
    a false negative here would allow a second stream to splice into a
    transcript the user is already reading -- the freeze class this module
    exists to prevent.

    Comment/keepalive lines (`: keepalive`) are scaffolding too: SSE comments
    are discarded by every conforming parser.
    """
    if not chunk:
        return True
    # Any content marker anywhere disqualifies the whole chunk immediately.
    for marker in _CONTENT_MARKERS:
        if marker in chunk:
            return False
    stripped = chunk.strip()
    # Pure SSE comment / keepalive: every line is a comment.
    if stripped.startswith(b":") and b"data:" not in stripped:
        return True
    # A non-empty text field means real output regardless of frame type. Absence
    # of the empty-string form is required, so `"text":""` stays scaffolding.
    if b'"text":"' in chunk and b'"text":""' not in chunk:
        return False
    if b'"content":"' in chunk and b'"content":""' not in chunk:
        return False
    return any(t in chunk for t in _SCAFFOLDING_TYPES)

