"""A small thread-safe, size-bounded TTL cache (stdlib only)."""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Generic, TypeVar

K = TypeVar("K")
V = TypeVar("V")


class TTLCache(Generic[K, V]):
    def __init__(self, maxsize: int = 2000, ttl_seconds: float = 300):
        self.maxsize = maxsize
        self.ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._data: "OrderedDict[K, tuple[float, V]]" = OrderedDict()

    def get(self, key: K) -> V | None:
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            inserted_at, value = entry
            if (time.monotonic() - inserted_at) >= self.ttl_seconds:
                del self._data[key]
                return None
            self._data.move_to_end(key)
            return value

    def set(self, key: K, value: V) -> None:
        with self._lock:
            self._data[key] = (time.monotonic(), value)
            self._data.move_to_end(key)
            while len(self._data) > self.maxsize:
                self._data.popitem(last=False)

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)
