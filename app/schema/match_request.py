"""
Dataclass for the incoming ``POST /library/metadata/matches`` request body.

Field reference: docs/API Endpoints.md#match-feature in
https://github.com/plexinc/tmdb-example-provider, and
https://developer.plex.tv/pms/#section/API-Info/Metadata-Providers.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class MatchRequest:
    """A parsed match request. ``title``/``year``/``guid``/``manual`` cover
    movie/show/artist/album matching. ``parentTitle``/``grandparentTitle``/
    ``index``/``parentIndex`` are the additional hints Plex sends for
    season/episode/track requests."""

    type: int | None = None
    title: str | None = None
    year: int | None = None
    guid: str | None = None
    manual: bool = False

    parentTitle: str | None = None
    grandparentTitle: str | None = None
    index: int | None = None
    parentIndex: int | None = None
    originallyAvailableAt: str | None = None
    filename: str | None = None

    # Required support for TV Shows/Seasons: embed a Children object (see
    # app/schema/plex.PlexChildren) on a positive match.
    includeChildren: bool = False
    # Optional SeasonType id - see app/schema/plex.py's SeasonType notes.
    episodeOrder: str | None = None

    @classmethod
    def from_dict(cls, body: dict[str, Any]) -> "MatchRequest":
        """Never raises on a malformed body - missing or unparseable
        fields fall back to this class's own defaults."""

        def _int_or_none(raw: Any) -> int | None:
            if raw is None:
                return None
            try:
                return int(raw)
            except (TypeError, ValueError):
                return None

        title = body.get("title")
        guid = body.get("guid")
        parent_title = body.get("parentTitle")
        grandparent_title = body.get("grandparentTitle")
        originally_available_at = body.get("originallyAvailableAt")
        filename = body.get("filename")
        episode_order = body.get("episodeOrder")

        return cls(
            type=_int_or_none(body.get("type")),
            title=title if title else None,
            year=_int_or_none(body.get("year")),
            guid=guid if guid else None,
            manual=bool(body.get("manual")),
            parentTitle=parent_title if parent_title else None,
            grandparentTitle=grandparent_title if grandparent_title else None,
            index=_int_or_none(body.get("index")),
            parentIndex=_int_or_none(body.get("parentIndex")),
            originallyAvailableAt=originally_available_at if originally_available_at else None,
            filename=filename if filename else None,
            includeChildren=bool(body.get("includeChildren")),
            episodeOrder=episode_order if episode_order else None,
        )
