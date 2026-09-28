"""Tests for the per-app service graph in app/services.py."""
import pytest
from flask import Flask

from app import create_app
from app.helper.config import TestConfig
from app.services import EXTENSION_KEY, Services, services


def _all_clients(svc: Services):
    return [c for clients in svc.clients_by_category.values() for c in clients]


def test_services_are_built_at_startup():
    app = create_app(TestConfig)
    # Present before any request has been made.
    svc = app.extensions.get(EXTENSION_KEY)
    assert isinstance(svc, Services)
    assert svc.enabled_categories


def test_services_accessor_returns_current_apps_services():
    app = create_app(TestConfig)
    with app.app_context():
        assert services() is app.extensions[EXTENSION_KEY]


def test_each_app_gets_its_own_services_and_caches():
    a = create_app(TestConfig)
    b = create_app(TestConfig)
    svc_a, svc_b = a.extensions[EXTENSION_KEY], b.extensions[EXTENSION_KEY]
    assert svc_a is not svc_b
    client_a, client_b = _all_clients(svc_a)[0], _all_clients(svc_b)[0]
    assert client_a is not client_b
    assert client_a._search_cache is not client_b._search_cache
    assert client_a._entry_cache is not client_b._entry_cache


def test_caches_are_shared_by_every_client_within_one_app():
    svc = create_app(TestConfig).extensions[EXTENSION_KEY]
    clients = _all_clients(svc)
    assert clients
    assert len({id(c._search_cache) for c in clients}) == 1
    assert len({id(c._entry_cache) for c in clients}) == 1


def test_services_accessor_errors_clearly_without_create_app():
    bare = Flask(__name__)
    with bare.app_context(), pytest.raises(RuntimeError, match="create_app"):
        services()
