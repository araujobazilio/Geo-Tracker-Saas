# =============================================================================
# GEO Tracker — Dockerfile (application image)
# =============================================================================
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# System dependencies required by psycopg / argon2 / uvicorn.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        libffi-dev \
        curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml alembic.ini ./
COPY app ./app
COPY alembic ./alembic
COPY scripts ./scripts
COPY README.md ./

RUN pip install --upgrade pip \
    && pip install ".[dev]"

EXPOSE 8000

# Default command runs the app via uvicorn. Override for workers / migrations.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
