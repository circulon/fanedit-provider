"""
Wraps a SourceClient with TTL caching for both ``search()`` and
``get_entry()``, so repeated calls close together hit the cache instead of
the live upstream. Every built source client is wrapped once, at
construction (see app/client/registry.py), so caching is shared across
whichever service calls it - MatchService and MetadataService both end up
going through the same cache for the same client.
"""
from __future__ import annotations

from app.client.base import SourceMetadata, SourceClient
from app.util.ttl_cache import TTLCache

# get_entry() can legitimately return None (confirmed not found). Wrapping
# a cached result in a 1-tuple disambiguates a cache hit of None from a
# cache miss, which TTLCache.get() also represents as None.
_NOT_FOUND = (None,)


class CachingSourceClient:
    """Decorates a SourceClient with TTL-cached search()/get_entry()/
    get_children()."""

    def __init__(
        self,
        client: SourceClient,
        search_cache: TTLCache,
        entry_cache: TTLCache,
        children_cache: TTLCache | None = None,
    ):
        self._client = client
        self._search_cache = search_cache
        self._entry_cache = entry_cache
        # Falls back to entry_cache when the caller doesn't pass a
        # dedicated cache, so existing construction sites keep working.
        self._children_cache = children_cache if children_cache is not None else entry_cache

    @property
    def name(self) -> str:
        return self._client.name

    def search(
        self, query: str, ignore_score: int, skip: int = 0, genre: str | None = None
    ) -> list[SourceMetadata]:
        key = self._search_key(query, ignore_score, skip, genre)

        cached = self._search_cache.get(key)
        if cached is not None:
            return cached

        results = self._client.search(query, ignore_score, skip=skip, genre=genre)
        self._search_cache.set(key, results)
        return results

    def get_entry(self, rating_key: str) -> SourceMetadata | None:
        key = self._entry_key(rating_key)

        cached = self._entry_cache.get(key)
        if cached is not None:
            return cached[0]

        entry = self._client.get_entry(rating_key)
        self._entry_cache.set(key, (entry,) if entry is not None else _NOT_FOUND)
        return entry

    def get_children(self, rating_key: str, episode_order: str | None = None) -> list[SourceMetadata] | None:
        """Delegates to the wrapped client's get_children() if it defines
        one; returns None (meaning "no children support") otherwise, so
        callers can always probe safely via getattr()."""
        inner = getattr(self._client, "get_children", None)
        if inner is None:
            return None

        key = self._children_key(rating_key, episode_order)
        cached = self._children_cache.get(key)
        if cached is not None:
            return cached[0]

        children = inner(rating_key, episode_order=episode_order)
        self._children_cache.set(key, (children,) if children is not None else _NOT_FOUND)
        return children

    # ------------------------------------------------------------------
    def _search_key(self, query: str, ignore_score: int, skip: int, genre: str | None) -> str:
        return f"{self.name}\0search\0{query}\0{ignore_score}\0{skip}\0{genre}"

    def _entry_key(self, rating_key: str) -> str:
        return f"{self.name}\0entry\0{rating_key}"

    def _children_key(self, rating_key: str, episode_order: str | None) -> str:
        return f"{self.name}\0children\0{rating_key}\0{episode_order}"
