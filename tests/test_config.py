"""Tests for app/helper/config_base.py."""
import logging

import pytest

from app import create_app
from app.helper.config import Config, TestConfig
from app.client.registry import source_setting_defaults
from app.helper.config_base import ConfigError, load_config, short_version
from app.helper.constants import SOURCE_CATEGORY_MATCH_TYPES, SOURCE_TYPES, URL_PREFIX_MATCHES, SourceType
from app.services import EXTENSION_KEY


def _load(config_object):
    config = {}
    load_config(config, config_object)
    return config


def test_every_category_gets_settings():
    categories = _load(TestConfig)["SOURCE_CATEGORIES"]
    assert set(categories) == set(SOURCE_TYPES)


def test_unmentioned_category_is_disabled():
    class OnlyMovies(TestConfig):
        pass

    for attr in [a for a in dir(OnlyMovies) if a.startswith("ENABLE_") and a != "ENABLE_MOVIE_SOURCES"]:
        setattr(OnlyMovies, attr, False)
    categories = _load(OnlyMovies)["SOURCE_CATEGORIES"]
    assert categories[SourceType.MOVIE].enabled
    assert not any(s.enabled for c, s in categories.items() if c != SourceType.MOVIE)


def test_env_overrides_apply_with_type_conversion(monkeypatch):
    monkeypatch.setenv("MINIMUM_EXACT_SCORE", "77")
    monkeypatch.setenv("ENABLE_MOVIE_SOURCES", "false")
    monkeypatch.setenv("MOVIE_SOURCES", "a, b ,c")
    monkeypatch.setenv("LOG_LEVEL", "debug")
    config = _load(Config)
    assert config["MINIMUM_EXACT_SCORE"] == 77
    assert config["ENABLE_MOVIE_SOURCES"] is False
    assert config["MOVIE_SOURCES"] == ["a", "b", "c"]
    assert config["LOG_LEVEL"] == "DEBUG"
    assert config["SOURCE_CATEGORIES"][SourceType.MOVIE].enabled is False


def test_env_overrides_skipped_when_testing(monkeypatch):
    monkeypatch.setenv("MINIMUM_EXACT_SCORE", "77")
    assert _load(TestConfig)["MINIMUM_EXACT_SCORE"] == TestConfig.MINIMUM_EXACT_SCORE


def test_invalid_env_value_fails_at_startup(monkeypatch):
    monkeypatch.setenv("MINIMUM_EXACT_SCORE", "high")
    with pytest.raises(ConfigError, match="MINIMUM_EXACT_SCORE"):
        create_app(Config)


def test_misspelt_category_setting_warns(caplog):
    class Typo(TestConfig):
        ENABLE_MOVEI_SOURCES = True

    with caplog.at_level(logging.WARNING):
        _load(Typo)
    assert "ENABLE_MOVEI_SOURCES" in caplog.text


# ----------------------------------------------------------------------
# The config decides what the provider serves
# ----------------------------------------------------------------------
def _types(app):
    return [t["type"] for t in app.test_client().get("/").get_json()["MediaProvider"]["Types"]]


def _only(category: SourceType, sources: list[str]):
    """A TestConfig with just ``category`` enabled, using ``sources``."""
    attrs = {a: False for a in dir(TestConfig) if a.startswith("ENABLE_") and a.endswith("_SOURCES")}
    attrs[f"ENABLE_{category.upper()}_SOURCES"] = True
    attrs[f"{category.upper()}_SOURCES"] = sources
    return type("OnlyConfig", (TestConfig,), attrs)


def test_enabled_category_with_no_sources_fails_at_startup():
    with pytest.raises(ConfigError, match=f"{SOURCE_TYPES[0].upper()}_SOURCES is empty"):
        create_app(_only(SOURCE_TYPES[0], []))


def test_unknown_source_name_fails_at_startup():
    with pytest.raises(ConfigError, match="no_such_source"):
        create_app(_only(SOURCE_TYPES[0], ["no_such_source"]))


def test_advertised_types_follow_config():
    category = SOURCE_TYPES[0]
    sources = getattr(TestConfig, f"{category.upper()}_SOURCES")
    app = create_app(_only(category, sources))
    assert _types(app) == [SOURCE_CATEGORY_MATCH_TYPES[category]]


