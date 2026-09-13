FROM python:3.14-slim AS base

# lxml needs libxml2/libxslt at runtime.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libxml2 \
        libxslt1.1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./

# Force pip to build statically without needing global system compilers
RUN STATIC_DEPS=true pip install --no-cache-dir lxml
RUN pip install --no-cache-dir -r requirements.txt

COPY pyproject.toml ./
COPY app ./app
COPY wsgi.py run.py ./

RUN useradd --create-home --uid 1000 appuser
USER appuser

ENV PORT=32900
EXPOSE 32900

#HEALTHCHECK --interval=60s --timeout=5s --start-period=10s --retries=3 \
#    CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"32900\")}/health', timeout=3)" || exit 1

CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT} --workers 2 --threads 4 --worker-class gthread --timeout 30 wsgi:app"]
