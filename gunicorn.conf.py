"""
gunicorn settings, taken from the app config (app/helper/config.py plus env
overrides - see app/helper/config_base.py) so PORT, WORKERS and THREADS are
set in one place. Loaded automatically by `gunicorn wsgi:app`.
"""
from app.helper.config import Config
from app.helper.config_base import load_config

_cfg: dict = {}
load_config(_cfg, Config)

bind = f"0.0.0.0:{_cfg['PORT']}"
workers = _cfg["WORKERS"]
threads = _cfg["THREADS"]
worker_class = "gthread"
timeout = 30
