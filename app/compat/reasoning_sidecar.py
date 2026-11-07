from collections import OrderedDict
from copy import deepcopy
from hashlib import sha256
from threading import RLock
import time
from typing import Any, Dict, List, Optional


class ReasoningSidecar:
    def __init__(self, max_entries=4096, ttl_s=3600.0):
        self.max_entries = max_entries
        self.ttl_s = ttl_s
        self._entries = OrderedDict()
        self._lock = RLock()

    def store(self, keys, artifact):
        now = time.monotonic()
        with self._lock:
            for key in list(self._entries):
                if self._entries[key][0] <= now:
                    del self._entries[key]
            for key in keys:
                previous = self._entries.pop(key, (0, {}))[1]
                self._entries[key] = (now + self.ttl_s, {**previous, **deepcopy(artifact)})
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)

    def lookup(self, keys):
        now = time.monotonic()
        with self._lock:
            for key in keys:
                entry = self._entries.get(key)
                if entry is None:
                    continue
                if entry[0] <= now:
                    del self._entries[key]
                    continue
                self._entries.move_to_end(key)
                return deepcopy(entry[1])
        return None


_cache = ReasoningSidecar()


def _keys(tool_call_ids, assistant_text):
    keys = [("tool", call_id) for call_id in tool_call_ids if call_id]
    if assistant_text:
        keys.append(("text", sha256(assistant_text.encode("utf-8")).hexdigest()[:24]))
    return keys


def store_reasoning_artifact(*, tool_call_ids: List[str], assistant_text: str = "",
                             thinking: str = "", signature: str = "",
                             thought_signature: str = "", reasoning_content: str = "",
                             redacted_thinking: Optional[List[Any]] = None) -> None:
    artifact = {key: value for key, value in {
        "thinking": thinking,
        "signature": signature,
        "thought_signature": thought_signature,
        "reasoning_content": reasoning_content,
        "redacted_thinking": redacted_thinking,
    }.items() if value}
    if artifact:
        _cache.store(_keys(tool_call_ids, assistant_text), artifact)


def lookup_reasoning_artifact(*, tool_call_id: str = "", assistant_text: str = "") -> Optional[Dict[str, Any]]:
    return _cache.lookup(_keys([tool_call_id], assistant_text))
