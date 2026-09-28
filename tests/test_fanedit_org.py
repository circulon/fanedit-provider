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
<div class="jrOverallRatings">
  <div class="jrOverallEditor" title="Trusted Reviewer rating"><span class="jrRatingValue"><span>9.4</span> <span class="rating_count">(<span class="count">2</span>)</span></span></div>
  <div class="jrOverallUser" title="User rating"><span class="jrRatingValue"><span>8.8</span><span class="jrReviewCount"> (<span class="count">5</span>)</span></span></div>
</div>
<div class="jrListingMainImage"><a href="/img/full.jpg"><img src="data:image/gif;base64,R0lGODlhAQABAIAAAP///wAAACH5BAEAAAAALAAAAAABAAEAAAICRAEAOw==" data-jr-src="/img/thumb.jpg"></a></div>
<div class="jrCustomFields">
  <div class="jrFaneditorname"><div class="jrFieldValue"><ul><li>Harmy</li></ul></div></div>
  <div class="jrOriginalmovietitle"><div class="jrFieldValue"><ul><li>Star Wars</li></ul></div></div>
  <div class="jrGenre"><div class="jrFieldValue"><ul><li>Adventure</li><li>Science Fiction</li></ul></div></div>
  <div class="jrFanedittype"><div class="jrFieldValue"><a>Despecialized</a></div></div>
  <div class="jrFaneditreleasedate"><div class="jrFieldValue"><a>March 2020</a></div></div>
  <div class="jrFaneditrunningtimemin"><div class="jrFieldValue">121 minutes</div></div>
  <div class="jrAdditionallinks"><div class="jrFieldValue"><a href="https://www.imdb.com/title/tt0076759/"><img src="imdb.png"></a></div></div>
</div>
<div id="changes"><div class="jrCustomFields"><div class="jrFieldGroup changes">
  <div class="jrEditingdetails jrFieldRow"><div class="jrFieldLabel">Editing Details:</div><div class="jrFieldValue">Removed CGI Jabba scene<br />
Restored original Han-shoots-first edit</div></div>
  <div class="jrCutlist jrFieldRow"><div class="jrFieldLabel">Cuts and Additions:</div><div class="jrFieldValue">Trimmed the Special Edition inserts.<br />
<br />
Restored the theatrical color grade.</div></div>
</div></div></div>
<div id="photoTab"><div class="jrThumbGallery"><div class="jrMediaThumb">
  <a href="/img/coverart.jpg" class="fancybox" rel="gallery" title="coverart"><img src="/img/coverart-thumb.jpg"></a>
</div></div></div>
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
    assert metadata["Genre"] == [{"tag": "Adventure"}, {"tag": "Science Fiction"}]
    assert metadata["duration"] == 121 * 60 * 1000
    assert metadata["Guid"] == [{"id": "imdb://tt0076759"}]
    assert metadata["Rating"] == [
        {"value": 9.4, "type": "critic", "image": "themoviedb://image.rating"},
        {"value": 8.8, "type": "audience", "image": "themoviedb://image.rating"},
    ]
    assert metadata["Image"] == [
        {"type": "coverPoster", "url": "/img/full.jpg", "alt": "Star Wars: Despecialized Edition"},
        {"type": "coverPoster", "url": "/img/coverart.jpg", "alt": "Star Wars: Despecialized Edition"},
    ]
    # "Changes from the original" has no dedicated Plex field - it's
    # carried via SourceMetadata.summary_extra and appended to the plain
    # synopsis by app/helper/mapper.py (see TestConfig.INCLUDE_EXTRA_IN_SUMMARY).
    # Each labeled field from the #changes tab becomes its own sub-item,
    # with a blank line between them for Plex summary readability.
    assert metadata["summary"] == (
        "A cleaned-up fan restoration.\n\n"
        "Changes:\n\n"
        "Editing Details:\n"
        "Removed CGI Jabba scene\n"
        "Restored original Han-shoots-first edit\n\n"
        "Cuts and Additions:\n"
        "Trimmed the Special Edition inserts.\n\n"
        "Restored the theatrical color grade."
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


# ----------------------------------------------------------------------
# Upstream failures
# ----------------------------------------------------------------------
def test_detail_page_outage_is_503_and_not_cached():
    client = _client()
    with respx.mock:
        respx.get(DETAIL_URL).mock(side_effect=httpx.ConnectError("down"))
        resp = client.get(f"{URL_PREFIX_METADATA}/{RATING_KEY}")
    assert resp.status_code == 503

    # Site back up: the outage wasn't cached as "not found".
    with respx.mock:
        respx.get(DETAIL_URL).mock(return_value=httpx.Response(200, html=DETAIL_PAGE_HTML))
        resp = client.get(f"{URL_PREFIX_METADATA}/{RATING_KEY}")
    assert resp.status_code == 200


def test_detail_page_server_error_is_503():
    with respx.mock:
        respx.get(DETAIL_URL).mock(return_value=httpx.Response(502))
        resp = _client().get(f"{URL_PREFIX_METADATA}/{RATING_KEY}")
    assert resp.status_code == 503


def test_detail_page_404_is_404():
    with respx.mock:
        respx.get(DETAIL_URL).mock(return_value=httpx.Response(404))
        resp = _client().get(f"{URL_PREFIX_METADATA}/{RATING_KEY}")
    assert resp.status_code == 404


def test_search_outage_is_503():
    with respx.mock:
        respx.get(SEARCH_URL).mock(side_effect=httpx.ConnectTimeout("slow"))
        resp = _client().post(URL_PREFIX_MATCHES, json={"type": 1, "title": "Star Wars Despecialized"})
    assert resp.status_code == 503


def test_auto_and_manual_match_share_one_upstream_search():
    client = _client()
    with respx.mock:
        route = respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, html=SEARCH_RESULTS_HTML))
        auto = client.post(URL_PREFIX_MATCHES, json={"type": 1, "title": "Star Wars: Despecialized Edition"})
        manual = client.post(
            URL_PREFIX_MATCHES, json={"type": 1, "title": "Star Wars: Despecialized Edition", "manual": 1}
        )
    assert auto.status_code == manual.status_code == 200
    assert auto.get_json()["MediaContainer"]["size"] == 1
    assert manual.get_json()["MediaContainer"]["size"] == 1
    assert route.call_count == 1
