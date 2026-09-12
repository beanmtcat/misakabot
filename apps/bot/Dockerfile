# syntax=docker/dockerfile:1
FROM python:3.12-slim

ARG APP_UID=10001
ARG APP_GID=10001

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    AUDIT_DATABASE_PATH=/app/data/misakabot.sqlite3

WORKDIR /app

# Keep the service unprivileged; SQLite data is persisted through /app/data.
RUN groupadd --system --gid "$APP_GID" misakabot \
    && useradd --system --uid "$APP_UID" --gid misakabot --home-dir /app misakabot \
    && mkdir -p /app/data \
    && chown -R misakabot:misakabot /app

COPY pyproject.toml ./
COPY src ./src
COPY tools ./tools
RUN pip install --no-cache-dir .

USER misakabot
VOLUME ["/app/data"]

# Default process: Telegram long-polling bot. To start the audit API instead,
# override this command with: misakabot-api
CMD ["misakabot"]
