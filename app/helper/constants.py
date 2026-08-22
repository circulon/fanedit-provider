"""
Shared constants: Plex's metadata type numbers, and the source-category
routing table built on top of them.
"""
from __future__ import annotations

from enum import IntEnum, StrEnum

# API path constants
URL_PREFIX_METADATA = "/metadata"
URL_PREFIX_MATCHES = "/matches"


class MatchRequestType(IntEnum):
    """Plex's metadata type numbers, as used in the match-request ``type``
    field and the MediaProvider ``Types`` array."""

    MOVIE = 1


class SourceType(StrEnum):
    """One member per app/client/source/<category>/ package (see
    app/client/registry.py), and per ENABLE_<CATEGORY>_SOURCES /
    <CATEGORY>_SOURCES env-var pair (app/helper/config.py). Each member's
    value is also its package directory name. Each category maps to
    exactly one MatchRequestType. This provider only serves movies -
    see app/helper/constants.py in circulon/metadata-provider for the
    full movie/show/season/episode/artist/album/track set."""

    MOVIE = "movie"


SOURCE_TYPES: tuple[SourceType, ...] = tuple(SourceType)

# Which match-request type each source category serves.
SOURCE_CATEGORY_MATCH_TYPES: dict[SourceType, MatchRequestType] = {
    SourceType.MOVIE: MatchRequestType.MOVIE,
}

# The subset of match types Plex's custom-provider API currently accepts -
# used to build the MediaProvider "Types" array (see app/routes.py).
PLEX_SUPPORTED_MATCH_TYPES: set[MatchRequestType] = {
    MatchRequestType.MOVIE,
}


def match_type_to_source_category(match_type: int | None) -> SourceType | None:
    """e.g. ``MatchRequestType.MOVIE -> SourceType.MOVIE``. Returns None for
    an unrecognized type - callers treat that as a 400."""
    for category, type_ in SOURCE_CATEGORY_MATCH_TYPES.items():
        if match_type == type_:
            return category
    return None
