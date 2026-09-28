"""A small thread-safe, size-bounded TTL cache (stdlib only)."""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Generic, TypeVar

K = TypeVar("K")
V = TypeVar("V")

_MISSING = object()


class _InFlight:
    """One in-progress load, shared by every caller waiting on the same key."""

    __slots__ = ("done", "value", "error")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.value = None
        self.error: BaseException | None = None


class TTLCache(Generic[K, V]):
    def __init__(self, maxsize: int = 2000, ttl_seconds: float = 300):
        self.maxsize = maxsize
        self.ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._data: "OrderedDict[K, tuple[float, V]]" = OrderedDict()
        self._in_flight: dict[K, _InFlight] = {}

    def get(self, key: K) -> V | None:
        """The cached value, or None on a miss/expiry. Use get_or_load() to
        cache None itself."""
        with self._lock:
            value = self._lookup(key)
        return None if value is _MISSING else value

    def set(self, key: K, value: V) -> None:
        with self._lock:
            self._store(key, value)

    def get_or_load(self, key: K, loader: Callable[[], V]) -> V:
        """Returns the cached value for ``key``, calling ``loader()`` on a
        miss and caching whatever it returns (including None).

        Concurrent misses for the same key share one ``loader()`` call:
        the first caller loads, the rest wait for its result. If the loader
        raises, nothing is cached and every waiting caller gets the same
        exception."""
        with self._lock:
            value = self._lookup(key)
            if value is not _MISSING:
                return value
            flight = self._in_flight.get(key)
            is_loader = flight is None
            if is_loader:
                flight = self._in_flight[key] = _InFlight()

        if not is_loader:
            flight.done.wait()
            if flight.error is not None:
                raise flight.error
            return flight.value

        try:
            value = loader()
        except BaseException as exc:
            flight.error = exc
            raise
        else:
            flight.value = value
            return value
        finally:
            with self._lock:
                if flight.error is None:
                    self._store(key, flight.value)
                self._in_flight.pop(key, None)
            flight.done.set()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    # -- callers must hold self._lock ----------------------------------
    def _lookup(self, key: K):
        entry = self._data.get(key)
        if entry is None:
            return _MISSING
        inserted_at, value = entry
        if (time.monotonic() - inserted_at) >= self.ttl_seconds:
            del self._data[key]
            return _MISSING
        self._data.move_to_end(key)
        return value

    def _store(self, key: K, value: V) -> None:
        self._data[key] = (time.monotonic(), value)
        self._data.move_to_end(key)
        while len(self._data) > self.maxsize:
            self._data.popitem(last=False)
