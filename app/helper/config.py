"""
This provider's settings. Only set what differs from BaseConfig
(app/helper/config_base.py), which also explains how env vars override
these.
"""
from app.helper.config_base import BaseConfig, BaseTestConfig


class Config(BaseConfig):
    # --- Provider identity -------------------------------------------------
    PROVIDER_IDENTIFIER = "tv.plex.agents.custom.circulon.fanedit"
    PROVIDER_TITLE = "FanEdit Movies"
    PROVIDER_USER_AGENT_NAME = "FanEditProvider"  # -> "FanEditProvider/<x.y>"

    # --- Metadata sources (every other category is disabled) ---------------
    ENABLE_MOVIE_SOURCES = True
    MOVIE_SOURCES = ["fanedit_org"]

    # --- Matching/scoring thresholds (0-100) --------------------------------
    MINIMUM_EXACT_SCORE = 90
    MINIMUM_MANUAL_SCORE = 70

    # --- Source client caches ----------------------------------------------
    ENTRY_CACHE_MAX_SIZE = 200
    SEARCH_CACHE_MAX_SIZE = 100


class TestConfig(BaseTestConfig, Config):
    """Config with env overrides disabled."""
