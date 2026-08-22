"""WSGI entrypoint, e.g. ``gunicorn wsgi:app``."""
from app import create_app
from app.helper.config import Config

app = create_app(Config)
