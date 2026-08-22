"""
Match Service - handles POST /library/metadata/matches.

Relevance scoring uses app/util/scoring.py's composite rapidfuzz score
rather than ``fuzz.WRatio`` (see that module for why).

manual vs. non-manual
-----------------------
Without ``manual: 1``, only the single best result is returned - and only
if unambiguous (no more than one candidate clears MINIMUM_EXACT_SCORE).
With ``manual: 1``, the full ranked, paged set of candidates above
MINIMUM_MANUAL_SCORE is returned, for a person to choose from.

Routing to a source category
------------------------------
``match_request.type`` maps to a source category (movie/show/season/
episode/music) via ``constants.match_type_to_source_category()``. A type
that isn't recognized, or maps to a category with no enabled sources,
raises ``UnsupportedMatchType`` (400) before any search runs.

Multi-source search
----------------------
Within the resolved category, sources (app/client/registry.py's
<CATEGORY>_SOURCES, in order) are tried one at a time; if a source's
results don't clear the score threshold, the next enabled source in the
category is tried. If a non-last source's `search()` call fails, it's
logged and treated as zero results, so the next source still runs; if the
last one fails, the error propagates.

See: docs/API Endpoints.md#match-feature in
https://github.com/plexinc/tmdb-example-provider, and
https://developer.plex.tv/pms/#section/API-Info/Metadata-Providers
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from app.client.base import SourceMetadata, SourceUnavailableError
from app.helper.config import Config
from app.helper.constants import (
    match_type_to_source_category,
    SOURCE_CATEGORY_MATCH_TYPES,
    SourceType,
)
from app.schema.match_request import MatchRequest
from app.schema.plex import MetadataType
from app.service.search import SearchService
from app.helper.mapper import Mapper
from app.util.scoring import rank_titles
from app.util.text_utils import strip_diacritics

logger = logging.getLogger(__name__)


@dataclass
class MatchThresholds:
    exact_minimum: int = Config.MINIMUM_EXACT_SCORE
    manual_minimum: int = Config.MINIMUM_MANUAL_SCORE


class MatchService:
    def __init__(
        self,
        search_by_category: dict[SourceType, SearchService],
        mapper: Mapper,
        thresholds: MatchThresholds | None = None,
        children_search: SearchService | None = None,
    ):
        #: One SearchService per enabled source category.
        self.search_by_category = search_by_category
        self.mapper = mapper
        self.thresholds = thresholds or MatchThresholds()
        #: Combined (all-category) SearchService used to resolve
        #: includeChildren=1, same as app/service/metadata.py uses - a
        #: matched ratingKey carries no category info of its own, so
        #: get_children() is tried across every enabled source.
        self.children_search = children_search

    @staticmethod
    def parse_guid(guid: str) -> dict:
        """Parse a GUID into its {scheme, metadataType, ratingKey} components."""
        match = re.match(r"^([^:]+)://([^/]+)/(.+)$", guid or "")
        if not match:
            raise ValueError(f'Invalid GUID format: "{guid}"')
        scheme, metadata_type, rating_key = match.groups()
        return {"scheme": scheme, "metadataType": metadata_type, "ratingKey": rating_key}

    def match(self, request: dict[str, Any], offset: int = 0, limit: int | None = None) -> dict[str, Any]:
        """Perform a match request. Returns a MediaContainer dict.

        ``offset``/``limit`` implement Response Paging and only apply when
        ``manual: 1`` is set.

        Raises ``UnsupportedMatchType`` for an unrecognized/unsupported
        ``type``.
        """
        match_request = MatchRequest.from_dict(request)
        search = self._resolve_search(match_request.type)

        logger.debug("request params: %s", request)

        if match_request.guid:
            direct_entry = self._try_match_by_guid(search, match_request.guid)
            if direct_entry is not None:
                self._maybe_attach_children(direct_entry, match_request)
                return self._container([direct_entry], offset=0, total_size=1)

        query_title = match_request.title
        if not query_title:
            return self._container([], offset=0, total_size=0)

        scored = self._search_and_score(search, query_title, match_request.manual)
        if not scored:
            return self._container([], offset=0, total_size=0)

        if not match_request.manual:
            if len(scored) > 1:
                return self._container([], offset=0, total_size=0)
            matched_entry, matched_score = scored[0]
            result = self.mapper.map_search_result(matched_entry, matched_score)
            self._maybe_attach_children(result, match_request, matched_entry)
            results = [result]
            return self._container(results, offset=0, total_size=len(results))

        total_size = len(scored)
        page = scored[offset : offset + limit] if limit is not None else scored[offset:]
        results = [self.mapper.map_search_result(entry, score) for entry, score in page]
        for result, (entry, _score) in zip(results, page):
            self._maybe_attach_children(result, match_request, entry)
        return self._container(results, offset=offset, total_size=total_size)

    def _maybe_attach_children(
        self, result: dict[str, Any], match_request: MatchRequest, entry: SourceMetadata | None = None
    ) -> None:
        """Embeds a Children object on ``result`` in place when
        ``includeChildren: 1`` was requested and the matched type supports
        children (required support for TV Shows/Seasons)."""
        if not match_request.includeChildren or self.children_search is None:
            return
        metadata_type = entry.metadata_type if entry is not None else result.get("type")
        if metadata_type not in (MetadataType.SHOW, MetadataType.SEASON):
            return

        rating_key = result.get("ratingKey")
        if not rating_key:
            return

        try:
            children = self.children_search.get_children(rating_key, episode_order=match_request.episodeOrder)
        except SourceUnavailableError:
            logger.warning("get_children(%r) failed while resolving includeChildren", rating_key, exc_info=True)
            children = None

        self.mapper.attach_children(result, children or [])

    # ------------------------------------------------------------------
    def _resolve_search(self, match_type: int | None) -> SearchService:
        """Maps the request's numeric ``type`` to the enabled SearchService
        for its source category, or raises UnsupportedMatchType."""
        category = match_type_to_source_category(match_type)
        if category is None:
            supported = ", ".join(
                f"{value} ({key})" for key, value in SOURCE_CATEGORY_MATCH_TYPES.items()
            )
            raise UnsupportedMatchType(
                f"Unsupported metadata type: {match_type!r}. "
                f"supported types: {supported}"
            )

        search = self.search_by_category.get(category)
        if search is None or not search.clients:
            raise UnsupportedMatchType(
                f"Metadata type {match_type!r} maps to the {category!r} source category, "
                f"which has no enabled sources - check ENABLE_{category.upper()}_SOURCES / "
                f"{category.upper()}_SOURCES."
            )
        return search

    def _try_match_by_guid(self, search: SearchService, guid: str) -> dict[str, Any] | None:
        try:
            parsed = self.parse_guid(guid)
        except ValueError:
            return None
        if parsed["scheme"] != self.mapper.scheme:
            return None
        entry = search.get_entry(parsed["ratingKey"])
        if entry is None:
            return None
        return self.mapper.map_full_entry(entry)

    def _search_and_score(
        self, search: SearchService, title: str, manual: bool = False
    ) -> list[tuple[SourceMetadata, int]]:
        """Try each enabled source in the resolved category, in order,
        scoring its results; return the first source's scored results that
        clear the threshold."""
        query = strip_diacritics(title) or title

        ignore_threshold = self.thresholds.exact_minimum
        if manual:
            ignore_threshold = self.thresholds.manual_minimum

        clients = search.clients
        for index, client in enumerate(clients):
            is_last = index == len(clients) - 1
            try:
                results = client.search(query, ignore_score=ignore_threshold)
            except SourceUnavailableError:
                if is_last:
                    raise
                logger.error(
                    "%s search failed for %r - trying next source", client.name, title, exc_info=True
                )
                continue

            scored = self._score_candidates(results, title, ignore_threshold)
            if scored:
                return scored

            logger.info(
                'No matches above threshold from %s for "%s" (%d raw result(s))%s',
                client.name, query, len(results),
                "" if is_last else " - trying next source",
            )

        return []

    def _score_candidates(
        self,
        found_results: list[SourceMetadata], title: str,
        score_threshold: int
    ) -> list[tuple[SourceMetadata, int]]:
        """Score raw results against the query title, drop anything under
        score_threshold, and return (entry, score) pairs sorted best-first."""
        if not found_results:
            return []

        titles = [entry.title or "" for entry in found_results]
        ranked = rank_titles(title, titles)

        scored: list[tuple[SourceMetadata, int]] = []
        for idx, score in ranked:
            entry = found_results[idx]
            should_ignore = score < score_threshold
            logger.info(
                "Search result - title: %s score: %d ignored: %s", entry.title, score, should_ignore
            )
            if should_ignore:
                continue
            scored.append((entry, score))

        return scored

    @staticmethod
    def _container(metadata_items: list[dict[str, Any]], offset: int, total_size: int) -> dict[str, Any]:
        return {
            "offset": offset,
            "totalSize": total_size,
            "size": len(metadata_items),
            "Metadata": metadata_items,
        }


class UnsupportedMatchType(ValueError):
    """Raised when a match request asks for a metadata type we don't serve."""
