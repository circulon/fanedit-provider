FROM python:3.14-slim AS base

LABEL org.opencontainers.image.title="FanEdit Metadata Provider" \
      org.opencontainers.image.description="Plex Metadata Provider for FanEdit movies" \
      org.opencontainers.image.url="https://hub.docker.com/r/circulon/fanedit-provider" \
      org.opencontainers.image.source="https://github.com/circulon/fanedit-provider" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.icon="https://raw.githubusercontent.com/circulon/fanedit-provider/main/provider-logo.png"

WORKDIR /app

COPY requirements.txt ./

# Wheels only (lxml's bundle libxml2/libxslt): fail fast rather than try a
# slow, compiler-less source build on an unsupported architecture.
RUN pip install --no-cache-dir --only-binary=:all: -r requirements.txt

COPY pyproject.toml ./
COPY app ./app
COPY wsgi.py run.py gunicorn.conf.py ./

RUN useradd --create-home --uid 1000 appuser
USER appuser

# Port, workers and threads come from the app config (PORT/WORKERS/THREADS
# env vars override) - see gunicorn.conf.py.
EXPOSE 32900

#HEALTHCHECK --interval=60s --timeout=5s --start-period=10s --retries=3 \
#    CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"32900\")}/health', timeout=3)" || exit 1

CMD ["gunicorn", "wsgi:app"]
