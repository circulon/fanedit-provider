"""
HTTP routes implementing the Plex Custom Media Provider contract.

Endpoints (see docs/API Endpoints.md and docs/MediaProvider.md in
https://github.com/plexinc/tmdb-example-provider, and
https://developer.plex.tv/pms/#section/API-Info/Metadata-Providers):

  GET  /                                        -> MediaProvider definition
  GET  /metadata/<ratingKey>            -> Metadata feature
                                                    (?includeChildren=1, ?episodeOrder=)
  GET  /metadata/<ratingKey>/children    -> Direct children (Seasons of a
                                                    Show, Episodes of a Season).
                                                    TV Shows/Seasons only. Mandatory
                                                    paging - defaults to 20 items.
  GET  /metadata/<ratingKey>/grandchildren -> Episodes of a Show, flattening
                                                    past Seasons. TV Shows only.
                                                    Mandatory paging - defaults to
                                                    20 items.
  GET  /metadata/<ratingKey>/images     -> Image list for an item
  GET  /metadata/<ratingKey>/extras     -> Extras list for an item
  POST /matches                -> Match feature
                                                    (includeChildren, episodeOrder)
  GET  /health                                  -> Simple liveness check
"""
from __future__ import annotations

import logging

from flask import Blueprint, current_app, jsonify, request
from werkzeug.exceptions import HTTPException

from app.client.base import SourceUnavailableError
from app.helper.constants import (
    PLEX_SUPPORTED_MATCH_TYPES,
    SOURCE_CATEGORY_MATCH_TYPES,
    SOURCE_TYPES,
    URL_PREFIX_MATCHES,
    URL_PREFIX_METADATA,
)
from app.service.match import UnsupportedMatchType
from app.service.metadata import NotFoundError
from app.services import services

bp = Blueprint("ifdb_provider", __name__)
logger = logging.getLogger(__name__)


def _envelope(container: dict) -> dict:
    """Wrap a partial MediaContainer with the provider identifier."""
    full = {"identifier": current_app.config["PROVIDER_IDENTIFIER"], **container}
    return {"MediaContainer": full}


def _header_or_query(name: str, default: str | None = None) -> str | None:
    return request.headers.get(name) or request.args.get(name) or default


def _bool_param(name: str) -> bool:
    """Parses a Plex-style ``1``/``0`` integer boolean query/header param
    (e.g. includeChildren) - true for "1", false for anything else
    (including absent)."""
    raw = _header_or_query(name)
    return raw == "1"


def _paging_params(default_size: int) -> tuple[int, int]:
    """Parse X-Plex-Container-Size/-Start per docs/API Endpoints.md#response-paging."""
    size_raw = _header_or_query("X-Plex-Container-Size")
    start_raw = _header_or_query("X-Plex-Container-Start")

    try:
        size = int(size_raw) if size_raw is not None else default_size
    except ValueError:
        size = default_size

    try:
        start = int(start_raw) if start_raw is not None else 0
    except ValueError:
        start = 0

    return max(size, 0), max(start, 0)


# ----------------------------------------------------------------------
# Provider definition
# ----------------------------------------------------------------------
def _build_provider_response() -> dict:
    """One ``Types`` entry per metadata type actually served - every type
    belonging to an enabled source category, intersected with
    PLEX_SUPPORTED_MATCH_TYPES so an unsupported type is
    never advertised even if enabled locally."""
    cfg = current_app.config
    identifier = cfg["PROVIDER_IDENTIFIER"]
    enabled_categories = services().enabled_categories

    types: list[dict] = []
    for category in SOURCE_TYPES:
        if category not in enabled_categories:
            continue
        match_type = SOURCE_CATEGORY_MATCH_TYPES[category]
        if match_type not in PLEX_SUPPORTED_MATCH_TYPES:
            continue
        types.append({"type": match_type, "Scheme": [{"scheme": identifier}]})

    return {
        "MediaProvider": {
            "identifier": identifier,
            "title": cfg["PROVIDER_TITLE"],
            "version": cfg["PROVIDER_VERSION"],
            "Types": types,
            "Feature": [
                {"type": "metadata", "key": URL_PREFIX_METADATA},
                {"type": "match", "key": URL_PREFIX_MATCHES},
            ],
        }
    }


