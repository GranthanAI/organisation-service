# --- Build stage ---
FROM python:3.12-slim AS builder
WORKDIR /build
COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv \
    && UV_PROJECT_ENVIRONMENT=/opt/venv uv sync --frozen --no-dev --no-install-project

# --- Runtime stage ---
FROM python:3.12-slim AS runner
WORKDIR /app
RUN useradd --system --uid 10001 --no-create-home app
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" PYTHONUNBUFFERED=1
COPY app/ ./app/
COPY scripts/ ./scripts/
COPY alembic.ini* ./
USER app
EXPOSE 8010
# The same image runs the API; background workers are switched on with WORKERS_ENABLED.
# FORWARDED_ALLOW_IPS: proxies whose X-Forwarded-* headers are trusted (the gateway / LB).
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port 8010 --proxy-headers --forwarded-allow-ips ${FORWARDED_ALLOW_IPS:-127.0.0.1}"]
