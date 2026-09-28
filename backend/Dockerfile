FROM docker:29.1.3-cli AS docker-cli

FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=60 \
    PIP_RETRIES=10

# System deps for pdfplumber / cryptography builds
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libffi-dev libxml2 libxslt1-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# The app calls a separately configured sandbox Docker daemon. Ship only the
# client; never run a Docker daemon or mount the application host's socket here.
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker

COPY requirements.lock.txt .
RUN pip install --require-hashes -r requirements.lock.txt

# Run as a non-root user (the image otherwise ran everything as root).
RUN groupadd --system --gid 1001 app \
    && useradd --system --uid 1001 --gid app --create-home --home-dir /home/app app

COPY --chown=app:app . .

USER app

EXPOSE 8000

# Container health: the app exposes GET /health (200 when booted + DB reachable).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