def test_unsupported_type_error_lists_only_enabled_types():
    app = create_app(TestConfig)
    body = app.test_client().post(URL_PREFIX_MATCHES, json={"type": 99, "title": "x"}).get_json()
    enabled = [c for c, s in app.config["SOURCE_CATEGORIES"].items() if s.enabled]
    disabled = [c for c, s in app.config["SOURCE_CATEGORIES"].items() if not s.enabled]
    for c in enabled:
        assert f"({c})" in body["message"]
    for c in disabled:
        assert f"({c})" not in body["message"]


def test_source_settings_come_from_config(monkeypatch):
    defaults = source_setting_defaults()
    if not defaults:
        pytest.skip("no source exposes settings")
    key, default = next((k, v) for k, v in defaults.items() if isinstance(v, (int, float)) and not isinstance(v, bool))
    # Default present in app.config ...
    assert create_app(TestConfig).config[key] == default
    # ... overridable in the Config class ...
    assert create_app(type("C", (TestConfig,), {key: default + 1})).config[key] == default + 1
    # ... and by env var (when not testing).
    monkeypatch.setenv(key, str(default + 2))
    config = {}
    load_config(config, Config, defaults)
    assert config[key] == default + 2


def test_source_setting_reaches_the_source_instance():
    defaults = source_setting_defaults()
    key = next((k for k, v in defaults.items() if k.endswith("_TIMEOUT")), None)
    if key is None:
        pytest.skip("no source exposes a timeout")
    app = create_app(type("C", (TestConfig,), {key: 1.5}))
    clients = [c._client for cs in app.extensions[EXTENSION_KEY].clients_by_category.values() for c in cs]
    configs = [getattr(c, "config", None) for c in clients]
    assert any(getattr(cfg, "timeout", None) == 1.5 for cfg in configs)


def test_gunicorn_settings_come_from_config(monkeypatch):
    import importlib
    import sys

    monkeypatch.setenv("PORT", "40000")
    monkeypatch.setenv("WORKERS", "3")
    sys.modules.pop("gunicorn.conf", None)
    spec = importlib.util.spec_from_file_location("gunicorn_conf", "gunicorn.conf.py")
    conf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(conf)
    assert conf.bind == "0.0.0.0:40000"
    assert conf.workers == 3
    assert conf.threads == Config.THREADS


# ----------------------------------------------------------------------
# Version and user agent come from pyproject.toml
# ----------------------------------------------------------------------
def _pyproject_version():
    import tomllib
    from pathlib import Path

    with open(Path(__file__).resolve().parents[1] / "pyproject.toml", "rb") as f:
        return tomllib.load(f)["project"]["version"]


def test_version_comes_from_pyproject():
    app = create_app(TestConfig)
    assert app.config["PROVIDER_VERSION"] == _pyproject_version()
    assert app.test_client().get("/").get_json()["MediaProvider"]["version"] == _pyproject_version()


def test_user_agent_is_name_and_major_minor():
    config = _load(TestConfig)
    major, minor = _pyproject_version().split(".")[:2]
    assert config["PROVIDER_USER_AGENT"] == f"{TestConfig.PROVIDER_USER_AGENT_NAME}/{major}.{minor}"


@pytest.mark.parametrize(
    "version, expected",
    [("1.1.0", "1.1"), ("0.0.0-dev", "0.0"), ("2.0.0rc1", "2.0"), ("10.12.3", "10.12"), ("3", "3")],
)
def test_short_version(version, expected):
    assert short_version(version) == expected


@pytest.mark.parametrize("key", ["PROVIDER_VERSION", "PROVIDER_USER_AGENT"])
def test_derived_settings_cannot_be_set_in_config(key):
    with pytest.raises(ConfigError, match=key):
        _load(type("C", (TestConfig,), {key: "9.9.9"}))


def test_version_env_var_is_ignored(monkeypatch):
    monkeypatch.setenv("PROVIDER_VERSION", "9.9.9")
    monkeypatch.setenv("PROVIDER_USER_AGENT", "Spoof/9.9")
    config = _load(Config)
    assert config["PROVIDER_VERSION"] == _pyproject_version()
    assert config["PROVIDER_USER_AGENT"].startswith(f"{Config.PROVIDER_USER_AGENT_NAME}/")


def test_user_agent_reaches_sources():
    app = create_app(TestConfig)
    clients = [c._client for cs in app.extensions[EXTENSION_KEY].clients_by_category.values() for c in cs]
    agents = {getattr(getattr(c, "config", None), "user_agent", None) for c in clients} - {None}
    assert agents == {app.config["PROVIDER_USER_AGENT"]}
