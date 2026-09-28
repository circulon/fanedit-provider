"""
Tests for children/grandchildren support - see:
  - app/schema/plex.py (PlexChildren, TYPES_WITH_CHILDREN, PARENT_TYPE_OF/
    GRANDPARENT_TYPE_OF)
  - app/helper/mapper.py (_own_key, _apply_hierarchy, map_children)
  - app/service/metadata.py (get_children/get_grandchildren, includeChildren)
  - app/service/match.py (includeChildren)
  - app/routes.py (/children, /grandchildren)

Uses small fake SourceClient objects (get_children implemented directly,
skipping app/client/registry.py's auto-discovery) rather than a real
network-backed source, per app/client/base.SourceClient's documented
optional get_children() contract.
"""
from __future__ import annotations

from app.client.base import SourceMetadata
from app.helper.config import TestConfig
from app.helper.constants import URL_PREFIX_METADATA
from app.helper.mapper import Mapper
from app.schema.plex import MetadataType
from app.service.match import MatchService, MatchThresholds
from app.service.metadata import MetadataService, NotFoundError
from app.service.search import SearchService

SCHEME = TestConfig.PROVIDER_IDENTIFIER


class FakeShowClient:
    """A minimal SourceClient: one Show (id "show-1") with two Seasons,
    each with two Episodes - all served from an in-memory dict."""

    name = "fake_show"

    def __init__(self):
        self.show = SourceMetadata(upstream_id="show-1", title="Test Show", metadata_type=MetadataType.SHOW)

        self.seasons = [
            SourceMetadata(
                upstream_id=f"season-{n}", title=f"Season {n}", metadata_type=MetadataType.SEASON,
                parent_upstream_id="show-1", parent_title="Test Show", index=n,
            )
            for n in (1, 2)
        ]

        self.episodes = {
            "season-1": [
                SourceMetadata(
                    upstream_id=f"s1e{n}", title=f"S1E{n}", metadata_type=MetadataType.EPISODE,
                    parent_upstream_id="season-1", parent_title="Season 1",
                    grandparent_upstream_id="show-1", grandparent_title="Test Show",
                    index=n, parent_index=1,
                )
                for n in (1, 2, 3)
            ],
            "season-2": [
                SourceMetadata(
                    upstream_id=f"s2e{n}", title=f"S2E{n}", metadata_type=MetadataType.EPISODE,
                    parent_upstream_id="season-2", parent_title="Season 2",
                    grandparent_upstream_id="show-1", grandparent_title="Test Show",
                    index=n, parent_index=2,
                )
                for n in (1, 2)
            ],
        }

    def search(self, query, ignore_score, skip=0, genre=None):
        return [self.show] if query.lower() in self.show.title.lower() else []

    def get_entry(self, rating_key):
        if rating_key == "show-1":
            return self.show
        for season in self.seasons:
            if season.upstream_id == rating_key:
                return season
        for episodes in self.episodes.values():
            for episode in episodes:
                if episode.upstream_id == rating_key:
                    return episode
        return None

    def get_children(self, rating_key, episode_order=None):
        if rating_key == "show-1":
            return list(self.seasons)
        if rating_key in self.episodes:
            return list(self.episodes[rating_key])
        return None


def _services():
    client = FakeShowClient()
    search = SearchService(clients=[client])
    mapper = Mapper(scheme=SCHEME)
    metadata_service = MetadataService(search=search, mapper=mapper)
    return client, search, mapper, metadata_service


# ----------------------------------------------------------------------
# Mapper: key / parentKey / parentType / parentGuid
# ----------------------------------------------------------------------
def test_show_and_season_key_points_at_children():
    mapper = Mapper(scheme=SCHEME)
    show = SourceMetadata(upstream_id="show-1", title="Test Show", metadata_type=MetadataType.SHOW)
    mapped = mapper.map_full_entry(show)
    assert mapped["key"] == f"{URL_PREFIX_METADATA}/show-1/children"


def test_movie_key_is_plain():
    mapper = Mapper(scheme=SCHEME)
    movie = SourceMetadata(upstream_id="movie-1", title="Test Movie", metadata_type=MetadataType.MOVIE)
    mapped = mapper.map_full_entry(movie)
    assert mapped["key"] == f"{URL_PREFIX_METADATA}/movie-1"


def test_episode_hierarchy_fields_are_populated():
    mapper = Mapper(scheme=SCHEME)
    episode = SourceMetadata(
        upstream_id="s1e1", title="S1E1", metadata_type=MetadataType.EPISODE,
        parent_upstream_id="season-1", parent_title="Season 1",
        grandparent_upstream_id="show-1", grandparent_title="Test Show",
        index=1, parent_index=1,
    )
    mapped = mapper.map_full_entry(episode)

    assert mapped["parentType"] == "season"
    assert mapped["parentGuid"] == f"{SCHEME}://season/season-1"
    # parentKey is plain (points at the season's own detail), unlike its "key".
    assert mapped["parentKey"] == f"{URL_PREFIX_METADATA}/season-1"

    assert mapped["grandparentType"] == "show"
    assert mapped["grandparentGuid"] == f"{SCHEME}://show/show-1"
    assert mapped["grandparentKey"] == f"{URL_PREFIX_METADATA}/show-1"


