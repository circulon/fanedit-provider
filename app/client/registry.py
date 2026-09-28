"""
Config-driven assembly of the enabled, ordered source clients used at
request time, per category (see app/helper/constants.SOURCE_TYPES).

Adding a new source
--------------------
1. Add a module under app/client/source/<category>/, e.g.
   app/client/source/movie/tmdb_lite.py, containing:
     - a class named ``TmdbLite`` (filename snake_case -> PascalCase)
       implementing app/client/base.SourceClient, with a ``name`` class
       attribute equal to its own module's filename ("tmdb_lite"). See
       the existing modules under app/client/source/ for examples.
     - optionally, a fully-defaulted ``TmdbLiteConfig`` dataclass. Each
       plain field becomes a setting named ``TMDB_LITE_<FIELD>`` (e.g.
       ``TMDB_LITE_TIMEOUT``), overridable in app/helper/config.py or by
       env var. A ``user_agent`` field is filled from PROVIDER_USER_AGENT.
2. Add "tmdb_lite" to that category's <CATEGORY>_SOURCES setting (in
   app/helper/config.py, or the env var of the same name) and make sure
   ENABLE_<CATEGORY>_SOURCES is true.

_discover_source_classes() scans each category's package at import time and
builds its name -> class mapping; nothing here needs manual editing when
sources are added or removed. A module that doesn't follow the naming
convention is logged and skipped. A name in an enabled category's
<CATEGORY>_SOURCES that doesn't match a discovered module, or an enabled
category with no sources, stops the app at startup (ConfigError).

Enabling/disabling and ordering
---------------------------------
Per category, ``Config.ENABLE_<CATEGORY>_SOURCES`` is the on/off switch;
``Config.<CATEGORY>_SOURCES`` is the ordered allow-list within an enabled
category. Order is the priority order sources are tried in - see
MatchService._search_and_score in app/service/match.py.
"""
from __future__ import annotations

import dataclasses
import importlib
import logging
import pkgutil
from dataclasses import dataclass
from collections.abc import Mapping
from typing import Any

from app.client.base import SourceClient
from app.client.caching import CachingSourceClient
from app.helper.config_base import ConfigError
from app.helper.constants import SOURCE_TYPES, SourceType
from app.util.ttl_cache import TTLCache

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
        logger.debug("No source package for category %r at app/client/source/%s.", category, category)
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


def _source_setting_key(source_name: str, field_name: str) -> str:
    """e.g. ("fanedit_org", "timeout") -> "FANEDIT_ORG_TIMEOUT"."""
    return f"{source_name}_{field_name}".upper()


def _configurable_fields(source: _DiscoveredSource) -> list[dataclasses.Field]:
    """A source config dataclass's fields that are exposed as settings:
    those with a plain str/int/float/bool/list default. ``user_agent`` is
    left out - it always comes from PROVIDER_USER_AGENT."""
    if source.config_cls is None:
        return []
    fields = []
    for f in dataclasses.fields(source.config_cls):
        if f.name == "user_agent":
            continue
        default = f.default
        if default is dataclasses.MISSING and f.default_factory is not dataclasses.MISSING:
            default = f.default_factory()
        if isinstance(default, (str, int, float, bool, list)):
            fields.append(f)
    return fields


def _field_default(f: dataclasses.Field) -> Any:
    return f.default_factory() if f.default is dataclasses.MISSING else f.default


def source_setting_defaults() -> dict[str, Any]:
    """Every discovered source's config fields as settings, e.g.
    ``{"FANEDIT_ORG_TIMEOUT": 25.0, ...}``, with the dataclass defaults.
    create_app() loads these into app.config beneath your Config, so each
    can be overridden in app/helper/config.py or by an env var of the same
    name."""
    defaults: dict[str, Any] = {}
    for sources in _SOURCE_CLASSES.values():
        for name, source in sources.items():
            for f in _configurable_fields(source):
                defaults[_source_setting_key(name, f.name)] = _field_default(f)
    return defaults


def _build_source(config: Mapping[str, Any], name: str, source: _DiscoveredSource) -> SourceClient:
    """Constructs one source client from its config dataclass, taking each
    field's value from the matching ``<SOURCE>_<FIELD>`` setting and
    ``user_agent`` from ``PROVIDER_USER_AGENT``."""
    if source.config_cls is None:
        return source.cls()
    kwargs = {
        f.name: config[_source_setting_key(name, f.name)]
        for f in _configurable_fields(source)
        if _source_setting_key(name, f.name) in config
    }
    if "user_agent" in {f.name for f in dataclasses.fields(source.config_cls)}:
        kwargs["user_agent"] = config["PROVIDER_USER_AGENT"]
    return source.cls(source.config_cls(**kwargs))


def build_enabled_clients(
    config: Mapping[str, Any],
    category: SourceType,
    search_cache: TTLCache,
    entry_cache: TTLCache,
    search_floor: int | None = None,
) -> list[SourceClient]:
    """Builds the enabled source clients for one category, in the order
    given by ``<CATEGORY>_SOURCES``, each wrapped in a CachingSourceClient
    using the given (shared) caches and search floor (see
    CachingSourceClient). Returns ``[]`` if ``ENABLE_<CATEGORY>_SOURCES`` is
    false.

    Raises ConfigError if the category is enabled but lists no sources, or
    names a source that doesn't exist - an enabled category is advertised
    to Plex, so it must be able to serve requests."""
    settings = config["SOURCE_CATEGORIES"][category]
    if not settings.enabled:
        return []

    key = category.upper()
    known = _SOURCE_CLASSES.get(category, {})
    if not settings.sources:
        raise ConfigError(
            f"ENABLE_{key}_SOURCES is true but {key}_SOURCES is empty - add a source "
            f"or disable the category. Available {category} sources: "
            f"{', '.join(sorted(known)) or '(none)'}."
        )
    unknown = [name for name in settings.sources if name not in known]
    if unknown:
        raise ConfigError(
            f"{key}_SOURCES names unknown source(s): {', '.join(unknown)}. "
            f"Available {category} sources: {', '.join(sorted(known)) or '(none)'} "
            f"(see app/client/source/{category}/)."
        )

    return [
        CachingSourceClient(
            _build_source(config, name, known[name]), search_cache, entry_cache, search_floor=search_floor
        )
        for name in settings.sources
    ]


def build_all_enabled_clients(config: Mapping[str, Any]) -> dict[SourceType, list[SourceClient]]:
    """Builds every category's enabled client list at once, keyed by
    category name. One search cache and one entry cache are shared by every
    client across all categories. Called once per app, from
    app/services.build_services()."""
    search_cache: TTLCache = TTLCache(
        maxsize=config["SEARCH_CACHE_MAX_SIZE"], ttl_seconds=config["SEARCH_CACHE_TTL_SECONDS"]
    )
    entry_cache: TTLCache = TTLCache(
        maxsize=config["ENTRY_CACHE_MAX_SIZE"], ttl_seconds=config["ENTRY_CACHE_TTL_SECONDS"]
    )
    # Search every source at the lower of the two match thresholds, so
    # automatic and manual matches for a title share one cached search.
    search_floor = min(config["MINIMUM_EXACT_SCORE"], config["MINIMUM_MANUAL_SCORE"])
    return {
        category: build_enabled_clients(config, category, search_cache, entry_cache, search_floor)
        for category in SOURCE_TYPES
    }
