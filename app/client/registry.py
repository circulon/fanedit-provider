"""
Config-driven assembly of the enabled, ordered source clients used at
request time, per category (movie/show/season/episode/music).

Adding a new source
--------------------
1. Add a module under app/client/source/<category>/, e.g.
   app/client/source/movie/tmdb_lite.py, containing:
     - a class named ``TmdbLite`` (filename snake_case -> PascalCase)
       implementing app/client/base.SourceClient, with a ``name`` class
       attribute equal to its own module's filename ("tmdb_lite"). See
       app/client/source/movie/example_movie.py,
       app/client/source/show/example_show.py, and
       app/client/source/music/example_music.py for examples.
     - optionally, a fully-defaulted ``TmdbLiteConfig`` dataclass. If it has
       a ``user_agent`` field, build_enabled_clients() fills it in from
       ``Config.PROVIDER_USER_AGENT``.
2. Add "tmdb_lite" to that category's <CATEGORY>_SOURCES env var and make
   sure ENABLE_<CATEGORY>_SOURCES is true.

_discover_source_classes() scans each category's package at import time and
builds its name -> class mapping; nothing here needs manual editing when
sources are added or removed. A module that doesn't follow the naming
convention, or a name in <CATEGORY>_SOURCES that doesn't match a discovered
module, is logged and skipped rather than crashing app startup.

Enabling/disabling and ordering
---------------------------------
Per category, ``Config.ENABLE_<CATEGORY>_SOURCES`` is the on/off switch;
``Config.<CATEGORY>_SOURCES`` is the ordered allow-list within an enabled
category. Order is the priority order sources are tried in - see
MatchService._search_and_score in app/service/match.py.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
import importlib
import logging
import pkgutil
from typing import TYPE_CHECKING

from app.client.base import SourceClient
from app.client.caching import CachingSourceClient
from app.helper.constants import SOURCE_TYPES, SourceType
from app.util.ttl_cache import TTLCache

if TYPE_CHECKING:
    from flask import Flask

logger = logging.getLogger(__name__)


def _snake_to_pascal(name: str) -> str:
    """'example_movie' -> 'ExampleMovie'."""
    return "".join(part.title() for part in name.split("_"))


@dataclass(frozen=True)
class _DiscoveredSource:
    cls: type
    config_cls: type | None  # None if the source takes no configuration.


def _discover_source_classes(category: SourceType) -> dict[str, _DiscoveredSource]:
    """Scans app/client/source/<category>/ for source modules, keyed by
    filename. Skips (with a warning) any module that doesn't follow the
    naming convention described in this module's docstring."""
    try:
        package = importlib.import_module(f"app.client.source.{category}")
    except ImportError:
        logger.warning(
            f"Code for ty[e '{category}' does not exist at app/client/source/{category} does not exist - skipping.",
        )
        return {}

    discovered: dict[str, _DiscoveredSource] = {}
    for module_info in pkgutil.iter_modules(package.__path__):
        module_name = module_info.name
        if module_name.startswith("_"):
            continue

        module = importlib.import_module(f"app.client.source.{category}.{module_name}")
        class_name = _snake_to_pascal(module_name)
        cls = getattr(module, class_name, None)
        if cls is None:
            logger.warning(
                "app/client/source/%s/%s.py has no %s class matching its filename - skipping.",
                category, module_name, class_name,
            )
            continue

        source_name = getattr(cls, "name", None)
        if source_name != module_name:
            logger.warning(
                "app.client.source.%s.%s.%s.name is %r, expected %r (its own module's "
                "filename) - skipping.",
                category, module_name, class_name, source_name, module_name,
            )
            continue

        config_cls = getattr(module, f"{class_name}Config", None)
        if config_cls is not None and not dataclasses.is_dataclass(config_cls):
            logger.warning(
                "app.client.source.%s.%s.%sConfig isn't a dataclass - ignoring it, "
                "%s will be built with no config.",
                category, module_name, class_name, class_name,
            )
            config_cls = None

        discovered[module_name] = _DiscoveredSource(cls=cls, config_cls=config_cls)

    return discovered


# Computed once at import time - these packages don't change at runtime.
_SOURCE_CLASSES: dict[SourceType, dict[str, _DiscoveredSource]] = {
    category: _discover_source_classes(category) for category in SOURCE_TYPES
}


def _build_source(app: "Flask", category: SourceType, name: str, source: _DiscoveredSource) -> SourceClient:
    """Constructs (and caches on app.extensions, for reuse across
    requests) one source client."""
    key = f"source_client:{category}:{name}"
    if key not in app.extensions:
        if source.config_cls is None:
            app.extensions[key] = source.cls()
        else:
            field_names = {f.name for f in dataclasses.fields(source.config_cls)}
            kwargs = {}
            if "user_agent" in field_names:
                kwargs["user_agent"] = app.config["PROVIDER_USER_AGENT"]
            app.extensions[key] = source.cls(source.config_cls(**kwargs))
    return app.extensions[key]


def build_enabled_clients(app: "Flask", category: SourceType) -> list[SourceClient]:
    """Builds the enabled source clients for one category, in the order
    given by ``Config.<CATEGORY>_SOURCES``, each wrapped in a
    CachingSourceClient sharing one search-cache and one entry-cache across
    every category. Returns ``[]`` immediately if
    ``ENABLE_<CATEGORY>_SOURCES`` is false."""
    cfg = app.config
    settings = cfg["SOURCE_CATEGORIES"].get(category)
    if settings is None:
        raise RuntimeError(
            f"No SOURCE_CATEGORIES entry for category {category!r} - check "
            "Config.SOURCE_CATEGORIES in app/helper/config.py."
        )
    enabled = settings.enabled
    order = settings.sources
    if not enabled:
        return []

    search_cache: TTLCache = app.extensions.setdefault(
        "source_search_cache",
        TTLCache(maxsize=cfg["SEARCH_CACHE_MAX_SIZE"], ttl_seconds=cfg["SEARCH_CACHE_TTL_SECONDS"]),
    )
    entry_cache: TTLCache = app.extensions.setdefault(
        "source_entry_cache",
        TTLCache(maxsize=cfg["ENTRY_CACHE_MAX_SIZE"], ttl_seconds=cfg["ENTRY_CACHE_TTL_SECONDS"]),
    )

    known = _SOURCE_CLASSES.get(category, {})
    clients: list[SourceClient] = []
    for name in order:
        source = known.get(name)
        if source is None:
            logger.warning(
                "%s_SOURCES contains unknown source %r - skipping. Known %s sources: %s",
                category.upper(), name, category, ", ".join(sorted(known)) or "(none)",
            )
            continue
        clients.append(CachingSourceClient(_build_source(app, category, name, source), search_cache, entry_cache))

    if not clients:
        logger.warning("No %s metadata sources enabled - check ENABLE_%s_SOURCES/%s_SOURCES",
                        category, category.upper(), category.upper())

    return clients


def build_all_enabled_clients(app: "Flask") -> dict[SourceType, list[SourceClient]]:
    """Builds every category's enabled client list at once, keyed by
    category name."""
    return {category: build_enabled_clients(app, category) for category in SOURCE_TYPES}
