"""Local development entrypoint: ``python run.py``."""
from app import create_app
from app.helper.config import Config

app = create_app(Config)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=app.config["PORT"], debug=app.config["DEBUG"])
