"""
Media Provider Template
=========================

Flask skeleton implementing Plex's Custom Media Provider HTTP contract
(https://developer.plex.tv/pms/#section/API-Info/Metadata-Providers) for
movies, TV shows, and music. See README.md for the architecture and how to
add a metadata source.
"""
import logging

from flask import Flask

from app.helper.config import Config, validate_source_categories


def create_app(config_object: type[Config] = Config) -> Flask:
    """Application factory."""
    validate_source_categories(config_object)

    app = Flask(__name__)
    app.config.from_object(config_object)

    logging.basicConfig(
        level=app.config["LOG_LEVEL"],
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    logger = logging.getLogger(__name__)

    @app.before_request
    def _log_request():  # pragma: no cover - trivial
        from flask import request

        logger.info("Incoming Request: %s %s", request.method, request.path)
        if request.method == 'GET':
            logger.debug(f"Request params {request.args.to_dict()}")
        elif request.method in ['POST', 'PUT']:
            if request.is_json:
                body_data = request.get_json()
            else:
                body_data = request.form.to_dict()
            logger.debug(f"Request body {body_data}")

    from .routes import bp as routes_bp

    app.register_blueprint(routes_bp)

    return app