@bp.get("/")
def get_provider():
    return jsonify(
        _build_provider_response()
    )


@bp.get("/health")
def health():
    return jsonify({"status": "ok"})


# ----------------------------------------------------------------------
# Metadata feature
# ----------------------------------------------------------------------
@bp.get(f"{URL_PREFIX_METADATA}/<rating_key>/images")
def get_images(rating_key: str):
    service = services().metadata
    result = service.get_images(rating_key)
    return jsonify(_envelope(result))


@bp.get(f"{URL_PREFIX_METADATA}/<rating_key>/extras")
def get_extras(rating_key: str):
    service = services().metadata
    result = service.get_extras(rating_key)
    return jsonify(_envelope(result))


@bp.get(f"{URL_PREFIX_METADATA}/<rating_key>/children")
def get_children(rating_key: str):
    """Direct children (Seasons for a Show, Episodes for a Season).
    Mandatory paging - defaults to the first 20 items when no paging
    headers/params are sent."""
    service = services().metadata
    size, start = _paging_params(default_size=current_app.config["DEFAULT_PAGE_SIZE"])
    episode_order = _header_or_query("episodeOrder")
    result = service.get_children(rating_key, offset=start, limit=size, episode_order=episode_order)
    return jsonify(_envelope(result))


@bp.get(f"{URL_PREFIX_METADATA}/<rating_key>/grandchildren")
def get_grandchildren(rating_key: str):
    """Episodes for a Show, flattening past the Season level. Mandatory
    paging - defaults to the first 20 items when no paging headers/params
    are sent."""
    service = services().metadata
    size, start = _paging_params(default_size=current_app.config["DEFAULT_PAGE_SIZE"])
    episode_order = _header_or_query("episodeOrder")
    result = service.get_grandchildren(rating_key, offset=start, limit=size, episode_order=episode_order)
    return jsonify(_envelope(result))


@bp.get(f"{URL_PREFIX_METADATA}/<rating_key>")
def get_metadata(rating_key: str):
    service = services().metadata
    include_children = _bool_param("includeChildren")
    episode_order = _header_or_query("episodeOrder")
    result = service.get_metadata(rating_key, include_children=include_children, episode_order=episode_order)
    return jsonify(_envelope(result))


# ----------------------------------------------------------------------
# Match feature
# ----------------------------------------------------------------------
@bp.post(URL_PREFIX_MATCHES)
def post_match():
    body = request.get_json(silent=True) or {}
    service = services().match
    size, start = _paging_params(default_size=current_app.config["DEFAULT_PAGE_SIZE"])
    result = service.match(body, offset=start, limit=size)
    return jsonify(_envelope(result))


# ----------------------------------------------------------------------
# Error handlers
# ----------------------------------------------------------------------
def _error(status: int, error: str, message: str):
    return jsonify({"error": error, "message": message}), status


@bp.app_errorhandler(NotFoundError)
def handle_not_found_error(err: NotFoundError):
    return _error(404, "Not Found", str(err))


@bp.app_errorhandler(UnsupportedMatchType)
def handle_unsupported_match_type(err: UnsupportedMatchType):
    return _error(400, "Bad Request", str(err))


@bp.app_errorhandler(SourceUnavailableError)
def handle_source_unavailable(err: SourceUnavailableError):
    # Upstream is down or erroring - a retryable failure, not "not found".
    logger.warning("Upstream source unavailable for %s %s: %s", request.method, request.path, err)
    return _error(503, "Service Unavailable", "An upstream metadata source is unavailable - try again later")


@bp.app_errorhandler(HTTPException)
def handle_http_exception(err: HTTPException):
    # Flask/werkzeug errors (404 unknown route, 405, malformed JSON 400, ...)
    # keep their own status code.
    return _error(err.code or 500, err.name, err.description or err.name)


@bp.app_errorhandler(Exception)
def handle_unexpected_error(err: Exception):
    logger.exception("Unhandled error while handling %s %s", request.method, request.path)
    return _error(500, "Internal Server Error", "An unexpected error occurred")
