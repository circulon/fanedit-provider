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

from flask import Blueprint, Flask, current_app, jsonify, request

from app.client.registry import build_all_enabled_clients
from app.helper.config import Config
from app.helper.constants import (
    PLEX_SUPPORTED_MATCH_TYPES,
    SOURCE_CATEGORY_MATCH_TYPES,
    SOURCE_TYPES,
    URL_PREFIX_MATCHES,
    URL_PREFIX_METADATA,
    SourceType,
)
from app.helper.mapper import Mapper
from app.service.match import MatchService, MatchThresholds, UnsupportedMatchType
from app.service.metadata import MetadataService, NotFoundError
from app.service.search import SearchService

bp = Blueprint("ifdb_provider", __name__)
logger = logging.getLogger(__name__)


def _get_mapper(app: Flask) -> Mapper:
    if "mapper" not in app.extensions:
        cfg = app.config
        app.extensions["mapper"] = Mapper(
            scheme=cfg["PROVIDER_IDENTIFIER"],
            include_extra_in_summary=cfg["INCLUDE_EXTRA_IN_SUMMARY"],
        )
    return app.extensions["mapper"]


def _get_clients_by_category(app: Flask) -> dict[SourceType, list]:
    """Builds (and caches on app.extensions) every category's enabled
    client list once."""
    if "clients_by_category" not in app.extensions:
        app.extensions["clients_by_category"] = build_all_enabled_clients(app)
    return app.extensions["clients_by_category"]


def _get_search_by_category(app: Flask) -> dict[SourceType, SearchService]:
    """One SearchService per source category, used by MatchService."""
    if "search_by_category" not in app.extensions:
        clients_by_category = _get_clients_by_category(app)
        app.extensions["search_by_category"] = {
            category: SearchService(clients=clients) for category, clients in clients_by_category.items()
        }
    return app.extensions["search_by_category"]


def _get_combined_search_service(app: Flask) -> SearchService:
    """A single SearchService over every enabled category's clients,
    concatenated in SOURCE_TYPES order - used for ratingKey lookups (GET
    /library/metadata/<ratingKey> and friends), which carry no type
    information."""
    if "combined_search" not in app.extensions:
        clients_by_category = _get_clients_by_category(app)
        combined = [client for category in SOURCE_TYPES for client in clients_by_category.get(category, [])]
        app.extensions["combined_search"] = SearchService(clients=combined)
    return app.extensions["combined_search"]


def _get_match_service(app: Flask) -> MatchService:
    if "match_service" not in app.extensions:
        cfg = app.config
        app.extensions["match_service"] = MatchService(
            search_by_category=_get_search_by_category(app),
            mapper=_get_mapper(app),
            thresholds=MatchThresholds(
                exact_minimum=cfg["MINIMUM_EXACT_SCORE"],
                manual_minimum=cfg["MINIMUM_MANUAL_SCORE"]
            ),
            # Same combined (all-category) search used for ratingKey
            # lookups - resolves includeChildren=1 regardless of which
            # category matched.
            children_search=_get_combined_search_service(app),
        )
    return app.extensions["match_service"]


def _get_metadata_service(app: Flask) -> MetadataService:
    if "metadata_service" not in app.extensions:
        app.extensions["metadata_service"] = MetadataService(
            search=_get_combined_search_service(app),
            mapper=_get_mapper(app),
        )
    return app.extensions["metadata_service"]


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
def _enabled_categories(app: Flask) -> set[SourceType]:
    clients_by_category = _get_clients_by_category(app)
    return {category for category, clients in clients_by_category.items() if clients}


def _build_provider_response() -> dict:
    """One ``Types`` entry per metadata type actually served - every type
    belonging to an enabled source category, intersected with
    PLEX_SUPPORTED_MATCH_TYPES so an unsupported type (currently music) is
    never advertised ev
    en if enabled locally."""
    cfg = current_app.config
    identifier = cfg["PROVIDER_IDENTIFIER"]
    enabled_categories = _enabled_categories(current_app)

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
    service = _get_metadata_service(current_app)
    result = service.get_images(rating_key)
    return jsonify(_envelope(result))


@bp.get(f"{URL_PREFIX_METADATA}/<rating_key>/extras")
def get_extras(rating_key: str):
    service = _get_metadata_service(current_app)
    result = service.get_extras(rating_key)
    return jsonify(_envelope(result))


@bp.get(f"{URL_PREFIX_METADATA}/<rating_key>/children")
def get_children(rating_key: str):
    """Direct children (Seasons for a Show, Episodes for a Season).
    Mandatory paging - defaults to the first 20 items when no paging
    headers/params are sent."""
    service = _get_metadata_service(current_app)
    size, start = _paging_params(default_size=current_app.config["DEFAULT_PAGE_SIZE"])
    episode_order = _header_or_query("episodeOrder")
    result = service.get_children(rating_key, offset=start, limit=size, episode_order=episode_order)
    return jsonify(_envelope(result))


@bp.get(f"{URL_PREFIX_METADATA}/<rating_key>/grandchildren")
def get_grandchildren(rating_key: str):
    """Episodes for a Show, flattening past the Season level. Mandatory
    paging - defaults to the first 20 items when no paging headers/params
    are sent."""
    service = _get_metadata_service(current_app)
    size, start = _paging_params(default_size=current_app.config["DEFAULT_PAGE_SIZE"])
    episode_order = _header_or_query("episodeOrder")
    result = service.get_grandchildren(rating_key, offset=start, limit=size, episode_order=episode_order)
    return jsonify(_envelope(result))


@bp.get(f"{URL_PREFIX_METADATA}/<rating_key>")
def get_metadata(rating_key: str):
    service = _get_metadata_service(current_app)
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
    service = _get_match_service(current_app)
    size, start = _paging_params(default_size=current_app.config["DEFAULT_PAGE_SIZE"])
    result = service.match(body, offset=start, limit=size)
    return jsonify(_envelope(result))


# ----------------------------------------------------------------------
# Error handlers
# ----------------------------------------------------------------------
@bp.app_errorhandler(NotFoundError)
def handle_not_found_error(err: NotFoundError):
    return jsonify({"error": "Not Found", "message": str(err)}), 404


@bp.app_errorhandler(UnsupportedMatchType)
def handle_unsupported_match_type(err: UnsupportedMatchType):
    return jsonify({"error": "Bad Request", "message": str(err)}), 400


@bp.app_errorhandler(Exception)
def handle_unexpected_error(err: Exception):
    logger.exception("Unhandled error while handling %s %s", request.method, request.path)
    return jsonify({"error": "Internal server error", "message": str(err)}), 500


@bp.app_errorhandler(404)
def handle_404(_err):
    return jsonify({"error": "Not Found", "message": "The requested resource was not found"}), 404
