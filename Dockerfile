# One image for the API, the worker and the mock health plan; docker-compose picks the command.
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY pyproject.toml README.md alembic.ini ./
COPY migrations ./migrations
COPY src ./src
RUN pip install --no-deps . \
    && useradd --create-home --uid 10001 app
USER app

EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://localhost:8080/health/live', timeout=2)" || exit 1

# Default: run migrations, then the API. The worker and mock payer override CMD in docker-compose.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn priorauth.api.main:app --host 0.0.0.0 --port 8080"]