# ----------------------------------------------------------------------
# MetadataService: /children, /grandchildren, includeChildren
# ----------------------------------------------------------------------
def test_get_children_of_show_returns_seasons():
    _, _, _, metadata_service = _services()
    result = metadata_service.get_children("show-1")
    assert result["totalSize"] == 2
    assert [m["title"] for m in result["Metadata"]] == ["Season 1", "Season 2"]
    # Seasons have their own children, so their key also points at /children.
    assert result["Metadata"][0]["key"] == f"{URL_PREFIX_METADATA}/season-1/children"


def test_get_children_of_season_returns_episodes():
    _, _, _, metadata_service = _services()
    result = metadata_service.get_children("season-1")
    assert result["totalSize"] == 3
    assert [m["title"] for m in result["Metadata"]] == ["S1E1", "S1E2", "S1E3"]
    assert result["Metadata"][0]["key"] == f"{URL_PREFIX_METADATA}/s1e1"


def test_get_children_paginates():
    _, _, _, metadata_service = _services()
    result = metadata_service.get_children("season-1", offset=1, limit=1)
    assert result["offset"] == 1
    assert result["totalSize"] == 3
    assert result["size"] == 1
    assert result["Metadata"][0]["title"] == "S1E2"


def test_get_children_of_unknown_ratingkey_raises_not_found():
    _, _, _, metadata_service = _services()
    try:
        metadata_service.get_children("nope")
        assert False, "expected NotFoundError"
    except NotFoundError:
        pass


def test_get_grandchildren_of_show_flattens_episodes():
    _, _, _, metadata_service = _services()
    result = metadata_service.get_grandchildren("show-1")
    assert result["totalSize"] == 5
    titles = [m["title"] for m in result["Metadata"]]
    assert titles == ["S1E1", "S1E2", "S1E3", "S2E1", "S2E2"]


def test_get_grandchildren_of_season_is_empty():
    _, _, _, metadata_service = _services()
    result = metadata_service.get_grandchildren("season-1")
    assert result["totalSize"] == 0
    assert result["Metadata"] == []


def test_include_children_embeds_children_object():
    _, _, _, metadata_service = _services()
    result = metadata_service.get_metadata("show-1", include_children=True)
    show = result["Metadata"][0]
    assert show["Children"]["size"] == 2
    assert [c["title"] for c in show["Children"]["Metadata"]] == ["Season 1", "Season 2"]


def test_include_children_omitted_for_movie_type():
    """A source without get_children shouldn't 404/error when
    includeChildren=1 is passed for a type with no children."""
    class FakeMovieClient:
        name = "fake_movie"

        def search(self, query, ignore_score, skip=0, genre=None):
            return []

        def get_entry(self, rating_key):
            return SourceMetadata(upstream_id="movie-1", title="A Movie", metadata_type=MetadataType.MOVIE)

    search = SearchService(clients=[FakeMovieClient()])
    mapper = Mapper(scheme=SCHEME)
    metadata_service = MetadataService(search=search, mapper=mapper)

    result = metadata_service.get_metadata("movie-1", include_children=True)
    assert "Children" not in result["Metadata"][0]


# ----------------------------------------------------------------------
# MatchService: includeChildren
# ----------------------------------------------------------------------
def test_match_include_children_embeds_children_for_show():
    client, search, mapper, _ = _services()
    # type 1/"movie" is this provider's only category (see
    # app/helper/constants.py) - the *entry* returned by FakeShowClient is
    # still a Show/Season, which is what _maybe_attach_children keys off of.
    match_service = MatchService(
        search_by_category={"movie": search},
        mapper=mapper,
        thresholds=MatchThresholds(exact_minimum=50, manual_minimum=30),
        children_search=search,
    )

    result = match_service.match({"type": 1, "title": "Test Show", "includeChildren": 1})
    matched = result["Metadata"][0]
    assert matched["Children"]["size"] == 2


def test_match_without_include_children_has_no_children_key():
    client, search, mapper, _ = _services()
    # type 1/"movie" is this provider's only category (see
    # app/helper/constants.py) - the *entry* returned by FakeShowClient is
    # still a Show/Season, which is what _maybe_attach_children keys off of.
    match_service = MatchService(
        search_by_category={"movie": search},
        mapper=mapper,
        thresholds=MatchThresholds(exact_minimum=50, manual_minimum=30),
        children_search=search,
    )

    result = match_service.match({"type": 1, "title": "Test Show"})
    matched = result["Metadata"][0]
    assert "Children" not in matched
