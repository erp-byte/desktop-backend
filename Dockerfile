# ─────────────────────────────────────────────────────────────────────────
# App Runner–ready image for the FastAPI backend.
#
# Serves the ASGI app with uvicorn as a long-running container process — unlike
# Dockerfile.lambda, which packages the SAME app as an AWS Lambda handler via
# Mangum. Mirrors the ../../backend Docker setup: python:3.11-slim + pip +
# uvicorn (pinned to 3.11 — the known-good interpreter for this stack).
#
# On AWS App Runner:
#   • set the service "Port" to 8000 (or inject a PORT env var),
#   • provide runtime env vars (DATABASE_URL, JWT_SECRET, AUTH_ENCRYPTION_KEY,
#     PACKING_TOKEN_KEY, ANTHROPIC_API_KEY, …) in the service config — never
#     bake secrets into the image,
#   • health check path: /health.
# ─────────────────────────────────────────────────────────────────────────
FROM python:3.11-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# Install deps first so the layer caches across code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App package + master-data files (app/main.py's lifespan ingests ./data at boot).
COPY app/ ./app/
COPY data/ ./data/

EXPOSE 8000

# Shell form so a platform-injected ${PORT} is honoured, defaulting to 8000
# to match ../../backend. App Runner routes to whatever "Port" you configure.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
