# =============================================================================
# GEO Tracker — Production Dockerfile (multi-stage)
# =============================================================================
# Stages:
#   A. frontend-build  — Node 24, builds CSS via Tailwind
#   B. python-builder  — Python 3.11, installs production deps + wheels
#   C. runtime         — Python 3.11-slim, non-root, production-only
# =============================================================================
# Build:
#   docker build -t geo-tracker:<git-sha> .
# =============================================================================

# --- Stage A: Frontend build -------------------------------------------------
FROM node:24-slim AS frontend-build

WORKDIR /build

COPY package.json package-lock.json tailwind.config.js ./
COPY app/static/css/tailwind-input.css ./app/static/css/tailwind-input.css
COPY app/templates/ ./app/templates/

RUN npm ci
RUN npm run build:frontend

# --- Stage B: Python builder -------------------------------------------------
FROM python:3.11-slim AS python-builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# Build-only system dependencies.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./

# Install production dependencies only (NOT .[dev]).
RUN pip install --upgrade pip \
    && pip install --target=/install . \
    && pip install --target=/install uvicorn[standard]

# --- Stage C: Runtime --------------------------------------------------------
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Runtime system dependencies (no compiler toolchain).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libpq5 \
        libffi8 \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Create unprivileged application user/group.
RUN groupadd --system --gid 1001 geo \
    && useradd --system --uid 1001 --gid geo --no-create-home --home-dir /app geo

# Copy installed Python packages from builder.
COPY --from=python-builder /install /usr/local/lib/python3.11/site-packages

# Copy application source.
COPY pyproject.toml alembic.ini ./
COPY app ./app
COPY alembic ./alembic
COPY scripts ./scripts
COPY README.md ./

# Copy compiled frontend assets from frontend-build.
COPY --from=frontend-build /build/app/static/css/app.css ./app/static/css/app.css

# Ensure the app user owns the working directory.
RUN chown --recursive geo:geo /app

# Switch to non-root user.
USER geo

EXPOSE 8000

# Healthcheck: liveness probe (no external dependencies).
# In production, TrustedHostMiddleware rejects requests whose Host header
# is not in ALLOWED_HOSTS.  Derive the healthcheck Host from the first
# ALLOWED_HOSTS entry so the probe is always authorized.  If ALLOWED_HOSTS
# is empty (dev/test), fall back to localhost.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD HOST=$(echo "$ALLOWED_HOSTS" | cut -d',' -f1 | tr -d ' ') && \
        [ -n "$HOST" ] || HOST=localhost && \
        curl -fsS -H "Host: $HOST" http://localhost:8000/health || exit 1

# Production command: no --reload, bounded workers.
# For a small VPS, 2 workers is conservative. Override via docker-compose.
# Use "python -m uvicorn" because pip --target installs packages but does
# not place console_scripts (like the uvicorn CLI) on PATH.
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
