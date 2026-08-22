"""
Metadata Service - handles GET /library/metadata/<ratingKey> (+ /images,
/extras).

See: docs/API Endpoints.md#metadata-feature in
https://github.com/plexinc/tmdb-example-provider
"""
from __future__ import annotations

from typing import Any

from app.client.base import SourceMetadata
from app.schema.plex import MetadataType
from app.service.search import SearchService
from app.helper.mapper import Mapper


class NotFoundError(Exception):
    """Raised when the requested ratingKey does not exist upstream."""


class MetadataService:
    """Thin translation layer over SearchService + Mapper. Constructed
    against the combined SearchService (every enabled category's clients)
    since a bare ratingKey path carries no type information indicating
    which category to search."""

    def __init__(self, search: SearchService, mapper: Mapper):
        self.search = search
        self.mapper = mapper

    def _fetch_entry(self, rating_key: str) -> SourceMetadata:
        entry = self.search.get_entry(rating_key)
        if entry is None:
            raise NotFoundError(f"No entry found for id '{rating_key}'")
        return entry

    def get_metadata(
        self, rating_key: str, include_children: bool = False, episode_order: str | None = None
    ) -> dict[str, Any]:
        """``include_children`` embeds a ``Children`` object (direct
        children only - Seasons for a Show, Episodes for a Season) per the
        ``includeChildren`` query param, required support for TV
        Shows/Seasons. A no-op for a type with no children (e.g. movie)."""
        entry = self._fetch_entry(rating_key)
        metadata = self.mapper.map_full_entry(entry)

        if include_children and entry.metadata_type in (MetadataType.SHOW, MetadataType.SEASON):
            children = self._fetch_children(rating_key, episode_order=episode_order) or []
            self.mapper.attach_children(metadata, children)

        return {
            "offset": 0,
            "totalSize": 1,
            "size": 1,
            "Metadata": [metadata],
        }

    # ------------------------------------------------------------------
    # /children and /grandchildren
    # ------------------------------------------------------------------
    def _fetch_children(self, rating_key: str, episode_order: str | None = None) -> list[SourceMetadata]:
        """Direct children for a ratingKey already known to exist. Raises
        NotFoundError if no enabled source can answer for it at all."""
        children = self.search.get_children(rating_key, episode_order=episode_order)
        if children is None:
            raise NotFoundError(f"No children found for id '{rating_key}'")
        return children

    def get_children(
        self, rating_key: str, offset: int = 0, limit: int | None = None, episode_order: str | None = None
    ) -> dict[str, Any]:
        """GET /library/metadata/<ratingKey>/children - Seasons for a Show,
        Episodes for a Season. Mandatory paging (see app/routes.py)."""
        self._fetch_entry(rating_key)  # 404 if the parent itself doesn't exist.
        children = self._fetch_children(rating_key, episode_order=episode_order)
        return self._children_container(children, offset=offset, limit=limit)

    def get_grandchildren(
        self, rating_key: str, offset: int = 0, limit: int | None = None, episode_order: str | None = None
    ) -> dict[str, Any]:
        """GET /library/metadata/<ratingKey>/grandchildren - Episodes for a
        Show (flattening past the Season level, e.g. for a
        skipChildren/single-season show); empty for a Season, which has no
        grandchildren of its own. Mandatory paging (see app/routes.py)."""
        entry = self._fetch_entry(rating_key)

        if entry.metadata_type == MetadataType.SEASON:
            return self._children_container([], offset=0, limit=limit)

        seasons = self._fetch_children(rating_key, episode_order=episode_order)
        grandchildren: list[SourceMetadata] = []
        for season in seasons:
            episodes = self.search.get_children(str(season.upstream_id), episode_order=episode_order)
            grandchildren.extend(episodes or [])

        return self._children_container(grandchildren, offset=offset, limit=limit)

    def _children_container(
        self, entries: list[SourceMetadata], offset: int, limit: int | None
    ) -> dict[str, Any]:
        total_size = len(entries)
        page = entries[offset : offset + limit] if limit is not None else entries[offset:]
        mapped = [self.mapper.map_full_entry(child) for child in page]
        return {
            "offset": offset,
            "totalSize": total_size,
            "size": len(mapped),
            "Metadata": mapped,
        }

    def get_images(self, rating_key: str) -> dict[str, Any]:
        entry = self._fetch_entry(rating_key)
        images = [{"type": img.type, "url": img.url, "alt": img.alt or entry.title} for img in entry.images]
        if not images and entry.thumb:
            images.append({"type": "coverPoster", "url": entry.thumb, "alt": entry.title})

        return {
            "offset": 0,
            "totalSize": len(images),
            "size": len(images),
            "Image": images,
        }

    def get_extras(self, rating_key: str) -> dict[str, Any]:
        entry = self._fetch_entry(rating_key)
        extras = [{"type": "extra", "url": extra["url"], "alt": extra["title"]} for extra in entry.extras]

        return {
            "offset": 0,
            "totalSize": len(extras),
            "size": len(extras),
            "Extras": extras,
        }
