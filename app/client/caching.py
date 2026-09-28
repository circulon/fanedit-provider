"""
Wraps a SourceClient with TTL caching for ``search()``, ``get_entry()`` and
``get_children()``, so repeated calls close together hit the cache instead
of the live upstream. Every built source client is wrapped once, at
construction (see app/client/registry.py), so caching is shared across
whichever service calls it - MatchService and MetadataService both end up
going through the same cache for the same client.

Concurrent identical calls share one upstream request (see
TTLCache.get_or_load). Errors (SourceUnavailableError) are never cached;
"not found" results (None / []) are.
"""
from __future__ import annotations

from app.client.base import SourceClient, SourceMetadata
from app.util.ttl_cache import TTLCache


class CachingSourceClient:
    """Decorates a SourceClient with TTL-cached search()/get_entry()/
    get_children()."""

    def __init__(
        self,
        client: SourceClient,
        search_cache: TTLCache,
        entry_cache: TTLCache,
        children_cache: TTLCache | None = None,
        search_floor: int | None = None,
    ):
        """``search_floor``: the lowest ``ignore_score`` any caller uses
        (normally ``min(MINIMUM_EXACT_SCORE, MINIMUM_MANUAL_SCORE)``). The
        upstream is always searched at that floor, so automatic and manual
        matches for the same title share one cached search; callers
        (MatchService) apply their own, stricter threshold to the results.
        ``None`` searches at whatever threshold each caller passes."""
        self._client = client
        self._search_cache = search_cache
        self._entry_cache = entry_cache
        # Falls back to entry_cache when the caller doesn't pass a
        # dedicated cache.
        self._children_cache = children_cache if children_cache is not None else entry_cache
        self._search_floor = search_floor

    @property
    def name(self) -> str:
        return self._client.name

    def search(
        self, query: str, ignore_score: int, skip: int = 0, genre: str | None = None
    ) -> list[SourceMetadata]:
        if self._search_floor is not None:
            ignore_score = min(ignore_score, self._search_floor)
        return self._search_cache.get_or_load(
            self._search_key(query, ignore_score, skip, genre),
            lambda: self._client.search(query, ignore_score, skip=skip, genre=genre),
        )

    def get_entry(self, rating_key: str) -> SourceMetadata | None:
        return self._entry_cache.get_or_load(
            self._entry_key(rating_key),
            lambda: self._client.get_entry(rating_key),
        )

    def get_children(self, rating_key: str, episode_order: str | None = None) -> list[SourceMetadata] | None:
        """Delegates to the wrapped client's get_children() if it defines
        one; returns None (meaning "no children support") otherwise, so
        callers can always call it safely."""
        inner = getattr(self._client, "get_children", None)
        if inner is None:
            return None
        return self._children_cache.get_or_load(
            self._children_key(rating_key, episode_order),
            lambda: inner(rating_key, episode_order=episode_order),
        )

    # ------------------------------------------------------------------
    def _search_key(self, query: str, ignore_score: int, skip: int, genre: str | None) -> str:
        return f"{self.name}\0search\0{query}\0{ignore_score}\0{skip}\0{genre}"

    def _entry_key(self, rating_key: str) -> str:
        return f"{self.name}\0entry\0{rating_key}"

    def _children_key(self, rating_key: str, episode_order: str | None) -> str:
        return f"{self.name}\0children\0{rating_key}\0{episode_order}"
