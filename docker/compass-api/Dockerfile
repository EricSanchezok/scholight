# Academic Compass — API Server Docker Image
# ============================================
# This image only runs the FastAPI server (search, survey, auth, projects).
# The daily arXiv sync pipeline runs on the host instance — never containerized.
#
# Build:
#   DOCKER_BUILDKIT=1 docker compose build
#   (cloud-auth is installed from the local clone — no SSH forwarding needed)
#
# Run:
#   docker compose --env-file .env.production up -d

FROM python:3.11-slim-bookworm

# System deps: curl for healthcheck, ca-certificates for HTTPS
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ── uv (fast Python package manager) ────────────────────────────
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# ── Non-root user ───────────────────────────────────────────────
RUN useradd --create-home --uid 1000 compass
WORKDIR /app

# ── Dependency layer (cached unless pyproject.toml/uv.lock change) ──
# cloud-auth is declared as a git+ssh dependency in pyproject.toml.
# We install it from the local clone to avoid SSH forwarding in Docker.
# --no-install-package cloud-auth skips the git remote entirely;
# then uv pip install adds it from the local copy into the same venv.
COPY cloud-auth/ /app/cloud-auth/

COPY pyproject.toml uv.lock /app/

RUN uv sync --frozen --no-dev --no-install-package cloud-auth \
    && uv pip install /app/cloud-auth/

# ── Application source ──────────────────────────────────────────
COPY compass/ /app/compass/

# ── AWS RDS SSL certificate ─────────────────────────────────────
# Bundled at a fixed absolute path; override COMPASS_PG_SSL_ROOT_CERT
# only if you need a different cert.
COPY global-bundle.pem /etc/ssl/certs/global-bundle.pem

# ── Startup script ──────────────────────────────────────────────
COPY docker/compass-api/start.py /app/start.py

# ── Runtime ─────────────────────────────────────────────────────
USER compass
EXPOSE 8000

ENV COMPASS_SERVER_HOST=0.0.0.0
ENV COMPASS_SERVER_PORT=8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl --fail http://localhost:8000/health || exit 1

CMD ["uv", "run", "python", "/app/start.py"]
