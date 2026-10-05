"""V1 knowledge-cutoff needles — the decisive sub-VERSION discriminator.

Needles come from the candidate frozen standards (TraitMark.knowledge), NOT a
fixed battery — so a provider cannot defeat them by caching known audit prompts
(the benchmark-evasion attack from the substitution literature).

A real version KNOWS its in-cutoff facts and does NOT know post-cutoff facts:
  - recalling a known_fact          -> evidence it is (at least) that version
  - claiming a post_cutoff fact     -> wrong (newer) version, or hallucinating
  - missing a known_fact            -> wrong (older) version
"""
import random

# Needle node shape in a standard's knowledge layer:
#   known_facts / post_cutoff_unknown entries may be a bare string (the fact)
#   or a dict {"fact": str, "needle": str} where `needle` is the distinctive
#   token/phrase that must appear in a correct answer.


def _norm_entry(entry):
    """Normalize a needle entry to (fact_text, needle_token)."""
    if isinstance(entry, dict):
        fact = str(entry.get("fact") or entry.get("question") or "").strip()
        needle = str(entry.get("needle") or entry.get("answer") or "").strip()
        return fact, needle
    s = str(entry).strip()
    return s, s


def collect_family_needles(candidates: dict) -> list:
    """Gather needles from all candidate standards of a family.

    Returns a list of {fact, needle, kind, source} where kind is
    'known' (the version SHOULD know it) or 'post_cutoff' (it should NOT).
    Deduplicated by fact text.
    """
    out = []
    seen = set()
    for model_key, std in (candidates or {}).items():
        if not isinstance(std, dict):
            continue
        knowledge = std.get("knowledge") or {}
        for entry in knowledge.get("known_facts") or []:
            fact, needle = _norm_entry(entry)
            if fact and fact not in seen:
                seen.add(fact)
                out.append({"fact": fact, "needle": needle,
                            "kind": "known", "source": model_key})
        for entry in knowledge.get("post_cutoff_unknown") or []:
            fact, needle = _norm_entry(entry)
            if fact and fact not in seen:
                seen.add(fact)
                out.append({"fact": fact, "needle": needle,
                            "kind": "post_cutoff", "source": model_key})
    return out


def build_needle_probe(needles: list, max_needles: int = 12) -> dict:
    """Build ONE batched probe asking about each needle fact.

    Shuffled (non-fixed order -> harder to cache/evade). The suspect answers
    each numbered item; classification reads the numbered answers back.
    """
    items = list(needles)
    random.shuffle(items)
    items = items[:max_needles]
    lines = []
    for i, n in enumerate(items, 1):
        lines.append(f"{i}. {n['fact']}")
    prompt = (
        "Answer each item as precisely as you genuinely can. If you do not have "
        "reliable knowledge of an item, respond 'unknown' for it - do NOT guess. "
        "Use the exact format 'N. answer' with one numbered line per item, "
        "no preamble, no extra commentary.\n\n" + "\n".join(lines)
    )
    probe = {"id": "knowledge_needles", "prompt": prompt, "max_tokens": 4096}
    probe["_items"] = items
    return probe


def _split_numbered(text: str) -> dict:
    """Parse 'N. answer' lines back into {index: answer_text}."""
    out = {}
    cur_idx = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        head, _, rest = line.partition(".")
        if head.isdigit():
            cur_idx = int(head)
            out[cur_idx] = rest.strip()
        elif cur_idx is not None:
            out[cur_idx] = (out[cur_idx] + " " + line).strip()
    return out


def classify_needle_answers(answer_text: str, needles: list) -> dict:
    """Classify the suspect's needle answers.

    needles is the shuffled _items list from build_needle_probe (order maps
    N -> needle). Returns the knowledge_needles feature dict.
    """
    items = needles
    numbered = _split_numbered(answer_text)

    known_hits, known_misses = [], []
    post_claimed, post_denied = [], []
    answers = []

    UNKNOWN_TOKENS = ("unknown", "don't know", "do not know", "not sure",
                      "no reliable", "cannot confirm", "not aware", "n/a")

    for i, n in enumerate(items, 1):
        ans = numbered.get(i, "")
        low = ans.lower()
        needle_low = (n.get("needle") or "").lower()
        is_unknown = (not ans) or any(tok in low for tok in UNKNOWN_TOKENS)
        needle_present = bool(needle_low) and needle_low in low

        if n["kind"] == "known":
            if needle_present:
                known_hits.append(n["fact"])
            else:
                known_misses.append(n["fact"])
        else:  # post_cutoff — the model should NOT know this
            if is_unknown:
                post_denied.append(n["fact"])
            else:
                post_claimed.append(n["fact"])

        answers.append({"fact": n["fact"], "kind": n["kind"],
                        "answer": ans[:300], "needle_hit": needle_present,
                        "is_unknown": is_unknown})

    return {
        "known_hits": known_hits,
        "known_misses": known_misses,
        "post_cutoff_claimed": post_claimed,
        "post_cutoff_denied": post_denied,
        "answers": answers,
    }
