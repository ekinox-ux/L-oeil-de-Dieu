"""Cache memoire des classements de recherche, par index.

Une entree garde le searcher Tantivy qui a produit le classement, les couples
(score, adresse) deja calcules et le total. Les adresses ne valent que pour ce
searcher : les documents sont relus avec lui, donc toujours coherents. Rien
n'est ecrit sur disque ni journalise ; le contenu des documents n'est pas
garde, seulement leurs adresses.
"""

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field


@dataclass
class CachedRanking:
    searcher: object
    ranked: list
    total: int | None
    # Moins de resultats que demande : il n'y en a pas d'autres.
    exhausted: bool
    signature: object
    created_at: float = field(default_factory=time.monotonic)

    def covers(self, needed: int) -> bool:
        return self.exhausted or len(self.ranked) >= needed


class ResultCache:
    """LRU borne en nombre d'entrees et en duree de vie."""

    def __init__(self, max_entries: int, ttl_seconds: float):
        self.max_entries = max_entries
        self.ttl_seconds = ttl_seconds
        self._entries: OrderedDict = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key, signature) -> CachedRanking | None:
        if self.max_entries <= 0:
            return None
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if entry.signature != signature or now - entry.created_at > self.ttl_seconds:
                del self._entries[key]
                return None
            self._entries.move_to_end(key)
            return entry

    def put(self, key, entry: CachedRanking) -> None:
        if self.max_entries <= 0:
            return
        with self._lock:
            self._entries[key] = entry
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)
