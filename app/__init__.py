"""
Application factory for a Plex Custom Metadata Provider
(https://developer.plex.tv/pms/#section/API-Info/Metadata-Providers).
Provider-specific settings live in app/helper/config.py and sources under
app/client/source/; see README.md.
"""
import logging

from flask import Flask

from app.helper.config import Config
from app.client.registry import source_setting_defaults
from app.helper.config_base import load_config


def create_app(config_object: type[Config] = Config) -> Flask:
    """Application factory."""
    app = Flask(__name__)
    load_config(app.config, config_object, source_setting_defaults())

    logging.basicConfig(
        level=app.config["LOG_LEVEL"],
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    logger = logging.getLogger(__name__)

    @app.before_request
    def _log_request():  # pragma: no cover - trivial
        from flask import request

        logger.info("Incoming Request: %s %s", request.method, request.path)
        if not logger.isEnabledFor(logging.DEBUG):
            return
        if request.method == "GET":
            logger.debug("Request params %s", request.args.to_dict())
        elif request.method in ("POST", "PUT"):
            # silent=True: a malformed body is the route's problem to report
            # (as a 400), not the logger's.
            body_data = request.get_json(silent=True) if request.is_json else request.form.to_dict()
            logger.debug("Request body %s", body_data)

    # Build every source client and service once, up front, so a bad
    # config fails here rather than on the first request. See
    # app/services.py.
    from .services import init_services

    init_services(app)

    from .routes import bp as routes_bp

    app.register_blueprint(routes_bp)

    return app
