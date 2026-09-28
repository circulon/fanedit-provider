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

from dataclasses import dataclass

from flask import Flask, current_app

from app.client.base import SourceClient
from app.client.registry import build_all_enabled_clients
from app.helper.constants import SOURCE_TYPES, SourceType
from app.helper.mapper import Mapper
from app.service.match import MatchService, MatchThresholds
from app.service.metadata import MetadataService
from app.service.search import SearchService

EXTENSION_KEY = "metadata_provider"


@dataclass(frozen=True)
class Services:
    clients_by_category: dict[SourceType, list[SourceClient]]
    mapper: Mapper
    match: MatchService
    metadata: MetadataService

    @property
    def enabled_categories(self) -> set[SourceType]:
        return {category for category, clients in self.clients_by_category.items() if clients}


def build_services(app: Flask) -> Services:
    cfg = app.config
    clients_by_category = build_all_enabled_clients(cfg)

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
