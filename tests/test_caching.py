"""Tests for TTLCache.get_or_load and CachingSourceClient."""
import threading
import time

import pytest

from app.client.base import SourceUnavailableError
from app.client.caching import CachingSourceClient
from app.util.ttl_cache import TTLCache


class _FakeSource:
    name = "fake"

    def __init__(self, delay: float = 0.0, error: Exception | None = None):
        self.calls: list[tuple] = []
        self.delay = delay
        self.error = error

    def search(self, query, ignore_score, skip=0, genre=None):
        self.calls.append(("search", query, ignore_score))
        time.sleep(self.delay)
        if self.error:
            raise self.error
        return []

    def get_entry(self, rating_key):
        self.calls.append(("entry", rating_key))
        time.sleep(self.delay)
        if self.error:
            raise self.error
        return None


def _wrap(source, search_floor=None):
    return CachingSourceClient(source, TTLCache(100, 60), TTLCache(100, 60), search_floor=search_floor)


def _run_concurrently(fn, n=8):
    barrier = threading.Barrier(n)
    results, errors = [], []

    def worker():
        barrier.wait()
        try:
            results.append(fn())
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    return results, errors


def test_get_or_load_caches_none():
    cache = TTLCache(10, 60)
    calls = []
    for _ in range(3):
        assert cache.get_or_load("k", lambda: calls.append(1)) is None
    assert len(calls) == 1


def test_get_or_load_expires():
    cache = TTLCache(10, ttl_seconds=0.05)
    calls = []
    cache.get_or_load("k", lambda: calls.append(1) or "v")
    time.sleep(0.06)
    cache.get_or_load("k", lambda: calls.append(1) or "v")
    assert len(calls) == 2


def test_concurrent_misses_share_one_upstream_call():
    source = _FakeSource(delay=0.1)
    client = _wrap(source)
    results, errors = _run_concurrently(lambda: client.get_entry("abc"))
    assert not errors
    assert len(results) == 8
    assert len(source.calls) == 1


def test_errors_are_shared_with_waiters_and_not_cached():
    source = _FakeSource(delay=0.1, error=SourceUnavailableError("down"))
    client = _wrap(source)
    results, errors = _run_concurrently(lambda: client.get_entry("abc"))
    assert not results
    assert len(errors) == 8
    assert all(isinstance(e, SourceUnavailableError) for e in errors)
    assert len(source.calls) == 1

    # Not cached: the next call goes upstream again.
    source.error = None
    assert client.get_entry("abc") is None
    assert len(source.calls) == 2


def test_search_floor_shares_one_search_between_thresholds():
    source = _FakeSource()
    client = _wrap(source, search_floor=65)
    client.search("Title", ignore_score=85)  # automatic match
    client.search("Title", ignore_score=65)  # manual match
    assert source.calls == [("search", "Title", 65)]


def test_without_floor_each_threshold_searches_separately():
    source = _FakeSource()
    client = _wrap(source)
    client.search("Title", ignore_score=85)
    client.search("Title", ignore_score=65)
    assert [c[2] for c in source.calls] == [85, 65]


def test_get_or_load_propagates_loader_error():
    cache = TTLCache(10, 60)
    with pytest.raises(ValueError):
        cache.get_or_load("k", lambda: (_ for _ in ()).throw(ValueError("boom")))
    assert cache.get("k") is None
