"""
Maps a source-agnostic ``SourceMetadata`` (app/client/base.py) into a
Plex-compatible ``PlexMetadata`` object (app/schema/plex.py), returned as a
plain dict.

``entry.metadata_type`` (set by the source) drives both the Plex ``type``
field and the GUID's metadataType component. Hierarchy (parent/grandparent)
fields are copied across whenever a source populates them, and stay unset
otherwise.
"""
from __future__ import annotations

import re
from typing import Any

from app.client.base import ImageEntry, PersonEntry, SourceMetadata
from app.helper.constants import URL_PREFIX_METADATA
from app.schema.plex import (
    GRANDPARENT_TYPE_OF,
    PARENT_TYPE_OF,
    TYPES_WITH_CHILDREN,
    MetadataType,
    PlexChildren,
    PlexGuid,
    PlexImage,
    PlexMetadata,
    PlexPerson,
    PlexRating,
    PlexSimilar,
    PlexTag,
    serialize,
)

_YEAR_RE = re.compile(r"(\d{4})")
_RATING_KEY_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


class Mapper:
    def __init__(self, scheme: str, include_extra_in_summary: bool = True):
        self.scheme = scheme
        self.include_extra_in_summary = include_extra_in_summary

    # ------------------------------------------------------------------
    def map_search_result(self, entry: SourceMetadata, score: int | None = None) -> dict[str, Any]:
        """Map a lightweight search candidate. Only ``ratingKey``/``key``/
        ``guid``/``type``/``title`` are guaranteed."""
        metadata = self._base_metadata(entry)
        metadata.score = score
        metadata.year = entry.year or self._extract_year(entry.originally_available_at)
        metadata.thumb = entry.thumb
        return metadata.to_dict()

    # ------------------------------------------------------------------
    def map_full_entry(self, entry: SourceMetadata) -> dict[str, Any]:
        """Map a full detail entry to a complete PlexMetadata object."""
        metadata = self._base_metadata(entry)

        metadata.originallyAvailableAt = entry.originally_available_at
        metadata.year = entry.year or self._extract_year(entry.originally_available_at)
        metadata.summary = self._build_summary(entry)
        metadata.originalTitle = entry.original_title
        metadata.titleSort = entry.title_sort
        metadata.contentRating = entry.content_rating
        metadata.tagline = entry.tagline
        metadata.studio = entry.studio
        metadata.isAdult = entry.is_adult
        metadata.duration = entry.duration_ms
        metadata.theme = entry.theme
        metadata.thumb = entry.thumb
        metadata.art = entry.art

        metadata.Image = [self._to_image(img, metadata.title) for img in entry.images]
        if not metadata.Image and entry.thumb:
            metadata.Image = [PlexImage(type="coverPoster", url=entry.thumb, alt=metadata.title)]
        metadata.OriginalImage = [self._to_image(img, metadata.title) for img in entry.original_images]

        metadata.Genre = [PlexTag(tag=g) for g in entry.genres if g]
        metadata.Guid = [PlexGuid(id=g) for g in entry.external_ids if g]
        metadata.Country = [PlexTag(tag=c) for c in entry.countries if c]
        metadata.Role = [self._to_person(p) for p in entry.roles]
        metadata.Director = [self._to_person(p) for p in entry.directors]
        metadata.Producer = [self._to_person(p) for p in entry.producers]
        metadata.Writer = [self._to_person(p) for p in entry.writers]
        metadata.Similar = [PlexSimilar(guid=s.guid, tag=s.tag) for s in entry.similar]
        metadata.Studio = [PlexTag(tag=s) for s in entry.studios if s]
        metadata.Network = [PlexTag(tag=n) for n in entry.networks if n]
        metadata.Rating = [
            PlexRating(value=round(float(r.value), 1), type=r.type, image=r.image) for r in entry.ratings
        ]

        self._apply_hierarchy(metadata, entry)
        metadata.skipChildren = entry.skip_children
        metadata.skipParent = entry.skip_parent

        return metadata.to_dict()

    # ------------------------------------------------------------------
    def map_children(self, entries: list[SourceMetadata]) -> dict[str, Any]:
        """Maps a parent's direct children (Seasons for a Show, Episodes
        for a Season) into a ``Children`` object - see PlexChildren. Used
        both to embed ``Children`` on a parent Metadata object
        (includeChildren=1) and, wrapped in a MediaContainer, as the body
        of the /children and /grandchildren endpoints (app/routes.py)."""
        mapped = [self.map_full_entry(child) for child in entries]
        return serialize(PlexChildren(size=len(mapped), Metadata=mapped))

    def attach_children(self, metadata_dict: dict[str, Any], entries: list[SourceMetadata]) -> dict[str, Any]:
        """Embeds a ``Children`` object into an already-mapped Metadata
        dict (in place) - see map_children()."""
        metadata_dict["Children"] = self.map_children(entries)
        return metadata_dict

    def _build_summary(self, entry: SourceMetadata) -> str | None:
        """Plain synopsis, plus ``entry.summary_extra`` appended when the
        source populated it and ``self.include_extra_in_summary`` is set."""
        summary = (entry.summary or "").strip()

        extra = (entry.summary_extra or "").strip() if self.include_extra_in_summary else ""
        if not extra:
            return summary or None

        return f"{summary}\n\n{extra}".strip() if summary else extra

    # ------------------------------------------------------------------
    def _base_metadata(self, entry: SourceMetadata) -> PlexMetadata:
        rating_key = str(entry.upstream_id)
        metadata_type = entry.metadata_type or MetadataType.MOVIE
        metadata_key = self._own_key(rating_key, metadata_type)
        return PlexMetadata(
            ratingKey=rating_key,
            key=metadata_key,
            guid=self.construct_guid(self.scheme, metadata_type, rating_key),
            title=entry.title or "",
            type=metadata_type,
        )

    def _apply_hierarchy(self, metadata: PlexMetadata, entry: SourceMetadata) -> None:
        """Copies parent/grandparent linkage fields across for a source
        that produces a child type (season/episode, album/track). No-op
        for an entry that doesn't set them.

        parentType/grandparentType (and, from them, parentGuid/
        grandparentGuid) are Plex-required for season/episode types and
        are inferred from the fixed movie/show/season/episode hierarchy
        (app/schema/plex.PARENT_TYPE_OF/GRANDPARENT_TYPE_OF) rather than
        needing a source to set them explicitly. Unlike this object's own
        ``key`` (see _own_key), parentKey/grandparentKey always point at
        the plain metadata endpoint - they're "go view the parent itself",
        not "browse the parent's children"."""
        parent_type = PARENT_TYPE_OF.get(entry.metadata_type or "")
        grandparent_type = GRANDPARENT_TYPE_OF.get(entry.metadata_type or "")

        if entry.parent_upstream_id:
            metadata.parentRatingKey = str(entry.parent_upstream_id)
            metadata.parentKey = f"{URL_PREFIX_METADATA}/{metadata.parentRatingKey}"
            if parent_type:
                metadata.parentType = parent_type
                metadata.parentGuid = self.construct_guid(self.scheme, parent_type, metadata.parentRatingKey)
        metadata.parentTitle = entry.parent_title
        metadata.parentThumb = entry.parent_thumb

        if entry.grandparent_upstream_id:
            metadata.grandparentRatingKey = str(entry.grandparent_upstream_id)
            metadata.grandparentKey = f"{URL_PREFIX_METADATA}/{metadata.grandparentRatingKey}"
            if grandparent_type:
                metadata.grandparentType = grandparent_type
                metadata.grandparentGuid = self.construct_guid(
                    self.scheme, grandparent_type, metadata.grandparentRatingKey
                )
        metadata.grandparentTitle = entry.grandparent_title
        metadata.grandparentThumb = entry.grandparent_thumb

        metadata.index = entry.index
        metadata.parentIndex = entry.parent_index

    @staticmethod
    def _own_key(rating_key: str, metadata_type: str) -> str:
        """Per the general API docs' note on ``key``/``type``: "One
        exception is the /children key for parents like shows and
        seasons. It will return a list of children even though the type
        describes the parent." So a Show/Season's own ``key`` points at
        its /children listing rather than its plain metadata endpoint
        (confirmed by the docs' own Show and Season example responses)."""
        base = f"{URL_PREFIX_METADATA}/{rating_key}"
        return f"{base}/children" if metadata_type in TYPES_WITH_CHILDREN else base

    @staticmethod
    def construct_guid(scheme: str, metadata_type: str, rating_key: str) -> str:
        """Format: ``{scheme}://{metadataType}/{ratingKey}``."""
        valid_rating_key = bool(_RATING_KEY_RE.match(rating_key or ""))
        if not valid_rating_key:
            raise ValueError(
                f'Invalid ratingKey: "{rating_key}". Must contain only ASCII letters, '
                "numbers, dashes, and underscores."
            )
        return f"{scheme}://{metadata_type}/{rating_key}"

    @staticmethod
    def _to_person(person: PersonEntry) -> PlexPerson:
        return PlexPerson(tag=person.tag, thumb=person.thumb, role=person.role, order=person.order)

    @staticmethod
    def _to_image(image: ImageEntry, default_alt: str) -> PlexImage:
        return PlexImage(type=image.type, url=image.url, alt=image.alt or default_alt)

    @staticmethod
    def _extract_year(date_str: str | None) -> int | None:
        if not date_str:
            return None
        match = _YEAR_RE.search(date_str)
        return int(match.group(1)) if match else None
