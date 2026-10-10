"""Harvest spend guard -- disk cache + HARD call budget for BILLED lanes (aibox).

Born 2026-10-08 after ~800 paid aibox requests produced one saved standard: a
killed or depth-floor-failed harvest kept its results only in memory, so every
re-run re-paid for calls that had already succeeded.

Behaviour (all of it OFF unless a driver calls `enable()`, so the live gate and
the test-suite are untouched):

  * every successful (HTTP 200) response from a billed source is appended to
    ``<cache_dir>/<lane>.jsonl`` the moment it lands -- a killed run loses at most
    the calls that were in flight;
  * a re-run answers identical requests from that cache at zero cost
    (harvest mode uses a model-seeded RNG so generated prompts repeat);
  * ``max_calls`` is a hard ceiling on NETWORK calls per lane per process. When it
    is hit, further calls return a synthetic ``budget_exhausted`` result instead of
    spending, and the driver aborts without saving.
"""
import hashlib
import json
import re
import threading
from pathlib import Path

_LOCK = threading.Lock()
_S = {"dir": None, "sources": frozenset(), "limit": 0,
      "net": {}, "hits": {}, "exhausted": set(), "mem": {}}


def enable(cache_dir, sources=("aibox",), max_calls=0):
    """Turn the guard on. max_calls=0 means cache only, no ceiling."""
    with _LOCK:
        p = Path(cache_dir)
        p.mkdir(parents=True, exist_ok=True)
        _S.update(dir=p, sources=frozenset(s.lower() for s in sources),
                  limit=int(max_calls), net={}, hits={}, exhausted=set(), mem={})


def disable():
    with _LOCK:
        _S.update(dir=None, sources=frozenset(), limit=0, net={}, hits={},
                  exhausted=set(), mem={})


def reset_counters():
    with _LOCK:
        _S.update(net={}, hits={}, exhausted=set())


def active_for(model_id: str) -> bool:
    if _S["dir"] is None:
        return False
    return (model_id or "").split("/")[0].strip().lower() in _S["sources"]


def _key(model_id, probe, extra) -> str:
    blob = json.dumps([model_id, probe.get("id"), probe.get("prompt"),
                       probe.get("max_tokens"), probe.get("messages"), extra],
                      sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _file(model_id: str) -> Path:
    return _S["dir"] / (re.sub(r"[^A-Za-z0-9_.-]+", "_", model_id) + ".jsonl")


def _load(model_id: str) -> dict:
    mem = _S["mem"].get(model_id)
    if mem is not None:
        return mem
    mem = {}
    f = _file(model_id)
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
                mem[rec["k"]] = rec["r"]
            except Exception:
                continue
    _S["mem"][model_id] = mem
    return mem


def lookup(model_id, probe, extra):
    """Cached ProbeResult fields (dict) or None. Counts a hit."""
    with _LOCK:
        hit = _load(model_id).get(_key(model_id, probe, extra))
        if hit is not None:
            _S["hits"][model_id] = _S["hits"].get(model_id, 0) + 1
        return hit


def store(model_id, probe, extra, result_dict: dict):
    """Persist a successful result immediately (append-only)."""
    if not (result_dict.get("status_code") == 200 and
            (result_dict.get("content") or result_dict.get("thinking")
             or result_dict.get("usage"))):
        return
    with _LOCK:
        k = _key(model_id, probe, extra)
        mem = _load(model_id)
        if k in mem:
            return
        mem[k] = result_dict
        with _file(model_id).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"k": k, "r": result_dict}, default=str) + "\n")


def charge(model_id) -> bool:
    """Reserve one NETWORK call. False when the per-lane ceiling is reached."""
    with _LOCK:
        used = _S["net"].get(model_id, 0)
        if _S["limit"] and used >= _S["limit"]:
            _S["exhausted"].add(model_id)
            return False
        _S["net"][model_id] = used + 1
        return True


def stats(model_id) -> dict:
    with _LOCK:
        return {"net": _S["net"].get(model_id, 0),
                "hits": _S["hits"].get(model_id, 0),
                "exhausted": model_id in _S["exhausted"],
                "limit": _S["limit"]}
