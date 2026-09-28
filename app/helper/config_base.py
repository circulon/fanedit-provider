"""
Shared configuration machinery.

Your provider's own settings live in app/helper/config.py, as a ``Config``
subclass of ``BaseConfig`` that only sets what differs from the defaults
here. ``TestConfig`` subclasses that in turn.

The config is the single source of truth for what the provider does: which
media types it advertises to Plex (the enabled categories), which sources
serve them, their settings, and the server's port/workers/threads.

How settings are resolved (see ``load_config()``, called by create_app()
and gunicorn.conf.py):

1. Defaults: ``BaseConfig``, plus each source's config-dataclass fields as
   ``<SOURCE>_<FIELD>`` settings (e.g. ``FANEDIT_ORG_TIMEOUT``), overridden
   by your ``Config``.
2. Environment variables (and a local ``.env``): any setting defined on the
   config class can be overridden by an env var of the same name, converted
   to the default's type (bool: 1/true/yes/on; list: comma-separated).
   Skipped when ``TESTING`` is true, so tests never depend on a local .env.
   An unparseable value stops startup (ConfigError).
3. Derived settings (can't be set in a Config class or by env var):
   - ``PROVIDER_VERSION``: ``project.version`` in pyproject.toml.
   - ``PROVIDER_USER_AGENT``: ``<PROVIDER_USER_AGENT_NAME>/<major>.<minor>``
     of that version, e.g. "FanEditProvider/1.1".
   - ``SOURCE_CATEGORIES``: from the ``ENABLE_<CATEGORY>_SOURCES`` /
     ``<CATEGORY>_SOURCES`` pairs. A category you don't mention is disabled.
"""
from __future__ import annotations

import logging
import os
import re
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from app.helper.constants import SOURCE_TYPES, SourceType

load_dotenv()

logger = logging.getLogger(__name__)


class ConfigError(RuntimeError):
    """The configuration is invalid; raised at startup."""


@dataclass(frozen=True)
class SourceCategorySettings:
    """One category's on/off switch and its ordered source list
    (``app.config["SOURCE_CATEGORIES"][category]``)."""

    enabled: bool
    sources: list[str]


