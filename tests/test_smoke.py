"""Smoke tests for the app factory and provider-definition endpoint."""
from app import create_app
from app.helper.config import TestConfig
from app.helper.constants import SOURCE_CATEGORY_MATCH_TYPES, URL_PREFIX_MATCHES


def _client():
    app = create_app(TestConfig)
    app.testing = True
    return app.test_client()


def test_health():
    resp = _client().get("/health")
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok"}


def test_provider_definition_advertises_config_enabled_types():
    app = create_app(TestConfig)
    resp = app.test_client().get("/")
    assert resp.status_code == 200
    body = resp.get_json()
    types = [t["type"] for t in body["MediaProvider"]["Types"]]
    # Exactly the categories enabled in the config, in SOURCE_TYPES order.
    expected = [
        SOURCE_CATEGORY_MATCH_TYPES[category]
        for category, settings in app.config["SOURCE_CATEGORIES"].items()
        if settings.enabled
    ]
    assert types == expected
    assert types  # at least one type is served
    for t in body["MediaProvider"]["Types"]:
        assert t["Scheme"] == [{"scheme": TestConfig.PROVIDER_IDENTIFIER}]


def test_disabled_categories_are_400():
    client = _client()
    resp = client.post(URL_PREFIX_MATCHES, json={"type": 3, "title": "Season 1"})
    assert resp.status_code == 400
    resp = client.post(URL_PREFIX_MATCHES, json={"type": 4, "title": "Pilot"})
    assert resp.status_code == 400


def test_unsupported_match_type_is_400():
    client = _client()
    resp = client.post(URL_PREFIX_MATCHES, json={"type": 99, "title": "whatever"})
    assert resp.status_code == 400


def test_music_type_disabled_by_default_is_400():
    client = _client()
    resp = client.post(URL_PREFIX_MATCHES, json={"type": 8, "title": "Radiohead"})
    assert resp.status_code == 400


def test_summary_extra_appended_when_present():
    from app.client.base import SourceMetadata
    from app.helper.mapper import Mapper

    mapper = Mapper(scheme=TestConfig.PROVIDER_IDENTIFIER, include_extra_in_summary=True)
    entry = SourceMetadata(
        upstream_id="abc123", title="Some Title",
        summary="A plain synopsis.", summary_extra="Changes:\n- Extended cut",
    )
    metadata = mapper.map_full_entry(entry)
    assert metadata["summary"] == "A plain synopsis.\n\nChanges:\n- Extended cut"


def test_summary_extra_omitted_when_absent():
    from app.client.base import SourceMetadata
    from app.helper.mapper import Mapper

    mapper = Mapper(scheme=TestConfig.PROVIDER_IDENTIFIER, include_extra_in_summary=True)
    entry = SourceMetadata(upstream_id="abc123", title="Some Title", summary="A plain synopsis.")
    metadata = mapper.map_full_entry(entry)
    assert metadata["summary"] == "A plain synopsis."


def test_summary_extra_gated_by_include_extra_in_summary():
    from app.client.base import SourceMetadata
    from app.helper.mapper import Mapper

    mapper = Mapper(scheme=TestConfig.PROVIDER_IDENTIFIER, include_extra_in_summary=False)
    entry = SourceMetadata(
        upstream_id="abc123", title="Some Title",
        summary="A plain synopsis.", summary_extra="Changes:\n- Extended cut",
    )
    metadata = mapper.map_full_entry(entry)
    assert metadata["summary"] == "A plain synopsis."
