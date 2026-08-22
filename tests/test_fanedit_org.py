"""
Integration tests for movie source fanedit_org, using respx to mock
fanedit.org's HTML responses. IFDB only enables a movie source
(MOVIE_SOURCES=["fanedit_org"] - see TestConfig in app/helper/config.py), so
there's no show/tvmaze source to exercise here.
"""
import httpx
import respx

from app import create_app
from app.helper.config import TestConfig
from app.helper.constants import URL_PREFIX_MATCHES, URL_PREFIX_METADATA

SEARCH_URL = "https://fanedit.org/fanedit-search/search-results/"


def _client():
    app = create_app(TestConfig)
    app.testing = True
    return app.test_client()


SEARCH_RESULTS_HTML = """
<html><body>
<div id="jr-pagenav-ajax">
  <div>
    <div class="jrListingThumbnail"><img data-jr-src="/img/thumb1.jpg"></div>
    <div class="jrListingTitle"><a href="/ifdb/star-wars-despecialized/">Star Wars: Despecialized Edition</a></div>
    <div class="jrFaneditreleasedate"><div class="jrFieldValue"><a>March 2020</a></div></div>
  </div>
</div>
</body></html>
"""

DETAIL_PAGE_HTML = """
<html><body>
<div id="primary"><div>
<h1><span itemprop="headline">Star Wars: Despecialized Edition</span></h1>
<div class="jrListingMainImage"><a href="/img/full.jpg"><img src="/img/thumb.jpg"></a></div>
<div class="jrCustomFields">
  <div class="jrFaneditorname"><div class="jrFieldValue"><ul><li>Harmy</li></ul></div></div>
  <div class="jrOriginalmovietitle"><div class="jrFieldValue"><ul><li>Star Wars</li></ul></div></div>
  <div class="jrFanedittype"><div class="jrFieldValue"><a>Despecialized</a></div></div>
  <div class="jrFaneditreleasedate"><div class="jrFieldValue"><a>March 2020</a></div></div>
  <div class="jrChangesfromtheoriginal"><div class="jrFieldValue"><ul>
    <li>Removed CGI Jabba scene</li>
    <li>Restored original Han-shoots-first edit</li>
  </ul></div></div>
</div>
</div></div>
<div id="fanedit-info"><div class="jrBriefsynopsis"><div class="jrFieldValue">A cleaned-up fan restoration.</div></div></div>
</body></html>
"""

# base64url(https://fanedit.org/ifdb/star-wars-despecialized/), padding stripped -
# what FaneditOrg._url_to_rating_key derives for DETAIL_URL below, and what
# get_entry() must be able to turn back into that same URL.
DETAIL_URL = "https://fanedit.org/ifdb/star-wars-despecialized/"
RATING_KEY = "aHR0cHM6Ly9mYW5lZGl0Lm9yZy9pZmRiL3N0YXItd2Fycy1kZXNwZWNpYWxpemVkLw"


def test_movie_match_round_trip():
    with respx.mock(assert_all_called=True) as mock:
        mock.get(SEARCH_URL).mock(return_value=httpx.Response(200, html=SEARCH_RESULTS_HTML))

        resp = _client().post(
            URL_PREFIX_MATCHES, json={"type": 1, "title": "Star Wars: Despecialized Edition"}
        )

    assert resp.status_code == 200
    metadata = resp.get_json()["MediaContainer"]["Metadata"]
    assert len(metadata) == 1
    assert metadata[0]["title"] == "Star Wars: Despecialized Edition"
    assert metadata[0]["type"] == "movie"
    assert metadata[0]["ratingKey"] == RATING_KEY
    assert metadata[0]["guid"] == f"{TestConfig.PROVIDER_IDENTIFIER}://movie/{RATING_KEY}"


def test_movie_full_metadata_round_trip():
    with respx.mock(assert_all_called=True) as mock:
        mock.get(DETAIL_URL).mock(return_value=httpx.Response(200, html=DETAIL_PAGE_HTML))

        resp = _client().get(f"{URL_PREFIX_METADATA}/{RATING_KEY}")

    assert resp.status_code == 200
    metadata = resp.get_json()["MediaContainer"]["Metadata"][0]
    assert metadata["title"] == "Star Wars: Despecialized Edition"
    assert metadata["Director"] == [{"tag": "Harmy", "role": "Fan Editor"}]
    assert metadata["originalTitle"] == "Star Wars"
    # "Changes from the original" has no dedicated Plex field - it's
    # carried via SourceMetadata.summary_extra and appended to the plain
    # synopsis by app/helper/mapper.py (see TestConfig.INCLUDE_EXTRA_IN_SUMMARY).
    assert metadata["summary"] == (
        "A cleaned-up fan restoration.\n\n"
        "Changes:\n"
        "- Removed CGI Jabba scene\n"
        "- Restored original Han-shoots-first edit"
    )


def test_show_type_request_is_400():
    """FanEdit.org has no show source enabled - a show-typed match request should
    be rejected rather than silently returning nothing."""
    resp = _client().post(URL_PREFIX_MATCHES, json={"type": 2, "title": "archer"})
    assert resp.status_code == 400


def test_no_results_returns_empty_metadata():
    with respx.mock(assert_all_called=True) as mock:
        mock.get(SEARCH_URL).mock(return_value=httpx.Response(200, html="<html><body></body></html>"))

        resp = _client().post(URL_PREFIX_MATCHES, json={"type": 1, "title": "Nonexistent Fanedit"})

    assert resp.status_code == 200
    assert resp.get_json()["MediaContainer"]["Metadata"] == []
