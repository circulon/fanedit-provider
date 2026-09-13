"""
HTML scrape of fanedit.org pages for movie metadata.

This module owns both the scraping mechanics and the translation to
SourceMetadata:

  1. ``search()`` - scrapes fanedit.org's search results page for candidate
     entries: url, title, and whatever else is visible right there in the
     listing (poster thumbnail, release year) without needing a second
     request, pre-filtered by ``ignore_score`` before being translated -
     see app/service/search.py, which relies on that pre-filtering to avoid
     resolving every candidate's full metadata eagerly.
  2. ``get_entry()`` - resolves a single candidate's full metadata by
     deriving the fanedit.org URL back out of the ratingKey (see
     ``_rating_key_to_url`` below) and scraping that entry's detail page
     directly.

Scraping is inherently more fragile than a JSON API would be. If
fanedit.org's markup or URL structure changes, only this module should
need updating.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
import logging
import re
from typing import Any
from urllib.parse import urljoin

import httpx
from lxml import html as lxml_html

from app.client.base import ImageEntry, PersonEntry, RatingEntry, SourceMetadata
from app.schema.plex import MetadataType
from app.util.scoring import title_match_score
from app.util.text_utils import parse_flexible_date

logger = logging.getLogger(__name__)

# Detail pages live under /ifdb/<slug>/ - used by _extract_generic's
# fallback link scan (see _url_to_rating_key's docstring for the inverse).
_DETAIL_PAGE_HREF_RE = re.compile(r"/ifdb/[^/]+/?$")

# An IMDb title id, e.g. "tt0088763". Pulled out of the Additional Links
# field by regex rather than by parsing the href as a URL, since some
# entries' Amazon/IMDb links are malformed (duplicated/nested URLs) - the
# id itself is still intact wherever it appears.
_IMDB_ID_RE = re.compile(r"tt\d{7,9}")

# The first run of digits in a runtime string, e.g. "102 minutes" -> 102.
_MINUTES_RE = re.compile(r"\d+")


@dataclass
class FaneditOrgConfig:
    match_type: str = "all"  # all | any | exact - fanedit.org's own keyword-match mode
    timeout: float = 25.0
    user_agent: str = "Provider/1.0"
    max_candidates: int = 25


class FaneditOrg:
    """SourceClient for a best-effort scrape of fanedit.org (see
    app/client/base.py.SourceClient for the interface this implements).
    Never raises - a broken scrape/request degrades to "found nothing"
    (empty list / None) rather than breaking the request, since scraping is
    inherently more fragile than a JSON API would be (see the module
    docstring)."""

    name = "fanedit_org"
    base_url: str = "https://fanedit.org"
    search_path: str = "/fanedit-search/search-results/"

    def __init__(self, config: FaneditOrgConfig | None = None, client: httpx.Client | None = None):
        self.config = config or FaneditOrgConfig()
        self.client = client or httpx.Client()

    # ------------------------------------------------------------------
    # SourceClient interface
    # ------------------------------------------------------------------
    def search(
        self, query: str, ignore_score: int, skip: int = 0, genre: str | None = None
    ) -> list[SourceMetadata]:
        try:
            candidates = self._scrape_search_candidates(query)
        except Exception as exc:  # noqa: BLE001 - a broken scrape shouldn't break the request
            logger.warning("fanedit.org search failed for %r: %s", query, exc, exc_info=True)
            return []

        matching = [
            c for c in candidates
            if title_match_score(query, c.get("title") or "") >= ignore_score
        ]
        logger.info(
            "fanedit.org search for %r found %d candidate(s), %d passed the title pre-filter",
            query, len(candidates), len(matching),
        )
        return [e for e in (self._normalize_candidate(c) for c in matching) if e is not None]

    def get_entry(self, rating_key: str) -> SourceMetadata | None:
        try:
            url = self._rating_key_to_url(rating_key)
        except Exception as exc:  # noqa: BLE001 - a malformed ratingKey shouldn't break the request
            logger.warning("Could not derive a fanedit.org URL from ratingKey %r: %s", rating_key, exc)
            return None

        try:
            detail = self._scrape_entry_detail(url)
        except Exception as exc:  # noqa: BLE001 - a broken scrape shouldn't break the request
            logger.warning("fanedit.org detail page scrape failed for %r: %s", url, exc, exc_info=True)
            return None

        if detail is None:
            return None
        return self._normalize_detail(rating_key, detail)

    # ------------------------------------------------------------------
    # Raw HTTP + scraping mechanics
    # ------------------------------------------------------------------
    def _scrape_search_candidates(self, query: str) -> list[dict[str, Any]]:
        """Returns candidate dicts: ``{"url", "title"}`` always present;
        ``"thumb"`` and ``"year"`` present when the structured strategy finds
        them (never present from the generic strategy).

        fanedit.org redirects straight to the entry's own detail page
        (302) instead of showing a results listing when a search has exactly
        one exact-title match - handled below by following redirects and
        checking where we actually landed, rather than treating the redirect
        as a request failure."""
        url = f"{self.base_url.rstrip('/')}{self.search_path}"
        params = {"query": self.config.match_type, "scope": "title", "keywords": query, "order": "rdate"}

        try:
            resp = self.client.get(
                url,
                params=params,
                headers={"User-Agent": self.config.user_agent},
                timeout=self.config.timeout,
                follow_redirects=True,
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("fanedit.org search request failed for %r: %s", query, exc)
            return []

        try:
            root = lxml_html.fromstring(resp.content)
        except Exception as exc:  # noqa: BLE001 - malformed HTML shouldn't break the request
            logger.warning("fanedit.org search returned unparseable HTML for %r: %s", query, exc)
            return []

        final_url = str(resp.url)
        if self.search_path in final_url:
            candidates = self._extract_structured(root, final_url)
            if not candidates:
                candidates = self._extract_generic(root, final_url)
        else:
            # A single exact match redirected straight to the entry's own
            # detail page rather than showing a results listing.
            logger.info(
                "fanedit.org search for %r redirected straight to a single result: %r",
                query, final_url,
            )
            candidate = self._candidate_from_detail_redirect(root, final_url)
            candidates = [candidate] if candidate else []

        deduped: dict[str, dict[str, Any]] = {}
        for candidate in candidates:
            deduped.setdefault(candidate["url"], candidate)
            if len(deduped) >= self.config.max_candidates:
                break

        results = list(deduped.values())
        logger.info("fanedit.org search for %r found %d candidate(s)", query, len(results))
        return results

    def _scrape_entry_detail(self, url: str) -> dict[str, Any] | None:
        """Scrapes a single entry's fanedit.org detail page directly.
        Returns a dict of raw scraped fields (see _extract_detail), or None
        if the page couldn't be fetched/parsed."""
        try:
            resp = self.client.get(
                url,
                headers={"User-Agent": self.config.user_agent},
                timeout=self.config.timeout,
                follow_redirects=True,
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("fanedit.org detail page request failed for %r: %s", url, exc)
            return None

        try:
            root = lxml_html.fromstring(resp.content)
        except Exception as exc:  # noqa: BLE001
            logger.warning("fanedit.org detail page returned unparseable HTML for %r: %s", url, exc)
            return None

        try:
            return self._extract_detail(root)
        except Exception as exc:  # noqa: BLE001 - a parsing surprise shouldn't break the request
            logger.warning("fanedit.org detail page extraction failed for %r: %s", url, exc, exc_info=True)
            return None

    # ------------------------------------------------------------------
    # search() extraction strategies
    # ------------------------------------------------------------------
    def _extract_structured(self, root, page_url: str) -> list[dict[str, Any]]:
        """Mirrors fanedit.org's known-good listing markup (jrListingTitle
        etc.). Also pulls the thumbnail and release year, which are visible
        right there in the listing without a second request."""
        try:
            entry_nodes = root.xpath(
                '//*[@id="jr-pagenav-ajax"]//div[contains(@class,"jrListingTitle")]/../..'
            )
            results = []
            for entry in entry_nodes:
                href = entry.xpath('string(.//div[contains(@class,"jrListingTitle")]/a/@href)').strip()
                title = entry.xpath('string(.//div[contains(@class,"jrListingTitle")]//a/text())').strip()
                if not href or not title:
                    continue

                candidate: dict[str, Any] = {"url": urljoin(page_url, href), "title": title}

                thumb = entry.xpath(
                    'string(.//div[contains(@class,"jrListingThumbnail")]//img/@data-jr-src)'
                ).strip()
                if thumb:
                    candidate["thumb"] = urljoin(page_url, thumb)

                release_date_str = entry.xpath(
                    'string(.//div[contains(@class,"jrFaneditreleasedate")]'
                    '//div[contains(@class,"jrFieldValue")]//a/text())'
                ).strip()
                year = self._safe_parse_year(release_date_str)
                if year:
                    candidate["year"] = year

                results.append(candidate)
            return results
        except Exception as exc:  # noqa: BLE001
            logger.debug("Structured data extraction failed: %s", exc, exc_info=True)
            return []

    @staticmethod
    def _extract_generic(root, page_url: str) -> list[dict[str, Any]]:
        """Falls back to a plain link-pattern scan if the structured markup
        isn't there - more resilient to theme/class-name changes, less
        resilient to URL-structure changes. Can only recover url/title, not
        thumbnail/year."""
        try:
            results = []
            for anchor in root.xpath("//a[@href]"):
                href = (anchor.get("href") or "").strip()
                if not href or not _DETAIL_PAGE_HREF_RE.search(href):
                    continue
                title = anchor.xpath("string(.)").strip()
                if not title:
                    continue
                results.append({"url": urljoin(page_url, href), "title": title})
            return results
        except Exception as exc:  # noqa: BLE001
            logger.debug("Generic data extraction failed: %s", exc, exc_info=True)
            return []

    def _candidate_from_detail_redirect(self, root, page_url: str) -> dict[str, Any] | None:
        """A single exact match on fanedit.org redirects straight from the
        search results page to the entry's own detail page instead of
        showing a listing (see _scrape_search_candidates's docstring).
        Reuses the detail-page extraction (_extract_detail) and reshapes it
        into the same lightweight candidate shape a normal listing row
        would produce, so the rest of _scrape_search_candidates (dedup,
        max_candidates) doesn't need to care which case it was."""
        try:
            detail = self._extract_detail(root)
        except Exception as exc:  # noqa: BLE001 - a parsing surprise shouldn't break the request
            logger.warning("Detail-redirect extraction failed for %r: %s", page_url, exc, exc_info=True)
            return None

        if detail is None or not detail.get("title"):
            return None

        candidate: dict[str, Any] = {"url": page_url, "title": detail["title"]}
        if detail.get("thumbnailUrl"):
            candidate["thumb"] = detail["thumbnailUrl"]
        release_date = detail.get("releaseDate")
        if release_date:
            # _extract_detail() already produces an ISO "YYYY-MM-DD" string
            # (not the raw scraped text _safe_parse_year expects), so just
            # pull the leading year out directly.
            try:
                candidate["year"] = int(release_date[:4])
            except (TypeError, ValueError):
                pass
        return candidate

    # ------------------------------------------------------------------
    # get_entry() detail-page extraction
    # ------------------------------------------------------------------
    @staticmethod
    def _extract_detail(root) -> dict[str, Any] | None:
        """Scrapes a fanedit.org detail page into a small dict of raw
        scraped fields, which ``_normalize_detail`` below translates into
        ``SourceMetadata``."""
        banner_nodes = root.xpath('//div[@id="primary"]/div')
        if not banner_nodes:
            return None
        banner_node = banner_nodes[0]

        title = banner_node.xpath('string(//h1/span[@itemprop="headline"]/text())').strip()
        if not title:
            return None

        fields_nodes = banner_node.xpath(
            './/div[contains(@class,"jrCustomFields")]//div[contains(@class,"jrFaneditorname")]/../..'
        )
        fields_node = fields_nodes[0] if fields_nodes else banner_node

        entry: dict[str, Any] = {"title": title}

        poster_url = root.xpath(
            'string(//div[contains(@class,"jrListingMainImage")]//a/@href)'
        ).strip()
        # This image is lazy-loaded: @src is a placeholder GIF and the real
        # URL is in @data-jr-src (see also _extract_structured's thumbnail
        # lookup, which uses data-jr-src for the same reason).
        thumbnail_url = banner_node.xpath(
            'string(//div[contains(@class,"jrListingMainImage")]/a//img/@data-jr-src)'
        ).strip()
        thumb = poster_url or thumbnail_url
        if thumb:
            entry["thumbnailUrl"] = thumb

        fan_editors = fields_node.xpath(
            './/div[contains(@class,"jrFaneditorname")]//div[contains(@class,"jrFieldValue")]//li//text()'
        )
        fan_editors = [str(v).strip() for v in fan_editors if str(v).strip()]
        if fan_editors:
            entry["faneditorName"] = fan_editors

        original_titles = fields_node.xpath(
            './/div[contains(@class,"jrOriginalmovietitle")]//div[contains(@class,"jrFieldValue")]//li//text()'
        )
        original_titles = [str(v).strip() for v in original_titles if str(v).strip()]
        if original_titles:
            entry["originalMovieTitles"] = original_titles

        genres = fields_node.xpath(
            './/div[contains(@class,"jrGenre")]//div[contains(@class,"jrFieldValue")]//li//text()'
        )
        genres = [str(g).strip() for g in genres if str(g).strip()]
        if genres:
            entry["genres"] = genres

        release_date_str = fields_node.xpath(
            'string(.//div[contains(@class,"jrFaneditreleasedate")]//div[contains(@class,"jrFieldValue")]//a/text())'
        ).strip()
        release_date = FaneditOrg._safe_parse_date(release_date_str)
        if release_date:
            entry["releaseDate"] = release_date.isoformat()

        runtime_str = fields_node.xpath(
            'string(.//div[contains(@class,"jrFaneditrunningtimemin")]//div[contains(@class,"jrFieldValue")])'
        ).strip()
        runtime_minutes = FaneditOrg._parse_minutes(runtime_str)
        if runtime_minutes is not None:
            entry["runtimeMinutes"] = runtime_minutes

        # The IMDb id is pulled by regex from the whole Additional Links
        # field rather than a specific href, since some entries' links are
        # malformed - see _IMDB_ID_RE.
        additional_links_nodes = fields_node.xpath(
            './/div[contains(@class,"jrAdditionallinks")]//div[contains(@class,"jrFieldValue")]'
        )
        if additional_links_nodes:
            links_html = lxml_html.tostring(additional_links_nodes[0], encoding="unicode")
            imdb_match = _IMDB_ID_RE.search(links_html)
            if imdb_match:
                entry["imdbId"] = imdb_match.group(0)

        ratings_nodes = root.xpath('//div[contains(@class,"jrOverallRatings")]')
        if ratings_nodes:
            editor_rating = FaneditOrg._extract_rating(ratings_nodes[0], "jrOverallEditor")
            if editor_rating is not None:
                entry["editorRating"] = editor_rating
            user_rating = FaneditOrg._extract_rating(ratings_nodes[0], "jrOverallUser")
            if user_rating is not None:
                entry["userRating"] = user_rating

        # Additional poster/cover art beyond the main listing image: the
        # Coverart photo gallery (when present) plus a standalone cover-art
        # field some entries have instead. De-duped against each other and
        # against the main poster.
        extra_image_urls: list[str] = []
        # Structural match (an <a> directly wrapping an <img>) rather than
        # requiring a specific lightbox-plugin class, which is a theme/JS
        # choice rather than part of the underlying field data.
        extra_image_urls.extend(
            str(h).strip()
            for h in root.xpath('//div[@id="photoTab"]//a[img]/@href')
            if str(h).strip()
        )
        extra_image_urls.extend(
            str(h).strip()
            for h in root.xpath(
                '//div[contains(@class,"jrCoverart")]//div[contains(@class,"jrFieldValue")]//img/@src'
            )
            if str(h).strip()
        )
        seen_images = {thumb} if thumb else set()
        deduped_extra_images = []
        for url in extra_image_urls:
            if url not in seen_images:
                seen_images.add(url)
                deduped_extra_images.append(url)
        if deduped_extra_images:
            entry["extraImageUrls"] = deduped_extra_images

        # Teasers/trailers embedded as YouTube iframes in the Video tab,
        # each usually preceded by a <b> label ("Teaser", "Trailer 1", ...).
        trailers: list[dict[str, str]] = []
        for i, iframe in enumerate(
            root.xpath('//div[@id="video"]//iframe[contains(@src,"youtube.com/embed")]'), start=1
        ):
            src = (iframe.get("src") or "").strip()
            if not src:
                continue
            label_nodes = iframe.xpath("preceding::b[1]")
            label = label_nodes[0].text_content().strip() if label_nodes else ""
            trailers.append({"title": label or f"Trailer {i}", "url": src})
        if trailers:
            entry["trailers"] = trailers

        fanedit_info_nodes = root.xpath('//div[@id="fanedit-info"]')
        if fanedit_info_nodes:
            synopsis = fanedit_info_nodes[0].xpath(
                'string(.//div[contains(@class,"jrBriefsynopsis")]//div[contains(@class,"jrFieldValue")]/text())'
            ).strip()
            if synopsis:
                entry["synopsis"] = synopsis

        # Each labeled row inside #changes (e.g. "Editing Details:", "Cuts
        # and Additions:") becomes its own {label, value} item so
        # _normalize_detail can render them as individual sub-entries.
        changes: list[dict[str, str]] = []
        changes_nodes = root.xpath('//div[@id="changes"]')
        if changes_nodes:
            change_rows = changes_nodes[0].xpath('.//div[contains(@class,"jrFieldRow")]')
            for row in change_rows:
                label_nodes = row.xpath('.//div[contains(@class,"jrFieldLabel")]')
                value_nodes = row.xpath('.//div[contains(@class,"jrFieldValue")]')
                if not value_nodes:
                    continue
                label = label_nodes[0].text_content().strip() if label_nodes else ""
                value = FaneditOrg._field_value_text(value_nodes[0])
                if value:
                    changes.append({"label": label, "value": value})
        if changes:
            entry["changes"] = changes

        return entry

    # Sentinel marking a <br> position, substituted before parsing so it
    # survives as a single non-whitespace character _field_value_text can
    # split on - distinct from any literal whitespace/newlines already
    # present in the field's stored HTML.
    _BR_TOKEN = "\uE000"

    @staticmethod
    def _field_value_text(value_node) -> str:
        """Renders a jrFieldValue node as plain text, treating only literal
        <br> tags as line breaks - other whitespace (including incidental
        newlines already present in the stored HTML) collapses the way a
        browser renders inline text. Consecutive <br>s collapse to a single
        blank line; leading/trailing blank lines are trimmed."""
        html_str = lxml_html.tostring(value_node, encoding="unicode")
        html_str = re.sub(r"<br\s*/?>", FaneditOrg._BR_TOKEN, html_str, flags=re.IGNORECASE)
        text = lxml_html.fromstring(html_str).text_content()
        text = re.sub(r"\s+", " ", text)

        cleaned: list[str] = []
        for seg in text.split(FaneditOrg._BR_TOKEN):
            seg = seg.strip()
            if seg:
                cleaned.append(seg)
            elif cleaned and cleaned[-1] != "":
                cleaned.append("")
        while cleaned and cleaned[-1] == "":
            cleaned.pop()
        while cleaned and cleaned[0] == "":
            cleaned.pop(0)
        return "\n".join(cleaned)

    @staticmethod
    def _parse_minutes(text: str) -> int | None:
        """The leading integer out of a runtime string, e.g. "102 minutes"
        -> 102, or None if there isn't one."""
        if not text:
            return None
        match = _MINUTES_RE.search(text)
        return int(match.group(0)) if match else None

    @staticmethod
    def _extract_rating(ratings_node, class_name: str) -> float | None:
        """The numeric rating value inside one of the Trusted
        Reviewer/User rating blocks (``class_name`` is "jrOverallEditor" or
        "jrOverallUser"), or None if that block has no reviews yet. A
        review count of 0 is only ever rendered as a bare "0.0" with no
        review-count element at all, so a missing/zero count is treated as
        "no rating" rather than a real 0.0 score."""
        nodes = ratings_node.xpath(f'.//div[contains(@class,"{class_name}")]')
        if not nodes:
            return None
        count_str = nodes[0].xpath('string(.//span[@class="count"])').strip()
        try:
            count = int(count_str)
        except ValueError:
            count = 0
        if count <= 0:
            return None
        value_str = nodes[0].xpath('string(.//span[contains(@class,"jrRatingValue")]/span[1])').strip()
        try:
            return float(value_str)
        except ValueError:
            return None

    # ------------------------------------------------------------------
    @staticmethod
    def _safe_parse_date(date_str: str):
        if not date_str:
            return None
        try:
            return parse_flexible_date(date_str)
        except ValueError:
            return None

    @staticmethod
    def _safe_parse_year(date_str: str) -> int | None:
        parsed = FaneditOrg._safe_parse_date(date_str)
        return parsed.year if parsed else None

    # ------------------------------------------------------------------
    # Translation from fanedit.org's own scraped-dict shapes into the
    # shared SourceMetadata shape - see app/client/base.py's module docstring.
    # ------------------------------------------------------------------
    def _normalize_candidate(self, candidate: dict[str, Any]) -> SourceMetadata | None:
        """A fanedit.org search-listing row: url/title always present,
        thumb/year present only when the structured scrape strategy found
        them."""
        url = candidate.get("url")
        title = candidate.get("title")
        if not url or not title:
            return None

        entry = SourceMetadata(
            upstream_id=self._url_to_rating_key(url), title=title, metadata_type=MetadataType.MOVIE
        )
        if candidate.get("thumb"):
            entry.thumb = candidate["thumb"]
        if candidate.get("year"):
            entry.year = candidate["year"]
        return entry

    @staticmethod
    def _normalize_detail(rating_key: str, detail: dict[str, Any]) -> SourceMetadata:
        """A fanedit.org detail-page scrape (see _extract_detail), translated
        into ``SourceMetadata``. Deliberately narrower than what the site
        actually shows: tagline, franchise, and the *original* movie's own
        release date have no clean home in SourceMetadata and are left out
        rather than guessed at."""
        entry = SourceMetadata(
            upstream_id=rating_key,
            title=detail.get("title") or "",
            metadata_type=MetadataType.MOVIE,
            originally_available_at=detail.get("releaseDate"),
            summary=(detail.get("synopsis") or "").strip() or None,
        )

        original_titles = [t for t in (detail.get("originalMovieTitles") or []) if t]
        if original_titles:
            entry.original_title = ", ".join(original_titles)

        genres = [g for g in (detail.get("genres") or []) if g]
        if genres:
            entry.genres = genres

        runtime_minutes = detail.get("runtimeMinutes")
        if runtime_minutes is not None:
            entry.duration_ms = runtime_minutes * 60_000

        imdb_id = detail.get("imdbId")
        if imdb_id:
            entry.external_ids = [f"imdb://{imdb_id}"]

        # Trusted Reviewer ratings are fanedit.org's curated/vetted review
        # tier, so they map to Plex's "critic" type; plain User ratings map
        # to "audience". Plex requires an image badge on each Rating entry;
        # fanedit.org has no rating-badge asset of its own, so both use the
        # generic themoviedb badge.
        ratings: list[RatingEntry] = []
        editor_rating = detail.get("editorRating")
        if editor_rating is not None:
            ratings.append(RatingEntry(value=editor_rating, type="critic", image="themoviedb://image.rating"))
        user_rating = detail.get("userRating")
        if user_rating is not None:
            ratings.append(RatingEntry(value=user_rating, type="audience", image="themoviedb://image.rating"))
        if ratings:
            entry.ratings = ratings

        thumb = detail.get("thumbnailUrl")
        if thumb:
            entry.thumb = thumb
            entry.images = [ImageEntry(type="coverPoster", url=thumb, alt=entry.title)]
        for extra_url in detail.get("extraImageUrls") or []:
            entry.images.append(ImageEntry(type="coverPoster", url=extra_url, alt=entry.title))

        trailers = [t for t in (detail.get("trailers") or []) if t.get("url")]
        if trailers:
            entry.extras = [{"url": t["url"], "title": t.get("title") or "Trailer"} for t in trailers]

        fan_editors = [e for e in (detail.get("faneditorName") or []) if e]
        if fan_editors:
            entry.directors = [PersonEntry(tag=editor, role="Fan Editor") for editor in fan_editors]

        # "Changes from the original" has no dedicated field on
        # SourceMetadata - app/client/base.py's ``summary_extra`` exists
        # precisely for source-specific text like this that doesn't fit
        # elsewhere; app/helper/mapper.py appends it to ``summary`` when
        # Config.INCLUDE_EXTRA_IN_SUMMARY is set (see _build_summary).
        #
        # Each item is one labeled field from the #changes tab (e.g.
        # "Editing Details:", "Cuts and Additions:") - rendered here as its
        # own sub-item with a blank line before the next one, so a long
        # Plex summary stays readable instead of running every field
        # together.
        changes = [c for c in (detail.get("changes") or []) if c.get("value")]
        if changes:
            sections = []
            for change in changes:
                label, value = change.get("label"), change["value"]
                sections.append(f"{label}\n{value}" if label else value)
            entry.summary_extra = "Changes:\n\n" + "\n\n".join(sections)

        return entry

    @staticmethod
    def _url_to_rating_key(url: str) -> str:
        """A fanedit.org URL -> Plex-safe ratingKey, by base64-encoding it.
        Used by search() (see _normalize_candidate), which discovers URLs
        directly from the search-results scrape rather than starting from
        an existing upstream id."""
        return base64.urlsafe_b64encode(url.encode("utf-8")).rstrip(b"=").decode("ascii")

    @staticmethod
    def _rating_key_to_url(rating_key: str) -> str:
        """Plex-safe ratingKey -> the original fanedit.org URL it was derived
        from (inverse of url_to_rating_key). Used by get_entry() above to
        resolve a full metadata fetch back to the detail page it came from -
        see SearchService.get_entry() in app/service/search.py."""
        raw = base64.urlsafe_b64decode(FaneditOrg._pad(rating_key))
        return raw.decode("utf-8")

    @staticmethod
    def _pad(b64_str: str) -> str:
        """Re-adds the ``=`` padding base64 requires but a Plex-safe
        ratingKey has had stripped (see _url_to_rating_key above)."""
        return b64_str + ("=" * (-len(b64_str) % 4))
