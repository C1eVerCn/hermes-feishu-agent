import json
import logging
import threading
from collections import OrderedDict
from typing import Optional

log = logging.getLogger(__name__)

_MAX_SIZE = 10_000
_TTL_SECONDS = 86_400  # 24 hours


class Dedup:
    """
    In-memory LRU deduplication by message_id.
    Thread-safe. No external dependencies.
    """

    def __init__(self, max_size: int = _MAX_SIZE, ttl: float = _TTL_SECONDS) -> None:
        self._max_size = max_size
        self._ttl = ttl
        self._seen: OrderedDict[str, float] = OrderedDict()  # key → timestamp
        self._lock = threading.Lock()
        self._insert_count = 0

    def is_duplicate(self, key: str) -> bool:
        import time
        now = time.monotonic()

        with self._lock:
            if key in self._seen:
                ts = self._seen[key]
                if now - ts < self._ttl:
                    return True
                # Expired entry — treat as new
                del self._seen[key]

            self._seen[key] = now
            self._seen.move_to_end(key)
            self._insert_count += 1

            # Periodic sweep and LRU eviction
            if self._insert_count % 1000 == 0:
                self._sweep(now)
            while len(self._seen) > self._max_size:
                self._seen.popitem(last=False)

        return False

    def _sweep(self, now: float) -> None:
        import time
        expired = [k for k, ts in self._seen.items() if now - ts >= self._ttl]
        for k in expired:
            del self._seen[k]


dedup = Dedup()
