"""
Common interface every metadata source client implements, so sources can be
mixed, reordered, and toggled purely through configuration - see
app/client/registry.py.

``SourceClient.search()``/``get_entry()`` return ``SourceMetadata``
objects, field-aligned with Plex's Metadata Provider objects
(https://developer.plex.tv/pms/#section/API-Info/Metadata-Providers).
app/helper/mapper.py maps a ``SourceMetadata`` to the Plex-facing
``PlexMetadata`` (app/schema/plex.py).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class PersonEntry:
    """One entry in a Role/Director/Producer/Writer array."""

    tag: str  # Required. Person's full name.
    thumb: str | None = None  # URL to person's photo.
    role: str | None = None  # Character name (Role) or credited role (others).
    order: int | None = None  # Display order in cast list.


@dataclass
class RatingEntry:
    """One entry in the Rating array."""

    value: float  # Required. 0-10.
    type: str = "audience"  # "audience" | "critic".
    image: str | None = None  # Rating-badge identifier, e.g. "imdb://image.rating".


@dataclass
class ImageEntry:
    """One entry in the Image/OriginalImage array."""

    url: str  # Required. Full URL to the image asset.
    type: str  # "background" | "backgroundSquare" | "clearLogo" | "coverPoster" | "snapshot".
    alt: str | None = None  # Alt text - typically the item's title.


@dataclass
class SimilarEntry:
    """One entry in the Similar array."""

    guid: str  # Required. GUID for the similar item.
    tag: str | None = None  # Title of the similar item.


@dataclass
class SourceMetadata:
    """Provider-agnostic representation of a single catalog/detail item.
    app/helper/mapper.py maps one of these to a Plex ``PlexMetadata`` dict.

    ``upstream_id`` is the raw id this source uses to look the item up
    again (becomes ``ratingKey``). ``title`` and ``upstream_id`` are the
    only required fields; everything else defaults to unset.
    """

    upstream_id: str
    title: str

    # "movie", "show", "season", "episode", "artist", "album", or "track" -
    # see app/schema/plex.MetadataType.
    metadata_type: str = "movie"

    # --- Required by Plex for movies ----------------------------------
    originally_available_at: str | None = None  # ISO 8601 "YYYY-MM-DD".

    # --- Recommended/optional scalar fields ------------------------------
    year: int | None = None
    summary: str | None = None
    original_title: str | None = None
    title_sort: str | None = None
    content_rating: str | None = None  # e.g. "PG-13"; non-US: "za/15".
    tagline: str | None = None
    studio: str | None = None
    is_adult: bool | None = None
    duration_ms: int | None = None
    thumb: str | None = None  # Default poster/thumbnail URL.
    art: str | None = None  # Default background artwork URL.
    theme: str | None = None  # URL to a short MP3 theme snippet.

    # --- Array fields ------------------------------------------------------
    images: list[ImageEntry] = field(default_factory=list)
    original_images: list[ImageEntry] = field(default_factory=list)
    genres: list[str] = field(default_factory=list)
    external_ids: list[str] = field(default_factory=list)  # "imdb://tt0088763", "tmdb://105", ...
    countries: list[str] = field(default_factory=list)
    roles: list[PersonEntry] = field(default_factory=list)  # Cast.
    directors: list[PersonEntry] = field(default_factory=list)
    producers: list[PersonEntry] = field(default_factory=list)
    writers: list[PersonEntry] = field(default_factory=list)
    similar: list[SimilarEntry] = field(default_factory=list)
    studios: list[str] = field(default_factory=list)
    ratings: list[RatingEntry] = field(default_factory=list)

    # --- Show only ---------------------------------------------------------
    networks: list[str] = field(default_factory=list)

    # --- Hierarchy fields (season/episode, album/track) ---------------------
    # "parent" is the immediate parent (show for a season, season for an
    # episode, artist for an album, album for a track); "grandparent" is
    # the top-level ancestor (show for an episode, artist for a track).
    parent_upstream_id: str | None = None
    parent_title: str | None = None
    parent_thumb: str | None = None
    grandparent_upstream_id: str | None = None
    grandparent_title: str | None = None
    grandparent_thumb: str | None = None
    # Season number (season type) / episode number (episode type) / track
    # number (track type).
    index: int | None = None
    # Season number, for an episode. Disc number, for a track.
    parent_index: int | None = None

    # --- Show/Episode only: skipChildren/skipParent -------------------------
    # Set skip_children=True on a show with no seasons (e.g. a podcast) so
    # clients use /grandchildren instead of /children. Set skip_parent=True
    # on an episode whose season is itself skipped. See
    # https://developer.plex.tv/pms/#section/API-Info/Metadata-Providers.
    skip_children: bool | None = None
    skip_parent: bool | None = None

    # --- Not part of the Plex Metadata object -------------------------------
    # Extra, already-formatted text a source wants appended to `summary`
    # (see app/helper/mapper.py._build_summary), gated by
    # Config.INCLUDE_EXTRA_IN_SUMMARY.
    summary_extra: str | None = None

    # Used by /extras (MetadataService.get_extras).
    extras: list[dict[str, Any]] = field(default_factory=list)


class SourceUnavailableError(Exception):
    """Raised by a SourceClient's search()/get_entry() to signal a
    recoverable failure to reach or parse that source. MatchService/
    SearchService treat this as "move on to the next enabled source" when
    one is available, and re-raise it when it's the last one, which the
    route layer turns into a 503. It is never cached, so the next request
    retries the upstream - raise it for outages rather than returning
    None/[] (which are cached as "not found"). Concrete
    sources should subclass this for their own error type - see
    app/client/source/movie/example_movie.py."""


class SourceClient(Protocol):
    """A single upstream metadata source. Concrete implementations live
    under app/client/source/<category>/ - see app/client/registry.py for
    the auto-registration convention."""

    #: Config key for this source - must match its own module's filename
    #: under app/client/source/<category>/, and an entry in that
    #: category's <CATEGORY>_SOURCES env var.
    name: str

    def search(
        self, query: str, ignore_score: int, skip: int = 0, genre: str | None = None
    ) -> list[SourceMetadata]:
        """Candidate SourceMetadata for a free-text query. Only
        ``upstream_id``/``title`` are guaranteed on a result.
        ``ignore_score`` (0-100) is available for a source that wants to
        pre-filter its own results before returning them. It's the lowest
        configured match threshold (see CachingSourceClient's
        search_floor); MatchService applies the stricter one itself. Returns ``[]``
        for no results; may raise SourceUnavailableError."""
        ...

    def get_entry(self, rating_key: str) -> SourceMetadata | None:
        """Full SourceMetadata for a single ratingKey, or None if not
        found. May raise SourceUnavailableError."""
        ...

    def get_children(self, rating_key: str, episode_order: str | None = None) -> list[SourceMetadata] | None:
        """OPTIONAL. Direct children of a parent item: Seasons for a Show,
        Episodes for a Season. Only a SHOW or SEASON source needs to
        implement this - see app/client/source/show/example_show.py's
        module docstring. ``episode_order`` is the optional
        ``episodeOrder``/``SeasonType`` id (see docs); a source that
        doesn't support multiple orderings should ignore it.

        Not part of the structural SourceClient contract enforced at
        registration time - callers (app/service/search.py) probe for it
        with ``getattr(client, "get_children", None)`` and treat a source
        that doesn't define it as having no children. Return ``None`` (or
        raise SourceUnavailableError) same as get_entry() for a not-found
        parent; return ``[]`` for a parent confirmed to have no children."""
        ...
