"""Error responses keep their status codes and don't leak internals."""
from app import create_app
from app.client.base import SourceUnavailableError
from app.helper.config import TestConfig
from app.helper.constants import URL_PREFIX_MATCHES, URL_PREFIX_METADATA
from app.services import EXTENSION_KEY


def _app():
    app = create_app(TestConfig)
    app.testing = True
    return app


def test_wrong_method_is_405():
    resp = _app().test_client().post("/health")
    assert resp.status_code == 405
    assert resp.get_json()["error"] == "Method Not Allowed"


def test_unknown_route_is_404():
    resp = _app().test_client().get("/nope")
    assert resp.status_code == 404
    assert resp.get_json()["error"] == "Not Found"


def test_malformed_json_match_body_is_400():
    resp = _app().test_client().post(URL_PREFIX_MATCHES, data="{not json", content_type="application/json")
    assert resp.status_code == 400


def test_source_unavailable_is_503(monkeypatch):
    app = _app()
    metadata = app.extensions[EXTENSION_KEY].metadata

    def boom(_rating_key):
        raise SourceUnavailableError("upstream down: secret-internal-detail")

    monkeypatch.setattr(metadata, "get_images", boom)
    resp = app.test_client().get(f"{URL_PREFIX_METADATA}/abc/images")
    assert resp.status_code == 503
    assert "secret-internal-detail" not in resp.get_data(as_text=True)


def test_unexpected_error_is_generic_500(monkeypatch):
    app = _app()
    metadata = app.extensions[EXTENSION_KEY].metadata

    def boom(_rating_key):
        raise RuntimeError("secret-internal-detail")

    monkeypatch.setattr(metadata, "get_images", boom)
    resp = app.test_client().get(f"{URL_PREFIX_METADATA}/abc/images")
    assert resp.status_code == 500
    assert resp.get_json()["error"] == "Internal Server Error"
    assert "secret-internal-detail" not in resp.get_data(as_text=True)
