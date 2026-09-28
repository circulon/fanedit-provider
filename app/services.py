"""
Per-app service graph.

Every long-lived object the routes need (source clients, their shared TTL
caches, the Mapper, and the Search/Match/Metadata services) is built once,
eagerly, by build_services() when create_app() runs, and stored as one typed
Services object on the app.

Why on the app and not on the blueprint or at module level?
  - The services are built from app.config, which doesn't exist until
    create_app(config_object) runs; the blueprint is created at import time.
  - The blueprint is a module-level object shared by every app that
    registers it. Keeping state on the app means each create_app() call
    (e.g. one per test) gets its own clients and its own caches.

Why eagerly, not lazily on first request?
  - A bad config fails at startup instead of on the first request.
  - Nothing is built concurrently by several gthread worker threads.
  - Without gunicorn --preload, each worker imports wsgi.py (and so builds
    its own httpx clients) after the fork, so no connections are shared
    between processes. If you ever enable --preload, create the httpx
    clients lazily inside the source classes instead.

Routes read the graph via services().
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from flask import Flask, current_app

from app.client.base import SourceClient
from app.client.registry import build_all_enabled_clients
from app.helper.constants import (
    PLEX_DOCUMENTED_MATCH_TYPES,
    SOURCE_CATEGORY_MATCH_TYPES,
    SOURCE_TYPES,
    SourceType,
)
from app.helper.mapper import Mapper
from app.service.match import MatchService, MatchThresholds
from app.service.metadata import MetadataService
from app.service.search import SearchService

EXTENSION_KEY = "metadata_provider"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Services:
    clients_by_category: dict[SourceType, list[SourceClient]]
    mapper: Mapper
    match: MatchService
    metadata: MetadataService

    @property
    def enabled_categories(self) -> list[SourceType]:
        """Enabled categories, in SOURCE_TYPES order. Every one has at least
        one source - build_all_enabled_clients() rejects any that don't."""
        return [category for category in SOURCE_TYPES if self.clients_by_category.get(category)]


def build_services(app: Flask) -> Services:
    cfg = app.config
    clients_by_category = build_all_enabled_clients(cfg)
    _warn_undocumented_types(clients_by_category)

    mapper = Mapper(
        scheme=cfg["PROVIDER_IDENTIFIER"],
        include_extra_in_summary=cfg["INCLUDE_EXTRA_IN_SUMMARY"],
    )

    # One SearchService per category, used by MatchService for typed match
    # requests.
    search_by_category = {
        category: SearchService(clients=clients) for category, clients in clients_by_category.items()
    }

    # A single SearchService over every enabled category's clients,
    # concatenated in SOURCE_TYPES order - used for ratingKey lookups
    # (GET /metadata/<ratingKey> and friends), which carry no type
    # information, and for resolving includeChildren=1 on matches.
    combined_search = SearchService(
        clients=[client for category in SOURCE_TYPES for client in clients_by_category.get(category, [])]
    )

    return Services(
        clients_by_category=clients_by_category,
        mapper=mapper,
        match=MatchService(
            search_by_category=search_by_category,
            mapper=mapper,
            thresholds=MatchThresholds(
                exact_minimum=cfg["MINIMUM_EXACT_SCORE"],
                manual_minimum=cfg["MINIMUM_MANUAL_SCORE"],
            ),
            children_search=combined_search,
        ),
        metadata=MetadataService(search=combined_search, mapper=mapper),
    )


def _warn_undocumented_types(clients_by_category: dict[SourceType, list[SourceClient]]) -> None:
    for category, clients in clients_by_category.items():
        if clients and SOURCE_CATEGORY_MATCH_TYPES[category] not in PLEX_DOCUMENTED_MATCH_TYPES:
            logger.warning(
                "ENABLE_%s_SOURCES is on, so type %d (%s) is advertised to Plex, but Plex's "
                "custom-provider API doesn't document support for it yet.",
                category.upper(), SOURCE_CATEGORY_MATCH_TYPES[category], category,
            )


def init_services(app: Flask) -> Services:
    """Builds the service graph and attaches it to ``app``. Called once from
    create_app()."""
    svc = build_services(app)
    app.extensions[EXTENSION_KEY] = svc
    return svc


def services() -> Services:
    """The current app's Services. Must be called inside an app/request
    context."""
    try:
        return current_app.extensions[EXTENSION_KEY]
    except KeyError:
        raise RuntimeError(
            "Services not initialised - build the app with app.create_app(), "
            "which calls app.services.init_services()."
        ) from None