@lru_cache
def project_version() -> str:
    """``project.version`` from pyproject.toml - the single source of truth
    for the provider's version. "0.0.0-dev" (with a warning) if it can't be
    read, e.g. pyproject.toml wasn't copied into the image."""
    pyproject_path = Path(__file__).resolve().parents[2] / "pyproject.toml"
    try:
        with open(pyproject_path, "rb") as f:
            return str(tomllib.load(f)["project"]["version"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("Couldn't read project.version from %s (%s) - using 0.0.0-dev", pyproject_path, exc)
        return "0.0.0-dev"


def short_version(version: str) -> str:
    """"1.1.0" -> "1.1", "2.0.0rc1" -> "2.0", "3" -> "3"."""
    match = re.match(r"(\d+)(?:\.(\d+))?", version)
    if not match:
        return version
    major, minor = match.groups()
    return f"{major}.{minor}" if minor is not None else major


# Computed by load_config(); a Config class may not set them.
DERIVED_SETTINGS = ("PROVIDER_VERSION", "PROVIDER_USER_AGENT", "SOURCE_CATEGORIES")


class BaseConfig:
    """Defaults shared by every provider. Override in app/helper/config.py."""

    # --- Provider identity (set these in your Config) -----------------------
    # Must be prefixed with "tv.plex.agents.custom.", globally unique, and
    # should not end with a type suffix like ".movie". See
    # docs/MediaProvider.md#defining-an-identifier in
    # https://github.com/plexinc/tmdb-example-provider.
    PROVIDER_IDENTIFIER: str = "tv.plex.agents.custom.yourname.template"
    PROVIDER_TITLE: str = "Metadata Provider Template"
    # Name part of PROVIDER_USER_AGENT, which is sent to upstream sources as
    # "<name>/<major>.<minor>" of the pyproject.toml version.
    PROVIDER_USER_AGENT_NAME: str = "MetadataProvider"

    # --- Metadata sources ---------------------------------------------------
    # Per category: ENABLE_<CATEGORY>_SOURCES (bool) and <CATEGORY>_SOURCES
    # (ordered list of source module names). Unset = disabled. Categories:
    # see app/helper/constants.SOURCE_TYPES.

    # Append a source's optional `summary_extra` text to `summary` - see
    # app/client/base.SourceMetadata.summary_extra.
    INCLUDE_EXTRA_IN_SUMMARY: bool = True

    # --- Matching/scoring thresholds (0-100) --------------------------------
    # MINIMUM_EXACT_SCORE: automatic matching only picks a title when exactly
    #   one result scores at or above this.
    # MINIMUM_MANUAL_SCORE: results listed for manual "fix match" requests.
    MINIMUM_EXACT_SCORE: int = 85
    MINIMUM_MANUAL_SCORE: int = 65

    # --- Response paging (manual matches, /children, /grandchildren) --------
    DEFAULT_PAGE_SIZE: int = 20

    # --- Server (gunicorn.conf.py; PORT also used by run.py) ----------------
    PORT: int = 32900
    WORKERS: int = 2  # gunicorn worker processes (each has its own caches)
    THREADS: int = 4  # request threads per worker
    DEBUG: bool = False
    LOG_LEVEL: str = "INFO"

    # --- Source client caches (shared by all sources, per worker process) ---
    ENTRY_CACHE_TTL_SECONDS: int = 60
    ENTRY_CACHE_MAX_SIZE: int = 2000
    SEARCH_CACHE_TTL_SECONDS: int = 60
    SEARCH_CACHE_MAX_SIZE: int = 500


class BaseTestConfig:
    """Mixin for TestConfig: skips env overrides so a local .env can't
    change test results."""

    TESTING = True
    DEBUG = True


# ----------------------------------------------------------------------
def load_config(
    config: dict[str, Any], config_object: type, extra_defaults: dict[str, Any] | None = None
) -> None:
    """Loads ``config_object`` into a Flask ``config`` mapping on top of
    ``extra_defaults`` (source settings - see
    app/client/registry.source_setting_defaults), applies env overrides
    (unless TESTING) and derives SOURCE_CATEGORIES."""
    settings = {**(extra_defaults or {}), **_settings_of(config_object)}
    derived = [key for key in DERIVED_SETTINGS if key in settings]
    if derived:
        raise ConfigError(
            f"{config_object.__name__} sets {', '.join(derived)}, which are derived - the version "
            "comes from pyproject.toml and the user agent from PROVIDER_USER_AGENT_NAME."
        )
    config.update(settings)
    if not config.get("TESTING"):
        _apply_env_overrides(config, settings)
    config["LOG_LEVEL"] = str(config.get("LOG_LEVEL", "INFO")).upper()
    config["PROVIDER_VERSION"] = project_version()
    config["PROVIDER_USER_AGENT"] = f"{config['PROVIDER_USER_AGENT_NAME']}/{short_version(config['PROVIDER_VERSION'])}"
    config["SOURCE_CATEGORIES"] = source_categories(config)


def _settings_of(config_object: type) -> dict[str, Any]:
    return {key: getattr(config_object, key) for key in dir(config_object) if key.isupper()}


def source_categories(config: dict[str, Any]) -> dict[SourceType, SourceCategorySettings]:
    """Builds one SourceCategorySettings per category from the
    ENABLE_<CATEGORY>_SOURCES / <CATEGORY>_SOURCES settings, and warns about
    any such setting that doesn't name a known category (e.g. a typo)."""
    known = {category.upper() for category in SOURCE_TYPES}
    for key in config:
        if key.startswith("ENABLE_") and key.endswith("_SOURCES"):
            name = key[len("ENABLE_"):-len("_SOURCES")]
            if name not in known:
                logger.warning(
                    "%s doesn't match any source category (%s) - ignoring it.",
                    key, ", ".join(sorted(known)),
                )

    return {
        category: SourceCategorySettings(
            enabled=bool(config.get(f"ENABLE_{category.upper()}_SOURCES", False)),
            sources=list(config.get(f"{category.upper()}_SOURCES", [])),
        )
        for category in SOURCE_TYPES
    }


def _apply_env_overrides(config: dict[str, Any], settings: dict[str, Any]) -> None:
    for key, default in settings.items():
        raw = os.environ.get(key)
        if raw is None:
            continue
        try:
            config[key] = _coerce(raw, default)
        except ValueError as exc:
            raise ConfigError(f"Invalid value for env var {key}={raw!r}: {exc}") from None


def _coerce(raw: str, default: Any) -> Any:
    if isinstance(default, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        return int(raw)
    if isinstance(default, float):
        return float(raw)
    if isinstance(default, list):
        return [item.strip() for item in raw.split(",") if item.strip()]
    return raw
